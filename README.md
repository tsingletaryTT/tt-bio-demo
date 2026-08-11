# tt-bio-demo

A turnkey conference demo for [tt-bio](https://github.com/moritztng/tt-bio) — protein
structure prediction on Tenstorrent hardware.

Watch a protein condense out of noise into its folded structure, in real time, computed on
the Blackhole cards in the room. Native GTK4, no browser.

> **Status:** design complete, implementation not yet started.
> See [the design spec](docs/superpowers/specs/2026-08-10-tt-bio-demo-design.md).

## What it does

- **Live fold trajectory** — streams the actual per-step output of tt-bio's diffusion
  sampler, so what you see is the computation, not an animation of it.
- **Hardware telemetry** — all four p300c cards on screen, working.
- **Pipeline progress** — MSA → trunk → diffusion → confidence, as it happens.
- **Visitor-driven or self-running** — an attract loop cycles curated proteins unattended;
  a visitor can step in and pick one, and the booth resets itself when they leave.

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
