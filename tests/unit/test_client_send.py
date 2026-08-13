"""The UI's send direction: a bounded outbox and a sender thread.

Everything in this file protects one property. `EventClient.send` is called
from a GTK callback (`ui/app.py`'s `_on_pick`, Task 17). An exception raised
inside a GLib callback freezes that source for the life of the process -- the
booth stops answering the daemon entirely, with nothing on screen to say why
-- and a `sendall` issued there blocks the whole UI, which then looks alive
and answers nothing. So `send()` may not raise and may not block, for any
reason at all, including "there is no daemon", "the daemon stopped reading"
and "the caller passed nonsense".

The second property is freshness: a pick means "fold this now". A pick queued
while the daemon is down and delivered ninety seconds later, to a visitor who
has walked away, is worse than no pick at all -- so the outbox is bounded, it
keeps the NEWEST picks, and whatever is still in it when a connection is
established is dropped rather than delivered stale.
"""

import os
import socket
import threading
import time

import pytest

from protocol.events import PROTOCOL_VERSION, decode_client_message, encode
from ui.client import OUTBOX_MAX, EventClient


def _hello(version=PROTOCOL_VERSION):
    return {"type": "hello", "version": version, "cards": [0, 1, 2, 3],
            "models": ["protenix-v2"], "preflight": "ok"}


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _record_lines(conn, stop, sink):
    """Frame whole client messages off `conn` into `sink` until EOF or `stop`.

    By hand on a bytes buffer rather than through `conn.makefile()`, for the
    reason runner/server.py's reader was written the same way one task
    earlier. This socket carries a timeout so the thread can notice `stop`,
    and a timeout raised inside a buffered file object leaves that object in
    an inconsistent state -- documented as such -- taking any half-received
    line with it. Measured here, not assumed: a `makefile()`-based version of
    this peer recorded a pick sent within its first 200 ms and then silently
    recorded nothing ever again, once one `readline` had timed out. A test
    peer that quietly stops hearing is the worst kind of harness, because it
    fails the assertion the test cared about and blames the code.
    """
    conn.settimeout(0.2)
    buffered = b""
    while not stop.is_set():
        try:
            chunk = conn.recv(4096)
        except socket.timeout:      # no bytes this pass; not a failure
            continue
        except OSError:
            return
        if not chunk:
            return                  # EOF
        buffered += chunk
        while b"\n" in buffered:
            line, buffered = buffered.split(b"\n", 1)
            if line:
                sink.append(decode_client_message(line))


def _greet(conn, hello):
    """Send the greeting; return False if the client has already gone.

    `EventClient` reports "connected" the moment `connect()` returns, which is
    before a peer's `accept()` has necessarily come back -- so a test that
    stops a client the instant it sees "connected" can legitimately have shut
    this socket down before the first byte of `hello` is written. That is the
    client behaving correctly; an unhandled BrokenPipeError on a test peer's
    accept thread is just noise on top of it.
    """
    try:
        conn.sendall(encode(hello))
        return True
    except OSError:
        conn.close()
        return False


class _Listener:
    """A minimal daemon-shaped peer: greets, then records whole lines."""

    def __init__(self, path, hello=None):
        self.path = str(path)
        self.hello = hello or _hello()
        self.received = []
        # A stopped _Listener leaves the socket FILE behind -- close() does not
        # unlink it -- and bind() onto an existing path fails with EADDRINUSE.
        # test_a_send_failure_does_not_end_the_read_loop replaces a stopped
        # daemon with a fresh one at the same address, which is exactly what a
        # daemon restart looks like from the UI's side, so the unlink belongs
        # here for the same reason MockRunner.start has one.
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._conn_lock = threading.Lock()
        self._conn = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3.0)
        self._server.close()

    def drop_current_connection(self):
        """Shut the accepted connection down, staying open for the next one.

        shutdown(), not close(): `_read` below is parked in `readline` on this
        socket, and closing the descriptor under it makes that call raise on
        every retry instead of ending. A shutdown gives it a clean EOF -- and
        it is also what makes the CLIENT's next write fail with EPIPE, which
        is the point at the one call site.
        """
        with self._conn_lock:
            conn = self._conn
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not _greet(conn, self.hello):
                continue
            with self._conn_lock:
                self._conn = conn
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn):
        with conn:
            _record_lines(conn, self._stop, self.received)


