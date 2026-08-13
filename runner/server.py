"""Unix-socket event server for the runner daemon.

The production counterpart to runner/mock.py. The mock replays a recorded script
to each client from the beginning; this broadcasts live events to whoever is
connected, so a UI that connects mid-fold gets `hello` and then joins in
progress.

Nothing here may raise into the daemon's fold loop: a UI that disappears is
completely normal (the screen is a separate process that can be restarted), and
must never disturb the compute side.

Since protocol v2 the socket is two-way: each accepted client also gets a reader
thread that decodes client->server messages (`pick`) and hands them to
`on_client_message`. That direction reads bytes from a process this daemon does
not control, over a socket a conference booth exposes to whatever connects to
it, so the governing rule for everything below `_reader_loop` is that a bad line
costs the line and nothing else. The client stays connected; the daemon keeps
folding.
"""

import itertools
import logging
import os
import socket
import threading
import time

from protocol.events import ProtocolError, decode_client_message, encode

log = logging.getLogger(__name__)

# The most buffered bytes one client may accumulate without a newline before it
# is dropped. Without a limit, a remote process -- a buggy UI, or a laptop in
# the room -- decides how much memory this daemon allocates simply by never
# sending `\n`, and the booth dies of what looks like a leak. 64 KiB is three
# orders of magnitude more than the only message this protocol has (a `pick` is
# under 100 bytes, and `MAX_TARGET_ID_LEN` bounds its one variable field), so
# nothing legitimate can approach it.
CLIENT_LINE_MAX_BYTES = 64 * 1024

# How many bytes to ask the kernel for per `recv`. Sized to hold many whole
# `pick` lines at once so a burst is one syscall, not one per message.
_READ_CHUNK_BYTES = 4096

# Minimum seconds between "a client sent something undecodable" log lines, per
# client. A client stuck in a loop sending garbage would otherwise write one
# line per bad message into `daemon.log` at whatever rate it can manage -- the
# same log root the janitor (`prune_log_root`) is trying to hold under a
# budget, and which the budget does not cover. The suppressed count is carried
# into the next line, so the log still says how bad it got.
_BAD_LINE_LOG_INTERVAL_S = 5.0


