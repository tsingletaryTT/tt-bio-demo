# tt-bio-demo

A turnkey conference demo for [tt-bio](https://github.com/moritztng/tt-bio) — protein
structure prediction on Tenstorrent hardware.

Watch a protein condense out of noise into its folded structure, in real time, computed on
the Blackhole cards in the room. Native GTK4, no browser.

> **Status:** Phase 1–2 (protocol and renderer) is implemented. The event protocol, a mock
> runner that replays a recorded fold, and the GTK4 renderer all work end to end today —
> against a recording, with **no Tenstorrent hardware involved yet**. See
> [the design spec](docs/superpowers/specs/2026-08-10-tt-bio-demo-design.md) and
> [the Phase 1–2 plan](docs/superpowers/plans/2026-08-10-protocol-and-renderer.md) for what's
> done versus what's still ahead.

## What works today

- **Event protocol** — newline-delimited JSON over a Unix socket (`protocol/events.py`),
  shared by the runner and the UI.
- **Mock runner** — replays a recorded fold trajectory (`runner/mock.py`) at the protocol's
  real pace, standing in for the compute daemon during this phase.
- **Renderer** — a GTK4 `GtkGLArea` (`ui/`) that streams the diffusion point cloud in real
  time, then cross-fades into a pLDDT-colored ribbon once the fold completes.

## Not yet built

The real `tt-bio-demod` compute daemon, `tt-smi` hardware telemetry, the pipeline-progress
widget, the visitor-facing gallery, the curated playlist, and Debian packaging are all later
phases — see the plan's "what this phase deliberately leaves out" for the full list. None of
that exists on this branch.

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
