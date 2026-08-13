"""Booth panels: per-chip telemetry and fold-pipeline progress.

Two composite widgets (`TelemetryPanel`, `PipelinePanel`) plus the pure
decisions they render (`card_color`, `stage_rows`), kept deliberately
separate per the task brief: "Keep the drawing thin and the decisions pure."
Neither `card_color` nor `stage_rows` touches GTK, imports torch, or imports
tt_bio -- both are plain functions over numbers and strings, tested directly
in tests/unit/test_panels.py. The widgets are thin assembly on top of them:
they own layout, CSS classes, and text formatting, and nothing else.

Visual design (fix round 1): built on the dark base (`_DARK_BASE`), not the
light backgrounds -- the brand's light tints (`_TEAL`/`_YELLOW`/`_RED`/
`_GREEN`) are designed to sit there (see the docs-site theme this palette
comes from), and putting them where they belong is what makes them both
legible (WCAG AA, >=4.5:1 -- see `contrast_ratio`/`MIN_CONTRAST_RATIO`
below) and on-brand at the same time, instead of fighting the palette with
hand-darkened one-off variants. Colour is spent sparingly: only the pipeline
panel's ACTIVE row and a chip that has crossed the quarantine line ever get
a saturated accent -- everything else reads by weight, fill, and text
content, which is also what keeps the tri-state (see `TelemetryPanel`)
distinguishable without relying on colour alone. Numbers are the largest,
brightest thing in the telemetry panel, in a monospace face (falls back to
whatever's installed if the named brand mono isn't present) so a value does
not visibly jitter column-to-column as it ticks.

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
import math
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

# Pango, for `EllipsizeMode` -- the one thing that makes this module's fixed
# cell widths a guarantee rather than a hope (see the reserved-footprint
# block below `PIPELINE_STALE_AFTER_S`). Version-pinned like the others so a
# stray unversioned import cannot decide it for us.
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, Gtk, Pango

from protocol.events import STAGE_ORDER, within_stage_frac
from ui.telemetry import distinct_board_count

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

# `_ACCENT` itself measures 4.40:1 against `_DARK_BASE` -- just UNDER the
# 4.5:1 AA floor this module holds itself to (see MIN_CONTRAST_RATIO below),
# so it cannot be used as TEXT on the dark ground even though it reads fine
# as a decorative fill (the pipeline panel's active progress bar uses the
# pure brand hex; fills are exempt, see the legibility test's own docstring
# in tests/unit/test_panels.py). This is a minimally-lightened tint of the
# exact same hue -- +10% toward white -- computed once and pinned here
# rather than derived at runtime, so `contrast_ratio(_ACCENT_TEXT,
# _DARK_BASE)` is a fixed, testable number (5.06:1, comfortable margin above
# 4.5). Used ONLY where accent colour is asked for as TEXT (the active
# pipeline row's label).
_ACCENT_TEXT = "#3299B9"

# card_color()'s two possible outputs. Colour is spent sparingly here too:
# a chip under the line gets a neutral, unremarkable reading (`_BG_ALT` --
# nothing to see); a chip AT OR OVER `max_temp_c` is the one place this
# panel's own colour is meant to say something, so it gets the same
# "something's wrong" register `runner/cards.py`'s "quarantined" card state
# already uses conceptually.
_CARD_NORMAL_COLOR = _BG_ALT
_CARD_HOT_COLOR = _ORANGE


# ---------------------------------------------------------------------------
# Pure decision: WCAG 2.x contrast, so the legibility guarantee is a real,
# tested calculation rather than an eyeballed palette choice. Used by
# tests/unit/test_panels.py's generalized "every label must be legible"
# check (which walks the real, rendered widget tree) and by the design note
# above `_ACCENT_TEXT`.
# ---------------------------------------------------------------------------

def relative_luminance(hex_color):
    """WCAG 2.x relative luminance of a `#RRGGBB` colour, in [0.0, 1.0].

    Standard formula (see W3C's WCAG 2.x "Relative Luminance" definition):
    sRGB channels are linearized, then combined with the fixed 0.2126 /
    0.7152 / 0.0722 weights that approximate human luminance sensitivity
    (green contributes most, blue least).
    """
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    def _linearize(channel):
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = _linearize(r), _linearize(g), _linearize(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a, hex_b):
    """WCAG 2.x contrast ratio between two `#RRGGBB` colours.

    Ranges from 1.0 (no contrast at all -- identical luminance) to 21.0
    (pure black on pure white). Symmetric: which colour is "foreground" and
    which is "background" does not matter, matching the WCAG formula's own
    definition (lighter luminance over darker, regardless of argument
    order).
    """
    l_a, l_b = relative_luminance(hex_a), relative_luminance(hex_b)
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


# WCAG 2.x "AA, normal-size text" floor. Named here, not left as a magic
# number in the test that enforces it (tests/unit/test_panels.py), because
# production colour choices in this module (`_ACCENT_TEXT` above) were
# picked specifically to clear it -- the constant is part of the design,
# not just the check.
MIN_CONTRAST_RATIO = 4.5


# ---------------------------------------------------------------------------
# Pure decision: which color a card's temperature reads as.
# ---------------------------------------------------------------------------

def card_color(temperature_c, max_temp_c=85.0):
    """The color a telemetry chip cell should render at, from temperature
    alone.

    (The FUNCTION keeps its `card_color` name deliberately, even though the
    panel now says "chip": it describes the same threshold
    `runner/cards.py`'s `CardPool` quarantines on, and that module -- the
    daemon's scheduling vocabulary -- is explicitly out of scope for the
    chips-not-boards rename. Renaming only this half would make the
    correspondence harder to see, not easier.)

    Binary, not a gradient: every temperature below `max_temp_c` reads as
    the exact same color, and every temperature at or above it reads as the
    other exact same color -- a visitor should be able to tell "fine" from
    "not fine" at a glance, not have to interpret which of several shades of
    green a chip is showing. `max_temp_c` defaults to 85.0 to match
    runner/cards.py's `CardPool` quarantine threshold exactly, including the
    `>=` boundary: a chip sampled at precisely `max_temp_c` is already
    quarantined there (`CardPool.update`: `self._hot[...] = temperature_c >=
    self.max_temp_c`), not merely "warm" -- so this uses the same `>=`, not
    `>`, to stay consistent with the fact this color is describing.

    A non-finite `temperature_c` (NaN, +-inf) is not a healthy reading --
    `ui.telemetry.parse_snapshot` is the primary defense (a non-finite
    telemetry value is now treated as unparseable at the source, the same
    as tt-smi's own "n/a" sentinel, so a real ChipReading should never carry
    one) but this function stays defensive on its own too: a NaN reads as
    the HOT color, never the normal one. There are only two buckets here,
    and "we don't have a real number" must never land in the one that looks
    healthy.
    """
    if not math.isfinite(temperature_c):
        return _CARD_HOT_COLOR
    return _CARD_HOT_COLOR if temperature_c >= max_temp_c else _CARD_NORMAL_COLOR


# ---------------------------------------------------------------------------
# Pure decision: the hero temperature's text, inside a fixed width budget.
# ---------------------------------------------------------------------------

def hero_text(temperature_c):
    """The big number on a chip cell: `"48.0°C"`, `"102°C"`.

    One tenth of a degree below 100, none at or above it. That is not a
    style preference, it is what keeps the number INSIDE the cell it has to
    fit in (`CHIP_CELL_WIDTH_PX`): at the shipped 26px monospace face,
    "100.0" is 112px against "99.9"'s 96px, and reserving the wider box for
    every chip all day would have cost the rail (and therefore the protein)
    64px permanently to accommodate a reading that means a chip is in
    serious trouble.

    Dropping the tenth there costs nothing anyone reads -- at 100°C the
    interesting fact is "100", not "100.0" -- and it is strictly better than
    the alternative the fixed width would otherwise force, which is
    ellipsizing the alarm number into "100.…" at exactly the moment someone
    needs to read it.

    Pure, so the width budget can be tested without a display.
    """
    if temperature_c >= 100.0 or temperature_c <= -100.0:
        return f"{temperature_c:.0f}°C"
    return f"{temperature_c:.1f}°C"


# ---------------------------------------------------------------------------
# Pure decision: what the telemetry panel's heading SAYS -- chips, and the
# boards they sit on.
# ---------------------------------------------------------------------------

def chip_count_text(readings, shown=None):
    """The telemetry panel's heading for a non-empty list of readings.

    This function exists because the heading was WRONG, not merely terse.
    It used to read "4 cards", which is two separate mistakes at once: a
    `tt-smi` device entry is a CHIP, not a card, and a visitor reading "4
    cards" in front of a QB2 concludes the box holds four boards. It holds
    two -- two p300c boards, each presenting two Blackhole chips (verified
    against `tt-smi -s` on this machine: bus ids 01/02:00.0 share board_id
    ...4062, 03/04:00.0 share ...4055).

    So the heading now says both numbers when both are knowable -- "4 chips
    on 2 boards" -- and that is not padding: the pair is the fact a visitor
    would otherwise get wrong in the more impressive direction, and it is
    the only place in the panel the board grouping appears. The per-chip
    cells deliberately do NOT repeat a board tag: at 10px letterspaced in a
    `CHIP_CELL_WIDTH_PX` cell there is no room for a token that says the
    same thing four times, and which board a chip sits on is a property of
    the MACHINE, not of that chip's temperature.

    Degrades, in order of what is knowable:

    - board count unavailable (`distinct_board_count` returns None -- an
      older tt-smi, or one chip's board_id unreadable): "4 chips". Never a
      guessed board count.
    - one board (all chips share a board_id): "2 chips on 1 board".
    - one chip: "1 chip" -- the board clause is dropped entirely rather
      than rendered as the vacuous "1 chip on 1 board".

    `shown` is how many CELLS the panel actually drew. When that is fewer
    than the machine has (see `MAX_CHIP_CELLS`), the heading says so --
    "8 chips on 4 boards, showing 4" -- so a capped panel can never
    under-report the hardware without admitting it. Same contract, same
    wording register, as `ui.chipviz.readout_text`'s "N/total". `None`
    means "not capped"; a `shown` that is not actually smaller than the
    count adds nothing.
    """
    chips = len(readings)
    chip_word = "chip" if chips == 1 else "chips"
    boards = distinct_board_count(readings)
    if boards is None or chips <= 1:
        text = f"{chips} {chip_word}"
    else:
        board_word = "board" if boards == 1 else "boards"
        text = f"{chips} {chip_word} on {boards} {board_word}"
    if shown is not None and shown < chips:
        text = f"{text}, showing {shown}"
    return text


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
    result in here, or use `PipelinePanel.set_stage_from_wire`, which does
    exactly that and is the entry point the daemon-driven wiring should
    actually call (see its docstring). (Passing the raw wire fraction
    straight through here would make `current`'s own row appear to start
    already-partway-done and never visibly reach 100% -- exactly the kind
    of bug the task brief warns is "invisible until a bar sits at 15%
    through the whole of diffusion.")

    Per row, in `STAGE_ORDER`'s own order:

    - a stage strictly BEFORE `current`: `(stage, 1.0, "done")`.
    - `current` itself: `(stage, frac, "active")` -- `frac` is clamped to
      [0.0, 1.0] so a wire glitch (or a caller that forgot the
      `within_stage_frac` conversion above) can't overflow/underflow a
      progress bar. A non-finite `frac` (NaN, +-inf) reads as 0.0 -- an
      unmeasured "how far in" must look empty, not (per Python's own
      `min(1.0, float('nan'))`, which silently keeps 1.0) a fully-filled
      bar claiming the stage is done when nothing of the sort is known.
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
            frac_value = float(frac)
            clamped = 0.0 if not math.isfinite(frac_value) else max(0.0, min(1.0, frac_value))
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
#
# `_BACKGROUND_BY_CLASS` is the single source of truth for "which CSS class
# carries an explicitly-set background": both panel roots below reference
# `_DARK_BASE` directly (the same constant this dict maps them to, not a
# second hand-copied literal), and tests/unit/test_panels.py's generalized
# legibility check reads THIS SAME dict to find "the nearest explicitly-set
# background" for any given label, rather than a second, independently
# maintained copy of this knowledge that could silently drift from the real
# stylesheet. Everything in this module now sits on one background tier
# (the dark ground) -- no nested card/chip backgrounds -- which is also why
# both entries map to the same colour; if a future redesign adds a second
# tier, add its class here and the legibility test picks it up for free.
# ---------------------------------------------------------------------------
_CSS_INSTALLED = False

_BACKGROUND_BY_CLASS = {
    "telemetry-panel": _DARK_BASE,
    "pipeline-panel": _DARK_BASE,
}

# A very low-opacity hairline, used to separate cards/rows on the dark
# ground without falling back to the "chunky 2px border, big radius" card
# look this redesign moves away from. Literal rgba() (not GTK's `alpha()`
# CSS function) so the exact value is visible here rather than computed by
# the CSS engine at load time.
_HAIRLINE = "rgba(199, 217, 216, 0.18)"  # _BG_ALT at 18% opacity
_TROUGH_TRACK = "rgba(199, 217, 216, 0.12)"  # _BG_ALT at 12% opacity

_PANEL_CSS = f"""
.telemetry-panel {{
    background-color: {_BACKGROUND_BY_CLASS["telemetry-panel"]};
    padding: 12px 16px;
    border-radius: 6px;
}}
.telemetry-status {{
    font-weight: 600;
    color: {_BG_ALT};
}}
.telemetry-status.telemetry-unknown {{
    color: {_RED};
}}
.telemetry-status.telemetry-empty {{
    color: {_BG_ALT};
}}
.telemetry-status.telemetry-ok {{
    color: {_BG};
}}
.telemetry-status.telemetry-stale {{
    color: {_YELLOW};
    font-style: italic;
}}
.telemetry-card-cell {{
    padding: 0 14px;
}}
.telemetry-card-cell-divider {{
    border-left: 1px solid {_HAIRLINE};
}}
.telemetry-field-label {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: {_BG_ALT};
}}
.telemetry-hero-number {{
    font-family: "Berkeley Mono", monospace;
    font-size: 26px;
    font-weight: 500;
    color: {_BG};
}}
.telemetry-hero-number.telemetry-hero-hot {{
    color: {_ORANGE};
}}
.telemetry-field-value {{
    font-family: "Berkeley Mono", monospace;
    font-size: 12px;
    color: {_BG_ALT};
}}
.pipeline-panel {{
    background-color: {_BACKGROUND_BY_CLASS["pipeline-panel"]};
    padding: 12px 16px;
    border-radius: 6px;
}}
.pipeline-row {{
    padding-bottom: 6px;
    margin-bottom: 6px;
}}
.pipeline-row-divider {{
    border-bottom: 1px solid {_HAIRLINE};
}}
.pipeline-stage-name {{
    min-width: 96px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
}}
/* Label text colour per row state -- weight carries as much of the
   distinction as hue here (per this redesign's "spend colour sparingly"
   direction): only the ACTIVE row's text gets the accent tint. */
.stage-done {{
    color: {_BG};
}}
.stage-active {{
    color: {_ACCENT_TEXT};
    font-weight: 800;
}}
.stage-pending {{
    color: {_BG_ALT};
}}
/* Gtk.ProgressBar's FILL lives on a nested `progress` node inside `trough`
   (see GTK4's widget node tree for progressbar) -- a plain `color:` rule on
   the progressbar widget itself only affects text, which Gtk.ProgressBar
   doesn't draw any of. These descendant-selector rules are what actually
   paint the bar. Decorative, not text -- exempt from this module's own
   >=4.5:1 text-legibility rule (see tests/unit/test_panels.py), so the
   ACTIVE fill uses the pure, saturated brand accent (`_ACCENT`, not the
   lightened `_ACCENT_TEXT` reserved for text) -- the one genuinely bright
   thing in either panel, on purpose.

   Design tweak (booth legibility pass): DONE's fill used to be `_BG_ALT`
   (near-white, `#C7D9D8`) -- at full width, that made every finished stage
   the single brightest, highest-contrast element in the panel, out-pulling
   the ACTIVE row it was supposed to defer to. DONE now fills with the
   muted brand `_GREEN` (`#6FABA0`) instead -- a completed stage reads as
   settled, not as the loudest thing on screen -- while `_ACCENT` stays the
   ONE hue reserved exclusively for the row actually moving. ACTIVE also
   gets a taller trough AND a taller fill (10px vs the 6px DONE/PENDING
   share) so the moving row carries a little extra weight that isn't
   hue-dependent -- the same "don't rely on colour alone" principle this
   module's own tri-state already leans on via label text and font-weight,
   extended to the bar itself for a colour-blind viewer.

   Second pass, found only by profiling the RENDERED PIXELS vertically
   (not by sampling one centre pixel, and not by reading this CSS): the
   desktop GTK theme draws its OWN 1px border on the `progress` node
   independently of anything set here, and a bare `min-height` on `trough`
   does NOT propagate to the `progress` node nested inside it -- so the
   fill itself stayed the theme's ~2px default regardless of trough
   height, wrapped in a theme-coloured (not brand-palette) border on each
   side. At a 2px fill, that border was HALF the bar's visual mass, so
   every row still read as the theme's blue outline colour, not `_GREEN`
   or `_ACCENT` -- the DONE/ACTIVE distinction this section exists for was
   invisible in practice even though the CSS "worked" and a single pixel
   sample at bar centre "confirmed" the right hex. Fixed by (1) setting an
   explicit `min-height` on the `progress` node itself, not just `trough`,
   so the fill has real substance instead of inheriting whatever the theme
   defaults to, and (2) `border: none; box-shadow: none;` on BOTH `trough`
   and `progress` -- GTK themes draw outlines with either property, so
   both are killed -- leaving zero theme-owned pixels anywhere in the bar's
   vertical extent. Verified after the fix with a full vertical pixel
   profile through the centre of every row, not a single sample (see this
   task's report). */
.pipeline-progress trough {{
    min-height: 6px;
    border-radius: 3px;
    border: none;
    box-shadow: none;
    padding: 0;
    background-color: {_TROUGH_TRACK};
}}
.stage-active.pipeline-progress trough {{
    min-height: 10px;
    border-radius: 5px;
}}
.pipeline-progress trough progress {{
    border: none;
    box-shadow: none;
    margin: 0;
    border-radius: 3px;
}}
.stage-done.pipeline-progress trough progress {{
    min-height: 6px;
    background-color: {_GREEN};
}}
.stage-active.pipeline-progress trough progress {{
    min-height: 10px;
    border-radius: 5px;
    background-color: {_ACCENT};
}}
.stage-pending.pipeline-progress trough progress {{
    min-height: 6px;
    background-color: {_TROUGH_TRACK};
}}
"""


def _ensure_css_installed():
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        log.debug("no default display; skipping panel CSS install")
        return
    provider = Gtk.CssProvider()
    # load_from_string (not the bytes-taking load_from_data, deprecated in
    # GTK 4.12+ and noisy on every test run): _PANEL_CSS is already a plain
    # str.
    provider.load_from_string(_PANEL_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


# One CSS class per possible telemetry-status/stage state -- listed once
# here so both widgets can reset ("remove every state class") without
# re-deriving the class name from the state string, and without a stale
# class from a previous render leaking into the next one (e.g. a row that
# was active on the last render reads pending now, but GTK doesn't remove
# CSS classes on its own just because a caller stopped adding them).
# `telemetry-ok` is listed even though nothing currently distinguishes it
# from the base style beyond brightness -- kept as an explicit state name
# (not folded away) so a future addition has a slot to hook without
# reshaping this tuple.
_TELEMETRY_STATUS_CLASSES = (
    "telemetry-unknown", "telemetry-empty", "telemetry-ok", "telemetry-stale",
)
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

# The same idea for the PIPELINE panel, which had none: how long it will keep
# showing a fold's progress after the last `stage` event before falling back
# to "nothing is running".
#
# Without this the panel was reset only on `job_start` (ui/app.py), so a
# daemon that died mid-fold left `DIFFUSION 62%` on screen for the rest of
# the conference day -- a progress bar that lies, which is the one thing this
# booth's brief rules out. TelemetryPanel has `STALE_AFTER_S` above and
# ChipVizPanel has `STAGE_STALE_AFTER_S`; this is the third instrument
# getting the same treatment.
#
# 20s, between two real pressures rather than by analogy:
#   - too long and a dead daemon's last progress reading stands for that long;
#   - too short and it flickers to empty DURING a legitimate fold. The
#     shipped 62-75s targets have genuinely callback-free windows -- host
#     featurization between `prep` and the first trunk progress event, and
#     the confidence head plus mmCIF write at the end (runner/folder.py) --
#     and an empty bar mid-fold is its own lie ("nothing is running" while
#     something is).
# 20s clears the 4.4s Trp-cage fold's entire event-free span several times
# over, and is a couple of seconds longer than ChipVizPanel's 15s for the
# same reason: this panel is the one that would flicker.
PIPELINE_STALE_AFTER_S = 20.0


# ── the telemetry panel's RESERVED FOOTPRINT ────────────────────────────────
#
# This panel is the reason the side rail used to visibly jerk, and these
# three constants are the fix. Measured, not guessed -- see this task's
# report for the harness and the before/after numbers.
#
# The defect: `update()` rebuilds the chip cells from scratch on every
# sampler tick, and until this fix NOTHING pinned how much room the result
# took. A GTK box's minimum size is its content's minimum size, so the
# panel's footprint tracked the TEXT INSIDE IT, and the rail -- whose
# `set_size_request(_SIDE_RAIL_WIDTH_PX)` is a FLOOR, not a ceiling -- was
# dragged along with it. Measured on this booth's four chips:
#
#   no telemetry yet          panel 312 x 51   -> rail allocated 430
#   4 chips at 48.0°C         panel 531 x 116  -> rail allocated 531
#   4 chips at 100.0°C        panel 595 x 116  -> rail allocated 595
#
# So the whole column -- and with it the left edge of the protein, which is
# the hero -- moved 101px sideways the moment the first `tt-smi` sample
# landed, moved back if the sampler ever went unreadable, and would have
# moved another 64px if a chip crossed 100°C. That is the jerk.
#
# The fix is to RESERVE the space instead of negotiating it:
#
#   1. `_cards_box` gets a fixed size request, so the panel is exactly as
#      tall with zero chips as with four (65px of deliberately empty space
#      in the "no telemetry" state, rather than 65px of everything below it
#      jumping up);
#   2. each cell is exactly `CHIP_CELL_WIDTH_PX` wide, whatever is in it;
#   3. every label inside a cell ellipsizes, which is what makes (2) a
#      GUARANTEE rather than a hope -- an ellipsizing label's minimum width
#      is the ellipsis, so no string, however long, can push a cell (and
#      therefore the rail) wider.
#
# `CHIP_CELL_WIDTH_PX` is then chosen so that (3) never actually fires for
# anything this panel can render -- truncating a chip's temperature would be
# a worse bug than the one being fixed. The widest strings the formats in
# `_build_card` can produce, measured at the shipped font sizes:
#
#   title      "CHIP 3 . N300-R2"    96px   (10px letterspaced)
#   hero       "99.9°C"              96px   (26px mono; see `hero_text` for
#                                            why 100°C and up is NARROWER)
#   secondary  "999W . 1350MHz"      98px   (12px mono)
#
# 98px of content plus the cell's own 2x14px padding is 126px; 130 leaves a
# little headroom for a font that measures slightly differently on another
# machine, and `test_panels.py` re-measures all three strings against this
# constant so a copy or format change that would truncate fails the suite
# instead of the booth.
CHIP_CELL_WIDTH_PX = 130

# One cell's height: title (14) + hero (32) + secondary (15) + 2x2px spacing.
# Reserved so the tri-state (`None` / `[]` / readings) cannot change the
# panel's height and shove the panels below it up and down.
CHIP_CELL_HEIGHT_PX = 65

# How many cells are drawn, at most. Same number and same reason as
# `ui.chipviz.MAX_CHIPS` (which cannot be imported here -- that module pulls
# in WebKit): four is what this booth's QB2 presents, and it is also the
# point past which the reservation above stops being able to hold the rail
# still. A machine with more chips gets the first four drawn and a heading
# that says so (`chip_count_text`), rather than a rail that silently grows
# a cell at a time. `test_panels.py` pins the two constants together.
MAX_CHIP_CELLS = 4


class TelemetryPanel(Gtk.Box):
    """Renders `ui.telemetry.TelemetrySampler`'s tri-state `latest()` /
    `age_s()` as one CHIP cell per device, via `.update(readings, age_s)`.

    The tri-state (see ui/telemetry.py's module docstring) is rendered as
    three states distinguishable by TEXT CONTENT (never colour alone) --
    a visitor, not just an operator reading logs, can tell apart on sight,
    per this task's controller ruling ("no telemetry is not fake
    telemetry"):

    - `readings is None`: `tt-smi` has never produced a usable answer (or
      every device in its very first snapshot was unreadable). No chips are
      drawn at all; the status line reads as a clear "no telemetry" state,
      never as a chip showing 0C/0W, which would look like real hardware
      idling rather than a sampler with nothing to report.
    - `readings == []`: `tt-smi` answered and truthfully reported zero
      devices. Also no chips drawn, but a visually distinct, calmer status
      line -- this is real information ("no chips detected"), not a
      failure.
    - `readings` non-empty: one CHIP cell per reading, headed by the chip
      count and the number of boards those chips sit on (see
      `chip_count_text`). Temperature is the hero number (largest,
      brightest); it turns the alarm colour only if the chip is at or over
      `card_color`'s quarantine threshold -- the one place either panel
      spends saturated colour on a "normal" chip.

    Independently of which of the three states above applies, `age_s`
    (seconds since the last successful sample, or `None` if there has never
    been one) drives a staleness note once it passes `STALE_AFTER_S`: the
    thing this adds is telling "the chips are genuinely idle" (a fresh `[]`
    or a fresh non-empty reading) apart from "we have not heard from tt-smi
    in a while" (an OLD non-empty or `[]` reading whose sampler may have
    wedged) -- see the module-level `STALE_AFTER_S` docstring for the
    threshold's rationale.
    """

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _ensure_css_installed()
        self.add_css_class("telemetry-panel")

        self._status_label = Gtk.Label(xalign=0.0)
        self._status_label.add_css_class("telemetry-status")
        # Ellipsizing is what stops the LONGEST of the four status strings
        # (the stale note, "No Tenstorrent chips detected - stale, last
        # heard 1204s ago", 397px) from setting the rail's width. It has
        # room for all of them at the shipped rail width and so never
        # actually fires; it is here as the structural guarantee, the same
        # one the cell labels carry -- see the reserved-footprint block.
        self._status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.append(self._status_label)

        self._cards_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        # THE fix for the rail's jerk: reserve the cells' space once, here,
        # instead of letting every `update()` renegotiate it. See the
        # reserved-footprint block above these constants for the measured
        # before/after. `halign=START` so cells stay packed to the left in
        # the reserved band rather than being spread across it when there
        # are fewer than `MAX_CHIP_CELLS` of them.
        self._cards_box.set_halign(Gtk.Align.START)
        self._cards_box.set_size_request(
            MAX_CHIP_CELLS * CHIP_CELL_WIDTH_PX, CHIP_CELL_HEIGHT_PX)
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
                self._with_staleness_note("No Tenstorrent chips detected", age_s, stale))
            self._status_label.add_css_class("telemetry-empty")
            if stale:
                self._status_label.add_css_class("telemetry-stale")
            return

        # Capped, and the heading says when it is: past `MAX_CHIP_CELLS` the
        # reserved band cannot hold the cells, and a rail that grows a
        # column at a time is the defect this whole reservation exists to
        # remove. Same cap, same disclosure, as ui/chipviz.py's `MAX_CHIPS`.
        drawn = readings[:MAX_CHIP_CELLS]
        self.last_status = "stale-ok" if stale else "ok"
        self._status_label.set_label(
            self._with_staleness_note(
                chip_count_text(readings, shown=len(drawn)), age_s, stale))
        self._status_label.add_css_class("telemetry-ok")
        if stale:
            self._status_label.add_css_class("telemetry-stale")
        for index, reading in enumerate(drawn):
            self._cards_box.append(self._build_card(reading, first=(index == 0)))

    @staticmethod
    def _with_staleness_note(text, age_s, stale):
        if not stale:
            return text
        return f"{text} — stale, last heard {age_s:.0f}s ago"

    def _clear_cards(self):
        """Remove every previously-rendered chip cell. Load-bearing, not
        cosmetic: without this, a 2s sampler tick would grow a fresh row of
        cells every poll for as long as the booth is open, and a
        `[readings] -> []` transition would leave the LAST good cells
        sitting on screen underneath a "no chips detected" banner --
        exactly the kind of stale-looking display Task 4/5's tri-state work
        exists to prevent. See tests/unit/test_panels.py's dedicated tests
        for both of those, verified against a `pass`-body mutation of this
        method."""
        child = self._cards_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._cards_box.remove(child)
            child = following

    def _build_card(self, reading, *, first):
        cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        cell.add_css_class("telemetry-card-cell")
        # Exactly one cell width, whatever is in it. Together with the
        # ellipsize calls below, this is what makes a chip going from 48.0
        # to 100.0 degrees -- or a longer board type, or a three-digit watt
        # figure -- cost nothing but the characters on screen. See the
        # reserved-footprint block.
        cell.set_size_request(CHIP_CELL_WIDTH_PX, CHIP_CELL_HEIGHT_PX)
        if not first:
            cell.add_css_class("telemetry-card-cell-divider")
        color = card_color(reading.temperature_c)
        is_hot = color == _CARD_HOT_COLOR
        cell.add_css_class("telemetry-card-hot" if is_hot else "telemetry-card-normal")

        # "CHIP n", not "CARD n": one tt-smi device entry is one chip, and a
        # p300c board carries two of them. See `chip_count_text` for the
        # whole argument and for why the board grouping lives in the heading
        # rather than being repeated on every cell.
        title = Gtk.Label(
            xalign=0.0, label=f"CHIP {reading.index} · {reading.board_type}".upper())
        title.add_css_class("telemetry-field-label")

        hero = Gtk.Label(xalign=0.0, label=hero_text(reading.temperature_c))
        hero.add_css_class("telemetry-hero-number")
        if is_hot:
            hero.add_css_class("telemetry-hero-hot")

        secondary = Gtk.Label(
            xalign=0.0, label=f"{reading.power_w:.0f}W · {reading.aiclk_mhz:.0f}MHz")
        secondary.add_css_class("telemetry-field-value")

        for widget in (title, hero, secondary):
            # The guarantee behind the cell's fixed width: an ellipsizing
            # label's MINIMUM width is the ellipsis, so no string this panel
            # can be handed -- however long a future board type or a
            # four-digit wattage -- can push the cell, the panel or the rail
            # wider. `CHIP_CELL_WIDTH_PX` is sized so that never fires for
            # anything the formats above can actually produce, and
            # test_panels.py re-measures the widest of them to keep it that
            # way.
            widget.set_ellipsize(Pango.EllipsizeMode.END)
            cell.append(widget)
        return cell


class PipelinePanel(Gtk.Box):
    """One row per protocol stage, driven by `.set_stage(stage, frac)` (or,
    for the daemon-driven wiring layer, `.set_stage_from_wire(stage,
    wire_frac)` -- see that method's docstring for why it, not `set_stage`,
    is the intended entry point once a real daemon is driving this panel).

    Thin assembly over `stage_rows`: this class owns no progress logic of
    its own, only the mapping from each `(name, fraction, state)` row to a
    label + progress bar and a CSS class for `state`. Colour is spent
    sparingly (per this redesign's direction): only the ACTIVE row's label
    and fill get the brand accent (`_ACCENT`), reserved exclusively for it;
    DONE reads settled in the muted brand green (`_GREEN`) rather than a
    bright neutral, and PENDING stays the dim, near-invisible track it
    always was. All three are also distinguishable without hue at all --
    fill extent (full / partial / empty) and weight (ACTIVE's trough is
    taller, its label bolder) -- so the row that is actually moving is the
    one the eye lands on, from across a booth, even for a colour-blind
    viewer.
    """

    def __init__(self, clock=time.monotonic):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        _ensure_css_installed()
        self.add_css_class("pipeline-panel")

        # Injectable for the same reason ui/chipviz.py's and ui/app.py's
        # are: a test that asserts on staleness should not have to sleep
        # through PIPELINE_STALE_AFTER_S.
        self._clock = clock
        # When the last real progress update arrived, and whether this panel
        # has since given up on it. `stale` is public: it is what a test (and
        # a future "the booth looks stuck" diagnostic) reads.
        self._last_update_at = None
        self.stale = False

        self._rows = {}
        stages = list(STAGE_ORDER)
        for index, stage in enumerate(stages):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.add_css_class("pipeline-row")
            if index < len(stages) - 1:
                row.add_css_class("pipeline-row-divider")
            label = Gtk.Label(label=stage.upper(), xalign=0.0)
            label.add_css_class("pipeline-stage-name")
            bar = Gtk.ProgressBar()
            bar.add_css_class("pipeline-progress")
            bar.set_hexpand(True)
            bar.set_valign(Gtk.Align.CENTER)
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
        already be the WITHIN-STAGE fraction (see the class docstring) --
        the daemon-driven wiring layer should call `set_stage_from_wire`
        instead, which takes the wire's raw fraction and converts it
        internally.

        Also the panel's "I have heard from the fold" stamp: anything
        arriving here resets the staleness countdown (see `tick`).
        """
        try:
            self._last_update_at = self._clock()
        except Exception:
            # A clock that raises must not cost the booth a progress
            # update; the worst case is that this reading never goes stale.
            log.exception("clock failed while stamping pipeline progress")
        self.stale = False
        self._render(stage, frac)

    def tick(self):
        """Give up on a fold nothing has reported on for a while.

        Called from the booth's own 100 ms state tick (ui/app.py). Renders
        the same "nothing is running" state `reset()` does -- every stage
        pending, every bar empty -- which is the honest reading once the
        daemon has gone quiet: it is what the panel shows before the first
        fold of the session, too.

        Deliberately does NOT re-stamp `_last_update_at`: `stale` latches
        until a real update arrives, so this repaints once rather than
        rebuilding six rows ten times a second for the rest of the day.
        """
        if self.stale or self._last_update_at is None:
            return False
        try:
            idle_s = self._clock() - self._last_update_at
        except Exception:
            log.exception("clock failed while checking pipeline staleness")
            return False
        if idle_s < PIPELINE_STALE_AFTER_S:
            return False
        log.info("no fold progress for %.0fs; clearing the pipeline panel "
                 "rather than leaving its last reading on screen", idle_s)
        self.stale = True
        self._render(None, 0.0)
        return True

    def _render(self, stage, frac):
        """Draw `(stage, frac)`, with no opinion about staleness. Split out
        of `set_stage` so `tick` can clear the panel without stamping it as
        freshly updated."""
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

    def set_stage_from_wire(self, stage, wire_frac):
        """Render one `stage` event straight off the wire.

        Ruling 2's letter ("stage_rows takes a within-stage fraction") is
        satisfied by `set_stage` alone, but nothing about that signature
        stops a caller from passing the raw wire fraction anyway --
        `stage_rows` clamps to [0, 1], so a raw `0.55` mid-diffusion still
        renders a plausible-looking (wrong) 55% bar with no error anywhere.
        This method is what makes that mistake structurally unreachable for
        the wiring layer: it calls `protocol.events.within_stage_frac`
        itself and only ever hands `set_stage` an already-converted value,
        so the daemon-driven caller (Task 9) never touches the raw-frac
        path at all. Call THIS from the wiring layer; call `set_stage`
        directly only when you already have a within-stage fraction in
        hand (tests, `reset()`, manual/demo control).
        """
        self.set_stage(stage, within_stage_frac(stage, wire_frac))

    def reset(self):
        """Back to "nothing has happened yet": every stage pending, every
        bar empty. Deliberately reuses `stage_rows`' own unrecognized-stage
        fallback (see its docstring) rather than a second, separately
        maintained "all pending" code path -- one behavior, tested once."""
        self.set_stage(None, 0.0)
