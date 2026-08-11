# tt-bio-demo Phase 1–2: Protocol and Renderer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the event protocol, a mock runner that replays a recorded fold, and the GTK4 OpenGL renderer — so a protein fold plays back end-to-end on any Linux machine with no Tenstorrent hardware attached.

**Architecture:** A stdlib+numpy protocol module shared by both processes defines newline-delimited JSON events over a Unix socket. A mock runner replays a fixture stream with realistic timing. The GTK4 UI connects as a client, renders diffusion frames as an OpenGL point cloud, and on completion parses the finished CIF with gemmi and cross-fades to a pLDDT-colored ribbon tube.

**Tech Stack:** Python 3.12 (system), GTK 4.14 via PyGObject, PyOpenGL 3.1.7 (OpenGL 3.3 core), numpy 1.26, gemmi 0.6.4, pytest.

**Spec:** [`../specs/2026-08-10-tt-bio-demo-design.md`](../specs/2026-08-10-tt-bio-demo-design.md) — this plan covers §10 build-order phases 1 and 2.

## Global Constraints

- **Target platform:** Ubuntu 24.04, Wayland, Python 3.12. All dependencies must come from Ubuntu repos — no pip installs in the UI environment.
- **No web browser, no WebKit.** Anywhere. This is the defining constraint of the project.
- **`protocol/events.py` must import only stdlib and numpy.** It is imported by both the system-python UI and the tt-bio venv runner; anything else breaks one side.
- **Nothing in the UI may ever display a stack trace or raw error text.** Errors are logged; the display shows neutral copy.
- **OpenGL 3.3 core profile.** The QB2's Mesa/Radeon supports 4.6, but 3.3 keeps the renderer portable to developer laptops.
- **Protocol version constant:** `PROTOCOL_VERSION = 1`.
- **Brand colors** (Tenstorrent, from the docs-site theme): dark base `#092221`, background `#F1F8F8`, primary accent `#1B8EB1`, teal `#74C5DF`, green `#6FABA0`, yellow `#F6BC42`, red-accent `#FA512E`.
- **pLDDT color ramp** (AlphaFold convention, so domain visitors recognize it): `>90` → `#0053D6`, `70–90` → `#65CBF3`, `50–70` → `#FFDB13`, `<50` → `#FF7D45`.
- **Commit after every task.** Conventional-commit prefixes (`feat:`, `test:`, `chore:`).

## Setup (do once, before Task 1)

```bash
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
                    python3-numpy python3-gemmi python3-opengl \
                    python3-pytest libgl1 libglu1-mesa
```

Verify:
```bash
python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk; import numpy, gemmi, OpenGL; print('ok')"
```
Expected: `ok`

Create `pytest.ini` at the repository root. Without `pythonpath`, every test in this
plan fails at import with `ModuleNotFoundError: No module named 'protocol'`:

```ini
[pytest]
pythonpath = .
testpaths = tests
```

Create the test package directories:

```bash
mkdir -p tests/unit tests/fixtures/streams tests/fixtures/structures
```

## File Structure

| File | Responsibility |
|---|---|
| `protocol/events.py` | Event schema, JSON codec, coordinate packing, `PROTOCOL_VERSION`. Shared by both processes; stdlib + numpy only. |
| `runner/mock.py` | Replays a JSONL fixture over a Unix socket with realistic timing. |
| `ui/client.py` | Socket client: connect, reconnect, decode, frame dropping. No GTK imports. |
| `ui/mathutil.py` | Perspective / look-at / rotation matrices. Pure numpy. |
| `ui/geometry.py` | CIF → C-alpha trace; Catmull-Rom spline; tube mesh; pLDDT→color. Pure numpy + gemmi. |
| `ui/shaders.py` | GLSL source strings for the point and ribbon programs. |
| `ui/glutil.py` | Shader compilation and VAO/VBO helpers. Requires a GL context. |
| `ui/viewer.py` | `GtkGLArea` subclass: camera, draw modes, cross-fade. |
| `ui/app.py` | GTK application and window; wires client events into the viewer. |
| `tests/fixtures/` | Recorded event stream, minimal CIF, expected meshes. |

Deliberate boundary: `mathutil`, `geometry`, and `client` contain no GL and no GTK, so the bulk of the logic is unit-testable headlessly. Only `glutil`, `viewer`, and `app` need a display, and those are verified manually with a runnable command.

---

### Task 1: Event protocol codec

**Files:**
- Create: `protocol/__init__.py` (empty)
- Create: `protocol/events.py`
- Test: `tests/unit/test_events.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PROTOCOL_VERSION: int`; `ProtocolError(Exception)`; `encode(event: dict) -> bytes`; `decode(line: bytes) -> dict`; `pack_coords(a: np.ndarray) -> str`; `unpack_coords(s: str) -> np.ndarray`; `EVENT_TYPES: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_events.py`:

```python
import base64
import numpy as np
import pytest

from protocol.events import (
    EVENT_TYPES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode,
    encode,
    pack_coords,
    unpack_coords,
)


def test_protocol_version_is_one():
    assert PROTOCOL_VERSION == 1


def test_all_spec_event_types_present():
    assert EVENT_TYPES == frozenset(
        {"hello", "not_ready", "job_start", "stage", "frame",
         "job_done", "job_error", "card_state"}
    )


def test_encode_appends_newline_and_decodes_back():
    event = {"type": "stage", "job_id": "j1", "stage": "trunk", "frac": 0.3}
    line = encode(event)
    assert line.endswith(b"\n")
    assert decode(line) == event


def test_encode_rejects_unknown_type():
    with pytest.raises(ProtocolError, match="unknown event type"):
        encode({"type": "nonsense"})


def test_decode_rejects_missing_type():
    with pytest.raises(ProtocolError, match="missing 'type'"):
        decode(b'{"job_id": "j1"}\n')


def test_decode_rejects_malformed_json():
    with pytest.raises(ProtocolError, match="malformed JSON"):
        decode(b'{"type": "stage"\n')


def test_decode_rejects_non_object():
    with pytest.raises(ProtocolError, match="not a JSON object"):
        decode(b'[1, 2, 3]\n')


def test_coords_round_trip_preserves_values_and_shape():
    coords = np.array([[1.5, -2.25, 3.0], [0.0, 0.5, -1.0]], dtype=np.float64)
    restored = unpack_coords(pack_coords(coords))
    assert restored.shape == (2, 3)
    assert restored.dtype == np.float32
    np.testing.assert_allclose(restored, coords, rtol=0, atol=1e-6)


def test_pack_coords_is_base64_of_float32_little_endian():
    coords = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
    raw = base64.b64decode(pack_coords(coords))
    assert raw == np.array([1.0, 2.0, 3.0], dtype="<f4").tobytes()


def test_pack_coords_rejects_wrong_shape():
    with pytest.raises(ProtocolError, match="shape"):
        pack_coords(np.zeros((4,), dtype=np.float32))


def test_unpack_coords_rejects_truncated_buffer():
    truncated = base64.b64encode(b"\x00" * 10).decode("ascii")
    with pytest.raises(ProtocolError, match="not a whole number"):
        unpack_coords(truncated)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_events.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'protocol'`

- [ ] **Step 3: Write minimal implementation**

Create `protocol/__init__.py` as an empty file, then `protocol/events.py`:

```python
"""Wire protocol shared by the runner and the UI.

Imports must stay limited to the standard library and numpy: this module is
imported both by the UI on system python3 and by the runner inside the tt-bio
venv, and any richer dependency would have to exist in both.
"""

import base64
import json

import numpy as np

PROTOCOL_VERSION = 1

EVENT_TYPES = frozenset(
    {"hello", "not_ready", "job_start", "stage", "frame",
     "job_done", "job_error", "card_state"}
)


class ProtocolError(Exception):
    """A message could not be encoded or decoded."""


def encode(event):
    """Serialize one event to a newline-terminated JSON line."""
    kind = event.get("type")
    if kind not in EVENT_TYPES:
        raise ProtocolError(f"unknown event type: {kind!r}")
    return (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line):
    """Parse one newline-terminated JSON line into an event dict."""
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"malformed JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise ProtocolError("not a JSON object")
    if "type" not in event:
        raise ProtocolError("missing 'type'")
    if event["type"] not in EVENT_TYPES:
        raise ProtocolError(f"unknown event type: {event['type']!r}")
    return event


def pack_coords(a):
    """Pack an (N, 3) coordinate array as base64 of little-endian float32."""
    arr = np.asarray(a)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ProtocolError(f"coordinates must have shape (N, 3), got {arr.shape}")
    buf = np.ascontiguousarray(arr, dtype="<f4").tobytes()
    return base64.b64encode(buf).decode("ascii")


def unpack_coords(s):
    """Inverse of pack_coords. Returns an (N, 3) float32 array."""
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception as exc:
        raise ProtocolError(f"invalid base64: {exc}") from exc
    if len(raw) % 12 != 0:
        raise ProtocolError(
            f"buffer of {len(raw)} bytes is not a whole number of 3-vectors"
        )
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 3)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_events.py -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add pytest.ini protocol/ tests/unit/test_events.py
git commit -m "feat: add event protocol codec with coordinate packing"
```

