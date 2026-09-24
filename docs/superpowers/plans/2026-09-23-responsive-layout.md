# Responsive Booth Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the GTK4 booth's layout size itself from the real window width instead of a
hardcoded 1920×1080 assumption, so it renders correctly from a 1024×768 floor up through the
booth's reference fullscreen resolution and beyond.

**Architecture:** Replace two fixed pixel constants (`_SIDE_RAIL_WIDTH_PX`,
`_HELP_CARD_WIDTH_PX`, used at their *live* call sites) with pure fraction-with-clamp
functions, and wire the rail's width into a custom `Gtk.BoxLayout` subclass's `do_allocate`
so it recomputes on every real layout pass (covers both "sized once at launch" and, for
`--windowed` dev use, live interactive resize) — this exact custom-layout-manager pattern
already exists once in this file (`_PinnedNaturalBoxLayout`) and was proven working for this
purpose empirically before this plan was written (see Task 4).

**Tech Stack:** Python, PyGObject (GTK4/Gdk4), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-23-responsive-layout-design.md`

## Global Constraints

- `rail_width_for(1920) == 552` exactly — the reference resolution's layout must not move a
  single pixel from what ships today. This is the single most important regression to catch.
- No font-size or CSS spacing changes. Geometry only.
- `ui/quad.py` gets no code changes in this plan unless a task's own verification step finds
  a real `StructureViewer` minimum-size problem — do not touch it speculatively.
- `_SIDE_RAIL_WIDTH_PX = 552` stays defined exactly as it is today. It remains the reference
  width `ui/chipviz.py`'s `RAIL_INNER_WIDTH_PX` and `ui/panels.py`'s arithmetic comment are
  calibrated against; this plan does not touch either file. The new `rail_width_for` function
  governs the *actual runtime* rail width; at the reference size the two agree exactly, and
  at other sizes the Tensix WebView panel (already documented project-wide as one scoped,
  decorative, fail-soft exception) may render at a very slightly different internal scale
  than its allocated column — an accepted, documented cosmetic limitation, not a bug to chase
  in this plan.
- Every new pixel-computing function is pure (no GTK objects in its signature or body) and
  gets its own GTK-free unit test before any GTK code calls it.

## Review Focus

- **A `--windowed` operator drags the window narrower than 840px** (`_RAIL_MIN_PX` (420) +
  a bare-minimum usable hero width) — the layout must not crash or produce a negative-width
  allocation; `rail_width_for` clamping handles the number, but the real `do_allocate`
  override needs a test that a pathologically small container width doesn't raise.
- **The help card is opened on a `--windowed` app narrower than the card's own natural
  width** — must not exceed the actual screen width or push content off both the right edge
  and the bottom (the two-axis version of the bug `_HELP_CARD_WIDTH_PX` already exists to
  prevent on one axis).
- **A monitor whose reported geometry is unusually small or absent** (a headless/virtual
  display misreporting, or `get_monitors()` returning zero items) — `_expected_window_width`
  must fall back to a sane default rather than raising or returning 0, which would make
  every downstream `width_px=0` construction call fail confusingly three modules away.
- **The rail's live-resize recompute firing during a resize that is still in progress**
  (multiple rapid `do_allocate` calls before settling) — each call must be independently
  correct from just that call's `width` argument, not accumulate state across calls.
- **The existing four-target manifest's real content at the 1024×768 floor** — not a
  synthetic width, the actual shipped `playlist/manifest.yaml` targets and questions, since a
  clamp formula that is merely arithmetically clamped but never checked against real copy
  is exactly the gap this project's own history (the DNA tagline, the help card) keeps
  finding.

---

## Task 1: `rail_width_for` — the pure sizing function

**Files:**
- Modify: `ui/app.py` (near `_SIDE_RAIL_WIDTH_PX`, line ~687)
- Test: `tests/unit/test_app_interaction.py` (new tests, appended near the other
  `_SIDE_RAIL_WIDTH_PX`-related tests around line 1283)

**Interfaces:**
- Produces: `rail_width_for(total_width_px: int) -> int`, plus module constants
  `_RAIL_FRACTION`, `_RAIL_MIN_PX`, `_RAIL_MAX_PX`.

- [ ] **Step 1: Write the failing tests**

```python
def test_rail_width_for_matches_the_reference_layout_exactly():
    """The one number that must never move: at the booth's own reference
    resolution, the new formula must reproduce today's fixed 552 exactly."""
    assert app_module.rail_width_for(1920) == app_module._SIDE_RAIL_WIDTH_PX