# Enough picks to overrun any socket send buffer these tests could meet, by
# two orders of magnitude, so the sender parks with certainty rather than with
# luck -- and "with luck" is how a threading test ships unable to fail.
#
# Measured on this box rather than assumed, and the arithmetic is worth
# knowing: an AF_UNIX socket's ~208 KB send buffer does NOT hold ~4000 50-byte
# picks. Each `sendall` becomes its own skb and the kernel charges the buffer
# skb->truesize, not payload size, so a small message costs kilobytes of
# budget. **71 picks** filled it (see the probe in the task-5 report), after
# which `sendall` blocks until the peer reads. Five thousand is therefore an
# enormous margin, not a marginal one.
_MORE_PICKS_THAN_A_SOCKET_BUFFER_HOLDS = 5_000


def _flood_with_picks(client, count=_MORE_PICKS_THAN_A_SOCKET_BUFFER_HOLDS,
                      give_up_after=3.0):
    """Send `count` picks; return (how many were sent, how long it took).

    The deadline is not decoration. Against an implementation that writes from
    `send()` -- the mutation these floods exist to catch -- every call after
    the socket buffer fills blocks for the socket's full timeout, so a plain
    `for` loop of five thousand would sit here for hours instead of failing.
    A test that cannot fail promptly is barely better than one that cannot
    fail: it stops being run.
    """
    started = time.monotonic()
    sent = 0
    for n in range(count):
        client.send_pick(f"t{n}")
        sent += 1
        if time.monotonic() - started > give_up_after:
            break
    return sent, time.monotonic() - started