---

### Task 2: Recorded fold fixture and mock runner

**Files:**
- Create: `tests/fixtures/streams/short_fold.jsonl`
- Create: `runner/__init__.py` (empty)
- Create: `runner/mock.py`
- Test: `tests/unit/test_mock_runner.py`

**Interfaces:**
- Consumes: `protocol.events.encode`, `decode`, `pack_coords`, `PROTOCOL_VERSION`.
- Produces: `load_stream(path: str) -> list[dict]`; `MockRunner(socket_path: str, events: list[dict], speed: float = 1.0)` with `.start()`, `.stop()`, and attribute `.socket_path`. Each fixture event may carry a `_delay_ms` key, stripped before sending.

- [ ] **Step 1: Write the fixture generator and the failing test**

Create `tests/fixtures/streams/make_short_fold.py` — the fixture is generated rather than hand-written because it contains packed binary coordinates:

```python
"""Generate the short_fold.jsonl fixture: a synthetic 3-residue fold.

Coordinates start as noise and converge to a straight line, which is enough
to exercise the point-cloud renderer and the frame pipeline deterministically.
"""

import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from protocol.events import PROTOCOL_VERSION, pack_coords

OUT = pathlib.Path(__file__).with_name("short_fold.jsonl")
N_ATOMS = 12
N_FRAMES = 6

rng = np.random.default_rng(1234)
target = np.zeros((N_ATOMS, 3), dtype=np.float32)
target[:, 0] = np.linspace(-10.0, 10.0, N_ATOMS)
noise = rng.normal(scale=8.0, size=(N_ATOMS, 3)).astype(np.float32)

events = [
    {"type": "hello", "version": PROTOCOL_VERSION, "cards": [0, 1, 2, 3],
     "models": ["protenix-v2"], "preflight": "ok", "_delay_ms": 0},
    {"type": "job_start", "job_id": "j1", "target_id": "synthetic",
     "model": "protenix-v2", "card": 0, "n_residues": N_ATOMS, "_delay_ms": 50},
    {"type": "stage", "job_id": "j1", "stage": "trunk", "frac": 0.3, "_delay_ms": 50},
]

for i in range(N_FRAMES):
    t = (i + 1) / N_FRAMES
    coords = noise * (1.0 - t) + target * t
    events.append({
        "type": "frame", "job_id": "j1", "step": i + 1, "total": N_FRAMES,
        "n_atoms": N_ATOMS, "coords_b64": pack_coords(coords), "_delay_ms": 100,
    })

events += [
    {"type": "stage", "job_id": "j1", "stage": "confidence", "frac": 0.9, "_delay_ms": 50},
    {"type": "job_done", "job_id": "j1", "cif_path": "tests/fixtures/structures/minimal.cif",
     "wall_s": 1.25, "mean_plddt": 82.4, "_delay_ms": 50},
]

OUT.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events))
print(f"wrote {OUT} ({len(events)} events)")
```

Create `tests/unit/test_mock_runner.py`:

```python
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
```

- [ ] **Step 2: Generate the fixture, then run the test to verify it fails**

Run:
```bash
python3 tests/fixtures/streams/make_short_fold.py
python3 -m pytest tests/unit/test_mock_runner.py -v
```
Expected: fixture writes 11 events; tests FAIL with `ModuleNotFoundError: No module named 'runner'`

- [ ] **Step 3: Write minimal implementation**

Create `runner/__init__.py` as an empty file, then `runner/mock.py`:

```python
"""Replay a recorded event stream over a Unix socket.

This is the project's core test instrument: it lets the entire UI be built and
exercised with no Tenstorrent hardware present. Each connecting client gets the
full stream from the beginning.
"""

import json
import os
import pathlib
import socket
import threading
import time

from protocol.events import encode


def load_stream(path):
    """Read a JSONL fixture into a list of event dicts, `_delay_ms` retained."""
    text = pathlib.Path(path).read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class MockRunner:
    """Serve a fixed event stream to any client that connects.

    `speed` divides each event's `_delay_ms`, so tests can replay instantly
    while a human demo replays at true recorded pace.
    """

    def __init__(self, socket_path, events, speed=1.0):
        self.socket_path = socket_path
        self.events = events
        self.speed = speed
        self._server = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        # A leftover socket file from a crashed run would make bind() fail.
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._server is not None:
            self._server.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            for event in self.events:
                if self._stop.is_set():
                    return
                delay_ms = event.get("_delay_ms", 0)
                if delay_ms:
                    time.sleep(delay_ms / 1000.0 / self.speed)
                payload = {k: v for k, v in event.items() if k != "_delay_ms"}
                try:
                    conn.sendall(encode(payload))
                except (BrokenPipeError, ConnectionResetError):
                    return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_mock_runner.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add runner/ tests/fixtures/streams/ tests/unit/test_mock_runner.py
git commit -m "feat: add mock runner replaying recorded fold streams"
```

---

### Task 3: UI socket client with reconnect and frame dropping

**Files:**
- Create: `ui/__init__.py` (empty)
- Create: `ui/client.py`
- Test: `tests/unit/test_client.py`

**Interfaces:**
- Consumes: `protocol.events.decode`, `PROTOCOL_VERSION`, `ProtocolError`; `runner.mock.MockRunner` (tests only).
- Produces: `LatestFrame` with `.put(event)`, `.take() -> dict | None`, `.dropped: int`; `EventClient(socket_path, on_event, on_state_change=None, reconnect_delay=1.0)` with `.start()`, `.stop()`, `.state` in `{"disconnected", "connected", "incompatible"}`. `on_event` is called from a background thread — callers on GTK must marshal to the main loop themselves.

Frame dropping lives here because §3 of the spec makes it a UI-side responsibility: `frame` events are advisory and only the newest matters, while every other event type must be delivered.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_client.py`:

```python
import pathlib
import threading
import time

from protocol.events import PROTOCOL_VERSION
from runner.mock import MockRunner, load_stream
from ui.client import EventClient, LatestFrame

FIXTURE = pathlib.Path("tests/fixtures/streams/short_fold.jsonl")


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
    sock_path = str(tmp_path / "runner.sock")
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
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
        assert done.wait(timeout=10.0), "job_done never arrived"
    finally:
        client.stop()
        runner.stop()

    kinds = [e["type"] for e in received]
    assert kinds[0] == "hello"
    assert kinds[-1] == "job_done"
    assert kinds.count("stage") == 2


def test_client_reports_connected_state(tmp_path):
    sock_path = str(tmp_path / "runner.sock")
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
    runner.start()

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ui'`

- [ ] **Step 3: Write minimal implementation**

Create `ui/__init__.py` as an empty file, then `ui/client.py`:

```python
"""Socket client for the runner's event stream.

Deliberately free of GTK imports so it can be tested headlessly. `on_event`
fires on a background thread; GTK callers must marshal to the main loop
themselves (see ui/app.py, which uses GLib.idle_add).
"""

import logging
import socket
import threading
import time

from protocol.events import PROTOCOL_VERSION, ProtocolError, decode

log = logging.getLogger(__name__)


class LatestFrame:
    """A one-slot buffer that keeps only the newest frame.

    Diffusion frames are advisory: if the renderer falls behind, showing the
    most recent coordinates is strictly better than working through a backlog,
    which would make the animation lag further behind with every frame.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self.dropped = 0

    def put(self, event):
        with self._lock:
            if self._frame is not None:
                self.dropped += 1
            self._frame = event

    def take(self):
        with self._lock:
            frame, self._frame = self._frame, None
            return frame


class EventClient:
    """Connects to the runner, decodes events, reconnects when dropped."""

    def __init__(self, socket_path, on_event, on_state_change=None,
                 reconnect_delay=1.0):
        self.socket_path = socket_path
        self.on_event = on_event
        self.on_state_change = on_state_change
        self.reconnect_delay = reconnect_delay
        self.state = "disconnected"
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            if self.on_state_change is not None:
                self.on_state_change(state)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._session()
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                log.debug("runner unavailable: %s", exc)
            self._set_state("disconnected")
            if self.state == "incompatible":
                return
            self._stop.wait(self.reconnect_delay)

    def _session(self):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(5.0)
        conn.connect(self.socket_path)
        self._set_state("connected")
        with conn, conn.makefile("rb") as stream:
            for line in stream:
                if self._stop.is_set():
                    return
                try:
                    event = decode(line)
                except ProtocolError as exc:
                    log.warning("dropping undecodable line: %s", exc)
                    continue
                if event["type"] == "hello":
                    if event.get("version") != PROTOCOL_VERSION:
                        log.error(
                            "runner speaks protocol v%s, UI speaks v%s; "
                            "refusing to interpret its messages",
                            event.get("version"), PROTOCOL_VERSION,
                        )
                        self._set_state("incompatible")
                        return
                self.on_event(event)
```

