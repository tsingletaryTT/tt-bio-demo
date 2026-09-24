"""The bridge: an ordinary daemon client that also happens to speak HTTP.

From the daemon's point of view this process is just another UI client, on
the same socket, speaking the exact same protocol/events.py wire format as
ui/client.py -- nothing here is a new daemon capability. What it adds is a
fan-out: every event the daemon sends is decoded once and re-published to
every connected browser tab over Server-Sent Events (`GET /events`), and a
browser's `pick`/`question`/`egg` action arrives as a small JSON POST and is
re-encoded with the daemon's own `encode_client_message` before being written
to the one real socket connection this process owns.

Why SSE and not a WebSocket: the traffic is almost entirely one direction
(daemon events out to N tabs) with rare, small actions the other way (a
visitor's tap). SSE covers exactly that shape with nothing beyond the
standard library -- no new dependency, no handshake/framing to get right by
hand -- and a browser's `EventSource` reconnects on its own. A `pick` is an
ordinary POST.

Runs with no hardware: point --daemon-socket at a `runner.mock.MockRunner`
serving one of tests/fixtures/streams/*.jsonl (see webview/README.md) and
every event this module forwards is real, recorded protocol traffic -- not
fabricated for the demo.

Import discipline: this module still reaches into no `runner.*` code -- everything it
knows about the daemon's own wire comes from `protocol/events.py`. It DOES reach into
several `ui.*` modules for their pure, GTK-free logic (ui.telemetry's tt-smi sampler,
ui.questions'/ui.chipviz's text-formatting functions, ui.playlist's loaders,
ui.cartoon's mesh builder) rather than reimplementing any of it -- which means, since
2026-09-24, this module requires `.venvs/venv-ui` specifically (PyGObject + gemmi +
numpy), not `.venvs/venv-runner`. Run it through `.venvs/venv-ui/bin/python3`, per this
project's convention.

Auth: binding beyond loopback (--host anything other than 127.0.0.1/
localhost/::1) exposes unauthenticated fold/Q&A control endpoints to every
client that can reach the port, so a non-loopback bind refuses to start
unless --auth-token/WEBVIEW_AUTH_TOKEN is set. See webview/README.md.
"""

import argparse
import hmac
import http.server
import json
import logging
import os.path
import queue
import socket
import threading
import urllib.parse
from pathlib import Path

