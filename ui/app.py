"""GTK application shell for the tt-bio demo -- the booth, assembled.

This module is the one place that knows about all of the others: the
viewer, the two panels, the gallery, the playlist, the telemetry sampler,
the socket client and the state machine. Its job is deliberately NARROW
and gets narrower as it gets longer -- it carries decisions out, it does
not make them:

- what the booth's state IS, and what each state means for the screen,
  belongs to `ui.states` (including the three named predicates this module
  reads: `points_are_visible`, `ribbon_may_be_revealed`, `showcase_ended`);
- what a panel looks like belongs to `ui.panels` (including the wire->
  within-stage fraction conversion, which is why this module calls
  `set_stage_from_wire` and never `set_stage`);
- how telemetry is sampled belongs to `ui.telemetry`;
- how a grid of targets is laid out belongs to `ui.gallery`;
- what a line of the diagnostics log SAYS belongs to `ui.diagnostics`
  (this file only decides when to feed it and when to repaint it).

If a booth decision ever appears in this file as an `if` on raw state, that
is the signal it belongs in one of those modules instead.

The two interactive surfaces (Task 10)
---------------------------------------
The diagnostics panel and the `?` help card are CHROME, not booth state:
they are laid over whatever `ui.states` is doing, and neither one touches
it. That is deliberate and it is what lets `?` work "at any time" -- from
attract, gallery, folding, showcase or preparing -- without the state
machine growing a sixth state and every transition in it growing an
opinion about overlays. What they do borrow from the state machine is its
principle that a visitor who walks away must not leave the booth changed:
`_tick_overlays` closes both of them after a period with no input at all
(`_HELP_IDLE_S`, `_DIAGNOSTICS_IDLE_S`).

The sequencing this file exists to get right
---------------------------------------------
Measured on hardware and independently reproduced: fold N's finished ribbon
lands AFTER fold N+1's `job_start`. The daemon does not wait for the UI --
it starts the next fold the moment the last one finishes -- so the arrival
order at this file's door is:

    job_done(N) ... job_start(N+1) ... frame(N+1) ... ribbon(N) ... frame(N+1) ...

The pre-Task-9 wiring reacted to each of those literally: `job_start(N+1)`
cleared the screen, then fold N's ribbon landed and cross-faded itself in
over fold N+1's opening noise cloud (three orders of magnitude wider than
the structure, so beyond the camera's far plane -- the ribbon faded in over
nothing), and the blend then STAYED at 1 until the fold after that, drawing
every remaining frame of fold N+1 at opacity 0. Only ~27% of each fold's
collapse ever reached a visitor's eye, and the finished structure held the
screen properly for ~21% of the cycle. For a demo whose entire premise is
"watch it fold", that was the headline defect.

The fix is the state machine's `showcase` dwell, carried out here in four
places, none of which decide anything on their own:

1. `job_start` during a showcase does NOT clear the screen; the clear is
   deferred (`_deferred_clear`) until the dwell expires. Fold N's finished
   structure keeps the screen it earned.
2. Point frames arriving during a showcase are SUPPRESSED, not drawn --
   and not discarded either: they stay in the one-slot latest-wins buffer
   (`ui.client.LatestFrame`), which is what lets step 3 cut straight to
   live diffusion with no blank gap. Suppressing them is also what keeps
   the cross-fade honest: the points still on screen underneath the
   arriving ribbon are fold N's OWN final cloud, so the structure
   condenses out of the cloud it actually came from.
3. When the dwell expires, `showcase_ended` fires once: apply the deferred
   clear, then immediately drain the buffered frame so the next fold's
   diffusion appears in the same instant the structure leaves.
4. A ribbon that arrives after its own dwell has expired is dropped
   (`ribbon_may_be_revealed`): by then the booth is showing the next fold's
   live diffusion, and cross-fading the previous structure over it is the
   same defect by another route.

`_SHOWCASE_DWELL_S` below carries the measurement that sets the trade.
"""

import argparse
import logging
import pathlib
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk

from protocol.events import unpack_coords
from ui.chipviz import ChipVizPanel
from ui.client import EventClient, LatestFrame
from ui.diagnostics import KIND_MARK, DiagnosticsLog, DiagnosticsPanel
from ui.gallery import Gallery
from ui.geometry import PLDDT_STOPS, ribbon_from_cif
from ui.panels import PipelinePanel, TelemetryPanel
from ui.playlist import PlaylistError, load_playlist, select_targets
from ui.states import (
    StateMachine, points_are_visible, ribbon_may_be_revealed, showcase_ended,
)
from ui.telemetry import TelemetrySampler
from ui.viewer import StructureViewer

log = logging.getLogger(__name__)

# Operator-neutral copy for the "preparing" overlay. The `missing` list from
# a not_ready event names real filesystem paths and model/config detail --
# useful to an operator reading the log, meaningless (and a mild information
# leak) to a visitor reading the screen. This string is the only thing that
# may ever reach display_message for that state; it never gets composed from
# `missing` in any way.
_PREPARING_MESSAGE = "Getting the booth ready. Please check back shortly."

# ── booth timing ────────────────────────────────────────────────────────────
#
# How long a finished structure holds the screen. `ui.states` defaults this
# to 3.0s; the booth passes its own value, and the difference is not a
# style preference -- it is the one number that sets how the cycle is split,
# so it is set here against a measurement rather than inherited.
#
# The arithmetic that produced the 3.0s default assumed a SERIAL cycle: one
# 4.4s fold, then a 3.0s look at the result, so 3.0/(4.4+3.0) = 41% of the
# loop showing the finished structure. The daemon does not work that way. It
# starts fold N+1 the instant fold N finishes and never pauses, so the cycle
# stays 4.4s and the dwell does not extend it -- it DISPLACES live diffusion,
# second for second. Measured against the recorded real fold replayed
# back-to-back (tests/fixtures/streams/real_fold_trpcage.jsonl, a 3.69s
# cycle carrying 30 diffusion frames), the split per cycle is:
#
#     dwell    frames of the collapse seen     cycle spent on the structure
#     3.0s     4 / 30   (13%)                  81%
#     2.0s     14 / 30  (47%)                  54%
#     1.5s     17 / 30  (57%)                  41%
#
# 3.0s would leave LESS of the collapse visible than the defect this task
# exists to fix (~27%), which is the wrong direction for a booth whose whole
# premise is watching it fold. 2.0s is the balance point: both halves of the
# cycle are substantial, and 2.0s is a hard floor of FULLY VISIBLE time --
# it is measured from the reveal, not from `job_done` (see
# `StateMachine.on_structure_revealed`), so unlike the ~1.2s the old
# sequencing left on screen it is not eaten by the ribbon build or the 0.8s
# cross-fade.
_SHOWCASE_DWELL_S = 2.0

# How often the booth hands the state machine a clock reading. The machine
# owns no timer of its own by design (ui/states.py), so this source is the
# only thing that can end a showcase dwell or fire the 45s idle timeout --
# at 100ms it costs nothing and quantizes both by at most a tenth of a
# second.
_STATE_TICK_MS = 100

# Frame drain cadence: ~30Hz, matching the viewer's own render rate. The
# buffer is latest-wins, so a slower drain loses frames rather than falling
# behind.
_FRAME_DRAIN_MS = 33

# Telemetry repaint cadence. The sampler polls tt-smi every 2s on its own
# thread; repainting at 500ms means a new reading reaches the screen well
# within one sample period without the panel rebuilding its chip cells 30x a
# second for data that changes twice a second.
_TELEMETRY_REPAINT_MS = 500

# ── layout ──────────────────────────────────────────────────────────────────
#
# The side rail is a FIXED column, never a greedy one. Without an explicit
# width and hexpand(False), the panels inside it negotiate their way to two
# thirds of the window and the protein -- the reason anyone stopped to look
# -- ends up squeezed into a corner. Verified on real glass with the
# throwaway composed-screen harness that preceded this file (see
# .superpowers/sdd/2026-08-12-ui-panels/booth-composed.png).
_SIDE_RAIL_WIDTH_PX = 430

# What the gallery gets to lay its cards out in: the window minus the rail.
# 1920 is this booth's screen; `ui.gallery.grid_shape` turns it into a
# column count, so a different screen simply gets a different one.
_GALLERY_WIDTH_PX = 1920 - _SIDE_RAIL_WIDTH_PX

# tt-bio's ASCII logo, verbatim from the upstream README. Rendered as TEXT
# in a monospace face rather than shipped as a bitmap: crisp at any size,
# and it is the project's own identity mark rather than an approximation of
# it. Pinned bottom-right of the whole screen (not inside the rail) because
# it is an identity mark, not part of the readout -- it should sit still
# while everything else changes.
TT_BIO_LOGO = (
    "████████╗████████╗        ██████╗  ██╗  ██████╗\n"
    "╚══██╔══╝╚══██╔══╝        ██╔══██╗ ██║ ██╔═══██╗\n"
    "   ██║      ██║    █████╗ ██████╔╝ ██║ ██║   ██║\n"
    "   ██║      ██║    ╚════╝ ██╔══██╗ ██║ ██║   ██║\n"
    "   ██║      ██║           ██████╔╝ ██║ ╚██████╔╝\n"
    "   ╚═╝      ╚═╝           ╚═════╝  ╚═╝  ╚═════╝"
)

# Where the playlist lives when nothing is passed on the command line:
# alongside this checkout, resolved off THIS FILE, never off the process's
# working directory (ui/playlist.py's own module docstring explains why that
# distinction has already cost this project once).
_DEFAULT_PLAYLIST = pathlib.Path(__file__).resolve().parent.parent / "playlist" / "manifest.yaml"