Note the `incompatible` path returns from `_run` without retrying: a mismatched pair will not fix itself, and reconnecting in a loop would only spam the log.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_client.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add ui/__init__.py ui/client.py tests/unit/test_client.py
git commit -m "feat: add event client with reconnect and frame dropping"
```

---

### Task 4: Matrix math utilities

**Files:**
- Create: `ui/mathutil.py`
- Test: `tests/unit/test_mathutil.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `perspective(fovy_deg, aspect, near, far) -> (4,4) float32`; `look_at(eye, target, up) -> (4,4) float32`; `rotation_y(angle_rad) -> (4,4) float32`; `identity() -> (4,4) float32`. All matrices are **column-major** as OpenGL expects, and are uploaded with `transpose=GL_FALSE`.

Only `rotation_y` is needed: the camera spins about the vertical axis at a fixed
elevation. A pitch matrix arrives with the trackball in Phase 3, not before.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_mathutil.py`:

```python
import numpy as np

from ui.mathutil import identity, look_at, perspective, rotation_y


def test_identity_is_identity():
    np.testing.assert_allclose(identity(), np.eye(4, dtype=np.float32))


def test_perspective_has_expected_shape_and_dtype():
    m = perspective(45.0, 16 / 9, 0.1, 100.0)
    assert m.shape == (4, 4)
    assert m.dtype == np.float32


def test_perspective_maps_near_plane_to_minus_one():
    near, far = 0.5, 50.0
    m = perspective(60.0, 1.0, near, far)
    point = np.array([0.0, 0.0, -near, 1.0], dtype=np.float32)
    clip = m.T @ point          # column-major storage, so transpose to apply
    assert np.isclose(clip[2] / clip[3], -1.0, atol=1e-5)


def test_perspective_maps_far_plane_to_plus_one():
    near, far = 0.5, 50.0
    m = perspective(60.0, 1.0, near, far)
    point = np.array([0.0, 0.0, -far, 1.0], dtype=np.float32)
    clip = m.T @ point
    assert np.isclose(clip[2] / clip[3], 1.0, atol=1e-5)


def test_look_at_places_target_on_negative_z_axis():
    eye = np.array([0.0, 0.0, 10.0])
    view = look_at(eye, np.zeros(3), np.array([0.0, 1.0, 0.0]))
    origin = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
    seen = view.T @ origin
    np.testing.assert_allclose(seen[:3], [0.0, 0.0, -10.0], atol=1e-5)


def test_rotation_y_by_ninety_degrees_maps_x_to_minus_z():
    m = rotation_y(np.pi / 2)
    v = np.array([1.0, 0.0, 0.0, 1.0], dtype=np.float32)
    np.testing.assert_allclose((m.T @ v)[:3], [0.0, 0.0, -1.0], atol=1e-6)


def test_rotation_y_leaves_the_y_axis_fixed():
    m = rotation_y(1.1)
    v = np.array([0.0, 3.0, 0.0, 1.0], dtype=np.float32)
    np.testing.assert_allclose((m.T @ v)[:3], [0.0, 3.0, 0.0], atol=1e-6)


def test_rotation_is_orthonormal():
    for angle in (0.7, -1.3):
        upper = rotation_y(angle)[:3, :3]
        np.testing.assert_allclose(upper @ upper.T, np.eye(3), atol=1e-6)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_mathutil.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ui.mathutil'`

- [ ] **Step 3: Write minimal implementation**

Create `ui/mathutil.py`:

```python
"""Column-major 4x4 matrices for the GL pipeline.

Every matrix here is stored the way OpenGL wants to receive it, so uniforms are
uploaded with transpose=GL_FALSE. To apply one to a column vector in numpy,
transpose it first: `m.T @ v`.
"""

import numpy as np


def identity():
    return np.eye(4, dtype=np.float32)


def perspective(fovy_deg, aspect, near, far):
    """Standard OpenGL perspective projection, depth range [-1, 1]."""
    f = 1.0 / np.tan(np.radians(fovy_deg) / 2.0)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = -1.0
    m[3, 2] = (2.0 * far * near) / (near - far)
    return m


def look_at(eye, target, up):
    """Right-handed view matrix looking from `eye` toward `target`."""
    eye = np.asarray(eye, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    up = np.asarray(up, dtype=np.float64)

    forward = target - eye
    forward /= np.linalg.norm(forward)
    side = np.cross(forward, up)
    side /= np.linalg.norm(side)
    true_up = np.cross(side, forward)

    m = np.eye(4, dtype=np.float32)
    m[0, :3] = side
    m[1, :3] = true_up
    m[2, :3] = -forward
    m[3, 0] = -np.dot(side, eye)
    m[3, 1] = -np.dot(true_up, eye)
    m[3, 2] = np.dot(forward, eye)
    return m


def rotation_y(angle_rad):
    c, s = np.cos(angle_rad), np.sin(angle_rad)
    m = np.eye(4, dtype=np.float32)
    m[0, 0], m[0, 2] = c, -s
    m[2, 0], m[2, 2] = s, c
    return m
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_mathutil.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add ui/mathutil.py tests/unit/test_mathutil.py
git commit -m "feat: add column-major matrix helpers for the GL pipeline"
```

---

### Task 5: Load C-alpha traces from CIF

**Files:**
- Create: `tests/fixtures/structures/minimal.cif`
- Create: `ui/geometry.py`
- Test: `tests/unit/test_geometry_load.py`

**Interfaces:**
- Consumes: gemmi.
- Produces: `load_ca_trace(cif_path) -> CaTrace`, a dataclass with `.coords: (N,3) float32`, `.plddt: (N,) float32`, `.chain_ids: list[str]`, `.n_residues: int`; raises `GeometryError` on a structure with no C-alpha atoms.

pLDDT is read from the B-factor column, which is where every AlphaFold-family predictor writes per-residue confidence.

- [ ] **Step 1: Write the fixture and the failing test**

Create `tests/fixtures/structures/minimal.cif` — five C-alpha atoms in a bent chain across two chains, with descending confidence so the color ramp is exercised:

```
data_minimal
#
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_alt_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_entity_id
_atom_site.label_seq_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.occupancy
_atom_site.B_iso_or_equiv
_atom_site.auth_seq_id
_atom_site.auth_asym_id
ATOM 1 C CA . ALA A 1 1 0.000 0.000 0.000 1.00 95.00 1 A
ATOM 2 C CA . GLY A 1 2 3.800 0.000 0.000 1.00 80.00 2 A
ATOM 3 C CA . SER A 1 3 7.600 2.000 0.000 1.00 60.00 3 A
ATOM 4 C CA . VAL A 1 4 11.400 2.000 1.500 1.00 40.00 4 A
ATOM 5 C CA . LEU B 2 1 15.200 0.000 1.500 1.00 88.00 1 B
#
```

Create `tests/unit/test_geometry_load.py`:

```python
import numpy as np
import pytest

from ui.geometry import GeometryError, load_ca_trace

FIXTURE = "tests/fixtures/structures/minimal.cif"


def test_loads_every_ca_atom():
    trace = load_ca_trace(FIXTURE)
    assert trace.n_residues == 5
    assert trace.coords.shape == (5, 3)
    assert trace.coords.dtype == np.float32


def test_coordinates_match_the_file():
    trace = load_ca_trace(FIXTURE)
    np.testing.assert_allclose(trace.coords[0], [0.0, 0.0, 0.0], atol=1e-5)
    np.testing.assert_allclose(trace.coords[3], [11.4, 2.0, 1.5], atol=1e-5)


def test_plddt_comes_from_the_b_factor_column():
    trace = load_ca_trace(FIXTURE)
    np.testing.assert_allclose(trace.plddt, [95.0, 80.0, 60.0, 40.0, 88.0], atol=1e-4)


def test_chain_ids_are_recorded_per_residue():
    trace = load_ca_trace(FIXTURE)
    assert trace.chain_ids == ["A", "A", "A", "A", "B"]


def test_missing_file_raises_geometry_error():
    with pytest.raises(GeometryError, match="could not read"):
        load_ca_trace("tests/fixtures/structures/does-not-exist.cif")


def test_structure_without_ca_atoms_raises(tmp_path):
    empty = tmp_path / "empty.cif"
    empty.write_text("data_empty\n#\n")
    with pytest.raises(GeometryError, match="no C-alpha atoms"):
        load_ca_trace(str(empty))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_geometry_load.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ui.geometry'`