def _wait_until_the_sender_parks(client, timeout=5.0):
    """Return the outbox depth once it stops draining, or None if it never does.

    "Stops draining" is the observable form of "the sender is blocked inside
    `sendall`", which is the state every test below that concerns blocking
    needs to be in before it asserts anything. Sampled rather than assumed:
    the buffer takes a moment to fill, and a test that looked once, early,
    would call a still-draining outbox parked and prove nothing.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        pending = client.pending_sends
        if pending and pending == last:
            return pending
        last = pending
        time.sleep(0.2)
    return None


class _DeafPeer:
    """A daemon-shaped peer that greets and then reads NOTHING until released.

    A real daemon can stop draining its socket: four folds in flight, a wedged
    worker, a process stopped under a debugger. This reproduces that state --
    the client's picks pile up in the kernel's buffer and then `sendall`
    blocks -- which is the only condition under which "send() does not block"
    means anything at all.

    `chatter=True` keeps sending events while refusing to read, which is what
    a mid-fold daemon does and what keeps the client's READ direction moving
    (its reader checks for shutdown once per line, so a chattering peer lets
    it exit at once instead of sitting out a five-second socket timeout). That
    separation is what lets a test see whether `stop()` waited for the SENDER.
    """

    def __init__(self, path, chatter=False):
        self.path = str(path)
        self.chatter = chatter
        self.received = []
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._released = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def release(self):
        """Start draining. Everything buffered so far arrives in order."""
        self._released.set()

    def stop(self):
        self._stop.set()
        self._released.set()
        self._thread.join(timeout=3.0)
        self._server.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not _greet(conn, _hello()):
                continue
            if self.chatter:
                threading.Thread(target=self._chatter, args=(conn,),
                                 daemon=True).start()
            threading.Thread(target=self._read_when_released, args=(conn,),
                             daemon=True).start()

    def _chatter(self, conn):
        event = encode({"type": "stage", "job_id": "j1",
                        "stage": "diffusion", "frac": 0.5})
        while not self._stop.is_set():
            try:
                conn.sendall(event)
            except OSError:
                return
            time.sleep(0.002)

    def _read_when_released(self, conn):
        while not self._released.is_set():
            if self._stop.is_set():
                return
            time.sleep(0.005)
        _record_lines(conn, self._stop, self.received)


@pytest.fixture
def listener(tmp_path):
    peer = _Listener(tmp_path / "sock")
    peer.start()
    try:
        yield peer
    finally:
        peer.stop()


def _client(path, **kw):
    return EventClient(str(path), on_event=lambda e: None, **kw)


def test_a_pick_reaches_a_listening_daemon(listener):
    client = _client(listener.path)
    client.start()
    try:
        assert _wait(lambda: client.state == "connected")
        assert client.send_pick("trpcage") is True
        assert _wait(lambda: [m["target_id"] for m in listener.received]
                     == ["trpcage"])
    finally:
        client.stop()


def test_a_pick_carries_the_protocol_version(listener):
    client = _client(listener.path)
    client.start()
    try:
        assert _wait(lambda: client.state == "connected")
        client.send_pick("trpcage")
        assert _wait(lambda: listener.received)
        assert listener.received[0]["version"] == PROTOCOL_VERSION
    finally:
        client.stop()


def test_send_before_there_has_ever_been_a_connection_never_raises(tmp_path):
    """_on_pick runs in a GLib callback. An exception there freezes that
    source for the life of the process, and the booth stops answering
    taps -- with nothing on screen to say why."""
    client = _client(tmp_path / "nothing-here.sock")
    client.start()
    try:
        assert client.send_pick("trpcage") in (True, False)   # must not raise
    finally:
        client.stop()


def test_send_does_not_block_the_caller(tmp_path):
    """There is no daemon at this path at all. The GTK main loop must come
    straight back regardless."""
    client = _client(tmp_path / "nothing-here.sock")
    client.start()
    try:
        started = time.monotonic()
        for _ in range(50):
            client.send_pick("trpcage")
        assert time.monotonic() - started < 0.5
    finally:
        client.stop()


def test_send_does_not_block_even_when_the_daemon_stops_reading(tmp_path):
    """The test above cannot fail against the mutation it names.

    "sendall directly from send()" against a socket path with no daemon does
    not block -- there is no connection to write to, so the mutated send()
    finds `None` and returns at once. The mutation is only visible against a
    daemon that is CONNECTED and has stopped draining its socket, which is
    the exact state a daemon in the middle of four folds can be in. Here
    every send after the kernel buffer fills would block in `sendall` for the
    socket's full timeout, once per call -- a frozen booth in front of a
    visitor -- while a queued send returns immediately whatever the peer does.
    """
    peer = _DeafPeer(tmp_path / "sock")
    peer.start()
    try:
        client = _client(peer.path)
        client.start()
        try:
            assert _wait(lambda: client.state == "connected")
            sent, elapsed = _flood_with_picks(client)
            assert elapsed < 2.0, (
                f"{sent} sends took {elapsed:.1f}s -- send() is doing socket "
                f"I/O on the caller's thread, which in production is the GTK "
                f"main loop"
            )
            assert sent == _MORE_PICKS_THAN_A_SOCKET_BUFFER_HOLDS
        finally:
            client.stop()
    finally:
        peer.stop()


def test_picks_made_while_disconnected_do_not_pile_up(tmp_path):
    """A booth left running with the daemon down for an hour must not
    deliver an hour of stale picks the moment it comes back."""
    client = _client(tmp_path / "nothing-here.sock")
    client.start()
    try:
        for n in range(1000):
            client.send_pick(f"t{n}")
        assert client.pending_sends <= OUTBOX_MAX
        assert client.dropped_sends > 0
    finally:
        client.stop()


def test_a_full_outbox_keeps_the_newest_picks(tmp_path):
    """Which end of a full outbox is dropped is the whole design.

    A pick means "fold this now". When the daemon has stalled and the outbox
    fills, the picks worth keeping are the ones the visitor made most
    recently; dropping the new ones to protect a queue of old ones is the
    stale-pick failure with extra steps. The peer here refuses to read until
    released, so the sender parks mid-write with a full outbox behind it --
    the only state in which this choice is observable.
    """
    peer = _DeafPeer(tmp_path / "sock")
    peer.start()
    try:
        client = _client(peer.path)
        client.start()
        try:
            assert _wait(lambda: client.state == "connected")
            sent, _ = _flood_with_picks(client)
            last = sent - 1
            assert client.dropped_sends > 0, (
                "the peer never stopped draining; nothing was ever dropped, "
                "so this run proved nothing about which end is dropped"
            )
            peer.release()
            assert _wait(lambda: peer.received
                         and peer.received[-1]["target_id"] == f"t{last}",
                         timeout=10.0), (
                "the last pick the visitor made never arrived: a full outbox "
                "dropped the newest picks instead of the oldest"
            )
        finally:
            client.stop()
    finally:
        peer.stop()


def test_picks_queued_while_the_daemon_was_down_are_not_delivered_when_it_returns(tmp_path):
    """The bound alone does not make a pick fresh, and this is the half of
    the freshness rule the bound does not cover.

    A visitor taps, the daemon is down, they walk away. Eight picks is a small
    queue, but delivering it the moment the daemon comes back is still a booth
    that starts folding something nobody is watching, with the pipeline panel
    animating for an empty chair. Whatever is still queued when a connection
    is established is stale by definition: it is dropped, and counted.
    """
    path = tmp_path / "sock"
    client = _client(path, reconnect_delay=0.05)
    client.start()
    peer = None
    try:
        for n in range(3):
            assert client.send_pick(f"t{n}") is True
        assert _wait(lambda: client.pending_sends == 3)

        peer = _Listener(path)          # the daemon comes back
        peer.start()
        assert _wait(lambda: client.state == "connected", timeout=10.0)
        assert _wait(lambda: client.dropped_sends >= 3)
        time.sleep(0.3)
        assert peer.received == [], (
            "picks made while the daemon was down were delivered to the "
            "daemon that replaced it"
        )

        # ...and the connection is live, so this is a drop and not a wedge.
        assert client.send_pick("trpcage") is True
        assert _wait(lambda: [m["target_id"] for m in peer.received]
                     == ["trpcage"])
    finally:
        client.stop()
        if peer is not None:
            peer.stop()


def test_a_malformed_message_is_refused_without_raising(tmp_path):
    client = _client(tmp_path / "nothing-here.sock")
    client.start()
    try:
        assert client.send({"type": "job_done", "job_id": "j1"}) is False
        assert client.send({"nonsense": True}) is False
    finally:
        client.stop()


def test_nothing_is_ever_sent_to_a_daemon_we_refuse_to_interpret(tmp_path):
    """The whole point of the incompatible state. A v2 UI whispering picks
    at a v1 daemon that will never answer them is the booth promising a
    capability it does not have."""
    peer = _Listener(tmp_path / "sock", hello=_hello(version=PROTOCOL_VERSION + 1))
    peer.start()
    try:
        client = _client(peer.path)
        client.start()
        try:
            assert _wait(lambda: client.state == "incompatible")
            assert client.send_pick("trpcage") is False
            time.sleep(0.3)
            assert peer.received == []
        finally:
            client.stop()
    finally:
        peer.stop()


def test_the_read_direction_still_works_while_sending(tmp_path):
    """Full duplex, and the regression that matters: a sender thread that
    takes the same lock the reader holds turns every fold into a stall."""
    seen = []
    peer = _Listener(tmp_path / "sock")
    peer.start()
    try:
        # outbox_max is raised for this test alone, and deliberately: at the
        # production OUTBOX_MAX of 8, a 20-pick burst from a tight Python loop
        # legitimately overruns the outbox (the producer is several times
        # faster than a consumer that makes a syscall per message) and some
        # picks are correctly dropped. That is test 5's subject, not this
        # one's -- here the question is only whether both directions move at
        # once, and an outbox large enough to hold the burst is what lets
        # "all 20 arrive" mean "the sender was never starved".
        client = EventClient(peer.path, on_event=seen.append, outbox_max=64)
        client.start()
        try:
            assert _wait(lambda: client.state == "connected")
            for n in range(20):
                client.send_pick(f"t{n}")
            assert _wait(lambda: len(peer.received) == 20)
            assert _wait(lambda: any(e["type"] == "hello" for e in seen))
        finally:
            client.stop()
    finally:
        peer.stop()


def test_a_send_failure_does_not_end_the_read_loop(tmp_path):
    """The daemon restarting mid-pick is ordinary. The client must come back
    connected, not sit in a state where it never reads again."""
    # Why the states list rather than `client.state == "connected"`: the state
    # cannot tell the connection that has just died from the one that replaced
    # it. A reader only discovers a dead peer when its next read returns, so
    # for a moment after the daemon goes away the client still reads
    # "connected" -- and a test that sends on that reading sends into the
    # corpse, has the pick correctly dropped, and fails for a reason that has
    # nothing to do with what it is testing. Counting the transitions names
    # the SECOND connection, which is the one this test is about.
    states = []
    peer = _Listener(tmp_path / "sock")
    peer.start()
    client = EventClient(peer.path, on_event=lambda e: None,
                         on_state_change=states.append, reconnect_delay=0.05)
    client.start()
    try:
        assert _wait(lambda: client.state == "connected")
        peer.stop()
        for _ in range(10):
            client.send_pick("trpcage")
        peer2 = _Listener(tmp_path / "sock")
        peer2.start()
        try:
            assert _wait(lambda: states.count("connected") == 2, timeout=10.0)
            assert client.send_pick("hemoglobin") is True
            assert _wait(lambda: any(m["target_id"] == "hemoglobin"
                                     for m in peer2.received))
        finally:
            peer2.stop()
    finally:
        client.stop()


def test_a_write_failure_does_not_kill_the_sender_thread(tmp_path):
    """The test above cannot fail against the mutation it names, so this one
    drives the schedule that makes the mutation visible.

    "the sender exits on the first write failure" only shows up if a write
    actually fails, and in the test above it usually never does: by the time
    the ten picks are made, the client's reader has already seen EOF, ended
    the session and retired the connection, so the sender is parked with
    nothing to write to and no failure ever happens. The race decides whether
    the test tests anything.

    Here the reader is parked INSIDE its own callback -- exactly where a real
    UI's GLib marshaling puts it -- so it cannot notice the peer going away,
    the connection stays published, and the sender writes into a socket whose
    peer is gone. `dropped_sends` is checked between the two picks so the
    failure is proven to have happened rather than hoped for; the second pick
    is what a sender that died on the first one can no longer carry.
    """
    in_callback = threading.Event()
    release_reader = threading.Event()
    states = []

    def on_event(event):
        in_callback.set()
        release_reader.wait(15.0)

    peer = _Listener(tmp_path / "sock")
    peer.start()
    client = EventClient(peer.path, on_event=on_event,
                         on_state_change=states.append, reconnect_delay=0.05)
    client.start()
    try:
        assert in_callback.wait(timeout=5.0), "the reader never reached hello"
        peer.drop_current_connection()

        client.send_pick("trpcage")
        assert _wait(lambda: client.dropped_sends == 1), (
            "the write into the dead connection never failed, so this run "
            "never exercised the sender's failure path"
        )
        client.send_pick("trpcage")
        assert _wait(lambda: client.dropped_sends == 2), (
            "the sender thread died on the first write failure: the second "
            "pick was never attempted"
        )

        # And the reconnect the daemon-restart case depends on: once the
        # reader is released it sees EOF, the loop reconnects, and the sender
        # -- still running -- carries the next pick. The SECOND "connected"
        # is what is waited for, not the state, for the reason spelled out in
        # test_a_send_failure_does_not_end_the_read_loop.
        release_reader.set()
        assert _wait(lambda: states.count("connected") == 2, timeout=10.0)
        assert client.send_pick("hemoglobin") is True
        assert _wait(lambda: any(m["target_id"] == "hemoglobin"
                                 for m in peer.received))
    finally:
        release_reader.set()
        client.stop()
        peer.stop()


def test_stop_leaves_no_sender_thread_running(listener):
    client = _client(listener.path)
    before = {t.name for t in threading.enumerate()}
    client.start()
    assert _wait(lambda: client.state == "connected")
    client.stop()
    assert _wait(lambda: not [t for t in threading.enumerate()
                              if t.name not in before and t.is_alive()])


def test_stop_returns_only_after_a_parked_sender_thread_has_exited(tmp_path):
    """The test above cannot fail against the mutation it names.

    `_wait` gives the sender five seconds to notice shutdown, and an idle
    sender notices in microseconds -- so with the join deleted it is still
    gone long before the poll expires, and the assertion holds for a reason
    that has nothing to do with joining. (This is the same defect that shipped
    in `test_stop_leaves_no_reader_thread_running` one task earlier.)

    So the schedule is driven instead of raced. The peer refuses to read,
    which parks the sender inside `sendall` for the socket's full timeout, and
    it chatters, which keeps the READER moving so it exits the instant
    `stop()` is called. A `stop()` that joins the sender returns after the
    sender is gone; one that does not returns while the sender is still inside
    a write, with several seconds of its park left to run.
    """
    peer = _DeafPeer(tmp_path / "sock", chatter=True)
    peer.start()
    try:
        client = _client(peer.path,
                         outbox_max=_MORE_PICKS_THAN_A_SOCKET_BUFFER_HOLDS)
        client.start()
        try:
            assert _wait(lambda: client.state == "connected")
            _flood_with_picks(client)
            # Parked, not merely slow: the outbox stops draining entirely
            # because the socket will take no more bytes.
            assert _wait_until_the_sender_parks(client), (
                "the outbox never stopped draining, so the sender is not "
                "parked in a write and this run proves nothing"
            )
            client.stop()
            assert not client._sender_thread.is_alive(), (
                "stop() returned while the sender thread was still inside a "
                "write"
            )
        finally:
            client.stop()
    finally:
        peer.stop()


def test_stop_is_safe_when_start_was_never_called(tmp_path):
    _client(tmp_path / "sock").stop()          # must not raise
