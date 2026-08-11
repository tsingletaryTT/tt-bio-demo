"""Spike script: run a REAL tt-bio protenix-v2 fold on hardware and capture the
dump_fn trajectory, timing, and progress_fn events.

This is deliberately NOT production code -- it is the instrument used by
docs/spike-real-fold.md to answer "what does dump_fn actually give you?"
empirically, instead of from reading the signature. It:

  1. Loads Protenix-v2 (downloads weights on first use), opens a real device.
  2. Builds features for examples/trpcage_no_msa.yaml (20 residues, no MSA).
  3. Monkeypatches tt_bio.protenix.edm_sample so a dump_fn is threaded through
     even though tt_bio.protenix.Protenix.fold's public signature does not
     accept one (see docs/spike-real-fold.md Q2 for why this is necessary).
  4. Runs the fold, recording every dump_fn call (shape/dtype/device/wall
     time) and every progress_fn call (stage/step/total/wall time).
  5. Runs a SECOND fold in the same process (model still resident) to test
     residency.
  6. Writes:
       - a raw per-step trajectory + timing dump (JSON) for analysis
       - the subsampled wire-format fixture tests/fixtures/streams/real_fold_trpcage.jsonl

Run with the tt-bio venv, from any writable cwd:
  .venvs/venv-runner/bin/python3 tests/fixtures/streams/capture_real_fold.py
"""

import json
import pathlib
import sys
import time

import numpy as np

# protocol.events must be importable; add the repo root (this file's
# grandparent's grandparent) to sys.path exactly like make_short_fold.py does.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from protocol.events import PROTOCOL_VERSION, pack_coords  # noqa: E402

TT_BOLTZ = pathlib.Path.home() / "code" / "tt-boltz"
EXAMPLE_YAML = TT_BOLTZ / "examples" / "trpcage_no_msa.yaml"

OUT_RAW = pathlib.Path(__file__).with_name("real_fold_trpcage.raw.json")
OUT_JSONL = pathlib.Path(__file__).with_name("real_fold_trpcage.jsonl")

N_STEP = 200        # tt-bio's own default for protenix-v2 (--sampling_steps unset)
N_CYCLES = 10       # tt-bio's own default for protenix-v2 (--recycling_steps unset)
TARGET_FRAMES = 30  # design's stated subsample target


def radius_of_gyration(coords):
    """Mean distance of atoms from their centroid, in the model's native units."""
    c = coords - coords.mean(axis=0, keepdims=True)
    return float(np.sqrt((c ** 2).sum(axis=1).mean()))