- [ ] **Step 3: Write minimal implementation**

Create `ui/geometry.py`:

```python
"""Turn predicted structures into renderable geometry.

Pure numpy and gemmi — no GL, no GTK — so all of it is unit-testable.
"""

from dataclasses import dataclass

import gemmi
import numpy as np


class GeometryError(Exception):
    """A structure could not be read or converted to geometry."""


@dataclass
class CaTrace:
    """The C-alpha backbone of a predicted structure."""

    coords: np.ndarray      # (N, 3) float32
    plddt: np.ndarray       # (N,) float32, 0-100
    chain_ids: list

    @property
    def n_residues(self):
        return len(self.coords)


def load_ca_trace(cif_path):
    """Read C-alpha positions and per-residue pLDDT from a CIF file.

    pLDDT is taken from the B-factor column, which is where AlphaFold-family
    predictors (including everything tt-bio serves) write per-residue confidence.
    """
    try:
        structure = gemmi.read_structure(str(cif_path))
    except Exception as exc:
        raise GeometryError(f"could not read {cif_path}: {exc}") from exc

    if len(structure) == 0:
        raise GeometryError(f"{cif_path} contains no C-alpha atoms")

    coords, plddt, chain_ids = [], [], []
    for chain in structure[0]:
        for residue in chain:
            atom = residue.find_atom("CA", "*")
            if atom is None:
                continue
            coords.append([atom.pos.x, atom.pos.y, atom.pos.z])
            plddt.append(atom.b_iso)
            chain_ids.append(chain.name)

    if not coords:
        raise GeometryError(f"{cif_path} contains no C-alpha atoms")

    return CaTrace(
        coords=np.asarray(coords, dtype=np.float32),
        plddt=np.asarray(plddt, dtype=np.float32),
        chain_ids=chain_ids,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_geometry_load.py -v`
Expected: PASS, 6 tests

If `test_structure_without_ca_atoms_raises` fails because gemmi returns zero models for an empty file, the `len(structure) == 0` guard already covers it — confirm the raised message still matches `no C-alpha atoms` and adjust that guard's message rather than the test.

- [ ] **Step 5: Commit**

```bash
git add ui/geometry.py tests/fixtures/structures/ tests/unit/test_geometry_load.py
git commit -m "feat: load C-alpha traces and pLDDT from CIF files"
```

---

### Task 6: Spline, tube mesh, and confidence coloring

**Files:**
- Modify: `ui/geometry.py` (append)
- Test: `tests/unit/test_geometry_mesh.py`

**Interfaces:**
- Consumes: `ui.geometry.CaTrace`, `GeometryError`.
- Produces: `catmull_rom(points, samples_per_segment=8) -> (M,3) float32`; `tube_mesh(centerline, radius=1.6, sides=10) -> (vertices (V,3) float32, normals (V,3) float32, indices (I,) uint32)`; `resample_scalar(values, n_out) -> (n_out,) float32`; `plddt_colors(plddt) -> (N,3) float32` RGB in 0–1.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_geometry_mesh.py`:

```python
import numpy as np
import pytest

from ui.geometry import catmull_rom, plddt_colors, resample_scalar, tube_mesh


def test_spline_passes_through_control_points():
    pts = np.array([[0.0, 0, 0], [1.0, 1, 0], [2.0, 0, 0], [3.0, 1, 0]])
    curve = catmull_rom(pts, samples_per_segment=4)
    # Every control point should appear somewhere on the curve.
    for p in pts:
        assert np.min(np.linalg.norm(curve - p, axis=1)) < 1e-4


def test_spline_sample_count_is_predictable():
    pts = np.zeros((5, 3))
    pts[:, 0] = np.arange(5)
    curve = catmull_rom(pts, samples_per_segment=4)
    # 4 segments between 5 points, 4 samples each, plus the final endpoint.
    assert len(curve) == 4 * 4 + 1


def test_spline_of_two_points_is_a_line():
    pts = np.array([[0.0, 0, 0], [10.0, 0, 0]])
    curve = catmull_rom(pts, samples_per_segment=5)
    assert np.allclose(curve[:, 1:], 0.0, atol=1e-6)
    assert curve[0][0] < curve[-1][0]


def test_spline_of_single_point_returns_that_point():
    curve = catmull_rom(np.array([[1.0, 2.0, 3.0]]), samples_per_segment=4)
    assert curve.shape == (1, 3)


def test_tube_mesh_vertex_and_index_counts():
    centerline = np.zeros((10, 3), dtype=np.float32)
    centerline[:, 0] = np.arange(10)
    verts, norms, idx = tube_mesh(centerline, radius=1.0, sides=8)
    assert verts.shape == (10 * 8, 3)
    assert norms.shape == (10 * 8, 3)
    assert idx.shape == ((10 - 1) * 8 * 6,)
    assert idx.dtype == np.uint32


def test_tube_mesh_normals_are_unit_length():
    centerline = np.zeros((6, 3), dtype=np.float32)
    centerline[:, 0] = np.arange(6)
    _, norms, _ = tube_mesh(centerline, radius=2.0, sides=6)
    np.testing.assert_allclose(np.linalg.norm(norms, axis=1), 1.0, atol=1e-5)


def test_tube_mesh_vertices_sit_at_radius_from_a_straight_axis():
    centerline = np.zeros((5, 3), dtype=np.float32)
    centerline[:, 0] = np.arange(5) * 2.0
    radius = 1.7
    verts, _, _ = tube_mesh(centerline, radius=radius, sides=8)
    # Axis is X, so distance in the YZ plane must equal the radius.
    np.testing.assert_allclose(
        np.linalg.norm(verts[:, 1:], axis=1), radius, atol=1e-4
    )


def test_tube_mesh_indices_stay_in_range():
    centerline = np.zeros((7, 3), dtype=np.float32)
    centerline[:, 2] = np.arange(7)
    verts, _, idx = tube_mesh(centerline, sides=5)
    assert idx.max() < len(verts)


def test_tube_mesh_rejects_degenerate_centerline():
    with pytest.raises(Exception):
        tube_mesh(np.zeros((1, 3), dtype=np.float32))


def test_resample_scalar_stretches_values_to_new_length():
    out = resample_scalar(np.array([0.0, 10.0]), 5)
    np.testing.assert_allclose(out, [0.0, 2.5, 5.0, 7.5, 10.0], atol=1e-5)


def test_resample_scalar_preserves_endpoints():
    values = np.array([90.0, 50.0, 70.0])
    out = resample_scalar(values, 17)
    assert np.isclose(out[0], 90.0)
    assert np.isclose(out[-1], 70.0)


def test_plddt_colors_follow_the_alphafold_ramp():
    colors = plddt_colors(np.array([95.0, 80.0, 60.0, 30.0]))
    assert colors.shape == (4, 3)
    np.testing.assert_allclose(colors[0], np.array([0x00, 0x53, 0xD6]) / 255.0, atol=1e-3)
    np.testing.assert_allclose(colors[1], np.array([0x65, 0xCB, 0xF3]) / 255.0, atol=1e-3)
    np.testing.assert_allclose(colors[2], np.array([0xFF, 0xDB, 0x13]) / 255.0, atol=1e-3)
    np.testing.assert_allclose(colors[3], np.array([0xFF, 0x7D, 0x45]) / 255.0, atol=1e-3)


def test_plddt_colors_are_in_unit_range():
    colors = plddt_colors(np.linspace(0.0, 100.0, 50))
    assert colors.min() >= 0.0 and colors.max() <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_geometry_mesh.py -v`
Expected: FAIL — `ImportError: cannot import name 'catmull_rom' from 'ui.geometry'`

- [ ] **Step 3: Write minimal implementation**

Append to `ui/geometry.py`:

```python
# ── Curve and mesh construction ──────────────────────────────────────────

