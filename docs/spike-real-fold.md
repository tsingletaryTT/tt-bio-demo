# Spike: does a real fold behave the way the Phase 3 design assumes?

**Date:** 2026-08-11
**Branch:** `spike-real-fold`
**Status:** exploratory spike, complete. Not a plan, not production code.

## Why this exists

Phase 1–2 built the protocol, a mock runner, and the renderer against a **synthetic**
fixture (numpy noise converging to a straight line). Nobody had ever run a real tt-bio
fold with `dump_fn` attached — the design's central claims about it (`docs/superpowers/
specs/2026-08-10-tt-bio-demo-design.md` §4: *"tt-bio's diffusion sampler already exposes
`dump_fn(sample, step, coords)`... The runner taps this hook"*) came from reading the
function signature, not from running it. This spike runs a real fold on real hardware and
measures what actually happens, before anyone writes a Phase 3 plan on top of it.

Target: `~/code/tt-boltz/examples/trpcage_no_msa.yaml` (Trp-cage, 20 residues,
`msa: empty`), model `protenix-v2`, on the real device (4× p300c Blackhole).

Instruments (spike-only, not production code):
- [`tests/fixtures/streams/capture_real_fold.py`](../tests/fixtures/streams/capture_real_fold.py)
  — runs two real folds in one process, taps the trajectory, writes the raw capture and the
  wire-format fixture.
- [`tests/fixtures/streams/replay_real_fold_ui.py`](../tests/fixtures/streams/replay_real_fold_ui.py)
  — replays the captured fixture through the real, unmodified `MockRunner` and the real,
  unmodified `ui.app.DemoApp`, and proves something rendered via `glReadPixels`.

Everything below is measured from actual runs on this box today, not inferred from source
reading alone (though source reading — `~/code/tt-boltz/tt_bio/protenix.py` and
`tt_bio/opendde.py` — is how the *interpretation* of the numbers was checked).

**One caveat that applies to every timing number in this document:** the weights
(`~/.boltz/protenix-v2.pt`, 1.86 GB) and the CCD mol library (`mols.tar`, 1.85 GB) were
already downloaded, and tt-metal's kernel-compile cache
(`~/.cache/tt-metal-cache-tt-bio/ttnn-0.68.0`) was already warm, before this spike started
today. I could not personally time a genuinely cold first-run (fresh download + fresh
kernel JIT compile) without deleting shared caches on a machine other people use, which the
ground rules for this spike rule out. **This is a real gap, not a hidden one** — see
"What this means for Phase 3" below.

---

## Q1: Can a real fold run end to end?

**Yes.** Ran `capture_real_fold.py` under `.venvs/venv-runner/bin/python3` from the repo
root. Exit 0, twice (once to validate the artifact left by an earlier attempt at this same
spike, once fully first-hand with stdout/stderr captured):

```
[timing] weight resolution: 0.00s (path=/home/ttuser/.boltz/protenix-v2.pt)
[timing] feature build: 0.00s (20 residues)
[timing] model load (device open + weights): 2.45s
[timing] fold #1: wall=5.73s dump_fn calls=201 progress_fn calls=210 n_atoms=154
[timing] fold #2 (same process, model resident): wall=4.36s dump_fn calls=201 progress_fn calls=210 n_atoms=154
[check] fold #1 vs fold #2 coords allclose(atol=1e-2): True
[timing] total process wall clock: 14.12s
[cleanup] device closed
```

154 all-atom coordinates for a 20-residue heavy-atom (no hydrogens) protein is in the
expected range (~7.7 atoms/residue). `n_step=200`, `n_cycles=10` are tt-bio's own defaults
for `protenix-v2` — nothing was tuned down to make this easier.

## Q2: What does `dump_fn` actually give you?

This is the question the whole visual premise rests on, so it gets the most space.

### Shape, dtype, device, call count — measured directly

Every `dump_fn` call was captured with `coords.shape`, `coords.dtype`, `coords.device`,
`coords.requires_grad`:

```
shape=[1, 154, 3]  dtype=torch.float32  device=cpu  requires_grad=False
```
for **all 201 calls** of fold #1 (and fold #2). 201 = steps `-1..199`: the sampler calls
`dump_fn(-1, x)` once for the initial noise draw, then `dump_fn(k, x)` for
`k in range(200)` — one call per denoising step, no batching, no skipped steps. `sample`
does not vary here because `n_sample=1`; see the multi-sample caveat below.

**Reading `coords` costs essentially nothing.** Verified by reading
`tt_bio/protenix.py`'s `edm_sample` (line ~2062 onward): the outer sampling loop keeps `x`
as a **host** `torch` tensor the entire time — `x = sigmas[0] * torch.randn(shape)`, plain
CPU arithmetic between steps. All device (`ttnn`) computation happens *inside*
`diffusion_module.denoise(...)`, which is called, awaited, and returns a host tensor before
the loop's next line runs. So the `.detach().cpu()` call `dump_fn` receives its argument
through is a no-op (already on CPU); tapping `dump_fn` adds one Python callback and a
`.reshape(-1,3).astype(float32)` in numpy — microseconds, not a device round-trip.

### All-atom, internal ordering — confirmed, not assumed

154 atoms for 20 residues only makes sense as an all-heavy-atom representation in the
model's internal atom order (not a C-alpha trace, not per-residue). The design's assumption
that "reconstructing an atom-to-residue mapping from `dump_fn` live is fiddly" (§4.2) is
correct — nothing here reconstructs one for `dump_fn`; it is deliberately treated as an
undifferentiated point cloud, exactly as designed.

### Noise → structure: quantified, not assumed

Radius of gyration per step (mean distance of the 154 atoms from their own centroid),
computed from the real captured coordinates:

```
step=  -1  rg=4357.730     (initial noise draw, sigma[0]=160*16=2560-scale noise)
step=  19  rg=1132.578
step=  39  rg= 606.291
step=  59  rg= 286.894
step=  79  rg= 132.045
step=  99  rg=  48.283
step= 119  rg=  17.299
step= 139  rg=   8.150
step= 159  rg=   7.033
step= 179  rg=   6.986
step= 199  rg=   6.990
```

Radius of gyration falls **~620x**, monotonically, converging by roughly step 150 of 200
and staying flat (6.97–7.03) for the last ~50 steps. This is not a subtle effect — the
first frame is diffuse noise spanning thousands of length units and the last is a compact,
stable structure. The visual premise holds, strongly, and is reproducible: an earlier
capture (before this session) measured 4357.7 → 6.99 as well, to within noise.

### The signature the design assumed is wrong for this model

The design's §4 states: *"tt-bio's diffusion sampler already exposes
`dump_fn(sample, step, coords)`... The runner taps this hook."* This is **only true for
`OpenDDE.fold`**, not for `Protenix.fold` — and `protenix-v2` is the model the demo's own
playlist example uses (`docs/superpowers/specs/...design.md` §5, `model: protenix-v2`).

Read directly from `~/code/tt-boltz/tt_bio/protenix.py`:

```python
def fold(self, feats, *, n_step=200, n_sample=1, seed=None, progress_fn=None,
         return_confidence=False, n_cycles=None, trace=False,
         max_parallel_samples=None):
