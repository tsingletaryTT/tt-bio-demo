"""webview.bridge: relaying real protocol events to N browser tabs, and
turning a tab's POST back into a real client->server message.

Same instrument as tests/unit/test_mock_runner.py -- runner.mock.MockRunner
replaying a real fixture over a real Unix socket -- so "the bridge forwards
what the daemon actually said" is proven against recorded protocol traffic,
not a hand-typed stand-in for it. Neither MockRunner nor webview.bridge
imports torch or tt-bio, so this runs under venv-ui, exactly like
test_mock_runner.py.
"""

import http.client
import json
import pathlib
import queue
import socket
import tempfile
import threading
import time

import pytest

from protocol.events import PROTOCOL_VERSION, ProtocolError, encode, pick_message
from runner.mock import MockRunner, load_stream
from webview import bridge
from webview.bridge import (
    MAX_POST_BODY_BYTES,
    POST_BODY_READ_TIMEOUT_S,
    RECONNECT_DELAY_S,
    SUBSCRIBER_QUEUE_MAX,
    DaemonLink,
    DaemonUnavailable,
    build_server,
    check_non_loopback_requires_auth,
    is_loopback_host,
)

FIXTURE = pathlib.Path("tests/fixtures/streams/short_fold.jsonl")
QUESTION_FIXTURE = pathlib.Path("tests/fixtures/streams/with_question.jsonl")


# ── helpers ──────────────────────────────────────────────────────────────


def _temp_socket_path():
    return tempfile.mktemp(prefix="tt-bio-demo-webview-test-", suffix=".sock")


def _drain(q, count, timeout=5.0):
    """Pull exactly `count` events off a subscriber queue, or fail loudly
    rather than hang the suite if the bridge silently dropped one."""
    events = []
    deadline = time.monotonic() + timeout
    while len(events) < count:
        remaining = deadline - time.monotonic()
        assert remaining > 0, (
            f"only got {len(events)}/{count} events before the timeout")
        events.append(q.get(timeout=remaining))
    return events


class _FakeSocket:
    """A `connect()` stand-in for DaemonLink that never touches a real
    socket -- for the two tests that need to control exactly what the
    "daemon" sends without a MockRunner's own timing involved."""

    def __init__(self, incoming=()):
        self._incoming = list(incoming)
        self.sent = []
        self.closed = False

    def settimeout(self, _seconds):
        pass

    def recv(self, _nbytes):
        if self._incoming:
            return self._incoming.pop(0)
        # Mirrors a real idle socket under DaemonLink's 0.5s recv timeout:
        # nothing to read yet, connection still open.
        raise socket.timeout()

    def sendall(self, data):
        self.sent.append(data)

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


# ── DaemonLink against a real MockRunner ────────────────────────────────


def test_events_are_relayed_in_order_and_unmodified():
    sock_path = _temp_socket_path()
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
    runner.start()
    link = DaemonLink(sock_path)
    link.start()
    try:
        q = link.subscribe()
        expected = [{k: v for k, v in e.items() if k != "_delay_ms"}
                   for e in load_stream(FIXTURE)]
        got = _drain(q, len(expected))
        assert got == expected
    finally:
        link.stop()
        runner.stop()


def test_two_subscribers_each_see_the_whole_stream():
    sock_path = _temp_socket_path()
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
    runner.start()
    link = DaemonLink(sock_path)
    link.start()
    try:
        q1 = link.subscribe()
        q2 = link.subscribe()
        expected_count = len(load_stream(FIXTURE))
        got1 = _drain(q1, expected_count)
        got2 = _drain(q2, expected_count)
        assert got1 == got2
        assert got1[0]["type"] == "hello"
        assert got1[-1]["type"] == "job_done"
    finally:
        link.stop()
        runner.stop()


def test_a_late_subscriber_is_caught_up_on_hello():
    """A tab opened after the booth already said hello must not be left
    guessing what it is looking at until the daemon's next unprompted
    event -- see DaemonLink.last_hello's own docstring."""
    sock_path = _temp_socket_path()
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
    runner.start()
    link = DaemonLink(sock_path)
    link.start()
    try:
        first = link.subscribe()
        _drain(first, 1)  # make sure hello has actually been broadcast
        deadline = time.monotonic() + 2.0
        while link.last_hello is None and time.monotonic() < deadline:
            time.sleep(0.01)
        late = link.subscribe()
        assert late.get(timeout=1.0) == link.last_hello
        assert link.last_hello["type"] == "hello"
    finally:
        link.stop()
        runner.stop()