# AlphaFold's confidence ramp. Domain visitors read these colors fluently, so
# we use the convention rather than inventing a brand-consistent one.
_PLDDT_STOPS = (
    (90.0, (0x00, 0x53, 0xD6)),   # very high
    (70.0, (0x65, 0xCB, 0xF3)),   # confident
    (50.0, (0xFF, 0xDB, 0x13)),   # low
    (0.0,  (0xFF, 0x7D, 0x45)),   # very low
)


def catmull_rom(points, samples_per_segment=8):
    """Sample a Catmull-Rom spline through every point of a polyline.

    Endpoints are duplicated so the curve spans the full polyline rather than
    starting at the second control point.
    """
    p = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(p) < 2:
        return p.astype(np.float32)

    ext = np.vstack([p[0], p, p[-1]])
    out = []
    for i in range(len(ext) - 3):
        p0, p1, p2, p3 = ext[i], ext[i + 1], ext[i + 2], ext[i + 3]
        for s in range(samples_per_segment):
            t = s / samples_per_segment
            t2, t3 = t * t, t * t * t
            out.append(0.5 * (
                (2.0 * p1)
                + (-p0 + p2) * t
                + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
                + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
            ))
    out.append(p[-1])
    return np.asarray(out, dtype=np.float32)


def tube_mesh(centerline, radius=1.6, sides=10):
    """Sweep a circular cross-section along a centerline into a closed tube.

    Uses parallel transport to carry the cross-section frame along the curve,
    which avoids the twisting that a fixed reference vector produces on curved
    backbones.
    """
    c = np.asarray(centerline, dtype=np.float64).reshape(-1, 3)
    n = len(c)
    if n < 2:
        raise GeometryError(f"a tube needs at least 2 centerline points, got {n}")

    tangents = np.gradient(c, axis=0)
    lengths = np.linalg.norm(tangents, axis=1, keepdims=True)
    tangents = tangents / np.maximum(lengths, 1e-9)

    # Seed the frame with any vector not parallel to the first tangent.
    ref = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(tangents[0], ref))) > 0.9:
        ref = np.array([0.0, 1.0, 0.0])
    normal = np.cross(tangents[0], ref)
    normal /= np.linalg.norm(normal)

    angles = np.linspace(0.0, 2.0 * np.pi, sides, endpoint=False)
    verts = np.zeros((n, sides, 3))
    norms = np.zeros((n, sides, 3))

    for i in range(n):
        if i > 0:
            # Parallel transport: project the previous normal perpendicular to
            # the new tangent instead of recomputing it from scratch.
            normal = normal - tangents[i] * np.dot(normal, tangents[i])
            length = np.linalg.norm(normal)
            if length < 1e-6:
                normal = np.cross(tangents[i], ref)
                length = np.linalg.norm(normal)
            normal = normal / length
        binormal = np.cross(tangents[i], normal)
        for j, a in enumerate(angles):
            direction = np.cos(a) * normal + np.sin(a) * binormal
            norms[i, j] = direction
            verts[i, j] = c[i] + radius * direction

    indices = []
    for i in range(n - 1):
        for j in range(sides):
            jn = (j + 1) % sides
            a = i * sides + j
            b = i * sides + jn
            d = (i + 1) * sides + j
            e = (i + 1) * sides + jn
            indices += [a, d, b, b, d, e]

    return (
        verts.reshape(-1, 3).astype(np.float32),
        norms.reshape(-1, 3).astype(np.float32),
        np.asarray(indices, dtype=np.uint32),
    )


def resample_scalar(values, n_out):
    """Stretch a per-residue scalar onto a denser (or sparser) sample count."""
    v = np.asarray(values, dtype=np.float64).ravel()
    if len(v) == 1:
        return np.full(n_out, v[0], dtype=np.float32)
    source = np.linspace(0.0, 1.0, len(v))
    target = np.linspace(0.0, 1.0, n_out)
    return np.interp(target, source, v).astype(np.float32)


def plddt_colors(plddt):
    """Map pLDDT values (0-100) to RGB in 0-1 using the AlphaFold ramp."""
    v = np.asarray(plddt, dtype=np.float64).ravel()
    out = np.zeros((len(v), 3), dtype=np.float32)
    for threshold, rgb in _PLDDT_STOPS:
        mask = v >= threshold
        # Later (lower) stops only fill values no earlier stop claimed.
        unset = np.all(out == 0.0, axis=1)
        out[mask & unset] = np.asarray(rgb, dtype=np.float32) / 255.0
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_geometry_mesh.py -v`
Expected: PASS, 13 tests

- [ ] **Step 5: Run the whole headless suite and commit**

```bash
python3 -m pytest tests/unit -v
git add ui/geometry.py tests/unit/test_geometry_mesh.py
git commit -m "feat: add spline, tube mesh, and pLDDT color ramp"
```

Expected: all tests from Tasks 1–6 pass (49 total).

---

### Task 7: GL scaffolding and an empty viewer widget

**Files:**
- Create: `ui/shaders.py`
- Create: `ui/glutil.py`
- Create: `ui/viewer.py`
- Create: `ui/app.py`
- Test: manual (requires a display)

**Interfaces:**
- Consumes: `ui.mathutil`.
- Produces: `shaders.POINT_VERT`, `POINT_FRAG`, `RIBBON_VERT`, `RIBBON_FRAG` (GLSL strings); `glutil.compile_program(vert_src, frag_src) -> int`; `glutil.GLError`; `viewer.StructureViewer(Gtk.GLArea)` with `.set_points(coords, opacity=1.0)`, `.set_ribbon(vertices, normals, colors, indices)`, `.set_blend(t)`, `.clear_structure()`; `app.DemoApp(Gtk.Application)` and `app.main(argv)`.

This task establishes a window that opens, initializes GL, and clears to the brand dark base. Rendering arrives in Tasks 8–9.

- [ ] **Step 1: Write the shader sources**

Create `ui/shaders.py`:

```python
"""GLSL sources for the two draw modes.

OpenGL 3.3 core: the QB2 reports 4.6 but 3.3 keeps the renderer usable on
developer laptops without changing anything.
"""

POINT_VERT = """
#version 330 core
layout(location = 0) in vec3 in_position;

uniform mat4 u_mvp;
uniform float u_point_size;

void main() {
    gl_Position = u_mvp * vec4(in_position, 1.0);
    // Shrink distant points so depth reads correctly without depth sorting.
    gl_PointSize = u_point_size / max(gl_Position.w, 0.1);
}
"""

POINT_FRAG = """
#version 330 core
out vec4 frag_color;

uniform vec3 u_color;
uniform float u_opacity;

void main() {
    // Carve a soft disc out of the square point sprite.
    vec2 offset = gl_PointCoord - vec2(0.5);
    float r = length(offset);
    if (r > 0.5) discard;
    float edge = smoothstep(0.5, 0.35, r);
    frag_color = vec4(u_color, u_opacity * edge);
}
"""

RIBBON_VERT = """
#version 330 core
layout(location = 0) in vec3 in_position;
layout(location = 1) in vec3 in_normal;
layout(location = 2) in vec3 in_color;

uniform mat4 u_mvp;
uniform mat4 u_model;

out vec3 v_normal;
out vec3 v_color;

void main() {
    gl_Position = u_mvp * vec4(in_position, 1.0);
    v_normal = mat3(u_model) * in_normal;
    v_color = in_color;
}
"""

RIBBON_FRAG = """
#version 330 core
in vec3 v_normal;
in vec3 v_color;
out vec4 frag_color;

uniform float u_opacity;

void main() {
    vec3 n = normalize(v_normal);
    vec3 light = normalize(vec3(0.4, 0.8, 0.6));
    float diffuse = max(dot(n, light), 0.0);
    // Rim light keeps the silhouette readable against the dark background.
    float rim = pow(1.0 - abs(n.z), 2.0) * 0.35;
    vec3 shaded = v_color * (0.35 + 0.65 * diffuse) + vec3(rim) * 0.6;
    frag_color = vec4(shaded, u_opacity);
}
"""
```

- [ ] **Step 2: Write the GL helper**

Create `ui/glutil.py`:

```python
"""Shader compilation helpers. Requires a current GL context."""

from OpenGL import GL


class GLError(Exception):
    """A shader failed to compile or link."""


def _compile_shader(source, kind):
    shader = GL.glCreateShader(kind)
    GL.glShaderSource(shader, source)
    GL.glCompileShader(shader)
    if not GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS):
        log = GL.glGetShaderInfoLog(shader).decode("utf-8", "replace")
        GL.glDeleteShader(shader)
        name = "vertex" if kind == GL.GL_VERTEX_SHADER else "fragment"
        raise GLError(f"{name} shader failed to compile:\n{log}")
    return shader


