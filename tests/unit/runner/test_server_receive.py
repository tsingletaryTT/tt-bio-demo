"""The read direction of `EventServer`.

`EventServer` has only ever written. This file pins what happens once it also
*reads*, which is the moment the booth daemon starts accepting bytes from a
process it does not control, on a socket a room full of strangers' laptops can
reach. Almost every test here is a variation on one sentence: a bad line must
cost the line, and nothing else. A booth daemon that a malformed message can
kill is worse than one that cannot be picked from at all.

Two implementation traps these tests exist to catch, both of which read as
correct code:

* The socket carries a send timeout (`client_send_timeout`, 1 s in production)
  and that timeout applies to *reads on the same socket*. A reader that treats
  `socket.timeout` as a disconnect drops every silent -- i.e. every normal --
  UI within a second. `test_a_silent_client_is_never_dropped` is that one.
* `conn.makefile()` is the obvious way to read lines and is wrong here for the
  same reason: Python documents a socket file object as unusable after a
  timeout, so the half of a line already buffered is simply lost.
  `test_a_line_split_across_two_writes_still_arrives_whole` is that one.

The fixtures use a 0.05 s timeout rather than the production 1 s so those two
paths are exercised in a fast test instead of only on the booth floor.
"""

import socket
import threading
import time

import pytest

from protocol.events import (
    PROTOCOL_VERSION, ProtocolError, decode, encode_client_message,
    pick_message,
)
from runner.server import CLIENT_LINE_MAX_BYTES, EventServer


def _hello():
    return {"type": "hello", "version": PROTOCOL_VERSION, "cards": [0, 1, 2, 3],
            "models": ["protenix-v2"], "preflight": "ok"}


def _job_done(job_id="j1"):
    return {"type": "job_done", "job_id": job_id, "cif_path": f"/{job_id}.cif",
            "wall_s": 4.4, "mean_plddt": 95.3}


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def server(tmp_path):
    """A server whose received client messages are recorded, with a short
    send timeout so the read-side timeout path is exercised in a fast test
    rather than only in production."""
    received = []
    s = EventServer(str(tmp_path / "sock"), _hello,
                    client_send_timeout=0.05,
                    on_client_message=received.append)
    s.received = received
    s.start()
    try:
        yield s
    finally:
        s.stop()


def _connect(server):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5.0)
    conn.connect(server.socket_path)
    stream = conn.makefile("rb")
    assert decode(stream.readline())["type"] == "hello"
    return conn, stream


def test_a_pick_from_a_client_reaches_the_callback(server):
    conn, _stream = _connect(server)
    conn.sendall(encode_client_message(pick_message("trpcage")))
    assert _wait(lambda: server.received == [pick_message("trpcage")])


def test_a_silent_client_is_never_dropped(server):
    """The default state of a connected UI is "sending nothing". With a
    0.05s socket timeout, a reader that mistakes socket.timeout for EOF
    disconnects every UI within a tenth of a second and the booth goes
    dark while looking perfectly healthy in the log."""
    conn, stream = _connect(server)
    time.sleep(0.4)                      # many read timeouts
    assert server.client_count == 1
    assert server.broadcast(_job_done()) == 1
    assert decode(stream.readline())["type"] == "job_done"


def test_a_malformed_line_costs_the_line_and_not_the_client(server):
    conn, stream = _connect(server)
    conn.sendall(b"not json{\n")
    conn.sendall(encode_client_message(pick_message("trpcage")))
    assert _wait(lambda: len(server.received) == 1)
    assert server.received[0]["target_id"] == "trpcage"
    assert server.client_count == 1
    assert server.broadcast(_job_done()) == 1


def test_an_unknown_message_type_is_ignored_not_acted_on(server):
    conn, _stream = _connect(server)
    conn.sendall(b'{"type":"shutdown","version":2,"target_id":"x"}\n')
    conn.sendall(encode_client_message(pick_message("trpcage")))
    assert _wait(lambda: len(server.received) == 1)
    assert [m["type"] for m in server.received] == ["pick"]


def test_a_message_from_the_wrong_protocol_version_is_ignored(server):
    conn, _stream = _connect(server)
    conn.sendall(b'{"type":"pick","version":1,"target_id":"trpcage"}\n')
    conn.sendall(encode_client_message(pick_message("hemoglobin")))
    assert _wait(lambda: len(server.received) == 1)
    assert server.received[0]["target_id"] == "hemoglobin"


def test_a_line_split_across_two_writes_still_arrives_whole(server):
    """A TCP-like stream splits wherever it likes, and the send-timeout on
    this socket makes a naive file-object reader lose the first half."""
    conn, _stream = _connect(server)
    payload = encode_client_message(pick_message("trpcage"))
    conn.sendall(payload[:9])
    time.sleep(0.2)                      # several read timeouts in between
    conn.sendall(payload[9:])
    assert _wait(lambda: server.received == [pick_message("trpcage")])


def test_two_messages_in_one_write_are_both_delivered(server):
    conn, _stream = _connect(server)
    conn.sendall(encode_client_message(pick_message("trpcage"))
                 + encode_client_message(pick_message("hemoglobin")))
    assert _wait(lambda: len(server.received) == 2)
    assert [m["target_id"] for m in server.received] == ["trpcage", "hemoglobin"]


