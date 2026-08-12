"""Booth panels: card telemetry and fold-pipeline progress.

Two composite widgets (`TelemetryPanel`, `PipelinePanel`) plus the pure
decisions they render (`card_color`, `stage_rows`), kept deliberately
separate per the task brief: "Keep the drawing thin and the decisions pure."
Neither `card_color` nor `stage_rows` touches GTK, imports torch, or imports
tt_bio -- both are plain functions over numbers and strings, tested directly
in tests/unit/test_panels.py. The widgets are thin assembly on top of them:
they own layout, CSS classes, and text formatting, and nothing else.

Base class note: the brief's produces line types these as
`TelemetryPanel(Gtk.Widget)` / `PipelinePanel(Gtk.Widget)`. Both are
implemented here as `Gtk.Box` subclasses instead -- `Gtk.Box` IS a
`Gtk.Widget` (isinstance(panel, Gtk.Widget) holds), but a *direct* Gtk.Widget
subclass in GTK4 has no layout manager of its own and must implement
`do_measure`/`do_size_allocate` (or install a `Gtk.LayoutManager`) before it
can arrange any children at all -- real work with no behavioral payoff over
`Gtk.Box`, which already does exactly what a "card row" / "stage list"
container needs. Every other custom widget already in this codebase
(`ui/viewer.py`'s `StructureViewer`) subclasses a GTK type that supplies the
behavior it needs (`Gtk.GLArea`) rather than raw `Gtk.Widget`, for the same
reason.
"""

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

from protocol.events import STAGE_ORDER

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand palette -- this project's, from the Tenstorrent docs theme (see the
# task brief). Named constants rather than literals scattered through the
# CSS/widget code below, matching ui/app.py's `_PREPARING_CSS` convention.
# ---------------------------------------------------------------------------
_DARK_BASE = "#092221"
_BG = "#F1F8F8"
_BG_ALT = "#C7D9D8"
_BG_ALT2 = "#DCF0EE"
_ACCENT = "#1B8EB1"
_TEAL = "#74C5DF"
_GREEN = "#6FABA0"
_YELLOW = "#F6BC42"
_RED = "#FF9E8A"
_ORANGE = "#FA512E"

# card_color()'s two possible outputs. Green reads as healthy at a glance;
# orange is the "something's wrong" register runner/cards.py's own
# "quarantined" card state already uses conceptually. Deliberately distinct
# from `_RED`, which TelemetryPanel reserves for its own "no telemetry at
# all" status text below -- a hot card and a dead sampler must not read as
# the same kind of trouble.
_CARD_NORMAL_COLOR = _GREEN
_CARD_HOT_COLOR = _ORANGE


# ---------------------------------------------------------------------------
# Pure decision: which color a card's temperature reads as.
# ---------------------------------------------------------------------------

def card_color(temperature_c, max_temp_c=85.0):
    """The color a telemetry card should render at, from temperature alone.

    Binary, not a gradient: every temperature below `max_temp_c` reads as
    the exact same color, and every temperature at or above it reads as the
    other exact same color -- a visitor should be able to tell "fine" from
    "not fine" at a glance, not have to interpret which of several shades of
    green a card is showing. `max_temp_c` defaults to 85.0 to match
    runner/cards.py's `CardPool` quarantine threshold exactly, including the
    `>=` boundary: a card sampled at precisely `max_temp_c` is already
    quarantined there (`CardPool.update`: `self._hot[...] = temperature_c >=
    self.max_temp_c`), not merely "warm" -- so this uses the same `>=`, not
    `>`, to stay consistent with the fact this color is describing.
    """
    return _CARD_HOT_COLOR if temperature_c >= max_temp_c else _CARD_NORMAL_COLOR


# ---------------------------------------------------------------------------
# Pure decision: one row per protocol stage, for the pipeline panel.
# ---------------------------------------------------------------------------