def main():
    t_process_start = time.perf_counter()

    import torch  # noqa: F401  (imported for parity with tt_bio's own import order)
    import tt_bio.protenix as ptx
    from tt_bio.main import PROTENIX_REPO, download_mols, hf_artifact
    from tt_bio.main import _read_bio_chains, _read_bio_constraints, _resolve_a3m_text
    from tt_bio.protenix_data import build_complex_features
    from tt_bio.protenix import Protenix

    cache = pathlib.Path("~/.boltz").expanduser()
    cache.mkdir(parents=True, exist_ok=True)

    # ---- Q1/Q5: weight resolution (downloads on first use if not cached) ----
    t0 = time.perf_counter()
    ckpt_path = hf_artifact(PROTENIX_REPO, "protenix-v2.pt", cache)
    t_weights = time.perf_counter() - t0
    print(f"[timing] weight resolution: {t_weights:.2f}s (path={ckpt_path})",
          file=sys.stderr)

    # ---- feature building (no MSA -- msa: empty in the yaml) ----
    t0 = time.perf_counter()
    chains = _read_bio_chains(EXAMPLE_YAML)
    bonds = _read_bio_constraints(EXAMPLE_YAML)
    chain_specs = [(cseq, _resolve_a3m_text(spec, cseq, None), mt)
                   for _cid, cseq, spec, mt in chains]
    feats = build_complex_features(
        chain_specs, mol_dir=str(download_mols(cache)),
        chain_ids=[cid for cid, _s, _sp, _mt in chains], bonds=bonds)
    t_feat = time.perf_counter() - t0
    n_residues = sum(len(cseq) for _c, cseq, _s, mt in chains if mt != "ligand")
    print(f"[timing] feature build: {t_feat:.2f}s ({n_residues} residues)",
          file=sys.stderr)

    # ---- Q3: model load (device open + weight upload) ----
    t0 = time.perf_counter()
    model = Protenix.load_from_checkpoint(str(ckpt_path))
    t_load = time.perf_counter() - t0
    print(f"[timing] model load (device open + weights): {t_load:.2f}s",
          file=sys.stderr)

    # ---- Q2: monkeypatch edm_sample so dump_fn is threaded through ----
    # tt_bio.protenix.Protenix.fold calls the module-level name `edm_sample`
    # directly and does NOT expose a dump_fn parameter on its own signature
    # (verified by reading the source, not assumed) -- so the only way to
    # observe the per-step trajectory through the real Protenix.fold() call
    # path is to intercept the lower-level sampler it delegates to.
    _orig_edm_sample = ptx.edm_sample
    trajectory = []       # one entry per dump_fn call
    progress_events = []  # one entry per progress_fn call

    def _dump_fn(step, coords):
        now = time.perf_counter()
        arr = coords.detach().cpu().numpy().astype(np.float32).reshape(-1, 3)
        trajectory.append({
            "step": step,
            "t": now,
            "shape": list(coords.shape),
            "dtype": str(coords.dtype),
            "device": str(coords.device),
            "requires_grad": bool(coords.requires_grad),
            "coords": arr,
        })

    def _edm_sample_patched(*args, **kwargs):
        kwargs.setdefault("dump_fn", _dump_fn)
        return _orig_edm_sample(*args, **kwargs)

    ptx.edm_sample = _edm_sample_patched

    def _progress_fn(stage, step=0, total=0):
        progress_events.append({"stage": stage, "step": step, "total": total,
                                 "t": time.perf_counter()})

    def run_one_fold(label):
        trajectory.clear()
        progress_events.clear()
        t0 = time.perf_counter()
        coords, conf = model.fold(
            feats, n_step=N_STEP, n_sample=1, seed=0, progress_fn=_progress_fn,
            n_cycles=N_CYCLES, return_confidence=True)
        wall = time.perf_counter() - t0
        print(f"[timing] {label}: wall={wall:.2f}s dump_fn calls={len(trajectory)} "
              f"progress_fn calls={len(progress_events)} n_atoms={coords.shape[1]}",
              file=sys.stderr)
        return coords, conf, wall

    # ---- Fold #1 (cold-ish: first fold this process has run) ----
    coords1, conf1, wall1 = run_one_fold("fold #1")
    traj1 = [dict(e) for e in trajectory]           # snapshot before fold #2 clears it
    prog1 = [dict(e) for e in progress_events]

    # ---- Q3: residency -- does a second fold work in the same process? ----
    coords2, conf2, wall2 = run_one_fold("fold #2 (same process, model resident)")
    traj2 = [dict(e) for e in trajectory]

    # Sanity: same seed should reproduce closely (bit-for-bit not guaranteed
    # across two independent sampler invocations sharing global torch RNG
    # state differently, but should be very close for a deterministic device
    # pipeline with seed=0 each time).
    coords_close = bool(np.allclose(
        coords1.numpy(), coords2.numpy(), atol=1e-2, rtol=1e-2))
    print(f"[check] fold #1 vs fold #2 coords allclose(atol=1e-2): {coords_close}",
          file=sys.stderr)

    # ---- Write the .cif for fold #1 so job_done can reference a real structure ----
    from tt_bio.main import _write_protenix_structure
    struct_dir = pathlib.Path(__file__).parent
    cif_path = struct_dir.parent / "structures" / "real_fold_trpcage.cif"
    cif_path.parent.mkdir(parents=True, exist_ok=True)
    _write_protenix_structure(coords1[0], feats, None, cif_path, "cif",
                               b_factors=conf1["plddt_atom"] * 100.0)
    print(f"[write] {cif_path}", file=sys.stderr)

    # ---- Q2: quantify noise -> structure convergence via radius of gyration ----
    for e in traj1:
        e["rg"] = radius_of_gyration(e["coords"])
    rg_series = [(e["step"], e["rg"]) for e in traj1]
    print("[rg] step -> radius_of_gyration (first 5, last 5):", file=sys.stderr)
    for s, rg in rg_series[:5]:
        print(f"    step={s:4d} rg={rg:8.3f}", file=sys.stderr)
    print("    ...", file=sys.stderr)
    for s, rg in rg_series[-5:]:
        print(f"    step={s:4d} rg={rg:8.3f}", file=sys.stderr)

    t_process_total = time.perf_counter() - t_process_start
    print(f"[timing] total process wall clock: {t_process_total:.2f}s", file=sys.stderr)

    # ---- Persist the raw capture (everything, for the report) ----
    raw = {
        "n_step": N_STEP, "n_cycles": N_CYCLES, "n_residues": n_residues,
        "n_atoms": int(coords1.shape[1]),
        "timing": {
            "weight_resolution_s": t_weights, "feature_build_s": t_feat,
            "model_load_s": t_load, "fold1_wall_s": wall1, "fold2_wall_s": wall2,
            "process_total_s": t_process_total,
        },
        "fold1_progress_events": prog1,
        "fold1_dump_fn_calls": len(traj1),
        "fold2_dump_fn_calls": len(traj2),
        "coords1_vs_coords2_allclose": coords_close,
        "fold1_trajectory_meta": [
            {"step": e["step"], "t": e["t"], "shape": e["shape"], "dtype": e["dtype"],
             "device": e["device"], "requires_grad": e["requires_grad"], "rg": e["rg"]}
            for e in traj1
        ],
    }
    OUT_RAW.write_text(json.dumps(raw, indent=2))
    print(f"[write] {OUT_RAW}", file=sys.stderr)

    # ---- Build the wire-format JSONL fixture, subsampled to ~TARGET_FRAMES ----
    write_fixture(traj1, prog1, wall1, n_residues, int(coords1.shape[1]),
                   conf1, cif_path)

    # ---- Clean device shutdown ----
    from tt_bio.tenstorrent import cleanup
    cleanup()
    print("[cleanup] device closed", file=sys.stderr)