def test_answer_events_from_the_question_fixture_reach_a_subscriber():
    """The affinity Q&A events (answer_start/answer_done) are ordinary
    events as far as this bridge is concerned -- no special-casing, and
    this is what proves that rather than assuming it."""
    sock_path = _temp_socket_path()
    events = load_stream(QUESTION_FIXTURE)
    runner = MockRunner(sock_path, events, speed=100.0)
    runner.start()
    link = DaemonLink(sock_path)
    link.start()
    try:
        q = link.subscribe()
        got = _drain(q, len(events))
        kinds = [e["type"] for e in got]
        assert "answer_start" in kinds
        assert "answer_done" in kinds
        done = next(e for e in got if e["type"] == "answer_done")
        assert "score" in done and "affinity_pred_value" in done
    finally:
        link.stop()
        runner.stop()


def test_reconnect_against_mock_runner_replays_the_whole_fixture():
    """Documents a real discrepancy rather than papering over it.

    MockRunner's own docstring promises "each connecting client gets the
    full stream from the beginning". The real EventServer (runner/server.py)
    does not: `_accept_loop` sends `hello`/`not_ready` to a fresh connection
    and everything after that is only ever `broadcast()`, never replayed.
    So a DaemonLink reconnect exercised against MockRunner shows a full
    historical re-replay landing on an already-subscribed browser tab --
    behavior a real daemon reconnect would never produce (it would send a
    fresh `hello` and nothing else). There is nothing to fix in DaemonLink
    for this: it is not wrong to relay whatever its one connection actually
    sends. This test exists so the mock's own replay-on-connect semantics
    are pinned somewhere explicit instead of silently assumed to match
    production.
    """
    sock_path = _temp_socket_path()
    events = load_stream(FIXTURE)
    runner = MockRunner(sock_path, events, speed=100.0)
    runner.start()
    link = DaemonLink(sock_path)
    link.start()
    try:
        q = link.subscribe()
        first_pass = _drain(q, len(events))
        assert first_pass[0]["type"] == "hello"
        assert first_pass[-1]["type"] == "job_done"
        # MockRunner closes each connection after one full replay (its own
        # `_serve`'s `with conn:` exits at the end of `self.events`);
        # DaemonLink notices the close and reconnects after
        # RECONNECT_DELAY_S, and MockRunner replays the SAME fixture again
        # from scratch for that new connection.
        second_pass = _drain(q, len(events), timeout=RECONNECT_DELAY_S + 5.0)
        assert second_pass == first_pass
    finally:
        link.stop()
        runner.stop()


# ── protocol version mismatch ─────────────────────────────────────────────


def _hello(version):
    return {"type": "hello", "version": version, "cards": [0],
            "models": ["protenix-v2"], "preflight": "ok", "qa_capable": False}


def test_an_incompatible_hello_is_flagged_and_still_delivered():
    """The bridge relays an incompatible hello for display (so a viewer can
    show what happened) but marks it -- see webview/static/app.js's
    `bridge_incompatible` handling, which is what that flag is for."""
    fake = _FakeSocket(incoming=[encode(_hello(PROTOCOL_VERSION + 1))])
    link = DaemonLink("/does/not/matter", connect=lambda: fake)
    link.start()
    try:
        q = link.subscribe()
        got = q.get(timeout=2.0)
        assert got["bridge_incompatible"] is True
        assert got["version"] == PROTOCOL_VERSION + 1
        deadline = time.monotonic() + 2.0
        while not link.incompatible and time.monotonic() < deadline:
            time.sleep(0.01)
        assert link.incompatible
    finally:
        link.stop()


def test_an_incompatible_daemon_is_never_sent_to_again():
    """send_client_message must refuse once incompatible=True -- a 204 back
    to a browser must never claim delivery to a daemon that may not even
    parse the message the same way this build does."""
    fake = _FakeSocket(incoming=[encode(_hello(PROTOCOL_VERSION + 1))])
    link = DaemonLink("/does/not/matter", connect=lambda: fake)
    link.start()
    try:
        deadline = time.monotonic() + 2.0
        while not link.incompatible and time.monotonic() < deadline:
            time.sleep(0.01)
        assert link.incompatible
        with pytest.raises(DaemonUnavailable):
            link.send_client_message(pick_message("trpcage"))
        assert fake.sent == []
    finally:
        link.stop()


