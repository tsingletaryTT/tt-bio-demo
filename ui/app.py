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
- how a grid of targets is laid out belongs to `ui.gallery`.

If a booth decision ever appears in this file as an `if` on raw state, that
is the signal it belongs in one of those modules instead.

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
from ui.client import EventClient, LatestFrame
from ui.gallery import Gallery
from ui.geometry import ribbon_from_cif
from ui.panels import PipelinePanel, TelemetryPanel
from ui.playlist import PlaylistError, load_playlist
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
# within one sample period without the panel rebuilding its cards 30x a
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


# CSS for the booth chrome, in the brand palette from the docs-site theme:
# dark base for every ground, accent/teal for text. Kept as a module-level
# constant (not rebuilt per window) since it never varies. The panels and
# the gallery install their own stylesheets the same way (ui/panels.py,
# ui/gallery.py) -- this one covers only what this file itself builds.
_APP_CSS = """
.preparing-overlay {
    background-color: rgba(9, 34, 33, 0.94); /* #092221, near-opaque */
}
.preparing-title {
    color: #74C5DF;
    font-size: 22px;
    font-weight: bold;
}
.preparing-message {
    color: #1B8EB1;
    font-size: 15px;
}
window, .booth-root, .booth-side {
    background-color: #092221;
}
.booth-logo {
    color: #74C5DF;
    font-family: "Berkeley Mono", monospace;
    font-size: 8pt;
}
.booth-title {
    color: #F1F8F8;
    font-size: 14pt;
    font-weight: 700;
}
.booth-sub {
    color: #C7D9D8;
    font-size: 10pt;
}
"""


class DemoApp(Gtk.Application):
    def __init__(self, socket_path=None, playlist_path=None, clock=None):
        super().__init__(application_id="com.tenstorrent.ttbiodemo")
        self.socket_path = socket_path
        self.playlist_path = playlist_path

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

        # The UI samples tt-smi itself rather than reading card telemetry off
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
        self.targets = []
        self._preparing_box = None
        self._preparing_message_label = None

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

        provider = Gtk.CssProvider()
        provider.load_from_string(_APP_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

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
        root_overlay.set_child(root)
        root_overlay.add_overlay(logo)
        window.set_child(root_overlay)

        # A kiosk: no chrome, no window management, the protein as large as
        # the glass allows.
        window.fullscreen()
        self._connect_visitor_input(window)
        window.present()

        self.viewer.start_animation()
        self._sync_to_state(force=True)

        self.sampler.start()
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
        for panel in (self.pipeline_panel, self.telemetry_panel):
            panel.set_hexpand(False)
            panel.set_vexpand(False)
            side.append(panel)
        return side

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
            self.targets = load_playlist(path)
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

    def _connect_visitor_input(self, window):
        """Any tap, click or keypress counts as a visitor touch.

        Which of those a venue actually has is a booth-setup decision (the
        plan leaves touchscreen hardware out of this phase on purpose), so
        all three are wired to the same place. The gesture sits on the
        window in the default bubble phase, so a tap on a gallery card is
        the card's first -- a touch during the gallery only resets the idle
        clock anyway, so it is harmless either way.
        """
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_args: self._on_touch())
        window.add_controller(click)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda *_args: self._on_touch() or False)
        window.add_controller(keys)

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
        self.states.on_touch()
        self._sync_to_state()

    def _on_pick(self, target_id):
        """A visitor picked a target off the gallery.

        The pick drives the state machine and closes the gallery. It does
        NOT yet reach the daemon: the socket protocol is one-way
        (runner/server.py broadcasts; there is no client->server message),
        so the daemon's priority queue -- which exists and reserves a
        higher priority for exactly this -- cannot currently be reached
        from here. Logged, and recorded as a known gap in this task's
        report; the booth still shows the visitor the fold that is running.
        """
        log.info("visitor picked %s", target_id)
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
            self.states.tick(self._clock())
            self._sync_to_state()
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

            if kind == "job_start":
                log.info("folding %s (%s residues) on card %s",
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
                # here: it carries per-card telemetry, and this file
                # rendering it would be exactly the coupling ui/telemetry.py
                # exists to prevent -- the panel is fed from an independent
                # tt-smi sampler so that a dead daemon still leaves the
                # silicon visibly breathing. Nothing on the wire feeds that
                # panel.
                log.warning("unhandled event type %r", kind)
        except Exception:
            log.exception("dropping malformed %s event", event.get("type"))
        return False

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
        except Exception:
            log.exception("dropping malformed frame")
        return True


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="tt-bio demo UI")
    parser.add_argument("--socket", default=None,
                        help="runner socket path; omit to show an empty viewer")
    parser.add_argument("--playlist", default=None,
                        help=f"playlist manifest (default: {_DEFAULT_PLAYLIST})")
    args = parser.parse_args(argv)
    return DemoApp(socket_path=args.socket,
                   playlist_path=args.playlist).run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
