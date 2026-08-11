import pathlib
import threading
import time

from protocol.events import PROTOCOL_VERSION
from runner.mock import MockRunner, load_stream
from ui.client import EventClient, LatestFrame

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
