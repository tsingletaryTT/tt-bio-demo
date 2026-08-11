"""Unix-socket event server for the runner daemon.

The production counterpart to runner/mock.py. The mock replays a recorded script
to each client from the beginning; this broadcasts live events to whoever is
connected, so a UI that connects mid-fold gets `hello` and then joins in
progress.

Nothing here may raise into the daemon's fold loop: a UI that disappears is
completely normal (the screen is a separate process that can be restarted), and
must never disturb the compute side.
"""

import logging
import os
import socket
import threading

from protocol.events import encode

log = logging.getLogger(__name__)


class EventServer:
    """Accepts UI clients and broadcasts protocol events to all of them."""

    def __init__(self, socket_path, hello_factory):
        self.socket_path = socket_path
        self._hello_factory = hello_factory
        self._server = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients = []

    @property
    def client_count(self):
        with self._lock:
            return len(self._clients)

    def start(self):
        try:
            os.unlink(self.socket_path)   # a crashed run leaves the file behind
        except FileNotFoundError:
            pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(8)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        with self._lock:
            clients, self._clients = self._clients, []
        for conn in clients:
            try:
                conn.close()
            except OSError:
                pass
        if self._server is not None:
            self._server.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def broadcast(self, event):
        """Send `event` to every connected client. Returns how many received it."""
        payload = encode(event)
        with self._lock:
            clients = list(self._clients)
        delivered, dead = 0, []
        for conn in clients:
            try:
                conn.sendall(payload)
                delivered += 1
            except OSError:
                dead.append(conn)
        if dead:
            with self._lock:
                self._clients = [c for c in self._clients if c not in dead]
            for conn in dead:
                try:
                    conn.close()
                except OSError:
                    pass
            log.info("dropped %d disconnected UI client(s)", len(dead))
        return delivered

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.sendall(encode(self._hello_factory()))
            except Exception:
                log.exception("failed to greet a UI client; dropping it")
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            with self._lock:
                self._clients.append(conn)
            log.info("UI client connected (%d total)", self.client_count)
