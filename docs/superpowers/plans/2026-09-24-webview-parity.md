# Webview Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `webview/` (the browser companion to the GTK4 booth) to near-parity with
the native app: real per-chip telemetry, a real Tensix activity panel, a real Q&A queue and
gallery, a help screen, and real ribbon rendering of a finished fold — all sourced from the
same daemon and the same already-tested Python modules the GTK app already trusts, with no
daemon changes.

**Architecture:** `webview/bridge.py`'s `DaemonLink` becomes a hub multiple publishers push
into (relayed daemon events, a telemetry sampler, a ribbon cache), not just a relay. New
bridge-only event types (`telemetry`, `qa_queue`, `ribbon_ready`, `questions_catalog`,
`playlist_catalog`) ride the same SSE stream. Server-side logic is reused directly
(`ui.telemetry`, `ui.questions`, `ui.playlist`, `ui.chipviz`, `ui.cartoon`) wherever the
bridge can compute the answer itself; only genuinely client-side math (mode/activity
lookups driven by live events, the WebGL camera and shaders) is ported to JS, each with a
cross-language test pinning it against the real Python constants it must not drift from.

**Tech Stack:** Python stdlib + the project's existing `ui.*`/`protocol.*` modules (runs
under `.venvs/venv-ui` — see Task 1 on why this changes from today), plain browser
JavaScript/CSS/HTML, hand-rolled WebGL (no three.js, no bundler), plain Node.js for JS unit
tests (no test framework).

**Spec:** [`docs/superpowers/specs/2026-09-24-webview-parity-design.md`](../specs/2026-09-24-webview-parity-design.md)

## Global Constraints

- Transport stays SSE + POST. No WebSocket.
- New bridge-originated event types are never added to `protocol/events.py`'s
  `EVENT_TYPES` — that set validates the *daemon's* vocabulary only. They are documented and
  validated in `webview/bridge.py` itself.
- Every new event type goes through `DaemonLink.publish()` (the same `_broadcast` path,
  same `SUBSCRIBER_QUEUE_MAX` backpressure) — never a second, parallel fan-out.
- Reuse Python logic wherever the bridge can compute the answer; port to JS only what must
  run client-side in response to a live event or user interaction.
- No new third-party JS dependency (no three.js, no CDN fetch) — hand-rolled WebGL only.
- `webview/bridge.py` now requires `.venvs/venv-ui` (PyGObject + gemmi + numpy), not
  `.venvs/venv-runner` — this is a real, deliberate change from today's "runs under either
  venv" claim; the module docstring is corrected in Task 1, not left stale.
- Every failure mode below degrades honestly (an em dash, a fallback sentence, a dropped
  ribbon) — never a crash, never a fabricated number, matching this project's
  content-honesty standard everywhere else in the codebase.

## Review Focus

- **`ui.cartoon.cartoon_from_cif` raises `GeometryError`** (a nucleic-acid-only or
  ligand-only structure with no drawable backbone, or a malformed CIF) — must not crash the
  bridge or the SSE loop; no `ribbon_ready` fires, the browser stays on the point cloud,
  exactly as if ribbon rendering did not exist for that fold. Covered in Task 10.
- **`tt-smi` is genuinely absent or always fails on this box** — `telemetry` must never
  fire, the browser's telemetry panel shows its honest "not yet known" state forever, never
  a crash, never a fabricated reading. Covered in Task 2.
- **A late-joining tab missed the `ribbon_ready` broadcast** (opened after `job_done`) — the
  on-demand `GET /ribbon/<job_id>` must serve the cached buffers, and must 404 honestly (not
  500, not hang) for an unknown or evicted `job_id`. Covered in Task 10.
- **An `answer_start`/`answer_done`/`answer_error` names a `question_id` absent from the
  loaded catalog** (a stale/edited `questions.yaml`, or a race) — must fall back to
  `ui.questions.question_label`'s own honest fallback text (`"Does the ligand bind
  {target_id}?"`), never raise. Covered in Task 6.
- **A slow/backgrounded browser tab** — the existing `SUBSCRIBER_QUEUE_MAX`/drop-for-that-
  subscriber-only behavior must hold for every NEW event type too, not just relayed daemon
  events, since all of them go through the same `publish()`/`_broadcast`. Covered in Task 1
  (the shared path) and re-asserted for `telemetry`/`ribbon_ready` in their own tasks.

---

### Task 1: `DaemonLink.publish()` — one hub, more publishers — plus the JS test harness

**Files:**
- Modify: `webview/bridge.py` (module docstring, `DaemonLink._dispatch`)
- Test: `tests/unit/test_webview_bridge.py`
- Create: `scripts/test-webview-js.sh`
- Create: `tests/webview_js/README.md`

**Interfaces:**
- Produces: `DaemonLink.publish(event: dict) -> None` — the same broadcast every later task's
  publisher (telemetry, ribbon cache, Q&A tracker) calls. `event` must have a `"type"` key;
  no other validation here (each publisher validates its own event shape).
- Produces: `scripts/test-webview-js.sh` — runs every `tests/webview_js/*.test.js` under
  plain `node`, exits nonzero if any fails. Later tasks add test files here; this task only
  builds the runner and proves it works on one trivial test.

- [ ] **Step 1: Write the failing test for `publish()`**

```python
# tests/unit/test_webview_bridge.py -- add near the existing DaemonLink tests
def test_publish_reaches_every_subscriber_the_same_way_dispatch_does():
    """publish() is the SAME broadcast _dispatch uses for relayed daemon
    events -- a later publisher (telemetry, ribbon cache) must not need a
    second fan-out mechanism, and must inherit the same backpressure."""
    link = bridge.DaemonLink("/nonexistent", connect=lambda: (_ for _ in ()).throw(OSError()))
    q = link.subscribe()
    q.get_nowait()  # drain the primed None-hello (there is none yet; queue is empty)
```

Actually simplify -- there is no primed hello yet on a fresh link, so `subscribe()`'s queue
starts empty. Replace the body above with:

```python
def test_publish_reaches_every_subscriber_the_same_way_dispatch_does():
    link = bridge.DaemonLink("/nonexistent")
    q = link.subscribe()
    link.publish({"type": "telemetry", "chips": []})
    assert q.get_nowait() == {"type": "telemetry", "chips": []}


def test_publish_drops_for_a_full_subscriber_only_never_raises():
    link = bridge.DaemonLink("/nonexistent")
    q = link.subscribe()
    for _ in range(bridge.SUBSCRIBER_QUEUE_MAX):
        q.put_nowait({"type": "telemetry", "chips": []})
    link.publish({"type": "telemetry", "chips": []})  # must not raise queue.Full outward
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -k publish -v`
Expected: FAIL with `AttributeError: 'DaemonLink' object has no attribute 'publish'`

- [ ] **Step 3: Implement `publish()`, rename `_dispatch`'s tail call**

In `webview/bridge.py`, rename the existing `_broadcast` method's call sites so `_dispatch`
calls the new public entry point instead of touching `_subscribers` directly:

```python
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
                log.warning("subscriber queue full; dropping an event for "
                           "one slow client")
```

Delete the old private `_broadcast` method body and replace its one call site (`_dispatch`'s
last line, `self._broadcast(event)`) with `self.publish(event)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -v`
Expected: PASS, and every pre-existing test in this file still passes (the rename is
behavior-preserving).

- [ ] **Step 5: Correct the module docstring's now-false claim**

`webview/bridge.py`'s own docstring currently says: *"this module reaches into `runner.*`
for nothing... so it runs under EITHER of the project's own venvs... with no preference
between them."* This plan's later tasks (2, 4, 6, 8, 10) import `ui.telemetry`,
`ui.questions`, `ui.playlist`, `ui.chipviz`, and `ui.cartoon` — all of which need
`.venvs/venv-ui` specifically (PyGObject + gemmi + numpy). Replace that paragraph:

```python
"""...
Import discipline: this module still reaches into no `runner.*` code -- everything it
knows about the daemon's own wire comes from `protocol/events.py`. It DOES reach into
several `ui.*` modules for their pure, GTK-free logic (ui.telemetry's tt-smi sampler,
ui.questions'/ui.chipviz's text-formatting functions, ui.playlist's loaders,
ui.cartoon's mesh builder) rather than reimplementing any of it -- which means, since
2026-09-24, this module requires `.venvs/venv-ui` specifically (PyGObject + gemmi +
numpy), not `.venvs/venv-runner`. Run it through `.venvs/venv-ui/bin/python3`, per this
project's convention.
...
"""
```

- [ ] **Step 6: Build the JS test runner**

```bash
mkdir -p tests/webview_js
```