def stage_rows(current, frac):
    """Build one `(stage, progress, state)` row per `protocol.events.
    STAGE_ORDER` entry, describing a fold currently at stage `current`.

    `frac` is the WITHIN-STAGE fraction (0.0-1.0) of `current`'s own
    progress -- how far through *that stage's* work the fold is -- NOT the
    wire's whole-fold fraction. The wire's `stage` event reports a
    whole-fold fraction with contiguous bands (see `protocol.events.
    STAGE_BANDS`), designed so a single overall progress bar never runs
    backwards across a stage transition; a per-row bar wants the opposite
    property, restarting at 0.0 for every stage. Converting one into the
    other is `protocol.events.within_stage_frac`'s job, not this function's
    -- call `within_stage_frac(current, wire_frac)` first and pass ITS
    result in here. (Passing the raw wire fraction straight through would
    make `current`'s own row appear to start already-partway-done and never
    visibly reach 100% -- exactly the kind of bug the task brief warns is
    "invisible until a bar sits at 15% through the whole of diffusion.")

    Per row, in `STAGE_ORDER`'s own order:

    - a stage strictly BEFORE `current`: `(stage, 1.0, "done")`.
    - `current` itself: `(stage, frac, "active")` -- `frac` is clamped to
      [0.0, 1.0] so a wire glitch (or a caller that forgot the
      `within_stage_frac` conversion above) can't overflow/underflow a
      progress bar.
    - a stage strictly AFTER `current`: `(stage, 0.0, "pending")`.

    An unrecognized `current` (a future protocol stage this build's copy of
    STAGE_ORDER doesn't know about) must not raise or guess where in the
    order it might belong: every row reads `(stage, 0.0, "pending")` in that
    case -- the same "nothing has happened yet" reading `PipelinePanel.
    reset()` uses, and the honest one: this function has no way to know
    whether the real fold is one stage in or five, so it claims none of the
    stages it *does* know about are done rather than guessing.
    """
    try:
        current_index = STAGE_ORDER.index(current)
    except ValueError:
        current_index = None

    rows = []
    for index, stage in enumerate(STAGE_ORDER):
        if current_index is None:
            rows.append((stage, 0.0, "pending"))
        elif index < current_index:
            rows.append((stage, 1.0, "done"))
        elif index == current_index:
            clamped = max(0.0, min(1.0, float(frac)))
            rows.append((stage, clamped, "active"))
        else:
            rows.append((stage, 0.0, "pending"))
    return rows


# ---------------------------------------------------------------------------
# CSS, installed once against the default display the first time either
# widget below is constructed. Guarded on a live display existing at all
# (Gdk.Display.get_default() is None in a fully headless process, e.g. some
# future test run with no DISPLAY/WAYLAND_DISPLAY) so constructing either
# panel never hard-requires one -- matching the rest of this codebase's
# convention that GTK object construction alone should not need a live
# display (see tests/unit/test_app_handle_event.py's module docstring).
# ---------------------------------------------------------------------------
_CSS_INSTALLED = False

_PANEL_CSS = f"""
.telemetry-panel {{
    background-color: {_BG};
    padding: 10px;
    border-radius: 6px;
}}
.telemetry-status {{
    font-weight: bold;
    color: {_DARK_BASE};
}}
.telemetry-status.telemetry-unknown {{
    color: {_RED};
}}
.telemetry-status.telemetry-empty {{
    color: {_ACCENT};
}}
.telemetry-status.telemetry-stale {{
    color: {_YELLOW};
    font-style: italic;
}}
.telemetry-card {{
    background-color: {_BG_ALT2};
    padding: 6px 10px;
    border-radius: 4px;
    border: 2px solid {_BG_ALT};
}}
.telemetry-card-normal {{
    border-color: {_CARD_NORMAL_COLOR};
}}
.telemetry-card-hot {{
    border-color: {_CARD_HOT_COLOR};
}}
.telemetry-card-title {{
    font-weight: bold;
    color: {_DARK_BASE};
}}
.pipeline-panel {{
    background-color: {_BG};
    padding: 10px;
    border-radius: 6px;
}}
.pipeline-stage-name {{
    min-width: 90px;
}}
/* Label text color per row state. */
.stage-done {{
    color: {_GREEN};
}}
.stage-active {{
    color: {_ACCENT};
    font-weight: bold;
}}
.stage-pending {{
    color: {_BG_ALT};
}}
/* Gtk.ProgressBar's FILL lives on a nested `progress` node inside `trough`
   (see GTK4's widget node tree for progressbar) -- a plain `color:` rule on
   the progressbar widget itself (the block above) only affects text, which
   Gtk.ProgressBar doesn't draw any of, so the three states would otherwise
   be visually IDENTICAL bars (all default theme blue) despite the correct
   CSS classes being applied. These descendant-selector rules are what
   actually paints each row a different color.
   confidence/saving may report at 0.0 through most of a fold and so
   otherwise show no fill at all -- give trough itself a state color too
   (a very light one for pending, invisible against the panel background
   for done/active since progress covers the trough at fraction 1.0/partial
   anyway) so a viewer can still tell the three ROWS apart even before any
   fill is visible. */
.stage-done trough {{
    background-color: {_BG_ALT2}; /* barely-there track under a full green fill */
}}
.stage-done trough progress {{
    background-color: {_GREEN};
}}
.stage-active trough {{
    background-color: {_BG_ALT2};
}}
.stage-active trough progress {{
    background-color: {_ACCENT};
}}
.stage-pending trough {{
    background-color: {_BG_ALT};
}}
.stage-pending trough progress {{
    background-color: {_BG_ALT};
}}
""".encode("utf-8")