def test_rail_width_for_is_floor_clamped_at_small_widths():
    assert app_module.rail_width_for(1024) == app_module._RAIL_MIN_PX
    assert app_module.rail_width_for(1) == app_module._RAIL_MIN_PX


def test_rail_width_for_is_ceiling_clamped_at_large_widths():
    assert app_module.rail_width_for(10_000) == app_module._RAIL_MAX_PX


def test_rail_width_for_is_monotonically_non_decreasing():
    widths = [800, 1024, 1280, 1366, 1600, 1920, 2560, 3840]
    computed = [app_module.rail_width_for(w) for w in widths]
    assert computed == sorted(computed)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k rail_width_for -v`
Expected: FAIL with `AttributeError: module 'ui.app' has no attribute 'rail_width_for'`

- [ ] **Step 3: Implement `rail_width_for` in `ui/app.py`**

Add immediately after the existing `_SIDE_RAIL_WIDTH_PX = 552` definition and its long
comment block (do not delete or edit that comment — it explains the reference value, which
this task does not change):

```python
# The reference value above (552 at the 1920-wide booth) is now the ANCHOR of a
# fraction-with-clamp formula rather than a fixed number applied at every width. Preserves
# today's exact layout at the reference resolution (rail_width_for(1920) == 552, pinned by
# test_rail_width_for_matches_the_reference_layout_exactly) while adapting below and above
# it -- see docs/superpowers/specs/2026-09-23-responsive-layout-design.md section 3.
_RAIL_FRACTION = _SIDE_RAIL_WIDTH_PX / 1920
# Floor: below this the rail's own panels (telemetry digits, the QuestionQueuePanel's
# wrapped text) start looking cramped rather than merely narrower. Chosen with headroom
# under 1024 * _RAIL_FRACTION (~294), which the fraction alone would produce at the stated
# floor resolution -- the clamp, not the fraction, is what governs the smallest supported
# size.
_RAIL_MIN_PX = 420
# Ceiling: an ultra-wide display should not hand the rail more width than its own fixed-size
# content (the Tensix panel, the pipeline bars) can use -- past this point extra width is
# wasted whitespace, not legibility.
_RAIL_MAX_PX = 700