```

**There is no `dump_fn` parameter on `Protenix.fold` at all.** The per-step hook only
exists on the module-level function it calls internally:

```python
def edm_sample(diffusion_module, cond, n_atoms, *, multiplicity=1, ...,
               progress_fn=None, dump_fn=None):
    ...
    if dump_fn is not None:
        for _m in range(M):
            dump_fn(-1, x[_m:_m + 1].detach().cpu())   # step -1 == initial noise
    ...
    for k in range(n_step):
        ...
        if dump_fn is not None:
            for _m in range(M):
                dump_fn(k, x[_m:_m+1].detach().cpu())
```

`edm_sample`'s `dump_fn` is a **2-argument** callback: `dump_fn(step, coords)`, with no
sample index. Contrast `tt_bio/opendde.py`'s `OpenDDE.fold`, which *does* expose a public
`dump_fn` parameter, and which wraps it into the 3-argument shape the design assumed:

```python
_df = (lambda step, x: dump_fn(step, step, x)) if dump_fn is not None else None
...
_df = (lambda step, x, _k=k: dump_fn(_k, step, x)) if dump_fn is not None else None
```

So `dump_fn(sample, step, coords)` is a real, public, stable contract for `OpenDDE` — and
not reachable at all through `Protenix.fold`'s public API. This spike's capture script had
to monkeypatch the **module-level** `tt_bio.protenix.edm_sample` before calling
`Protenix.fold(...)`, exactly the way one would patch a private implementation detail, not
call a documented hook:

```python
_orig_edm_sample = ptx.edm_sample
def _edm_sample_patched(*args, **kwargs):
    kwargs.setdefault("dump_fn", _dump_fn)
    return _orig_edm_sample(*args, **kwargs)