def test_a_compatible_hello_is_not_flagged():
    fake = _FakeSocket(incoming=[encode(_hello(PROTOCOL_VERSION))])
    link = DaemonLink("/does/not/matter", connect=lambda: fake)
    link.start()
    try:
        q = link.subscribe()
        got = q.get(timeout=2.0)
        assert "bridge_incompatible" not in got
        assert not link.incompatible
    finally:
        link.stop()


# ── DaemonLink against a fake transport (fast, no real socket timing) ────


def test_send_client_message_with_no_connection_raises():
    link = DaemonLink("/does/not/matter")
    with pytest.raises(DaemonUnavailable):
        link.send_client_message(pick_message("trpcage"))


def test_send_client_message_writes_the_exact_encoded_bytes():
    fake = _FakeSocket()
    link = DaemonLink("/does/not/matter", connect=lambda: fake)
    link.start()
    try:
        deadline = time.monotonic() + 2.0
        while not link.connected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert link.connected
        link.send_client_message(pick_message("trpcage"))
        assert fake.sent == [encode_client_message_of("trpcage")]
    finally:
        link.stop()


def encode_client_message_of(target_id):
    from protocol.events import encode_client_message
    return encode_client_message(pick_message(target_id))


def test_concurrent_send_client_messages_never_interleave_on_the_wire():
    """Two browser tabs' actions arrive on two different HTTP threads and
    both call send_client_message concurrently -- without a lock held
    across the actual write (not just the socket-pointer read), their
    payloads can interleave on the daemon's one real connection. Uses
    _SlowFakeSocket, whose sendall() deliberately takes real time mid-write,
    to make that race reproducible rather than hoping to catch it by luck."""
    fake = _SlowFakeSocket()
    link = DaemonLink("/does/not/matter", connect=lambda: fake)
    link.start()
    try:
        deadline = time.monotonic() + 2.0
        while not link.connected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert link.connected

        errors = []

        def send(target_id):
            try:
                link.send_client_message(pick_message(target_id))
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(exc)

        t1 = threading.Thread(target=send, args=("aaaaaaaaaaaaaaaa",))
        t2 = threading.Thread(target=send, args=("bbbbbbbbbbbbbbbb",))
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        assert not errors

        joined = b"".join(fake.sent)
        lines = joined.splitlines(keepends=True)
        # Two complete, unmangled JSON lines -- if the two sendall() calls
        # had interleaved their chunks, this would either be more than two
        # lines (a stray newline landing mid-payload) or a line that fails
        # to parse as JSON (two half-payloads glued together).
        assert len(lines) == 2
        for line in lines:
            json.loads(line)
    finally:
        link.stop()


def test_a_full_subscriber_queue_is_dropped_not_blocked_on():
    """A tab that stops reading must not stall the reader thread for every
    OTHER tab -- see publish's own comment. Reaches into `_subscribers`
    directly with a maxsize=1 queue so the full condition is guaranteed
    rather than raced for."""
    link = DaemonLink("/does/not/matter")
    tiny = queue.Queue(maxsize=1)
    link._subscribers.add(tiny)
    hello = {"type": "hello", "version": 4, "cards": [0], "models": [],
             "preflight": "ok", "qa_capable": False}
    not_ready = {"type": "not_ready", "missing": ["x"]}
    link.publish(hello)
    link.publish(not_ready)  # must not block just because `tiny` is full
    assert tiny.get_nowait() == hello
    assert tiny.empty()


def test_publish_reaches_every_subscriber_the_same_way_dispatch_does():
    """publish() is the SAME broadcast _dispatch uses for relayed daemon
    events -- a later publisher (telemetry, ribbon cache) must not need a
    second fan-out mechanism, and must inherit the same backpressure."""
    link = DaemonLink("/nonexistent")
    q = link.subscribe()
    link.publish({"type": "telemetry", "chips": []})
    assert q.get_nowait() == {"type": "telemetry", "chips": []}


def test_publish_drops_for_a_full_subscriber_only_never_raises():
    link = DaemonLink("/nonexistent")
    q = link.subscribe()
    for _ in range(SUBSCRIBER_QUEUE_MAX):
        q.put_nowait({"type": "telemetry", "chips": []})
    link.publish({"type": "telemetry", "chips": []})  # must not raise queue.Full outward


