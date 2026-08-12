"""GTK application shell for the tt-bio demo."""

import argparse
import logging
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk

from protocol.events import unpack_coords
from ui.client import EventClient, LatestFrame
from ui.geometry import ribbon_from_cif
from ui.viewer import StructureViewer

log = logging.getLogger(__name__)

# Operator-neutral copy for the "preparing" overlay. The `missing` list from
# a not_ready event names real filesystem paths and model/config detail --
# useful to an operator reading the log, meaningless (and a mild information
# leak) to a visitor reading the screen. This string is the only thing that
# may ever reach display_message for that state; it never gets composed from
# `missing` in any way.
_PREPARING_MESSAGE = "Getting the booth ready. Please check back shortly."


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


# CSS for the preparing overlay, in the brand palette from the docs-site
# theme: dark base for the backdrop, accent/teal for the text. Kept as a
# module-level constant (not rebuilt per window) since it never varies.
_PREPARING_CSS = b"""
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
"""


class DemoApp(Gtk.Application):
    def __init__(self, socket_path=None):
        super().__init__(application_id="com.tenstorrent.ttbiodemo")
        self.socket_path = socket_path
        self.viewer = None

        # `display_state`/`missing`/`display_message` are a single plain
        # observable value, deliberately not a state machine -- Task 7 of
        # this plan introduces a real StateMachine with its own "preparing"
        # state, and Task 9 reconciles the two into one source of truth.
        # Until then this stays exactly this simple: something
        # _handle_event sets and (if a window exists) the overlay reads.
        # None means "no opinion yet" -- the app hasn't heard not_ready or
        # job_start. The overlay widgets themselves (self._preparing_*) are
        # created lazily in do_activate, so all of this is fully
        # constructible and testable with no display connection at all.
        self.display_state = None
        self.missing = []
        self.display_message = ""
        self._preparing_box = None
        self._preparing_message_label = None

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

    def do_activate(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("tt-bio")
        window.set_default_size(1280, 800)

        provider = Gtk.CssProvider()
        provider.load_from_data(_PREPARING_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.viewer = StructureViewer()

        # The preparing overlay sits on top of the viewer, not in place of
        # it, and its visibility is driven purely by display_state -- it
        # has no dependency on the viewer ever having held a ribbon or even
        # a single frame of points, so it renders correctly from the very
        # first activate, before any fold (or even any connection) happens.
        overlay = Gtk.Overlay()
        overlay.set_child(self.viewer)
        overlay.add_overlay(self._build_preparing_overlay())
        window.set_child(overlay)
        window.present()

        self.viewer.start_animation()
        self._sync_preparing_overlay()

        if self.socket_path:
            self._start_client()

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

    def _sync_preparing_overlay(self):
        # Safe to call with no window ever built (self._preparing_box is
        # None in every headless test) -- it just does nothing then. This
        # is what "observable without a window" means in practice: setting
        # display_state never requires GTK widgets to exist.
        if self._preparing_box is None:
            return
        is_preparing = self.display_state == "preparing"
        self._preparing_box.set_visible(is_preparing)
        if is_preparing:
            self._preparing_message_label.set_label(self.display_message)

    def _start_client(self):
        self._frames = LatestFrame()
        self._client = EventClient(
            self.socket_path, self._on_event,
            on_state_change=lambda s: GLib.idle_add(self._on_state, s),
        )
        self._client.start()
        # Drain the frame buffer on the main loop at display rate; the client
        # thread must never touch GTK directly.
        GLib.timeout_add(33, self._drain_frames)

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
            if kind == "job_start":
                # The daemon only ever sends job_start once it is actually
                # folding, so receiving one is proof the booth has left
                # whatever not_ready state it was in -- clear that FIRST,
                # before touching self.viewer below. clear_structure() is a
                # real GL call in production and never raises there, but in
                # a headless test self.viewer is None and it does raise;
                # ordering the state clear ahead of it means that this
                # branch's job of ending "preparing" still happens even
                # though the broad except below (correctly) swallows the
                # AttributeError that follows.
                self.display_state = None
                self.missing = []
                self.display_message = ""
                self._sync_preparing_overlay()
                log.info("folding %s (%s residues) on card %s",
                         event.get("target_id"), event.get("n_residues"),
                         event.get("card"))
                self.viewer.clear_structure()
            elif kind == "not_ready":
                # The daemon's preflight or model load hasn't finished.
                # `missing` names exactly what's wrong (e.g. real filesystem
                # paths) -- that detail is exactly what an operator needs
                # and exactly what must never reach the screen (constraint:
                # no raw error text on display). So it goes to the log at a
                # level an operator watching the booth will actually see,
                # and display_message stays a fixed, neutral string that
                # never incorporates `missing` in any way.
                missing = event.get("missing", [])
                self.missing = missing
                self.display_state = "preparing"
                self.display_message = _PREPARING_MESSAGE
                if missing:
                    log.warning("booth not ready: %s", _format_missing(missing))
                else:
                    log.warning("booth not ready (no detail given)")
                self._sync_preparing_overlay()
            elif kind == "stage":
                log.info("stage %s %.0f%%", event.get("stage"),
                         100.0 * event.get("frac", 0.0))
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
        try:
            log.info("runner connection: %s", state)
            self.viewer.connection_state = state
        except Exception:
            log.exception("dropping unrecognized connection state %r", state)
        return False

    def _drain_frames(self):
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
    args = parser.parse_args(argv)
    return DemoApp(socket_path=args.socket).run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