def _ensure_css_installed():
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        log.debug("no default display; skipping panel CSS install")
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_PANEL_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


# One CSS class per possible telemetry-status/card/stage state -- listed
# once here so both widgets can reset ("remove every state class") without
# re-deriving the class name from the state string, and without a stale
# class from a previous render leaking into the next one (e.g. a card that
# was hot on the last update reads normal now, but GTK doesn't remove CSS
# classes on its own just because a caller stopped adding them).
_TELEMETRY_STATUS_CLASSES = (
    "telemetry-unknown", "telemetry-empty", "telemetry-ok", "telemetry-stale",
)
_CARD_STATE_CLASSES = ("telemetry-card-normal", "telemetry-card-hot")
_STAGE_STATE_CLASSES = ("stage-done", "stage-active", "stage-pending")

# How long a reading may go un-refreshed before the panel calls it stale
# rather than live. 3x TelemetrySampler's own default `period_s` (2.0s, see
# ui/telemetry.py) -- generous enough that one slow-but-eventually-answering
# `tt-smi` poll (it can legitimately take a couple of seconds under load)
# never trips this, while a sampler that has genuinely stopped answering
# (thread died, or every subsequent poll is failing and being ignored per
# the tri-state contract) is flagged within three missed cycles, not left
# looking like a live reading indefinitely.
STALE_AFTER_S = 6.0


class TelemetryPanel(Gtk.Box):
    """Renders `ui.telemetry.TelemetrySampler`'s tri-state `latest()` /
    `age_s()` as one card per device, via `.update(readings, age_s)`.

    The tri-state (see ui/telemetry.py's module docstring) is rendered as
    three states a visitor -- not just an operator reading logs -- can tell
    apart on sight, per this task's controller ruling ("no telemetry is not
    fake telemetry"):

    - `readings is None`: `tt-smi` has never produced a usable answer (or
      every device in its very first snapshot was unreadable). No cards are
      drawn at all; the status line reads as a clear "no telemetry" state,
      never as a card showing 0C/0W, which would look like real hardware
      idling rather than a sampler with nothing to report.
    - `readings == []`: `tt-smi` answered and truthfully reported zero
      devices. Also no cards drawn, but a visually distinct, calmer status
      line -- this is real information ("no cards detected"), not a
      failure.
    - `readings` non-empty: one card per reading, colored via `card_color`.

    Independently of which of the three states above applies, `age_s`
    (seconds since the last successful sample, or `None` if there has never
    been one) drives a staleness note once it passes `STALE_AFTER_S`: the
    thing this adds is telling "the cards are genuinely idle" (a fresh `[]`
    or a fresh non-empty reading) apart from "we have not heard from tt-smi
    in a while" (an OLD non-empty or `[]` reading whose sampler may have
    wedged) -- see the module-level `STALE_AFTER_S` docstring for the
    threshold's rationale.
    """

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        _ensure_css_installed()
        self.add_css_class("telemetry-panel")

        self._status_label = Gtk.Label(xalign=0.0)
        self._status_label.add_css_class("telemetry-status")
        self.append(self._status_label)

        self._cards_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.append(self._cards_box)

        # Rendered state, kept for tests and diagnostics -- not read by GTK
        # itself, but it lets a test assert "what did the panel just decide
        # to show" without scraping widget text back out.
        self.last_status = None

        self.update(None, None)

    def update(self, readings, age_s):
        """Render one poll result. See the class docstring for the
        three-way `readings` split and how `age_s` layers a staleness note
        on top of any of them."""
        self._clear_cards()
        for css_class in _TELEMETRY_STATUS_CLASSES:
            self._status_label.remove_css_class(css_class)

        stale = age_s is not None and age_s >= STALE_AFTER_S

        if readings is None:
            # No prior good sample exists (see ui/telemetry.py: this is the
            # ONLY case age_s is also None) -- there is nothing to call
            # stale, only unknown from the start.
            self.last_status = "unknown"
            self._status_label.set_label("NO TELEMETRY — tt-smi has not answered")
            self._status_label.add_css_class("telemetry-unknown")
            return

        if not readings:
            self.last_status = "stale-empty" if stale else "empty"
            self._status_label.set_label(
                self._with_staleness_note("No Tenstorrent cards detected", age_s, stale))
            self._status_label.add_css_class("telemetry-empty")
            if stale:
                self._status_label.add_css_class("telemetry-stale")
            return

        self.last_status = "stale-ok" if stale else "ok"
        card_word = "card" if len(readings) == 1 else "cards"
        self._status_label.set_label(
            self._with_staleness_note(f"{len(readings)} {card_word}", age_s, stale))
        self._status_label.add_css_class("telemetry-ok")
        if stale:
            self._status_label.add_css_class("telemetry-stale")
        for reading in readings:
            self._cards_box.append(self._build_card(reading))

    @staticmethod
    def _with_staleness_note(text, age_s, stale):
        if not stale:
            return text
        return f"{text} — stale, last heard {age_s:.0f}s ago"

    def _clear_cards(self):
        child = self._cards_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._cards_box.remove(child)
            child = following

    def _build_card(self, reading):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("telemetry-card")
        color = card_color(reading.temperature_c)
        box.add_css_class(
            "telemetry-card-hot" if color == _CARD_HOT_COLOR else "telemetry-card-normal")

        title = Gtk.Label(xalign=0.0, label=f"card {reading.index} · {reading.board_type}")
        title.add_css_class("telemetry-card-title")
        temp = Gtk.Label(xalign=0.0, label=f"{reading.temperature_c:.1f} °C")
        power = Gtk.Label(xalign=0.0, label=f"{reading.power_w:.0f} W")
        clock = Gtk.Label(xalign=0.0, label=f"{reading.aiclk_mhz:.0f} MHz")
        for widget in (title, temp, power, clock):
            box.append(widget)
        return box