```markdown
<!-- tests/webview_js/README.md -->
# webview_js tests

Plain Node.js scripts, no test framework -- matching this project's own "no framework,
minimal dependency" ethos already applied to `tests/unit/test_run_webview_sh.py`'s shell
tests. Each `*.test.js` file is a standalone script using Node's built-in `assert`; a
non-zero exit means a failure. Run all of them with `scripts/test-webview-js.sh`.

Files under test (`webview/static/*.js`) are loaded with plain `require()` after being
adapted with `module.exports` at the bottom of each file -- see any `*.test.js` for the
pattern. This does not change how a browser loads them (a browser never sees
`module.exports`; `typeof module !== "undefined"` guards it in every source file).
```

```javascript
// tests/webview_js/smoke.test.js -- proves the runner itself works
const assert = require("assert");
assert.strictEqual(1 + 1, 2);
console.log("smoke.test.js: OK");
```

```bash
#!/usr/bin/env bash
# scripts/test-webview-js.sh -- run every tests/webview_js/*.test.js under plain node.
# No framework: each file is a standalone script using Node's built-in `assert`; a
# non-zero exit from `node` is a failure. See tests/webview_js/README.md.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEST_DIR="${REPO_ROOT}/tests/webview_js"

if ! command -v node >/dev/null 2>&1; then
  echo "test-webview-js.sh: node not found -- install Node.js to run these tests" >&2
  exit 1
fi

fail=0
count=0
for test_file in "$TEST_DIR"/*.test.js; do
  [[ -e "$test_file" ]] || continue
  count=$((count + 1))
  echo "== $(basename "$test_file") ==" >&2
  if ! node "$test_file"; then
    fail=1
  fi
done

if [[ "$count" -eq 0 ]]; then
  echo "test-webview-js.sh: no *.test.js files found under $TEST_DIR" >&2
  exit 1
fi

if [[ "$fail" -ne 0 ]]; then
  echo "test-webview-js.sh: one or more JS tests FAILED" >&2
  exit 1
fi
echo "test-webview-js.sh: all $count JS test file(s) passed" >&2
```

```bash
chmod +x scripts/test-webview-js.sh
```

- [ ] **Step 7: Run the JS test runner to verify it passes**

Run: `scripts/test-webview-js.sh`
Expected: `smoke.test.js: OK`, then `test-webview-js.sh: all 1 JS test file(s) passed`

- [ ] **Step 8: Wire it into `scripts/test.sh`**

Open `scripts/test.sh`. Find the point where it reports the UI-half and runner-half
results (the `OVERALL: PASS`/`FAIL` summary block) and add a third leg immediately before
that summary is computed:

```bash
echo "run: JS unit tests (scripts/test-webview-js.sh)"
if scripts/test-webview-js.sh > /tmp/webview-js-test-output.$$ 2>&1; then
  js_result="passed"
else
  js_result="FAILED"
fi
cat /tmp/webview-js-test-output.$$
rm -f /tmp/webview-js-test-output.$$
```

Add `js_result` to the combined summary printed at the end, alongside the existing UI-half
and runner-half lines, and make the script's own final exit code non-zero if
`js_result != "passed"` (matching how it already fails on a UI or runner failure).

- [ ] **Step 9: Run the full suite once to confirm the new leg reports correctly**

Run: `scripts/test.sh`
Expected: the existing UI/runner summary, PLUS a new `JS unit tests: passed` line, overall
still `PASS`.

- [ ] **Step 10: Commit**

```bash
git add webview/bridge.py tests/unit/test_webview_bridge.py scripts/test-webview-js.sh \
        tests/webview_js/ scripts/test.sh
git commit -m "feat(webview): DaemonLink.publish() as a shared broadcast hub, JS test harness"
```

---

### Task 2: Real telemetry — `TelemetryBroadcaster`

**Files:**
- Create: `webview/telemetry_broadcaster.py`
- Test: `tests/unit/test_telemetry_broadcaster.py`
- Modify: `webview/bridge.py` (`main()`, to start/stop it)

**Interfaces:**
- Consumes: `DaemonLink.publish(event)` (Task 1); `ui.telemetry.TelemetrySampler`,
  `ui.telemetry.ChipReading` (both already exist, unchanged).
- Produces: `TelemetryBroadcaster(daemon_link, sampler=None, period_s=2.0)` with
  `.start()`/`.stop()` — a daemon thread that calls `sampler.latest()` every `period_s` and
  `publish()`es a `telemetry` event ONLY when the reading list has changed since the last
  publish (comparing the tuple of `(index, temperature_c, power_w, aiclk_mhz, board_id)` per
  chip — not object identity, since `ChipReading` is a fresh dataclass instance each sample).
  Event shape:
  `{"type": "telemetry", "chips": [{"index": 0, "board_type": "p300c",
  "temperature_c": 47.8, "power_w": 19.0, "aiclk_mhz": 800.0, "board_id": "..."}]}`.
  `sampler` is injectable (same seam `DaemonLink.connect` uses) so tests never spawn a real
  `tt-smi` subprocess.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_telemetry_broadcaster.py
"""TelemetryBroadcaster: samples ui.telemetry, publishes on change only.

A fake sampler (not the real TelemetrySampler) drives every test here --
this is a unit test of the CHANGE-DETECTION and publish wiring, not of
tt-smi itself (ui/telemetry.py's own test file already covers that).
"""
import threading
import time

import pytest

from ui.telemetry import ChipReading
from webview import bridge
from webview.telemetry_broadcaster import TelemetryBroadcaster


class _FakeSampler:
    def __init__(self, readings_sequence):
        self._sequence = list(readings_sequence)
        self._index = 0
        self.calls = 0

    def latest(self):
        self.calls += 1
        i = min(self._index, len(self._sequence) - 1)
        return self._sequence[i]

    def advance(self):
        self._index += 1


class _FakeLink:
    def __init__(self):
        self.published = []
        self._lock = threading.Lock()

    def publish(self, event):
        with self._lock:
            self.published.append(event)


def _reading(index=0, temp=47.8, power=19.0, aiclk=800.0, board_id="B0"):
    return ChipReading(index=index, board_type="p300c", temperature_c=temp,
                       power_w=power, aiclk_mhz=aiclk, board_id=board_id)


def test_a_reading_publishes_exactly_once_while_unchanged():
    sampler = _FakeSampler([[_reading()]] * 5)
    link = _FakeLink()
    b = TelemetryBroadcaster(link, sampler=sampler, period_s=0.01)
    b.start()
    time.sleep(0.1)
    b.stop()
    assert len(link.published) == 1
    assert link.published[0] == {
        "type": "telemetry",
        "chips": [{"index": 0, "board_type": "p300c", "temperature_c": 47.8,
                   "power_w": 19.0, "aiclk_mhz": 800.0, "board_id": "B0"}],
    }


def test_a_real_change_publishes_again():
    sampler = _FakeSampler([[_reading(power=19.0)], [_reading(power=19.0)],
                             [_reading(power=55.0)]])

    class _AdvancingSampler(_FakeSampler):
        def latest(self):
            reading = super().latest()
            self.advance()
            return reading

    sampler = _AdvancingSampler([[_reading(power=19.0)], [_reading(power=55.0)],
                                  [_reading(power=55.0)]])
    link = _FakeLink()
    b = TelemetryBroadcaster(link, sampler=sampler, period_s=0.01)
    b.start()
    time.sleep(0.1)
    b.stop()
    powers = [event["chips"][0]["power_w"] for event in link.published]
    assert powers[0] == 19.0
    assert 55.0 in powers
    assert powers[-1] == 55.0
    # No consecutive duplicate once it settles at 55.0.
    assert not (len(powers) >= 2 and powers[-1] == powers[-2] == 55.0
                and len(set(powers[-3:])) == 1 and len(powers) > 3)


def test_no_sample_yet_publishes_nothing():
    """tt-smi genuinely absent/failing -- TelemetrySampler.latest() is
    None until a first sample ever succeeds. Never a fabricated reading,
    never a crash."""
    sampler = _FakeSampler([None, None, None])
    link = _FakeLink()
    b = TelemetryBroadcaster(link, sampler=sampler, period_s=0.01)
    b.start()
    time.sleep(0.05)
    b.stop()
    assert link.published == []


def test_stop_actually_stops_the_thread():
    sampler = _FakeSampler([[_reading()]])
    link = _FakeLink()
    b = TelemetryBroadcaster(link, sampler=sampler, period_s=0.01)
    b.start()
    time.sleep(0.02)
    b.stop()
    count_after_stop = sampler.calls
    time.sleep(0.05)
    assert sampler.calls == count_after_stop, (
        "the sampler kept being polled after stop() -- the thread was not "
        "actually stopped, only forgotten")


def test_a_full_subscriber_queue_does_not_block_a_real_telemetry_event_for_others():
    """Review Focus: the DaemonLink.publish() backpressure contract (Task
    1 -- drop for the full subscriber only, never raise outward) must
    hold for a REAL telemetry event too, not just the generic event
    Task 1's own test used. Exercises DaemonLink.publish() directly
    (not a fake) against two real queue.Queue subscribers."""
    import queue as queue_module

    link = bridge.DaemonLink("/nonexistent")
    full_q = link.subscribe()
    for _ in range(bridge.SUBSCRIBER_QUEUE_MAX):
        full_q.put_nowait({"type": "filler"})
    healthy_q = link.subscribe()

    sampler = _FakeSampler([[_reading(power=19.0)]])
    b = TelemetryBroadcaster(link, sampler=sampler, period_s=0.01)
    b.start()
    time.sleep(0.05)
    b.stop()

    # The full subscriber's queue is untouched beyond its cap (publish()
    # dropped the telemetry event for it, per Task 1) -- and the healthy
    # subscriber still received it.
    assert full_q.qsize() == bridge.SUBSCRIBER_QUEUE_MAX
    received = []
    while True:
        try:
            received.append(healthy_q.get_nowait())
        except queue_module.Empty:
            break
    assert any(e.get("type") == "telemetry" for e in received)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_telemetry_broadcaster.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'webview.telemetry_broadcaster'`

- [ ] **Step 3: Implement it**

```python
# webview/telemetry_broadcaster.py
"""TelemetryBroadcaster: samples ui.telemetry on the bridge's own thread and
publishes a `telemetry` event on change.

This is bridge-side telemetry, deliberately independent of the daemon --
the same reason ui/telemetry.py's own module docstring gives for why the
native UI samples on its own thread rather than reading it off the wire: a
wedged or dead daemon must still leave the silicon visibly breathing on
screen. `ui.telemetry.TelemetrySampler` already does the hard part (a
background thread, tt-smi, never-raises contract); this class only adds
"publish when the reading actually changed" on top of it.
"""
import dataclasses
import logging
import threading

from ui.telemetry import TelemetrySampler

log = logging.getLogger(__name__)

DEFAULT_PERIOD_S = 2.0


def _reading_key(reading):
    """A hashable snapshot of one ChipReading's fields, for change
    detection -- comparing dataclass INSTANCES would always differ (a
    fresh object every sample), so this compares VALUES instead."""
    return (reading.index, reading.board_type, reading.temperature_c,
            reading.power_w, reading.aiclk_mhz, reading.board_id)


def _reading_to_dict(reading):
    return dataclasses.asdict(reading)


class TelemetryBroadcaster:
    """Publishes a `telemetry` event to `daemon_link` whenever the sampled
    reading list changes. `sampler` is injectable (default: a real
    `TelemetrySampler`) so tests never spawn a real `tt-smi`."""

    def __init__(self, daemon_link, sampler=None, period_s=DEFAULT_PERIOD_S):
        self._daemon_link = daemon_link
        self._sampler = sampler if sampler is not None else TelemetrySampler(
            period_s=period_s)
        self._owns_sampler = sampler is None
        self._period_s = period_s
        self._stop = threading.Event()
        self._thread = None
        self._last_key = None

    def start(self):
        if self._owns_sampler:
            self._sampler.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._period_s + 2.0)
        if self._owns_sampler:
            self._sampler.stop()

    def _run(self):
        while not self._stop.is_set():
            readings = self._sampler.latest()
            if readings is not None:
                key = tuple(_reading_key(r) for r in readings)
                if key != self._last_key:
                    self._last_key = key
                    self._daemon_link.publish({
                        "type": "telemetry",
                        "chips": [_reading_to_dict(r) for r in readings],
                    })
            self._stop.wait(self._period_s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_telemetry_broadcaster.py -v`
Expected: PASS

- [ ] **Step 5: Wire it into `main()`**

In `webview/bridge.py`'s `main()`, after `link.start()`:

```python
    from webview.telemetry_broadcaster import TelemetryBroadcaster
    telemetry = TelemetryBroadcaster(link)
    telemetry.start()
```

And in the `finally` block, before `link.stop()`:

```python
        telemetry.stop()
```

- [ ] **Step 6: Run the full non-hardware suite**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_telemetry_broadcaster.py tests/unit/test_webview_bridge.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add webview/telemetry_broadcaster.py tests/unit/test_telemetry_broadcaster.py webview/bridge.py
git commit -m "feat(webview): real per-chip telemetry via a bridge-side TelemetrySampler"
```

---

### Task 3: Browser telemetry panel

**Files:**
- Create: `webview/static/telemetry.js`
- Test: `tests/webview_js/telemetry.test.js`
- Modify: `webview/static/index.html` (script tag, panel markup)
- Modify: `webview/static/style.css` (panel styling, reusing existing brand CSS vars)
- Modify: `webview/static/app.js` (dispatch `telemetry` events into the new module)

**Interfaces:**
- Consumes: the `telemetry` SSE event (Task 2).
- Produces: `window.Telemetry.render(container, chips)` — pure DOM update, one row per chip
  (`"CHIP {index} · {board_type}"`, `"{temp}°C"`, `"{power}W · {aiclk}MHz"`), an em dash row
  for a chip whose value is `null`. `window.Telemetry.formatChip(chip)` — the pure
  string-building part, exported separately so it is testable with no DOM at all.

- [ ] **Step 1: Write the failing JS test**

```javascript
// tests/webview_js/telemetry.test.js
const assert = require("assert");
const { formatChip } = require("../../webview/static/telemetry.js");

assert.deepStrictEqual(
  formatChip({index: 0, board_type: "p300c", temperature_c: 47.8, power_w: 19.0, aiclk_mhz: 800.0, board_id: "B0"}),
  {label: "CHIP 0 · P300C", temp: "47.8°C", rest: "19W · 800MHz"});

assert.deepStrictEqual(
  formatChip({index: 2, board_type: "p300c", temperature_c: null, power_w: null, aiclk_mhz: null, board_id: null}),
  {label: "CHIP 2 · P300C", temp: "—", rest: "—"});

console.log("telemetry.test.js: OK");
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/webview_js/telemetry.test.js`
Expected: FAIL — `Cannot find module '../../webview/static/telemetry.js'`

- [ ] **Step 3: Implement `telemetry.js`**

```javascript
// webview/static/telemetry.js
"use strict";

// Real per-chip telemetry, matching ui/panels.py's TelemetryPanel -- one row
// per chip, an em dash for a field the sampler could not read, never a
// fabricated number. `formatChip` is pure (no DOM) so it is directly
// testable; `render` is the thin DOM half.

function formatChip(chip) {
  const temp = chip.temperature_c == null ? "—" : `${chip.temperature_c.toFixed(1)}°C`;
  const power = chip.power_w == null ? null : `${Math.round(chip.power_w)}W`;
  const clock = chip.aiclk_mhz == null ? null : `${Math.round(chip.aiclk_mhz)}MHz`;
  const rest = (power == null && clock == null) ? "—"
    : `${power ?? "—"} · ${clock ?? "—"}`;
  return {
    label: `CHIP ${chip.index} · ${chip.board_type.toUpperCase()}`,
    temp,
    rest,
  };
}

function render(container, chips) {
  container.innerHTML = "";
  for (const chip of chips) {
    const { label, temp, rest } = formatChip(chip);
    const cell = document.createElement("div");
    cell.className = "telemetry-cell";
    cell.innerHTML =
      `<div class="telemetry-label">${label}</div>` +
      `<div class="telemetry-temp">${temp}</div>` +
      `<div class="telemetry-rest">${rest}</div>`;
    container.appendChild(cell);
  }
}

if (typeof module !== "undefined") {
  module.exports = { formatChip, render };
}
if (typeof window !== "undefined") {
  window.Telemetry = { formatChip, render };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `node tests/webview_js/telemetry.test.js`
Expected: `telemetry.test.js: OK`

- [ ] **Step 5: Wire into `index.html` and `app.js`**

In `index.html`, add before the closing `</head>` or with the other scripts, and add a
container in the toolbar area:

```html
<script src="/telemetry.js"></script>
```

```html
<div id="telemetry-panel" class="telemetry-panel"></div>
```

In `app.js`'s `handleEvent` switch, add a case:

```javascript
    case "telemetry": {
      Telemetry.render(document.getElementById("telemetry-panel"), event.chips);
      break;
    }
```

- [ ] **Step 6: Add CSS matching the existing brand palette**

In `style.css`, add (reusing the same CSS variables the rest of the page already defines):

```css
.telemetry-panel {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 12px 0;
}
.telemetry-cell {
  background: var(--bg-alt);
  border-radius: 8px;
  padding: 8px 14px;
  min-width: 120px;
}
.telemetry-label {
  font-size: 11px;
  letter-spacing: .08em;
  color: var(--accent-text);
  font-weight: 700;
}
.telemetry-temp {
  font-size: 20px;
  font-weight: 700;
  color: var(--fg);
}
.telemetry-rest {
  font-size: 12px;
  color: var(--fg-muted);
}
```

- [ ] **Step 7: Verify live against the mock runner**

```bash
.venvs/venv-ui/bin/python3 -c "
from runner.mock import MockRunner, load_stream
import time
r = MockRunner('/tmp/webview-verify.sock', load_stream('tests/fixtures/streams/quad_fold.jsonl'), speed=1.0)
r.start()
time.sleep(60)
" &
sleep 1
.venvs/venv-ui/bin/python3 -m webview.bridge --daemon-socket /tmp/webview-verify.sock --port 8123 &
sleep 1
curl -s -N http://127.0.0.1:8123/events | head -5
```

Expected: a `hello` line, no `telemetry` line yet (the mock's fake `TelemetrySampler` has no
real `tt-smi` to sample unless this box has real hardware — if it does, a `telemetry` line
with real numbers appears within ~2s; if not, this is exactly the "no sample yet" case Task
2's own test already covers, and nothing crashes). Kill both background processes after.

- [ ] **Step 8: Run the JS suite and commit**

Run: `scripts/test-webview-js.sh`
Expected: PASS

```bash
git add webview/static/telemetry.js tests/webview_js/telemetry.test.js \
        webview/static/index.html webview/static/style.css webview/static/app.js
git commit -m "feat(webview): real telemetry panel in the browser"
```

---

### Task 4: Tensix-viz pure logic, ported to JS with a drift test

**Files:**
- Create: `webview/static/tensix_logic.js`
- Test: `tests/webview_js/tensix_logic.test.js`
- Test: `tests/unit/test_webview_js_constants.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces: `vizMode(notReady, stage)`, `modeCaption(mode)`, `powerActivity(watts)`,
  `withinStageFrac(stage, frac)` — direct JS ports of `ui.chipviz.viz_mode`/`mode_caption`/
  `power_activity` and `protocol.events.within_stage_frac`. All exported via
  `module.exports`/`window.TensixLogic`.

- [ ] **Step 1: Write the failing JS tests**

```javascript
// tests/webview_js/tensix_logic.test.js
const assert = require("assert");
const { vizMode, modeCaption, powerActivity, withinStageFrac } =
  require("../../webview/static/tensix_logic.js");

assert.strictEqual(vizMode(true, "diffusion"), "idle");
assert.strictEqual(vizMode(false, null), "idle");
assert.strictEqual(vizMode(false, "diffusion"), "diffusion");
assert.strictEqual(vizMode(false, "trunk"), "thinking");
assert.strictEqual(vizMode(false, "confidence"), "inference");
assert.strictEqual(vizMode(false, "msa"), "idle");
assert.strictEqual(vizMode(false, "some-future-stage"), "inference");

assert.strictEqual(modeCaption("diffusion"), "denoising");
assert.strictEqual(modeCaption("thinking"), "refining");
assert.strictEqual(modeCaption("unknown-mode"), "unknown-mode");

assert.ok(Math.abs(powerActivity(15.0) - 0.0) < 1e-9);
assert.ok(Math.abs(powerActivity(90.0) - 1.0) < 1e-9);
assert.ok(powerActivity(52.5) > 0.5, "the curve boosts the midpoint above linear 0.5");

const withinDiffusion = withinStageFrac("diffusion", 0.55);
assert.ok(Math.abs(withinDiffusion - 0.5) < 1e-6);

console.log("tensix_logic.test.js: OK");
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/webview_js/tensix_logic.test.js`
Expected: FAIL — module not found

- [ ] **Step 3: Read the exact Python source values before porting**

```bash
grep -n "STAGE_BANDS = {" -A 10 protocol/events.py
```

Confirm the exact band for `"diffusion"` (expected `(0.15, 0.95)` per the spec's own
research) and copy every band verbatim — a typo'd band boundary here is exactly the kind of
drift Step 5's test exists to catch, but getting it right the first time avoids a
round-trip.

- [ ] **Step 4: Implement `tensix_logic.js`**

```javascript
// webview/static/tensix_logic.js
"use strict";

// Direct ports of ui/chipviz.py's pure mode/activity logic and
// protocol/events.py's within_stage_frac -- see
// tests/unit/test_webview_js_constants.py for the drift test that keeps
// these literal tables from silently disagreeing with the Python source.

const MODE_BY_STAGE = {
  msa: "idle", prep: "idle", trunk: "thinking",
  diffusion: "diffusion", confidence: "inference", saving: "idle",
};
const UNKNOWN_STAGE_MODE = "inference";

const MODE_CAPTION = {
  idle: "idle", inference: "scoring", thinking: "refining", diffusion: "denoising",
};

const POWER_FLOOR_W = 15.0;
const POWER_CEILING_W = 90.0;
const POWER_CURVE = 0.6;

// Copy this table verbatim from protocol/events.py's STAGE_BANDS -- see Step 3.
const STAGE_BANDS = {
  msa: [0.0, 0.05], prep: [0.05, 0.15], trunk: [0.15, 0.15],
  diffusion: [0.15, 0.95], confidence: [0.95, 0.98], saving: [0.98, 1.0],
};

function vizMode(notReady, stage) {
  if (notReady) return "idle";
  if (stage == null) return "idle";
  return Object.prototype.hasOwnProperty.call(MODE_BY_STAGE, stage)
    ? MODE_BY_STAGE[stage] : UNKNOWN_STAGE_MODE;
}

function modeCaption(mode) {
  return Object.prototype.hasOwnProperty.call(MODE_CAPTION, mode) ? MODE_CAPTION[mode] : mode;
}

function powerActivity(watts) {
  const span = POWER_CEILING_W - POWER_FLOOR_W;
  if (!(span > 0) || typeof watts !== "number" || !isFinite(watts)) return 0.0;
  const fraction = Math.max(0.0, Math.min(1.0, (watts - POWER_FLOOR_W) / span));
  return Math.pow(fraction, POWER_CURVE);
}

function withinStageFrac(stage, frac) {
  const band = STAGE_BANDS[stage];
  if (!band || typeof frac !== "number" || !isFinite(frac)) return null;
  const [lo, hi] = band;
  const span = hi - lo;
  if (span <= 0) return 0.0;
  return Math.max(0.0, Math.min(1.0, (frac - lo) / span));
}

if (typeof module !== "undefined") {
  module.exports = { vizMode, modeCaption, powerActivity, withinStageFrac,
                      MODE_BY_STAGE, MODE_CAPTION, POWER_FLOOR_W, POWER_CEILING_W,
                      POWER_CURVE, STAGE_BANDS };
}
if (typeof window !== "undefined") {
  window.TensixLogic = { vizMode, modeCaption, powerActivity, withinStageFrac };
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `node tests/webview_js/tensix_logic.test.js`
Expected: `tensix_logic.test.js: OK`

- [ ] **Step 6: Write the cross-language drift test**

```python
# tests/unit/test_webview_js_constants.py
"""Pins webview/static/tensix_logic.js's literal constant tables against
the real Python source they must never silently disagree with -- the same
'one check that reads what actually executes' shape
test_weights_cache_is_derived_once.py and test_run_webview_sh.py already
use in this repo, applied to a JS port instead of a shell/Python pair.

This does NOT run the JS -- it re-derives the JS file's own literal tables
by executing it under Node and reading its exported constants back, then
compares them field-for-field against ui.chipviz/protocol.events' real
values. A hand-edited JS constant that drifts from the Python source fails
here, at test time, not months later when someone notices the Tensix
panel's colors look subtly wrong.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

from protocol.events import STAGE_BANDS
from ui.chipviz import _MODE_BY_STAGE, _MODE_CAPTION, _POWER_CEILING_W, _POWER_CURVE, _POWER_FLOOR_W

JS_FILE = pathlib.Path(__file__).resolve().parents[1] / "webview" / "static" / "tensix_logic.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _js_constants():
    script = f"""
const m = require({json.dumps(str(JS_FILE))});
console.log(JSON.stringify({{
  MODE_BY_STAGE: m.MODE_BY_STAGE, MODE_CAPTION: m.MODE_CAPTION,
  POWER_FLOOR_W: m.POWER_FLOOR_W, POWER_CEILING_W: m.POWER_CEILING_W,
  POWER_CURVE: m.POWER_CURVE, STAGE_BANDS: m.STAGE_BANDS,
}}));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                            check=True, timeout=10)
    return json.loads(result.stdout)


def test_mode_by_stage_matches():
    assert _js_constants()["MODE_BY_STAGE"] == _MODE_BY_STAGE


def test_mode_caption_matches():
    assert _js_constants()["MODE_CAPTION"] == _MODE_CAPTION


def test_power_constants_match():
    js = _js_constants()
    assert js["POWER_FLOOR_W"] == _POWER_FLOOR_W
    assert js["POWER_CEILING_W"] == _POWER_CEILING_W
    assert js["POWER_CURVE"] == _POWER_CURVE


def test_stage_bands_match():
    js = _js_constants()["STAGE_BANDS"]
    python_bands = {stage: list(band) for stage, band in STAGE_BANDS.items()}
    assert js == python_bands
```

- [ ] **Step 7: Run the drift test to verify it passes**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_js_constants.py -v`
Expected: PASS. If any assertion fails, fix `tensix_logic.js`'s table (never the Python
side, which is the existing source of truth) and re-run.

- [ ] **Step 8: Prove the drift test can actually fail**

Temporarily change one value in `tensix_logic.js` (e.g. `POWER_CURVE = 0.5`), re-run Step
7's command, confirm it goes red, then revert the change.

- [ ] **Step 9: Commit**

```bash
git add webview/static/tensix_logic.js tests/webview_js/tensix_logic.test.js \
        tests/unit/test_webview_js_constants.py
git commit -m "feat(webview): port Tensix mode/activity/progress logic to JS, with a drift test"
```

---

### Task 5: Browser Tensix activity panel — real animation

**Files:**
- Modify: `webview/bridge.py` (serve the two vendored tensix-viz asset files)
- Test: `tests/unit/test_webview_bridge.py`
- Create: `webview/static/tensix.js`
- Test: `tests/webview_js/tensix.test.js`
- Modify: `webview/static/index.html`, `webview/static/app.js`, `webview/static/style.css`

**Interfaces:**
- Consumes: `tensix_logic.js` (Task 4), `telemetry` events (Task 2), the existing `stage`
  event (already forwarded, `frac` field already present but unused by `app.js` today).
- Produces: `window.Tensix.init(cards)` — builds one canvas + `TensixViz` instance per card;
  `window.Tensix.onStage(card, stage, frac)`; `window.Tensix.onTelemetry(chips)`. Both are
  pure dispatch functions the tests can call directly against a fake `TensixViz` stand-in
  (Step 1), separate from the real library's own DOM/canvas requirements.

- [ ] **Step 1: Write the failing JS test (against a fake TensixViz)**

```javascript
// tests/webview_js/tensix.test.js
const assert = require("assert");

// A fake TensixViz standing in for the real vendored library, recording
// every call this module makes into it -- so this test exercises the
// WIRING (which card gets which call, with what value), not the real
// library's own canvas rendering (tensix-viz's own test suite covers that).
class FakeTensixViz {
  constructor(canvas, opts) { this.canvas = canvas; this.opts = opts; this.calls = []; }
  activate(mode) { this.calls.push(["activate", mode]); }
  setActivity(a) { this.calls.push(["setActivity", a]); }
  setProgress(p) { this.calls.push(["setProgress", p]); }
}
global.window = global.window || {};
global.window.TensixViz = FakeTensixViz;
global.document = {
  createElement: () => ({ getContext: () => ({}) }),
};

const { Tensix } = require("../../webview/static/tensix.js");

const instances = Tensix.init([0, 1]);
assert.strictEqual(Object.keys(instances).length, 2);

Tensix.onStage(0, "diffusion", 0.55);
assert.deepStrictEqual(instances[0].calls, [["activate", "diffusion"], ["setProgress", 0.5]]);

Tensix.onTelemetry([{index: 0, power_w: 90.0}, {index: 1, power_w: 15.0}]);
const activityCalls0 = instances[0].calls.filter(c => c[0] === "setActivity");
const activityCalls1 = instances[1].calls.filter(c => c[0] === "setActivity");
assert.ok(Math.abs(activityCalls0[0][1] - 1.0) < 1e-9);
assert.ok(Math.abs(activityCalls1[0][1] - 0.0) < 1e-9);

console.log("tensix.test.js: OK");
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/webview_js/tensix.test.js`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `tensix.js`**

```javascript
// webview/static/tensix.js
"use strict";

// Real per-chip Tensix activity, driven by the SAME vendored tensix-viz.js
// the GTK app ships (served verbatim by the bridge -- see webview/bridge.py's
// _STATIC_CONTENT_TYPES and Step 5 below). Mirrors ui/chipviz.py's
// build_page_html wrapper: one TensixViz instance per chip canvas, .activate
// on stage change, .setActivity from telemetry, .setProgress from the wire's
// own stage.frac.

const { vizMode, withinStageFrac, powerActivity } =
  (typeof require !== "undefined") ? require("./tensix_logic.js") : window.TensixLogic;

const instances = {};
let lastMode = {};

function init(cards) {
  for (const key of Object.keys(instances)) delete instances[key];
  for (const card of cards) {
    const canvas = document.createElement("canvas");
    canvas.width = 86;
    canvas.height = 104;
    canvas.dataset.card = String(card);
    const container = (typeof document.getElementById === "function")
      ? document.getElementById("tensix-panel") : null;
    if (container) container.appendChild(canvas);
    instances[card] = new window.TensixViz(canvas, { arch: "blackhole", showMemory: true });
    lastMode[card] = "idle";
  }
  return instances;
}

function onStage(card, stage, frac) {
  const inst = instances[card];
  if (!inst) return;
  const mode = vizMode(false, stage);
  if (mode !== lastMode[card]) {
    inst.activate(mode);
    lastMode[card] = mode;
  }
  const progress = withinStageFrac(stage, frac);
  if (progress != null) inst.setProgress(progress);
}

function onNotReady(cards) {
  for (const card of cards) {
    const inst = instances[card];
    if (inst && lastMode[card] !== "idle") {
      inst.activate("idle");
      lastMode[card] = "idle";
    }
  }
}

function onTelemetry(chips) {
  for (const chip of chips) {
    const inst = instances[chip.index];
    if (!inst || chip.power_w == null) continue;
    inst.setActivity(powerActivity(chip.power_w));
  }
}

const Tensix = { init, onStage, onNotReady, onTelemetry };
if (typeof module !== "undefined") module.exports = { Tensix };
if (typeof window !== "undefined") window.Tensix = Tensix;
```

- [ ] **Step 4: Run to verify it passes**

Run: `node tests/webview_js/tensix.test.js`
Expected: `tensix.test.js: OK`

- [ ] **Step 5: Serve the vendored tensix-viz assets from the bridge**

`webview/bridge.py`'s `_serve_static` already resolves any path under `STATIC_DIR` — the
simplest, most consistent approach is to symlink the two vendored files into
`webview/static/` at bridge startup rather than adding a second static root:

```python
# In webview/bridge.py, near STATIC_DIR:
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
```

Call `_ensure_tensix_viz_assets_linked()` once at the top of `main()`, before
`build_server(...)`.

- [ ] **Step 6: Test the asset-linking**

```python
# tests/unit/test_webview_bridge.py -- add
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
```

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -k tensix_viz_assets -v`
Expected: PASS

- [ ] **Step 7: Wire into `index.html`/`app.js`**

```html
<script src="/tensix_logic.js"></script>
<script src="/tensix-viz.js"></script>
<script src="/tensix.js"></script>
<div id="tensix-panel" class="tensix-panel"></div>
```

In `app.js`, after `rebuildCellsIfNeeded` runs (so `Tensix.init` sees the real card list),
call `Tensix.init(state.cards)`. In the `hello` case, also handle `not_ready` by calling
`Tensix.onNotReady(state.cards)`. In the `stage` case, add `Tensix.onStage(cellForJob(event.job_id)?.card, event.stage, event.frac)`
(guarding for a null cell exactly as the existing `stage` handler already does). In the new
`telemetry` case (Task 3), also call `Tensix.onTelemetry(event.chips)`.

- [ ] **Step 8: Add panel CSS**

```css
.tensix-panel {
  display: flex;
  gap: 6px;
  margin: 12px 0;
}
.tensix-panel canvas {
  border: 1px solid var(--hairline);
  border-radius: 4px;
  background: var(--bg-cell);
}
```

- [ ] **Step 9: Verify live against the mock runner (real vendored assets, real browser)**

```bash
.venvs/venv-ui/bin/python3 -c "
from runner.mock import MockRunner, load_stream
import time
r = MockRunner('/tmp/webview-verify2.sock', load_stream('tests/fixtures/streams/quad_fold.jsonl'), speed=1.0)
r.start()
time.sleep(60)
" &
sleep 1
.venvs/venv-ui/bin/python3 -m webview.bridge --daemon-socket /tmp/webview-verify2.sock --port 8124 &
sleep 1
curl -sf http://127.0.0.1:8124/tensix-viz.js -o /dev/null && echo "tensix-viz.js served OK"
google-chrome --headless --disable-gpu --no-sandbox --window-size=1600,900 \
  --screenshot=/tmp/webview-tensix-check.png http://127.0.0.1:8124/ 2>&1 | tail -5
```

Look at `/tmp/webview-tensix-check.png` (the `Read` tool) — expect four small chip canvases
rendering something (a live tensix-viz grid, not blank). Kill both background processes
after.

- [ ] **Step 10: Run the JS suite and commit**

Run: `scripts/test-webview-js.sh`
Expected: PASS

```bash
git add webview/bridge.py tests/unit/test_webview_bridge.py webview/static/tensix.js \
        tests/webview_js/tensix.test.js webview/static/index.html webview/static/app.js \
        webview/static/style.css
git commit -m "feat(webview): real Tensix activity panel, reusing the vendored library verbatim"
```

---

### Task 6: Server-side Q&A state tracker — real queue, real text

**Files:**
- Create: `webview/qa_tracker.py`
- Test: `tests/unit/test_qa_tracker.py`
- Modify: `webview/bridge.py` (wire the tracker into `_dispatch`)

**Interfaces:**
- Consumes: `ui.questions.pending_text/in_flight_text/answered_text/error_text/
  no_question_in_flight_text/no_question_answered_text` (all existing, unchanged);
  `ui.playlist.Question` (existing dataclass).
- Produces: `QaTracker(questions: list[Question])` with `.on_event(event) -> dict | None` —
  called for every relayed `answer_start`/`answer_done`/`answer_error` event; returns a
  `qa_queue` event dict (or `None` for any other event type, so the caller can call this
  unconditionally without its own type-check). Event shape:
  `{"type": "qa_queue", "pending": "...", "in_flight": "...", "answered": "...", "answered_is_error": bool}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_qa_tracker.py
"""QaTracker: server-side Q&A queue state, rendered with ui.questions'
real pure text functions -- so the browser's Q&A panel shows the SAME
text the native QuestionQueuePanel does, computed by the same code."""
from ui.playlist import Question
from webview.qa_tracker import QaTracker


def _questions():
    return [
        Question(id="dhfr_mtx", target_id="dhfr",
                 question="Does methotrexate block dihydrofolate reductase?",
                 ligand_name="Methotrexate", expected_s=None),
        Question(id="fkbp12_sb3", target_id="fkbp12",
                 question="Does SB3 bind FKBP12?", ligand_name="SB3", expected_s=None),
    ]


def test_a_non_qa_event_returns_none():
    tracker = QaTracker(_questions())
    assert tracker.on_event({"type": "job_start", "job_id": "j1"}) is None


def test_answer_start_moves_a_question_to_in_flight():
    tracker = QaTracker(_questions())
    result = tracker.on_event({"type": "answer_start", "question_id": "fkbp12_sb3",
                                "target_id": "fkbp12"})
    assert result["type"] == "qa_queue"
    assert "SB3 bind FKBP12" in result["in_flight"]
    assert "dihydrofolate reductase" in result["pending"]
    assert result["answered"] == "No question answered yet"
    assert result["answered_is_error"] is False


def test_answer_done_moves_it_to_answered_and_clears_in_flight():
    tracker = QaTracker(_questions())
    tracker.on_event({"type": "answer_start", "question_id": "fkbp12_sb3",
                       "target_id": "fkbp12"})
    result = tracker.on_event({"type": "answer_done", "question_id": "fkbp12_sb3",
                                "target_id": "fkbp12", "score": 0.94})
    assert "0.94" in result["answered"]
    assert result["answered_is_error"] is False
    assert result["in_flight"] == "No question in flight"


def test_answer_error_shows_an_honest_failure_not_a_score():
    tracker = QaTracker(_questions())
    tracker.on_event({"type": "answer_start", "question_id": "dhfr_mtx", "target_id": "dhfr"})
    result = tracker.on_event({"type": "answer_error", "question_id": "dhfr_mtx",
                                "target_id": "dhfr"})
    assert result["answered_is_error"] is True
    assert "0." not in result["answered"]


def test_an_unknown_question_id_falls_back_honestly_never_raises():
    """Review Focus: a question_id absent from the loaded catalog (a stale
    questions.yaml, or a race) must degrade to question_label's own
    fallback text, never crash the tracker or the SSE loop."""
    tracker = QaTracker(_questions())
    result = tracker.on_event({"type": "answer_start", "question_id": "not-a-real-id",
                                "target_id": "trypsin"})
    assert "trypsin" in result["in_flight"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_qa_tracker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webview.qa_tracker'`

- [ ] **Step 3: Implement it**

```python
# webview/qa_tracker.py
"""QaTracker: server-side affinity-Q&A queue state, rendered with
ui.questions' real pure text functions.

Mirrors ui/questions.py's QuestionQueuePanel exactly (same pending/
in-flight/answered derivation), running here instead of in a GTK label
setter -- so the browser's Q&A panel shows text computed by the SAME code
the native panel calls, never a second hand-typed copy in JS. See
docs/superpowers/specs/2026-09-24-webview-parity-design.md section 6.
"""
import logging

from ui.questions import (answered_text, error_text, in_flight_text,
                           no_question_answered_text, no_question_in_flight_text,
                           pending_text)

log = logging.getLogger(__name__)


class QaTracker:
    def __init__(self, questions):
        self._questions = list(questions)
        self._by_id = {q.id: q for q in self._questions}
        self._in_flight = None   # dict: question_id, target_id, question_text
        self._answered = None    # dict: question_id, target_id, score, question_text, error

    def _pending_questions(self):
        exclude = set()
        if self._in_flight is not None:
            exclude.add(self._in_flight["question_id"])
        if self._answered is not None:
            exclude.add(self._answered["question_id"])
        return [q for q in self._questions if q.id not in exclude]

    def _render(self):
        pending = pending_text(self._pending_questions())
        if self._in_flight is not None:
            in_flight = in_flight_text(self._in_flight["target_id"],
                                       self._in_flight["question_text"])
        else:
            in_flight = no_question_in_flight_text()
        if self._answered is not None:
            if self._answered["error"]:
                answered = error_text(self._answered["target_id"],
                                      self._answered["question_text"])
                is_error = True
            else:
                answered = answered_text(self._answered["target_id"],
                                         self._answered["score"],
                                         self._answered["question_text"])
                is_error = False
        else:
            answered = no_question_answered_text()
            is_error = False
        return {"type": "qa_queue", "pending": pending, "in_flight": in_flight,
                "answered": answered, "answered_is_error": is_error}

    def _clear_in_flight_if_matches(self, question_id):
        if self._in_flight is not None and self._in_flight["question_id"] == question_id:
            self._in_flight = None

    def on_event(self, event):
        kind = event.get("type")
        question_id = event.get("question_id")
        target_id = event.get("target_id")
        question = self._by_id.get(question_id)
        question_text = getattr(question, "question", None)
        if kind == "answer_start":
            self._in_flight = {"question_id": question_id, "target_id": target_id,
                               "question_text": question_text}
        elif kind == "answer_done":
            self._clear_in_flight_if_matches(question_id)
            self._answered = {"question_id": question_id, "target_id": target_id,
                              "score": event.get("score"), "question_text": question_text,
                              "error": False}
        elif kind == "answer_error":
            self._clear_in_flight_if_matches(question_id)
            self._answered = {"question_id": question_id, "target_id": target_id,
                              "score": None, "question_text": question_text,
                              "error": True}
        else:
            return None
        return self._render()
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_qa_tracker.py -v`
Expected: PASS

- [ ] **Step 5: Wire into `_dispatch`**

This task does not yet load the REAL `questions.yaml` (Task 8 does, along with the
playlist) — for now, wire `QaTracker` with an empty question list so the mechanism is
provably correct end to end with the mock fixtures already in `tests/fixtures/streams/`
(`with_question.jsonl`), and Task 8 swaps the empty list for the real catalog with no other
change:

```python
# In webview/bridge.py's DaemonLink.__init__:
        from webview.qa_tracker import QaTracker
        self._qa_tracker = QaTracker(questions=[])

# In _dispatch, after self.publish(event):
        qa_event = self._qa_tracker.on_event(event)
        if qa_event is not None:
            self.publish(qa_event)
```

- [ ] **Step 6: Add a bridge-level integration test**

```python
# tests/unit/test_webview_bridge.py -- add
def test_a_relayed_answer_event_also_publishes_a_qa_queue_event():
    link = bridge.DaemonLink("/nonexistent")
    q = link.subscribe()
    link._dispatch(json.dumps({"type": "answer_start", "question_id": "x",
                               "target_id": "dhfr"}).encode() + b"\n")
    first = q.get_nowait()
    assert first["type"] == "answer_start"
    second = q.get_nowait()
    assert second["type"] == "qa_queue"
```

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -k qa_queue -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add webview/qa_tracker.py tests/unit/test_qa_tracker.py webview/bridge.py \
        tests/unit/test_webview_bridge.py
git commit -m "feat(webview): server-side Q&A queue tracker, reusing ui.questions' real text"
```

---

### Task 7: Browser Q&A queue panel

**Files:**
- Modify: `webview/static/app.js` (replace the single `#qa-panel` fields with a queue)
- Modify: `webview/static/index.html`
- Test: `tests/webview_js/qa_panel.test.js`
- Create: `webview/static/qa_panel.js`

**Interfaces:**
- Consumes: the `qa_queue` event (Task 6).
- Produces: `window.QaPanel.render(dom, event)` — pure DOM update of the three rows, reusing
  the existing `setQuestionText` oompa-bounce animation (already in `app.js`) for the
  in-flight row only.

- [ ] **Step 1: Write the failing JS test**

```javascript
// tests/webview_js/qa_panel.test.js
const assert = require("assert");

const dom = {
  pending: { textContent: "" },
  inFlight: { innerHTML: "", classList: { add() {}, remove() {} } },
  answered: { textContent: "", classList: { add() {}, remove() {} } },
};
global.document = { createElement: () => ({ style: {} }) };

const { QaPanel } = require("../../webview/static/qa_panel.js");

QaPanel.render(dom, {pending: "Does X bind Y? (not yet timed)",
                     in_flight: "Checking — Does A bind B?",
                     answered: "score: 0.94/1.0 — nesso1's predicted probability the ligand binds",
                     answered_is_error: false});
assert.strictEqual(dom.pending.textContent, "Does X bind Y? (not yet timed)");
assert.strictEqual(dom.answered.textContent.includes("0.94"), true);

console.log("qa_panel.test.js: OK");
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/webview_js/qa_panel.test.js`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `qa_panel.js`**

```javascript
// webview/static/qa_panel.js
"use strict";

function render(dom, event) {
  dom.pending.textContent = event.pending;
  if (typeof setQuestionText === "function") {
    // setQuestionText already exists in app.js (the oompa-bounce
    // animation); animated only while a question is genuinely checking --
    // "motion means pending, stillness means here's the answer."
    const isChecking = event.in_flight !== "No question in flight";
    setQuestionText(dom.inFlight, event.in_flight, { animated: isChecking });
  } else {
    dom.inFlight.textContent = event.in_flight;
  }
  dom.answered.textContent = event.answered;
  dom.answered.classList.toggle("qa-error", !!event.answered_is_error);
}

const QaPanel = { render };
if (typeof module !== "undefined") module.exports = { QaPanel };
if (typeof window !== "undefined") window.QaPanel = QaPanel;
```

- [ ] **Step 4: Run to verify it passes**

Run: `node tests/webview_js/qa_panel.test.js`
Expected: `qa_panel.test.js: OK`

- [ ] **Step 5: Replace the old single-question markup and wire the event**

In `index.html`, replace the existing `#qa-panel` internals with three rows:

```html
<section id="qa-panel" class="panel qa-panel" hidden>
  <h2>Affinity questions</h2>
  <div id="qa-pending" class="qa-pending"></div>
  <div id="qa-in-flight" class="qa-in-flight"></div>
  <div id="qa-answered" class="qa-answered"></div>
</section>
```

In `app.js`, remove the old `answer_start`/`answer_done`/`answer_error` cases' direct DOM
manipulation and replace with one `qa_queue` case:

```javascript
    case "qa_queue": {
      qaPanel.hidden = false;
      QaPanel.render({
        pending: qaPendingEl, inFlight: qaQuestion, answered: qaScore,
      }, event);
      break;
    }
```

(Reusing the existing `qaQuestion`/`qaScore` element references already declared near the
top of `app.js`; add one new `qaPendingEl = document.getElementById("qa-pending")`
alongside them.) The raw `answer_start`/`answer_done`/`answer_error` cases can be deleted
from `app.js`'s switch entirely — `qa_queue` (published immediately after each of them, per
Task 6 Step 5) now carries everything the panel needs.

Add `<script src="/qa_panel.js"></script>` to `index.html`.

- [ ] **Step 6: Add `.qa-error` CSS**

```css
.qa-answered.qa-error { color: var(--danger); }
```

- [ ] **Step 7: Verify live against the `with_question.jsonl` mock fixture**

```bash
.venvs/venv-ui/bin/python3 -c "
from runner.mock import MockRunner, load_stream
import time
r = MockRunner('/tmp/webview-verify3.sock', load_stream('tests/fixtures/streams/with_question.jsonl'), speed=1.0)
r.start()
time.sleep(30)
" &
sleep 1
.venvs/venv-ui/bin/python3 -m webview.bridge --daemon-socket /tmp/webview-verify3.sock --port 8125 &
sleep 3
curl -s -N --max-time 4 http://127.0.0.1:8125/events | grep qa_queue
```

Expected: at least one `data: {"type": "qa_queue", ...}` line with real pending/in-flight
text. Kill both background processes after.

- [ ] **Step 8: Run the JS suite and commit**

Run: `scripts/test-webview-js.sh`
Expected: PASS

```bash
git add webview/static/qa_panel.js tests/webview_js/qa_panel.test.js \
        webview/static/index.html webview/static/app.js webview/static/style.css
git commit -m "feat(webview): real Q&A queue panel (pending/in-flight/answered)"
```

---

### Task 8: Real catalogs — questions.yaml and manifest.yaml, server-side

**Files:**
- Modify: `webview/bridge.py` (`main()`, `DaemonLink`)
- Test: `tests/unit/test_webview_bridge.py`
- Modify: `webview/static/app.js` (gallery uses the real catalog instead of "seen so far")

**Interfaces:**
- Consumes: `ui.playlist.load_playlist`, `ui.playlist.load_questions` (existing, unchanged).
- Produces: `questions_catalog`/`playlist_catalog` events, sent once per new subscriber
  (primed the same way `last_hello` already is), replacing Task 6's placeholder empty
  `QaTracker(questions=[])` with the real loaded list.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_webview_bridge.py -- add
def test_a_new_subscriber_is_primed_with_both_catalogs(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "- id: trpcage\n  input_path: examples/trp.yaml\n  model: protenix-v2\n"
        "  name: Trp-cage\n  blurb: A tiny protein.\n  expected_s: 4.6\n")
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        "- id: q1\n  target_id: trpcage\n  question: Does X bind Y?\n"
        "  ligand_name: X\n")
    link = bridge.DaemonLink("/nonexistent")
    link.load_catalogs(manifest_path=manifest, questions_path=questions)
    q = link.subscribe()
    events = [q.get_nowait() for _ in range(2)]
    types = {e["type"] for e in events}
    assert types == {"questions_catalog", "playlist_catalog"}
    playlist_event = next(e for e in events if e["type"] == "playlist_catalog")
    assert playlist_event["targets"][0]["name"] == "Trp-cage"
    questions_event = next(e for e in events if e["type"] == "questions_catalog")
    assert questions_event["questions"][0]["question"] == "Does X bind Y?"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -k catalogs -v`
Expected: FAIL — `AttributeError: 'DaemonLink' object has no attribute 'load_catalogs'`

- [ ] **Step 3: Implement it**

In `webview/bridge.py`, add to `DaemonLink.__init__`:

```python
        self._catalog_events = []  # primed into every new subscribe(), like last_hello
```

Add a method:

```python
    def load_catalogs(self, manifest_path=None, questions_path=None):
        """Load the real playlist/manifest.yaml and playlist/questions.yaml
        (via ui.playlist's own validating loaders) and build the two
        catalog events every subscriber is primed with. Raises
        PlaylistError outright on a malformed file -- a config error, not
        a runtime one, the same standard ui.playlist's own loaders already
        hold themselves to; main() lets this propagate rather than
        starting a bridge that silently has no real gallery or questions.
        """
        from ui.playlist import load_playlist, load_questions
        targets = load_playlist(manifest_path) if manifest_path else load_playlist(
            Path(__file__).parent.parent / "playlist" / "manifest.yaml")
        questions = load_questions(questions_path)
        self._catalog_events = [
            {"type": "playlist_catalog", "targets": [
                {"id": t.id, "name": t.name, "tagline": t.tagline,
                 "expected_s": t.expected_s} for t in targets]},
            {"type": "questions_catalog", "questions": [
                {"id": q.id, "target_id": q.target_id, "question": q.question,
                 "ligand_name": q.ligand_name, "expected_s": q.expected_s}
                for q in questions]},
        ]
        from webview.qa_tracker import QaTracker
        self._qa_tracker = QaTracker(questions=questions)
        return targets, questions
```

Update `subscribe()` to also prime the catalog events, right after the `last_hello` priming:

```python
        for event in self._catalog_events:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -k catalogs -v`
Expected: PASS

- [ ] **Step 5: Wire into `main()`**

```python
    link = DaemonLink(args.daemon_socket)
    link.load_catalogs()
    link.start()
```

Add a `--playlist`/`--questions` CLI flag pair mirroring `run-demo.sh`'s own naming, both
optional (default to the real shipped files):

```python
    parser.add_argument("--playlist", default=None,
                        help="playlist/manifest.yaml path (default: the shipped one)")
    parser.add_argument("--questions", default=None,
                        help="playlist/questions.yaml path (default: the shipped one)")
    ...
    link.load_catalogs(manifest_path=args.playlist, questions_path=args.questions)
```

- [ ] **Step 6: Update the browser gallery to use the real catalog**

In `app.js`, replace `state.seenTargets`/`addSeenTarget` entirely: add a
`playlist_catalog`/`questions_catalog` case in `handleEvent` that stores `state.targets =
event.targets` (or `state.questionCatalog`), and rebuild `#seen-targets` (rename to
`#gallery` in `index.html` if you like, or keep the id and just change what populates it)
from the real target list instead of the pick-triggered history. Each button's label is now
`target.name` (e.g. "Trp-cage") instead of the raw id, still POSTing the real `target.id` to
`/pick`.

- [ ] **Step 7: Update `webview/README.md`**

Remove scope-cut items (b) ("the gallery is targets seen so far") and (c) ("a question
shows target_id + raw score, not the human-written question text") from the "Where this
deliberately stops short" list — both are now closed. Leave the egg and full
reconnect-frame-backfill items in place (still deferred, per spec §8/§10).

- [ ] **Step 8: Run the full bridge test file and commit**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py tests/unit/test_qa_tracker.py -v`
Expected: PASS

```bash
git add webview/bridge.py tests/unit/test_webview_bridge.py webview/static/app.js \
        webview/static/index.html webview/README.md
git commit -m "feat(webview): real gallery + real Q&A catalog, loaded server-side"
```

---

### Task 9: Help screen

**Files:**
- Modify: `webview/bridge.py` (`GET /help.json`)
- Test: `tests/unit/test_webview_bridge.py`
- Create: `webview/static/help_panel.js`
- Test: `tests/webview_js/help_panel.test.js`
- Modify: `webview/static/index.html`, `webview/static/app.js`, `webview/static/style.css`

**Interfaces:**
- Consumes: `ui.app._help_intro`, `ui.app._key_help`, `ui.app._help_panels`,
  `ui.app._PLDDT_LEGEND` (all existing plain-data functions/tuples, confirmed import-safe
  with no display — see the spec's own verification).
- Produces: `GET /help.json` → `{"intro": [...], "keys": [[k, meaning], ...],
  "panels": [...], "plddt_legend": [[css_class, range, meaning], ...]}`.
  `window.HelpPanel.render(container, data)`.

- [ ] **Step 1: Write the failing bridge test**

```python
# tests/unit/test_webview_bridge.py -- add
def test_help_json_serves_real_help_content():
    from ui.app import _PLDDT_LEGEND
    handler_cls = bridge.make_handler(bridge.DaemonLink("/nonexistent"))
    # Exercise the pure builder directly rather than a full HTTP round trip
    # (the existing do_GET tests already cover HTTP plumbing generically).
    payload = bridge.build_help_payload(n_chips=4)
    assert isinstance(payload["intro"], list) and len(payload["intro"]) > 0
    assert any(k == "?" or k == "? or F1" for k, _ in payload["keys"])
    assert len(payload["plddt_legend"]) == len(_PLDDT_LEGEND)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -k help_json -v`
Expected: FAIL — `AttributeError: module 'webview.bridge' has no attribute 'build_help_payload'`

- [ ] **Step 3: Implement `build_help_payload` and the route**

```python
# In webview/bridge.py:
def build_help_payload(n_chips):
    from ui.app import _help_intro, _help_panels, _key_help, _PLDDT_LEGEND
    return {
        "intro": list(_help_intro(n_chips)),
        "keys": [list(pair) for pair in _key_help(n_chips)],
        "panels": list(_help_panels(n_chips)),
        "plddt_legend": [list(row) for row in _PLDDT_LEGEND],
    }
```

In `do_GET`, add a branch before the static-file fallthrough:

```python
        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/events":
                self._serve_events()
            elif path == "/help.json":
                self._serve_help()
            else:
                self._serve_static(path)

        def _serve_help(self):
            n_chips = len(daemon_link.last_hello.get("cards", [])) if daemon_link.last_hello else 0
            payload = build_help_payload(n_chips)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -k help_json -v`
Expected: PASS

- [ ] **Step 5: Write the failing JS test**

```javascript
// tests/webview_js/help_panel.test.js
const assert = require("assert");
const { HelpPanel } = require("../../webview/static/help_panel.js");

const container = { innerHTML: "" };
HelpPanel.render(container, {
  intro: ["A protein folds itself."],
  keys: [["? or F1", "this card, any time"]],
  panels: ["The pipeline panel shows six stages."],
  plddt_legend: [["plddt-high", "90+", "very high, trust it"]],
});
assert.ok(container.innerHTML.includes("A protein folds itself."));
assert.ok(container.innerHTML.includes("this card, any time"));

console.log("help_panel.test.js: OK");
```

- [ ] **Step 6: Run to verify it fails, then implement**

```javascript
// webview/static/help_panel.js
"use strict";

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function render(container, data) {
  const intro = data.intro.map(p => `<p>${escapeHtml(p)}</p>`).join("");
  const keys = data.keys.map(([k, m]) =>
    `<div class="help-key-row"><span class="help-key">${escapeHtml(k)}</span>` +
    `<span class="help-desc">${escapeHtml(m)}</span></div>`).join("");
  const panels = data.panels.map(p => `<p>${escapeHtml(p)}</p>`).join("");
  const legend = data.plddt_legend.map(([cls, range, meaning]) =>
    `<div class="help-legend-row"><span class="plddt-swatch ${escapeHtml(cls)}"></span>` +
    `${escapeHtml(range)} · ${escapeHtml(meaning)}</div>`).join("");
  container.innerHTML =
    `<h2>What you are looking at</h2>${intro}` +
    `<h3>Keys</h3>${keys}` +
    `<h3>The panels</h3>${panels}` +
    `<h3>Confidence colour</h3>${legend}`;
}

const HelpPanel = { render };
if (typeof module !== "undefined") module.exports = { HelpPanel };
if (typeof window !== "undefined") window.HelpPanel = HelpPanel;
```

Run: `node tests/webview_js/help_panel.test.js`
Expected: `help_panel.test.js: OK`

- [ ] **Step 7: Wire into `index.html`/`app.js`**

```html
<script src="/help_panel.js"></script>
<div id="help-overlay" class="help-overlay" hidden>
  <div id="help-content"></div>
  <button id="help-close">Close</button>
</div>
```

In `app.js`, add a keydown listener (mirroring the existing `viewToggle` click-handler
style) for `?`:

```javascript
document.addEventListener("keydown", (ev) => {
  if (ev.key === "?" ) {
    fetch("/help.json").then(r => r.json()).then(data => {
      HelpPanel.render(document.getElementById("help-content"), data);
      document.getElementById("help-overlay").hidden = false;
    });
  }
});
document.getElementById("help-close").addEventListener("click", () => {
  document.getElementById("help-overlay").hidden = true;
});
```

Also add a small, always-visible on-screen `?` affordance (a button, not keyboard-only —
per the spec's own note that a remote tunnel viewer has no reason to know the native app's
bindings):

```html
<button id="help-open-btn" class="help-open-btn">?</button>
```

```javascript
document.getElementById("help-open-btn").addEventListener("click", () => {
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "?" }));
});
```

- [ ] **Step 8: Add CSS**

```css
.help-overlay {
  position: fixed; inset: 0; background: rgba(9,34,33,0.96);
  display: flex; align-items: center; justify-content: center; z-index: 100;
}
.help-overlay > div { max-width: 700px; padding: 24px; }
.help-open-btn {
  position: fixed; bottom: 16px; right: 16px; border-radius: 50%;
  width: 36px; height: 36px; z-index: 50;
}
```

- [ ] **Step 9: Verify live**

```bash
.venvs/venv-ui/bin/python3 -m webview.bridge --daemon-socket /tmp/webview-verify3.sock --port 8126 &
sleep 1
curl -s http://127.0.0.1:8126/help.json | python3 -m json.tool | head -20
```

Expected: real intro paragraphs, real key list. Kill the background process after.

- [ ] **Step 10: Run both suites and commit**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -v && scripts/test-webview-js.sh`
Expected: both PASS

```bash
git add webview/bridge.py tests/unit/test_webview_bridge.py webview/static/help_panel.js \
        tests/webview_js/help_panel.test.js webview/static/index.html webview/static/app.js \
        webview/static/style.css
git commit -m "feat(webview): help screen, reusing ui.app's real help content"
```

---

### Task 10: Ribbon precompute — `RibbonCache`

**Files:**
- Create: `webview/ribbon_cache.py`
- Test: `tests/unit/test_ribbon_cache.py`
- Modify: `webview/bridge.py` (wire into `_dispatch`, add `GET /ribbon/<job_id>`)

**Interfaces:**
- Consumes: `ui.cartoon.cartoon_from_cif` (existing, unchanged); the relayed `job_done`
  event (already carries `cif_path`, confirmed `runner/folder.py:352`).
- Produces: `RibbonCache(daemon_link, max_size=8, max_workers=2)` with `.on_job_done(event)`
  (submits async work, returns immediately) and `.get(job_id)` (the cached, packed buffers,
  or `None`). Publishes a `ribbon_ready` event on completion:
  `{"type": "ribbon_ready", "job_id": ..., "vertices_b64": ..., "normals_b64": ...,
  "colors_b64": ..., "indices_b64": ..., "vertex_count": N, "index_count": M}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_ribbon_cache.py
"""RibbonCache: server-side ribbon precompute via ui.cartoon.cartoon_from_cif,
off the dispatch thread, cached and packed for the wire."""
import base64
import pathlib
import struct
import time

import numpy as np

from webview.ribbon_cache import RibbonCache, unpack_float32_b64

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "structures"


class _FakeLink:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


def _wait_for(predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_a_real_cif_produces_a_ribbon_ready_event():
    link = _FakeLink()
    cache = RibbonCache(link)
    cache.on_job_done({"type": "job_done", "job_id": "j1",
                       "cif_path": str(FIXTURES / "real_fold_trpcage.cif"),
                       "mean_plddt": 95.3})
    assert _wait_for(lambda: len(link.published) == 1)
    event = link.published[0]
    assert event["type"] == "ribbon_ready"
    assert event["job_id"] == "j1"
    assert event["vertex_count"] > 0
    verts = unpack_float32_b64(event["vertices_b64"])
    assert len(verts) == event["vertex_count"] * 3
    cache.shutdown()


def test_get_returns_the_cached_buffers_for_an_on_demand_request():
    link = _FakeLink()
    cache = RibbonCache(link)
    cache.on_job_done({"type": "job_done", "job_id": "j2",
                       "cif_path": str(FIXTURES / "real_fold_trpcage.cif")})
    assert _wait_for(lambda: cache.get("j2") is not None)
    assert cache.get("does-not-exist") is None
    cache.shutdown()


def test_a_cif_with_no_drawable_backbone_publishes_nothing_and_does_not_crash():
    """Review Focus: cartoon_from_cif raising GeometryError (a ligand-only
    or malformed structure) must degrade to 'no ribbon', never a crash."""
    import pathlib as _p
    bad_cif = _p.Path("/tmp/ribbon_cache_test_empty.cif")
    bad_cif.write_text("data_empty\n#\n")
    link = _FakeLink()
    cache = RibbonCache(link)
    cache.on_job_done({"type": "job_done", "job_id": "j3", "cif_path": str(bad_cif)})
    time.sleep(0.3)  # give the worker a chance; nothing should ever arrive
    assert link.published == []
    assert cache.get("j3") is None
    cache.shutdown()
    bad_cif.unlink()


def test_a_job_done_with_no_cif_path_is_ignored():
    link = _FakeLink()
    cache = RibbonCache(link)
    cache.on_job_done({"type": "job_done", "job_id": "j4"})
    time.sleep(0.1)
    assert link.published == []
    cache.shutdown()


def test_the_cache_evicts_the_oldest_entry_past_max_size():
    link = _FakeLink()
    cache = RibbonCache(link, max_size=2)
    for i in range(3):
        cache.on_job_done({"type": "job_done", "job_id": f"j{i}",
                           "cif_path": str(FIXTURES / "real_fold_trpcage.cif")})
    assert _wait_for(lambda: cache.get("j2") is not None)
    assert cache.get("j0") is None, "the oldest entry must have been evicted"
    cache.shutdown()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_ribbon_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webview.ribbon_cache'`

- [ ] **Step 3: Implement it**

```python
# webview/ribbon_cache.py
"""RibbonCache: server-side ribbon precompute, off the dispatch thread.

On a relayed job_done carrying a real cif_path, submits ui.cartoon.
cartoon_from_cif (the exact function the GTK app already trusts) to a
small thread pool, caches the packed result, and publish()es a
ribbon_ready event. A late-joining tab gets the same cached buffers via
GET /ribbon/<job_id> (webview/bridge.py). See
docs/superpowers/specs/2026-09-24-webview-parity-design.md section 8.
"""
import base64
import collections
import logging
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_MAX_SIZE = 8
DEFAULT_MAX_WORKERS = 2


def pack_float32_b64(array):
    return base64.b64encode(np.asarray(array, dtype=np.float32).tobytes()).decode("ascii")


def pack_uint32_b64(array):
    return base64.b64encode(np.asarray(array, dtype=np.uint32).tobytes()).decode("ascii")


def unpack_float32_b64(encoded):
    return np.frombuffer(base64.b64decode(encoded), dtype=np.float32)


def unpack_uint32_b64(encoded):
    return np.frombuffer(base64.b64decode(encoded), dtype=np.uint32)


class RibbonCache:
    def __init__(self, daemon_link, max_size=DEFAULT_MAX_SIZE,
                 max_workers=DEFAULT_MAX_WORKERS):
        self._daemon_link = daemon_link
        self._max_size = max_size
        self._cache = collections.OrderedDict()  # job_id -> event dict
        self._executor = ThreadPoolExecutor(max_workers=max_workers,
                                            thread_name_prefix="ribbon-cache")

    def on_job_done(self, event):
        cif_path = event.get("cif_path")
        job_id = event.get("job_id")
        if not cif_path or not job_id:
            return
        self._executor.submit(self._build, job_id, cif_path)

    def _build(self, job_id, cif_path):
        from ui.cartoon import cartoon_from_cif
        try:
            vertices, normals, colors, indices = cartoon_from_cif(cif_path)
        except Exception:
            # A ligand-only/nucleic-only/malformed structure has no
            # drawable ribbon -- the browser simply stays on its held
            # point cloud, exactly as if this feature did not exist for
            # this fold. Never a crash, never a half-built event.
            log.info("no ribbon for job %s (cif=%s); cartoon_from_cif could "
                     "not build one", job_id, cif_path, exc_info=True)
            return
        event = {
            "type": "ribbon_ready", "job_id": job_id,
            "vertices_b64": pack_float32_b64(vertices),
            "normals_b64": pack_float32_b64(normals),
            "colors_b64": pack_float32_b64(colors),
            "indices_b64": pack_uint32_b64(indices),
            "vertex_count": len(vertices), "index_count": len(indices),
        }
        self._cache[job_id] = event
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
        self._daemon_link.publish(event)

    def get(self, job_id):
        return self._cache.get(job_id)

    def shutdown(self):
        self._executor.shutdown(wait=True, cancel_futures=True)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_ribbon_cache.py -v`
Expected: PASS

- [ ] **Step 5: Wire into `_dispatch` and add the on-demand route**

In `webview/bridge.py`'s `DaemonLink.__init__`:

```python
        from webview.ribbon_cache import RibbonCache
        self.ribbon_cache = RibbonCache(self)
```

In `_dispatch`, after the existing `self.publish(event)` (and after the QaTracker call from
Task 6):

```python
        if event.get("type") == "job_done":
            self.ribbon_cache.on_job_done(event)
```

In `stop()`, add `self.ribbon_cache.shutdown()`.

Add the route in `do_GET`:

```python
            elif path.startswith("/ribbon/"):
                self._serve_ribbon(path[len("/ribbon/"):])
```

```python
        def _serve_ribbon(self, job_id):
            cached = daemon_link.ribbon_cache.get(job_id)
            if cached is None:
                self.send_error(404, "no ribbon cached for this job_id")
                return
            data = json.dumps(cached).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
```

- [ ] **Step 6: Add bridge-level integration tests (dispatch wiring, the on-demand 404, and backpressure)**

```python
# tests/unit/test_webview_bridge.py -- add
def test_job_done_triggers_a_ribbon_and_get_ribbon_serves_it():
    import pathlib, time
    fixtures = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "structures"
    link = bridge.DaemonLink("/nonexistent")
    link._dispatch(json.dumps({
        "type": "job_done", "job_id": "j-int-1",
        "cif_path": str(fixtures / "real_fold_trpcage.cif"),
        "mean_plddt": 95.3,
    }).encode() + b"\n")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and link.ribbon_cache.get("j-int-1") is None:
        time.sleep(0.02)
    assert link.ribbon_cache.get("j-int-1") is not None
    link.ribbon_cache.shutdown()


def test_get_ribbon_404s_honestly_for_an_unknown_job_id():
    """Review Focus: a late-joining tab's on-demand GET /ribbon/<job_id>
    for an unknown or evicted job_id must 404, never hang or 500. Spins
    up a REAL HTTP server (http.server.HTTPServer over make_handler) on
    an ephemeral port, rather than calling the handler method directly,
    so this actually exercises the route dispatch in do_GET."""
    import http.server
    import threading
    import urllib.error
    import urllib.request

    link = bridge.DaemonLink("/nonexistent")
    handler_cls = bridge.make_handler(link)
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ribbon/does-not-exist",
                                   timeout=5)
            assert False, "expected an HTTPError for an unknown job_id"
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        thread.join(timeout=5)
        link.ribbon_cache.shutdown()


def test_a_full_subscriber_queue_does_not_block_a_real_ribbon_ready_for_others():
    """Review Focus: re-asserts Task 1's backpressure contract for a
    REAL ribbon_ready event -- a slow/backgrounded tab must not block
    the ribbon from reaching every other tab."""
    import pathlib
    import queue as queue_module
    import time

    fixtures = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "structures"
    link = bridge.DaemonLink("/nonexistent")
    full_q = link.subscribe()
    for _ in range(bridge.SUBSCRIBER_QUEUE_MAX):
        full_q.put_nowait({"type": "filler"})
    healthy_q = link.subscribe()

    link._dispatch(json.dumps({
        "type": "job_done", "job_id": "j-backpressure-1",
        "cif_path": str(fixtures / "real_fold_trpcage.cif"),
    }).encode() + b"\n")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and link.ribbon_cache.get("j-backpressure-1") is None:
        time.sleep(0.02)
    assert link.ribbon_cache.get("j-backpressure-1") is not None

    assert full_q.qsize() == bridge.SUBSCRIBER_QUEUE_MAX
    received = []
    while True:
        try:
            received.append(healthy_q.get_nowait())
        except queue_module.Empty:
            break
    assert any(e.get("type") == "ribbon_ready" for e in received)
    link.ribbon_cache.shutdown()
```

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -k ribbon -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add webview/ribbon_cache.py tests/unit/test_ribbon_cache.py webview/bridge.py \
        tests/unit/test_webview_bridge.py
git commit -m "feat(webview): server-side ribbon precompute via ui.cartoon, cached + on-demand"
```

---

### Task 11: WebGL camera math and shaders, ported with a drift test

**Files:**
- Create: `webview/static/mat4.js`
- Test: `tests/webview_js/mat4.test.js`
- Create: `webview/static/ribbon_shaders.js`
- Modify: `tests/unit/test_webview_js_constants.py`

**Interfaces:**
- Produces: `Mat4.identity()`, `Mat4.perspective(fovyDeg, aspect, near, far)`,
  `Mat4.lookAt(eye, target, up)`, `Mat4.rotationY(angleRad)`, `Mat4.multiply(a, b)` — all
  returning/accepting a flat `Float32Array(16)`, column-major (matches `ui/mathutil.py`'s
  own stated convention exactly, including the historically-buggy `look_at` basis this
  project already paid to fix once — ported from the ALREADY-CORRECTED source, not
  re-derived).
- Produces: `RIBBON_VERT_SRC`, `RIBBON_FRAG_SRC` — WebGL2 (`#version 300 es`) translations
  of `ui/shaders.py`'s `RIBBON_VERT`/`RIBBON_FRAG`.

- [ ] **Step 1: Write the failing JS tests**

```javascript
// tests/webview_js/mat4.test.js
const assert = require("assert");
const { identity, perspective, lookAt, rotationY, multiply } =
  require("../../webview/static/mat4.js");

const id = identity();
assert.strictEqual(id.length, 16);
assert.strictEqual(id[0], 1); assert.strictEqual(id[5], 1);
assert.strictEqual(id[10], 1); assert.strictEqual(id[15], 1);
assert.strictEqual(id[1], 0);

// look_at from (0,0,5) toward the origin, up=(0,1,0): forward is -Z, so the
// view matrix's third COLUMN (indices 8,9,10 in column-major [col][row])
// should be +Z (since m[:3,2] = -forward = -(-1) = +1 on Z).
const view = lookAt([0, 0, 5], [0, 0, 0], [0, 1, 0]);
assert.ok(Math.abs(view[10] - 1.0) < 1e-6,
  "look_at's forward/-forward basis must match ui/mathutil.py's own convention " +
  "(m[:3,2] = -forward) -- this is the exact bug class this project already " +
  "paid to find once (rows vs columns for a column-major matrix)");

const proj = perspective(60, 1.0, 0.1, 100.0);
assert.ok(proj[0] > 0 && proj[5] > 0);
assert.ok(Math.abs(proj[11] - (-1.0)) < 1e-6);

const rot = rotationY(Math.PI / 2);
assert.ok(Math.abs(rot[0]) < 1e-6, "cos(90deg) ~ 0");
assert.ok(Math.abs(rot[2] - (-1.0)) < 1e-6, "-sin(90deg) = -1");

const m = multiply(identity(), identity());
assert.deepStrictEqual(Array.from(m), Array.from(identity()));

console.log("mat4.test.js: OK");
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/webview_js/mat4.test.js`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `mat4.js`, ported directly from `ui/mathutil.py`**

```javascript
// webview/static/mat4.js
"use strict";

// Column-major 4x4 matrices for WebGL, ported directly from ui/mathutil.py
// -- including its look_at basis, which this project already found and
// fixed a real bug in once (rows where column-major storage needs
// columns, wrong for every off-axis camera -- 2026-08-11 review). Ported
// from the ALREADY-CORRECTED source, not re-derived from scratch, so that
// bug class is not available to reintroduce here. Every matrix is a flat
// Float32Array(16); `gl.uniformMatrix4fv(loc, false, m)` (transpose=false)
// matches this convention exactly, the same way ui/viewer.py's own
// GL.glUniformMatrix4fv(..., GL_FALSE, ...) does.

function identity() {
  const m = new Float32Array(16);
  m[0] = m[5] = m[10] = m[15] = 1;
  return m;
}

function perspective(fovyDeg, aspect, near, far) {
  const f = 1.0 / Math.tan((fovyDeg * Math.PI / 180) / 2.0);
  const m = new Float32Array(16);
  m[0] = f / aspect;
  m[5] = f;
  m[10] = (far + near) / (near - far);
  m[11] = -1.0;
  m[14] = (2.0 * far * near) / (near - far);
  return m;
}

function _sub(a, b) { return [a[0]-b[0], a[1]-b[1], a[2]-b[2]]; }
function _cross(a, b) {
  return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
}
function _dot(a, b) { return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]; }
function _norm(v) {
  const n = Math.hypot(v[0], v[1], v[2]);
  return [v[0]/n, v[1]/n, v[2]/n];
}

function lookAt(eye, target, up) {
  const forward = _norm(_sub(target, eye));
  const side = _norm(_cross(forward, up));
  const trueUp = _cross(side, forward);
  const m = identity();
  // Column-major: m[col*4 + row]. side/trueUp/-forward are COLUMNS 0/1/2,
  // matching ui/mathutil.py's m[:3, 0] = side / m[:3, 1] = true_up /
  // m[:3, 2] = -forward exactly (numpy's [:3, col] on a column-major
  // array is this same column).
  m[0] = side[0]; m[1] = side[1]; m[2] = side[2];
  m[4] = trueUp[0]; m[5] = trueUp[1]; m[6] = trueUp[2];
  m[8] = -forward[0]; m[9] = -forward[1]; m[10] = -forward[2];
  m[12] = -_dot(side, eye);
  m[13] = -_dot(trueUp, eye);
  m[14] = _dot(forward, eye);
  return m;
}

function rotationY(angleRad) {
  const c = Math.cos(angleRad), s = Math.sin(angleRad);
  const m = identity();
  m[0] = c; m[8] = -s;
  m[2] = s; m[10] = c;
  return m;
}

function multiply(a, b) {
  const out = new Float32Array(16);
  for (let col = 0; col < 4; col++) {
    for (let row = 0; row < 4; row++) {
      let sum = 0;
      for (let k = 0; k < 4; k++) sum += a[k*4 + row] * b[col*4 + k];
      out[col*4 + row] = sum;
    }
  }
  return out;
}

const Mat4 = { identity, perspective, lookAt, rotationY, multiply };
if (typeof module !== "undefined") module.exports = Mat4;
if (typeof window !== "undefined") window.Mat4 = Mat4;
```

- [ ] **Step 4: Run to verify it passes**

Run: `node tests/webview_js/mat4.test.js`
Expected: `mat4.test.js: OK`

- [ ] **Step 5: Port the ribbon shaders, WebGL2-compatible**

```javascript
// webview/static/ribbon_shaders.js
"use strict";

// WebGL2 (GLSL ES 300) translations of ui/shaders.py's RIBBON_VERT/
// RIBBON_FRAG. Same uniforms, same lighting math -- see that file for the
// original desktop GLSL 330 core source this is ported from.

const RIBBON_VERT_SRC = `#version 300 es
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
`;

const RIBBON_FRAG_SRC = `#version 300 es
precision highp float;
in vec3 v_normal;
in vec3 v_color;
out vec4 frag_color;

uniform float u_opacity;

void main() {
    vec3 n = normalize(v_normal);
    vec3 light = normalize(vec3(0.4, 0.8, 0.6));
    float diffuse = max(dot(n, light), 0.0);
    float rim = pow(1.0 - abs(n.z), 2.0) * 0.35;
    vec3 shaded = v_color * (0.35 + 0.65 * diffuse) + vec3(rim) * 0.6;
    frag_color = vec4(shaded, u_opacity);
}
`;

if (typeof module !== "undefined") {
  module.exports = { RIBBON_VERT_SRC, RIBBON_FRAG_SRC };
}
if (typeof window !== "undefined") {
  window.RibbonShaders = { RIBBON_VERT_SRC, RIBBON_FRAG_SRC };
}
```

- [ ] **Step 6: Add a drift test comparing the shader's shading math constants**

```python
# tests/unit/test_webview_js_constants.py -- add
def test_ribbon_shader_lighting_constants_match():
    """The fragment shader's lighting formula (light direction, diffuse
    floor/scale, rim exponent) is copied by hand, not derived -- this pins
    the literal numbers against ui/shaders.py's own source text so a typo
    in the port (e.g. 0.65 -> 0.56) is caught here instead of only being
    visible as 'the ribbon looks a bit off' in a screenshot."""
    from ui.shaders import RIBBON_FRAG
    js_src = (pathlib.Path(__file__).resolve().parents[1] / "webview" / "static"
              / "ribbon_shaders.js").read_text()
    for literal in ("0.4, 0.8, 0.6", "0.35, 0.65", "1.0 - abs(n.z), 2.0", "0.35", "0.6"):
        assert literal in RIBBON_FRAG, f"test fixture assumption broken: {literal!r} not in Python source"
        assert literal in js_src, f"ribbon_shaders.js is missing the literal {literal!r} from RIBBON_FRAG"
```

- [ ] **Step 7: Run the drift test**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_js_constants.py -k shader -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add webview/static/mat4.js tests/webview_js/mat4.test.js webview/static/ribbon_shaders.js \
        tests/unit/test_webview_js_constants.py
git commit -m "feat(webview): port camera math (mat4.js) and ribbon shaders to WebGL2"
```

---

### Task 12: WebGL ribbon renderer, wired into the existing point-cloud cell

**Files:**
- Create: `webview/static/ribbon_renderer.js`
- Test: `tests/webview_js/ribbon_renderer.test.js`
- Modify: `webview/static/app.js` (the `Cell` class)
- Modify: `webview/static/index.html`, `webview/static/style.css`

**Interfaces:**
- Consumes: `mat4.js`, `ribbon_shaders.js` (Task 11); the `ribbon_ready` event (Task 10).
- Produces: `RibbonRenderer(canvas)` with `.upload(buffers)` (build GL buffers from the
  unpacked vertex/normal/color/index arrays) and `.draw(rotationAngle, opacity)`.

- [ ] **Step 1: Write the failing JS test (pure buffer-decode logic, no real WebGL context)**

```javascript
// tests/webview_js/ribbon_renderer.test.js
const assert = require("assert");
const { decodeBuffers } = require("../../webview/static/ribbon_renderer.js");

// Same base64 float32 convention as protocol/events.py's pack_coords --
// 2 vertices, little-endian float32.
const buf = Buffer.alloc(6 * 4);
[1.0, 2.0, 3.0, 4.0, 5.0, 6.0].forEach((v, i) => buf.writeFloatLE(v, i * 4));
const b64 = buf.toString("base64");

const decoded = decodeBuffers({
  vertices_b64: b64, normals_b64: b64, colors_b64: b64,
  indices_b64: Buffer.from(new Uint32Array([0, 1, 0]).buffer).toString("base64"),
  vertex_count: 2, index_count: 3,
});
assert.strictEqual(decoded.vertices.length, 6);
assert.ok(Math.abs(decoded.vertices[0] - 1.0) < 1e-5);
assert.strictEqual(decoded.indices.length, 3);
assert.strictEqual(decoded.indices[1], 1);

console.log("ribbon_renderer.test.js: OK");
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/webview_js/ribbon_renderer.test.js`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `ribbon_renderer.js`**

```javascript
// webview/static/ribbon_renderer.js
"use strict";

const Mat4 = (typeof require !== "undefined") ? require("./mat4.js") : window.Mat4;
const { RIBBON_VERT_SRC, RIBBON_FRAG_SRC } =
  (typeof require !== "undefined") ? require("./ribbon_shaders.js") : window.RibbonShaders;

function _b64ToFloat32(b64) {
  const binary = (typeof atob !== "undefined") ? atob(b64) : Buffer.from(b64, "base64").toString("binary");
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Float32Array(bytes.buffer);
}

function _b64ToUint32(b64) {
  const binary = (typeof atob !== "undefined") ? atob(b64) : Buffer.from(b64, "base64").toString("binary");
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Uint32Array(bytes.buffer);
}

// decodeBuffers is pure (no GL) so it is directly testable -- see Step 1.
function decodeBuffers(event) {
  return {
    vertices: _b64ToFloat32(event.vertices_b64),
    normals: _b64ToFloat32(event.normals_b64),
    colors: _b64ToFloat32(event.colors_b64),
    indices: _b64ToUint32(event.indices_b64),
  };
}

function _compileShader(gl, type, src) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, src);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const info = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`shader compile failed: ${info}`);
  }
  return shader;
}

class RibbonRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.gl = canvas.getContext("webgl2");
    this.indexCount = 0;
    if (!this.gl) return;  // no WebGL2 -- ribbon stays unavailable, point cloud unaffected
    const gl = this.gl;
    const vert = _compileShader(gl, gl.VERTEX_SHADER, RIBBON_VERT_SRC);
    const frag = _compileShader(gl, gl.FRAGMENT_SHADER, RIBBON_FRAG_SRC);
    this.program = gl.createProgram();
    gl.attachShader(this.program, vert);
    gl.attachShader(this.program, frag);
    gl.linkProgram(this.program);
    this.uMvp = gl.getUniformLocation(this.program, "u_mvp");
    this.uModel = gl.getUniformLocation(this.program, "u_model");
    this.uOpacity = gl.getUniformLocation(this.program, "u_opacity");
    this.vao = gl.createVertexArray();
    this.vertexBuf = gl.createBuffer();
    this.normalBuf = gl.createBuffer();
    this.colorBuf = gl.createBuffer();
    this.indexBuf = gl.createBuffer();
  }

  upload(event) {
    if (!this.gl) return;
    const gl = this.gl;
    const { vertices, normals, colors, indices } = decodeBuffers(event);
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.vertexBuf);
    gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
    gl.enableVertexAttribArray(0);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.normalBuf);
    gl.bufferData(gl.ARRAY_BUFFER, normals, gl.STATIC_DRAW);
    gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 0, 0);
    gl.enableVertexAttribArray(1);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.colorBuf);
    gl.bufferData(gl.ARRAY_BUFFER, colors, gl.STATIC_DRAW);
    gl.vertexAttribPointer(2, 3, gl.FLOAT, false, 0, 0);
    gl.enableVertexAttribArray(2);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.indexBuf);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indices, gl.STATIC_DRAW);
    this.indexCount = indices.length;
  }

  draw(rotationAngle, opacity) {
    if (!this.gl || this.indexCount === 0) return;
    const gl = this.gl;
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.enable(gl.DEPTH_TEST);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(this.program);
    const model = Mat4.rotationY(rotationAngle);
    const view = Mat4.lookAt([0, 0, 40], [0, 0, 0], [0, 1, 0]);
    const proj = Mat4.perspective(45, this.canvas.width / this.canvas.height, 0.1, 500.0);
    const mvp = Mat4.multiply(proj, Mat4.multiply(view, model));
    gl.uniformMatrix4fv(this.uMvp, false, mvp);
    gl.uniformMatrix4fv(this.uModel, false, model);
    gl.uniform1f(this.uOpacity, opacity);
    gl.bindVertexArray(this.vao);
    gl.drawElements(gl.TRIANGLES, this.indexCount, gl.UNSIGNED_INT, 0);
  }
}

if (typeof module !== "undefined") module.exports = { RibbonRenderer, decodeBuffers };
if (typeof window !== "undefined") window.RibbonRenderer = RibbonRenderer;
```

- [ ] **Step 4: Run to verify it passes**

Run: `node tests/webview_js/ribbon_renderer.test.js`
Expected: `ribbon_renderer.test.js: OK`

- [ ] **Step 5: Wire into `app.js`'s `Cell` class**

Add a second, stacked canvas per cell (CSS `position: absolute`, same size, initial
`opacity: 0`) and a `RibbonRenderer` instance:

```javascript
// In Cell's constructor, after the existing 2D canvas is created:
    this.ribbonCanvas = document.createElement("canvas");
    this.ribbonCanvas.width = 480; this.ribbonCanvas.height = 360;
    this.ribbonCanvas.className = "ribbon-canvas";
    this.el.appendChild(this.ribbonCanvas);
    this.ribbonRenderer = new RibbonRenderer(this.ribbonCanvas);
    this.ribbonOpacity = 0.0;
    this.ribbonAngle = 0.0;
```

Add a handler and extend `draw()`:

```javascript
  onRibbonReady(event) {
    this.ribbonRenderer.upload(event);
    this.ribbonOpacity = 0.0;  // cross-fade in, not a hard cut -- see the spec
  }

  // In the existing draw() method, after the 2D point-cloud draw call:
    if (this.ribbonRenderer.indexCount > 0) {
      this.ribbonOpacity = Math.min(1.0, this.ribbonOpacity + 0.02);
      this.ribbonAngle += 0.004;
      this.ribbonRenderer.draw(this.ribbonAngle, this.ribbonOpacity);
      this.ribbonCanvas.style.opacity = String(this.ribbonOpacity);
    }
```

In `handleEvent`'s `job_start` case, reset the ribbon state for that cell (a new fold
starting must not keep showing the previous one's finished ribbon at full opacity):

```javascript
    case "job_start": {
      // ... existing body ...
      if (cell) { cell.ribbonRenderer.indexCount = 0; cell.ribbonOpacity = 0.0; }
      break;
    }
```

Add the new case:

```javascript
    case "ribbon_ready": {
      const cell = cellForJob(event.job_id);
      if (cell) cell.onRibbonReady(event);
      break;
    }
```

- [ ] **Step 6: Add CSS**

```css
.cell { position: relative; }
.ribbon-canvas {
  position: absolute; top: 0; left: 0; width: 100%; height: 100%;
  transition: opacity 0.3s ease-in-out;
}
```

Add the new scripts to `index.html` in dependency order:

```html
<script src="/mat4.js"></script>
<script src="/ribbon_shaders.js"></script>
<script src="/ribbon_renderer.js"></script>
```

- [ ] **Step 7: Verify live, real browser, real fixture**

```bash
.venvs/venv-ui/bin/python3 -c "
from runner.mock import MockRunner, load_stream
import time
r = MockRunner('/tmp/webview-verify-ribbon.sock', load_stream('tests/fixtures/streams/quad_fold.jsonl'), speed=1.0)
r.start()
time.sleep(60)
" &
sleep 1
.venvs/venv-ui/bin/python3 -m webview.bridge --daemon-socket /tmp/webview-verify-ribbon.sock --port 8127 &
sleep 1
google-chrome --headless --disable-gpu --no-sandbox --window-size=1600,900 \
  --enable-webgl --ignore-gpu-blocklist \
  --screenshot=/tmp/webview-ribbon-early.png http://127.0.0.1:8127/ 2>&1 | tail -5
sleep 8
google-chrome --headless --disable-gpu --no-sandbox --window-size=1600,900 \
  --enable-webgl --ignore-gpu-blocklist \
  --screenshot=/tmp/webview-ribbon-later.png http://127.0.0.1:8127/ 2>&1 | tail -5
```

Look at both screenshots (the `Read` tool). `quad_fold.jsonl` is a short, fast-cycling
fixture (per its own use elsewhere in this codebase), so the later screenshot should show
at least one cell's point cloud replaced/overlaid by an actual shaded ribbon mesh, not just
dots. If headless Chrome's software WebGL renders nothing (a real risk in some headless
environments), note this explicitly in the task report rather than silently calling it
done — real hardware verification (Task 13) is the fallback proof either way. Kill both
background processes after.

- [ ] **Step 8: Run the JS suite and commit**

Run: `scripts/test-webview-js.sh`
Expected: PASS

```bash
git add webview/static/ribbon_renderer.js tests/webview_js/ribbon_renderer.test.js \
        webview/static/app.js webview/static/index.html webview/static/style.css
git commit -m "feat(webview): WebGL ribbon renderer, cross-fading in over the point cloud"
```

---

### Task 13: End-to-end verification, docs, real hardware

**Files:**
- Modify: `webview/README.md`
- Modify: `tests/fixtures/streams/quad_fold.jsonl` or a new fixture (only if a `job_done`
  with a real `cif_path` is not already present in an existing fixture — check first)
- No new production code; this task is verification and documentation only.

- [ ] **Step 1: Confirm (or add) a mock fixture exercising the full new surface**

```bash
grep -c '"type":"job_done"' tests/fixtures/streams/quad_fold.jsonl tests/fixtures/streams/with_question.jsonl
grep '"cif_path"' tests/fixtures/streams/*.jsonl
```

If no existing fixture's `job_done` line carries a `cif_path`, add one field to one
`job_done` line in `tests/fixtures/streams/quad_fold.jsonl` pointing at
`tests/fixtures/structures/real_fold_trpcage.cif` (an absolute path resolved relative to
the repo root at test time, matching how other fixtures already reference real files) so
the mock-runner path exercises `RibbonCache` too, not only the hardware path.

- [ ] **Step 2: One full mock-runner-driven verification pass, every new surface**

```bash
.venvs/venv-ui/bin/python3 -c "
from runner.mock import MockRunner, load_stream
import time
r = MockRunner('/tmp/webview-final-verify.sock', load_stream('tests/fixtures/streams/quad_fold.jsonl'), speed=1.0)
r.start()
time.sleep(90)
" &
sleep 1
.venvs/venv-ui/bin/python3 -m webview.bridge --daemon-socket /tmp/webview-final-verify.sock --port 8128 &
sleep 1
timeout 10 curl -s -N http://127.0.0.1:8128/events > /tmp/webview-final-events.log
grep -o '"type": *"[a-z_]*"' /tmp/webview-final-events.log | sort -u
```

Expected output includes every one of: `hello`, `job_start`, `stage`, `frame`, `job_done`,
`telemetry` (if this box has real `tt-smi`), `qa_queue` (if the fixture asks a question),
`playlist_catalog`, `questions_catalog`, `ribbon_ready` (if Step 1 added a `cif_path`). Kill
both background processes after. If any expected type is missing, that task's own wiring
has a gap — go back and fix it there, not here.

- [ ] **Step 3: Run the FULL test suite (Python + JS)**

Run: `scripts/test.sh`
Expected: `OVERALL: PASS`, including the new JS leg from Task 1.

- [ ] **Step 4: Update `webview/README.md`'s scope-cut list**

The five original items become: (a) ribbon rendering — **closed** (Task 10-12); (b) real
gallery — **closed** (Task 8); (c) real question text — **closed** (Task 8); (d) the easter
egg — **still deliberately dropped**, unchanged reasoning; (e) full frame-level reconnect
backfill — **still deliberately out of scope**, unchanged reasoning (a reopened tab gets
the latest hello/catalogs/telemetry/qa_queue snapshot via priming, but not a replay of
frames from a fold already in progress — name this precisely rather than overclaiming).

- [ ] **Step 5: Real hardware verification (the one task that needs it)**

Take a `gozer` lease (never raw `tt-smi -r`; see this project's CLAUDE.md), start the real
daemon (`scripts/run-demo.sh`) and the bridge pointed at its real socket, open the page in a
real browser, and confirm live: real telemetry numbers changing, the Tensix panel animating
with real activity, a real question asked and answered through the queue, and a real
finished fold cross-fading into a WebGL ribbon. This is the "one real fold is the only proof
the load path works" standard this project holds every daemon-adjacent change to — screenshot
or describe what was actually seen in the task report, not just "tests passed."

- [ ] **Step 6: Commit**

```bash
git add webview/README.md tests/fixtures/streams/quad_fold.jsonl
git commit -m "docs(webview): close three of five scope-cuts; end-to-end + real hardware verified"
```