def test_a_malformed_line_from_the_daemon_is_dropped_not_fatal():
    hello = {"type": "hello", "version": 4, "cards": [0],
             "models": ["protenix-v2"], "preflight": "ok",
             "qa_capable": False}
    fake = _FakeSocket(incoming=[b"not json at all\n", encode(hello)])
    link = DaemonLink("/does/not/matter", connect=lambda: fake)
    link.start()
    try:
        q = link.subscribe()
        got = q.get(timeout=2.0)
        assert got == hello
        # The malformed line must not have produced a second (garbage)
        # broadcast ahead of the real one, and must not have killed the
        # reader thread before it reached the real one either.
        assert q.empty()
    finally:
        link.stop()


# ── the HTTP layer ───────────────────────────────────────────────────────


class _SlowFakeSocket(_FakeSocket):
    """A connect() stand-in whose sendall() writes in two chunks with a
    sleep between them -- long enough that, without DaemonLink's own send
    lock serializing concurrent callers, a second thread's sendall() can
    interleave its own chunks in between and corrupt both messages on the
    "wire" (here: the `sent` list, in write order)."""

    def sendall(self, data):
        mid = max(1, len(data) // 2)
        self.sent.append(data[:mid])
        time.sleep(0.05)
        self.sent.append(data[mid:])


class _Server:
    """A running bridge HTTP server on an ephemeral port, torn down at the
    end of the `with` block."""

    def __init__(self, daemon_link, auth_token=None,
                 body_read_timeout_s=POST_BODY_READ_TIMEOUT_S):
        self.server = build_server(daemon_link, "127.0.0.1", 0,
                                   auth_token=auth_token,
                                   body_read_timeout_s=body_read_timeout_s)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def connection(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)


def test_post_pick_forwards_to_the_daemon():
    fake = _FakeSocket()
    link = DaemonLink("/does/not/matter", connect=lambda: fake)
    link.start()
    try:
        deadline = time.monotonic() + 2.0
        while not link.connected and time.monotonic() < deadline:
            time.sleep(0.01)
        with _Server(link) as server:
            conn = server.connection()
            body = json.dumps({"target_id": "trpcage"}).encode()
            conn.request("POST", "/pick", body=body,
                        headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 204
        assert fake.sent == [encode_client_message_of("trpcage")]
    finally:
        link.stop()


def test_post_pick_with_missing_target_id_is_a_400():
    fake = _FakeSocket()
    link = DaemonLink("/does/not/matter", connect=lambda: fake)
    with _Server(link) as server:
        conn = server.connection()
        conn.request("POST", "/pick", body=b"{}",
                    headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 400
    assert fake.sent == []


def test_post_pick_with_no_daemon_connection_is_a_503():
    link = DaemonLink("/does/not/exist")  # never started, never connects
    with _Server(link) as server:
        conn = server.connection()
        body = json.dumps({"target_id": "trpcage"}).encode()
        conn.request("POST", "/pick", body=body,
                    headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 503


def test_post_with_a_malformed_content_length_is_a_400_not_a_crash():
    link = DaemonLink("/does/not/matter")
    with _Server(link) as server:
        conn = server.connection()
        # http.client validates headers it builds itself, so the malformed
        # value is written directly to the wire rather than through
        # putheader() -- this is exactly the kind of input a real client
        # (or a fuzzer, on the surface the README documents as optionally
        # bound to 0.0.0.0) could send.
        conn.putrequest("POST", "/pick")
        conn.putheader("Content-Length", "not-a-number")
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 400


def test_post_with_an_oversized_body_is_refused_before_reading_it_all():
    link = DaemonLink("/does/not/matter")
    with _Server(link) as server:
        conn = server.connection()
        oversized = b'{"target_id": "' + b"x" * MAX_POST_BODY_BYTES + b'"}'
        assert len(oversized) > MAX_POST_BODY_BYTES
        conn.request("POST", "/pick", body=oversized,
                    headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 413


def test_post_to_an_unknown_action_is_a_404():
    link = DaemonLink("/does/not/matter")
    with _Server(link) as server:
        conn = server.connection()
        conn.request("POST", "/quit", body=b"{}")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 404


def test_static_files_are_served_and_path_traversal_is_refused():
    link = DaemonLink("/does/not/matter")
    with _Server(link) as server:
        conn = server.connection()
        conn.request("GET", "/index.html")
        resp = conn.getresponse()
        assert resp.status == 200
        assert b"<title>" in resp.read()

        conn2 = server.connection()
        conn2.request("GET", "/../bridge.py")
        resp2 = conn2.getresponse()
        resp2.read()
        assert resp2.status in (403, 404)


def test_tensix_viz_assets_get_linked_into_static(tmp_path, monkeypatch):
    fake_assets = tmp_path / "assets"
    fake_assets.mkdir()
    (fake_assets / "tensix-viz.js").write_text("/* fake js */")
    (fake_assets / "tensix-viz.css").write_text("/* fake css */")
    fake_static = tmp_path / "static"
    fake_static.mkdir()
    monkeypatch.setattr(bridge, "TENSIX_VIZ_ASSETS_DIR", fake_assets)
    monkeypatch.setattr(bridge, "STATIC_DIR", fake_static)
    bridge._ensure_tensix_viz_assets_linked()
    assert (fake_static / "tensix-viz.js").read_text() == "/* fake js */"
    assert (fake_static / "tensix-viz.css").read_text() == "/* fake css */"
    # Idempotent -- a second call must not raise.
    bridge._ensure_tensix_viz_assets_linked()


def test_tensix_viz_assets_missing_source_is_logged_not_fatal(
        tmp_path, monkeypatch, caplog):
    """A missing vendored asset (e.g. an incomplete checkout) must degrade to
    a hidden panel, never a crash at startup -- the same fail-soft contract
    ui/chipviz.py's own read_assets() holds itself to."""
    fake_assets = tmp_path / "assets"
    fake_assets.mkdir()
    # Deliberately empty -- neither tensix-viz.js nor tensix-viz.css exists.
    fake_static = tmp_path / "static"
    fake_static.mkdir()
    monkeypatch.setattr(bridge, "TENSIX_VIZ_ASSETS_DIR", fake_assets)
    monkeypatch.setattr(bridge, "STATIC_DIR", fake_static)
    with caplog.at_level("WARNING"):
        bridge._ensure_tensix_viz_assets_linked()
    assert not (fake_static / "tensix-viz.js").exists()
    assert not (fake_static / "tensix-viz.css").exists()
    assert any("tensix-viz asset missing" in rec.message for rec in caplog.records)


def test_a_symlinked_asset_inside_static_is_served_and_traversal_still_refused(
        tmp_path, monkeypatch):
    """`_ensure_tensix_viz_assets_linked()` puts a real symlink INSIDE
    webview/static/ pointing at ui/assets/tensix-viz/ -- outside it, on
    purpose. `_serve_static`'s path-traversal guard must serve that
    symlink's target rather than refuse it, while still blocking a client
    request that tries to escape STATIC_DIR itself via "..".

    A first version of this guard called `.resolve()` on the whole
    candidate path, which follows a trailing symlink's target too -- so it
    refused this exact vendored file with a 403, caught only by live-
    verifying this task's Step 9 against a real browser, not by any unit
    test. This one exists so that regression cannot come back silently.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.js").write_text("/* real vendored content */")
    fake_static = tmp_path / "static"
    fake_static.mkdir()
    (fake_static / "linked.js").symlink_to(outside / "real.js")
    monkeypatch.setattr(bridge, "STATIC_DIR", fake_static)
    link = DaemonLink("/does/not/matter")
    with _Server(link) as server:
        conn = server.connection()
        conn.request("GET", "/linked.js")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read() == b"/* real vendored content */"

        # Reaching the same file directly, bypassing the symlink, by
        # escaping STATIC_DIR with ".." must still be refused.
        conn2 = server.connection()
        conn2.request("GET", "/../outside/real.js")
        resp2 = conn2.getresponse()
        resp2.read()
        assert resp2.status in (403, 404)


def test_events_endpoint_streams_real_events_from_the_mock_runner():
    """A real MockRunner replay, decoded once by DaemonLink and delivered
    over a real HTTP connection -- not a fake standing in for either half.

    `link.start()` is deliberately deferred until AFTER the raw socket has
    the SSE response's headers in hand: `_serve_events` subscribes before
    it sends those headers (see the comment there), so seeing the headers
    proves the subscription already exists, and only then is there no race
    against how fast MockRunner replays its fixture.
    """
    sock_path = _temp_socket_path()
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=20.0)
    runner.start()
    link = DaemonLink(sock_path)
    try:
        with _Server(link) as server:
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.settimeout(5.0)
            raw.connect(("127.0.0.1", server.port))
            raw.sendall(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += raw.recv(65536)
            _headers, buf = buf.split(b"\r\n\r\n", 1)

            link.start()  # only now can any event legally reach this socket

            seen_types = []
            deadline = time.monotonic() + 5.0
            while len(seen_types) < 2 and time.monotonic() < deadline:
                if b"\n\n" not in buf:
                    buf += raw.recv(65536)
                    continue
                frame, buf = buf.split(b"\n\n", 1)
                if not frame.startswith(b"data: "):
                    continue  # a ": keep-alive" comment line
                seen_types.append(json.loads(frame[len(b"data: "):])["type"])
            raw.close()
            assert seen_types[:2] == ["hello", "job_start"]
    finally:
        link.stop()
        runner.stop()


# ── request body: negative Content-Length, and a bounded read timeout ────


def test_post_with_a_negative_content_length_is_a_400():
    """A negative value passes the `> MAX_POST_BODY_BYTES` upper-bound check
    and would otherwise reach rfile.read(negative), which reads until EOF
    instead of respecting any bound at all."""
    link = DaemonLink("/does/not/matter")
    with _Server(link) as server:
        conn = server.connection()
        conn.putrequest("POST", "/pick")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 400


def test_a_stalled_partial_body_times_out_as_a_408_not_a_hang():
    """A client that declares a Content-Length and then sends only part of
    the body (or nothing further) must not be able to hold this connection's
    one ThreadingHTTPServer thread forever."""
    link = DaemonLink("/does/not/matter")
    with _Server(link, body_read_timeout_s=0.2) as server:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(5.0)
        raw.connect(("127.0.0.1", server.port))
        raw.sendall(
            b"POST /pick HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n"
            b"only-ten-b"  # 10 bytes -- far short of the declared 100
        )
        buf = b""
        deadline = time.monotonic() + 5.0
        while b"\r\n" not in buf and time.monotonic() < deadline:
            buf += raw.recv(65536)
        raw.close()
        status_line = buf.split(b"\r\n", 1)[0]
        assert b"408" in status_line


# ── auth: a non-loopback bind and the token it requires ──────────────────


def test_is_loopback_host_classifies_correctly():
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("10.0.0.5")
    assert not is_loopback_host("")


def test_non_loopback_without_a_token_refuses_to_start():
    with pytest.raises(SystemExit, match="refusing to bind"):
        check_non_loopback_requires_auth("0.0.0.0", None)


def test_non_loopback_with_a_token_is_allowed():
    check_non_loopback_requires_auth("0.0.0.0", "some-secret")  # must not raise


def test_loopback_without_a_token_is_allowed():
    check_non_loopback_requires_auth("127.0.0.1", None)  # must not raise -- today's default


def test_a_request_without_a_token_is_401_when_one_is_configured():
    link = DaemonLink("/does/not/matter")
    with _Server(link, auth_token="secret123") as server:
        conn = server.connection()
        conn.request("GET", "/index.html")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 401


def test_a_request_with_the_correct_query_token_is_allowed():
    link = DaemonLink("/does/not/matter")
    with _Server(link, auth_token="secret123") as server:
        conn = server.connection()
        conn.request("GET", "/index.html?token=secret123")
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()


def test_a_request_with_the_correct_bearer_header_is_allowed():
    link = DaemonLink("/does/not/matter")
    with _Server(link, auth_token="secret123") as server:
        conn = server.connection()
        conn.request("GET", "/index.html",
                    headers={"Authorization": "Bearer secret123"})
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()


def test_a_request_with_the_wrong_token_is_401():
    link = DaemonLink("/does/not/matter")
    with _Server(link, auth_token="secret123") as server:
        conn = server.connection()
        conn.request("GET", "/index.html?token=wrong-guess")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 401


def test_post_actions_are_gated_by_the_token_too_not_just_get():
    """The Review Focus for this fix: /events streams live demo state and a
    POST can trigger a fold -- both must require the token when one is
    configured, not just one or the other."""
    link = DaemonLink("/does/not/matter")
    with _Server(link, auth_token="secret123") as server:
        conn = server.connection()
        body = json.dumps({"target_id": "trpcage"}).encode()
        conn.request("POST", "/pick", body=body,
                    headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 401


def test_no_configured_token_means_every_request_is_allowed():
    """The default, unmodified behavior for every other test in this file --
    stated as its own explicit test so a future change to the auth gate
    cannot silently start requiring a token when none was ever configured."""
    link = DaemonLink("/does/not/matter")
    with _Server(link) as server:  # auth_token=None, the default
        conn = server.connection()
        conn.request("GET", "/index.html")
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
