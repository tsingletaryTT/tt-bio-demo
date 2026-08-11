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

        if self.socket_path:
            self._start_client()

    def _start_client(self):
        self._frames = LatestFrame()
        self._client = EventClient(self.socket_path, self._on_event)
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
                        self.viewer.set_blend(1.0)
        except Exception:
            log.exception("dropping malformed %s event", event.get("type"))
        return False

    def _drain_frames(self):
        frame = self._frames.take()
        if frame is None:
            return True
        # unpack_coords raises protocol.events.ProtocolError on truncated
        # base64 or a byte count that isn't a whole number of 3-vectors --
        # decode() validates only that "type" is present and known, so a
        # malformed coords_b64 payload reaches here unguarded. This source
        # is a REPEATING GLib.timeout_add, unlike _handle_event's one-shot
        # idle_add -- and confirmed by direct reproduction (see
        # task-8-report.md), an uncaught exception here doesn't crash the
        # process, it permanently removes this 33ms timeout source from the
        # main loop. Without this guard, one malformed frame anywhere in an
        # otherwise-fine stream freezes the viewer forever on whatever was
        # last successfully drawn -- no crash, no error on screen, nothing
        # to signal that it happened, which is worse than a crash for an
        # unattended booth. Drop the bad frame and keep going; the next one
        # arrives in <=33ms regardless. Catch Exception broadly (not just
        # ProtocolError) since set_points() itself does array reshaping on
        # attacker/wire-shaped data too.
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