def write_fixture(traj, progress_events, wall_s, n_residues, n_atoms, conf, cif_path):
    """Subsample the captured trajectory to ~TARGET_FRAMES frames and emit the
    project's wire format (hello/job_start/stage/frame x N/stage/job_done),
    with _delay_ms set from the REAL measured inter-frame timing so replay
    runs at true pace."""
    steps = [e["step"] for e in traj]
    n = len(traj)
    if n <= TARGET_FRAMES:
        picked = list(range(n))
    else:
        picked = sorted(set(int(round(i * (n - 1) / (TARGET_FRAMES - 1)))
                             for i in range(TARGET_FRAMES)))

    t0 = traj[0]["t"]
    events = [
        {"type": "hello", "version": PROTOCOL_VERSION, "cards": [0, 1, 2, 3],
         "models": ["protenix-v2"], "preflight": "ok", "_delay_ms": 0},
        {"type": "job_start", "job_id": "real1", "target_id": "trpcage_no_msa",
         "model": "protenix-v2", "card": 0, "n_residues": n_residues, "_delay_ms": 50},
    ]

    # Stage events observed from the real progress_fn stream: tt-bio itself
    # only ever reports "trunk" and "diffusion" (see docs/spike-real-fold.md
    # Q4) -- there is no model-side "confidence"/"saving" progress. We insert
    # one synthetic "trunk" stage marker before the frames (matching what the
    # real run reported at its last trunk progress event) since the mock
    # protocol's existing consumers expect a stage before frames.
    trunk_events = [p for p in progress_events if p["stage"] == "trunk"]
    if trunk_events:
        events.append({"type": "stage", "job_id": "real1", "stage": "trunk",
                        "frac": 0.3, "_delay_ms": 50})

    prev_t = t0
    for idx in picked:
        e = traj[idx]
        delay_ms = max(0, int(round((e["t"] - prev_t) * 1000)))
        prev_t = e["t"]
        events.append({
            "type": "frame", "job_id": "real1", "step": max(0, e["step"] + 1),
            "total": N_STEP, "n_atoms": n_atoms,
            "coords_b64": pack_coords(e["coords"]), "_delay_ms": delay_ms,
        })

    events += [
        {"type": "stage", "job_id": "real1", "stage": "confidence", "frac": 0.95,
         "_delay_ms": 50},
        {"type": "job_done", "job_id": "real1",
         "cif_path": "tests/fixtures/structures/real_fold_trpcage.cif",
         "wall_s": round(wall_s, 3),
         # confidence_head.confidence() returns plddt as a 0-1 fraction (see
         # b_factors=... * 100.0 above and worker.py's _row(), which reports
         # it unscaled) -- but the existing short_fold.jsonl fixture (and
         # thus the wire convention this project has established) uses a
         # 0-100 scale (82.4). Scale here for consistency; see
         # docs/spike-real-fold.md Q4/"what this means" for why the model's
         # own units and the protocol's established units disagree.
         "mean_plddt": round(float(conf["plddt"]) * 100.0, 2),
         "_delay_ms": 50},
    ]

    OUT_JSONL.write_text("".join(
        json.dumps(e, separators=(",", ":")) + "\n" for e in events))
    print(f"[write] {OUT_JSONL} ({len(events)} events, {len(picked)} frames "
          f"subsampled from {n} real dump_fn calls)", file=sys.stderr)


if __name__ == "__main__":
    main()
