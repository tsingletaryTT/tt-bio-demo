import socket
import threading

from protocol.events import PROTOCOL_VERSION, decode
from runner.server import EventServer


def _hello():
    return {"type": "hello", "version": PROTOCOL_VERSION, "cards": [0],
            "models": ["protenix-v2"], "preflight": "ok"}


def _connect(path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5.0)
    client.connect(path)
    return client


def _wait_for_clients(server, n, timeout=5.0):
    deadline = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if server.client_count >= n:
            return True
        deadline.wait(0.02)
    return False


def test_a_connecting_client_receives_hello_first(tmp_path):
    server = EventServer(str(tmp_path / "r.sock"), _hello)
    server.start()
    try:
        client = _connect(server.socket_path)
        with client.makefile("rb") as stream:
            first = decode(stream.readline())
        assert first["type"] == "hello"
        assert first["version"] == PROTOCOL_VERSION
    finally:
        client.close()
        server.stop()


def test_hello_is_built_fresh_for_each_connection(tmp_path):
    calls = []

    def counting_hello():
        calls.append(1)
        return _hello()

    server = EventServer(str(tmp_path / "r.sock"), counting_hello)
    server.start()
    try:
        for _ in range(2):
            client = _connect(server.socket_path)
            with client.makefile("rb") as stream:
                stream.readline()
            client.close()
    finally:
        server.stop()
    assert len(calls) == 2, "hello must reflect current state, not a cached snapshot"


def test_broadcast_reaches_a_connected_client(tmp_path):
    server = EventServer(str(tmp_path / "r.sock"), _hello)
    server.start()
    try:
        client = _connect(server.socket_path)
        stream = client.makefile("rb")
        stream.readline()  # hello
        assert _wait_for_clients(server, 1)
        server.broadcast({"type": "stage", "job_id": "j1", "stage": "trunk", "frac": 0.3})
        event = decode(stream.readline())
        assert event["stage"] == "trunk"
    finally:
        stream.close()
        client.close()
        server.stop()


def test_broadcast_reaches_every_connected_client(tmp_path):
    server = EventServer(str(tmp_path / "r.sock"), _hello)
    server.start()
    clients, streams = [], []
    try:
        for _ in range(3):
            c = _connect(server.socket_path)
            s = c.makefile("rb")
            s.readline()
            clients.append(c)
            streams.append(s)
        assert _wait_for_clients(server, 3)
        assert server.broadcast({"type": "card_state", "card": 0, "state": "busy"}) == 3
        for s in streams:
            assert decode(s.readline())["state"] == "busy"
    finally:
        for s in streams:
            s.close()
        for c in clients:
            c.close()
        server.stop()


def test_broadcasting_with_no_clients_is_harmless(tmp_path):
    server = EventServer(str(tmp_path / "r.sock"), _hello)
    server.start()
    try:
        assert server.broadcast({"type": "card_state", "card": 0, "state": "idle"}) == 0
    finally:
        server.stop()


def test_a_disconnected_client_is_dropped_without_affecting_others(tmp_path):
    server = EventServer(str(tmp_path / "r.sock"), _hello)
    server.start()
    survivor = None
    try:
        doomed = _connect(server.socket_path)
        with doomed.makefile("rb") as s:
            s.readline()
        survivor = _connect(server.socket_path)
        survivor_stream = survivor.makefile("rb")
        survivor_stream.readline()
        assert _wait_for_clients(server, 2)

        doomed.close()
        # Two broadcasts: the first may be what discovers the dead peer.
        server.broadcast({"type": "card_state", "card": 0, "state": "idle"})
        server.broadcast({"type": "card_state", "card": 1, "state": "idle"})

        seen = [decode(survivor_stream.readline()) for _ in range(2)]
        assert [e["card"] for e in seen] == [0, 1]
    finally:
        if survivor is not None:
            survivor.close()
        server.stop()


def test_a_stale_socket_file_does_not_block_startup(tmp_path):
    path = tmp_path / "r.sock"
    path.write_text("leftover from a crashed run")
    server = EventServer(str(path), _hello)
    server.start()
    try:
        client = _connect(str(path))
        client.close()
    finally:
        server.stop()


def test_stop_removes_the_socket_file(tmp_path):
    path = tmp_path / "r.sock"
    server = EventServer(str(path), _hello)
    server.start()
    server.stop()
    assert not path.exists()
