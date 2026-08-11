# Add `dump_fn` to `Protenix.fold`

**Applies to:** `moritztng/tt-bio` v0.6.2, commit `7ae0b961f2a069c476a558756d931491a70c005b`
(local checkout: `~/code/tt-boltz`, installed wheel matches byte-for-byte).

## The inconsistency

`OpenDDE.fold` accepts a `dump_fn` parameter (`tt_bio/opendde.py:340-342`) and threads it
into the diffusion sampler so a caller can observe per-step intermediate coordinates.
`Protenix.fold` does not (`tt_bio/protenix.py:1691-1693`) — it has `progress_fn` but no
`dump_fn` — even though the `edm_sample` function it calls internally already accepts one
(`tt_bio/protenix.py:2062-2065`) and already implements the full per-step callback contract
(lines 2132-2134, 2162-2164).

This reads as an oversight, not a design decision. The plumbing exists all the way down to
`edm_sample`; `Protenix.fold` (the older of the two) just never exposed the last inch, and
`OpenDDE.fold` (written later, on the same stack, calling the same `edm_sample`) does.

Today the *only* way to get a trajectory out of `Protenix.fold` is to monkeypatch the
module-level `tt_bio.protenix.edm_sample` before calling `fold()` — coupling a caller to a
private implementation detail that a future refactor (rename, inline, split into
sample-parallel and sample-serial variants, etc.) could break silently and without any
deprecation signal.

## The fix

Mirrors `OpenDDE.fold`'s existing pattern exactly — same parameter position (after `trace`,
before `max_parallel_samples`), same per-sample wrapping idiom for the two branches
`Protenix.fold` already has (batched-multiplicity vs. the per-sample loop):

```diff
     def fold(self, feats, *, n_step=200, n_sample=1, seed=None, progress_fn=None,
-             return_confidence=False, n_cycles=None, trace=False,
+             return_confidence=False, n_cycles=None, trace=False, dump_fn=None,
              max_parallel_samples=None):
```

```diff
         if n_sample > 1 and getattr(self.diffusion, "supports_multiplicity", False):
             _mps = DEFAULT_MAX_PARALLEL_SAMPLES if max_parallel_samples is None else max_parallel_samples
+            _df = (lambda step, x: dump_fn(step, step, x)) if dump_fn is not None else None
             coords = edm_sample(self.diffusion, cond, N, n_step=n_step, multiplicity=n_sample,
                                  max_parallel_samples=_mps, seed=seed, trace=trace,
-                                 progress_fn=progress_fn)
+                                 progress_fn=progress_fn, dump_fn=_df)
         else:
             coords = []
             for k in range(n_sample):
                 sd_seed = None if seed is None else seed + k
+                _df = (lambda step, x, _k=k: dump_fn(_k, step, x)) if dump_fn is not None else None
                 coords.append(edm_sample(self.diffusion, cond, N, n_step=n_step, seed=sd_seed,
-                                         trace=trace, progress_fn=progress_fn)[0])
+                                         trace=trace, progress_fn=progress_fn, dump_fn=_df)[0])
```

Callers get `dump_fn(sample, step, coords)` — same 3-argument shape `OpenDDE.fold` already
gives — with `sample` ranging over `n_sample` draws and `step` running `-1` (initial noise)
then `0..n_step-1` (one call per denoising step), matching `edm_sample`'s own contract.
The full diff is 5 insertions, 3 deletions, one file (see the patch file). When `dump_fn` is
not passed, `_df` is `None` and the call sites are functionally identical to what they were
before; the no-`dump_fn` path is untouched (verified below).

## Why it matters

This is for **tt-bio-demo**, a GTK4 conference-booth demo that renders a protein fold live
on Tenstorrent hardware — the audience watches the actual diffusion trajectory, atoms
condensing out of noise, streamed from the sampler in real time. A spike
(`docs/spike-real-fold.md` in this repo) proved the premise on real hardware: radius of
gyration falls **4357.7 → 6.99 (~620×)**, monotonically, across the 201 `dump_fn` calls of a
real 200-step `protenix-v2` fold (20-residue Trp-cage, no MSA) — a visibly dramatic
noise-to-structure collapse.