def test_a_client_that_never_sends_a_newline_is_dropped_not_buffered(server):
    """Otherwise a remote process decides how much memory this daemon
    allocates, and the booth dies of something that looks like a leak."""
    conn, _stream = _connect(server)
    blob = b"x" * 4096
    try:
        for _ in range((CLIENT_LINE_MAX_BYTES // len(blob)) + 4):
            conn.sendall(blob)
    except OSError:
        pass                              # the server closing on us is the point
    assert _wait(lambda: server.client_count == 0)


def test_the_server_still_accepts_clients_after_dropping_a_bad_one(server):
    conn, stream = _connect(server)
    # The stream as well as the socket, and not by accident: `makefile()`
    # takes its own reference to the underlying descriptor, so `conn.close()`
    # on its own marks the socket closed on this side while leaving the fd
    # open -- the server sees no EOF, the client is never dropped, and this
    # test fails against a perfectly correct reader. (It did.)
    stream.close()
    conn.close()
    assert _wait(lambda: server.client_count == 0)
    conn2, stream2 = _connect(server)
    assert server.broadcast(_job_done()) == 1
    assert decode(stream2.readline())["type"] == "job_done"


def test_a_raising_callback_costs_the_message_not_the_reader(tmp_path):
    """on_client_message runs on the reader thread. An exception escaping it
    kills that thread, and that client is deaf for the rest of the day with
    nothing on screen saying so."""
    seen = []

    def explode(message):
        seen.append(message)
        raise RuntimeError("boom")

    s = EventServer(str(tmp_path / "sock"), _hello, client_send_timeout=0.05,
                    on_client_message=explode)
    s.start()
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(5.0)
        conn.connect(s.socket_path)
        stream = conn.makefile("rb")
        assert decode(stream.readline())["type"] == "hello"
        conn.sendall(encode_client_message(pick_message("a")))
        conn.sendall(encode_client_message(pick_message("b")))
        assert _wait(lambda: len(seen) == 2), "the reader died on the first one"
        assert s.client_count == 1
    finally:
        s.stop()


def test_a_server_with_no_callback_still_reads_and_discards(tmp_path):
    """`on_client_message` is optional, and the lines are still *read*.

    A daemon that has not wired a consumer up yet -- which is every daemon
    until Task 9 -- must not be a daemon whose socket silently stops
    draining. The second half of this test is what makes "and discards"
    mean something: a megabyte of perfectly good picks, far more than any
    socket buffer will hold, cannot be written at all unless somebody on the
    other end is reading it, so a server that skips the reader thread when
    there is no callback fails here instead of passing on the strength of
    one small message nobody looked at.
    """
    s = EventServer(str(tmp_path / "sock"), _hello, client_send_timeout=0.05)
    s.start()
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(5.0)
        conn.connect(s.socket_path)
        stream = conn.makefile("rb")
        assert decode(stream.readline())["type"] == "hello"
        conn.sendall(encode_client_message(pick_message("trpcage")))
        time.sleep(0.2)
        assert s.client_count == 1
        assert s.broadcast(_job_done()) == 1
        assert decode(stream.readline())["type"] == "job_done"

        one_pick = encode_client_message(pick_message("trpcage"))
        batch = one_pick * (1024 * 1024 // len(one_pick))
        conn.sendall(batch)               # blocks forever if nobody drains
        time.sleep(0.2)
        assert s.client_count == 1
        assert s.broadcast(_job_done("j2")) == 1
        assert decode(stream.readline())["job_id"] == "j2"
    finally:
        s.stop()


# How big one event in `test_concurrent_broadcasts_never_split_a_line` is.
#
# This number is the whole test. `sendall` is only non-atomic when it has to
# loop, and it only has to loop when the payload does not fit in the socket's
# send buffer in one go (~208 KB by default on this kernel). With the small
# events the rest of this file uses, every `sendall` is a single write syscall,
# two concurrent broadcasts can never interleave, and a version of this test
# built from `_job_done()` stays green against a `broadcast` with no send lock
# at all -- a test for a race that has never been observed to fail, which is
# the failure mode this plan has now shipped five times. 1 MiB is comfortably
# over the buffer, so each broadcast is several partial writes with real gaps
# between them, and the unlocked version fails every run. Measured, not
# assumed: see the task report.
_SPLIT_TEST_EVENT_BYTES = 1024 * 1024


def _fat_job_done(job_id):
    """A `job_done` too big for one write syscall. See the comment above."""
    event = _job_done(job_id)
    event["note"] = "x" * _SPLIT_TEST_EVENT_BYTES
    return event


def test_concurrent_broadcasts_never_split_a_line(tmp_path):
    """Four worker reader threads call broadcast at once from Task 6 on.
    sendall is not atomic: two of them writing to one client socket outside
    the lock interleave partial writes, and the UI sees half a job_done
    glued to half a frame. Every line the client reads must decode.

    Deliberately does not use the `server` fixture: this needs a send timeout
    long enough that a megabyte event is never mistaken for a wedged client
    (the point here is corruption, not the timeout path), and it needs the
    client to be drained by a thread of its own -- a client that is not
    reading while the broadcast is in flight fills its buffer and gets
    dropped for being stuck, which would end the test before it proved
    anything.
    """
    n_threads, per_thread = 4, 5
    total = n_threads * per_thread

    s = EventServer(str(tmp_path / "sock"), _hello, client_send_timeout=5.0)
    s.start()
    conn = None
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(30.0)
        conn.connect(s.socket_path)
        stream = conn.makefile("rb")
        assert decode(stream.readline())["type"] == "hello"

        problems, good = [], []

        def drain():
            # Exactly `total` reads, whether or not the stream is corrupt:
            # interleaving glues payloads together but does not destroy
            # newlines, so a split stream still holds one newline per event
            # and this loop still consumes all of it -- it just decodes fewer
            # of them. Reading more would only park this thread on a line
            # that is never coming.
            for _ in range(total):
                try:
                    line = stream.readline()
                except OSError as exc:            # the socket was shut down
                    problems.append(f"read failed: {exc}")
                    return
                if not line:
                    return
                try:
                    event = decode(line)
                except ProtocolError as exc:
                    problems.append(f"undecodable line ({len(line)} bytes): {exc}")
                    continue
                if event["type"] != "job_done":
                    problems.append(f"unexpected event type {event['type']!r}")
                    continue
                good.append(event["job_id"])

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        senders = [threading.Thread(target=lambda n=n: [
            s.broadcast(_fat_job_done(f"j{n}-{i}")) for i in range(per_thread)])
            for n in range(n_threads)]
        for t in senders:
            t.start()
        for t in senders:
            t.join(timeout=60.0)
            assert not t.is_alive(), "a broadcast thread never finished"

        reader.join(timeout=5.0)
        if reader.is_alive():
            # Parked in readline() waiting for a line that is never coming --
            # which itself means bytes went missing. Wake it so the
            # assertions below report the real problem rather than hanging.
            conn.shutdown(socket.SHUT_RDWR)
            reader.join(timeout=5.0)

        assert problems == []
        # Not redundant with `problems == []`: an empty problem list would
        # also be what a test that never sent anything produces.
        assert len(good) == total, f"{len(good)} of {total} events arrived whole"
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        s.stop()


def test_a_client_that_speaks_while_being_broadcast_to_is_not_disturbed(server):
    conn, stream = _connect(server)
    for i in range(20):
        conn.sendall(encode_client_message(pick_message(f"t{i}")))
        server.broadcast(_job_done(f"j{i}"))
    assert _wait(lambda: len(server.received) == 20)
    for _ in range(20):
        assert decode(stream.readline())["type"] == "job_done"


def test_stop_leaves_no_reader_thread_running(tmp_path):
    """`stop()` must *join* the reader threads, not merely close the sockets
    and hope.

    The schedule here is driven rather than raced, and that is the whole
    reason the test is shaped this way. The obvious version -- connect, call
    `stop()`, check for surviving threads -- proves nothing: `stop()` shuts
    the sockets down, which wakes every reader out of `recv` immediately, and
    a reader with nothing to do then exits within microseconds. Measured
    against a `stop()` whose join was deleted, that version passed ten times
    out of ten. So this one parks a reader thread somewhere it cannot leave
    on its own -- inside `on_client_message`, which runs on the reader thread
    and which Task 9 will fill with real work -- and releases it 0.3 s after
    `stop()` is called. A `stop()` that joins returns after the reader is
    gone; a `stop()` that does not returns while it is still in the callback,
    with the daemon believing it has shut down while one of its threads is
    still running its code and holding its socket.

    Threads are identified by `ident`, not `name`: names are chosen by the
    code under test, and a test that matches on them is testing the naming
    scheme.
    """
    in_callback = threading.Event()
    release = threading.Event()
    stopping = threading.Event()

    def slow_callback(message):
        in_callback.set()
        release.wait(5.0)                 # capped so a broken run cannot hang

    def releaser():
        stopping.wait(10.0)
        time.sleep(0.3)
        release.set()

    s = EventServer(str(tmp_path / "sock"), _hello, client_send_timeout=0.05,
                    on_client_message=slow_callback)
    s.start()
    conn = None
    try:
        # Started -- and counted as pre-existing -- before the reader thread
        # exists, so the releaser itself can never be mistaken for a leftover
        # reader below.
        helper = threading.Thread(target=releaser, daemon=True)
        helper.start()
        before = {t.ident for t in threading.enumerate()}

        conn, _stream = _connect(s)
        conn.sendall(encode_client_message(pick_message("trpcage")))
        assert in_callback.wait(5.0), "the message never reached the callback"

        stopping.set()
        s.stop()

        leftover = [t for t in threading.enumerate()
                    if t.ident not in before and t.is_alive()]
        assert leftover == [], (
            f"stop() returned with {len(leftover)} reader thread(s) still "
            f"running: {[t.name for t in leftover]}")
    finally:
        release.set()
        if conn is not None:
            conn.close()
        s.stop()