ptx.edm_sample = _edm_sample_patched
```

This worked, and worked reliably (two folds, both fully captured). But it is coupled to
tt-bio's internal module structure, not to any documented API, and it will break silently
if tt-bio ever renames/inlines/refactors `edm_sample`. See "What this means for Phase 3."

## Q3: Timing and residency

Breakdown of fold #1's 5.73 s wall time, derived from `progress_fn` timestamps (10 trunk
calls, 200 diffusion calls) and `dump_fn` timestamps:

| Phase | Time | Detail |
|---|---|---|
| weight resolution | ~0 s | already cached this session (see caveat above) |
| feature build | 2 ms | 20 residues, no MSA |
| model load (device open + checkpoint upload) | 2.45 s | |
| trunk (10 recycling cycles) | 1.27 s | ~142 ms/cycle |
| *(gap: diffusion conditioning setup)* | 0.71 s | pair-z conditioning, atom cache, DiT upload |
| diffusion (200 steps) | 3.42 s | ~17 ms/step |
| confidence head + return | ~0.24 s | (residual: 5.73 − 1.27 − 0.71 − 3.42 − model_load's own 0 overlap) |
| **fold #1 total** | **5.73 s** | |
| **fold #2, same process, model resident** | **4.36 s** | ~24% faster than fold #1 |
| process total (imports + both folds + cif write + cleanup) | 14.12 s | |

**Residency works.** A second `Protenix.fold()` call in the same process, same model
object, ran successfully and was ~1.4 s faster than the first (no repeated weight upload,
warm program cache). `np.allclose(coords1, coords2, atol=1e-2)` was `True` with the same
seed — the two independent samples land in the same basin, as `edm_sample`'s own docstring
promises for repeated seeded runs. This supports the design's "one worker per card, models
resident" plan (§2).

**What was not measured:** genuine cold start. `model_load_s=2.45s` is device-open plus
weight upload with a warm kernel-compile cache; a first-ever run on a fresh machine would
also pay tt-metal's kernel JIT compile time, which this spike could not isolate without
clearing a cache other people rely on. Budget for this being materially slower on a
freshly-imaged booth machine until it is measured directly.

## Q4: What does the progress/stage information look like?

The design's protocol table (§3) assumes six stage values: `msa`/`prep`/`trunk`/
`diffusion`/`confidence`/`saving`. Measured from real `progress_fn` calls during the real
fold, and cross-checked against every `progress_fn(...)` call site in
`tt_bio/protenix.py` (there are exactly two):

```python
progress_fn("trunk", step=cyc, total=n_cycles)        # 10 calls, total=10
progress_fn("diffusion", step=k, total=n_step)         # 200 calls, total=200
```

**tt-bio itself only ever reports `trunk` and `diffusion`.** There is no `msa`, `prep`,
`confidence`, or `saving` stage anywhere in `tt_bio.protenix`'s `progress_fn` calls — those
four would have to be synthesized entirely by the runner daemon (bracketing calls before
MSA/feature loading, before/after `fold()`, and around the `.cif` write), not read from
tt-bio's own instrumentation. This is a real design gap, not a naming nit: the design's
component table (§2) says `runner/folder.py` "invokes tt-bio, holds models resident, taps
`dump_fn`" as if `stage` were similarly just tapped — it is not; `stage` is two-thirds
synthetic from tt-bio's perspective.

## Q5: Failure modes

**stderr noise, every run, not just cold-start:**
- `ttnn`'s import always prints one DEBUG line dumping its full `Config{...}` (cache paths,
  including a *relative* `root_report_path=generated/ttnn/reports`).
- Every `get_device()` call prints ~40 lines of `UMD`/`Metal`/`Fabric`/`BuildKernels` INFO
  logs (topology discovery, per-chip harvesting masks, IOMMU setup, and one recurring
  warning: *"Firmware bundle version 19.11.0 ... is newer than the latest fully tested
  version 19.5.0 for blackhole architecture"*). This is not a one-time cost; it happens on
  every device open, so a daemon that opens a device once at startup pays it once, but one
  that reopens per-job would pay it repeatedly.
- `nanobind` (ttnn's C++ binding layer) sometimes dumps hundreds of lines of
  "leaked instance/type/function" warnings to stderr at interpreter exit. Observed
  **absent** in the full capture run (which calls `tt_bio.tenstorrent.cleanup()` before
  exit) but **present** in a bare `import tt_bio, torch, ttnn` + exit with no device ever
  opened, and present again in one other quick open/close test. The pattern is not fully
  understood — cleanup() looks like it usually helps, but not reliably enough to promise a
  clean shutdown log purely from calling it. A booth's log rotation/alerting should not
  treat this dump as an error signal.

**Stray files — real and substantial, contradicting a narrower prior finding.** A bare
`import ttnn` with no device opened does **not** create a `generated/` directory in the
CWD (verified: none appeared after a bare-import test). But actually opening a device and
running kernels does, via tt-metal's separate Inspector/Watcher subsystems (found in
`libtt_metal.so` via `strings`: `TT_METAL_INSPECTOR`, `TT_METAL_INSPECTOR_LOG_PATH`,
`TT_METAL_WATCHER`, ...):

| Action | `generated/` size |
|---|---|
| bare `import ttnn`, no device opened | none created |
| `get_device()` + `cleanup()`, no fold | 40 KB |
| two full 200-step folds (this spike's capture run) | **121 MB** (`generated/inspector/mesh_workloads_log.yaml` alone: 125 MB before final flush accounting) |

This scales with op/step count, lands **relative to the daemon's CWD** (not a fixed system
path), and is unbounded — a real disk-fill risk for a daemon that runs continuously all
day at a booth from one fixed working directory.

**A landmine while investigating the above: `TT_METAL_WATCHER=0` does not mean "disabled."**
Setting it hung the process — `get_device()` never returned within a 2-minute timeout,
spinning on `"Watcher checking device N"` log lines emitted continuously (a busy-poll, not
a crash). Had to be force-killed. No lasting damage — verified afterward that `tt-smi -s`
and a fresh plain `get_device()`/`cleanup()` still worked normally, and no process was left
behind. But this means whatever env var Phase 3 uses to control Inspector/Watcher output
needs its actual on/off semantics verified directly, not assumed from the name — `0` looks
like "off" and is not.

**Device cleanup and reuse:** `tt_bio.tenstorrent.cleanup()` closes the device cleanly;
the device was successfully reopened and closed again afterward with no lingering state,
including after the `TT_METAL_WATCHER=0` hang above was killed. No stray processes were
left by any run in this spike (checked with `ps aux` after each step).

---

## The artifact

[`tests/fixtures/streams/real_fold_trpcage.jsonl`](../tests/fixtures/streams/real_fold_trpcage.jsonl)
— 35 events (`hello`, `job_start`, `stage:trunk`, 30×`frame`, `stage:confidence`,
`job_done`), subsampled from the real 201-call trajectory to the design's ~30-frame target,
matching the existing wire format exactly (same event types/fields as
`tests/fixtures/streams/short_fold.jsonl`; built with `protocol.events.pack_coords`).
`_delay_ms` on each frame is the real measured inter-`dump_fn`-call gap, so replay at
`speed=1.0` runs at true recorded pace. The companion `.cif`
([`tests/fixtures/structures/real_fold_trpcage.cif`](../tests/fixtures/structures/real_fold_trpcage.cif),
154 `ATOM` records) is the real folded structure, written by tt-bio's own
`_write_protenix_structure`, B-factor column populated from real per-atom pLDDT ×100.

**Note on `mean_plddt`'s scale**, worth flagging precisely because it is easy to get
wrong silently: tt-bio's own result dict reports `plddt` as a **0–1 fraction**
(`tt_bio/worker.py`: `"plddt": round(c["plddt"], 6)`), while the project's own established
wire convention (`short_fold.jsonl`: `"mean_plddt":82.4`) is **0–100**. The fixture's
`job_done.mean_plddt: 95.27` was deliberately scaled by 100 to match the existing
convention, not copied raw from tt-bio.

Raw per-call capture (shape/dtype/device/timing/rg for all 201 steps of both folds) is in
[`tests/fixtures/streams/real_fold_trpcage.raw.json`](../tests/fixtures/streams/real_fold_trpcage.raw.json),
kept alongside the wire fixture as the evidence trail for the numbers above.

### Proved it plays: replay through the real MockRunner and the real UI

[`tests/fixtures/streams/replay_real_fold_ui.py`](../tests/fixtures/streams/replay_real_fold_ui.py)
starts the real, unmodified `runner.mock.MockRunner` serving the fixture over a Unix
socket, constructs the real, unmodified `ui.app.DemoApp` pointed at it, and monkeypatches
only `StructureViewer._on_render` (bound at the class, not edited in `ui/viewer.py`) to
also call `glReadPixels` after each real render and record whether/where non-background
pixels appear. Run under `.venvs/venv-ui/bin/python3` against the live Wayland display
(`WAYLAND_DISPLAY=wayland-0`):

```
render samples with non-background pixels: 218 / 222
frac_content range: 0.0010 .. 0.2259

