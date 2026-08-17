# Cold start: what the first fold on a fresh machine costs

Measured 2026-08-17 on the p300c dev box, under a `gozer` lease, one chip.

This closes the follow-up [`docs/spike-real-fold.md`](spike-real-fold.md) point 8 opened and
called "the most important follow-up measurement before Phase 3 ships" — *"Cold-start timing
is still unmeasured and should be budgeted, not assumed."*

## The numbers

Trp-cage, 20 residues, folded through the booth's own path (`runner.folder.Folder`, the class
the daemon uses), one chip:

| | Model load | Fold | Total |
|---|---|---|---|
| **Cold** — kernel cache empty | 5.65 s | **88.81 s** | **94.47 s** |
| **Warm** — cache populated, fresh process | 3.49 s | 5.91 s | 9.40 s |

**Kernel compilation costs about 83 seconds** for this one target (88.81 − 5.91), and the
cold run's model load is ~2 s slower as well.

Both runs emitted 246 events and produced the same structure — the cold run is not doing
less work, it is doing the same work plus compiling.

## Two older claims, settled

**"a first-ever compile costs ~177 s versus ~12 s warm"** — from
`docs/superpowers/specs/2026-08-10-tt-bio-demo-design.md`, which cited *this file* before it
existed. The figure was never sourced. The real cold cost for one target is **94.5 s**, not
177 s, and warm is **9.4 s**, not 12 s. Right order of magnitude, wrong numbers, no evidence
behind them.

**"the chip's first run takes ten seconds"** — from `ui/app.py` and `scripts/record-egg.py`,
and the reason the booth's egg fallback gives up at six seconds. **Confirmed**: a fresh
process against a warm cache is 9.40 s. That claim was about process start-up with kernels
already compiled, which is a different thing from an empty cache, and both numbers are worth
keeping distinct.

## Where the cache actually lives

```
~/.cache/tt-metal-cache-tt-bio/ttnn-0.68.0/tt-metal-cache<hash>/kernels/
```

Not `~/.cache/tt-metal`, not `$TT_METAL_HOME/built`, not `/tmp/tt-metal-cache` — the name is
tt-bio-specific and none of the usual guesses find it. The path is discoverable from
`generated/watcher/kernel_elf_paths.txt` under whatever `TT_METAL_LOGS_PATH` points at.

Sizes on this box:

- **One target (Trp-cage) from empty: 921 MB, 1794 kernel ELFs.**
- **The full accumulated cache: 1.5 GB, 9230 ELFs** — six playlist targets, the `Ctrl+G` egg,
  and four-chip work, built up since 2026-08-11.

## What this does NOT tell you

**The cost of pre-warming the whole playlist.** One target produced 1794 of the 9230 ELFs the
full cache holds, but the six targets share a great deal — you cannot multiply 83 s by six and
get an answer. Each distinct sequence length is its own set of shapes, so the true
full-playlist figure is somewhere between one target's 83 s and six times it, and nobody has
measured it. Budget generously and measure it before it matters.

**Anything about a freshly imaged machine.** This measured an empty *kernel cache* on a box
that already had weights, a built `venv-runner`, and a warm page cache. A real first boot also
pays the 3.7 GB weight download and the venv build.

## Method, so it can be repeated

1. `gozer run --chips 1 ...` — never touch the chips without a lease.
2. `mv ~/.cache/tt-metal-cache-tt-bio` aside. **Moved, not deleted**: that 1.5 GB is every
   playlist target's warm kernels, and a partial cache from one measurement is not a fair
   trade for it. The restore ran from a shell trap so an interrupted run still put it back.
3. Fold once — that is the cold number.
4. Fold again in a **separate process** — that is the warm number. A second fold inside the
   same process is faster for reasons unrelated to the cache (model resident, device already
   open), so it would not answer this question.
5. Restore, and confirm the restore: `1.5G` and `9230` ELFs back where they started.

## What to do about it

Warm the cache before the doors open, by folding each target you intend to show once. There is
no supported one-shot command for this today: `tt-bio warmup` exists, but its help says
*"Pre-compile all ttnn kernels for **Boltz-2** inference"* and the booth folds **protenix-v2**,
so it does not warm the kernels this demo actually uses.

The `tt-bio-demo-weights` package description claims it "pre-warms the tt-metal kernel cache".
It does not — its postinst only downloads and verifies weights. Until that is implemented,
warming is a manual step, and it is worth roughly a minute and a half per target you have
never folded on that machine.