def _format_missing(missing):
    """Render a not_ready event's `missing` value for the log without ever
    raising.

    `missing` is trusted to be a list of strings, and normally is one --
    but this is wire data from the daemon, and the whole point of logging
    it is to help an operator diagnose a *different* problem. If some
    future daemon change (or a wire bug) ever sends something else --  a
    single string, a number, `None` -- `"; ".join(missing)` would raise
    inside this log call itself. That exception would be caught by
    _handle_event's outer broad `except Exception`, but at the cost of
    replacing this specific, useful detail with the generic "dropping
    malformed not_ready event", which is exactly the outcome an operator
    at 11pm can least afford: the one signal that could tell them what's
    actually wrong, swallowed by the same guard that's supposed to keep
    the app alive. repr() never raises on anything, so this always
    produces *something* diagnosable instead.
    """
    if isinstance(missing, list):
        return "; ".join(str(item) for item in missing)
    return repr(missing)


# ── visitor input ───────────────────────────────────────────────────────────
#
# Key names as `Gdk.keyval_name` reports them, lowercased. `?` is `question`
# on every layout that has it as a shifted character; `F1` and the dedicated
# `Help` key some keyboards carry are accepted as the same request, because a
# booth operator reaching for help should not have to know which one this
# build wanted.
_HELP_KEYS = frozenset({"question", "f1", "help"})
_DIAGNOSTICS_KEYS = frozenset({"d"})

# How long an overlay a visitor left open survives their walking away.
#
# The state machine's own 45s idle timeout only covers the states it owns
# (gallery, folding); this chrome sits outside those, so it needs its own
# rule or a visitor who opens the help card and leaves would hand the next
# person a booth with a wall of text over the protein. The help card is
# transient by nature and goes first (60s -- comfortably longer than it takes
# to read, short enough that the protein is back before the next visitor
# arrives). The diagnostics panel is deliberately much more patient (5
# minutes): it is also the panel WE want up while standing at the booth
# talking to someone, and having it vanish mid-sentence would be worse than
# useless. Both are measured from the last visitor input of any kind, so
# neither can close while someone is actually pressing things.
_HELP_IDLE_S = 60.0
_DIAGNOSTICS_IDLE_S = 300.0

# ── logging a failure that repeats ──────────────────────────────────────────
#
# `_drain_frames` runs every 33ms and `_handle_event` fires per event, so a
# systematically malformed stream (a daemon sending a bad coords_b64, a
# protocol change this build cannot parse) turns a single `log.exception`
# into ~30 tracebacks a second for as long as the booth is up -- which is
# both an unbounded log and, worse, a wall of noise that buries the FIRST
# occurrence, the only one an operator actually needs to read.
#
# ui/chipviz.py already made this trade for its own 1Hz JS failures by
# dropping straight to `log.debug`. This does the same thing without losing
# the signal: the first failure of each kind is logged in full, at error
# level, with its traceback; every `_DROP_LOG_EVERY`-th one after that logs
# a one-line count so a persistent problem stays visible; everything in
# between goes to debug.
_DROP_LOG_EVERY = 100

# ── palette ─────────────────────────────────────────────────────────────────
#
# The same brand constants ui/panels.py names, repeated here rather than
# imported so this file's stylesheet stays readable as a stylesheet -- but
# with the same values and the same rule: `_ACCENT` (#1B8EB1) measures
# 4.40:1 on the dark ground and is therefore a FILL colour only; anywhere
# accent-coloured TEXT is wanted, `_ACCENT_TEXT` (#3299B9, 5.06:1) is what
# clears the 4.5:1 AA floor this project holds every label to.
_DARK_BASE = "#092221"
_BG = "#F1F8F8"
_BG_ALT = "#C7D9D8"
_ACCENT_TEXT = "#3299B9"
_TEAL = "#74C5DF"

# The single source of truth for "which CSS class in _APP_CSS carries an
# explicitly-set background", read by tests/unit/_legibility.py's shared
# walker exactly as ui/panels.py's and ui/gallery.py's equivalents are. Every
# entry maps to the same dark ground: the preparing and help overlays are
# near-opaque washes OF that ground rather than a second surface colour, so
# the contrast a label really has is the contrast against `_DARK_BASE`.
#
# `.booth-root` is on the root overlay as well as the root box, so that the
# logo (and the help card) -- which are overlay children, i.e. siblings of
# the root box rather than its descendants -- still have a
# background-painting ANCESTOR and can therefore be contrast-checked at all.
_BACKGROUND_BY_CLASS = {
    "booth-root": _DARK_BASE,
    "booth-side": _DARK_BASE,
    "preparing-overlay": _DARK_BASE,
    "help-overlay": _DARK_BASE,
}

# The pLDDT legend's swatches, generated from the ONE ramp the ribbon itself
# is coloured by (ui/geometry.py's `PLDDT_STOPS`) rather than a second list
# of hexes hand-copied into this file. A legend that has drifted from the
# thing it describes is worse than no legend, and hand-copying is how that
# drift happens; tests/unit/test_app_interaction.py pins the two together.
#
# Note these are FILLS, never text: `#0053D6` measures 2.54:1 against the
# dark ground and could not legally be a label colour here. The label next
# to each swatch is ordinary body text on the overlay's own ground.
_PLDDT_LEGEND = (
    ("plddt-very-high", "above 90", "very high — trust the detail"),
    ("plddt-confident", "70 to 90", "confident backbone"),
    ("plddt-low", "50 to 70", "low — treat with care"),
    ("plddt-very-low", "below 50", "very low — likely floppy or disordered"),
)


def _plddt_swatch_css():
    """One `.plddt-*` background rule per ramp stop, in ramp order.

    `PLDDT_STOPS` is ordered high threshold first, which is the order
    `_PLDDT_LEGEND` above reads in, so they zip directly. Built as CSS text
    (not set per-widget) so the swatches live in the same stylesheet as
    everything else and the legibility guard sees them for what they are:
    background-painting classes that must never carry a label.
    """
    rules = []
    for (css_class, _range_text, _meaning), (_threshold, rgb) in zip(
            _PLDDT_LEGEND, PLDDT_STOPS):
        hex_color = "#%02X%02X%02X" % tuple(rgb)
        rules.append(f".{css_class} {{ background-color: {hex_color}; }}")
    return "\n".join(rules)


# CSS for the booth chrome, in the brand palette from the docs-site theme:
# dark base for every ground, accent/teal for text. Kept as a module-level
# constant (not rebuilt per window) since it never varies. The panels and
# the gallery install their own stylesheets the same way (ui/panels.py,
# ui/gallery.py) -- this one covers only what this file itself builds.
_APP_CSS = f"""
.preparing-overlay {{
    background-color: rgba(9, 34, 33, 0.94); /* #092221, near-opaque */
}}
.preparing-title {{
    color: {_TEAL};
    font-size: 22px;
    font-weight: bold;
}}
.preparing-message {{
    /* _ACCENT_TEXT, not the raw brand accent: #1B8EB1 measures 4.40:1 on
       this ground -- under the 4.5:1 floor the rest of the booth holds to,
       and this is the one line a visitor reads when something has gone
       wrong. Caught by extending the shared legibility guard to cover this
       file's own widgets (Task 10). */
    color: {_ACCENT_TEXT};
    font-size: 15px;
}}
window, .booth-root, .booth-side {{
    background-color: {_DARK_BASE};
}}
.booth-logo {{
    color: {_TEAL};
    font-family: "Berkeley Mono", monospace;
    font-size: 8pt;
}}
.booth-title {{
    color: {_BG};
    font-size: 14pt;
    font-weight: 700;
}}
.booth-sub {{
    color: {_BG_ALT};
    font-size: 10pt;
}}
/* The two affordances that tell a visitor this booth does anything at all
   when you press it. Small, letterspaced, permanently visible -- the panel
   and the help card they open are not. */
.booth-hint {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: {_BG_ALT};
}}
.booth-hint-key {{
    font-family: "Berkeley Mono", monospace;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: {_ACCENT_TEXT};
}}
/* The `?` card. A near-opaque wash of the same ground rather than a
   different surface colour, so it reads as the booth dimming itself rather
   than as a dialog from some other application. */
.help-overlay {{
    background-color: rgba(9, 34, 33, 0.96);
}}
.help-title {{
    color: {_BG};
    font-size: 26px;
    font-weight: 700;
}}
.help-body {{
    color: {_BG_ALT};
    font-size: 13pt;
}}
.help-section {{
    color: {_ACCENT_TEXT};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.12em;
}}
.help-key {{
    font-family: "Berkeley Mono", monospace;
    color: {_BG};
    font-size: 12pt;
    font-weight: 700;
}}
.help-desc {{
    color: {_BG_ALT};
    font-size: 12pt;
}}
.help-note {{
    color: {_BG_ALT};
    font-size: 10pt;
    font-style: italic;
}}
.plddt-swatch {{
    border-radius: 3px;
    min-width: 34px;
    min-height: 14px;
}}
{_plddt_swatch_css()}
"""