from protocol.events import (
    PROTOCOL_VERSION,
    ProtocolError,
    decode,
    decode_client_message,
    egg_message,
    encode_client_message,
    pick_message,
    question_message,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# The same vendored tensix-viz build ui/chipviz.py already ships, reused
# verbatim rather than fetched from anywhere at runtime -- see
# ui/assets/tensix-viz/PROVENANCE.md for why this project vendors these two
# files at all (offline at the venue) and "Do not hand-edit these files".
TENSIX_VIZ_ASSETS_DIR = Path(__file__).parent.parent / "ui" / "assets" / "tensix-viz"


def _ensure_tensix_viz_assets_linked():
    """Symlink the vendored tensix-viz.{js,css} into webview/static/ so the
    existing _serve_static path-traversal guard covers them with no second
    static root. Idempotent -- safe to call on every startup. Falls back to
    a real copy if symlinking is unavailable (e.g. some container/FS setups),
    so a booth this runs on is never left with a missing asset over a
    filesystem quirk this bridge cannot control."""
    for name in ("tensix-viz.js", "tensix-viz.css"):
        source = TENSIX_VIZ_ASSETS_DIR / name
        dest = STATIC_DIR / name
        if dest.exists() or dest.is_symlink():
            continue
        if not source.is_file():
            log.warning("tensix-viz asset missing at %s; Tensix panel will "
                       "have no library to draw with", source)
            continue
        try:
            dest.symlink_to(source)
        except OSError:
            import shutil
            shutil.copyfile(source, dest)

# How long to wait before retrying a dropped or refused daemon connection.
# Same order of magnitude as the daemon's own WORKER_RESTART_DELAY_S: "this
# needs a moment or a human", not a tight retry loop hammering a socket that
# just isn't there yet.
RECONNECT_DELAY_S = 2.0

# After this many consecutive failed connection attempts, the log message
# escalates to WARNING with a running count instead of the ordinary one-line
# notice. A fixed retry interval with no escalation looks identical in the
# log whether the daemon is a few seconds from being up or never coming
# back at all -- this gives an operator tailing the log a way to tell those
# apart. ~30s at the default delay.
RECONNECT_ESCALATE_AFTER = 15

# Upper bound on a POST body this bridge will read into memory. Every real
# body here is a JSON object with one or two id fields, each capped by the
# daemon's own MAX_TARGET_ID_LEN (64 chars) -- a few hundred bytes covers
# that with room to spare. Without this, a client-supplied Content-Length
# decides how much memory one request gets to allocate, which is exactly
# what runner/server.py's CLIENT_LINE_MAX_BYTES (64 KiB) exists to prevent
# on the daemon-facing socket; this bridge is the new public HTTP surface
# (the README documents --host 0.0.0.0 as a supported mode) and needs the
# same kind of bound.
MAX_POST_BODY_BYTES = 4096

# Bound on one subscriber's backlog. A tab that stops reading (backgrounded,
# a dead network path) must not be allowed to grow without limit, and it must
# not be allowed to make the daemon-reading thread block on ITS queue while
# every other tab waits behind it -- see publish.
SUBSCRIBER_QUEUE_MAX = 200

# How long GET /events waits for a real event before writing a comment line,
# so a browser (and any proxy sitting in front of this) does not decide the
# connection is dead during a quiet stretch between folds.
SSE_KEEPALIVE_S = 15.0

# Bound on reading a POST body once Content-Length is known-good. Without
# this, a client that sends headers and then only part of the body (or
# nothing further) leaves rfile.read() blocked forever on that one
# ThreadingHTTPServer thread -- and since this bridge's public HTTP surface
# is exactly the non-loopback mode the README documents, a handful of such
# slow POSTs can exhaust threads and starve every legitimate tab/action.
POST_BODY_READ_TIMEOUT_S = 5.0

# A bridge bound to one of these is reachable only from this same machine --
# an SSH tunnel or a local browser, never a client elsewhere on the network.
# Anything else (including 0.0.0.0) is a non-loopback bind and, per the
# module docstring above, requires an auth token before this process will
# start.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

# Every action a browser tab may ask the daemon to take, and the pure
# function that turns a POST body into the matching protocol message. A
# table, not a chain of `if kind == ...`, for the same reason
# protocol.events.CLIENT_MESSAGE_FIELDS is a table: adding an action here
# cannot accidentally add one with no validation, because building the
# message always goes through pick_message/question_message/egg_message,
# which is where encode_client_message's own field checks actually live.
#
# `egg` is wired here for protocol completeness (a browser tab CAN ask for
# it), but nothing in webview/static/ puts a button in front of it. The
# native booth deliberately keeps the easter egg undocumented -- see
# ui/quad.py's own note that "an easter egg that is documented is a
# feature" -- and a web viewer surfacing a labelled control for it would be
# exactly that.
_ACTION_BUILDERS = {
    "pick": lambda body: pick_message(body.get("target_id", "")),
    "question": lambda body: question_message(
        body.get("question_id", ""), body.get("target_id", "")),
    "egg": lambda body: egg_message(body.get("egg_id", "")),
}


class DaemonUnavailable(Exception):
    """Raised by DaemonLink.send_client_message when there is no live
    connection to forward a client message over."""


def is_loopback_host(host):
    return host in LOOPBACK_HOSTS


def check_non_loopback_requires_auth(host, auth_token):
    """Refuse to proceed if `host` is not a loopback address and no
    auth_token is configured. Pulled out of main() as a pure function so it
    is directly unit-testable without argv/subprocess plumbing.

    A message passed to SystemExit is printed to stderr and the process
    exits 1 -- exactly the behavior a CLI script wants here, with no
    separate print() call to keep in sync with it.
    """
    if not is_loopback_host(host) and not auth_token:
        raise SystemExit(
            f"refusing to bind the webview bridge to {host} without "
            "--auth-token or WEBVIEW_AUTH_TOKEN set: a non-loopback bridge "
            "exposes fold/Q&A controls to anyone who can reach this host. "
            "Use the default loopback bind behind an SSH tunnel, or set a "
            "token.")


class DaemonLink:
    """The one connection to the daemon, decoded once and fanned out.

    `connect` is injectable so tests can hand this a fake transport instead
    of a real AF_UNIX socket -- the same seam runner/workers.py's
    `_detect_tenstorrent_devices` etc. use for the same reason.
    """

    def __init__(self, socket_path, connect=None):
        self.socket_path = socket_path
        self._connect = connect or self._default_connect
        self._sock = None
        self._sock_lock = threading.Lock()
        # Guards the actual write, not just the pointer read -- see
        # send_client_message. ThreadingHTTPServer runs one thread per
        # connection, so two browser POSTs can call send_client_message
        # concurrently; without a lock held across the write itself, their
        # newline-delimited payloads can interleave on the one real socket
        # this process owns, and the daemon reads garbage.
        self._send_lock = threading.Lock()
        self._subscribers = set()
        self._subscribers_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        # The most recent hello/not_ready, so a tab that opens mid-session
        # is told what booth it is looking at immediately rather than
        # waiting for the daemon's next unprompted event -- ui/client.py
        # gets this for free because `hello` is the first line of any fresh
        # connection; a shared subscriber fan-out does not have that
        # property on its own; this is what restores it.
        self.last_hello = None
        # Set once a `hello` reports a protocol version this bridge does not
        # speak. Mirrors ui/client.py's own "incompatible" refusal: once
        # true, the connection this phase is reading is torn down, no
        # further reconnect attempt is made, and send_client_message refuses
        # rather than writing a pick a mismatched daemon may silently drop.
        self.incompatible = False

    def _default_connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        return sock

    @property
    def connected(self):
        with self._sock_lock:
            return self._sock is not None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._sock_lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def subscribe(self):
        """A queue that receives every future event, primed with the last
        hello/not_ready if one has already arrived.

        Primed BEFORE the queue is added to `_subscribers`, not after: the
        daemon sends exactly one hello/not_ready per connection (it is the
        greeting, not a periodic status), so there is nothing for a live
        broadcast to race against here -- but adding to `_subscribers` first
        would let a concurrent `publish` of some other event deliver into
        this queue before the priming put, handing the caller
        [live_event, hello] instead of [hello, live_event]. Priming first
        means the very worst case is a subscriber occasionally missing a
        same-connection hello it was already too late for, never seeing one
        out of order.
        """
        q = queue.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        if self.last_hello is not None:
            q.put_nowait(self.last_hello)
        with self._subscribers_lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self._subscribers_lock:
            self._subscribers.discard(q)

    def send_client_message(self, message):
        """Encode `message` (from pick_message/question_message/egg_message)
        and write it to the daemon. Raises ProtocolError if `message` itself
        is malformed, DaemonUnavailable if there is nothing to write it to.

        The daemon's own `decode_client_message` is the real validation
        boundary (CLIENT_MESSAGE_FIELDS' own comment: it exists because the
        daemon "reads this off a public socket in a room full of strangers'
        laptops"). Round-tripping through it here, rather than writing a
        second copy of "target_id must be a non-empty string under
        MAX_TARGET_ID_LEN", answers a browser's malformed POST with a
        synchronous 400 instead of a silent send into a daemon that will
        just log-and-drop it (Daemon._handle_client_message's documented
        behavior for exactly this case) with nothing on the wire to say so.
        """
        if self.incompatible:
            # Refuse rather than send: encode_client_message validates
            # against THIS build's protocol/events.py, not the daemon's --
            # a message that looks well-formed here could be meaningless or
            # silently dropped by a daemon speaking a different version, and
            # a 204 back to the browser would misreport that as delivered.
            raise DaemonUnavailable(
                "daemon speaks an incompatible protocol version")
        payload = encode_client_message(message)
        decode_client_message(payload)
        with self._sock_lock:
            sock = self._sock
        if sock is None:
            raise DaemonUnavailable("not connected to a daemon")
        try:
            # The connection lock above only protects reading `self._sock`;
            # the write itself is serialized separately so two concurrent
            # callers can never interleave their payloads on the wire (see
            # `_send_lock`'s own comment).
            with self._send_lock:
                sock.sendall(payload)
        except OSError as exc:
            raise DaemonUnavailable(f"daemon connection lost: {exc}") from exc

    def _run(self):
        consecutive_failures = 0
        while not self._stop.is_set():
            try:
                self._serve_one_connection()
                consecutive_failures = 0
            except OSError as exc:
                consecutive_failures += 1
                if consecutive_failures == RECONNECT_ESCALATE_AFTER:
                    log.warning(
                        "daemon connection has failed %d times in a row "
                        "(%s); still retrying every %.0fs, but this now "
                        "looks like a permanent problem rather than a slow "
                        "start -- check --daemon-socket",
                        consecutive_failures, exc, RECONNECT_DELAY_S)
                else:
                    log.warning("daemon connection attempt failed: %s", exc)
            with self._sock_lock:
                self._sock = None
            if self.incompatible:
                log.error(
                    "daemon protocol is incompatible with this bridge "
                    "(this build speaks v%s); not retrying", PROTOCOL_VERSION)
                return
            if self._stop.wait(RECONNECT_DELAY_S):
                return

    def _serve_one_connection(self):
        sock = self._connect()
        with self._sock_lock:
            self._sock = sock
        log.info("connected to daemon at %s", self.socket_path)
        buf = b""
        with sock:
            sock.settimeout(0.5)
            while not self._stop.is_set() and not self.incompatible:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    log.info("daemon closed the connection")
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._dispatch(line + b"\n")

    def _dispatch(self, line):
        try:
            event = decode(line)
        except ProtocolError as exc:
            # One malformed line from the daemon must not take the whole
            # bridge down -- every other UI client this daemon has ever
            # spoken to shares that same rule (ui/client.py's own decode
            # loop). Logged, not raised.
            log.warning("dropping malformed event from daemon: %s", exc)
            return
        if event["type"] == "hello" and event.get("version") != PROTOCOL_VERSION:
            # Mirrors ui/client.py's own refusal, one layer over: that
            # module refuses to INTERPRET an incompatible daemon; this one
            # additionally stops relaying to it and stops trying to
            # reconnect (see _run), because forwarding events whose shape
            # this build's protocol/events.py does not actually own is how
            # a version mismatch turns into a confusing crash three modules
            # downstream instead of one clear log line here.
            log.error(
                "daemon speaks protocol v%s, this bridge speaks v%s -- "
                "relaying this hello for display, then giving up on this "
                "connection and not reconnecting",
                event.get("version"), PROTOCOL_VERSION)
            self.incompatible = True
            event = {**event, "bridge_incompatible": True}
        if event["type"] in ("hello", "not_ready"):
            self.last_hello = event
        self.publish(event)

    def publish(self, event):
        """Broadcast `event` to every subscriber, the same fan-out
        `_dispatch` uses for a relayed daemon event. The one entry point
        every OTHER publisher in this module (TelemetryBroadcaster,
        RibbonCache, the Q&A tracker) uses too -- so a slow/backgrounded
        tab's queue filling up drops an event for THAT tab only, for a
        bridge-originated event exactly as it already does for a relayed
        one. See SUBSCRIBER_QUEUE_MAX.
        """
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # A frozen or backgrounded tab must not make the daemon-
                # reading thread block -- that would stall every OTHER
                # tab's view of a live booth because of one dead one. Drop
                # for this subscriber only, loudly.
                log.warning("subscriber queue full; dropping an event for "
                           "one slow client")


def make_handler(daemon_link, auth_token=None,
                  body_read_timeout_s=POST_BODY_READ_TIMEOUT_S):
    """Build a BaseHTTPRequestHandler bound to this one DaemonLink.

    A factory rather than a module-level class because http.server hands the
    class itself to the socket server, with no constructor hook for extra
    arguments -- this closes over `daemon_link` (and `auth_token`,
    `body_read_timeout_s`) instead.

    `auth_token`, when set, is required on EVERY request -- GET (/events,
    static files) and POST alike. Leaving any one of them open would be
    exactly the "a check that knows less than the thing it's protecting"
    mistake this project's own history calls out repeatedly: /events in
    particular streams live, otherwise-private demo state, not just the
    mutating actions.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "tt-bio-demo-webview/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.info("%s - %s", self.address_string(), fmt % args)

        def _authorized(self):
            if auth_token is None:
                return True
            provided = None
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                provided = auth_header[len("Bearer "):]
            if provided is None:
                query = urllib.parse.urlsplit(self.path).query
                values = urllib.parse.parse_qs(query).get("token")
                if values:
                    provided = values[0]
            if provided is None:
                return False
            # Constant-time comparison: a client guessing the token one
            # character at a time should not be able to use response-time
            # differences to confirm each correct prefix.
            return hmac.compare_digest(provided, auth_token)

        def _send_unauthorized(self):
            body = json.dumps({"error": "unauthorized"}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authorized():
                self._send_unauthorized()
                return
            path = urllib.parse.urlsplit(self.path).path
            if path == "/events":
                self._serve_events()
            else:
                self._serve_static(path)

        def do_POST(self):
            if not self._authorized():
                self._send_unauthorized()
                return
            path = urllib.parse.urlsplit(self.path).path
            builder = _ACTION_BUILDERS.get(path.lstrip("/"))
            if builder is None:
                self.send_error(404, "no such action")
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self.send_error(400, "malformed Content-Length header")
                return
            if length < 0:
                # A negative value passes the upper-bound check below (it is
                # less than MAX_POST_BODY_BYTES) and would reach rfile.read()
                # with a negative count, which reads until EOF instead of
                # respecting any bound at all.
                self.send_error(400, "negative Content-Length")
                return
            if length > MAX_POST_BODY_BYTES:
                self.send_error(
                    413, f"body too large (max {MAX_POST_BODY_BYTES} bytes)")
                return
            self.connection.settimeout(body_read_timeout_s)
            try:
                raw = self.rfile.read(length) if length else b"{}"
            except (socket.timeout, TimeoutError):
                # A client that sent valid, under-the-limit headers and then
                # stalled mid-body must not be allowed to hold this thread
                # (one per connection, under ThreadingHTTPServer) forever.
                self.send_error(
                    408, "timed out waiting for the request body")
                return
            finally:
                # Reset for whatever this (persistent, HTTP/1.1) connection
                # does next -- a lowered read timeout must not leak into the
                # framework's own between-requests header read.
                self.connection.settimeout(None)
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                self.send_error(400, f"malformed request body: {exc}")
                return
            try:
                daemon_link.send_client_message(builder(body))
            except ProtocolError as exc:
                self.send_error(400, str(exc))
                return
            except DaemonUnavailable as exc:
                self.send_error(503, str(exc))
                return
            self.send_response(204)
            self.end_headers()

        def _serve_static(self, path):
            if path == "/":
                path = "/index.html"
            static_root = STATIC_DIR.resolve()
            # Collapse ".."/"." components LEXICALLY (os.path.normpath does
            # no filesystem access and never follows a symlink), then check
            # the result stays inside static_root. This is deliberately NOT
            # `.resolve()` on the whole candidate: `.resolve()` also follows
            # the FINAL path component's own symlink, and
            # _ensure_tensix_viz_assets_linked() puts real symlinks to
            # ui/assets/tensix-viz/ (outside this directory, on purpose)
            # directly inside STATIC_DIR -- a first version of this guard
            # resolved those straight into a 403, since their target is
            # legitimately outside static_root. Blocking `..`-escapes still
            # works exactly as before (the traversal test below pins that);
            # only OUR OWN, deliberately-planted file symlinks are now
            # servable. No client-supplied path can create a new symlink
            # here, so this does not open any traversal this guard exists to
            # stop.
            normalized = os.path.normpath(str(static_root / path.lstrip("/")))
            candidate = Path(normalized)
            try:
                candidate.relative_to(static_root)
            except ValueError:
                # Outside webview/static entirely -- e.g. "/../bridge.py".
                self.send_error(403, "forbidden")
                return
            if not candidate.is_file():
                self.send_error(404, "not found")
                return
            content_type = _STATIC_CONTENT_TYPES.get(
                candidate.suffix, "application/octet-stream")
            data = candidate.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_events(self):
            # Subscribed BEFORE the 200 goes out, not after: a client that
            # has seen a successful response must be guaranteed to receive
            # every event broadcast from that instant on. Registering the
            # queue after end_headers() would leave a window where the
            # client believes it is receiving live events and isn't yet.
            #
            # The subscribe() call and the header-sending it protects both
            # live inside this try/finally, not just the read loop after
            # them: a client that resets the connection between opening it
            # and headers finishing raises out of send_response/end_headers,
            # and without the finally covering that span, daemon_link would
            # never learn to drop this queue -- a leak that grows by one
            # dead Queue per aborted connection for the life of the process.
            q = daemon_link.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                while True:
                    try:
                        event = q.get(timeout=SSE_KEEPALIVE_S)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    line = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                daemon_link.unsubscribe(q)

    return Handler


def build_server(daemon_link, host, port, auth_token=None,
                  body_read_timeout_s=POST_BODY_READ_TIMEOUT_S):
    return http.server.ThreadingHTTPServer(
        (host, port),
        make_handler(daemon_link, auth_token=auth_token,
                    body_read_timeout_s=body_read_timeout_s))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon-socket", required=True,
                        help="Unix socket to connect to as an ordinary "
                             "daemon client -- a real daemon's --socket, "
                             "or a runner.mock.MockRunner's socket_path.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--auth-token", default=None,
                        help="Shared secret every request must present "
                             "(query ?token=... or Authorization: Bearer "
                             "...) -- also settable via WEBVIEW_AUTH_TOKEN. "
                             "Required when --host is not a loopback "
                             "address (127.0.0.1/localhost/::1).")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    auth_token = args.auth_token or os.environ.get("WEBVIEW_AUTH_TOKEN")
    check_non_loopback_requires_auth(args.host, auth_token)
    _ensure_tensix_viz_assets_linked()

    link = DaemonLink(args.daemon_socket)
    link.start()
    from webview.telemetry_broadcaster import TelemetryBroadcaster
    telemetry = TelemetryBroadcaster(link)
    telemetry.start()
    server = build_server(link, args.host, args.port, auth_token=auth_token)
    log.info("serving http://%s:%d (daemon socket: %s)%s",
             args.host, args.port, args.daemon_socket,
             " [auth token required]" if auth_token else "")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        telemetry.stop()
        server.shutdown()
        link.stop()


if __name__ == "__main__":
    main()
