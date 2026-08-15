import os
import pathlib
import socket
import threading
import time

from protocol.events import PROTOCOL_VERSION, encode
from runner.mock import MockRunner, load_stream
from ui.client import EventClient, LatestFrame, LatestFrameByJob

FIXTURE = pathlib.Path("tests/fixtures/streams/short_fold.jsonl")


def _start_fixture_runner(tmp_path):
    """Start a MockRunner replaying the short_fold fixture; return (sock_path, runner)."""
    sock_path = str(tmp_path / "runner.sock")
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
    runner.start()
    return sock_path, runner


def test_latest_frame_keeps_only_newest():
    buf = LatestFrame()
    buf.put({"type": "frame", "step": 1})
    buf.put({"type": "frame", "step": 2})
    buf.put({"type": "frame", "step": 3})
    assert buf.take()["step"] == 3
    assert buf.dropped == 2


def test_latest_frame_empties_after_take():
    buf = LatestFrame()
    buf.put({"type": "frame", "step": 1})
    assert buf.take()["step"] == 1
    assert buf.take() is None


# ---------------------------------------------------------------------------
# LatestFrameByJob: the same contract, once per fold.
#
# Four chips fold at once, so there are up to four live frame streams. Every
# test below distinguishes the streams from each other -- four identical
# frames would make "each job kept its own newest" and "one shared slot was
# written four times" the same assertion, which is exactly the defect this
# class exists to remove.
# ---------------------------------------------------------------------------

def test_each_job_keeps_its_own_newest_frame():
    buf = LatestFrameByJob()
    for job in ("a", "b"):
        for step in (1, 2, 3):
            buf.put({"type": "frame", "job_id": job, "step": step})
    taken = buf.take_all()
    assert {job: event["step"] for job, event in taken.items()} == {"a": 3, "b": 3}


def test_one_folds_frames_never_overwrite_anothers():
    """The single-slot buffer's failure mode, stated directly: fold b is
    faster, so with one slot every cell would show b's coordinates."""
    buf = LatestFrameByJob()
    buf.put({"type": "frame", "job_id": "a", "step": 7})
    for step in range(50):
        buf.put({"type": "frame", "job_id": "b", "step": step})
    assert buf.take_all()["a"]["step"] == 7


def test_taking_empties_the_buffer():
    buf = LatestFrameByJob()
    buf.put({"type": "frame", "job_id": "a", "step": 1})
    assert buf.take_all()["a"]["step"] == 1
    assert buf.take_all() == {}
    assert len(buf) == 0


def test_the_buffer_is_bounded_by_jobs_not_by_frames():
    """An all-day booth folds thousands of jobs; a dict that remembered every
    one is a leak with a screen attached."""
    buf = LatestFrameByJob(max_jobs=4)
    for n in range(200):
        buf.put({"type": "frame", "job_id": f"j{n}", "step": 0})
    assert len(buf) == 4


def test_the_oldest_job_is_the_one_evicted():
    """Oldest by when it last produced a frame -- the only ordering this
    class can honestly know. A fold still streaming must outlive one that
    stopped."""
    buf = LatestFrameByJob(max_jobs=2)
    buf.put({"type": "frame", "job_id": "old", "step": 0})
    buf.put({"type": "frame", "job_id": "still-going", "step": 0})
    buf.put({"type": "frame", "job_id": "still-going", "step": 1})
    buf.put({"type": "frame", "job_id": "new", "step": 0})
    assert set(buf.take_all()) == {"still-going", "new"}


def test_a_frame_with_no_job_id_gets_its_own_slot():
    """It cannot be routed to a cell -- ui/app.py drops it at drain time --
    but dropping it HERE would mean the buffer silently disagreed with its
    caller about what latest-wins applies to."""
    buf = LatestFrameByJob()
    buf.put({"type": "frame", "step": 1})
    buf.put({"type": "frame", "job_id": "a", "step": 2})
    assert set(buf.take_all()) == {None, "a"}


def test_client_receives_all_non_frame_events(tmp_path):
    sock_path, runner = _start_fixture_runner(tmp_path)

    received = []
    done = threading.Event()

    def on_event(event):
        received.append(event)
        if event["type"] == "job_done":
            done.set()

    client = EventClient(sock_path, on_event)
    client.start()
    try:
        assert done.wait(timeout=10.0), "job_done never arrived"
    finally:
        client.stop()
        runner.stop()

    kinds = [e["type"] for e in received]
    assert kinds[0] == "hello"
    assert kinds[-1] == "job_done"
    assert kinds.count("stage") == 2


def test_client_reports_connected_state(tmp_path):
    sock_path, runner = _start_fixture_runner(tmp_path)

    states = []
    connected = threading.Event()

    def on_state(state):
        states.append(state)
        if state == "connected":
            connected.set()

    client = EventClient(sock_path, lambda e: None, on_state_change=on_state)
    client.start()
    try:
        assert connected.wait(timeout=10.0)
    finally:
        client.stop()
        runner.stop()

    assert "connected" in states


def test_client_rejects_incompatible_protocol_version(tmp_path):
    sock_path = str(tmp_path / "runner.sock")
    bad_hello = {"type": "hello", "version": PROTOCOL_VERSION + 99,
                 "cards": [], "models": [], "preflight": "ok"}
    runner = MockRunner(sock_path, [bad_hello], speed=100.0)
    runner.start()

    states = []
    incompatible = threading.Event()

    def on_state(state):
        states.append(state)
        if state == "incompatible":
            incompatible.set()

    client = EventClient(sock_path, lambda e: None, on_state_change=on_state)
    client.start()
    try:
        assert incompatible.wait(timeout=10.0), "version mismatch not detected"
    finally:
        client.stop()
        runner.stop()


