import pathlib
import socket

from protocol.events import decode, unpack_coords
from runner.mock import MockRunner, load_stream

FIXTURE = pathlib.Path("tests/fixtures/streams/short_fold.jsonl")


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
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(sock_path)
        with client.makefile("rb") as stream:
            received = [decode(line) for line in stream]
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
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(sock_path)
        with client.makefile("rb") as stream:
            received = [decode(line) for line in stream]
    finally:
        runner.stop()

    assert all("_delay_ms" not in e for e in received)


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
