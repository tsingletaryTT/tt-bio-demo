#!/usr/bin/env python3
"""Minimal before/after reproducer for the Protenix.fold(dump_fn=...) patch.

BEFORE this patch, the only way to observe Protenix's per-step diffusion
trajectory was to monkeypatch the module-level tt_bio.protenix.edm_sample
before calling fold() -- coupling to a private implementation detail:

    import tt_bio.protenix as ptx
    _orig = ptx.edm_sample
    def _patched(*a, **kw):
        kw["dump_fn"] = my_callback          # note: must FORCE-override, see below
        return _orig(*a, **kw)
    ptx.edm_sample = _patched
    coords, conf = model.fold(feats, ...)     # no dump_fn kwarg exists to pass
    ptx.edm_sample = _orig

AFTER this patch, it is one keyword argument, exactly like OpenDDE.fold
already supported:

    coords, conf = model.fold(feats, ..., dump_fn=my_callback)

This script runs the AFTER path for real, on hardware, against the same
20-residue no-MSA target used by docs/spike-real-fold.md, and prints the
radius-of-gyration collapse that the demo's visual premise depends on.

Run with:
  .venvs/venv-runner/bin/python3 docs/upstream/protenix-dump-fn/reproduce.py
(from the tt-bio-demo repo root, against a tt-bio checkout with the patch
applied -- see README.md in this directory for how to apply it.)
"""
import pathlib
import sys

import numpy as np

TT_BOLTZ = pathlib.Path.home() / "code" / "tt-boltz"
EXAMPLE_YAML = TT_BOLTZ / "examples" / "trpcage_no_msa.yaml"


def radius_of_gyration(coords):
    c = coords - coords.mean(axis=0, keepdims=True)
    return float(np.sqrt((c ** 2).sum(axis=1).mean()))


def main():
    import torch  # noqa: F401
    from tt_bio.main import PROTENIX_REPO, download_mols, hf_artifact
    from tt_bio.main import _read_bio_chains, _read_bio_constraints, _resolve_a3m_text
    from tt_bio.protenix_data import build_complex_features
    from tt_bio.protenix import Protenix

    cache = pathlib.Path("~/.boltz").expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    ckpt_path = hf_artifact(PROTENIX_REPO, "protenix-v2.pt", cache)

    chains = _read_bio_chains(EXAMPLE_YAML)
    bonds = _read_bio_constraints(EXAMPLE_YAML)
    chain_specs = [(cseq, _resolve_a3m_text(spec, cseq, None), mt)
                   for _cid, cseq, spec, mt in chains]
    feats = build_complex_features(
        chain_specs, mol_dir=str(download_mols(cache)),
        chain_ids=[cid for cid, _s, _sp, _mt in chains], bonds=bonds)

    print("Opening device, loading protenix-v2...", file=sys.stderr)
    model = Protenix.load_from_checkpoint(str(ckpt_path))

    trajectory = []

    def on_step(sample, step, coords):
        # sample: which of n_sample draws (always 0 here, n_sample=1)
        # step:   -1 for the initial noise draw, then 0..n_step-1
        # coords: (1, N_atom, 3) host torch.float32 tensor -- no device round-trip
        trajectory.append((step, radius_of_gyration(
            coords.detach().cpu().numpy().reshape(-1, 3))))

    coords, conf = model.fold(
        feats, n_step=200, n_sample=1, seed=0, n_cycles=10,
        return_confidence=True, dump_fn=on_step)   # <-- the new parameter

    print(f"\n{len(trajectory)} dump_fn calls (expected 201: step -1 + 200 steps)")
    print("radius of gyration, noise -> structure:")
    for step, rg in trajectory[:3]:
        print(f"  step={step:4d}  rg={rg:9.3f}")
    print("  ...")
    for step, rg in trajectory[-3:]:
        print(f"  step={step:4d}  rg={rg:9.3f}")
    print(f"\nfinal mean pLDDT: {conf['plddt'] * 100:.1f}")

    from tt_bio.tenstorrent import cleanup
    cleanup()


if __name__ == "__main__":
    main()