def compile_program(vert_src, frag_src):
    """Compile and link a shader program, returning its GL handle."""
    vert = _compile_shader(vert_src, GL.GL_VERTEX_SHADER)
    frag = _compile_shader(frag_src, GL.GL_FRAGMENT_SHADER)
    program = GL.glCreateProgram()
    GL.glAttachShader(program, vert)
    GL.glAttachShader(program, frag)
    GL.glLinkProgram(program)
    GL.glDeleteShader(vert)
    GL.glDeleteShader(frag)
    if not GL.glGetProgramiv(program, GL.GL_LINK_STATUS):
        log = GL.glGetProgramInfoLog(program).decode("utf-8", "replace")
        GL.glDeleteProgram(program)
        raise GLError(f"program failed to link:\n{log}")
    return program
```

- [ ] **Step 3: Write the viewer skeleton**

Create `ui/viewer.py`:

```python
"""The structure viewer: a GtkGLArea that draws points and ribbons."""

import logging

import gi

gi.require_version("Gtk", "4.0")

import numpy as np
from gi.repository import Gtk
from OpenGL import GL

from ui import mathutil, shaders
from ui.glutil import GLError, compile_program

log = logging.getLogger(__name__)

# Tenstorrent dark base — the background the whole demo sits on.
BACKGROUND = (0x09 / 255.0, 0x22 / 255.0, 0x21 / 255.0, 1.0)
# Teal, used for the diffusion point cloud before confidence data exists.
POINT_COLOR = (0x74 / 255.0, 0xC5 / 255.0, 0xDF / 255.0)


class StructureViewer(Gtk.GLArea):
    """Renders a diffusion point cloud, a finished ribbon, or a blend."""

    def __init__(self):
        super().__init__()
        self.set_has_depth_buffer(True)
        self.set_auto_render(True)

        self._point_program = None
        self._ribbon_program = None
        self._ready = False

        self._spin = 0.0
        self._blend = 0.0          # 0 = points only, 1 = ribbon only
        self._center = np.zeros(3, dtype=np.float32)
        self._extent = 20.0

        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.connect("render", self._on_render)

    # ── lifecycle ────────────────────────────────────────────────────────

    def _on_realize(self, _area):
        self.make_current()
        if self.get_error() is not None:
            log.error("GL area failed to realize: %s", self.get_error())
            return
        try:
            self._point_program = compile_program(
                shaders.POINT_VERT, shaders.POINT_FRAG)
            self._ribbon_program = compile_program(
                shaders.RIBBON_VERT, shaders.RIBBON_FRAG)
        except GLError:
            log.exception("shader setup failed; viewer will stay blank")
            return

        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glEnable(GL.GL_PROGRAM_POINT_SIZE)
        self._ready = True

    def _on_unrealize(self, _area):
        self.make_current()
        for program in (self._point_program, self._ribbon_program):
            if program:
                GL.glDeleteProgram(program)
        self._point_program = self._ribbon_program = None
        self._ready = False

    # ── camera ───────────────────────────────────────────────────────────

    def _mvp(self):
        width = max(self.get_width(), 1)
        height = max(self.get_height(), 1)
        distance = self._extent * 2.6

        model = mathutil.rotation_y(self._spin)
        eye = np.array([0.0, self._extent * 0.35, distance])
        view = mathutil.look_at(eye, np.zeros(3), np.array([0.0, 1.0, 0.0]))
        proj = mathutil.perspective(45.0, width / height, 0.5, distance * 4.0)

        # Column-major storage means composition reads right-to-left when
        # transposed, so multiply in this order to get proj * view * model.
        return (model @ view @ proj).astype(np.float32), model

    def _on_render(self, _area, _context):
        GL.glClearColor(*BACKGROUND)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        return True
```

- [ ] **Step 4: Write the application shell**

Create `ui/app.py`:

```python
"""GTK application shell for the tt-bio demo."""

import argparse
import logging
import sys

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from ui.viewer import StructureViewer

log = logging.getLogger(__name__)


class DemoApp(Gtk.Application):
    def __init__(self, socket_path=None):
        super().__init__(application_id="com.tenstorrent.ttbiodemo")
        self.socket_path = socket_path
        self.viewer = None

    def do_activate(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("tt-bio")
        window.set_default_size(1280, 800)

        self.viewer = StructureViewer()
        window.set_child(self.viewer)
        window.present()


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="tt-bio demo UI")
    parser.add_argument("--socket", default=None,
                        help="runner socket path; omit to show an empty viewer")
    args = parser.parse_args(argv)
    return DemoApp(socket_path=args.socket).run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 5: Verify manually**

Run: `python3 -m ui.app`

Expected: a 1280×800 window titled "tt-bio" opens, filled with the dark forest-teal brand background (`#092221`). No warnings about shader compilation in the terminal. Close the window to exit.

If the window is black rather than dark teal, the shaders failed to compile — check the logged exception; that is the failure this step exists to catch.

- [ ] **Step 6: Commit**

```bash
git add ui/shaders.py ui/glutil.py ui/viewer.py ui/app.py
git commit -m "feat: add GL scaffolding and viewer window"
```

---

### Task 8: Render the diffusion point cloud

**Files:**
- Modify: `ui/viewer.py`
- Test: manual (requires a display)

**Interfaces:**
- Consumes: `StructureViewer` from Task 7.
- Produces: `StructureViewer.set_points(coords: np.ndarray, opacity: float = 1.0)` — uploads an (N,3) float32 array and triggers a redraw; recenters the camera on first upload.

- [ ] **Step 1: Add point buffer management to the viewer**

In `ui/viewer.py`, add to `__init__` (after `self._extent = 20.0`):

```python
        self._point_vao = None
        self._point_vbo = None
        self._point_count = 0
        self._point_opacity = 1.0
        self._pending_points = None
```

Add these methods to `StructureViewer`:

```python
    # ── point cloud ──────────────────────────────────────────────────────

    def set_points(self, coords, opacity=1.0):
        """Upload a diffusion frame. Safe to call before GL is realized."""
        arr = np.ascontiguousarray(coords, dtype=np.float32).reshape(-1, 3)
        self._pending_points = arr
        self._point_opacity = opacity
        self._frame_camera(arr)
        self.queue_render()

    def _frame_camera(self, coords):
        """Center and scale the camera to fit the given coordinates."""
        if len(coords) == 0:
            return
        self._center = coords.mean(axis=0)
        spread = float(np.abs(coords - self._center).max())
        # Ease toward the new extent so a noisy first frame doesn't snap the
        # camera around as the cloud contracts.
        self._extent = max(self._extent * 0.8 + spread * 0.2, 5.0)

    def _upload_points(self):
        coords = self._pending_points
        self._pending_points = None

        if self._point_vao is None:
            self._point_vao = GL.glGenVertexArrays(1)
            self._point_vbo = GL.glGenBuffers(1)

        GL.glBindVertexArray(self._point_vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._point_vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, coords.nbytes, coords, GL.GL_DYNAMIC_DRAW)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        GL.glBindVertexArray(0)
        self._point_count = len(coords)

    def _draw_points(self, mvp, opacity):
        if not self._point_count or opacity <= 0.0:
            return
        GL.glUseProgram(self._point_program)
        GL.glUniformMatrix4fv(
            GL.glGetUniformLocation(self._point_program, "u_mvp"),
            1, GL.GL_FALSE, mvp)
        GL.glUniform1f(
            GL.glGetUniformLocation(self._point_program, "u_point_size"),
            self._extent * 3.5)
        GL.glUniform3f(
            GL.glGetUniformLocation(self._point_program, "u_color"), *POINT_COLOR)
        GL.glUniform1f(
            GL.glGetUniformLocation(self._point_program, "u_opacity"), opacity)
        GL.glBindVertexArray(self._point_vao)
        GL.glDrawArrays(GL.GL_POINTS, 0, self._point_count)
        GL.glBindVertexArray(0)
```

Replace `_on_render` with:

```python
    def _on_render(self, _area, _context):
        GL.glClearColor(*BACKGROUND)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        if not self._ready:
            return True

        if self._pending_points is not None:
            self._upload_points()

        mvp, _model = self._mvp()
        self._draw_points(mvp, self._point_opacity * (1.0 - self._blend))
        return True
```

Also update `_mvp` to translate by the structure center — replace the `model` line with:

```python
        model = mathutil.rotation_y(self._spin)
        model[3, :3] -= self._center @ model[:3, :3]
```

- [ ] **Step 2: Add a playback harness to the app**

In `ui/app.py`, replace `do_activate` and add the client wiring:

```python
    def do_activate(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("tt-bio")
        window.set_default_size(1280, 800)

        self.viewer = StructureViewer()
        window.set_child(self.viewer)
        window.present()

        if self.socket_path:
            self._start_client()

    def _start_client(self):
        self._frames = LatestFrame()
        self._client = EventClient(self.socket_path, self._on_event)
        self._client.start()
        # Drain the frame buffer on the main loop at display rate; the client
        # thread must never touch GTK directly.
        GLib.timeout_add(33, self._drain_frames)

    def _on_event(self, event):
        kind = event["type"]
        if kind == "frame":
            self._frames.put(event)
        else:
            GLib.idle_add(self._handle_event, event)

    def _handle_event(self, event):
        kind = event["type"]
        if kind == "job_start":
            log.info("folding %s (%s residues) on card %s",
                     event.get("target_id"), event.get("n_residues"),
                     event.get("card"))
        elif kind == "stage":
            log.info("stage %s %.0f%%", event.get("stage"),
                     100.0 * event.get("frac", 0.0))
        elif kind == "job_done":
            log.info("done in %.2fs", event.get("wall_s", 0.0))
        return False

    def _drain_frames(self):
        frame = self._frames.take()
        if frame is not None:
            self.viewer.set_points(unpack_coords(frame["coords_b64"]))
        return True
```

Add these imports at the top of `ui/app.py`:

```python
from gi.repository import GLib, Gtk

from protocol.events import unpack_coords
from ui.client import EventClient, LatestFrame
from ui.viewer import StructureViewer
```

(Replace the existing `from gi.repository import Gtk` line.)

- [ ] **Step 3: Verify manually**

In one terminal, serve the fixture at true recorded pace:

```bash
python3 - <<'PY'
import time
from runner.mock import MockRunner, load_stream
r = MockRunner("/tmp/ttbio-demo.sock", load_stream("tests/fixtures/streams/short_fold.jsonl"), speed=1.0)
r.start()
print("serving on /tmp/ttbio-demo.sock; Ctrl-C to stop")
try:
    time.sleep(3600)
except KeyboardInterrupt:
    r.stop()
PY
```

In another:

```bash
python3 -m ui.app --socket /tmp/ttbio-demo.sock
```

Expected: a teal point cloud appears, scattered wide, and over roughly six frames contracts into a straight line of twelve evenly spaced points along the X axis. The terminal logs `folding synthetic (12 residues) on card 0`, two stage lines, and `done in 1.25s`.

If points render as squares rather than discs, `GL_PROGRAM_POINT_SIZE` is not enabled — confirm the `glEnable` call in `_on_realize`.

- [ ] **Step 4: Commit**

```bash
git add ui/viewer.py ui/app.py
git commit -m "feat: render diffusion frames as a point cloud"
```

---

### Task 9: Render the finished ribbon

**Files:**
- Modify: `ui/viewer.py`
- Modify: `ui/app.py`
- Test: manual (requires a display)

**Interfaces:**
- Consumes: `ui.geometry.load_ca_trace`, `catmull_rom`, `tube_mesh`, `resample_scalar`, `plddt_colors`; `StructureViewer` from Task 8.
- Produces: `StructureViewer.set_ribbon(vertices, normals, colors, indices)`; `ui.geometry.ribbon_from_cif(cif_path, samples_per_segment=8, radius=1.6, sides=10) -> (vertices, normals, colors, indices)`.

- [ ] **Step 1: Write the failing test for the composed helper**

Append to `tests/unit/test_geometry_mesh.py`:

```python
def test_ribbon_from_cif_produces_consistent_buffers():
    from ui.geometry import ribbon_from_cif

    verts, norms, colors, idx = ribbon_from_cif(
        "tests/fixtures/structures/minimal.cif", samples_per_segment=4, sides=6
    )
    assert verts.shape == norms.shape == colors.shape
    assert verts.shape[1] == 3
    assert len(verts) % 6 == 0
    assert idx.max() < len(verts)
    assert colors.min() >= 0.0 and colors.max() <= 1.0
    assert verts.dtype == np.float32 and idx.dtype == np.uint32
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_geometry_mesh.py::test_ribbon_from_cif_produces_consistent_buffers -v`
Expected: FAIL — `ImportError: cannot import name 'ribbon_from_cif'`

- [ ] **Step 3: Implement the composed helper**

Append to `ui/geometry.py`:

```python
def ribbon_from_cif(cif_path, samples_per_segment=8, radius=1.6, sides=10):
    """Read a CIF and build everything the ribbon renderer needs.

    Returns (vertices, normals, colors, indices) with one color per vertex,
    interpolated from per-residue pLDDT along the spline.
    """
    trace = load_ca_trace(cif_path)
    centerline = catmull_rom(trace.coords, samples_per_segment)
    verts, norms, indices = tube_mesh(centerline, radius=radius, sides=sides)

    # One pLDDT value per centerline sample, then repeated around each ring.
    along = resample_scalar(trace.plddt, len(centerline))
    ring_colors = plddt_colors(along)
    colors = np.repeat(ring_colors, sides, axis=0).astype(np.float32)

    return verts, norms, colors, indices
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_geometry_mesh.py -v`
Expected: PASS, 14 tests

- [ ] **Step 5: Add ribbon rendering to the viewer**

In `ui/viewer.py`, add to `__init__`:

```python
        self._ribbon_vao = None
        self._ribbon_buffers = None
        self._ribbon_index_count = 0
        self._pending_ribbon = None
```

Add these methods:

```python
    # ── ribbon ───────────────────────────────────────────────────────────

    def set_ribbon(self, vertices, normals, colors, indices):
        """Upload a finished structure. Safe to call before GL is realized."""
        self._pending_ribbon = (
            np.ascontiguousarray(vertices, dtype=np.float32),
            np.ascontiguousarray(normals, dtype=np.float32),
            np.ascontiguousarray(colors, dtype=np.float32),
            np.ascontiguousarray(indices, dtype=np.uint32),
        )
        self._frame_camera(self._pending_ribbon[0])
        self.queue_render()

    def clear_structure(self):
        self._point_count = 0
        self._ribbon_index_count = 0
        self._blend = 0.0
        self.queue_render()

    def _upload_ribbon(self):
        verts, norms, colors, indices = self._pending_ribbon
        self._pending_ribbon = None

        if self._ribbon_vao is None:
            self._ribbon_vao = GL.glGenVertexArrays(1)
            self._ribbon_buffers = GL.glGenBuffers(4)

        vbo_pos, vbo_norm, vbo_color, ebo = self._ribbon_buffers
        GL.glBindVertexArray(self._ribbon_vao)

        for location, buffer, data in (
            (0, vbo_pos, verts), (1, vbo_norm, norms), (2, vbo_color, colors)
        ):
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, buffer)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
            GL.glEnableVertexAttribArray(location)
            GL.glVertexAttribPointer(location, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)

        GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, ebo)
        GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices,
                        GL.GL_STATIC_DRAW)
        GL.glBindVertexArray(0)
        self._ribbon_index_count = len(indices)

    def _draw_ribbon(self, mvp, model, opacity):
        if not self._ribbon_index_count or opacity <= 0.0:
            return
        GL.glUseProgram(self._ribbon_program)
        GL.glUniformMatrix4fv(
            GL.glGetUniformLocation(self._ribbon_program, "u_mvp"),
            1, GL.GL_FALSE, mvp)
        GL.glUniformMatrix4fv(
            GL.glGetUniformLocation(self._ribbon_program, "u_model"),
            1, GL.GL_FALSE, model)
        GL.glUniform1f(
            GL.glGetUniformLocation(self._ribbon_program, "u_opacity"), opacity)
        GL.glBindVertexArray(self._ribbon_vao)
        GL.glDrawElements(GL.GL_TRIANGLES, self._ribbon_index_count,
                          GL.GL_UNSIGNED_INT, None)
        GL.glBindVertexArray(0)
```

Replace `_on_render` with:

```python
    def _on_render(self, _area, _context):
        GL.glClearColor(*BACKGROUND)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        if not self._ready:
            return True

        if self._pending_points is not None:
            self._upload_points()
        if self._pending_ribbon is not None:
            self._upload_ribbon()

        mvp, model = self._mvp()
        self._draw_ribbon(mvp, model, self._blend)
        self._draw_points(mvp, self._point_opacity * (1.0 - self._blend))
        return True
```

The ribbon draws before the points so the translucent points blend over it correctly during the cross-fade.

- [ ] **Step 6: Load the ribbon on job_done**

In `ui/app.py`, extend `_handle_event`'s `job_done` branch:

