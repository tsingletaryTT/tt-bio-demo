# Spike: protenix-v1 on tt-bio 0.15.2

Run on 2026-10-13, chip 2 of the QB2 (board 0000046131924055) under gozer lease
`1fab5e`, with `TT_VISIBLE_DEVICES=0000:03:00.0`. The spike script stayed in the
scratchpad. It used the booth's own `runner.folder.Folder(model="protenix-v1")`,
with the `load()` model-id check patched in memory only. No repo code changed.

## Setup

`pip install tt-bio==0.15.2` into `.venvs/venv-runner` replaced 0.12.0.
`pip show` reports tt-bio 0.15.2 and ttnn 0.68.0. ttnn did not change, so the
vendored SFPI toolchain needs no work. `scripts/setup-venvs.sh` was not run.

## (a) The v1 weights file

    tt-bio weights --download protenix-v1 --cache ~/.boltz
    protenix-v1  protenix-v1  huggingface.co+1  present  1.37G  /home/ttuser/.boltz/protenix/model_v0.5.0.pt

- Path: `~/.boltz/protenix/model_v0.5.0.pt` (a `protenix/` subdirectory, not a
  flat `protenix-v1.pt`).
- Size: 1474265486 bytes (1.37 GiB).
- sha256: `9ea20b0aba42f2256711da1d0cd081510a4b291e64375bff6b70ced70b87a5f1`
  (matches the expected value).
- `weights.resolve("protenix-v1", "/tmp/x")` returns `/tmp/x/protenix/model_v0.5.0.pt`.
- `weights.artifacts_for("protenix-v1")` returns `['mols', 'protenix-v1']`. The
  order is mols first. Code that indexes this list by position needs to know.
- The old `~/.boltz/protenix-v2.pt` (1859785497 bytes) is untouched and still
  reported by `tt-bio weights` as `protenix-v2  manual  present  1.73G`. tt-bio
  no longer downloads it (source is "manual") but still recognises a file that is
  already there.

## (b) A real 200-step Trp-cage fold on v1 streams frames

Input `examples/trpcage_no_msa.yaml`, `n_step=200`, three consecutive folds in
one resident process (log: `spike_v1.log` in the scratchpad):

    load_s 5.73
    fold 0: frames=30 first_frame=52.50 total_s=94.23 plddt=93.50 finite=True stages=['confidence', 'diffusion', 'msa', 'prep', 'saving', 'trunk']
    fold 1: frames=30 first_frame=0.39 total_s=4.07 plddt=93.52 finite=True stages=[...same...]
    fold 2: frames=30 first_frame=0.40 total_s=3.98 plddt=93.50 finite=True stages=[...same...]

- Frames: 30 per fold through `runner.dump_tap`, the same count the v2 passes
  recorded. No traceback.
- Mean pLDDT 93.50 to 93.52. The v2 booth number is 95.3.
- Warm fold time is about 4.0 s (v2 on 0.8.0 was 4.6 s).
- Cold versus warm: the first fold of v1 took 94.23 s, and its first frame came
  at 52.5 s. The cost is JIT kernel compilation for shapes this chip had not
  compiled for v1 on 0.15.2. The second and third folds took 4.07 s and 3.98 s,
  with the first frame at 0.4 s. Any timing run must discard the first fold, and
  a venue should warm every target once after install or a board reset.
- Model load took 5.73 s (the weights file was already on disk and in page cache).
- v1 runs 4 trunk cycles by default (v2 runs 10). The checkpoint sets this, and
  tt-bio's `RECYCLING_STEPS` table confirms it. Trunk progress events therefore
  count up to 4, not 10.

## (c) The API calls in `runner/folder.py` on 0.15.2

- `_read_bio_chains(Path)` returns a list of 5-tuples (`tuple_len 5`). The
  unpacking in `_run_fold` works unchanged.
- `build_complex_features(chains, mol_dir, chain_ids, bonds, ...)` accepted the
  existing call. Its signature gained `modifications`, `templates` and
  `template_dir`, all optional.
- `weights.fetch(key, *, root=None, force=False, quiet=False)`,
  `weights.resolve(key, root=None)` and `weights.status(key, root=None)` have the
  signatures `runner/folder.py` and the preflight use.
- `edm_sample` still has a `dump_fn` parameter (`True`), so `runner.dump_tap`
  works. The folds above prove it end to end.
- `Protenix.load_from_checkpoint(path, ...)` loads the v1 file with no model
  argument. Its docstring still says "v2 checkpoint". The only change `load()`
  needs is the model id passed to `weights.fetch`.
- `tt-bio weights` on 0.15.2 also lists `nesso1` as present, which the affinity
  feature uses.

## What the next task needs

1. `Folder.load()` must accept `protenix-v1` and call `weights.fetch("protenix-v1", root=cache)`.
2. Preflight and the doctor must expect `protenix/model_v0.5.0.pt`, and must not
   assume the checkpoint sits directly in the cache root.
3. Expected times and pLDDT bands in the manifest are v2 numbers and need
   re-measuring on v1, after discarding each target's first fold.
