"""GTK application shell for the tt-bio demo."""

import argparse
import logging
import sys

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from protocol.events import unpack_coords
from ui.client import EventClient, LatestFrame
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
        return False

    def _drain_frames(self):
        frame = self._frames.take()
        if frame is not None:
            self.viewer.set_points(unpack_coords(frame["coords_b64"]))
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