```python
        elif kind == "job_done":
            log.info("done in %.2fs", event.get("wall_s", 0.0))
            cif_path = event.get("cif_path")
            if cif_path:
                try:
                    verts, norms, colors, idx = ribbon_from_cif(cif_path)
                except GeometryError:
                    log.exception("could not build ribbon for %s", cif_path)
                else:
                    self.viewer.set_ribbon(verts, norms, colors, idx)
                    self.viewer.set_blend(1.0)
```

Add the import:

```python
from ui.geometry import GeometryError, ribbon_from_cif
```

And add a temporary `set_blend` to `StructureViewer` (Task 10 animates it):

```python
    def set_blend(self, t):
        """0 shows only points, 1 only the ribbon."""
        self._blend = float(np.clip(t, 0.0, 1.0))
        self.queue_render()
```

- [ ] **Step 7: Verify manually**

Serve the fixture and run the app exactly as in Task 8, Step 3.

Expected: the point cloud contracts as before, then snaps to a colored tube following the five C-alpha positions of `minimal.cif` — blue at one end grading through pale blue and yellow to orange, matching the descending pLDDT values in the fixture.

- [ ] **Step 8: Commit**

```bash
git add ui/geometry.py ui/viewer.py ui/app.py tests/unit/test_geometry_mesh.py
git commit -m "feat: render finished structures as a pLDDT-colored ribbon"
```

---

### Task 10: Cross-fade, idle spin, and connection state

**Files:**
- Modify: `ui/viewer.py`
- Modify: `ui/app.py`
- Test: `tests/unit/test_blend.py` plus manual

**Interfaces:**
- Consumes: everything from Tasks 7–9.
- Produces: `ui.viewer.blend_step(current, target, dt, duration) -> float` (pure, testable); `StructureViewer.start_animation()`, `.stop_animation()`, `.begin_crossfade()`; `StructureViewer.connection_state` property accepting `"connected"`, `"disconnected"`, `"incompatible"`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_blend.py`:

```python
import pytest

from ui.viewer import blend_step


def test_blend_advances_toward_target():
    assert blend_step(0.0, 1.0, dt=0.25, duration=1.0) == pytest.approx(0.25)


def test_blend_reaches_target_exactly():
    assert blend_step(0.9, 1.0, dt=1.0, duration=1.0) == pytest.approx(1.0)


def test_blend_never_overshoots():
    assert blend_step(0.95, 1.0, dt=10.0, duration=1.0) == pytest.approx(1.0)


def test_blend_can_run_backwards():
    assert blend_step(1.0, 0.0, dt=0.5, duration=1.0) == pytest.approx(0.5)


def test_blend_backwards_clamps_at_zero():
    assert blend_step(0.1, 0.0, dt=5.0, duration=1.0) == pytest.approx(0.0)


def test_blend_holds_when_already_at_target():
    assert blend_step(1.0, 1.0, dt=0.5, duration=1.0) == pytest.approx(1.0)


def test_zero_duration_snaps_immediately():
    assert blend_step(0.0, 1.0, dt=0.001, duration=0.0) == pytest.approx(1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_blend.py -v`
Expected: FAIL — `ImportError: cannot import name 'blend_step'`

Note: importing `ui.viewer` pulls in GTK and PyOpenGL but does not create a GL context, so this test runs headlessly. If it fails at import in a truly headless CI container, mark the module with `pytest.importorskip("gi")`.

- [ ] **Step 3: Implement blending and animation**

Add to `ui/viewer.py` at module level (before the class):

```python
def blend_step(current, target, dt, duration):
    """Advance a 0-1 blend value toward `target`, never overshooting."""
    if duration <= 0.0:
        return target
    delta = dt / duration
    if target > current:
        return min(current + delta, target)
    return max(current - delta, target)
```

Add to `StructureViewer.__init__`:

```python
        self._blend_target = 0.0
        self._tick_id = None
        self._last_frame_time = None
        self.connection_state = "disconnected"
```

Add these methods:

```python
    # ── animation ────────────────────────────────────────────────────────

    CROSSFADE_SECONDS = 0.8
    SPIN_RATE = 0.35  # radians per second

    def start_animation(self):
        """Drive spin and cross-fade from GTK's frame clock."""
        if self._tick_id is None:
            self._tick_id = self.add_tick_callback(self._on_tick)

    def stop_animation(self):
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None

    def begin_crossfade(self):
        """Fade from the point cloud to the ribbon."""
        self._blend_target = 1.0

    def _on_tick(self, _widget, frame_clock):
        now = frame_clock.get_frame_time() / 1e6  # microseconds to seconds
        if self._last_frame_time is None:
            self._last_frame_time = now
            return True
        dt = now - self._last_frame_time
        self._last_frame_time = now

        self._spin += self.SPIN_RATE * dt
        self._blend = blend_step(
            self._blend, self._blend_target, dt, self.CROSSFADE_SECONDS)
        self.queue_render()
        return True
```

Replace the temporary `set_blend` from Task 9 with:

```python
    def set_blend(self, t):
        """Jump the blend immediately; prefer begin_crossfade() for transitions."""
        self._blend = self._blend_target = float(np.clip(t, 0.0, 1.0))
        self.queue_render()
```

Update `clear_structure` (added in Task 9) so it also resets the blend *target*, not
just the current value. Without this, the second fold of a session inherits
`_blend_target == 1.0` from the first, and the tick callback fades toward a ribbon
that `clear_structure` just discarded — the point cloud would vanish into an empty
frame. Replace the method with:

```python
    def clear_structure(self):
        self._point_count = 0
        self._ribbon_index_count = 0
        self._blend = 0.0
        self._blend_target = 0.0
        self.queue_render()
```

- [ ] **Step 4: Wire it into the app**

In `ui/app.py`:

Start the animation after presenting the window — add to `do_activate` before the `if self.socket_path:` line:

```python
        self.viewer.start_animation()
```

In `_handle_event`, change the `job_start` branch to reset for a new fold:

```python
        if kind == "job_start":
            log.info("folding %s (%s residues) on card %s",
                     event.get("target_id"), event.get("n_residues"),
                     event.get("card"))
            self.viewer.clear_structure()
```

And in `job_done`, replace `self.viewer.set_blend(1.0)` with:

```python
                    self.viewer.begin_crossfade()
```

Add connection-state reporting — pass `on_state_change` when constructing the client in `_start_client`:

```python
        self._client = EventClient(
            self.socket_path, self._on_event,
            on_state_change=lambda s: GLib.idle_add(self._on_state, s),
        )
```

And add the handler:

```python
    def _on_state(self, state):
        # The display must survive the runner dying: log the transition and
        # keep rendering whatever is already on screen.
        log.info("runner connection: %s", state)
        self.viewer.connection_state = state
        return False
```

- [ ] **Step 5: Run the full headless suite**

Run: `python3 -m pytest tests/unit -v`
Expected: PASS, 57 tests

- [ ] **Step 6: Verify manually**

Serve the fixture and run the app as in Task 8, Step 3.

Expected: the structure rotates slowly and continuously from the moment the window opens; the point cloud contracts over six frames; when the fold completes the points **fade out over about 0.8 seconds** as the colored ribbon fades in, rather than snapping. Stop the mock runner with Ctrl-C — the ribbon keeps rotating and the terminal logs `runner connection: disconnected`, with no crash and no blank window. Restart the runner and the log shows `connected` again and a new fold begins.

That last check is the spec's central resilience claim (§6), so treat a crash or blank window here as a task failure, not a cosmetic issue.

- [ ] **Step 7: Commit**

```bash
git add ui/viewer.py ui/app.py tests/unit/test_blend.py
git commit -m "feat: add cross-fade, idle spin, and connection-state handling"
```

---

## Definition of done

Phase 1–2 is complete when:

1. `python3 -m pytest tests/unit -v` passes with 57 tests.
2. `python3 -m ui.app --socket /tmp/ttbio-demo.sock` against the mock runner shows the full sequence: noise cloud → contraction → cross-fade → rotating pLDDT-colored ribbon.
3. Killing and restarting the mock runner mid-playback never blanks the window or raises to the terminal.

## What this phase deliberately leaves out

Handled in later plans, listed so no one implements them early:

- **Phase 3** — the real `tt-bio-demod`, job queue, preflight, `tt-smi` telemetry panel, pipeline progress widget, gallery, and the four-state machine.
- **Phase 4** — Debian packaging, the curated playlist and blurbs, thumbnails, and the soak test.
- **Trackball interaction** — the camera spins on its own; visitor-driven rotation is a Phase 3 decision tied to the gallery's input handling.
