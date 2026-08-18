# protenix-v2: 107-residue protein+ligand dies with an L1/circular-buffer clash on 0.6.3 (worked on 0.6.2) — grid-dependent, fails ≥110 cores

**Draft for `moritztng/tt-bio`. Not filed.** Paste the body below.

---

`examples/affinity_fkg.yaml` (FKBP12, 107 residues + CCD ligand `SB3`, `msa: empty`) folded
fine on **0.6.2** and dies on **0.6.3** and on **main (`6fc864c9`)**:

```
TT_THROW @ tt_metal/impl/program/program.cpp:1052
Statically allocated circular buffers in program 362 clash with L1 buffers on
core range [(x=0,y=0) - (x=10,y=9)]. L1 buffer allocated at 1155072 and static
circular buffer region ends at 1159680
```

The failure is in the MSA track: `_trunk_cond` → `_msa` → `_in_proj_matmul`.

## Reproducer

Fails in under a second, no weights-dependent setup beyond the usual:

```bash
TT_VISIBLE_DEVICES=0 tt-bio predict examples/affinity_fkg.yaml \
    --model protenix-v2 --accelerator tenstorrent --single_sequence
```

## The interesting part: it is grid-dependent, with a threshold between 100 and 110 cores

Using **your own `TT_BIO_FORCE_GRID`** from `cde28838` (thank you — it made this diagnosable
in one sweep), on a p300c whose default main grid is 11×10:

| grid | cores | result |
|---|---|---|
| **11×10 (default)** | 110 | **FAIL** |
| **13×10** | 130 | **FAIL** |
| 10×10 | 100 | OK |
| 11×9 | 99 | OK |
| 9×10 | 90 | OK |
| 8×10 | 80 | OK |
| 8×8 | 64 | OK |

It fails at **≥110 cores** and works at **≤100**. Note `11×9` succeeds with the *same width*
as the failing default, so this tracks core count rather than either dimension — which
suggests something in the MSA track's L1 budget is sized per-grid and crosses the static-CB
boundary above ~100 cores.

At a working grid the fold is not merely alive but fast: **9.8 s warm at 11×9**, against
11.7 s on 0.6.2.

## What we ruled out

- **ttnn / tt-metal.** `ttnn==0.68.0` is pinned identically in 0.6.2, 0.6.3 and main. Same
  wheel, same allocator, same compiler — the layer emitting the error never moved.
- **Driver / firmware / machine.** TT-KMD 2.9.0 (installed 2026-08-10), firmware bundle
  19.11.0.0, Ubuntu 24.04. The machine has not rebooted since before 0.6.2 folded this input
  successfully, so the working and failing runs share a boot, a driver and a firmware.
- **A bad chip or board.** Reproduced on both p300c boards; Trp-cage folds fine on the same
  chips in the same processes.
- **Ligands as such.** The same protein *without* `SB3` folds. DHFR *with* its own MTX ligand
  folds at nearly twice the residue count.
- **Target size.** 107 residues fails; 187, 223 and 585 fold. (585 = human serum albumin,
  ~97 s, mean pLDDT 81 — 0.6.3 folds it happily.)
- **Accumulated state / ordering / fragmentation.** Fails as the first fold of a fresh
  process and mid-sequence alike. The program id varies with run order (we have seen 359,
  362, 909) but **the addresses are identical every time** — `1155072` and `1159680`, a
  4,608-byte overlap — across two machines-worth of sessions, three tt-bio versions and both
  boards.

## Environment

- tt-bio **0.6.3** and **main `6fc864c9`** (both fail); **0.6.2** folds it at 11.7 s
- `ttnn==0.68.0`, torch per your pins
- 2× p300c Blackhole (4 chips), TT-KMD 2.9.0, firmware bundle 19.11.0.0
- Ubuntu 24.04, kernel 7.0.0-28-generic

## Two small things noticed alongside

1. **`TT_VISIBLE_DEVICES` is read inconsistently.** `ttnn`'s device open accepts a PCI BDF
   (`0000:01:00.0`), but the `predict` path does `int()` on it and raises
   `ValueError: invalid literal for int() with base 10: '0000:01:00.0'`. The CLI needs the
   index form. Not a blocker once known, but the two paths disagree.
2. **0.6.3 requires one-chip visibility to open a device in-process.**
   `ensure_p300_mesh_descriptor()` forces a **1×1** P300 mesh-graph descriptor whenever P300
   chips are detected, so opening with a whole p300c *board pair* visible fails with
   `Physical chip id 0 not found in control plane chip mapping`. That is new in 0.6.3 and
   absent from 0.6.2. It is fine for anything that pins one chip per worker process, but it
   bites test harnesses and any lease that grants a board pair.

## What we did on our side

We removed FKBP12 from our demo's playlist rather than ship a card that crashes when someone
presses it, and it comes back when this is fixed. We have a working local workaround —
pinning the grid to 11×9 from our own code — but we would rather not carry a monkeypatch into
your internals, and the ~6 % it costs our largest target suggests the real fix belongs in the
L1 sizing rather than in the grid.

Happy to run anything that would help narrow it further.
