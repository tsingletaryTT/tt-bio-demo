"""Replay a recorded event stream over a Unix socket.

This is the project's core test instrument: it lets the entire UI be built and
exercised with no Tenstorrent hardware present. Each connecting client gets the
full stream from the beginning.
"""

import json
import os
import pathlib
import socket
import threading
import time

from protocol.events import encode


def load_stream(path):
    """Read a JSONL fixture into a list of event dicts, `_delay_ms` retained."""
    text = pathlib.Path(path).read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class MockRunner:
    """Serve a fixed event stream to any client that connects.

    `speed` divides each event's `_delay_ms`, so tests can replay instantly
    while a human demo replays at true recorded pace.
    """

    def __init__(self, socket_path, events, speed=1.0):
        self.socket_path = socket_path
        self.events = events
        self.speed = speed
        self._server = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        # A leftover socket file from a crashed run would make bind() fail.
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._server is not None:
            self._server.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            for event in self.events:
                if self._stop.is_set():
                    return
                delay_ms = event.get("_delay_ms", 0)
                if delay_ms:
                    time.sleep(delay_ms / 1000.0 / self.speed)
                payload = {k: v for k, v in event.items() if k != "_delay_ms"}
                try:
                    conn.sendall(encode(payload))
                except (BrokenPipeError, ConnectionResetError):
                    return