evenly-spaced trace (10 samples across the run):
    t=  0.00s  frac_content=0.0085  bbox=[441, 153, 953, 765]
    t=  0.95s  frac_content=0.0065  bbox=[567, 314, 732, 492]
    t=  1.88s  frac_content=0.0026  bbox=[608, 369, 675, 436]
    t=  2.82s  frac_content=0.0020  bbox=[610, 366, 668, 425]
    t=  3.76s  frac_content=0.1893  bbox=[351, 120, 1027, 734]   <- ribbon reveal
    t=  4.74s  frac_content=0.0084  bbox=[388, 127, 908, 759]    <- loop restarts
    t=  5.68s  frac_content=0.0061  bbox=[564, 311, 719, 492]
    t=  6.63s  frac_content=0.0024  bbox=[611, 365, 671, 436]
    t=  7.57s  frac_content=0.0021  bbox=[597, 374, 670, 425]
    t=  8.52s  frac_content=0.2228  bbox=[223, 81, 1017, 691]    <- ribbon reveal again
RESULT: PASS
```

This is the point cloud's on-screen bounding box **collapsing** from a 512×612 px spread
down to a tight 67×70 px cluster over ~2.8 seconds (the real noise→structure convergence,
now visible), followed by a large-area, high-`frac_content` frame consistent with the
ribbon reveal (`ribbon_from_cif` successfully parsed the real captured `.cif` — no
`GeometryError` was logged). The cycle repeats at ~4.75 s intervals: `ui/client.py`'s
`EventClient` reconnects (`reconnect_delay=1.0`) once `MockRunner` finishes serving the
fixture and replays it again from `hello` — so this fixture, unmodified, loops
continuously in the UI exactly like a real Attract-mode target would. All 83 existing
unit tests still pass (`scripts/test.sh`, unaffected by anything in this spike).

---

## What this means for the Phase 3 design

1. **`Protenix.fold` has no public `dump_fn`.** The design's framing — "tt-bio already
   exposes `dump_fn(sample, step, coords)`; the runner taps this hook" — is true for
   `OpenDDE` and **false** for `Protenix`, which is the family the demo's own playlist
   example (`model: protenix-v2`) uses. Getting a trajectory out of a real `Protenix.fold`
   call requires monkeypatching the module-level `tt_bio.protenix.edm_sample` before
   calling `fold()`, as this spike's capture script does. That is coupling to an
   implementation detail, not a documented API — it should either be raised upstream with
   tt-bio (add a real `dump_fn` parameter to `Protenix.fold`, mirroring `OpenDDE.fold`) or
   explicitly owned and version-pinned as vendored-monkeypatch code in
   `runner/folder.py`, with a test that fails loudly if `edm_sample` moves or is renamed in
   a tt-bio upgrade. Either way, this needs a decision Phase 3's plan does not currently
   make.

2. **The `stage` vocabulary is mostly synthetic.** tt-bio's `progress_fn` only ever emits
   `trunk` and `diffusion` for Protenix. `msa`, `prep`, `confidence`, and `saving` — three
   of the six values the protocol table promises — do not come from tt-bio at all and must
   be emitted by `runner/folder.py` itself, bracketing the calls it makes around `fold()`.
   This is buildable, just not "tapped" the way trunk/diffusion are.

3. **pLDDT needs an explicit ×100 scale**, or `job_done.mean_plddt` will silently report
   0.95 instead of 95 the first time someone wires tt-bio's raw `conf["plddt"]` straight
   into the wire event.

4. **The core visual premise is solid and should not change.** Radius of gyration fell
   ~620x, monotonically, and the collapse is visible on screen (glReadPixels bounding box:
   512×612 px → 67×70 px) before the ribbon reveal. Nothing here suggests changing the
   point-cloud → ribbon handoff design.

5. **Tapping `dump_fn` is cheap and does not need throttling for cost reasons.** Coordinates
   are already host `torch` tensors between steps (device compute happens inside
   `denoise()` and returns before the loop continues); the existing plan to subsample to
   ~30 frames is about socket/render bandwidth, not about protecting the sampler from an
   expensive per-step device→host copy — there isn't one to protect against.

6. **`generated/` growth is a real, unbounded disk-fill risk the design does not currently
   address.** Two folds of 200 steps each produced 121 MB of Inspector/Watcher logs,
   relative to whatever the daemon's CWD happens to be. A booth running continuously for a
   full day, one fold every ~45 s per the design's own `expected_s` figures, could
   plausibly generate gigabytes. Phase 3 needs either a fixed, absolute
   `TT_METAL_INSPECTOR_LOG_PATH` with rotation, or a verified way to disable these
   subsystems — and should verify the *actual* semantics of whatever env var it picks
   first: `TT_METAL_WATCHER=0` hung this box for two minutes rather than disabling
   anything.

7. **Device-open stderr (~40 lines of INFO) happens on every `get_device()` call, not just
   cold start.** Harmless if logged to a file as the design's systemd unit implies, but
   should not be misread as anomalous during ops review, and argues for opening the device
   exactly once per daemon lifetime (which the design's "models resident" plan already
   intends).

8. **Cold-start timing is still unmeasured and should be budgeted, not assumed.**
   [**Resolved 2026-08-17** — measured at last: 94.5 s cold against 9.4 s warm for Trp-cage,
   ~83 s of it kernel compilation. See `docs/cold-start.md`. The rest of this point stands as
   written at the time.] Every
   number in this report (2.45 s model load, 5.7 s first fold) was measured with a warm
   weight cache and a warm kernel-compile cache. A fresh-imaged booth machine's real
   first-run cost (≥3.65 GB of downloads plus first-time kernel JIT compilation) is
   unknown and could matter a great deal for the "goes from imaged to running demo" success
   criterion (§1) — this is the most important follow-up measurement before Phase 3 ships,
   and should be done on a machine whose caches can legitimately be cleared.

9. **Residency and the wire format both hold up.** A second fold in the same process
   worked, was ~24% faster, and reproduced the first fold's coordinates
   (`atol=1e-2`) — supports "one worker per card, models resident." And the real captured
   trajectory replayed through the entirely unmodified `MockRunner` and `DemoApp` with no
   code changes and no test failures — the protocol and the consumer code do not need to
   change shape to accept a real trajectory, only the producer side (`runner/folder.py`,
   not yet built) needs to account for points 1–3 and 6–8 above.