Getting that trajectory required monkeypatching `tt_bio.protenix.edm_sample` (see
`docs/spike-real-fold.md`, "Q2: What does `dump_fn` actually give you?"). That works, but it
is not a supported contract — it silently breaks if `edm_sample` is ever renamed, inlined, or
split. With a public `dump_fn` on `Protenix.fold`, the demo (and any other trajectory
consumer — debugging a sampler, visualizing convergence, diagnosing a bad fold, building a
progress UI) gets a real, versioned API instead of reaching past it.

## Evidence this works

Verified against the installed wheel (`tt-bio-demo/.venvs/venv-runner/.../tt_bio/`, backed up
before touching it, restored byte-for-byte afterward — `sha256sum` confirmed identical to the
pre-patch file both before patching and after restore) on real Tenstorrent hardware (4×
p300c Blackhole), using the **public** `dump_fn=` parameter, no monkeypatching:

Ran three folds in one process, same seed (`seed=0`), same target
(`~/code/tt-boltz/examples/trpcage_no_msa.yaml`, `n_step=200`, `n_cycles=10`):

| Fold | Method | `dump_fn` calls | Wall time |
|---|---|---|---|
| A | **public `dump_fn=` parameter** (this patch) | 201 | 5.67s |
| B | monkeypatched `edm_sample` (the old spike route) | 201 | 4.41s |
| C | no `dump_fn` at all (regression check) | — | 4.38s |

Comparisons, all in the same process on the same device:

- **A vs. B (public vs. monkeypatch), same seed:** identical call count (201), identical
  step sequence (`-1, 0, 1, ..., 199`), identical `sample` index (always `0`, as expected for
  `n_sample=1`), identical shape/dtype/device on every call
  (`[1, 154, 3] / torch.float32 / cpu`), and **coordinates bit-for-bit identical**
  (`max_abs_diff=0.0`, `np.allclose(atol=1e-6)=True`). This is the equivalence that matters:
  the new public path calls exactly the code the monkeypatch used to have to intercept.
- **A vs. C (dump_fn present vs. absent):** fold output coordinates **bit-for-bit identical**
  (`max_abs_diff=0.0`). `dump_fn` is a pure observer — passing it does not perturb the
  computation, and the no-`dump_fn` path is provably untouched.
- **A vs. the spike's recorded fixture** (`tests/fixtures/streams/real_fold_trpcage.raw.json`,
  captured via the monkeypatch route in an earlier, separate process): same call count (201),
  same shapes, and the radius-of-gyration curve matches **exactly**
  (step −1: `4357.730` vs. `4357.730`; step 199: `6.990` vs. `6.990`) — the deterministic,
  seeded device pipeline reproduces the same trajectory across separate process invocations,
  not just within one.
- Also confirmed: once this patch is applied, the *old* monkeypatch idiom
  (`kwargs.setdefault("dump_fn", ...)`) silently stops intercepting anything, because
  `Protenix.fold` now always passes `dump_fn=...` explicitly (mirroring `OpenDDE.fold`'s own
  style) — a small, concrete illustration of exactly the fragility this patch removes. A
  robust monkeypatch has to force-override the kwarg instead; a caller using the new public
  parameter doesn't need to know or care.

Also confirmed unaffected:
- `scripts/test.sh` (tt-bio-demo's own suite): **83/83 passed**, before and after.
- `tt-smi -s` device probe: 4 devices visible, before and after.
- No stray processes left running (`ps aux` checked after every run).
- The installed `tt_bio/protenix.py` was restored to the exact original bytes
  (verified by `sha256sum` against both the pre-patch file and `git show
  7ae0b961:tt_bio/protenix.py`).

## What's in this directory

- `0001-feat-protenix-add-dump_fn-to-Protenix.fold.patch` — `git format-patch` output,
  applies cleanly with `git am` on top of `7ae0b961` (tt-bio v0.6.2). 1 file changed,
  5 insertions, 3 deletions.
- `reproduce.py` — minimal script demonstrating the new `dump_fn=` parameter end to end on
  real hardware (radius-of-gyration printout), runnable with
  `.venvs/venv-runner/bin/python3 docs/upstream/protenix-dump-fn/reproduce.py` from the
  tt-bio-demo repo root against a patched tt-bio checkout.
- This `README.md`.

To apply: `cd <tt-bio checkout> && git am < 0001-feat-protenix-add-dump_fn-to-Protenix.fold.patch`.