# ── help copy ───────────────────────────────────────────────────────────────
#
# Plain language, on purpose: the person most likely to press `?` is the one
# who does not already know what any of this is. Every claim here is one the
# booth can actually back up -- the fold IS running on the cards in this
# room, the trajectory IS the model's own, and the timings are the measured
# ones from docs/followups.md's 30-fold soak (4.35-4.45s warm), not marketing
# numbers.
_HELP_INTRO = (
    "A protein structure prediction, running right now on Tenstorrent "
    "Blackhole chips a few feet away. This is not a recording or an "
    "animation: the cloud of points collapsing on screen is the model's own "
    "working, streamed off the chip as it computes, and the ribbon at the "
    "end is the structure it just predicted.",

    "A protein is a chain of amino acids that only does its job once it "
    "folds into a particular three-dimensional shape. Predicting that shape "
    "from the sequence of the chain alone is the problem this model solves "
    "— here, in about four and a half seconds per protein.",

    "It works by denoising: the model starts from a cloud of random atom "
    "positions and pulls it, over roughly 200 small steps, into a real "
    "structure. Touch the screen to see everything this booth folds.",

    # The disclosure, in the visitor's own words. The booth folds its
    # playlist in its own order and a tap cannot change that: the socket
    # protocol is one-way (runner/server.py broadcasts, ui/client.py never
    # sends), so `_on_pick` reaches the state machine and nothing further.
    # Stated as a fact rather than an apology, and paired with what IS on
    # offer -- see ui/gallery.py's own module docstring, which carries the
    # same rule for the copy on that screen.
    "The booth works through its proteins one after another, all day. You "
    "can look through them at any time; asking it to fold a particular one "
    "on demand isn't wired up yet, so what you see next is whatever it "
    "reaches next.",
)

# Every key the booth answers to, and what it does. This table is the ONE
# place the bindings are described to a visitor, and the test
# `test_every_key_the_booth_answers_to_is_listed_in_the_help` walks
# `_handle_key`'s real behavior against it -- so a binding added to the
# handler without a line here fails the suite rather than quietly becoming
# folklore.
_KEY_HELP = (
    ("?  or  F1", "this card — from any screen, at any time"),
    ("D", "diagnostics: the live protocol log in the right-hand rail"),
    ("Esc", "close this card, or close the diagnostics panel"),
    ("any other key,\nor a tap anywhere",
     "wake the booth and look through the proteins it folds"),
    ("Ctrl + F", "leave or return to fullscreen — for the booth operator"),
    ("Ctrl + Q", "quit the booth — for the booth operator"),
)

_HELP_PANELS = (
    "Pipeline — one row per stage of a fold: msa, prep, trunk, diffusion, "
    "confidence, saving. The bright row is the stage running right now; "
    "diffusion owns most of the bar because it does most of the work.",

    # The cadence here is `ui/telemetry.py`'s TelemetrySampler(period_s=2.0)
    # -- one `tt-smi` snapshot every two seconds, on its own thread. This
    # paragraph used to say "read from the driver twice a second", which was
    # wrong twice over: it is 4x the real rate (500ms is `_TELEMETRY_REPAINT_MS`,
    # the REPAINT cadence, not the sample rate) and it is a `tt-smi`
    # subprocess, not a driver read. The chip panel below it genuinely does
    # read the driver, once a second, and says so.
    "Chips — temperature, power draw and clock speed for every Tenstorrent "
    "chip in this machine, taken from a tt-smi snapshot every two seconds. A "
    "Blackhole p300c board carries two chips, so the four chips here are two "
    "boards. It is independent of the fold, so the silicon keeps breathing "
    "even if a fold stalls.",

    # Every claim in this paragraph was checked against the rendered pixels
    # before it was written. An earlier draft said each grid was "driven by
    # that chip's own clock" -- the per-chip feed IS wired (ui/chipviz.py),
    # but at this size it makes no visible difference, so the sentence was
    # cut rather than left as a nice-sounding thing the screen does not
    # actually do. What IS live and per-chip is the clock number, and the
    # temperatures directly above it.
    "Tensix activity — one animated Tensix core grid per chip, in the same "
    "left-to-right order as the readouts above it. Only the chip actually "
    "running this fold animates the work — a spreading ring while the model "
    "is denoising atom positions, a steady glow while it is reasoning about "
    "which residues touch — and the others sit idle, because today the fold "
    "runs on one chip and the header says which. The number beside it is the "
    "fastest clock any of these chips is running at right now, read from the "
    "driver every second. It is a picture of the work, not a trace of "
    "individual cores.",
)

_APP_CSS_INSTALLED = False


def _ensure_app_css_installed():
    """Install `_APP_CSS` once, against the default display.

    Same shape (and same reason) as ui/panels.py's and ui/gallery.py's
    installers: guarded on a display existing at all, so building a widget
    from this module never hard-requires one -- which is what lets the
    legibility tests construct the help card and the side rail directly,
    without running `do_activate` or a main loop.
    """
    global _APP_CSS_INSTALLED
    if _APP_CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        log.debug("no default display; skipping app CSS install")
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_APP_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _APP_CSS_INSTALLED = True


