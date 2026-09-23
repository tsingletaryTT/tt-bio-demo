# Responsive booth layout — design spec

**Date:** 2026-09-23
**Status:** Approved, ready for implementation plan

## 0. Why

The booth's GTK4 layout assumes a fixed 1920×1080 canvas: `_SIDE_RAIL_WIDTH_PX = 552` and
`_GALLERY_WIDTH_PX = 1920 - 552` are hardcoded pixel constants in `ui/app.py`, and several
other widgets (the `?` help card, the confidence-legend-beside-caption strip) are sized or
tested against that same fixed number. Below that width the rail's own hard floor and the
hero side fight over space that doesn't exist, producing a visibly unstable layout — first
observed running `scripts/run-demo.sh --windowed` at its 1280×800 dev default, but the same
problem would hit any booth operator whose actual display isn't exactly 1920 wide.

Not everyone pushes 1920×1080. The booth should render correctly across a real range of
sizes, fullscreen or windowed, down to a small-laptop floor — not just the one resolution it
happens to have been built and tested against.

## 1. Scope

**In scope:**
- The rail/hero width split (`_SIDE_RAIL_WIDTH_PX`/`_GALLERY_WIDTH_PX` in `ui/app.py`)
  becomes a function of the actual window width, not a fixed pair of constants.
- The `?` help card's width (`_HELP_CARD_WIDTH_PX`) becomes a function of window width the
  same way.
- The confidence-legend-beside-caption strip (`_build_target_info`) is re-verified (not
  re-designed) against the new, narrower floor-size hero width.
- Existing pixel-pinned tests for all of the above are extended to a small matrix of
  representative widths instead of one fixed point.

**Out of scope (unchanged by this work):**
- The 2×2 quad grid's own internal layout (`ui/quad.py`) — already uses `Gtk.Grid` with
  homogeneous rows/columns and ellipsized/width-capped per-cell text, so it already shrinks
  correctly once the hero side actually receives the right space. No code change proposed
  here unless implementation turns up a real problem (see §4).
- Font sizes and CSS spacing constants. This is a geometry/space-allocation fix, not a
  DPI-scaling project — text stays the size it is today at every supported width.
- Quad-vs-solo mode selection logic (untouched; still governed by chip count, not window
  size).

## 2. Target size range

Full responsive support from a small-laptop floor up through the real booth's reference
resolution and beyond:

- **Floor:** 1024×768
- **Common laptop:** 1366×768
- **Reference (today's fixed assumption, must not visually change):** 1920×1080
- **Wide:** one larger size (e.g. 2560×1440) to confirm the rail doesn't grow unbounded

## 3. The core mechanism

Replace the fixed constants with a pure function and a fraction-with-clamp formula:

```python
_RAIL_FRACTION = 552 / 1920    # ~0.2875 — today's exact proportion at the reference size
_RAIL_MIN_PX = 420              # floor: rail text/panels stay legible, never over-squeezed
_RAIL_MAX_PX = 700              # ceiling: an ultra-wide display doesn't over-grow the rail

def rail_width_for(total_width_px: int) -> int:
    return int(clamp(total_width_px * _RAIL_FRACTION, _RAIL_MIN_PX, _RAIL_MAX_PX))
```

Properties this function must have, each independently testable with no GTK involved:
- `rail_width_for(1920) == 552` exactly — the reference resolution's layout must not move
  a single pixel from what ships today.
- Monotonically non-decreasing as `total_width_px` grows.
- Clamped at both ends: never below `_RAIL_MIN_PX`, never above `_RAIL_MAX_PX`.

The hero/gallery width is always `total_width_px - rail_width_for(total_width_px)` — never a
second independent formula, so the two sides can never disagree about how much space exists
between them.

**Wiring into the real layout:** today the rail gets `set_size_request(_SIDE_RAIL_WIDTH_PX,
-1)` once, at construction. This becomes a recompute on every resize — the window's own
`notify::default-width` (or equivalent resize signal) recalculates `rail_width_for(...)` and
re-applies it. The existing `_FixedWidthBox`/rail layout-manager machinery in `ui/app.py`
(built to make `set_size_request` actually stick as a floor rather than just a suggestion)
is reused as-is; only the *value* it's fed becomes live instead of constant.

## 4. Quad grid and hero content

No structural change proposed. `Gtk.Grid` with homogeneous rows/columns already
redistributes space correctly once given the right total width, and `ui/quad.py`'s per-cell
labels are already ellipsized and width-capped (`_CAPTION_MAX_CHARS`, `_CHIP_LABEL_MAX_CHARS`)
so they don't force a larger minimum as cells shrink.

One real unknown, to be **verified, not assumed**, during implementation: `StructureViewer`
(the embedded OpenGL widget) may carry a hardcoded minimum size left over from a
1920-only assumption. If its true minimum exceeds what a quad cell gets at the 1024×768 floor,
that becomes the actual supported floor — the spec's floor number gets revised downward with
the measurement that found it, not asserted past a real constraint.

## 5. Other fixed-width widgets

- **`_HELP_CARD_WIDTH_PX`** (already the subject of one past sizing bug — see
  `docs/followups.md`'s help-card entry) — becomes `clamp(window_width * FRACTION, MIN, MAX)`
  via the same style of function as `rail_width_for`, computed once when the card is built
  against the window's current width. It does not need to live-resize while open.
- **The confidence-legend-beside-caption strip** (`_build_target_info`) — no formula change;
  it already measures against whatever `_GALLERY_WIDTH_PX`-equivalent width it's given, so
  once that width is correct at every tested size, this strip is correct too. Re-verified,
  not re-designed: confirm all seven shipped taglines still produce one consistent strip
  height at the 1024 floor width, the same measurement already proven at 1920 (all seven
  landed at an identical 96px there). If any tagline wraps differently at the narrower floor
  width, the fallback is tighter ellipsizing, never letting the strip grow and steal height
  from the render — that invariant does not change.

## 6. Testing strategy

- **New pure unit tests** for `rail_width_for` (and the help-card equivalent): no GTK
  needed — exact value at 1920 (backward-compat pin), floor/ceiling clamp behavior at the
  extremes, monotonicity.
- **Existing pixel-pinned tests extended to a matrix**, not a single point: the confidence-
  legend-height test and the help-card-fits test each run at 1024×768, 1366×768, 1920×1080,
  and the one wide size, using the *same* width-computing function the real layout code
  calls — never a second hand-typed copy of the arithmetic (the exact class of bug this
  project has been bitten by before: two places agreeing about a number is a fact to
  verify, not assume).

## 7. Verification plan

Self-verified with real screenshots, not just green tests — using a headless Wayland
compositor (`weston --backend=headless --renderer=pixman --debug`, confirmed working during
this design's own brainstorming) to run the actual booth app at each matrix size with zero
risk to anyone's live desktop session, and `weston-screenshooter` to capture what it actually
looks like. One compositor instance per size in the matrix, same app, screenshots compared
before this is called done. Final sign-off still wants a human's own eyes on the real booth
hardware, the same as any other visual change here, but "did an agent actually look at it"
no longer has to mean "poking at someone's live X session."

## 8. Rollout

No `--devices`/CLI-visible flag needed — this is purely how the existing layout computes its
own sizes, transparent to every existing invocation of `scripts/run-demo.sh`. No debian
packaging changes (this is `ui/app.py` source, already shipped in the `tt-bio-demo` package).
No changes to `webview/` — the browser viewer has its own, CSS-flex-based responsive fix
already (`webview/static/style.css`), unrelated to this native-GTK-specific mechanism.
