# Vendored: tensix-viz

`tensix-viz.js` and `tensix-viz.css` in this directory are **not this project's
code**. They are copied verbatim from:

- **Project:** tensix-viz — "Tenstorrent hardware topology visualizer, chip to cluster"
- **Upstream:** https://github.com/tsingletaryTT/tensix-viz
- **Version:** 1.2.0
- **Commit:** `3986ed580388d040af3afcd73b2b34fb8279ea9d` (`chore(release): bump version to 1.2.0`)
- **Licence:** Apache-2.0 — the same licence this repository ships under
  (see `../../../LICENSE`), so no additional licence text is required here;
  this file is the attribution.
- **Copied on:** 2026-08-12

## Why vendored rather than fetched

The booth runs **offline at the venue** (see the README's Requirements: "Network
at provisioning time; **none required at the venue**"). The upstream project
offers a CDN embed; a CDN embed is a demo that goes blank the moment the
conference wifi does. These two files are self-contained — zero runtime
dependencies, no network access of their own — so a byte-for-byte copy on disk
is both the smallest and the most reliable way to ship them.

They are loaded by `ui/chipviz.py`, which reads them off disk and inlines them
into a single `about:blank` page. Nothing here is ever fetched over a network at
runtime.

## Updating

Copy the two files again from an upstream checkout and update the version and
commit above:

    cp ~/code/tensix-viz/tensix-viz.{js,css} ui/assets/tensix-viz/

`tests/unit/test_chipviz.py` asserts both files are present and non-trivial, so
a half-finished update fails the suite rather than silently shipping a blank
animation. Do **not** hand-edit these files: local edits would be lost on the
next copy, and `ui/chipviz.py` deliberately keeps every project-specific
decision (grid layout, per-chip fan-out, telemetry mapping) on the Python side
for exactly that reason.
