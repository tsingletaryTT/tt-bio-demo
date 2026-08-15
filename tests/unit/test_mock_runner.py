import pathlib
import socket
import threading
import time

from protocol.events import decode, unpack_coords
from runner.mock import MockRunner, load_stream
from ui.client import EventClient

FIXTURE = pathlib.Path("tests/fixtures/streams/short_fold.jsonl")


def _replay_all(sock_path):
    """Connect to socket server and drain all events until EOF."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(sock_path)
    with client.makefile("rb") as stream:
        return [decode(line) for line in stream]


def test_load_stream_reads_all_events():
    events = load_stream(FIXTURE)
    assert len(events) == 11
    assert events[0]["type"] == "hello"
    assert events[-1]["type"] == "job_done"


def test_load_stream_frames_carry_decodable_coordinates():
    frames = [e for e in load_stream(FIXTURE) if e["type"] == "frame"]
    assert len(frames) == 6
    coords = unpack_coords(frames[0]["coords_b64"])
    assert coords.shape == (12, 3)


def test_runner_serves_every_event_in_order(tmp_path):
    sock_path = str(tmp_path / "runner.sock")
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
    runner.start()
    try:
        received = _replay_all(sock_path)
    finally:
        runner.stop()

    assert [e["type"] for e in received] == [
        "hello", "job_start", "stage",
        "frame", "frame", "frame", "frame", "frame", "frame",
        "stage", "job_done",
    ]


def test_runner_strips_internal_delay_key(tmp_path):
    sock_path = str(tmp_path / "runner.sock")
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
    runner.start()
    try:
        received = _replay_all(sock_path)
    finally:
        runner.stop()

    assert all("_delay_ms" not in e for e in received)


def test_runner_preserves_event_content(tmp_path):
    """Verify that received events match source events (minus _delay_ms)."""
    sock_path = str(tmp_path / "runner.sock")
    source_events = load_stream(FIXTURE)
    runner = MockRunner(sock_path, source_events, speed=100.0)
    runner.start()
    try:
        received = _replay_all(sock_path)
    finally:
        runner.stop()

    # Verify hello event content is preserved
    source_hello = source_events[0]
    received_hello = received[0]
    expected_hello = {k: v for k, v in source_hello.items() if k != "_delay_ms"}
    assert received_hello == expected_hello

    # Verify job_done event content is preserved
    source_done = source_events[-1]
    received_done = received[-1]
    expected_done = {k: v for k, v in source_done.items() if k != "_delay_ms"}
    assert received_done == expected_done


def test_a_pick_sent_to_the_mock_runner_does_not_disturb_the_replay(tmp_path):
    """runner/mock.py is the project's core test instrument and it will never
    read a byte from a client. A UI that now sends picks must not be able to
    wedge it -- otherwise every UI test that replays a fixture is one
    `_on_pick` away from hanging.

    The picks go nowhere: they sit in the socket's receive queue until the
    connection closes. That is fine and is the point -- what must not happen
    is the UI blocking on them, or the replay stalling behind them.
    """
    sock_path = str(tmp_path / "runner.sock")
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=1.0)
    runner.start()

    received = []
    done = threading.Event()

    def on_event(event):
        received.append(event)
        if event["type"] == "job_done":
            done.set()

    client = EventClient(sock_path, on_event)
    client.start()
    try:
        # Tap away for the whole ~0.5 s the fixture takes to replay.
        sent = 0
        while not done.is_set() and sent < 300:
            assert client.send_pick("trpcage") in (True, False)
            sent += 1
            time.sleep(0.002)
        assert sent > 0
        assert done.wait(timeout=10.0), "the replay stalled while picks were sent"
    finally:
        client.stop()
        runner.stop()

    # The stream is served from the beginning to every client, and the client
    # reconnects once the mock runner closes, so only the first replay is
    # this test's business.
    assert [e["type"] for e in received[:11]] == [
        "hello", "job_start", "stage",
        "frame", "frame", "frame", "frame", "frame", "frame",
        "stage", "job_done",
    ]


def test_runner_removes_stale_socket_file(tmp_path):
    sock_path = tmp_path / "runner.sock"
    sock_path.write_text("stale")
    runner = MockRunner(str(sock_path), load_stream(FIXTURE), speed=100.0)
    runner.start()
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock_path))
        client.close()
    finally:
        runner.stop()


def test_stop_waits_for_the_connections_it_is_still_serving(tmp_path):
    """`stop()` joined only the accept loop, so it promised more teardown
    than it delivered: it returned while per-connection replay threads were
    still writing. Harmless in tests, which replay at speed=100; a lingering
    thread at speed=1.0 (docs/followups.md).

    THE TIMING HERE IS THE TEST. A `_serve` thread only checks `_stop`
    between events, so with a 500 ms gap it stays inside `time.sleep` for up
    to half a second after stop() sets the flag. Without the join, stop()
    returns during that window and the thread is demonstrably still alive;
    with it, stop() waits out the sleep. A short delay makes both behaviours
    look identical -- the first version of this test used 200 ms and passed
    against a stop() that joined nothing at all, which is exactly the
    "test that cannot fail" pattern this project keeps catching in itself.
    """
    import time

    events = [{"type": "stage", "stage": "trunk", "frac": f / 10.0,
               "_delay_ms": 500} for f in range(10)]
    runner = MockRunner(str(tmp_path / "sock"), events, speed=1.0)
    runner.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(tmp_path / "sock"))
        # Wait for the thread to be RUNNING, not merely registered: it is
        # tracked before it is started, and a not-yet-started thread reports
        # is_alive() False, which would make the assertion below vacuous.
        deadline = time.monotonic() + 2.0
        serving = None
        while time.monotonic() < deadline:
            with runner._connections_lock:
                if runner._connections and runner._connections[0].is_alive():
                    serving = runner._connections[0]
                    break
            time.sleep(0.01)
        assert serving is not None, "the runner never started serving"

        runner.stop()

        assert not serving.is_alive(), \
            "stop() returned while a connection was still being served"
    finally:
        client.close()
