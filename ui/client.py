"""Socket client for the runner's event stream.

Deliberately free of GTK imports so it can be tested headlessly. `on_event`
fires on a background thread; GTK callers must marshal to the main loop
themselves (see ui/app.py, which uses GLib.idle_add).
"""

import logging
import socket
import threading

from protocol.events import PROTOCOL_VERSION, ProtocolError, decode

log = logging.getLogger(__name__)


class LatestFrame:
    """A one-slot buffer that keeps only the newest frame.

    Diffusion frames are advisory: if the renderer falls behind, showing the
    most recent coordinates is strictly better than working through a backlog,
    which would make the animation lag further behind with every frame.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self.dropped = 0

    def put(self, event):
        with self._lock:
            if self._frame is not None:
                self.dropped += 1
            self._frame = event

    def take(self):
        with self._lock:
            frame, self._frame = self._frame, None
            return frame


class EventClient:
    """Connects to the runner, decodes events, reconnects when dropped."""

    def __init__(self, socket_path, on_event, on_state_change=None,
                 reconnect_delay=1.0):
        self.socket_path = socket_path
        self.on_event = on_event
        self.on_state_change = on_state_change
        self.reconnect_delay = reconnect_delay
        self.state = "disconnected"
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            # The join timeout must exceed the socket read timeout in
            # _session (5.0s): when the thread is blocked in `for line in
            # stream` with no data pending, it cannot notice `_stop` until
            # that read call returns -- either because data arrives or
            # because the socket timeout fires. A shorter join timeout here
            # would let stop() return before the thread has actually
            # exited, which is a benign no-op in tests (where the mock
            # runner's own .stop() closes the connection and unblocks the
            # read immediately) but not a real guarantee.
            self._thread.join(timeout=6.0)

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            if self.on_state_change is not None:
                self.on_state_change(state)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._session()
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                log.debug("runner unavailable: %s", exc)
            # Check for "incompatible" before touching state: it was just set
            # inside _session(), and calling _set_state("disconnected")
            # unconditionally here would immediately clobber it, defeating
            # the no-retry guard below and causing an infinite reconnect
            # spam against a runner that speaks the wrong protocol version.
            if self.state == "incompatible":
                return
            self._set_state("disconnected")
            self._stop.wait(self.reconnect_delay)

    def _session(self):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(5.0)
        conn.connect(self.socket_path)
        self._set_state("connected")
        with conn, conn.makefile("rb") as stream:
            for line in stream:
                if self._stop.is_set():
                    return
                try:
                    event = decode(line)
                except ProtocolError as exc:
                    log.warning("dropping undecodable line: %s", exc)
                    continue
                if event["type"] == "hello":
                    if event.get("version") != PROTOCOL_VERSION:
                        log.error(
                            "runner speaks protocol v%s, UI speaks v%s; "
                            "refusing to interpret its messages",
                            event.get("version"), PROTOCOL_VERSION,
                        )
                        self._set_state("incompatible")
                        return
                try:
                    self.on_event(event)
                except Exception:
                    # A raising callback (e.g. a bug in a later GTK
                    # marshaling wrapper) must not be mistaken for a
                    # connection failure and must not cost the rest of the
                    # stream: one bad event should neither end the
                    # reconnect loop nor leave `state` wedged at
                    # "connected" with no further callbacks. Catch
                    # Exception, not BaseException, so KeyboardInterrupt/
                    # SystemExit still propagate and can stop the thread.
                    log.exception("on_event callback raised; continuing")