class PipelinePanel(Gtk.Box):
    """One row per protocol stage, driven by `.set_stage(stage, frac)`.

    Thin assembly over `stage_rows`: this class owns no progress logic of
    its own, only the mapping from each `(name, fraction, state)` row to a
    label + progress bar and a CSS class for `state`. `frac` here is the
    WITHIN-STAGE fraction `stage_rows` expects -- see its docstring; this
    panel does not call `protocol.events.within_stage_frac` itself, that is
    the wiring layer's job (the daemon-driven caller has the wire's raw
    whole-fold fraction and the stage name; this panel only ever sees
    already-converted numbers).
    """

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        _ensure_css_installed()
        self.add_css_class("pipeline-panel")

        self._rows = {}
        for stage in STAGE_ORDER:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            label = Gtk.Label(label=stage, xalign=0.0)
            label.add_css_class("pipeline-stage-name")
            bar = Gtk.ProgressBar()
            bar.set_hexpand(True)
            row.append(label)
            row.append(bar)
            self.append(row)
            self._rows[stage] = (label, bar)

        # Rendered state, kept for tests/diagnostics -- see TelemetryPanel's
        # equivalent `last_status` field for why.
        self.last_rows = []

        self.reset()

    def set_stage(self, stage, frac):
        """Render one `stage` event's worth of progress. `frac` must
        already be the WITHIN-STAGE fraction (see the class docstring)."""
        rows = stage_rows(stage, frac)
        self.last_rows = rows
        for name, value, state in rows:
            label, bar = self._rows[name]
            bar.set_fraction(value)
            for css_class in _STAGE_STATE_CLASSES:
                label.remove_css_class(css_class)
                bar.remove_css_class(css_class)
            new_class = f"stage-{state}"
            label.add_css_class(new_class)
            bar.add_css_class(new_class)

    def reset(self):
        """Back to "nothing has happened yet": every stage pending, every
        bar empty. Deliberately reuses `stage_rows`' own unrecognized-stage
        fallback (see its docstring) rather than a second, separately
        maintained "all pending" code path -- one behavior, tested once."""
        self.set_stage(None, 0.0)
