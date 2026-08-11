"""GTK application shell for the tt-bio demo."""

import argparse
import logging
import sys

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from protocol.events import unpack_coords
from ui.client import EventClient, LatestFrame
from ui.geometry import GeometryError, ribbon_from_cif
from ui.viewer import StructureViewer

log = logging.getLogger(__name__)


class DemoApp(Gtk.Application):
    def __init__(self, socket_path=None):
        super().__init__(application_id="com.tenstorrent.ttbiodemo")
        self.socket_path = socket_path
        self.viewer = None

    def do_activate(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("tt-bio")
        window.set_default_size(1280, 800)

        self.viewer = StructureViewer()
        window.set_child(self.viewer)
        window.present()

        self.viewer.start_animation()

        if self.socket_path:
            self._start_client()

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
                log.info("folding %s (%s residues) on card %s",
                         event.get("target_id"), event.get("n_residues"),
                         event.get("card"))
                self.viewer.clear_structure()
            elif kind == "stage":
                log.info("stage %s %.0f%%", event.get("stage"),
                         100.0 * event.get("frac", 0.0))
            elif kind == "job_done":
                log.info("done in %.2fs", event.get("wall_s", 0.0))
                cif_path = event.get("cif_path")
                if cif_path:
                    try:
                        verts, norms, colors, idx = ribbon_from_cif(cif_path)
                    except GeometryError:
                        # Leave whatever's on screen (the last diffusion
                        # frame) exactly as it is and just log -- never a
                        # stack trace on screen, never a crash. set_ribbon
                        # and set_blend below are only reached on success,
                        # so a bad CIF simply forfeits the ribbon reveal for
                        # this job instead of corrupting the current view.
                        log.exception("could not build ribbon for %s", cif_path)
                    else:
                        self.viewer.set_ribbon(verts, norms, colors, idx)
                        self.viewer.begin_crossfade()
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