def rail_width_for(total_width_px):
    """How wide the side rail gets, given the window's actual total width.

    Pure arithmetic, no GTK -- the real allocation-time caller is
    `_ResponsiveSplitLayout.do_allocate` (see `_build_ui`'s `root` box), and this function's
    only job is to be independently correct and independently testable from that wiring.
    """
    return max(_RAIL_MIN_PX, min(_RAIL_MAX_PX, round(total_width_px * _RAIL_FRACTION)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k rail_width_for -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add ui/app.py tests/unit/test_app_interaction.py
git commit -m "feat: rail_width_for, a pure fraction-with-clamp rail sizing function"
```

---

## Task 2: `help_card_width_for` — the same shape, for the `?` card

**Files:**
- Modify: `ui/app.py` (near `_HELP_CARD_WIDTH_PX`, line ~1458)
- Test: `tests/unit/test_app_interaction.py`

**Interfaces:**
- Consumes: nothing from Task 1 (independent formula, same shape).
- Produces: `help_card_width_for(total_width_px: int) -> int`, plus
  `_HELP_CARD_FRACTION`, `_HELP_CARD_MIN_PX`, `_HELP_CARD_MAX_PX`.

- [ ] **Step 1: Write the failing tests**

```python
def test_help_card_width_for_matches_the_reference_layout_exactly():
    assert app_module.help_card_width_for(1920) == app_module._HELP_CARD_WIDTH_PX


def test_help_card_width_for_is_floor_clamped():
    assert app_module.help_card_width_for(1024) == app_module._HELP_CARD_MIN_PX


def test_help_card_width_for_never_exceeds_the_reference_card_width():
    """Wider than 1920 does not mean a wider card -- past the reference size extra
    width is not more readable, so the ceiling is the reference value itself."""
    assert app_module.help_card_width_for(3840) == app_module._HELP_CARD_WIDTH_PX
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k help_card_width_for -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement `help_card_width_for` in `ui/app.py`**

Add immediately after `_HELP_CARD_WIDTH_PX = 1400` and its existing comment:

```python
# Same fraction-with-clamp shape as rail_width_for, independently: the help card's ideal
# width is not derived from the rail's, it happens to need its own anchor at the reference
# size. The ceiling is the reference value itself (1400) rather than a larger number --
# past 1920 wide, a wider card is not more readable, only surrounded by more whitespace, and
# _HELP_CARD_WIDTH_PX's own comment already measured 1400 as the width that keeps this
# card's content within the booth's own screen height.
_HELP_CARD_FRACTION = _HELP_CARD_WIDTH_PX / 1920
# Floor: below this the two-column KEYS/intro layout the card's own comment describes would
# need to wrap narrow enough to blow past a short screen's own height again -- the exact
# defect _HELP_CARD_WIDTH_PX was raised to fix once already, one axis over.
_HELP_CARD_MIN_PX = 700
_HELP_CARD_MAX_PX = _HELP_CARD_WIDTH_PX


def help_card_width_for(total_width_px):
    """How wide the `?` help card gets, given the window's actual total width."""
    return max(_HELP_CARD_MIN_PX,
               min(_HELP_CARD_MAX_PX, round(total_width_px * _HELP_CARD_FRACTION)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k help_card_width_for -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add ui/app.py tests/unit/test_app_interaction.py
git commit -m "feat: help_card_width_for, the help card's own responsive width"
```

---

## Task 3: `_expected_window_width` — what width will this window actually get

**Files:**
- Modify: `ui/app.py` (a new method on `DemoApp`, called from `_build_gallery` and
  `_build_help_overlay`)
- Test: `tests/unit/test_app_interaction.py`

**Interfaces:**
- Consumes: `self.windowed` (existing `DemoApp` attribute, already set in `__init__`).
- Produces: `DemoApp._expected_window_width() -> int`, used by Task 5 (gallery) and Task 6
  (help card) to seed their one-time-at-construction width.

**Why this task exists, verified empirically before this plan was written:** a
`Gtk.ApplicationWindow`'s `get_width()`/`get_height()` return `0` at construction time and
even immediately after `present()` — real allocation only becomes available after the
compositor round-trip, which happens well after every `_build_*` method in `do_activate` has
already run. There is no way to read "the real width" off the window object itself at the
point gallery/help-card construction needs it. The two things that ARE known at that point:
what `--windowed` requested (a literal constant), and — for the normal fullscreen path —
the display's own monitor geometry, which `Gdk.Display.get_monitors()` reports correctly
with no window realized at all.

- [ ] **Step 1: Write the failing tests**

```python
def test_expected_window_width_uses_the_windowed_default_when_windowed():
    app = _app()
    app.windowed = True
    assert app._expected_window_width() == 1280


def test_expected_window_width_falls_back_when_no_monitor_is_reported():
    """A headless/virtual display or a Gdk backend that reports zero monitors must not
    crash construction or hand a 0-width request three modules downstream -- it falls back
    to the reference resolution's width."""
    app = _app()
    app.windowed = False

    class _EmptyMonitors:
        def get_n_items(self):
            return 0

    app._get_monitors_for_test = lambda: _EmptyMonitors()
    assert app._expected_window_width() == 1920
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k expected_window_width -v`
Expected: FAIL — `_app()`'s fake `DemoApp` has no `_expected_window_width` method yet.

- [ ] **Step 3: Implement `_expected_window_width`**

Add as a method on `DemoApp`, near `_build_side_rail` (both are "layout" methods):

```python
    def _get_monitors_for_test(self):
        """The real monitor list. A tiny seam so a test can hand this method
        something that reports zero monitors without needing a real (or
        fully faked) `Gdk.Display` -- see `_expected_window_width`'s own
        docstring for why this can't just read `window.get_width()`."""
        return Gdk.Display.get_default().get_monitors()

    def _expected_window_width(self):
        """The width this window will actually end up at, usable BEFORE the
        window is realized (`window.get_width()` reads 0 until well after
        `present()` -- verified empirically, not assumed, before this method
        was written).

        `--windowed` requests a literal size (`set_default_size`, see
        `do_activate`) that this method must match exactly, not guess at
        from a monitor. The normal, fullscreen path has no such literal
        request -- fullscreen always becomes whatever the display's own
        monitor reports -- so THAT path reads `Gdk.Display`'s monitor
        geometry instead, which is available immediately, with no window
        realized at all.

        Falls back to the reference resolution if no monitor is reported at
        all (an unusual Gdk backend, or a virtual display returning zero
        items) -- a 0-width fallback would hand a `width_px=0` construction
        argument three modules downstream for something that would have
        no clear connection back to this method.
        """
        if self.windowed:
            return 1280
        monitors = self._get_monitors_for_test()
        if monitors.get_n_items() == 0:
            return 1920
        return monitors.get_item(0).get_geometry().width
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k expected_window_width -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add ui/app.py tests/unit/test_app_interaction.py
git commit -m "feat: _expected_window_width, resolved before the window is ever realized"
```

---

## Task 4: `_ResponsiveSplitLayout` — the rail's live width, wired into real allocation

**Files:**
- Modify: `ui/app.py` (new class near `_PinnedNaturalBoxLayout`/`_FixedWidthBox`, and the
  `root` box construction in `do_activate`, line ~2300)
- Test: `tests/unit/test_app_interaction.py`

**Interfaces:**
- Consumes: `rail_width_for` (Task 1).
- Produces: `_ResponsiveSplitLayout(Gtk.BoxLayout)`, applied to the `root` box that holds
  `[hero, side_rail]`.

**This is the mechanism this whole plan exists to deliver, and it was proven working with a
real, running, off-screen GTK4 window before this task was written** (three throwaway probe
scripts against a headless `weston` compositor — see the design spec's §7 and this task's
own manual-verification step below for the same recipe). Three things were confirmed, and
this task's code follows them exactly:

1. `Gtk.Widget` has no public `width`/`height` GObject property to `notify::` on in this GTK
   version — that route does not exist, and this task does not attempt it.
2. A custom `Gtk.BoxLayout` subclass's `do_allocate(widget, width, height, baseline)` DOES
   receive the container's real, live allocated width on every real layout pass, including a
   live interactive resize (confirmed: `set_default_size` called after `present()` produces a
   new `do_allocate` call with the new width, and a child's `set_size_request` called from
   inside that same override correctly changes its actual allocated size).
3. This is the exact same pattern `_PinnedNaturalBoxLayout` already uses in this file for a
   different purpose (pinning the rail's own natural width) — a `Gtk.BoxLayout` subclass
   whose overridden method calls the parent implementation after doing its own work.

- [ ] **Step 1: Write the failing test**

```python
def test_the_responsive_split_layout_gives_the_rail_the_live_width():
    """Not a GTK-measurement test against an unrealized widget (those report
    0, per this task's own investigation) -- built and asked directly for
    its allocation via Gtk.Widget.measure/size_allocate, the same technique
    test_the_confidence_legend_costs_the_protein_no_height already uses
    elsewhere in this file for a real, non-zero layout answer."""
    rail = Gtk.Box()
    hero = Gtk.Label(label="hero")
    hero.set_hexpand(True)

    root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    root.set_layout_manager(
        app_module._ResponsiveSplitLayout(rail, orientation=Gtk.Orientation.HORIZONTAL))
    root.append(hero)
    root.append(rail)

    root.size_allocate(Gdk.Rectangle(x=0, y=0, width=1024, height=768), -1)
    assert rail.get_width() == app_module.rail_width_for(1024)

    root.size_allocate(Gdk.Rectangle(x=0, y=0, width=1920, height=1080), -1)
    assert rail.get_width() == app_module._SIDE_RAIL_WIDTH_PX


def test_the_responsive_split_layout_does_not_raise_on_a_tiny_width():
    rail = Gtk.Box()
    hero = Gtk.Label(label="hero")
    root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    root.set_layout_manager(
        app_module._ResponsiveSplitLayout(rail, orientation=Gtk.Orientation.HORIZONTAL))
    root.append(hero)
    root.append(rail)
    root.size_allocate(Gdk.Rectangle(x=0, y=0, width=1, height=1), -1)  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k responsive_split -v`
Expected: FAIL with `AttributeError: module 'ui.app' has no attribute '_ResponsiveSplitLayout'`

- [ ] **Step 3: Implement `_ResponsiveSplitLayout`**

Add near `_PinnedNaturalBoxLayout`/`_FixedWidthBox` (both already solve a related "make this
box's width mean something real" problem):

```python
class _ResponsiveSplitLayout(Gtk.BoxLayout):
    """The rail/hero split's own layout manager: on every real allocation
    (window build, and any later live resize), recomputes the rail's width
    from the container's actual current width via `rail_width_for` and
    re-applies it as the rail's size request BEFORE delegating to the
    ordinary box-layout allocation that actually places both children.

    Proven against a real, running, off-screen GTK4 window before this
    class was written (see this task's docstring in the plan this class
    was implemented from) -- `do_allocate` receives the container's live
    width on every real layout pass, a child's `set_size_request` called
    from inside it takes effect in that same pass, and this is the same
    "subclass the layout manager, not the widget" shape
    `_PinnedNaturalBoxLayout` above already uses (`gtk_widget_measure`
    delegates to the layout manager, not to a widget subclass's own
    vfunc -- confirmed once already, for that class).
    """

    def __init__(self, rail_widget, **kwargs):
        super().__init__(**kwargs)
        self._rail = rail_widget

    def do_allocate(self, widget, width, height, baseline):
        self._rail.set_size_request(rail_width_for(width), -1)
        Gtk.BoxLayout.do_allocate(self, widget, width, height, baseline)
```

Then, in `do_activate`, replace:

```python
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root.add_css_class("booth-root")
        root.append(hero)
        root.append(self._build_side_rail())
```

with:

```python
        side_rail = self._build_side_rail()
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root.add_css_class("booth-root")
        root.set_layout_manager(
            _ResponsiveSplitLayout(side_rail, orientation=Gtk.Orientation.HORIZONTAL,
                                    spacing=0))
        root.append(hero)
        root.append(side_rail)
```

And in `_build_side_rail`, remove the now-superseded fixed-width line (the layout manager
sets it on every allocation instead):

```python
        side.set_size_request(_SIDE_RAIL_WIDTH_PX, -1)
```

Leave `side.set_hexpand(False)` in place — that is still what stops the rail from claiming
extra space itself; the layout manager only decides HOW MUCH floor it gets.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k "responsive_split" -v`
Expected: 2 passed

- [ ] **Step 5: Run the full existing rail-width regression tests**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k "rail_width or fixed_width" -v`
Expected: all still pass — `test_the_rail_is_552px_wide...` (line ~1283) and the
`rail.get_size_request()[0] == _SIDE_RAIL_WIDTH_PX` test (line ~1316) build the app at the
reference size, where `rail_width_for(1920) == 552`, so their existing assertions hold
unmodified.

- [ ] **Step 6: Commit**

```bash
git add ui/app.py tests/unit/test_app_interaction.py
git commit -m "feat: the side rail's width now responds to the window's real size"
```

---

## Task 5: Gallery width uses `_expected_window_width`, not the fixed constant

**Files:**
- Modify: `ui/app.py` (`_build_gallery`, uses `width_px=_GALLERY_WIDTH_PX` at line ~2605)
- Test: `tests/unit/test_app_interaction.py`

**Interfaces:**
- Consumes: `rail_width_for` (Task 1), `_expected_window_width` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
def test_gallery_width_shrinks_the_rail_out_at_the_floor_size():
    """Not a pixel-exact assertion (Gallery's own grid_shape rounds to whole
    columns) -- the invariant is that the width handed to Gallery at the
    floor size is meaningfully smaller than at the reference size, proving
    the live window width is actually reaching this call and not the old
    fixed constant."""
    app = _app()
    app.windowed = True  # _expected_window_width returns the literal 1280 default
    app.targets = [_target("dna", "DNA double helix", "tagline")]
    app._load_questions()
    app._build_gallery()
    expected = 1280 - app_module.rail_width_for(1280)
    assert app.gallery.width_px == expected
    assert expected < app_module._GALLERY_WIDTH_PX
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k gallery_width_shrinks -v`
Expected: FAIL — `app.gallery.width_px` is currently always `_GALLERY_WIDTH_PX` regardless of
`app.windowed`.

- [ ] **Step 3: Update `_build_gallery`**

Replace:

```python
        self.gallery = Gallery(self.targets, on_pick=self._on_pick,
                               width_px=_GALLERY_WIDTH_PX,
                               questions=self.questions, on_ask=self._on_ask)
```

with:

```python
        expected_width = self._expected_window_width()
        gallery_width = expected_width - rail_width_for(expected_width)
        self.gallery = Gallery(self.targets, on_pick=self._on_pick,
                               width_px=gallery_width,
                               questions=self.questions, on_ask=self._on_ask)
```

Leave `_GALLERY_WIDTH_PX` itself defined (do not delete it) — the confidence-legend and
help-card tests in Task 6/7 still reference it as "the reference-size expected value" to
compare against, and `Gallery`'s own docstring/tests may still cite it.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k gallery_width_shrinks -v`
Expected: PASS

- [ ] **Step 5: Run the full gallery test file to confirm no regression at the reference size**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k gallery -v`
Expected: all pass — every existing test builds `_app()` with `windowed` defaulting to
`False` and no monitor stub, which per Task 3 falls back to `1920` when `Gdk.Display` reports
a monitor in the test environment, or `1920` outright if it reports none — either way,
`gallery_width == 1920 - rail_width_for(1920) == 1920 - 552 == _GALLERY_WIDTH_PX`, identical
to today.

- [ ] **Step 6: Commit**

```bash
git add ui/app.py tests/unit/test_app_interaction.py
git commit -m "feat: gallery width follows the window's expected width, not a fixed constant"
```

---

## Task 6: Help card width uses the same live-width path

**Files:**
- Modify: `ui/app.py` (`_build_help_overlay`, uses `card.set_size_request(_HELP_CARD_WIDTH_PX, -1)`)
- Test: `tests/unit/test_app_interaction.py`

**Interfaces:**
- Consumes: `help_card_width_for` (Task 2), `_expected_window_width` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
def test_help_card_shrinks_to_fit_a_narrow_windowed_booth():
    app = _app()
    app.windowed = True
    card = app_module.DemoApp._build_help_overlay(app)
    expected = app_module.help_card_width_for(1280)
    assert card.get_size_request()[0] == expected
    assert expected < app_module._HELP_CARD_WIDTH_PX
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k help_card_shrinks -v`
Expected: FAIL — the card is currently always `_HELP_CARD_WIDTH_PX` wide.

- [ ] **Step 3: Update `_build_help_overlay`**

Replace:

```python
        card.set_size_request(_HELP_CARD_WIDTH_PX, -1)
```

with:

```python
        card.set_size_request(help_card_width_for(self._expected_window_width()), -1)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k help_card_shrinks -v`
Expected: PASS

- [ ] **Step 5: Extend the existing help-card-fits-the-screen test to the size matrix**

Find `test_the_help_card_still_fits_the_booth_s_own_screen` (line ~805) and replace its
single reference-size measurement with a loop over the matrix, using each size's own screen
HEIGHT as the bound (a 768px-tall floor screen is a different budget than the 1080px
reference, not the same number reused):

```python
def test_the_help_card_still_fits_the_booth_s_own_screen():
    """... (existing docstring stays; extended 2026-09-23 to a size matrix
    per docs/superpowers/specs/2026-09-23-responsive-layout-design.md --
    each shipped resolution gets checked against ITS OWN screen height, not
    the reference 1080 reused for every width, which would silently pass a
    card that overflows a shorter floor-size screen)."""
    matrix = [(1024, 768), (1366, 768), (1920, 1080), (2560, 1440)]
    too_tall = []
    for total_width, total_height in matrix:
        app = _app()
        app._expected_window_width = lambda w=total_width: w
        card_width = app_module.help_card_width_for(total_width)
        card = app_module.DemoApp._build_help_overlay(app)

        def show(widget):
            widget.set_visible(True)
            child = widget.get_first_child()
            while child is not None:
                show(child)
                child = child.get_next_sibling()
        show(card)

        _minimum, natural, _, _ = card.measure(Gtk.Orientation.VERTICAL, card_width)
        if natural > total_height:
            too_tall.append(
                f"{total_width}x{total_height}: card wants {natural}px tall")
    assert not too_tall, "\n".join(too_tall)
```

Note the `app._expected_window_width = lambda w=total_width: w` monkeypatch inside the loop
(shown above): `_build_help_overlay` calls `self._expected_window_width()` directly (Step 3
of this task), so overriding that one method on the fake app is the whole seam needed — no
new method, no second way to inject a width.

- [ ] **Step 6: Run the extended test**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k help_card_still_fits -v`
Expected: PASS. If any matrix entry fails, that is a real finding (the copy trimmed for
1400px-wide wrapping may wrap differently narrower) — trim that paragraph further rather
than widening `_HELP_CARD_MIN_PX` past what Task 2 chose, per the spec's "measure, don't
assume" instruction; report the specific failing size and line if this happens.

- [ ] **Step 7: Commit**

```bash
git add ui/app.py tests/unit/test_app_interaction.py
git commit -m "feat: help card width responsive; its own fits-the-screen test now checks the matrix"
```

---

## Task 7: Confidence-legend strip re-verified at the floor width

**Files:**
- Modify: `tests/unit/test_app_interaction.py` only — no production code change; this is the
  spec's §5 "re-verify, not re-design" instruction.

**Interfaces:**
- Consumes: `rail_width_for` (Task 1), the existing `_strip_height` helper and
  `test_the_confidence_legend_costs_the_protein_no_height` (line ~1938).

- [ ] **Step 1: Write the new (initially may pass or fail — this is a real measurement,
  not a known-answer test) width-matrix version**

Add alongside the existing reference-size test, not replacing it:

```python
def test_the_confidence_legend_still_costs_nothing_at_the_floor_width():
    """The reference-size version of this test (above) already proved the
    legend costs the render no height AT 1920. This proves the same
    invariant at the narrowest supported width, where a tagline that fit
    comfortably beside the legend at 1920 might wrap differently in a
    meaningfully narrower column -- checked, not assumed, per the spec."""
    floor_width = 1024 - app_module.rail_width_for(1024)
    targets = load_playlist(app_module._DEFAULT_PLAYLIST)
    assert targets

    too_tall = []
    for target in targets:
        app = _app()
        app.targets = [target]
        app._slots[0].shown_target_id = target.id
        strip = app._build_target_info()
        with_legend = _strip_height(strip, floor_width)
        app._confidence_legend_box.unparent()
        without_legend = _strip_height(strip, floor_width)
        if with_legend != without_legend:
            too_tall.append(
                f"{target.id} at {floor_width}px: {with_legend}px with the legend, "
                f"{without_legend}px without -- the render loses "
                f"{with_legend - without_legend}px at the floor width")
    assert not too_tall, "\n".join(too_tall)
```

- [ ] **Step 2: Run it**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_app_interaction.py -k confidence_legend_still_costs -v -s`
Expected: PASS. If it fails, that is real, useful information this plan exists to surface —
report which target(s) failed and by how much; do not loosen the assertion or widen
`_RAIL_MIN_PX`/narrow the floor to make it pass without looking at why.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_app_interaction.py
git commit -m "test: confirm the confidence legend still costs no height at the floor width"
```

---

## Task 8: Real-app visual verification via headless weston (no Tenstorrent hardware)

**Files:**
- Create: `scripts/verify-responsive-layout.sh` (a throwaway-friendly, but real and rerunnable,
  verification script — not a pytest test, since it drives a real running app + real
  screenshots)

**Interfaces:**
- Consumes: `runner.mock.MockRunner` (already exists, `runner/mock.py`) — no daemon, no
  device, no Tenstorrent chip touched at any point in this task.
- Produces: PNG screenshots under a scratch directory, for a human (or a later `Read` call)
  to look at.

This task's recipe was proven working, end to end, against a real running instance of this
exact booth, before this plan was written — see the design spec's §7. It needs weston
installed (`apt-get install -y weston`) but no other new dependency.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# verify-responsive-layout.sh -- screenshot the real booth app at a matrix of window
# sizes, using a headless weston compositor and a mock daemon. Touches no Tenstorrent
# device: runner.mock.MockRunner replays a recorded fixture over a real Unix socket,
# which is all ui.app needs to have something real to draw.
#
# Usage: scripts/verify-responsive-layout.sh [out-dir]
# Requires: weston (apt-get install -y weston), .venvs/venv-ui built.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_UI="${REPO_ROOT}/.venvs/venv-ui"
OUT_DIR="${1:-/tmp/responsive-layout-shots}"
mkdir -p "$OUT_DIR"

if ! command -v weston >/dev/null 2>&1; then
  echo "weston not found -- sudo apt-get install -y weston" >&2
  exit 1
fi

SIZES=("1024x768" "1366x768" "1920x1080" "2560x1440")
SOCKET="${OUT_DIR}/mock.sock"

for size in "${SIZES[@]}"; do
  width="${size%x*}"
  height="${size#*x}"
  echo "== ${size} ==" >&2

  wl_socket="verify-${size}"
  rm -f "/run/user/$(id -u)/${wl_socket}"*
  weston --debug --backend=headless --renderer=pixman \
    --width="$width" --height="$height" --socket="$wl_socket" --idle-time=0 \
    > "${OUT_DIR}/weston-${size}.log" 2>&1 &
  weston_pid=$!
  sleep 2

  rm -f "$SOCKET"
  "${VENV_UI}/bin/python3" -c "
from runner.mock import MockRunner, load_stream
import time
r = MockRunner('${SOCKET}', load_stream('tests/fixtures/streams/with_question.jsonl'), speed=1.0)
r.start()
time.sleep(3600)
" > "${OUT_DIR}/mock-${size}.log" 2>&1 &
  mock_pid=$!
  sleep 1

  WAYLAND_DISPLAY="$wl_socket" unset DISPLAY 2>/dev/null || true
  env WAYLAND_DISPLAY="$wl_socket" "${VENV_UI}/bin/python3" -m ui.app \
    --socket "$SOCKET" --playlist "${REPO_ROOT}/playlist/manifest.yaml" \
    > "${OUT_DIR}/app-${size}.log" 2>&1 &
  app_pid=$!
  sleep 5

  (cd "$OUT_DIR" && WAYLAND_DISPLAY="$wl_socket" weston-screenshooter) \
    > "${OUT_DIR}/shot-${size}.log" 2>&1 || true
  mv "${OUT_DIR}"/wayland-screenshot*.png "${OUT_DIR}/${size}.png" 2>/dev/null || \
    echo "WARNING: no screenshot produced for ${size}, see ${OUT_DIR}/shot-${size}.log" >&2

  kill -TERM "$app_pid" "$mock_pid" "$weston_pid" 2>/dev/null || true
  wait "$app_pid" "$mock_pid" "$weston_pid" 2>/dev/null || true
done

echo "Screenshots in ${OUT_DIR}/*.png -- look at each one." >&2
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/verify-responsive-layout.sh
```

- [ ] **Step 3: Run it**

Run: `scripts/verify-responsive-layout.sh`
Expected: four PNGs under `/tmp/responsive-layout-shots/` (or the directory you passed),
one per matrix size, each showing the real booth app — rail, hero content, and (once a
`with_question.jsonl`-driven `job_start`/`frame` arrives) real folding content, all present
and legible with no visibly squeezed or overlapping panels.

- [ ] **Step 4: Actually look at each screenshot**

Use `Read` on each of the four PNGs. Confirm, at each size:
- The rail (pipeline/telemetry panels) is fully visible, no text cut off.
- The hero/gallery content is not squeezed into a sliver.
- Nothing overlaps.

If any size looks wrong, that is this task's real finding — fix the responsible task above
and re-run this script, not a reason to weaken this task's own check.

- [ ] **Step 5: Commit the script (not the screenshots — they're scratch output)**

```bash
git add scripts/verify-responsive-layout.sh
git commit -m "test: a repeatable, hardware-free visual verification script for the booth's layout"
```

---

## Task 9: Whole-plan regression pass

**Files:** none new — this task runs the existing suite, no code changes expected.

- [ ] **Step 1: Run the full non-hardware suite**

Run: `scripts/test.sh`
Expected: `OVERALL: PASS`, both halves green, same total test count as before this plan plus
the new tests this plan added (Tasks 1–7).

- [ ] **Step 2: If anything is red, fix it in the task that owns the broken behavior**

Do not patch a failure from this task directly — trace it back to the task whose change
caused it and fix it there, so that task's own commit stays the one that introduced (and
fixed) the regression.

- [ ] **Step 3: Final commit if any fixes were needed**

```bash
git add -A
git commit -m "fix: whole-branch regression pass for the responsive layout work"
```