class EventServer:
    """Accepts UI clients, broadcasts events to them, and reads their messages.

    `on_client_message`, if given, is called with each successfully decoded
    client->server message, **on that client's reader thread**. It is optional
    on purpose: a daemon that has not wired a consumer up yet still reads and
    discards its clients' lines rather than letting them back up, so the read
    side is never the thing that breaks first.
    """

    def __init__(self, socket_path, hello_factory, client_send_timeout=1.0,
                 on_client_message=None):
        self.socket_path = socket_path
        self._hello_factory = hello_factory
        # Bounds how long a single send to one client may block the caller
        # (the accept loop for `hello`, `broadcast()` for the fold loop). See
        # `_accept_loop` for the reasoning behind the default. Overridable so
        # tests can exercise the timeout path without a slow test.
        #
        # It also bounds how long a reader thread sits in `recv` before coming
        # up for air, because a socket has ONE timeout covering both
        # directions -- see `_reader_loop`, where that is the single most
        # misread thing in this file.
        self._client_send_timeout = client_send_timeout
        self._on_client_message = on_client_message
        self._server = None
        self._thread = None
        self._stop = threading.Event()
        # Guards `_clients` and `_readers` only. Deliberately NOT the lock
        # held across a `sendall` (see `_send_lock`): a client that has stopped
        # draining its socket blocks a send for up to `client_send_timeout`,
        # and if that were this lock, it would also block `client_count`, the
        # accept loop registering a new client, and every reader thread trying
        # to drop a disconnected one.
        self._lock = threading.Lock()
        # Serializes the whole `sendall` loop in `broadcast`. `sendall` is not
        # atomic -- a payload larger than the socket's send buffer becomes
        # several partial writes -- so two threads broadcasting at once to one
        # client interleave their bytes and split a JSON line down the middle.
        # The UI then sees half a `job_done` glued to half a `frame`, which
        # decodes as garbage. That was unreachable while the single fold loop
        # was the only caller; as of the multi-chip pool, four worker reader
        # threads call `broadcast` concurrently and it happens routinely.
        # Lock order, where both are taken: `_send_lock` first, then `_lock`.
        self._send_lock = threading.Lock()
        self._clients = []
        self._readers = []
        self._reader_seq = itertools.count(1)

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
        """Shut the socket down and return only once nothing is still running.

        The join at the end is the load-bearing part. Reader threads are
        daemon threads, so the *process* can exit without them; but `stop()`
        is also called by tests and by the daemon's own restart paths, and a
        `stop()` that returned while readers were still appending to
        `_clients` and logging would mean a "stopped" server that is still
        doing things. Bounded, so a wedged reader delays shutdown by a known
        amount instead of hanging it.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        with self._lock:
            clients, self._clients = self._clients, []
            readers, self._readers = self._readers, []
        for conn in clients:
            # shutdown() before close(): a reader thread blocked in recv() is
            # NOT reliably woken by another thread closing the descriptor, but
            # a shutdown makes that recv return EOF at once. Without it every
            # reader sleeps out the rest of its `client_send_timeout` before
            # noticing, and stop() waits for the slowest of them.
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass                      # already dead; close() still applies
            try:
                conn.close()
            except OSError:
                pass
        # One deadline for all of them, not one timeout each: shutdown time
        # must not scale with the number of connected screens.
        deadline = time.monotonic() + max(2.0, self._client_send_timeout * 2.0)
        for reader in readers:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = [r.name for r in readers if r.is_alive()]
        if alive:
            log.warning("reader thread(s) still running after stop(): %s", alive)
        if self._server is not None:
            self._server.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def broadcast(self, event):
        """Send `event` to every connected client. Returns how many received it.

        Never raises into the caller (the daemon's fold loop): a malformed
        `event` is a bug in the caller, not a client problem, and is logged
        and treated as reaching nobody rather than propagating. A client that
        cannot keep up (see `_accept_loop` for the send timeout) is dropped
        exactly like one that has disconnected outright -- diffusion frames
        are advisory, so a stuck screen is not worth stalling compute for.

        Callable from any number of threads at once (the multi-chip pool calls
        it from one reader thread per card): `_send_lock` is held across the
        whole send loop, so no two writers can interleave partial `sendall`s
        on one client socket and split a JSON line in half.
        """
        try:
            payload = encode(event)
        except Exception:
            log.exception("dropping malformed broadcast event: %r", event)
            return 0
        with self._send_lock:
            with self._lock:
                clients = list(self._clients)
            delivered, dead = 0, []
            for conn in clients:
                try:
                    conn.sendall(payload)
                    delivered += 1
                except OSError:
                    # Covers both a closed/reset peer and `socket.timeout` (a
                    # subclass of OSError) from a peer that stopped reading --
                    # see the send timeout set in `_accept_loop`.
                    dead.append(conn)
        # Outside `_send_lock`: dropping takes `_lock` and logs, neither of
        # which needs to hold up another thread's broadcast.
        for conn in dead:
            self._drop_client(conn, "unresponsive or disconnected")
        return delivered

    def _drop_client(self, conn, reason):
        """Deregister and close one client. Safe to call twice on the same one.

        Both directions end here -- a failed send in `broadcast`, and EOF, a
        read error or an over-long line in `_reader_loop` -- so there is one
        answer to "is this socket still in `_clients`", and a dropped client
        looks the same in the log whichever side noticed first.
        """
        with self._lock:
            removed = conn in self._clients
            if removed:
                self._clients.remove(conn)
        try:
            conn.close()
        except OSError:
            pass
        if removed:
            log.info("dropped a UI client (%s); %d remain",
                     reason, self.client_count)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            # Bound how long a slow or wedged client can block a send. A UI
            # process that is suspended, deadlocked, or simply not draining
            # its socket buffer would otherwise let `sendall` block
            # indefinitely -- both here (freezing the accept loop, so no
            # *other* client could connect either) and in `broadcast()`
            # (freezing the daemon's fold loop, so compute would stop
            # because a screen stopped reading). 1 second is generous enough
            # to absorb a scheduling hiccup but short enough that the one-time
            # cost of discovering a stuck client is negligible against a fold
            # that runs for seconds to minutes; once dropped, later
            # broadcasts no longer pay the cost at all.
            conn.settimeout(self._client_send_timeout)
            # Outside `_send_lock` deliberately, and safely: this client is
            # not in `_clients` yet, so no `broadcast` can be writing to this
            # socket at the same time and interleave with the greeting.
            # Taking the lock here would instead let one wedged newcomer's
            # greeting hold up every broadcast for `client_send_timeout`.
            try:
                conn.sendall(encode(self._hello_factory()))
            except Exception:
                log.exception("failed to greet a UI client; dropping it")
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            # Registered and only then read from. A reader started before the
            # greeting could hand a `pick` to the daemon for a client that is
            # about to be dropped for failing to be greeted, and one started
            # before registration could try to drop a client that is not in
            # `_clients` yet.
            reader = threading.Thread(
                target=self._reader_loop, args=(conn,), daemon=True,
                name=f"eventserver-reader-{next(self._reader_seq)}")
            with self._lock:
                self._clients.append(conn)
                # Finished readers are pruned here rather than accumulated:
                # `_readers` exists so `stop()` can join what is running, and
                # a booth whose UI reconnects every few seconds for eight
                # hours would otherwise leave tens of thousands of dead
                # Thread objects behind it by closing time.
                self._readers = [r for r in self._readers if r.is_alive()]
                self._readers.append(reader)
            reader.start()
            log.info("UI client connected (%d total)", self.client_count)

    # -- the read direction ------------------------------------------------

    def _reader_loop(self, conn):
        """Decode client->server messages from one client until it goes away.

        Framing is done by hand, on an explicit `bytes` buffer, and NOT with
        `conn.makefile()`. That is not a style preference:

        * This socket has a timeout (`client_send_timeout`), set so a wedged
          UI cannot block a send forever, and a socket timeout applies to
          BOTH directions. A UI that is doing nothing -- the normal state of
          a UI, most of the time -- therefore produces a `socket.timeout` on
          this read every `client_send_timeout` seconds. It means "no bytes
          this pass", never "the client is gone": a reader that treats it as
          a disconnect closes every screen in the booth within a second of it
          connecting, while the log says only that a client disconnected.
        * Python documents a socket file object as being left in an unusable
          state after a timeout, and whatever it had buffered of a
          half-received line is gone with it. A `pick` split across two
          writes -- which a stream is free to do whenever it likes -- would
          arrive as two undecodable fragments.

        So: `recv` into `buf`, split on `\\n`, keep the remainder for next
        time, and bound `buf` so a client that never sends a newline cannot
        choose how much memory this daemon allocates.
        """
        buf = b""
        bad_lines = _BadLineLog()
        while not self._stop.is_set():
            try:
                chunk = conn.recv(_READ_CHUNK_BYTES)
            except socket.timeout:
                continue                  # a silent client is a normal client
            except OSError as exc:
                # `socket.timeout` is itself an OSError, so its clause above
                # must stay first. What is left here is a real read failure
                # (reset by peer, closed under us by `stop()`), which is the
                # same event as a failed send: drop this client, keep going.
                self._drop_client(conn, f"read failed: {exc}")
                return
            if not chunk:
                self._drop_client(conn, "disconnected")
                return
            buf += chunk
            while True:
                newline = buf.find(b"\n")
                if newline < 0:
                    break
                line, buf = buf[:newline], buf[newline + 1:]
                if line.strip():
                    self._handle_client_line(line, bad_lines)
            # Checked after draining every complete line, so a legitimate
            # burst of messages larger than the limit is fine -- only an
            # unterminated one counts against it.
            if len(buf) > CLIENT_LINE_MAX_BYTES:
                self._drop_client(
                    conn, f"sent {len(buf)} bytes with no newline "
                          f"(limit {CLIENT_LINE_MAX_BYTES})")
                return

    def _handle_client_line(self, line, bad_lines):
        """Decode one line and hand it to `on_client_message`.

        Everything that can go wrong here costs this one line. A
        `ProtocolError` -- malformed JSON, an unknown type, a version this
        build does not speak, an absurd `target_id` -- is logged and dropped
        with the client left connected: a visitor whose UI sent one bad line
        must not lose the screen. And the callback is the daemon's code
        running on this thread, so an exception escaping it would kill the
        reader and leave that client silently deaf for the rest of the day.
        """
        try:
            message = decode_client_message(line)
        except ProtocolError as exc:
            bad_lines.record(exc)
            return
        if self._on_client_message is None:
            return                        # read and discarded, by design
        try:
            self._on_client_message(message)
        except Exception:
            log.exception("on_client_message raised on %r; dropping the message",
                          message.get("type"))


class _BadLineLog:
    """Rate limiter for one client's "that line was garbage" log messages.

    A client looping on a malformed message can produce them as fast as the
    socket allows, into the same log root `prune_log_root` is trying to keep
    under a budget -- and `daemon.log` is not what that budget covers. One
    line per `_BAD_LINE_LOG_INTERVAL_S` per client, carrying the count of the
    ones it stands in for, keeps the signal without the flood.
    """

    def __init__(self, interval_s=_BAD_LINE_LOG_INTERVAL_S, clock=time.monotonic):
        self._interval_s = interval_s
        self._clock = clock
        self._last = None
        self._suppressed = 0

    def record(self, exc):
        now = self._clock()
        if self._last is not None and now - self._last < self._interval_s:
            self._suppressed += 1
            return
        if self._suppressed:
            log.warning("ignored a client message: %s (and %d more like it "
                        "in the last %.0fs)", exc, self._suppressed,
                        now - self._last)
        else:
            log.warning("ignored a client message: %s", exc)
        self._last = now
        self._suppressed = 0