def test_client_survives_absent_socket_and_connects_when_it_appears(tmp_path):
    sock_path = str(tmp_path / "runner.sock")

    connected = threading.Event()
    client = EventClient(
        sock_path, lambda e: None,
        on_state_change=lambda s: connected.set() if s == "connected" else None,
        reconnect_delay=0.1,
    )
    client.start()
    try:
        time.sleep(0.3)  # no server yet; the client must not crash
        runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
        runner.start()
        assert connected.wait(timeout=10.0), "did not reconnect once server appeared"
    finally:
        client.stop()
        runner.stop()


def test_client_survives_on_event_exception_and_keeps_delivering(tmp_path):
    """A raising on_event callback (e.g. a bug in the caller's GTK marshaling
    wrapper) must not end the reconnect loop, must not be mistaken for a
    dropped connection, and must not stop the rest of the stream from being
    delivered.
    """
    sock_path, runner = _start_fixture_runner(tmp_path)

    received = []
    states = []
    done = threading.Event()

    def on_event(event):
        if event["type"] == "job_start":
            raise RuntimeError("boom: simulated bug in a downstream callback")
        received.append(event)
        if event["type"] == "job_done":
            done.set()

    client = EventClient(sock_path, on_event, on_state_change=states.append)
    client.start()
    try:
        assert done.wait(timeout=10.0), "job_done never arrived after on_event raised"
    finally:
        client.stop()
        runner.stop()

    kinds = [e["type"] for e in received]
    assert "job_start" not in kinds  # this event's callback raised, so it never got appended
    assert kinds[0] == "hello"
    assert kinds[-1] == "job_done"
    # The exception must not itself be mistaken for a dropped connection: no
    # reconnect should have been triggered by it. (The stream legitimately
    # ends and disconnects once the mock runner finishes sending job_done,
    # so "disconnected" may appear once at the tail -- that's a real
    # end-of-stream, not the bug this test guards against. A second
    # "connected" would mean the exception was wrongly treated as a
    # connection failure and caused a reconnect.)
    assert states.count("connected") == 1, (
        "a raising on_event callback must not be mistaken for a dropped "
        "connection and trigger a reconnect"
    )


def test_stop_joins_thread_blocked_on_a_silent_connection(tmp_path):
    """stop() must actually wait for the background thread to exit, even
    when that thread is blocked inside a socket read with no data pending --
    not only when the peer happens to close the connection right away. A
    real, still-computing runner can go quiet between events for longer than
    a hasty join timeout, and this is the state that made the previous
    join(timeout=2.0) look correct only because every other test's
    runner.stop() closed the connection and unblocked the read immediately.

    Synchronization note: we wait for the *hello event* (delivered through
    on_event), not just the "connected" state, before calling stop(). The
    "connected" state fires before the first line is even read, so waiting
    on it alone races with the background thread's per-line _stop check --
    stop() could land before the thread ever attempts the second, blocking
    read, and the test would pass without exercising that path at all
    (observed directly: flaky between ~0s and ~5s across repeated runs).
    Waiting for the hello event proves the thread has moved past the first
    line and is now blocked on the second read, which is the state this
    test exists to cover.
    """
    sock_path = str(tmp_path / "runner.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)

    accepted = threading.Event()
    release_server = threading.Event()

    def serve_then_go_silent():
        conn, _ = server.accept()
        hello = {"type": "hello", "version": PROTOCOL_VERSION,
                 "cards": [], "models": [], "preflight": "ok"}
        conn.sendall(encode(hello))
        accepted.set()
        release_server.wait(15.0)  # hold the connection open, sending nothing
        conn.close()

    server_thread = threading.Thread(target=serve_then_go_silent, daemon=True)
    server_thread.start()

    hello_received = threading.Event()
    client = EventClient(sock_path, lambda e: hello_received.set())
    client.start()
    try:
        assert accepted.wait(timeout=5.0), "server never accepted a connection"
        assert hello_received.wait(timeout=5.0), "client never processed hello"
        client.stop()
        assert not client._thread.is_alive(), (
            "stop() returned before the background thread actually exited"
        )
    finally:
        client.stop()
        release_server.set()
        server_thread.join(timeout=2.0)
        server.close()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


# ── the timeout relationship that used to be prose ──────────────────────────

def test_every_join_outlasts_the_socket_timeout():
    """A worker thread blocked in a socket call cannot notice `_stop` until
    that call returns, and it returns when the socket timeout fires. So every
    join in `stop()` must outlast it, or `stop()` returns while a thread is
    still inside a read or a write.

    That relationship used to be PROSE -- 5.0 and 6.0 written out separately
    at three call sites, with comments explaining that one had to exceed the
    other -- so an isolated edit to either silently reintroduced the bug the
    pairing was written to fix (docs/followups.md). It is derived now, and
    this is what says so.

    Mutation: setting either join constant below SOCKET_TIMEOUT_S. Red.
    """
    from ui.client import (READER_JOIN_TIMEOUT_S, SENDER_JOIN_TIMEOUT_S,
                           SOCKET_TIMEOUT_S)

    assert READER_JOIN_TIMEOUT_S > SOCKET_TIMEOUT_S
    assert SENDER_JOIN_TIMEOUT_S > SOCKET_TIMEOUT_S


def test_the_client_uses_those_constants_rather_than_its_own_numbers():
    """The constants are only worth having if the code reads them. Catches a
    literal creeping back in beside them -- which is how this started.
    """
    import inspect

    from ui import client as client_module

    source = inspect.getsource(client_module.EventClient)
    assert "timeout=6.0" not in source and "timeout=8.0" not in source, \
        "a join timeout is hardcoded again instead of derived"
    assert "settimeout(5.0)" not in source, \
        "the socket timeout is hardcoded again instead of named"
