# tt-bio-demo

A turnkey conference demo for [tt-bio](https://github.com/moritztng/tt-bio) — protein
structure prediction on Tenstorrent hardware.

Watch a protein condense out of noise into its folded structure, in real time, computed on
the Blackhole cards in the room. Native GTK4, no browser.

> **Status:** Phase 3a (the real daemon driving the real UI) is implemented and has run
> end to end on hardware: `scripts/run-demo.sh` starts the compute daemon and the GTK4 UI
> together, wired over a Unix socket, and a real fold — atom cloud condensing, then
> cross-fading to the pLDDT ribbon — renders **driven entirely by live computation on a
> Tenstorrent card, with no recorded fixture anywhere in the path**. See
> [the design spec](docs/superpowers/specs/2026-08-10-tt-bio-demo-design.md),
> [the Phase 1–2 plan](docs/superpowers/plans/2026-08-10-protocol-and-renderer.md), and
> [the Phase 3a plan](docs/superpowers/plans/2026-08-11-runner-daemon.md) for what's done
> versus what's still ahead.

## What works today

- **Event protocol** — newline-delimited JSON over a Unix socket (`protocol/events.py`),
  shared by the runner and the UI.
- **Compute daemon** (`runner/`) — opens a Tenstorrent device once, holds the model
  resident across folds (a warm fold measured ~4.3–4.5s vs. a cold one's ~5.7s), folds
  every `.yaml` target in a playlist directory, samples `tt-smi` for card health and
  quarantines an unsafe card rather than handing it out, and contains tt-metal's own
  Inspector/Watcher log output to a configured root instead of the process's CWD.
- **Mock runner** — replays a recorded fold trajectory (`runner/mock.py`) at the protocol's
  real pace, still used by the UI-side test suite so those tests don't need hardware.
- **Renderer** — a GTK4 `GtkGLArea` (`ui/`) that streams the diffusion point cloud in real
  time, then cross-fades into a pLDDT-colored ribbon once the fold completes, and
  reconnects automatically (keeping the last structure rotating) if the daemon disappears
  and returns — verified against the real daemon, not just the mock, including a hard
  `SIGKILL` mid-fold.
- **`scripts/run-demo.sh`** — the turnkey launcher: starts both processes in their own
  venvs, wires them by socket, and tears the daemon down on Ctrl-C so a leaked device
  handle can't block the next run.

## Not yet built

The pipeline-progress widget, the telemetry panel, the visitor-facing gallery, the
four-state attract-loop machine, the curated playlist (today's playlist is just whatever
`.yaml` files a directory is pointed at — no blurbs, no pre-cached MSAs, no thumbnails),
and Debian packaging are all later phases — see the Phase 3a plan's "what this phase
deliberately leaves out" for the full list. The daemon emits the events these panels will
eventually consume; nothing renders them yet, and none of that UI exists on this branch.
Also outstanding: the two known blockers in [`docs/followups.md`](docs/followups.md)
(`ribbon_from_cif` running synchronously on the GTK main loop, and multi-chain structures
being splined as one continuous tube) — both are UI-side and belong to that later phase.

## Running it today

On a box with the two venvs built (`scripts/setup-venvs.sh`) and a Tenstorrent card free:

```bash
./scripts/run-demo.sh
```

This starts the daemon (`venv-runner`) and the UI (`venv-ui`), wires them over a Unix
socket under `${XDG_RUNTIME_DIR:-/tmp}/tt-bio-demo/`, and folds whatever `.yaml` it finds
in a small playlist directory it builds itself (defaulting to
`~/code/tt-boltz/examples/trpcage_no_msa.yaml`, a 20-residue target that needs no MSA
server). Ctrl-C tears the daemon down cleanly. See `scripts/run-demo.sh --help` for the
options — socket, log root, playlist, weights, device index, and the two disk/RAM budgets
below.

Two growth risks are bounded, not just noted: tt-metal's own log output (contained to
`--log-root` and swept between folds against `--log-budget-gb`, default 2 GB — and, as of
this phase, with tt-metal's Inspector subsystem disabled outright by default, since it was
found to hold one log file open and appending for the daemon's entire life, which no
directory sweep can actually reclaim while the process runs; see `runner/env.py`'s module
docstring for the measured growth rate before that fix), and the daemon's own `.cif`
output (swept the same way against `--structures-budget-gb`, default 200 MB).

## Intended install

```bash
sudo apt install tt-bio-demo-all
```

On a freshly imaged QB2 this brings tt-bio, the model weights (including the OpenFold3
checkpoint), the curated content, and the application — and pre-warms the tt-metal kernel
cache so the first fold at the booth is a warm one.

## Requirements

- Tenstorrent QB2 (4× p300c Blackhole) — developed and tested there
- Ubuntu 24.04, Wayland
- Network at provisioning time; **none required at the venue**
