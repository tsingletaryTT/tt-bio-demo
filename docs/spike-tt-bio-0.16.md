# Spike: moving the pin from tt-bio 0.15.2 to 0.16.1

Date: 2026-10-14. Branch: `tt-bio-latest-upgrade`. Nothing in this note was run on a
Tenstorrent device. It records what a static read of the two wheels shows, and what is
left for a hardware pass.

## What changed upstream

0.16.0 was cut on 2026-10-13 and 0.16.1 on 2026-10-14. 0.16.1 fixes ESMC embeddings, which
this booth does not use (the affinity featurizer uses ESM-2 through nesso1, and the fold
path uses no language model). Most of 0.16.0 is speed work on models the booth does not
fold: BoltzGen, RoseTTAFold3, OpenFold3, OpenBind-0, AF2-IG, RFdiffusion3, BindCraft 2.

## Method

`pip download tt-bio==0.15.2` and `==0.16.1` into scratch directories, unzipped, read as
source. Nothing was imported, because importing `ttnn` opens a device. The signature
comparison parses both trees with `ast`.

## Findings

**Dependencies are identical.** Same `Requires-Dist` and `Requires-Python`, including
`ttnn==0.68.0`. The vendored SFPI toolchain needs no change, and `pip install
"tt-bio[tenstorrent]==0.16.1"` into `.venvs/venv-runner` changed only the `tt-bio` package.

**Every API the repo calls has the same signature.** 30 symbols checked across
`tt_bio.main`, `tenstorrent`, `runtime`, `weights`, `protenix`, `protenix_data`, `nesso1`
and `nesso1_input`, including `_read_bio_chains` (still the 5-tuple), `weights.fetch`,
`weights.artifacts_for`, `Protenix.fold`, `edm_sample` (still takes `dump_fn`, which the
live trajectory tap relies on) and `Nesso1.from_pretrained`. `weights.py` and
`protenix_data.py` are byte-identical. A matching signature does not prove matching behavior, so the hardware pass
below still has to run.

**Trace replay is on by default only in the CLI.** The 0.16.0 notes say Protenix-v1 and v2
replay the diffusion step from a device trace on Blackhole by default. In the library,
`Protenix.fold(trace=False)` is still the default and `get_device()` still opens no trace
region. `runner/folder.py` calls both without a trace argument, so the booth keeps the
eager path and `dump_fn` still fires on every step. The booth does not adopt tracing: a
long-lived process folding different targets of the same size is the shape that 0.6.3
fixed a wrong-structure bug for, and the saving the notes report is about 1 s on a
512-token fold.

**The normal-mode lever set grew.** `NORMAL_LEVERS` gained `dit_sdpa`, `dit_chunk` and
`conf_bins`. These are fused attention and confidence-binning kernels on the path the
booth folds through, so fold times and pLDDT can move. `TT_BIO_LEVERS` overrides the set.

**Other changes on the fold path.** `edm_sample` runs its atom-sized host math on one torch
thread (`serial_host`). The device confidence head, already on for v1, is unchanged in
behavior. `runtime.py` renumbers device ids to dense UMD indices, which matters for
Galaxy hosts and not for this four-card box. The model table
(11 predict models, 3 design, 1 affinity, the esmc and saprot groups) and the
capabilities matrix are identical, so the site's "Fifteen model families" holds.

**Commit counts at v0.16.1** (the spec's method, full clone): 14,568 total, 13,931 carry
his name as author. All 537 commits since v0.15.2 are under his identity. v0.15.2 is not
an ancestor of v0.16.1 (two commits on it are absent from 0.16.1), so the new totals are
not the old totals plus 537.

## Hardware pass (done on 0.18.0)

This section was written before the pin moved to 0.18.0. The hardware pass below was run on
0.18.0 instead of 0.16.1 (see the update at the end). The plan was:

1. One chip under a lease through `scripts/hw-run.sh`, in the background: one resident
   `Folder`, three folds per target, first fold of each shape discarded, same method as
   `docs/measurements-2026-10-13-protenix-v1.md`.
2. A real fold that streams 30 frames through the `dump_fn` tap.
3. One affinity question through the Q&A worker (nesso1 changed by about 85 lines).
4. The four-chip suite.
5. If numbers moved, refresh the manifest, thumbnails, README, site cards and the handout.


## Update: 0.17.0 and 0.18.0 (2026-10-15)

PyPI had 0.17.0 (2026-10-15 02:01 UTC) and 0.18.0 (2026-10-15 16:21 UTC) by the next day, and
the pin moved straight to 0.18.0. The same static method as above, against the 0.16.1 tree.

- Dependencies identical to 0.16.1 (`ttnn==0.68.0`, same Python range). No SFPI work.
- The same 30 symbols the repo calls have unchanged signatures. `weights.py`,
  `protenix_data.py`, `capabilities.py` and `main.py` are byte-identical to 0.16.1, so the model
  list and "Fifteen model families" hold. `_device_init_lock` is absent in both, as before.
- **Fold-path changes.** `protenix.py` now has its own `NORMAL_LEVERS`: the shared set plus
  `transition_ws`, `ws_l1acc`, `trimul_t1_c256` and `trimul_t1_c384` (the last two do not apply to
  Protenix-v1's pair width of 128). The shared set gained `opm_jd`. The diffusion pair-conditioning
  matmuls no longer force a core grid, which removes a multicast deadlock that was first seen on
  Protenix-v1 at 512 tokens or more (upstream tt-metal#58653). `TT_BIO_LEVERS` still overrides.
- Trace capture moved into a `_T.capture_trace` helper. The booth does not use trace.
- nesso1's normal lever set grew (`trimul_t1`, `trimul_t2`, `triatt_tail`, `transition_bw`,
  `trimul_t1_dram`), so the Q&A path also runs different kernels. `test_affinity_real.py` passed on a chip.
- Commit counts at v0.18.0 (the spec's `-i` method, full clone): 15,129 total, 14,492 with his
  name. The same method gives 13,931 at v0.16.1, which matches the figure printed before.
  All 561 commits in v0.16.1..v0.18.0 are under his identity.

Measured on 0.18.0: see the 0.18.0 section of
`docs/measurements-2026-10-13-protenix-v1.md`. Times and pLDDT bands barely moved.
