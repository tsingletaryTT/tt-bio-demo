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
        # The per-connection replay threads, so stop() can join them. A list
        # plus a lock rather than a set: they are appended from the accept
        # loop and read from whichever thread calls stop().
        self._connections = []
        self._connections_lock = threading.Lock()
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
        """Stop accepting, and wait for the connections already being served.

        The per-connection `_serve` threads are joined as well as the accept
        loop. Joining only the accept loop promised more teardown than it
        delivered: `stop()` returned while replay threads were still writing,
        which is harmless in tests (they replay at `speed=100`) but leaves a
        lingering thread at `speed=1.0` -- and the promise, not the leak, is
        the problem. See docs/followups.md.

        Each join is bounded: a `_serve` thread checks `_stop` between
        events, so it exits within one event's sleep, and a stuck one must
        not wedge a test suite's teardown.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._connections_lock:
            serving = list(self._connections)
            self._connections.clear()
        for thread in serving:
            thread.join(timeout=2.0)
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
            thread = threading.Thread(target=self._serve, args=(conn,),
                                      daemon=True)
            # Tracked so `stop()` can join it. Registered BEFORE the thread
            # starts, so a stop() racing this line still finds it.
            with self._connections_lock:
                self._connections.append(thread)
            thread.start()

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