class DemoApp(Gtk.Application):
    def __init__(self, socket_path=None, playlist_path=None, target_ids=None,
                 clock=None):
        super().__init__(application_id="com.tenstorrent.ttbiodemo")
        self.socket_path = socket_path
        self.playlist_path = playlist_path
        # Which manifest entries this run actually offers. None/empty means
        # "all of them". scripts/run-demo.sh passes the SAME selection here
        # that it uses to build the daemon's fold directory, which is what
        # stops the gallery advertising a target the daemon has no input
        # file for -- see ui/playlist.py's module docstring.
        self.target_ids = list(target_ids) if target_ids else []

        # The booth's one source of truth for what is on screen. Everything
        # else in this class either feeds it (events, touches, the clock) or
        # reads it (`_sync_to_state`).
        self.states = StateMachine(showcase_dwell_s=_SHOWCASE_DWELL_S)
        # What `_sync_to_state` last acted on, so it can spot the EDGE of a
        # transition (`showcase_ended`) rather than its level.
        self._last_state = self.states.state

        # Injectable purely so tests can drive the two time-based
        # transitions -- the showcase dwell and the 45s idle timeout -- by
        # hand instead of sleeping through them. Production always gets
        # time.monotonic (never time.time: a wall-clock adjustment mid-fold
        # must not end a showcase or fire an idle timeout).
        self._clock = clock if clock is not None else time.monotonic

        # The UI samples tt-smi itself rather than reading chip telemetry off
        # the socket, so a wedged or dead daemon still leaves the silicon
        # visibly breathing (spec section 6, ui/telemetry.py). Constructed
        # here, started in do_activate: constructing it starts no thread, so
        # a headless test can swap it out.
        self.sampler = TelemetrySampler()

        # Widgets, all created in do_activate (which needs a display). Named
        # here so every one of them is a real attribute from construction --
        # a headless test substitutes a recorder, and `_sync_to_state` and
        # the tick callbacks all tolerate `None`.
        self.viewer = None
        self.gallery = None
        self.screens = None
        self.telemetry_panel = None
        self.pipeline_panel = None
        # The Tensix activity panel (ui/chipviz.py). May legitimately stay
        # None (headless tests, and the moment before do_activate runs) and
        # may legitimately exist-but-be-unavailable (no WebKit, no chips);
        # `_sync_chipviz` tolerates both.
        self.chipviz_panel = None
        self.targets = []
        self._preparing_box = None
        self._preparing_message_label = None
        self._window = None

        # ── the two interactive surfaces this task adds ──────────────────
        #
        # The log is created here, not in do_activate, for the same reason
        # `_frames` is: events can arrive before (or without) any widget, and
        # a bounded buffer that is always present means no call site has to
        # ask whether diagnostics exist yet. The PANEL may legitimately be
        # None (headless tests, and the moment before do_activate runs);
        # every method below tolerates that.
        self.diagnostics = DiagnosticsLog()
        self.diagnostics_panel = None
        self._diagnostics_toggle_label = None
        self._help_box = None

        # Visibility is tracked as plain booleans, NOT read back off the
        # widgets: `_handle_key`'s decisions have to be testable without a
        # display, and a widget-derived answer would also be wrong for the
        # window between construction and realization.
        self.diagnostics_visible = False
        self.help_visible = False

        # When the last visitor input of any kind happened, on the injected
        # clock -- the only thing that can close an overlay nobody is using
        # (see `_HELP_IDLE_S`). None means "nobody has touched this booth
        # yet", which is exactly when there is nothing to time out.
        self._last_input_at = None

        # Neutral copy for the preparing overlay plus the raw `missing`
        # detail, which goes to the log and NEVER to the screen. The state
        # itself is not stored here -- `display_state` reads through to the
        # machine (see that property), which is the plan's named integration
        # seam: two places that can disagree about whether the booth is
        # preparing is exactly what Task 9 had to collapse.
        self.missing = []
        self.display_message = ""

        # Diffusion frames, one slot, latest wins. Created here rather than
        # in `_start_client` so `_drain_frames` is callable with no socket
        # at all (headless tests, and a booth started with no --socket).
        self._frames = LatestFrame()
        self._client = None

        # How many times each kind of repeating failure has been logged --
        # see _note_dropped and _DROP_LOG_EVERY. Per-app rather than
        # module-level so one booth session's counts cannot leak into
        # another's (or into another test's).
        self._drop_counts = {}

        # The `job_start` clear that a showcase deferred. See the module
        # docstring: the clear belongs to `job_start`, not to the end of the
        # dwell, so with no next fold in flight (an idle or dead daemon)
        # nothing is cleared and the last structure keeps the screen.
        self._deferred_clear = False

        # ribbon_from_cif costs up to ~1.2s at 3000 residues (measured,
        # docs/followups.md) and must never run on the thread that calls
        # _handle_event -- for real traffic that's the GTK main loop, via
        # _on_event's GLib.idle_add dispatch. See _spawn_ribbon_worker,
        # _ribbon_worker_main, and _drain_pending_ribbon below for the full
        # scheme; the fields here are just its state:
        #
        # _ribbon_lock guards the two fields below it against concurrent
        # access from worker threads and the main thread at once.
        # _ribbon_generation is a monotonic counter, bumped once per
        # job_done that actually starts a worker -- it is the answer to
        # "which fold is the newest one in flight", independent of which
        # worker happens to finish first. _pending_ribbon holds at most one
        # not-yet-applied result, as (generation, cif_path, outcome) --
        # never more than one, because a fold that is superseded before its
        # result is even applied should never reach the screen at all (see
        # _ribbon_worker_main's docstring for the ordering argument).
        # _ribbon_threads is bookkeeping only (test joins + shutdown
        # visibility) -- nothing about correctness depends on this list;
        # the generation check is what actually decides what lands.
        self._ribbon_lock = threading.Lock()
        self._ribbon_generation = 0
        self._pending_ribbon = None
        self._ribbon_threads = []

    # ── the one source of truth ─────────────────────────────────────────
    #
    # Task 1 gave this class a plain `display_state` field so the preparing
    # screen could ship before a state machine existed; Task 7 then built
    # the machine, with its own `preparing` state. The plan named this the
    # phase's one integration seam and required Task 9 to collapse them
    # rather than leave two fields that can disagree. This is that collapse:
    # `display_state` is now a read-through, kept only because it is the
    # name the rest of the codebase (and Task 1's tests) already use.

    @property
    def display_state(self):
        """The booth's state, as a plain string. Read-only: to change it,
        drive the machine (`_handle_event`, `_on_touch`, `_on_pick`,
        `_tick_state`)."""
        return self.states.state

    def do_activate(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("tt-bio")
        window.set_default_size(1280, 800)
        self._window = window

        _ensure_app_css_installed()

        self.viewer = StructureViewer()
        self.viewer.set_hexpand(True)
        self.viewer.set_vexpand(True)

        # The preparing overlay sits on top of the viewer, not in place of
        # it, and its visibility is driven purely by the booth state -- it
        # has no dependency on the viewer ever having held a ribbon or even
        # a single frame of points, so it renders correctly from the very
        # first activate, before any fold (or even any connection) happens.
        viewer_page = Gtk.Overlay()
        viewer_page.set_hexpand(True)
        viewer_page.set_vexpand(True)
        viewer_page.set_child(self.viewer)
        viewer_page.add_overlay(self._build_preparing_overlay())

        # The hero slot holds either the protein or the gallery; the rail
        # stays put across both, so the silicon keeps visibly breathing
        # while a visitor is choosing what to fold.
        self.screens = Gtk.Stack()
        self.screens.set_hexpand(True)
        self.screens.set_vexpand(True)
        self.screens.add_named(viewer_page, "viewer")
        self._build_gallery()

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root.add_css_class("booth-root")
        root.append(self.screens)
        root.append(self._build_side_rail())

        logo = Gtk.Label(label=TT_BIO_LOGO)
        logo.add_css_class("booth-logo")
        logo.set_halign(Gtk.Align.END)
        logo.set_valign(Gtk.Align.END)
        logo.set_margin_end(28)
        logo.set_margin_bottom(22)

        root_overlay = Gtk.Overlay()
        # The overlay paints the same ground as the box inside it, so its
        # own overlay children (the logo, the help card) sit on a known,
        # explicitly-set background rather than on whatever the desktop
        # theme would otherwise show through -- which is also what makes
        # them contrast-checkable (see `_BACKGROUND_BY_CLASS`).
        root_overlay.add_css_class("booth-root")
        root_overlay.set_child(root)
        root_overlay.add_overlay(logo)
        root_overlay.add_overlay(self._build_help_overlay())
        window.set_child(root_overlay)

        # A kiosk: no chrome, no window management, the protein as large as
        # the glass allows.
        window.fullscreen()
        self._connect_visitor_input(window)
        window.present()

        self.viewer.start_animation()
        self._sync_to_state(force=True)

        self.sampler.start()
        # The Tensix panel's AICLK poll. Started here, next to the telemetry
        # sampler, for the same reason and with the same independence: it
        # reads the DRIVER, not the socket, so the animation keeps its clock
        # readout honest even with no daemon at all. A no-op when the panel
        # is unavailable.
        self.chipviz_panel.set_running(True)
        self._sync_chipviz()
        self._start_timers()

        if self.socket_path:
            self._start_client()

    def do_shutdown(self):
        """Stop the two background threads this app owns before GTK tears
        the process down. Both are daemon threads, so this is tidiness
        rather than a correctness requirement -- but `TelemetrySampler.stop`
        joins, which makes an in-flight `tt-smi` call finish against a live
        process rather than one already unwinding."""
        try:
            self.sampler.stop()
            if self.chipviz_panel is not None:
                # Removes the GLib poll source outright (ui/chipviz.py's
                # `set_running`), rather than leaving a timer registered
                # against a widget that is going away.
                self.chipviz_panel.set_running(False)
            if self._client is not None:
                self._client.stop()
        except Exception:
            log.exception("error during shutdown")
        Gtk.Application.do_shutdown(self)

    # ── layout ───────────────────────────────────────────────────────────

    def _build_side_rail(self):
        """The fixed-width column: identity, then what the machine is doing,
        then what the silicon is doing.

        `set_hexpand(False)` plus an explicit width is load-bearing, not a
        preference -- see `_SIDE_RAIL_WIDTH_PX`.
        """
        _ensure_app_css_installed()
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        side.add_css_class("booth-side")
        side.set_size_request(_SIDE_RAIL_WIDTH_PX, -1)
        side.set_hexpand(False)
        side.set_valign(Gtk.Align.START)
        for margin in ("set_margin_top", "set_margin_bottom",
                       "set_margin_start", "set_margin_end"):
            getattr(side, margin)(18)

        title = Gtk.Label(label="Folding on Tenstorrent Blackhole")
        title.add_css_class("booth-title")
        title.set_xalign(0.0)
        title.set_wrap(True)
        subtitle = Gtk.Label(label="live diffusion trajectory, computed in this room")
        subtitle.add_css_class("booth-sub")
        subtitle.set_xalign(0.0)
        subtitle.set_wrap(True)
        side.append(title)
        side.append(subtitle)

        self.pipeline_panel = PipelinePanel()
        self.telemetry_panel = TelemetryPanel()
        # Directly BELOW the telemetry panel, sharing its left-to-right chip
        # order, so animation N sits under chip N's readout: the two are one
        # instrument in two halves (the numbers, then the picture), which is
        # what makes the animation legible as "these four chips, right here"
        # rather than as generic decoration. Hides itself when unavailable
        # (no WebKit, no chips, no bundled assets) -- see ui/chipviz.py.
        self.chipviz_panel = ChipVizPanel()
        for panel in (self.pipeline_panel, self.telemetry_panel,
                      self.chipviz_panel):
            panel.set_hexpand(False)
            panel.set_vexpand(False)
            side.append(panel)

        # Below the progress legend and the chip readout, in the space the
        # rail was leaving empty (see .superpowers/.../booth-wired.png): the
        # hint row, always visible, and the diagnostics panel it opens,
        # hidden until asked for. The protein is the hero; this is for the
        # curious visitor and for us.
        side.append(self._build_hint_row())
        self.diagnostics_panel = DiagnosticsPanel()
        self.diagnostics_panel.set_visible(self.diagnostics_visible)
        self.diagnostics_panel.refresh(self.diagnostics, force=True)
        side.append(self.diagnostics_panel)
        return side

    def _build_hint_row(self):
        """The two small affordances that say the booth is interactive.

        Both are clickable AND keyed, because the user asked for both ("with
        a press of a button or a click"), and because a booth may or may not
        have a keyboard in front of the public. Each click handler CLAIMS its
        gesture sequence, which is what stops the window-wide "any click is a
        visitor touch" gesture (see `_connect_visitor_input`) from also
        opening the gallery underneath the thing the visitor just pressed.
        """
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        row.set_halign(Gtk.Align.START)

        self._diagnostics_toggle_label = self._build_hint(
            row, self._diagnostics_hint_text(), self._toggle_diagnostics)
        self._build_hint(row, "?  HELP", self._show_help)
        return row

    def _build_hint(self, row, text, on_click):
        """One clickable hint label. Returns the label so its text can be
        updated (the diagnostics one changes as the panel opens/closes)."""
        label = Gtk.Label(label=text, xalign=0.0)
        label.add_css_class("booth-hint")
        click = Gtk.GestureClick()
        click.connect("pressed",
                      lambda gesture, *_args: self._on_hint_pressed(gesture, on_click))
        label.add_controller(click)
        row.append(label)
        return label

    def _on_hint_pressed(self, gesture, on_click):
        """One hint label was clicked or tapped.

        The `set_state(CLAIMED)` is the load-bearing line, not boilerplate:
        this gesture and the window-wide "any click is a visitor touch" one
        both sit in the bubble phase, so without claiming the sequence BOTH
        fire and the visitor gets a gallery they did not ask for on top of
        the panel they did. Split out of the lambda that installs it so that
        claim is directly testable with no display and no synthesized input
        (tests/unit/test_app_interaction.py).
        """
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._note_input()
        on_click()

    def _diagnostics_hint_text(self):
        return ("▾  DIAGNOSTICS  ·  D" if self.diagnostics_visible
                else "▸  DIAGNOSTICS  ·  D")

    def _build_gallery(self):
        """Load the playlist and build the pick grid, or ship without one.

        A manifest that is missing or malformed must not take the booth
        down and must not put a stack trace on the screen: it costs the
        gallery (a touch then simply leaves the protein up, and the idle
        timeout returns to attract on its own) and nothing else. The
        detail goes to the log, where an operator can act on it.
        """
        path = self.playlist_path or _DEFAULT_PLAYLIST
        try:
            self.targets = select_targets(load_playlist(path), self.target_ids)
        except PlaylistError:
            log.exception("playlist %s could not be loaded; the booth will "
                          "run without a gallery", path)
            self.targets = []
            return
        self.gallery = Gallery(self.targets, on_pick=self._on_pick,
                               width_px=_GALLERY_WIDTH_PX)
        # Cards at their natural width, centred -- NOT stretched to fill.
        # `Gallery` sets hexpand(True) on itself, which is right for a grid
        # that fills a window and wrong for this booth's one-target
        # playlist: rendered and looked at, a single card stretched to the
        # full 1490px hero slot is a 1300px-wide empty thumbnail tile with
        # one small letter adrift in it (verified on screen before this
        # line existed). Natural width is `columns x ~348px`, and
        # `grid_shape` already caps the column count at 400px per column,
        # so this can never want more room than the slot has. Vertical
        # expansion is left alone so a playlist taller than the screen
        # still scrolls.
        self.gallery.set_hexpand(False)
        self.gallery.set_halign(Gtk.Align.CENTER)
        self.screens.add_named(self.gallery, "gallery")

    def _build_preparing_overlay(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_hexpand(True)
        box.set_vexpand(True)
        box.set_halign(Gtk.Align.FILL)
        box.set_valign(Gtk.Align.FILL)
        box.add_css_class("preparing-overlay")

        title = Gtk.Label(label="Preparing")
        title.add_css_class("preparing-title")
        title.set_halign(Gtk.Align.CENTER)
        title.set_valign(Gtk.Align.CENTER)
        title.set_vexpand(True)

        message = Gtk.Label()
        message.add_css_class("preparing-message")
        message.set_halign(Gtk.Align.CENTER)
        message.set_wrap(True)
        message.set_justify(Gtk.Justification.CENTER)

        box.append(title)
        box.append(message)

        self._preparing_box = box
        self._preparing_message_label = message
        return box

    # ── the `?` card ─────────────────────────────────────────────────────

    def _build_help_overlay(self):
        """The help card: what this booth is, every key, and what the panels
        on the right actually mean.

        Written for a visitor who has never heard of any of this -- no
        jargon that isn't unpacked in the same sentence -- and true: the
        fold really is running on the chips in this room while this card is
        on top of it (the overlay is a widget, not a modal loop; every GLib
        source underneath keeps firing, which is the point of building it
        this way and is asserted in tests/unit/test_app_interaction.py).
        """
        _ensure_app_css_installed()
        ground = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        ground.add_css_class("help-overlay")
        ground.set_hexpand(True)
        ground.set_vexpand(True)
        ground.set_halign(Gtk.Align.FILL)
        ground.set_valign(Gtk.Align.FILL)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        card.set_halign(Gtk.Align.CENTER)
        # vexpand AND valign=CENTER: in a vertical box, `valign` alone only
        # centres a child inside the (natural-height) cell it was given, so
        # the card would sit at the top of a 1080px screen with half the
        # glass empty below it -- looked at, on real glass, before this line
        # existed. Expanding first gives it the whole column to centre in.
        card.set_vexpand(True)
        card.set_valign(Gtk.Align.CENTER)
        card.set_size_request(980, -1)
        for margin in ("set_margin_top", "set_margin_bottom",
                       "set_margin_start", "set_margin_end"):
            getattr(card, margin)(40)

        card.append(self._help_label("What you are looking at", "help-title"))
        for paragraph in _HELP_INTRO:
            card.append(self._help_label(paragraph, "help-body", wrap=True))

        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=56)
        columns.set_homogeneous(True)
        columns.append(self._build_help_keys())
        columns.append(self._build_help_panels())
        card.append(columns)

        card.append(self._help_label(
            "The fold carries on behind this card — nothing is paused.",
            "help-note", wrap=True))

        ground.append(card)
        ground.set_visible(False)
        self._help_box = ground
        return ground

    @staticmethod
    def _help_label(text, css_class, wrap=False):
        """One label on the help card.

        Every label built here takes a colour-bearing class from `_APP_CSS`
        -- the project's legibility rule ("an explicitly-set background
        implies an explicitly-set foreground") applies to this card exactly
        as it does to the panels, and the shared guard in
        tests/unit/_legibility.py now walks this tree to prove it.
        """
        label = Gtk.Label(label=text, xalign=0.0)
        label.add_css_class(css_class)
        label.set_wrap(wrap)
        if wrap:
            label.set_max_width_chars(88)
        return label

    def _build_help_keys(self):
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        column.append(self._help_label("KEYS", "help-section"))
        grid = Gtk.Grid(column_spacing=20, row_spacing=8)
        for row_index, (keys, meaning) in enumerate(_KEY_HELP):
            key_label = self._help_label(keys, "help-key")
            key_label.set_valign(Gtk.Align.START)
            meaning_label = self._help_label(meaning, "help-desc", wrap=True)
            meaning_label.set_max_width_chars(34)
            grid.attach(key_label, 0, row_index, 1, 1)
            grid.attach(meaning_label, 1, row_index, 1, 1)
        column.append(grid)
        return column

    def _build_help_panels(self):
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        column.append(self._help_label("THE PANELS ON THE RIGHT", "help-section"))
        for paragraph in _HELP_PANELS:
            label = self._help_label(paragraph, "help-desc", wrap=True)
            label.set_max_width_chars(52)
            column.append(label)

        # Per RESIDUE, not per atom. ui/geometry.py's `load_ca_trace` reads
        # one pLDDT per residue -- the CA atom's B-factor -- and that is
        # what colours the ribbon a visitor is looking at while reading
        # this legend. ui/diagnostics.py's STAGE_TEACHING carried the same
        # error and is fixed with it.
        column.append(self._help_label(
            "pLDDT — the model's own confidence, per residue:", "help-desc",
            wrap=True))
        for css_class, range_text, meaning in _PLDDT_LEGEND:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            # A swatch is a painted BOX, never a coloured label: the top of
            # the ramp (#0053D6) measures 2.54:1 on this ground and would be
            # illegible as text. The words next to it are ordinary body
            # text on the card's own ground.
            swatch = Gtk.Box()
            swatch.add_css_class("plddt-swatch")
            swatch.add_css_class(css_class)
            swatch.set_valign(Gtk.Align.CENTER)
            row.append(swatch)
            row.append(self._help_label(f"{range_text}  ·  {meaning}",
                                        "help-desc", wrap=True))
            column.append(row)
        return column

    def _connect_visitor_input(self, window):
        """Any tap, click or keypress reaches the booth through here.

        Which of those a venue actually has is a booth-setup decision (the
        plan leaves touchscreen hardware out of this phase on purpose), so
        all three are wired to the same place. The gesture sits on the
        window in the default bubble phase, so a tap on a gallery card (or
        on a hint label, which CLAIMS its sequence -- see `_build_hint`) is
        that widget's first.

        Almost every key is still just "a visitor touched the booth". The
        handful that are not -- `?`, `D`, `Esc`, and the operator's two
        Ctrl chords -- are decided in `_handle_key`, which takes a plain
        key NAME and a plain bool so that decision is testable with no GTK,
        no window and no display.
        """
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_args: self._on_click())
        window.add_controller(click)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        window.add_controller(keys)

    def _on_key_pressed(self, _controller, keyval, _keycode, modifiers):
        """GTK adapter: turn a keyval + modifier mask into the two plain
        values `_handle_key` reasons about, and nothing else."""
        name = Gdk.keyval_name(keyval) or ""
        ctrl = bool(modifiers & Gdk.ModifierType.CONTROL_MASK)
        return self._handle_key(name, ctrl=ctrl)

    # ── the booth's screen, reconciled against the machine ───────────────

    def _sync_to_state(self, force=False):
        """Make the screen agree with `self.states`. Called after every
        single thing that can move the machine -- events, touches, picks,
        the clock -- and idempotent, so calling it more often than
        necessary costs nothing.

        This is where the deferred `job_start` clear finally happens (see
        the module docstring, step 3): the moment the dwell expires, the
        booth applies the clear it held back and immediately drains the
        buffered diffusion frame, so the next fold's collapse appears in
        the same instant the finished structure leaves rather than after a
        blank gap.
        """
        state = self.states.state
        previous, self._last_state = self._last_state, state

        if showcase_ended(previous, state) and self._deferred_clear:
            self._deferred_clear = False
            self.viewer.clear_structure()
            # Not a fresh frame -- the newest SUPPRESSED one, still sitting
            # in the latest-wins buffer. This is the whole reason frames are
            # suppressed rather than dropped.
            self._drain_frames()

        if state == previous and not force:
            return

        if self.screens is not None:
            self.screens.set_visible_child_name(
                "gallery" if state == "gallery" and self.gallery is not None
                else "viewer")

        if self._preparing_box is not None:
            is_preparing = state == "preparing"
            self._preparing_box.set_visible(is_preparing)
            if is_preparing:
                self._preparing_message_label.set_label(self.display_message)

    # ── visitor input ────────────────────────────────────────────────────

    def _on_touch(self):
        """A visitor touched the booth."""
        self._note_input()
        self.states.on_touch()
        self._sync_to_state()

    def _on_click(self):
        """A click or tap anywhere on the window.

        One rule, and it is the same one the keyboard follows: if the help
        card is up, this dismisses it and does nothing else. A visitor who
        taps to get rid of the help card has not asked to open a gallery
        behind it, and finding one there would be exactly the "why did it
        do that" moment this whole task exists to remove.
        """
        self._note_input()
        if self.help_visible:
            self._set_help_visible(False)
            return
        self._on_touch()

    def _handle_key(self, name, ctrl=False):
        """Decide what one key press means. Returns True if the booth
        consumed it (GTK's "handled"), which is always -- a kiosk has
        nowhere else for a key to go.

        Deliberately takes a NAME and a BOOL rather than a GDK keyval and a
        modifier mask: this is the whole of the booth's keyboard policy, it
        is the part a visitor and an operator both depend on, and it is
        therefore the part that must be testable without a display. The GTK
        adapter is `_on_key_pressed`, three lines above, and it decides
        nothing.

        Order matters, and each step is here for a reason:

        1. The operator's Ctrl chords work from ANY screen, including with
           the help card up -- if the booth needs to be quit or unfullscreened
           at a venue, no visitor-facing state may stand in the way.
        2. With the help card up, ANY key closes it and nothing else
           happens. `?` and `Esc` are the documented ways out (per the
           user's request), but a visitor pressing something random while a
           wall of text is up means "get rid of this", not "and also open
           the gallery behind it".
        3. `?` opens the card from anywhere -- attract, gallery, folding,
           showcase, preparing. It is chrome; it does not touch booth state.
        4. `D` toggles diagnostics; `Esc` closes it if it is open.
        5. Everything else is a visitor touch, exactly as before.
        """
        self._note_input()
        lowered = (name or "").lower()

        if ctrl and lowered == "q":
            log.info("operator quit (ctrl+q)")
            self.quit()
            return True
        if ctrl and lowered == "f":
            self._toggle_fullscreen()
            return True
        if ctrl:
            # An unbound chord is swallowed rather than treated as a touch:
            # a stray Ctrl+something must not open the gallery.
            return True

        if self.help_visible:
            self._set_help_visible(False)
            return True

        if lowered in _HELP_KEYS:
            self._show_help()
            return True
        if lowered in _DIAGNOSTICS_KEYS:
            self._toggle_diagnostics()
            return True
        if lowered == "escape":
            # Nothing to close but the diagnostics panel; and if that is
            # shut too, Escape does nothing at all -- notably it does NOT
            # count as a touch, so a visitor cannot back out of a screen
            # into a gallery they did not ask for.
            if self.diagnostics_visible:
                self._set_diagnostics_visible(False)
            return True

        self._on_touch()
        return True

    # ── chrome: the diagnostics panel and the help card ──────────────────
    #
    # Neither of these is booth STATE -- they are chrome laid over whatever
    # the state machine is doing, which is precisely why `?` can work "at
    # any time" without ui/states.py growing a sixth state and every
    # transition in it growing an opinion about overlays. The one thing they
    # do borrow from the state machine is its idea that a visitor who walks
    # away should not leave the booth changed: `_tick_overlays` closes them
    # both after a period of no input at all.

    def _note_input(self):
        """Stamp "a human did something just now", for the overlay idle
        timers. Cheap and idempotent; called from every input path."""
        try:
            self._last_input_at = self._clock()
        except Exception:
            # A clock that raises must not cost the booth a keypress.
            log.exception("clock failed while stamping visitor input")

    def _toggle_diagnostics(self):
        self._set_diagnostics_visible(not self.diagnostics_visible)

    def _set_diagnostics_visible(self, visible):
        self.diagnostics_visible = visible
        if self.diagnostics_panel is not None:
            self.diagnostics_panel.set_visible(visible)
            if visible:
                # Repaint immediately rather than waiting up to 100ms for
                # the next tick: the panel must never appear empty (or
                # showing the state it had when it was last closed) in the
                # instant a visitor opens it.
                self.diagnostics_panel.refresh(self.diagnostics, force=True)
        if self._diagnostics_toggle_label is not None:
            self._diagnostics_toggle_label.set_label(self._diagnostics_hint_text())

    def _show_help(self):
        self._set_help_visible(True)

    def _set_help_visible(self, visible):
        self.help_visible = visible
        if self._help_box is not None:
            self._help_box.set_visible(visible)

    def _toggle_fullscreen(self):
        """Operator escape hatch: get to the desktop without killing the
        booth. A no-op if there is no window yet (headless tests)."""
        if self._window is None:
            return
        if self._window.is_fullscreen():
            self._window.unfullscreen()
        else:
            self._window.fullscreen()

    def _tick_overlays(self, now):
        """Close chrome a visitor walked away from.

        Runs off the same 100ms tick as the state machine. Both timers are
        measured from the last input of ANY kind, so neither overlay can
        close while someone is still pressing things -- and neither can
        outlive the visitor who opened it, which is what the booth's own
        idle timeout guarantees for the screens the state machine owns.
        """
        if self._last_input_at is None:
            return
        idle_s = now - self._last_input_at
        if self.help_visible and idle_s >= _HELP_IDLE_S:
            log.info("help overlay closed after %.0fs idle", idle_s)
            self._set_help_visible(False)
        if self.diagnostics_visible and idle_s >= _DIAGNOSTICS_IDLE_S:
            log.info("diagnostics panel closed after %.0fs idle", idle_s)
            self._set_diagnostics_visible(False)

    def _on_pick(self, target_id):
        """A visitor picked a target off the gallery.

        The pick drives the state machine and closes the gallery. It does
        NOT yet reach the daemon: the socket protocol is one-way
        (runner/server.py broadcasts; there is no client->server message),
        so the daemon's priority queue -- which exists and reserves a
        higher priority for exactly this -- cannot currently be reached
        from here. Logged, and recorded as a known gap in this task's
        report; the booth still shows the visitor the fold that is running.

        Every visitor-facing string that used to contradict this paragraph
        has been reworded (whole-branch review, Critical 2): the gallery
        says what it is rather than "TAP TO FOLD", and the help card says
        plainly that picking on demand is not wired up. If this method ever
        DOES reach the daemon, that copy is what changes with it --
        ui/gallery.py's module docstring and `_HELP_INTRO` above both point
        back here.
        """
        log.info("visitor picked %s", target_id)
        self._note_input()
        self._note_diagnostics(
            self.diagnostics.note, f"visitor picked {target_id}", KIND_MARK)
        self.states.on_pick(target_id)
        self._sync_to_state()

    # ── GLib sources ─────────────────────────────────────────────────────

    def _start_timers(self):
        """Register every repeating source the booth needs, in one place.

        One place on purpose: each of these is a thing that silently stops
        the booth if it is missing (no state tick means no showcase ever
        ends and the idle timeout never fires; no frame drain means no
        diffusion; no telemetry repaint means the silicon stops breathing),
        and a test can therefore check that all three exist by looking at
        one call.
        """
        GLib.timeout_add(_FRAME_DRAIN_MS, self._drain_frames)
        GLib.timeout_add(_STATE_TICK_MS, self._tick_state)
        GLib.timeout_add(_TELEMETRY_REPAINT_MS, self._tick_telemetry)

    def _tick_state(self):
        """Hand the state machine a clock reading, then reconcile the
        screen. The machine owns no timer of its own (ui/states.py), so
        this source is the only thing that can end a showcase dwell or fire
        the 45s idle timeout.

        Guarded, with the `return True` OUTSIDE the try, for the reason
        spelled out in `_drain_frames`: this is a REPEATING source, and an
        exception escaping it removes the source permanently -- a booth
        frozen mid-showcase with nothing on screen saying so.
        """
        try:
            now = self._clock()
            self.states.tick(now)
            self._sync_to_state()
            self._tick_overlays(now)
            # The pipeline panel's own staleness check, on the same tick.
            # It is reset by `job_start` and nothing else, so without this a
            # daemon that died mid-fold would leave e.g. "DIFFUSION 62%" on
            # screen for the rest of the day -- see ui/panels.py's
            # PIPELINE_STALE_AFTER_S. The panel owns the clock and the
            # threshold; this only gives it a chance to look.
            #
            # Its own guard, like `_sync_chipviz` and `_note_diagnostics`:
            # everything after this line in the tick (the diagnostics
            # repaint) must not be pre-empted by a panel misbehaving.
            if self.pipeline_panel is not None:
                try:
                    self.pipeline_panel.tick()
                except Exception:
                    log.exception("pipeline staleness check dropped")
            # The diagnostics panel repaints from here rather than from
            # every appended line: a 30Hz frame stream would otherwise
            # re-label twenty rows thirty times a second to show a list a
            # human reads at reading speed. `refresh` is a no-op when the
            # log has not moved (revision check) and the panel is skipped
            # entirely while it is closed.
            if self.diagnostics_panel is not None and self.diagnostics_visible:
                self.diagnostics_panel.refresh(self.diagnostics)
        except Exception:
            log.exception("state tick failed")
        return True

    def _tick_telemetry(self):
        """Repaint the telemetry panel from the SAMPLER, never from the
        socket -- that independence is why ui/telemetry.py exists, and it
        is what leaves the silicon visibly breathing when the daemon dies.

        `latest()` is a tri-state (None = tt-smi gave no usable answer,
        [] = tt-smi answered and reported no devices, [readings] = normal)
        and all three are passed straight through: the panel renders them
        as three distinguishable things, and collapsing the first two here
        (`or []`) would turn "we cannot read the hardware" into "there is
        no hardware".

        Same repeating-source guard shape as `_tick_state`.
        """
        try:
            if self.telemetry_panel is not None:
                self.telemetry_panel.update(self.sampler.latest(),
                                            self.sampler.age_s())
        except Exception:
            log.exception("telemetry repaint failed")
        return True

    def _start_client(self):
        self._client = EventClient(
            self.socket_path, self._on_event,
            on_state_change=lambda s: GLib.idle_add(self._on_state, s),
        )
        self._client.start()

    def _on_event(self, event):
        kind = event["type"]
        if kind == "frame":
            self._frames.put(event)
        else:
            GLib.idle_add(self._handle_event, event)

    def _handle_event(self, event):
        # Runs via GLib.idle_add. Measured directly on this PyGObject/GLib
        # version: an uncaught exception here does NOT crash the process --
        # PyGObject's default exception hook logs a traceback and GLib
        # permanently removes the offending source instead of rescheduling
        # it. For a one-shot idle callback that's harmless (it was only
        # going to fire once anyway); but see _drain_frames below, where the
        # equivalent failure on a *repeating* source is a silent, permanent
        # freeze -- worse than a crash for an unattended booth demo, since
        # nothing signals that it happened. Guard this one too for the same
        # "never let wire-shaped data reach an unguarded field access"
        # reason: `event` always has a valid "type" (decode() guarantees
        # that much, nothing else), but e.g. a non-numeric "frac" would
        # still raise inside the %.0f formatting below.
        try:
            kind = event["type"]

            # The machine first, and the screen reconciled second, BEFORE
            # any of the per-event rendering below: what the booth is doing
            # must not depend on a panel or a viewer call succeeding. (The
            # rendering work below is what can plausibly fail on
            # wire-shaped data; the machine only reads event["type"].)
            self.states.on_event(event)
            if kind == "not_ready":
                # Set before the sync below so the overlay it makes visible
                # already carries its copy. Neutral by construction: the
                # `missing` detail names real filesystem paths and must
                # never reach the screen, only the log.
                self.missing = event.get("missing", [])
                self.display_message = _PREPARING_MESSAGE
            self._sync_to_state()
            self._note_diagnostics(self.diagnostics.note_event, event)
            # Re-aim the Tensix animation. A `stage` event carries the one
            # thing that says what the SILICON is doing; every other event
            # only refreshes the booth state (which is what turns the
            # animation off while the daemon is `preparing`). See
            # ui/chipviz.py's `viz_mode` for why the stage, not the screen,
            # decides.
            self._sync_chipviz(
                event.get("stage") if kind == "stage" else None,
                # Only job_start carries which chip claimed the fold, and
                # that is what lets the panel animate THAT chip rather than
                # claiming all four are working. Passing None for every
                # other event leaves the last attribution in place, which
                # is correct for the stage events that follow.
                card=event.get("card") if kind == "job_start" else None)

            if kind == "job_start":
                log.info("folding %s (%s residues) on chip %s",
                         event.get("target_id"), event.get("n_residues"),
                         event.get("card"))
                if self.pipeline_panel is not None:
                    self.pipeline_panel.reset()
                if self.states.state == "showcase":
                    # The daemon has started the next fold while a finished
                    # structure is still being showcased -- the ordering
                    # this whole file is arranged around. Defer the clear;
                    # `_sync_to_state` applies it the moment the dwell ends.
                    self._deferred_clear = True
                else:
                    self.viewer.clear_structure()
            elif kind == "not_ready":
                # The daemon's preflight or model load hasn't finished.
                # `missing` names exactly what's wrong (e.g. real filesystem
                # paths) -- that detail is exactly what an operator needs
                # and exactly what must never reach the screen (constraint:
                # no raw error text on display), so it goes to the log at a
                # level an operator watching the booth will actually see.
                if self.missing:
                    log.warning("booth not ready: %s", _format_missing(self.missing))
                else:
                    log.warning("booth not ready (no detail given)")
            elif kind == "stage":
                log.info("stage %s %.0f%%", event.get("stage"),
                         100.0 * event.get("frac", 0.0))
                if self.pipeline_panel is not None:
                    # The wire carries a WHOLE-FOLD fraction; the panel
                    # wants a within-stage one. set_stage_from_wire is the
                    # one place that conversion lives, which is precisely
                    # why this call site uses it and never set_stage.
                    self.pipeline_panel.set_stage_from_wire(
                        event.get("stage"), event.get("frac", 0.0))
            elif kind == "job_done":
                log.info("done in %.2fs", event.get("wall_s", 0.0))
                cif_path = event.get("cif_path")
                if cif_path:
                    # ribbon_from_cif's cost moved off this thread -- see
                    # _spawn_ribbon_worker. The result comes back later, via
                    # _drain_pending_ribbon on the main loop, not here.
                    self._spawn_ribbon_worker(cif_path)
                else:
                    # A misconfigured runner sending job_done with nothing
                    # to render used to no-op here silently; log it so it's
                    # diagnosable instead of just "the ribbon never showed up".
                    log.warning("job_done for %s has no cif_path; nothing "
                                "to render", event.get("job_id"))
            elif kind == "job_error":
                # Per the protocol spec, `message` may hold arbitrary detail
                # from the runner and must never reach the display -- log it
                # in full so a failed fold is still diagnosable from the
                # logs, which is all this branch does.
                log.error("job %s failed: %s", event.get("job_id"),
                          event.get("message"))
            else:
                # A future protocol addition should be visible in the logs,
                # not silently dropped the way job_error was before this fix.
                #
                # `card_state` lands here deliberately, and must keep landing
                # here: it carries per-chip telemetry, and this file
                # rendering it would be exactly the coupling ui/telemetry.py
                # exists to prevent -- the panel is fed from an independent
                # tt-smi sampler so that a dead daemon still leaves the
                # silicon visibly breathing. Nothing on the wire feeds that
                # panel.
                log.warning("unhandled event type %r", kind)
        except Exception:
            # Rate-limited: a protocol change this build cannot parse would
            # otherwise write one traceback per event for the rest of the
            # day. See _note_dropped.
            kind = event.get("type")
            self._note_dropped(f"event:{kind!r}", f"dropping malformed {kind!r} event")
        return False

    def _sync_chipviz(self, stage=None, card=None):
        """Tell the Tensix activity panel what the booth is doing, and
        (when a `job_start` just said so) which chip is doing it.

        Its own guard, for the same reason `_note_diagnostics` has one: an
        animation is the least important thing happening in `_handle_event`,
        and a failure inside a WebView must never pre-empt the rendering
        below it or turn a perfectly good event into a "dropping malformed
        ..." log line. Tolerates the panel being absent (headless tests) and
        being present-but-unavailable (no WebKit, no chips), because both are
        ordinary.
        """
        if self.chipviz_panel is None:
            return
        try:
            if card is not None:
                self.chipviz_panel.set_folding_chip(card)
            self.chipviz_panel.set_mode(self.states.state, stage)
        except Exception:
            log.exception("Tensix activity panel update dropped")

    def _note_dropped(self, key, message):
        """Log a failure that can repeat every frame, without flooding.

        See `_DROP_LOG_EVERY`. `key` groups failures that are the same
        problem (the frame stream, one event type); `message` is already
        formatted, because this is only ever called from an `except` block
        where the cost of one f-string is irrelevant.

        MUST be called from inside an `except` block -- the first-occurrence
        branch uses `log.exception`, whose whole value here is the traceback
        of the failure that started it.
        """
        count = self._drop_counts.get(key, 0) + 1
        self._drop_counts[key] = count
        if count == 1:
            log.exception("%s (first occurrence; repeats are logged at debug, "
                          "with a count every %d)", message, _DROP_LOG_EVERY)
        elif count % _DROP_LOG_EVERY == 0:
            log.warning("%s (%d times now)", message, count)
        else:
            log.debug("%s (repeat %d)", message, count, exc_info=True)

    def _note_diagnostics(self, method, *args):
        """Feed the diagnostics log without ever letting it cost the booth
        anything.

        Its own guard, deliberately, even though every call site is already
        inside a broad `except`: a diagnostics line is the least important
        thing happening in any of those methods, and it must not be able to
        pre-empt the rendering below it or turn a perfectly good event into
        a "dropping malformed ..." log entry. `ui.diagnostics` is written not
        to raise on wire-shaped data (`_safe`/`_num`); this is the belt to
        that module's braces.
        """
        try:
            method(*args)
        except Exception:
            log.exception("diagnostics line dropped")

    # ── ribbon construction off the main loop ───────────────────────────
    #
    # ribbon_from_cif is pure numpy/gemmi (ui/geometry.py's own docstring:
    # "no GL, no GTK"), so it is safe to run on a plain background thread
    # with no GTK/GL calls anywhere in it. Only the hand-back to the viewer
    # touches GTK, and that always happens on the main loop, via
    # _drain_pending_ribbon.
    #
    # Ordering: the daemon folds continuously (the attract loop), so a slow
    # structure's worker can still be running when the next job_done
    # arrives. Rather than trying to cancel an in-flight worker -- there is
    # no way to interrupt a thread already inside gemmi/numpy C calls, and
    # daemon threads make that unnecessary anyway (see _spawn_ribbon_worker)
    # -- every worker is stamped with a monotonic generation number at
    # spawn time, and both the worker (when it finishes) and the drain
    # step (when the main loop actually applies a result) check that
    # generation against "what is the newest fold we know about right now".
    # A stale result is simply dropped, silently, at whichever of those two
    # points notices first. This makes "only the newest lands" a single
    # integer comparison, correct no matter which worker happens to finish
    # first -- no thread cancellation, no comparing job_id strings (which
    # would need the daemon to guarantee an ordering the protocol doesn't
    # actually promise), and no queue that could let a superseded ribbon
    # sit and apply later.

    def _spawn_ribbon_worker(self, cif_path):
        """Move ribbon_from_cif's cost onto a background thread.

        Threads, not e.g. a process pool: the payload (a cif_path string
        in, four numpy arrays out) is small, gemmi/numpy release the GIL
        for the bulk of their work same as any C-backed numeric library,
        and a thread pool would add a shutdown-coordination problem this
        task doesn't need -- daemon=True below already makes a worker in
        flight at app-exit harmless (see the class docstring above and the
        module-level shutdown note in the task brief): a daemon thread is
        killed outright when the process exits, never blocking it, and it
        never touches a GTK widget itself (only _drain_pending_ribbon does,
        and only from the main loop).
        """
        with self._ribbon_lock:
            self._ribbon_generation += 1
            generation = self._ribbon_generation

        # Bookkeeping only (test joins + a bound on how many dead Thread
        # objects accumulate across a long attract-loop session) -- prune
        # finished workers before adding the new one rather than letting
        # this list grow for the life of the process.
        self._ribbon_threads = [t for t in self._ribbon_threads if t.is_alive()]

        worker = threading.Thread(
            target=self._ribbon_worker_main,
            args=(generation, cif_path),
            name=f"ribbon-worker-{generation}",
            daemon=True,
        )
        self._ribbon_threads.append(worker)
        worker.start()

    def _ribbon_worker_main(self, generation, cif_path):
        """Runs entirely off the main loop -- must never raise out of this
        method. A plain threading.Thread whose target raises doesn't freeze
        anything the way an uncaught exception in a GLib source would (see
        _handle_event's docstring), but it would still just vanish into
        Python's default thread excepthook with nothing for the app to act
        on and nothing applied or logged through this app's own channel.
        Catch broadly -- deliberately not just GeometryError, since
        anything ribbon_from_cif raises must produce the same "log it,
        leave the screen alone" outcome, not a silent thread death.
        """
        try:
            result = ribbon_from_cif(cif_path)
        except Exception as exc:
            outcome = ("error", exc)
        else:
            outcome = ("ok", result)

        with self._ribbon_lock:
            # Stale: a newer fold has started since this worker was
            # spawned -- drop this result outright, it must never reach
            # the screen even if nothing else is waiting to replace it.
            stale = generation != self._ribbon_generation
            # Superseded-in-slot: a *result* from a newer generation is
            # already waiting to be drained. Guards the opposite race from
            # `stale` above -- a straggler finishing after a faster, newer
            # worker must not clobber the newer result that's already
            # sitting in the slot before the main loop got to it.
            superseded_in_slot = (
                self._pending_ribbon is not None
                and generation < self._pending_ribbon[0]
            )
            if not stale and not superseded_in_slot:
                self._pending_ribbon = (generation, cif_path, outcome)

        # Wake the main loop regardless of whether this worker's result was
        # the one actually stored -- GLib.idle_add is safe to call from any
        # thread (the same pattern EventClient's on_state_change already
        # uses, from its own background thread, into this same app; see
        # ui/client.py), and _drain_pending_ribbon is a correct, cheap
        # no-op when there is nothing left to apply.
        GLib.idle_add(self._drain_pending_ribbon)

    def _drain_pending_ribbon(self):
        """Runs on the main loop (scheduled via GLib.idle_add, from a
        ribbon worker thread). Applies the newest still-relevant
        ribbon-construction result to the viewer, or silently discards it
        if a newer fold has started since -- see the class-level comment
        above this section for why a generation check, not thread
        cancellation, is what makes "only the newest lands" correct here.

        Guarded the same broad way as every other GLib-invoked callback in
        this file (_handle_event, _on_state, _drain_frames): this is a
        one-shot idle source, so an uncaught exception here wouldn't cause
        the *repeating*-source freeze those methods' comments warn about,
        but the app must still never show a stack trace or die on e.g. a
        viewer that was torn down mid-drain (app shutting down with a
        worker's result still in flight) -- so treat that case the same as
        any other malformed-input failure: log it, don't crash, leave
        whatever is on screen exactly as it is.
        """
        with self._ribbon_lock:
            pending = self._pending_ribbon
            self._pending_ribbon = None
            current_generation = self._ribbon_generation

        if pending is None:
            return False

        generation, cif_path, outcome = pending
        if generation != current_generation:
            # A newer fold started after this result was produced (it was
            # stored back when it was still current) -- it's stale now.
            return False

        if not ribbon_may_be_revealed(self.states.state):
            # The dwell this structure was built for has already expired
            # (a slow build can outlast it), so the booth has moved on to
            # the next fold's live diffusion. Cross-fading this ribbon in
            # now would hide that diffusion behind a structure from a fold
            # that is over -- the headline defect, arriving late instead of
            # early. Drop it and say so in the log.
            log.info("ribbon for %s finished after its showcase ended; "
                     "not revealing it over live diffusion", cif_path)
            return False

        kind, payload = outcome
        try:
            if kind == "error":
                # Leave whatever's on screen (the last diffusion frame, or
                # a previous ribbon) exactly as it is and just log -- never
                # a stack trace on screen, never a crash. set_ribbon and
                # begin_crossfade below are only reached on success, so a
                # bad CIF simply forfeits the ribbon reveal for this job
                # instead of corrupting the current view.
                log.error("could not build ribbon for %s", cif_path,
                          exc_info=payload)
            else:
                verts, norms, colors, idx = payload
                self.viewer.set_ribbon(verts, norms, colors, idx)
                self.viewer.begin_crossfade()
                # The structure is only NOW something a visitor can see, so
                # this is when its dwell starts -- not back at job_done,
                # which is separated from this instant by the build above
                # and the 0.8s cross-fade below it.
                self.states.on_structure_revealed()
        except Exception:
            log.exception("dropping ribbon result for %s", cif_path)
        return False

    def _on_state(self, state):
        # The display must survive the runner dying: log the transition and
        # keep rendering whatever is already on screen. This runs via
        # GLib.idle_add (on_state_change is invoked from EventClient's
        # background thread; see ui/client.py's own docstring on why GTK/GL
        # calls must never happen there directly), so it's a one-shot idle
        # source like _handle_event -- no repeating-source freeze risk to a
        # *timer*.
        #
        # It still needs its own guard, though: connection_state's setter
        # (ui/viewer.py) deliberately raises ValueError on anything outside
        # {"connected", "disconnected", "incompatible"}, so that a *future*
        # mismatch -- someone adding a state to EventClient without teaching
        # this setter about it -- fails loudly wherever it's set, instead of
        # being stored silently and discovered much later. But
        # EventClient._set_state only invokes on_state_change when the state
        # actually *changes* (see ui/client.py), so if that new, unrecognized
        # state ever becomes the client's steady state, an unguarded raise
        # here would fire once and then never be retried: there'd be no
        # further transition to trigger it again, and connection_state would
        # stay stuck reporting whatever the last valid transition left it at
        # -- silently, on exactly the runner-disconnect path this task
        # exists to harden. Guard it like every other GLib-invoked callback
        # in this file, so the validator can do its job (a bad value still
        # gets logged, loudly, right here) without bricking this channel.
        #
        # Note what this does NOT do: nothing here touches the telemetry
        # panel. A dead daemon must leave the silicon visibly breathing, and
        # it does, because the panel is fed by ui/telemetry.py's own tt-smi
        # sampler on its own thread, with no dependency on this connection
        # at all.
        try:
            log.info("runner connection: %s", state)
            self._note_diagnostics(self.diagnostics.note_connection, state)
            self.viewer.connection_state = state
        except Exception:
            log.exception("dropping unrecognized connection state %r", state)
        return False

    def _drain_frames(self):
        """Put the newest diffusion frame on screen -- unless a finished
        structure is currently holding it.

        Suppressed rather than discarded (see the module docstring, step 2):
        the frame stays in the one-slot latest-wins buffer, so the moment
        the dwell expires `_sync_to_state` drains it and the booth cuts
        straight to live diffusion. Taking it here and throwing it away
        would leave a blank screen for one frame interval at exactly the
        transition a visitor is watching.
        """
        if not points_are_visible(self.states.state):
            return True

        frame = self._frames.take()
        if frame is None:
            return True
        # unpack_coords raises protocol.events.ProtocolError on truncated
        # base64 or a byte count that isn't a whole number of 3-vectors, and
        # decode() only validates that "type" is present and known -- so a
        # malformed coords_b64 payload reaches here unguarded. This source is
        # a REPEATING GLib.timeout_add, unlike _handle_event's one-shot
        # idle_add: confirmed by direct reproduction, an uncaught exception
        # here doesn't crash the process, it permanently removes this 33ms
        # timeout source -- a silent freeze, worse than a crash for an
        # unattended booth since nothing signals it happened. Drop the bad
        # frame and keep going; the next one arrives in <=33ms regardless.
        # Catch Exception broadly (not just ProtocolError) since
        # set_points() itself reshapes wire-shaped data too.
        try:
            coords = unpack_coords(frame["coords_b64"])
            self.viewer.set_points(coords)
            # Logged where it is DRAWN, not where it arrives: the buffer
            # between socket and screen is latest-wins, so this is the only
            # place that can honestly say "a visitor saw this frame".
            self._note_diagnostics(self.diagnostics.note_frame, frame, coords)
        except Exception:
            # Rate-limited, and this is the call site that most needs it:
            # this source runs every 33ms, so a systematically malformed
            # frame stream produced ~30 tracebacks a second. See
            # _note_dropped.
            self._note_dropped("frame", "dropping malformed frame")
        return True


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="tt-bio demo UI")
    parser.add_argument("--socket", default=None,
                        help="runner socket path; omit to show an empty viewer")
    parser.add_argument("--playlist", default=None,
                        help=f"playlist manifest (default: {_DEFAULT_PLAYLIST})")
    parser.add_argument("--targets", default=None,
                        help="comma-separated manifest ids to offer; omit for "
                             "every target in the manifest. MUST match what "
                             "the daemon was given (scripts/run-demo.sh does "
                             "this for you) -- a gallery card whose target the "
                             "daemon has no input file for can never be folded")
    args = parser.parse_args(argv)
    target_ids = [part.strip() for part in (args.targets or "").split(",")
                  if part.strip()]
    return DemoApp(socket_path=args.socket,
                   playlist_path=args.playlist,
                   target_ids=target_ids).run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
