# tt-bio-demo Phase 5: Multi-chip folding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four Blackhole chips fold four proteins at once, one per chip, shown as a 2×2 quad. The booth stops being a four-chip machine with one chip working.

**Architecture:** The daemon stops owning a device. It becomes a parent that owns the queue, the card pool, the socket and four subprocesses — one per chip, each holding its own `Folder` and its own resident model. Worker events are multiplexed onto the existing socket **unchanged**: the wire protocol does not move. The UI stops assuming one fold in flight and keys its per-fold state by `job_id`.

**Tech Stack:** Python 3.12; `tt_bio.runtime` / `tt_bio.main` for device assignment; GTK 4.14 via PyGObject; the project's own `protocol/events.py`.

**Spec:** [`../specs/2026-08-13-multi-chip-folding.md`](../specs/2026-08-13-multi-chip-folding.md) — it is authoritative and it is already grounded in a hardware spike run on 2026-08-13. Do not re-derive its findings; build on them.
**Read before starting:** [`../../followups.md`](../../followups.md), especially "Short runs cannot see unbounded growth" and "Write tests that can fail". Both are load-bearing here: this phase multiplies every log writer by four and rewrites the most heavily-tested module in the runner.

**Amended 2026-08-13, after the first draft was reviewed:** the first draft declared a deviation — the socket was one-way, so a visitor's pick could only *nominate* a target and take the focus when the attract loop reached it. That has been decided the other way: **a pick starts folding.** The protocol therefore gains a client→server message and `PROTOCOL_VERSION` goes `1` → `2`; the deviation is gone rather than deferred. Three tasks carry it — Task 3 (the message), Tasks 4 and 5 (the two directions of the socket), Task 9 (the daemon turning a pick into a dispatched fold) — and Task 17 connects the tap and rewrites every visitor-facing string that used to say a pick starts nothing. The tasks after Task 3 were renumbered accordingly; there is no Task 4 or 5 from the first draft still hiding under an old number.

---

## How this plan works, and why

Phase 3a's review loop found **nineteen defects in the plan's own reference code**, faithfully transcribed by implementers. Phase 3b's whole-branch review then mutation-tested the suite and found the three survivors all asserted on something *adjacent* to the behaviour rather than the behaviour itself.

So, exactly as in the Phase 3b plan:

- **The tests are the specification.** They are given in full. **Implementations are deliberately NOT given.** You are expected to design and write the code, not transcribe it.
- **Every test names the mutation it must catch.** Before you mark a task done, apply that mutation, watch the test go red, revert, watch it go green. Report both. **A test that does not fail against its named mutation is not finished, regardless of what it asserts.**
- **If a constraint here is wrong, say so.** Nineteen times an implementer's pushback was the thing that saved the build.

---

## Global Constraints

**Interpreters**

- Never a bare `python3`. UI runs under `.venvs/venv-ui/bin/python3`; runner under `.venvs/venv-runner/bin/python3`. The worker subprocess is spawned with `sys.executable`, which inside the daemon *is* venv-runner's interpreter — never a hardcoded path, never `"python3"`.
- `./scripts/test.sh` plain must stay green. Baseline at the start of this phase: **525 UI + 134 runner**. Hardware tests are opt-in via `--hw` and must never run by default.

**Boundaries**

- **Do not shell out to `tt-bio predict`.** It returns finished structures; the booth's entire premise is the live per-denoising-step trajectory, which comes from the `dump_fn` tap on the Python API (`runner/dump_tap.py` → `runner/folder.py`). What this phase borrows from tt-bio is its *worker and device machinery*; the fold loop and the event stream stay ours. `runner/folder.py` does not change.
- UI code never imports torch or tt-bio. `protocol/` stays stdlib + numpy only — both venvs import it.
- `protocol/events.py` changes in exactly one way, in Task 3: it gains a client→server direction (`CLIENT_MESSAGE_TYPES`, `pick_message`, `encode_client_message`, `decode_client_message`) and `PROTOCOL_VERSION` goes `1` → `2`. `EVENT_TYPES` — the server→client vocabulary — gains nothing, and no worker control line may appear in either set. Task 3 makes the change and pins every part of it, in both venvs.
- Runner-side tests go in `tests/unit/runner/`; everything else in `tests/unit/`. The split is by directory (see `scripts/test.sh`'s header).

**Hardware**

- Never `tt-smi -r`. Never leave a process holding a device.
- The machine is shared. Hardware is available **today** and may be taken back at any time. Every task below is marked **[no device]** or **[hardware]**. All seventeen implementation tasks are **[no device]**; only Tasks 18 and 19 need silicon, and they are last on purpose.
- The parent daemon opens **no device**, ever. It may import `ttnn` (it already does, via preflight's tap check) but it never calls `get_device()` and never holds a `DeviceLease`.

**GTK**

- GTK is touched only from the main loop. An unhandled exception in a GLib callback silently freezes that source forever — every GLib-invoked callback carries a broad guard with the `return` **outside** the `try`. Preserve that shape in everything you add.
- Nothing in the UI may ever display a stack trace or raw error text.
- Every `Gtk.Label` carries a colour-bearing CSS class and clears ≥4.5:1 contrast; `tests/unit/_legibility.py`'s `assert_every_label_is_legible` enforces it. A new widget module registers its own `_BACKGROUND_BY_CLASS` and is added to a legibility test — that is not optional decoration, it is how the guard sees the widget at all.

**Exact values this phase introduces** (all of them; there are no others to invent)

| Name | Value | Where | Why this number |
|---|---|---|---|
| `MAX_SLOTS` | `4` | `ui/slots.py` | The quad is 2×2. A fifth card on a bigger machine is shown in telemetry and folds, but gets no cell. |
| `MAX_WORKERS` | `4` | `runner/workers.py` | The cap handed to `detect_tenstorrent_devices`. Deliberately equal to `MAX_SLOTS` but a *different* constant in a different venv: the runner may not import `ui.*` and the two answer different questions ("how many chips may we fold on" vs "how many cells fit"). |
| `EVENT_FD` | `3` | `runner/workers.py` | Events leave the worker on a dedicated inherited fd, never stdout — tt-metal writes to fd 1/2 at the C++ level and would shred a JSON stream. |
| `CONTROL_PREFIX` | `"worker."` | `runner/workers.py` | Namespace for parent↔worker control lines, which must never reach the socket. |
| `CONTROL_READY` | `"worker.ready"` | `runner/workers.py` | Model resident, device open, ready for a first job. |
| `CONTROL_IDLE` | `"worker.idle"` | `runner/workers.py` | Finished with a job; the authoritative dispatch-readiness signal. |
| `CONTROL_FATAL` | `"worker.fatal"` | `runner/workers.py` | The worker is about to exit and cannot serve again. |
| `WORKER_READY_TIMEOUT_S` | `180.0` | `runner/pool.py` | Cold model load measured 3.1 s solo, 6.4–9.2 s under four-way contention (spec, "Verified by spike"); 180 s covers a cold checkpoint/mol resolution on top of that with an order of magnitude of margin. |
| `WORKER_RESTART_DELAY_S` | `5.0` | `runner/pool.py` | Same backoff `runner/daemon.py` already uses for `LOAD_RETRY_PERIOD_S` and "no schedulable cards". One number, one meaning: "this needs a moment or a human". |
| `WORKER_RETIRE_AFTER` | `3` | `runner/pool.py` | Consecutive deaths with no completed job before a card is dropped from rotation for the session. Mirrors `QUARANTINE_AFTER = 3` for targets. |
| `WORKER_STOP_GRACE_S` | `10.0` | `runner/pool.py` | SIGTERM→SIGKILL grace on shutdown. Long enough for `ttnn` teardown (measured ~1–2 s), short enough that a booth restart is not a wait. |
| `WORKER_LOG_CAP_BYTES` | `64 * 1024**2` | `runner/pool.py` | Per-worker stdout/stderr cap, truncated in place. Four of these is 256 MB, comfortably inside the 2 GB `--log-budget-gb` default. |
| `PROTECTED_STRUCTURE_COUNT` | `3` **per card** | `runner/daemon.py` | Unchanged in value; changed in scope — one deque per card, not one shared. |
| `_SHOWCASE_DWELL_S` | `2.0` | `ui/app.py` | Unchanged. It is now a **per-slot** dwell (Task 12). |
| worker log root | `<log-root>/card-<n>` | `runner/workers.py` | One `TT_METAL_LOGS_PATH` per worker, so four writers cannot interleave into one tree and a crash's logs are attributable. |
| worker structures dir | `/tmp/tt-bio-demo/structures/device-<n>` | already in `runner/folder.py` | Already namespaced by `device_id`. No change; Task 11 makes the pruner walk all four. |
| `PROTOCOL_VERSION` | `2` (was `1`) | `protocol/events.py` | The contract gained a direction. `hello` already carries the version and `ui/client.py` already refuses a mismatch — see Task 3's ruling for what that refusal does in each direction. |
| `CLIENT_MESSAGE_TYPES` | `frozenset({"pick"})` | `protocol/events.py` | A **separate** vocabulary from `EVENT_TYPES`, so `encode` structurally cannot put a pick on the wire to a UI and `decode` structurally cannot accept one as an event. |
| `MAX_TARGET_ID_LEN` | `64` | `protocol/events.py` | A `target_id` arrives from another process. Longer than the longest playlist stem by a wide margin, short enough that a hostile client cannot choose how much the daemon allocates. |
| `CLIENT_LINE_MAX_BYTES` | `64 * 1024` | `runner/server.py` | The cap on one unterminated client line. Same reasoning, one layer down: without it, a client that never sends a newline decides the daemon's memory use. |
| `OUTBOX_MAX` | `8` | `ui/client.py` | Picks queued while disconnected are dropped oldest-first. A pick means "fold this now"; one delivered ninety seconds later, to a visitor who has gone, is worse than none. |
| `VISITOR_PRIORITY` | `10` | `runner/queue.py` | Any value above `0` works — the queue is already priority-ordered and has simply never been given one. `10` leaves room for a band between the playlist and a visitor without renumbering either. |
| `MAX_PENDING_PICKS` | `1` | `runner/daemon.py` | One visitor, one pick — the same thing the UI tracks with a single `selected_target`. A new pick replaces the pending one, which is what bounds a child tapping forty targets in ten seconds. |
| `DISPATCH_POLL_S` | `0.25` | `runner/daemon.py` | The busy-path poll. The 5 s and 10 s idle backoffs keep their numbers but become interruptible by `Daemon._wake`, so a pick is not discovered ten seconds late. |
| `PICK_PENDING_WARN_S` | `10.0` | `ui/slots.py` | How long a pick may sit unstarted before the booth says more than "next up". Roughly two folds' worth of wait: long enough not to fire in the ordinary case, short enough to beat the twenty seconds after which a visitor concludes the booth is broken. |

**Commit after every task**, with conventional-commit prefixes.

---

## One correction to the spec, declared up front

A reviewer should judge this rather than report it as a gap. (The first draft declared a second one — that a pick could not reach the daemon. It has been resolved rather than deferred; see the amendment note at the top and Tasks 3, 4, 5, 9 and 17.)

**`tt_bio.main` is NOT importable without importing ttnn.** The spec says `tt_bio.runtime` is ttnn-free — that is **true and verified**:

```
tt_bio.runtime -> ttnn imported: False | torch: False
tt_bio.main    -> ttnn imported: True  | torch: True
```

`_build_worker_device_assignments`, `_detect_p300_devices` and `_find_ttnn_mesh_graph_descriptor` are *themselves* ttnn-free — they read `/sys/class/tenstorrent` and use `importlib.util.find_spec("ttnn")`, which does not import it — but the **module they live in** imports `ttnn` and `torch` at module scope. Importing them in the parent therefore pulls ttnn into the parent process.

This does not break the design, and the reason is worth stating because it is what makes the whole architecture safe: **importing ttnn opens no device — only `get_device()` does** — and the parent computes the assignments and hands them to each child through `subprocess.Popen(env=...)`. The child's environment is therefore complete *before its interpreter starts*, which is a stronger ordering guarantee than "set the variable before the import" could ever be. The parent already imports tt-bio today (`runner/preflight.py`'s tap check imports `tt_bio.protenix`), so this is the status quo, not a new cost.

Consequence: `runner/workers.py` imports tt-bio **lazily, inside functions**, the same discipline `runner/folder.py` and `runner/daemon.py` already follow — so the module itself, and the 134 existing runner tests, never pay for ttnn. Task 1 pins this with a test.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `runner/workers.py` | **new** | What a worker is: `WorkerSpec`, the device assignment, the environment, the control vocabulary. No subprocess management, no threads. |
| `runner/worker.py` | **new** | The worker process itself: `python3 -m runner.worker`. Holds one `Folder`, reads commands on stdin, writes events on `EVENT_FD`. |
| `runner/pool.py` | **new** | `WorkerPool`: spawn, dispatch, multiplex, notice death, respawn, retire. The only module that owns subprocesses or reader threads. |
| `runner/daemon.py` | rewritten | Parent: queue, `CardPool`, `EventServer`, pool. Owns no `Folder` and no device. |
| `runner/folder.py` | unchanged | Now instantiated inside a worker instead of inside the daemon. Its `device_id` finally means something. |
| `runner/cards.py` | unchanged | Already multi-index. Task 10 exercises it for the first time. |
| `runner/queue.py` | extended | Already priority-ordered and thread-safe. Task 9 gives it `VISITOR_PRIORITY` and `remove()`, makes its visitor path reachable for the first time, and rewrites the docstring that says it cannot be. |
| `runner/server.py` | extended | Gains the client→server direction (Task 4): one reader thread per client, `on_client_message`, and a `broadcast` whose send loop is finally inside its lock. |
| `protocol/events.py` | **extended, then pinned** | One client→server message and a version bump (Task 3). `EVENT_TYPES` does not move. |
| `ui/slots.py` | **new** | Per-fold state, pure: `SlotState` (one cell's dwell), `SlotRouter` (job_id → slot, focus slot, the visitor's pick and how long it has waited). No GTK. |
| `ui/quad.py` | **new** | `QuadView`: four `StructureViewer`s in a 2×2 grid with per-cell captions. Assembly only. |
| `ui/viewer.py` | unchanged | Stays a **single-structure** renderer. That is the point: everything it learned about camera ownership, blend targets and per-job reset is per-cell machinery already. |
| `ui/states.py` | narrowed | Keeps `attract`/`gallery`/`folding`/`preparing`, the deferred touch and the idle timeout. Its `showcase` now follows the **focus slot**. |
| `ui/app.py` | rewritten in parts | Wiring: per-slot frame buffers, per-slot ribbon generations, per-slot deferred clears. Still makes no decisions of its own. |
| `ui/client.py` | extended | Gains a send direction — a bounded outbox and a sender thread (Task 5) — and `LatestFrameByJob` beside `LatestFrame` (Task 15), same latest-wins contract, one slot per `job_id`. |
| `ui/chipviz.py` | extended | Per-chip modes instead of one folding chip. |
| `ui/gallery.py` | copy only | `_CAPTION_BODY` stops saying the booth folds "one after another"; `_CARD_HINT` and the module docstring stop saying a pick starts nothing, because it now does (Task 17). |
| `ui/diagnostics.py` | copy only | `STAGE_TEACHING` stops saying the fold runs on one chip. Its log gains the card. |
| `ui/panels.py` | unchanged | `TelemetryPanel` already shows all four chips and is already independent of the socket. Do not couple it to the pool. |
| `tests/fixtures/streams/make_quad_fold.py` | **new** | Generates `quad_fold.jsonl`: four interleaved folds on four cards. The instrument that makes the whole UI half testable with no hardware. |

**Deliberate boundary:** `runner/workers.py`, `ui/slots.py` and `tests/fixtures/streams/make_quad_fold.py` contain no subprocesses, no GTK and no hardware, so the two genuinely hard questions in this phase — "which chip gets which environment" and "which cell does this event belong to" — are answerable in a unit test.

---

## Per-fold vs global: the table that decides the whole UI

The brief calls this out because getting it wrong reintroduces defects that cost Phase 3b several review rounds. **This table is normative.** If you find yourself needing to move something between the columns, that is a plan bug — raise it, do not just do it.

| Machinery | Today | After this phase | Why |
|---|---|---|---|
| `StructureViewer` (points, ribbon, blend, buffers) | one | **per slot** (4) | Four structures on screen at once. |
| Camera ownership (`_camera_subject`, `_camera_framed`, `_SUBJECT_*`) | one | **per slot**, rule unchanged | Each cell frames its own structure. The rule "the camera frames whatever is actually on screen" is *already* per-viewer; nothing about it becomes global. Do not touch `ui/viewer.py`'s camera code. |
| `clear_structure()` on `job_start` | one | **per slot** | Fold N+1 on chip 2 must not clear chip 0's cell. |
| Deferred clear (`_deferred_clear`) | one bool | **per slot** (a set of slots) | Same reason. A global bool would clear all four cells when one dwell expired. |
| Ribbon generation counter (`_ribbon_generation`) | one int | **per slot** (`dict slot → int`) | A global counter means a `job_done` on chip 3 invalidates chip 0's in-flight ribbon build. This is the single most likely silent defect in the whole UI change. |
| `_pending_ribbon` | one slot | **per slot** (`dict slot → tuple`) | Four builds can be in flight at once; one shared slot loses three of them. |
| `LatestFrame` buffer | one | **per job** (`LatestFrameByJob`) | Four frame streams fighting for one latest-wins slot means every cell shows whichever fold was fastest. |
| Showcase dwell | global, in `StateMachine` | **per slot**, in `SlotState` | Cell 1 is mid-diffusion while cell 0 holds a finished structure. A global dwell suppresses frames in all four. |
| `points_are_visible` / `ribbon_may_be_revealed` / `showcase_ended` | on booth state | **on slot state** | Same reason. `showcase_ended(previous, current)` is reused verbatim — `SlotState` uses the same `"showcase"` string, so the existing function works on both and must not be duplicated. |
| Pipeline panel (`set_stage_from_wire`) | fed by every `stage` | **fed only by the focus slot's job** | One panel, one bar. A second job's stage events would make it run backwards. |
| Booth state (`attract`/`gallery`/`folding`/`preparing`) | global | **stays global** | One booth, one screen, one visitor. |
| Booth `showcase` state | global | **stays global, but follows the focus slot** | It exists so the gallery, the idle timeout and the preparing overlay keep working. Frame suppression no longer reads it. |
| Deferred touch (`_deferred_touch`) | global | **stays global** | It is about the visitor, not about a fold. |
| The visitor's pick (`selected_target`) | global, and unreachable | **stays global**, in `SlotRouter` | One visitor, one pick. It *selects* a slot — the focus — but it is not per-slot state, and a second copy of it in `ui/app.py` is how the daemon and the screen end up disagreeing about what was asked for. |
| 45 s idle timeout, `_last_input_at` | global | **stays global** | Ditto. |
| Help / diagnostics / Tensix overlays and their idle timers | global | **stays global** | Chrome, laid over whatever the booth is doing. Unchanged. |
| `TelemetrySampler` + `TelemetryPanel` | global, 4 chips | **stays global, unchanged** | It already shows all four chips and is already independent of the socket. Do not couple it to the pool. |
| `missing` / `display_message` / preparing overlay | global | **stays global** | A daemon that cannot fold is a booth-wide fact. |
| `_drop_counts`, `DiagnosticsLog` | global | **stays global** | One log for the booth. Diagnostics lines gain the card. |
| Tensix chip modes (`set_folding_chip`) | one chip | **per chip** (`set_chip_stages`) | The whole reason the panel had to be walked back to stay honest. |

---

## Task order and hardware exposure

Tasks 1–17 need **no device** and can be completed if the hardware is taken away tomorrow. Tasks 18 and 19 are the only ones that need silicon, and they are deliberately last and self-contained: if hardware disappears mid-phase, everything up to Task 17 still lands, `./scripts/test.sh` is still green, and the branch is still mergeable behind the existing single-card `--devices 0` path.

---

### Task 1: What a worker is — device assignment and environment [no device]

**Files:** Create `runner/workers.py`. Test: `tests/unit/runner/test_worker_specs.py`

**Why first:** Everything downstream needs to know which chips exist and exactly what environment each worker gets. Getting the p300 mesh-graph descriptor wrong is, per the spec, "the most likely cause of a worker that opens a device and then behaves strangely" — and it is entirely decidable without a device, because `_detect_p300_devices` reads sysfs and `_find_ttnn_mesh_graph_descriptor` only resolves a path.

**Produces:**

- `WorkerSpec` — a frozen dataclass: `card: int`, `label: str`, `visible_devices: str`, `logical_device_id: int`, `mesh_graph_descriptor: str | None`.
- `worker_specs(device_ids=None, max_workers=MAX_WORKERS) -> list[WorkerSpec]`, built from `tt_bio.runtime.detect_tenstorrent_devices` → `tt_bio.runtime.build_local_workers` → `tt_bio.main._build_worker_device_assignments`.
- `worker_environ(spec, *, log_root, n_workers, base=None) -> dict[str, str]`.
- `WorkerSpecError(Exception)`.
- The control vocabulary: `EVENT_FD`, `CONTROL_PREFIX`, `CONTROL_READY`, `CONTROL_IDLE`, `CONTROL_FATAL`, `control(kind, **fields) -> dict`, `is_control(event) -> bool`.

**Reuse, do not reinvent** (spec, "Use tt-bio's own worker machinery"): `detect_tenstorrent_devices` validates a requested id against `/dev/tenstorrent` and errors clearly on a typo; `_build_worker_device_assignments` carries the p300 MGD handling. Do not hand-roll either. Do **not** invent mutual exclusion between workers either: `tt_bio.tenstorrent.get_device()` already takes a `DeviceLease` **and** serializes bring-up host-wide through `_device_init_lock` (a flock at `/tmp/tt-bio-device-open.lock`, which exists because concurrent UMD device init deadlocks and can bring a chip up "remote-only"). Four workers opening at once is precisely the case that lock was written for; you get it for free by calling `Folder.load()`.

- [ ] **Step 1: Write the failing tests**

```python
import importlib
import sys

import pytest

from runner.workers import (
    CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY, EVENT_FD, WorkerSpec,
    WorkerSpecError, control, is_control, worker_environ, worker_specs,
)


class _FakeSlot:
    """Stands in for tt_bio.runtime.WorkerSlot."""

    def __init__(self, device_id, host="quietbox"):
        self.device_id = device_id
        self.worker_id = f"{host}:tt:{device_id}"
        self.label = f"{host}:tt{device_id}"


@pytest.fixture
def fake_tt_bio(monkeypatch):
    """Replace the three tt-bio entry points with recorders.

    Deliberately NOT a stub that returns a fixed answer: each call's
    arguments are recorded, because half of what this module has to get
    right is *what it asks tt-bio for*.
    """
    calls = {}

    def detect(device_ids, num_devices, max_workers):
        calls["detect"] = (device_ids, num_devices, max_workers)
        return [0, 1, 2, 3]

    def build(accelerator, jobs, devices):
        calls["build"] = (accelerator, len(jobs), list(devices))
        return [_FakeSlot(d) for d in devices]

    def assignments(devices):
        calls["assign"] = list(devices)
        return {d: {"visible_devices": str(d), "logical_device_id": 0,
                    "mesh_graph_descriptor": "/mgd/p150.textproto"}
                for d in devices}

    import runner.workers as mod
    monkeypatch.setattr(mod, "_detect_tenstorrent_devices", detect, raising=False)
    monkeypatch.setattr(mod, "_build_local_workers", build, raising=False)
    monkeypatch.setattr(mod, "_worker_device_assignments", assignments, raising=False)
    return calls


def test_one_spec_per_detected_chip(fake_tt_bio):
    specs = worker_specs()
    assert [s.card for s in specs] == [0, 1, 2, 3]
    assert all(isinstance(s, WorkerSpec) for s in specs)


def test_each_spec_is_pinned_to_its_own_chip(fake_tt_bio):
    """The failure the spike existed to rule out: a worker that says chip 3
    and opens chip 0. visible_devices is the only thing that decides."""
    for spec in worker_specs():
        assert spec.visible_devices == str(spec.card)
        assert spec.logical_device_id == 0


def test_the_p300_mesh_graph_descriptor_reaches_every_spec(fake_tt_bio):
    """A lone P300 is a custom topology; without the 1x1 MGD the chip opens
    and then behaves strangely (spec)."""
    assert all(s.mesh_graph_descriptor == "/mgd/p150.textproto"
               for s in worker_specs())


def test_a_requested_device_list_is_passed_through_for_validation(fake_tt_bio):
    """detect_tenstorrent_devices is what turns a typo into a clear error.
    Filtering the list ourselves afterwards would skip that."""
    worker_specs(device_ids="0,2")
    assert fake_tt_bio["detect"][0] == "0,2"


def test_a_bad_device_id_becomes_a_WorkerSpecError(monkeypatch):
    import runner.workers as mod

    def detect(device_ids, num_devices, max_workers):
        raise ValueError("Requested Tenstorrent device id(s) [7] not available")

    monkeypatch.setattr(mod, "_detect_tenstorrent_devices", detect, raising=False)
    with pytest.raises(WorkerSpecError, match="7"):
        worker_specs(device_ids="7")


def test_no_chips_is_an_error_not_an_empty_booth(monkeypatch):
    import runner.workers as mod
    monkeypatch.setattr(mod, "_detect_tenstorrent_devices",
                        lambda *a, **k: [], raising=False)
    with pytest.raises(WorkerSpecError):
        worker_specs()


def test_the_environment_pins_visibility_before_the_interpreter_starts(fake_tt_bio):
    spec = worker_specs()[2]
    env = worker_environ(spec, log_root="/logs", n_workers=4, base={})
    assert env["TT_VISIBLE_DEVICES"] == "2"
    assert env["TT_BIO_LOGICAL_DEVICE_ID"] == "0"
    assert env["TT_MESH_GRAPH_DESC_PATH"] == "/mgd/p150.textproto"


def test_each_worker_gets_its_own_tt_metal_log_root(fake_tt_bio):
    """Four writers into one tree makes a crash unattributable, and makes
    the pruner's oldest-first sweep delete another worker's evidence."""
    roots = {worker_environ(s, log_root="/logs", n_workers=4,
                            base={})["TT_METAL_LOGS_PATH"]
             for s in worker_specs()}
    assert len(roots) == 4
    assert all(r.startswith("/logs/") for r in roots)


def test_the_inspector_stays_off_in_every_worker(fake_tt_bio):
    """runner/env.py turned Inspector off because it holds a log file open
    and writes 13-14 MB/s into it after unlink. Four workers is four of
    those."""
    env = worker_environ(worker_specs()[0], log_root="/logs", n_workers=4,
                         base={})
    assert env["TT_METAL_INSPECTOR"] == "0"


def test_host_threads_are_capped_for_the_number_of_workers(fake_tt_bio):
    """tt-bio documents this exact case: 'an external launcher runs one
    single-card predict per chip; each process then sees n_workers == 1,
    sizes its pools to all cores, and N co-resident folds oversubscribe the
    host N-fold.' We are that launcher."""
    one = worker_environ(worker_specs()[0], log_root="/logs", n_workers=1,
                         base={})
    four = worker_environ(worker_specs()[0], log_root="/logs", n_workers=4,
                          base={})
    assert int(four["OMP_NUM_THREADS"]) < int(one["OMP_NUM_THREADS"])
    assert int(four["OMP_NUM_THREADS"]) >= 1


def test_an_operator_set_variable_is_never_clobbered(fake_tt_bio):
    """Same guarantee runner_environ already makes, for the same reason."""
    env = worker_environ(worker_specs()[0], log_root="/logs", n_workers=4,
                         base={"TT_METAL_INSPECTOR": "1"})
    assert env["TT_METAL_INSPECTOR"] == "1"


def test_visibility_is_never_left_to_an_operator_to_get_wrong(fake_tt_bio):
    """The one exception to the rule above. An ambient TT_VISIBLE_DEVICES
    inherited from the parent's shell would silently pin every worker to
    the same chip -- and detect_tenstorrent_devices itself honours the
    ambient value, so a stale one narrows the whole booth to one card."""
    env = worker_environ(worker_specs()[3], log_root="/logs", n_workers=4,
                         base={"TT_VISIBLE_DEVICES": "0"})
    assert env["TT_VISIBLE_DEVICES"] == "3"


def test_control_lines_are_distinguishable_from_protocol_events():
    from protocol.events import EVENT_TYPES
    for kind in (CONTROL_READY, CONTROL_IDLE, CONTROL_FATAL):
        assert kind not in EVENT_TYPES
        assert is_control({"type": kind})
    assert not is_control({"type": "job_done", "job_id": "j1"})
    assert not is_control({})


def test_control_carries_its_fields():
    assert control(CONTROL_IDLE, job_id="j1") == {"type": CONTROL_IDLE,
                                                  "job_id": "j1"}


def test_the_event_fd_is_not_a_standard_stream():
    """tt-metal writes to fd 1 and fd 2 from C++. An event stream on either
    is a shredded event stream."""
    assert EVENT_FD not in (0, 1, 2)


def test_the_module_imports_without_tt_bio(monkeypatch):
    """The parent must not pay for ttnn just to import this module, and the
    134 existing runner tests must not either. tt-bio is imported lazily,
    inside the functions that need it."""
    for name in [m for m in sys.modules if m == "tt_bio" or m.startswith("tt_bio.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "tt_bio", None)   # importing it now raises
    monkeypatch.delitem(sys.modules, "runner.workers", raising=False)
    importlib.import_module("runner.workers")          # must not raise
```

**Mutations these must catch:** hardcoding `visible_devices` to `"0"` for every spec (test 2 red); dropping `mesh_graph_descriptor` from the spec (test 3 red); filtering `device_ids` locally instead of passing it to `detect_tenstorrent_devices` (test 4 red); letting a `ValueError` escape unwrapped (test 5 red); giving all workers one shared `TT_METAL_LOGS_PATH` (test 8 red); dropping `TT_METAL_INSPECTOR` (test 9 red); ignoring `n_workers` when capping host threads (test 10 red); using `setdefault` for `TT_VISIBLE_DEVICES` (test 12 red); moving the tt-bio imports to module scope (test 16 red).

Test 12 is the subtle one and it is deliberately the opposite rule from test 11. Every other variable is `setdefault`, because an operator who set it meant it. `TT_VISIBLE_DEVICES` is assignment, because an operator's stale value silently collapses the booth onto one chip *and* narrows `detect_tenstorrent_devices`' own enumeration.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

The three `_`-prefixed module-level names the fixture patches (`_detect_tenstorrent_devices`, `_build_local_workers`, `_worker_device_assignments`) are the seam: make them thin module-level functions that do the lazy tt-bio import and nothing else, so a test can replace them without a tt-bio install.

---

### Task 2: The worker process [no device]

**Files:** Create `runner/worker.py`. Test: `tests/unit/runner/test_worker_process.py`

**Why:** This is the piece that actually folds. It holds one `Folder` — unchanged from today, finally with a `device_id` that means something — and turns a command stream into the same protocol events the daemon emits today.

**Produces:**

- `WorkerSession(folder, emit, control_emit)` with `.run(command_lines)` — pure enough to test with a fake `Folder` and a list of lines.
- `main(argv=None)` — `python3 -m runner.worker --card N --event-fd 3`.

**Contract, which the tests pin:**

- Commands arrive on **stdin**, one JSON object per line: `{"cmd": "fold", "job_id":…, "target_id":…, "input_path":…, "n_residues":…}` and `{"cmd": "stop"}`.
- Events leave on **`EVENT_FD`**, one JSON object per line, flushed per line. Protocol events are emitted **exactly as `Folder.fold` produces them** — no rewriting, no added fields. `card` is already carried by `job_start` because the daemon passes `card=` into `fold()`; the worker passes its own card.
- The worker emits `CONTROL_READY` once, after `Folder.load()` succeeds, and `CONTROL_IDLE` after every job, **whether it succeeded or failed**.
- A `FoldError` becomes a `job_error` event emitted by the worker itself, then `CONTROL_IDLE`. The parent does not synthesize it.
- Anything the worker cannot recover from becomes `CONTROL_FATAL` followed by exit. It never exits silently.

- [ ] **Step 1: Write the failing tests**

```python
import json

import pytest

from runner.folder import FoldError
from runner.workers import CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY
from runner.worker import WorkerSession


class _FakeFolder:
    """A Folder that emits a plausible event sequence without a device."""

    def __init__(self, outcomes=None, on_load=None):
        self.outcomes = list(outcomes or [])
        self.on_load = on_load
        self.loaded = 0
        self.closed = 0
        self.folds = []

    def load(self):
        self.loaded += 1
        if self.on_load is not None:
            self.on_load()

    def close(self):
        self.closed += 1

    def fold(self, job_id, input_path, emit, *, target_id, n_residues, card=0,
             n_step=200):
        self.folds.append((job_id, target_id, card))
        emit({"type": "job_start", "job_id": job_id, "target_id": target_id,
              "model": "protenix-v2", "card": card, "n_residues": n_residues})
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        # BaseException, not Exception. KeyboardInterrupt and SystemExit do NOT
        # subclass Exception, so an `isinstance(outcome, Exception)` check here
        # silently declines to raise them -- and the test that asks "is the
        # device released when a worker is killed mid-fold?" then falls through
        # to a normal job_done and passes against every possible
        # implementation. That is not a hypothetical: it shipped in this plan
        # and Task 2's implementer found it with `DID NOT RAISE
        # KeyboardInterrupt`, having had zero coverage of the release path.
        if isinstance(outcome, BaseException):
            raise outcome
        emit({"type": "job_done", "job_id": job_id, "cif_path": f"/tmp/{job_id}.cif",
              "wall_s": 4.4, "mean_plddt": 95.3})


def _session(folder, card=2):
    events, controls = [], []
    return (WorkerSession(folder, events.append, controls.append, card=card),
            events, controls)


def _fold(job_id, target_id="trpcage"):
    return json.dumps({"cmd": "fold", "job_id": job_id, "target_id": target_id,
                       "input_path": f"/p/{target_id}.yaml", "n_residues": 20})


def test_the_model_loads_once_and_stays_resident():
    folder = _FakeFolder()
    session, _e, _c = _session(folder)
    session.run([_fold("j1"), _fold("j2"), json.dumps({"cmd": "stop"})])
    assert folder.loaded == 1
    assert len(folder.folds) == 2


def test_ready_is_announced_only_after_load_succeeds():
    order = []
    folder = _FakeFolder(on_load=lambda: order.append("load"))
    session, _e, controls = _session(folder)
    session.control_emit = lambda ev: order.append(ev["type"])
    session.run([json.dumps({"cmd": "stop"})])
    assert order[:2] == ["load", CONTROL_READY]


def test_every_job_folds_on_this_workers_own_card():
    """The whole point of one process per chip."""
    folder = _FakeFolder()
    session, events, _c = _session(folder, card=3)
    session.run([_fold("j1"), json.dumps({"cmd": "stop"})])
    start = [e for e in events if e["type"] == "job_start"][0]
    assert start["card"] == 3
    assert folder.folds[0][2] == 3


def test_protocol_events_are_forwarded_unchanged():
    """The EVENT vocabulary does not change -- Task 3 moves the version and
    adds a client->server message, and neither touches what a worker emits.
    A worker that decorates its events is a worker whose events the UI has
    to learn about."""
    folder = _FakeFolder()
    session, events, _c = _session(folder)
    session.run([_fold("j1"), json.dumps({"cmd": "stop"})])
    done = [e for e in events if e["type"] == "job_done"][0]
    assert set(done) == {"type", "job_id", "cif_path", "wall_s", "mean_plddt"}


def test_idle_follows_every_job():
    folder = _FakeFolder()
    session, _e, controls = _session(folder)
    session.run([_fold("j1"), _fold("j2"), json.dumps({"cmd": "stop"})])
    assert [c["type"] for c in controls].count(CONTROL_IDLE) == 2


def test_a_failed_fold_becomes_a_job_error_and_still_frees_the_worker():
    folder = _FakeFolder(outcomes=[FoldError("boom")])
    session, events, controls = _session(folder)
    session.run([_fold("j1"), _fold("j2"), json.dumps({"cmd": "stop"})])
    errors = [e for e in events if e["type"] == "job_error"]
    assert len(errors) == 1 and errors[0]["job_id"] == "j1"
    assert [c["type"] for c in controls].count(CONTROL_IDLE) == 2
    assert len(folder.folds) == 2, "a failed fold must not end the worker"


def test_a_non_FoldError_exception_is_also_reported_and_survived():
    """Folder.fold documents FoldError, but the booth must not bet on every
    collaborator keeping its promise -- runner/daemon.py already has this
    backstop and it must not be lost in the move."""
    folder = _FakeFolder(outcomes=[RuntimeError("contract violated")])
    session, events, controls = _session(folder)
    session.run([_fold("j1"), _fold("j2"), json.dumps({"cmd": "stop"})])
    assert [e["type"] for e in events].count("job_error") == 1
    assert len(folder.folds) == 2


def test_a_job_error_never_carries_the_raw_message_to_the_screen_unfiltered():
    """The UI's contract is that `message` is for the log only. The worker
    still has to SEND it, so this pins that it is present and is a string --
    the constraint lives on the UI side, and a missing field would make the
    daemon's log useless instead."""
    folder = _FakeFolder(outcomes=[FoldError("/secret/path exploded")])
    session, events, _c = _session(folder)
    session.run([_fold("j1"), json.dumps({"cmd": "stop"})])
    error = [e for e in events if e["type"] == "job_error"][0]
    assert isinstance(error["message"], str) and error["message"]


def test_a_load_failure_is_fatal_and_says_so_before_exiting():
    folder = _FakeFolder(on_load=lambda: (_ for _ in ()).throw(
        RuntimeError("device already leased")))
    session, _e, controls = _session(folder)
    with pytest.raises(SystemExit):
        session.run([_fold("j1")])
    assert controls[-1]["type"] == CONTROL_FATAL
    assert CONTROL_READY not in [c["type"] for c in controls]


def test_the_device_is_released_on_a_clean_stop():
    folder = _FakeFolder()
    session, _e, _c = _session(folder)
    session.run([json.dumps({"cmd": "stop"})])
    assert folder.closed == 1


def test_the_device_is_released_even_when_a_fold_was_in_flight():
    """'Never leave a process holding a device' is a global constraint, and
    a worker killed mid-fold is the ordinary case at booth shutdown."""
    folder = _FakeFolder(outcomes=[KeyboardInterrupt()])
    session, _e, _c = _session(folder)
    with pytest.raises(KeyboardInterrupt):
        session.run([_fold("j1")])
    assert folder.closed == 1


def test_a_malformed_command_line_is_survived():
    folder = _FakeFolder()
    session, _e, _c = _session(folder)
    session.run(["not json{", json.dumps({"cmd": "nonsense"}), _fold("j1"),
                 json.dumps({"cmd": "stop"})])
    assert len(folder.folds) == 1


def test_end_of_stdin_ends_the_worker_cleanly():
    """The parent dying closes our stdin. An orphaned worker holding a chip
    open indefinitely is a documented tt-bio failure mode (a stray worker
    pinned /dev/tenstorrent/3 for two hours)."""
    folder = _FakeFolder()
    session, _e, _c = _session(folder)
    session.run([])                       # EOF immediately
    assert folder.closed == 1
```

**Mutations these must catch:** calling `load()` per job (test 1 red); emitting `CONTROL_READY` before `load()` (test 2 red); hardcoding `card=0` (test 3 red); adding a field to a forwarded event (test 4 red); skipping `CONTROL_IDLE` on the error path (tests 6, 7 red); letting a `FoldError` end the run loop (test 6 red); narrowing the backstop to `except FoldError` (test 7 red); swallowing a load failure and continuing (test 9 red); dropping `close()` from the `finally` (tests 11, 13 red).

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

`main()` is thin: parse `--card`/`--event-fd`, build a `Folder(device_id=card)`, wrap `os.fdopen(event_fd, "w")`, and drive `WorkerSession` from `sys.stdin`. Do **not** call anything like tt-bio's `_silence_subprocess_output` — the parent (Task 6) owns where fd 1 and 2 go, and a worker that redirects them to `/dev/null` throws away the only diagnostic an operator has when a chip fails to come up.

---

### Task 3: The wire moves exactly this far — one client→server message [no device]

**Files:** Modify `protocol/events.py`. Regenerate `tests/fixtures/streams/short_fold.jsonl`, edit `tests/fixtures/streams/real_fold_trpcage.jsonl`. Tests: `tests/unit/test_protocol_client_messages.py`, `tests/unit/test_protocol_is_frozen.py`, `tests/unit/runner/test_protocol_is_frozen_runner.py`

**Why:** The socket has been one-way since Phase 3a — `runner/server.py` broadcasts and never reads, `ui/client.py` reads and never writes — and that is the only reason a visitor's pick cannot cause a fold. The decision has been taken that it should: **a pick starts folding.** That needs a message travelling the other way, which changes the contract, which is a version bump. This task is the whole protocol change and nothing else, because every other task in the pair of directions (Tasks 4, 5, 9, 17) is easier to review against a contract that is already fixed and already tested.

**Produces:**

- `PROTOCOL_VERSION = 2`.
- `EVENT_TYPES` — **unchanged**, still exactly the eight server→client types. Multi-chip adds no event.
- `CLIENT_MESSAGE_TYPES = frozenset({"pick"})` — the client→server vocabulary, a **separate** frozenset.
- `MAX_TARGET_ID_LEN = 64`.
- `pick_message(target_id) -> dict` — `{"type": "pick", "version": PROTOCOL_VERSION, "target_id": target_id}`.
- `encode_client_message(message) -> bytes` and `decode_client_message(line) -> dict`, mirroring `encode`/`decode` exactly: one newline-terminated JSON line, `ProtocolError` on anything else.
- Still stdlib + numpy only. Both venvs import this module and that does not change.

**Why two vocabularies rather than one bigger `EVENT_TYPES`:** `EventServer.broadcast` encodes with `encode`, and must stay structurally unable to put a `pick` on the wire to a UI. `EventClient` decodes with `decode`, and must stay structurally unable to accept a `pick` as an event. A single shared set makes both of those possible, and the two directions quietly become one channel where anything may travel either way. Two sets means a direction error is a `ProtocolError` at the boundary instead of a mystery three modules later.

**Version ruling, both directions — because `ui/client.py` already has an "incompatible" path and it is easy to assume it does something it does not.**

What that path does **today**, checked rather than remembered: `EventClient._session` compares `hello`'s `version` against its own `PROTOCOL_VERSION`; on a mismatch it logs at error level, calls `_set_state("incompatible")` and returns. `_run` then checks for exactly that state **before** touching it again and returns too — so the reader thread exits for the life of the process and never retries, deliberately (its comment says so: an unconditional `_set_state("disconnected")` there would clobber the guard and spam reconnects). `ui/app.py`'s `_on_state` hands the state to `StructureViewer.connection_state`, whose validator already accepts `"incompatible"` as one of its three legal values, so nothing raises and no raw text reaches the glass.

**That refusal stays, in both directions.** It is the right answer, not the lazy one:

- **A v2 UI against a v1 daemon.** The UI would send `pick` lines that a v1 daemon never reads — they would sit in its receive buffer until it fills, and nothing would ever fold on demand. The booth would silently promise a capability it does not have, which is precisely the failure the whole-branch review called Critical 2. A refusal is louder and more honest.
- **A v1 UI against a v2 daemon.** Already-installed v1 code cannot be changed by anything written here, and it refuses on its own. Nothing in this plan should try to work around that: both halves ship in one Debian package (Phase 4, this branch), so a version mismatch means a half-finished upgrade — a thing to notice, not to paper over.

What that costs, stated plainly so nobody discovers it at a venue: an incompatible booth is a booth whose screen never gets events. So the rule has a second half, which **Task 5 pins**: on `incompatible` the UI shows the same neutral overlay it already shows for `not_ready`, the version numbers go to the log and the diagnostics rail and never to the screen, and the client sends nothing, ever, to a daemon it has refused to interpret.

**The fixtures move with the version.** Two committed fixtures carry `"version":1` in their `hello` line and are replayed to a real `EventClient` by the UI half's tests; bumping the constant without them makes those tests fail as "incompatible" for a reason nobody will remember an hour later. `short_fold.jsonl` is regenerable — `make_short_fold.py` already interpolates `PROTOCOL_VERSION` — so regenerate it. `real_fold_trpcage.jsonl` is a hardware capture that cannot be regenerated without silicon: edit its single `hello` line in place, and leave every other line byte-identical (`capture_real_fold.py` already interpolates the constant, so future captures need nothing). The ratchet test below is what keeps this from happening again on the next bump.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_protocol_client_messages.py`:

```python
import ast
import pathlib
import sys

import pytest

from protocol.events import (
    CLIENT_MESSAGE_TYPES, EVENT_TYPES, MAX_TARGET_ID_LEN, PROTOCOL_VERSION,
    ProtocolError, decode, decode_client_message, encode,
    encode_client_message, pick_message,
)


def test_the_version_is_two_because_the_contract_changed():
    """Not decoration: ui/client.py refuses to interpret a daemon whose
    version differs from its own, so this number is the only thing standing
    between a v2 UI and a v1 daemon that will never answer its picks."""
    assert PROTOCOL_VERSION == 2


def test_a_pick_is_not_an_event():
    """The two directions are separate vocabularies. If a pick is also an
    event, EventServer.broadcast can send one to a UI and ui/client.py will
    hand one to _handle_event as if the daemon had said it."""
    assert "pick" not in EVENT_TYPES
    with pytest.raises(ProtocolError):
        encode(pick_message("trpcage"))
    with pytest.raises(ProtocolError):
        decode(b'{"type":"pick","version":2,"target_id":"trpcage"}\n')


def test_an_event_is_not_a_client_message():
    """The mirror image, and the one that matters for the daemon: a client
    that sends `job_done` must not be able to inject a fold result."""
    assert not (EVENT_TYPES & CLIENT_MESSAGE_TYPES)
    with pytest.raises(ProtocolError):
        encode_client_message({"type": "job_done", "job_id": "j1"})
    with pytest.raises(ProtocolError):
        decode_client_message(
            b'{"type":"job_done","job_id":"j1","cif_path":"/a.cif"}\n')


def test_the_client_vocabulary_is_exactly_one_message():
    """A general RPC channel is not what this phase is for."""
    assert CLIENT_MESSAGE_TYPES == frozenset({"pick"})


def test_a_pick_carries_the_version_it_was_written_against():
    message = pick_message("trpcage")
    assert message["type"] == "pick"
    assert message["version"] == PROTOCOL_VERSION
    assert message["target_id"] == "trpcage"


def test_a_pick_round_trips():
    assert decode_client_message(
        encode_client_message(pick_message("trpcage"))) == pick_message("trpcage")


def test_an_encoded_pick_is_exactly_one_line():
    """The daemon frames on newlines. A target_id containing one must not be
    able to split a message into two."""
    line = encode_client_message(pick_message("trp\ncage"))
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1


def test_a_message_from_a_different_protocol_version_is_refused():
    with pytest.raises(ProtocolError):
        decode_client_message(
            b'{"type":"pick","version":1,"target_id":"trpcage"}\n')


def test_a_message_with_no_version_is_refused():
    """An unversioned message is one we cannot reason about at all."""
    with pytest.raises(ProtocolError):
        decode_client_message(b'{"type":"pick","target_id":"trpcage"}\n')


def test_malformed_json_is_a_ProtocolError_not_a_crash():
    for junk in (b"not json{\n", b"\n", b'{"type":\n', b"\xff\xfe\n"):
        with pytest.raises(ProtocolError):
            decode_client_message(junk)


def test_a_json_array_is_refused():
    with pytest.raises(ProtocolError):
        decode_client_message(b'["pick","trpcage"]\n')


def test_an_unknown_message_type_is_refused():
    with pytest.raises(ProtocolError):
        decode_client_message(
            b'{"type":"shutdown","version":2,"target_id":"x"}\n')


def test_an_absurd_target_id_is_refused():
    """The daemon reads this off a socket. A megabyte target_id is a
    megabyte the daemon should never have allocated, and the length limit
    is the only thing that says so."""
    huge = "a" * (MAX_TARGET_ID_LEN + 1)
    with pytest.raises(ProtocolError):
        decode_client_message(encode_client_message(
            {"type": "pick", "version": PROTOCOL_VERSION, "target_id": huge}))


def test_a_target_id_at_the_limit_is_accepted():
    """A limit that is off by one is a limit that rejects real targets."""
    ok = "a" * MAX_TARGET_ID_LEN
    assert decode_client_message(encode_client_message(
        {"type": "pick", "version": PROTOCOL_VERSION,
         "target_id": ok}))["target_id"] == ok


def test_a_non_string_target_id_is_refused():
    for bad in (17, None, ["trpcage"], {"a": 1}):
        with pytest.raises(ProtocolError):
            decode_client_message(
                encode_client_message({"type": "pick",
                                       "version": PROTOCOL_VERSION,
                                       "target_id": bad}))


def test_an_empty_target_id_is_refused():
    with pytest.raises(ProtocolError):
        decode_client_message(
            b'{"type":"pick","version":2,"target_id":""}\n')


def test_this_module_still_imports_nothing_but_stdlib_and_numpy():
    """The rule that makes protocol/ importable from BOTH venvs, enforced
    against the file rather than against anyone's memory of it. The client
    direction is exactly the kind of addition that reaches for a validation
    library."""
    source = pathlib.Path("protocol/events.py").read_text()
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names) | {"numpy"}, sorted(roots)
```

`tests/unit/test_protocol_is_frozen.py` and `tests/unit/runner/test_protocol_is_frozen_runner.py` — the same body in both, one per venv, because both halves make the claim and each must hold it in the interpreter that actually runs it:

```python
from protocol.events import CLIENT_MESSAGE_TYPES, EVENT_TYPES, PROTOCOL_VERSION


def test_the_protocol_version_is_two_on_this_side_too():
    """Both halves must agree on this number or the UI refuses the daemon at
    `hello`. A bump made in one venv's checkout and not the other is exactly
    the failure this pair of files exists to catch."""
    assert PROTOCOL_VERSION == 2


def test_the_event_vocabulary_is_unchanged_by_multi_chip():
    """Multi-chip is a scheduling change. If this fails, something leaked
    onto the wire that should not have -- a worker control line is the
    likely candidate."""
    assert EVENT_TYPES == frozenset(
        {"hello", "not_ready", "job_start", "stage", "frame",
         "job_done", "job_error", "card_state"})


def test_the_client_vocabulary_is_exactly_one_message():
    """The pick, and nothing else. A second client->server message is a
    third version, a decision, and a task of its own."""
    assert CLIENT_MESSAGE_TYPES == frozenset({"pick"})


def test_the_two_directions_never_overlap():
    assert not (EVENT_TYPES & CLIENT_MESSAGE_TYPES)
```

And, in the UI-half copy only (`tests/unit/test_protocol_is_frozen.py`), the ratchet that keeps the recorded streams in step with the constant:

```python
import json
import pathlib


def test_no_committed_fixture_advertises_a_stale_protocol_version():
    """A fixture whose `hello` says v1 makes ui/client.py declare the stream
    incompatible and stop reading -- the failure looks like "the UI tests
    hang and see no events", which is a long way from "somebody bumped a
    constant". Every .jsonl under tests/fixtures/streams/ is replayed to a
    real EventClient by something, so every one of them has to move."""
    from protocol.events import PROTOCOL_VERSION
    for path in sorted(pathlib.Path("tests/fixtures/streams").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event.get("type") == "hello":
                assert event["version"] == PROTOCOL_VERSION, path
```

And, in the runner-half copy only, the check the UI cannot make because it may not import `runner.*`:

```python
def test_no_worker_control_line_is_a_protocol_message_in_either_direction():
    """A control line that is also a wire type is a control line the pool
    will forward to the socket."""
    from runner.workers import CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY
    from protocol.events import CLIENT_MESSAGE_TYPES, EVENT_TYPES
    for kind in (CONTROL_READY, CONTROL_IDLE, CONTROL_FATAL):
        assert kind not in EVENT_TYPES
        assert kind not in CLIENT_MESSAGE_TYPES
```

**Mutations these must catch:** leaving `PROTOCOL_VERSION` at `1` (version tests red); adding `"pick"` to `EVENT_TYPES` instead of its own set (tests 2, 3 red, plus the frozen pair); making `CLIENT_MESSAGE_TYPES` an alias of `EVENT_TYPES` (test 3 red); accepting any `version`, or none (tests 8, 9 red); dropping the `target_id` type check (test 15 red); dropping the length limit, or writing it as `>=` (tests 13, 14 red); letting `decode_client_message` accept a list (test 11 red); importing a third-party validator (test 17 red); bumping the version without regenerating the fixtures (the fixture ratchet red).

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

`decode_client_message` is deliberately strict where `decode` is not: `decode` accepts any well-formed event of a known type because the daemon is trusted, and this one is reading from a process the daemon does not control. Validate `type`, `version`, and that `target_id` is a non-empty `str` of at most `MAX_TARGET_ID_LEN` characters. It validates **nothing about what the target means** — whether that id names a real playlist entry is the daemon's question, and Task 9 answers it against the playlist rather than against a string.

Regenerate `short_fold.jsonl` with `make_short_fold.py` under venv-ui, edit `real_fold_trpcage.jsonl`'s `hello` line in place, and run the UI half **before** commit: any test that replays a fixture through `EventClient` is the one that tells you whether the fixture fix is complete.

---

### Task 4: The daemon can be spoken to [no device]

**Files:** Modify `runner/server.py`. Test: `tests/unit/runner/test_server_receive.py`

**Why:** `EventServer` has only ever written. Giving it a read direction is the point at which the booth daemon starts accepting bytes from another process, and **a booth daemon that a bad line can kill is worse than one that cannot be picked from.** Everything in this task is about that sentence. The pick itself is Task 9; this task ends with a server that decodes client messages, hands them to a callback and survives everything else.

**Produces:**

- `EventServer(socket_path, hello_factory, client_send_timeout=1.0, on_client_message=None)`.
- `CLIENT_LINE_MAX_BYTES = 64 * 1024`.
- One reader thread per accepted client (daemon threads), started after the greeting has been sent and the client registered.
- `stop()` joins the reader threads with a bounded timeout and leaves nothing running.
- `on_client_message=None` is fully supported: lines are decoded and discarded. The server never *requires* a consumer, so a daemon that has not wired one up yet is not a daemon that breaks.

**The reader contract, which the tests pin:**

- A line that `decode_client_message` accepts is handed to `on_client_message(message)`. The callback runs **on the reader thread** and is wrapped: a raising callback costs that message and nothing else.
- A `ProtocolError` — malformed JSON, unknown type, wrong version, absurd `target_id` — is logged (rate-limited) and dropped. **The client stays connected.** A visitor's UI is not disconnected because one line was bad.
- EOF or `OSError` drops that client exactly the way a failed send already does: close it, remove it from `_clients`, log at info.
- More than `CLIENT_LINE_MAX_BYTES` of buffered bytes with no newline drops the client. An unbounded line buffer is a remote process choosing how much memory this daemon allocates.
- Partial lines are held across reads and completed later; two messages arriving in one write are both delivered.

**Two implementation constraints that are not obvious and that a reviewer should check for first:**

1. **Do not use `conn.makefile()` on the read side.** `_accept_loop` already calls `conn.settimeout(self._client_send_timeout)` — 1.0 s — to bound how long a wedged UI can block a send, and that timeout applies to **reads on the same socket**. A reader looping over a file object will therefore raise `socket.timeout` roughly once a second against a perfectly healthy, silent UI, and Python's socket file objects are documented as being left in an unusable state after a timeout, so any bytes already buffered for a half-received line are gone. Read with `conn.recv()` into an explicit `bytes` buffer, split on `\n` yourself, and treat `socket.timeout` as "no bytes this pass, go round again". A reader that treats it as death disconnects every idle UI after one second — which is every UI, almost all of the time.
2. **`broadcast` must hold its lock across the whole `sendall` loop** (or take a per-connection send lock). Today it copies the client list under `_lock` and then sends *outside* it, which was safe when the single fold loop was the only caller. As of Task 6 it is called from **four** worker reader threads, and two concurrent `sendall`s on one client socket can interleave partial writes and split a JSON line in half — a stream corruption the single-fold daemon could never produce and four workers produce routinely. This is a real defect introduced by the multi-chip change, it is cheap to close here while this file is open, and Task 6 depends on it. Do not reorder the two tasks.

- [ ] **Step 1: Write the failing tests**

```python
import json
import socket
import threading
import time

import pytest

from protocol.events import (
    PROTOCOL_VERSION, decode, encode_client_message, pick_message,
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
    conn, _stream = _connect(server)
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
    finally:
        s.stop()


def test_concurrent_broadcasts_never_split_a_line(server):
    """Four worker reader threads call broadcast at once from Task 6 on.
    sendall is not atomic: two of them writing to one client socket outside
    the lock interleave partial writes, and the UI sees half a job_done
    glued to half a frame. Every line the client reads must decode."""
    conn, stream = _connect(server)
    threads = [threading.Thread(target=lambda n=n: [
        server.broadcast(_job_done(f"j{n}-{i}")) for i in range(50)])
        for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    for _ in range(200):
        decode(stream.readline())          # raises ProtocolError on a split line


def test_a_client_that_speaks_while_being_broadcast_to_is_not_disturbed(server):
    conn, stream = _connect(server)
    for i in range(20):
        conn.sendall(encode_client_message(pick_message(f"t{i}")))
        server.broadcast(_job_done(f"j{i}"))
    assert _wait(lambda: len(server.received) == 20)
    for _ in range(20):
        assert decode(stream.readline())["type"] == "job_done"


def test_stop_leaves_no_reader_thread_running(server):
    _conn, _stream = _connect(server)
    before = {t.name for t in threading.enumerate()}
    server.stop()
    assert _wait(lambda: not [t for t in threading.enumerate()
                              if t.name not in before and t.is_alive()])
```

**Mutations these must catch:** treating `socket.timeout` on the read as a disconnect (test 2 red); dropping the client on a `ProtocolError` (test 3 red); acting on an unknown type, or on a mismatched version (tests 4, 5 red); discarding the partial buffer between reads — which is what `makefile()` does after a timeout (test 6 red); splitting on the first newline only and dropping the remainder (test 7 red); an unbounded line buffer (test 8 red); letting a reader-thread exception escape (test 10 red); requiring `on_client_message` to be non-None (test 11 red); leaving the `sendall` loop outside the lock (test 12 red); never joining the reader threads (test 14 red).

**Test 12 is the one that cannot be argued from a code reading**, because the interleaving depends on how the kernel schedules two partial writes. Verify it by moving the `sendall` loop back outside the lock and running it a few times; if it stays green every time, add clients or events until it fails, then keep the version that fails. A test for a race that has never been observed to fail is not a test.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

Rate-limit the malformed-line log the way the pool rate-limits its own junk-line log (Task 6): a client stuck in a loop sending garbage must not be able to fill the log root the janitor is trying to bound.

---

### Task 5: The UI can speak [no device]

**Files:** Modify `ui/client.py`. Test: `tests/unit/test_client_send.py`; extend `tests/unit/test_mock_runner.py`

**Why:** The other half of the socket. This task ends with an `EventClient` that can put a `pick` on the wire from a GTK callback without ever blocking the main loop, without ever raising into it, and without ever sending anything to a daemon it has refused to interpret.

**Produces:**

- `OUTBOX_MAX = 8`.
- `EventClient(..., outbox_max=OUTBOX_MAX)`, with a **sender thread** started by `start()` and joined by `stop()`, and `self._conn` published under `self._conn_lock` by `_session` and cleared when it exits.
- `.send(message) -> bool` — validates with `encode_client_message`, enqueues, returns whether it was enqueued. **Never raises and never blocks**: it is called from a GLib callback, where an exception freezes a source forever and a blocking `sendall` freezes the whole booth.
- `.send_pick(target_id) -> bool`.
- `.dropped_sends` — a counter, public, because a test has to be able to assert the outbox is bounded without reaching into a private field.

**Why a thread and a queue rather than just calling `sendall` in `send()`:** a `sendall` from the GTK main loop is a call that can block for as long as the peer's receive buffer stays full — the daemon is a process that could be stopped, wedged, or in the middle of four folds — and a blocked main loop is a frozen booth with a live-looking screen. A pick is ~60 bytes and would almost always go straight out; "almost always" is not a property to hang the main loop on. The queue is bounded and **drops the oldest** on overflow, and picks queued while disconnected are dropped rather than held: a pick means "fold this now", and a pick delivered ninety seconds later to a visitor who has walked away is worse than no pick at all.

**The version-mismatch behaviour, pinned here** (the ruling is in Task 3): once `state == "incompatible"`, `send` returns `False` and enqueues nothing, forever. The UI has declared it cannot interpret that daemon; talking to it anyway is the one thing worse than staying quiet.

- [ ] **Step 1: Write the failing tests**

```python
import json
import pathlib
import socket
import threading
import time

import pytest

from protocol.events import PROTOCOL_VERSION, decode_client_message, encode
from ui.client import OUTBOX_MAX, EventClient


def _hello(version=PROTOCOL_VERSION):
    return {"type": "hello", "version": version, "cards": [0, 1, 2, 3],
            "models": ["protenix-v2"], "preflight": "ok"}


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class _Listener:
    """A minimal daemon-shaped peer: greets, then records whole lines."""

    def __init__(self, path, hello=None):
        self.path = str(path)
        self.hello = hello or _hello()
        self.received = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3.0)
        self._server.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.sendall(encode(self.hello))
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn):
        conn.settimeout(0.2)
        with conn, conn.makefile("rb") as stream:
            while not self._stop.is_set():
                try:
                    line = stream.readline()
                except (socket.timeout, OSError):
                    continue
                if not line:
                    return
                self.received.append(decode_client_message(line))


@pytest.fixture
def listener(tmp_path):
    peer = _Listener(tmp_path / "sock")
    peer.start()
    try:
        yield peer
    finally:
        peer.stop()


def _client(path, **kw):
    return EventClient(str(path), on_event=lambda e: None, **kw)


def test_a_pick_reaches_a_listening_daemon(listener):
    client = _client(listener.path)
    client.start()
    try:
        assert _wait(lambda: client.state == "connected")
        assert client.send_pick("trpcage") is True
        assert _wait(lambda: [m["target_id"] for m in listener.received]
                     == ["trpcage"])
    finally:
        client.stop()


def test_a_pick_carries_the_protocol_version(listener):
    client = _client(listener.path)
    client.start()
    try:
        assert _wait(lambda: client.state == "connected")
        client.send_pick("trpcage")
        assert _wait(lambda: listener.received)
        assert listener.received[0]["version"] == PROTOCOL_VERSION
    finally:
        client.stop()


def test_send_before_there_has_ever_been_a_connection_never_raises(tmp_path):
    """_on_pick runs in a GLib callback. An exception there freezes that
    source for the life of the process, and the booth stops answering
    taps -- with nothing on screen to say why."""
    client = _client(tmp_path / "nothing-here.sock")
    client.start()
    try:
        assert client.send_pick("trpcage") in (True, False)   # must not raise
    finally:
        client.stop()


def test_send_does_not_block_the_caller(tmp_path):
    """There is no daemon at this path at all. The GTK main loop must come
    straight back regardless."""
    client = _client(tmp_path / "nothing-here.sock")
    client.start()
    try:
        started = time.monotonic()
        for _ in range(50):
            client.send_pick("trpcage")
        assert time.monotonic() - started < 0.5
    finally:
        client.stop()


def test_picks_made_while_disconnected_do_not_pile_up(tmp_path):
    """A booth left running with the daemon down for an hour must not
    deliver an hour of stale picks the moment it comes back."""
    client = _client(tmp_path / "nothing-here.sock")
    client.start()
    try:
        for n in range(1000):
            client.send_pick(f"t{n}")
        assert client.pending_sends <= OUTBOX_MAX
        assert client.dropped_sends > 0
    finally:
        client.stop()


def test_a_malformed_message_is_refused_without_raising(tmp_path):
    client = _client(tmp_path / "nothing-here.sock")
    client.start()
    try:
        assert client.send({"type": "job_done", "job_id": "j1"}) is False
        assert client.send({"nonsense": True}) is False
    finally:
        client.stop()


def test_nothing_is_ever_sent_to_a_daemon_we_refuse_to_interpret(tmp_path):
    """The whole point of the incompatible state. A v2 UI whispering picks
    at a v1 daemon that will never answer them is the booth promising a
    capability it does not have."""
    peer = _Listener(tmp_path / "sock", hello=_hello(version=PROTOCOL_VERSION + 1))
    peer.start()
    try:
        client = _client(peer.path)
        client.start()
        try:
            assert _wait(lambda: client.state == "incompatible")
            assert client.send_pick("trpcage") is False
            time.sleep(0.3)
            assert peer.received == []
        finally:
            client.stop()
    finally:
        peer.stop()


def test_the_read_direction_still_works_while_sending(tmp_path):
    """Full duplex, and the regression that matters: a sender thread that
    takes the same lock the reader holds turns every fold into a stall."""
    seen = []
    peer = _Listener(tmp_path / "sock")
    peer.start()
    try:
        client = EventClient(peer.path, on_event=seen.append)
        client.start()
        try:
            assert _wait(lambda: client.state == "connected")
            for n in range(20):
                client.send_pick(f"t{n}")
            assert _wait(lambda: len(peer.received) == 20)
            assert _wait(lambda: any(e["type"] == "hello" for e in seen))
        finally:
            client.stop()
    finally:
        peer.stop()


def test_a_send_failure_does_not_end_the_read_loop(tmp_path):
    """The daemon restarting mid-pick is ordinary. The client must come back
    connected, not sit in a state where it never reads again."""
    peer = _Listener(tmp_path / "sock")
    peer.start()
    client = _client(peer.path, reconnect_delay=0.05)
    client.start()
    try:
        assert _wait(lambda: client.state == "connected")
        peer.stop()
        for _ in range(10):
            client.send_pick("trpcage")
        peer2 = _Listener(tmp_path / "sock")
        peer2.start()
        try:
            assert _wait(lambda: client.state == "connected", timeout=10.0)
            assert client.send_pick("hemoglobin") is True
            assert _wait(lambda: any(m["target_id"] == "hemoglobin"
                                     for m in peer2.received))
        finally:
            peer2.stop()
    finally:
        client.stop()


def test_stop_leaves_no_sender_thread_running(listener):
    client = _client(listener.path)
    before = {t.name for t in threading.enumerate()}
    client.start()
    assert _wait(lambda: client.state == "connected")
    client.stop()
    assert _wait(lambda: not [t for t in threading.enumerate()
                              if t.name not in before and t.is_alive()])


def test_stop_is_safe_when_start_was_never_called(tmp_path):
    _client(tmp_path / "sock").stop()          # must not raise
```

And in `tests/unit/test_mock_runner.py`:

```python
def test_a_pick_sent_to_the_mock_runner_does_not_disturb_the_replay(tmp_path):
    """runner/mock.py is the project's core test instrument and it will
    never read a byte from a client. A UI that now sends picks must not be
    able to wedge it -- otherwise every UI test that replays a fixture is
    one `_on_pick` away from hanging."""
    from protocol.events import encode_client_message, pick_message
    ...  # start a MockRunner on short_fold.jsonl, connect a real EventClient,
         # call client.send_pick("trpcage") repeatedly while the stream
         # replays, and assert every event in the fixture still arrives
```

**Mutations these must catch:** calling `sendall` directly from `send()` (test 4 red — it blocks against a dead path); an unbounded outbox (test 5 red); letting `encode_client_message`'s `ProtocolError` escape (test 6 red); sending while `incompatible` (test 7 red); holding one lock across both directions (test 8 red); a sender thread that exits on the first write failure (test 9 red); never joining the sender thread (test 10 red); `stop()` assuming `start()` ran (test 11 red).

Test 5 needs `pending_sends` and `dropped_sends` to be public for the same reason `SlotRouter.tracked_jobs` is (Task 12): a boundedness claim asserted against a private field is the "adjacent to the behaviour" shape `docs/followups.md` names as this project's recurring test defect.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

The sender thread waits on the outbox with a timeout so `stop()` is prompt; it takes the current connection under `_conn_lock`, writes, and on any `OSError` drops the message and logs at debug — the reconnect loop in `_run` already owns recovery and must remain the only thing that does. Do not have the sender reconnect. Do not have `send()` touch the socket.

`ui/app.py` is **not** touched in this task: `_on_pick` is Task 17, and the neutral overlay for `incompatible` is Task 17's too, so the copy change and the behaviour change land in one commit.

---

### Task 6: The pool — spawn, dispatch, multiplex [no device]

**Files:** Create `runner/pool.py`. Test: `tests/unit/runner/test_worker_pool.py`

**Why:** The parent's whole job. One module owns every subprocess and every reader thread, so "who is holding a device" has one answer.

**Produces:** `WorkerPool(specs, on_event, *, log_root, spawn=None, clock=time.monotonic)` with:

- `.start()` / `.stop()`
- `.dispatch(job, card)` — send one fold command; raises `ValueError` if that card is not ready
- `.ready_cards()` — cards whose worker has announced `CONTROL_READY` and is not busy
- `.any_ready()` — is the booth able to fold at all
- `.busy_job(card)` — the `job_id` in flight on that card, or `None`
- `.cards` — every card the pool manages, retired ones included

`spawn` is the seam: a callable `(spec, env) -> WorkerHandle` so the tests drive a fake worker with no subprocess at all. The real one is `subprocess.Popen`.

Two more constructor keywords — `on_worker_lost=` and `restart_delay_s=` — arrive in **Task 7**, which owns death and respawn. Build the constructor so adding them there is a keyword, not a rewrite.

**The multiplexing contract:**

- A line from a worker whose `type` is in `EVENT_TYPES` is passed to `on_event(card, event)` **unchanged**.
- A line whose `type` starts with `CONTROL_PREFIX` is consumed by the pool and **never** reaches `on_event`.
- An undecodable line is dropped with a rate-limited log and does not kill the reader.

- [ ] **Step 1: Write the failing tests**

```python
import json
import threading

import pytest

from runner.queue import Job
from runner.workers import CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY, WorkerSpec
from runner.pool import WorkerPool


def _spec(card):
    return WorkerSpec(card=card, label=f"quietbox:tt{card}",
                      visible_devices=str(card), logical_device_id=0,
                      mesh_graph_descriptor="/mgd/p150.textproto")


class _FakeWorker:
    """A worker handle whose event stream the test writes by hand."""

    def __init__(self, spec, env):
        self.spec = spec
        self.env = env
        self.commands = []
        self.terminated = False
        self.killed = False
        self._lines = []
        self._cv = threading.Condition()
        self._eof = False

    # -- what the pool calls --
    def send(self, command):
        self.commands.append(command)

    def readline(self):
        with self._cv:
            while not self._lines and not self._eof:
                self._cv.wait(timeout=2.0)
            return self._lines.pop(0) if self._lines else ""

    def terminate(self):
        self.terminated = True
        self.die()

    def kill(self):
        self.killed = True
        self.die()

    @property
    def alive(self):
        return not self._eof

    # -- what the test calls --
    def emit(self, obj):
        with self._cv:
            self._lines.append(json.dumps(obj) + "\n")
            self._cv.notify_all()

    def emit_raw(self, text):
        with self._cv:
            self._lines.append(text)
            self._cv.notify_all()

    def die(self):
        with self._cv:
            self._eof = True
            self._cv.notify_all()


@pytest.fixture
def pool(tmp_path):
    made = {}

    def spawn(spec, env):
        worker = _FakeWorker(spec, env)
        made[spec.card] = worker
        return worker

    p = WorkerPool([_spec(c) for c in (0, 1, 2, 3)], on_event=lambda c, e: None,
                   log_root=str(tmp_path), spawn=spawn)
    p.workers = made          # test handle; the pool keeps its own bookkeeping
    yield p
    p.stop()


def _wait(predicate, timeout=3.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _job(job_id="j1", target_id="trpcage"):
    return Job(job_id=job_id, target_id=target_id,
               input_path=f"/p/{target_id}.yaml", n_residues=20)


def test_one_subprocess_per_chip(pool):
    pool.start()
    assert sorted(pool.workers) == [0, 1, 2, 3]


def test_each_subprocess_gets_its_own_visibility(pool):
    pool.start()
    assert {c: w.env["TT_VISIBLE_DEVICES"] for c, w in pool.workers.items()} == {
        0: "0", 1: "1", 2: "2", 3: "3"}


def test_no_card_is_dispatchable_before_its_worker_says_ready(pool):
    pool.start()
    assert pool.ready_cards() == []
    with pytest.raises(ValueError):
        pool.dispatch(_job(), card=0)


def test_a_ready_worker_becomes_dispatchable(pool):
    pool.start()
    pool.workers[1].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [1])


def test_dispatch_sends_the_job_to_that_cards_worker_alone(pool):
    pool.start()
    for card in (0, 1, 2, 3):
        pool.workers[card].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [0, 1, 2, 3])
    pool.dispatch(_job("j7"), card=2)
    assert [c["job_id"] for c in pool.workers[2].commands] == ["j7"]
    assert all(not pool.workers[c].commands for c in (0, 1, 3))


def test_a_busy_card_is_not_dispatchable_again(pool):
    pool.start()
    pool.workers[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [0])
    pool.dispatch(_job("j1"), card=0)
    assert pool.ready_cards() == []
    assert pool.busy_job(0) == "j1"


def test_idle_frees_the_card_for_the_next_job(pool):
    pool.start()
    pool.workers[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [0])
    pool.dispatch(_job("j1"), card=0)
    pool.workers[0].emit({"type": CONTROL_IDLE, "job_id": "j1"})
    assert _wait(lambda: pool.ready_cards() == [0])
    assert pool.busy_job(0) is None


def test_protocol_events_reach_the_callback_with_their_card(tmp_path):
    seen = []

    def spawn(spec, env):
        w = _FakeWorker(spec, env)
        made[spec.card] = w
        return w

    made = {}
    p = WorkerPool([_spec(0), _spec(1)], on_event=lambda c, e: seen.append((c, e)),
                   log_root=str(tmp_path), spawn=spawn)
    p.start()
    try:
        made[1].emit({"type": "job_start", "job_id": "j1", "target_id": "t",
                      "model": "protenix-v2", "card": 1, "n_residues": 20})
        assert _wait(lambda: len(seen) == 1)
        assert seen[0][0] == 1
        assert seen[0][1]["type"] == "job_start"
    finally:
        p.stop()


def test_a_protocol_event_is_forwarded_byte_for_byte(tmp_path):
    """The multiplexer tags nothing and rewrites nothing -- the spec's
    'forward to the UI unchanged'."""
    seen, made = [], {}

    def spawn(spec, env):
        made[spec.card] = _FakeWorker(spec, env)
        return made[spec.card]

    original = {"type": "frame", "job_id": "j1", "step": 5, "total": 200,
                "n_atoms": 20, "coords_b64": "AAAA"}
    p = WorkerPool([_spec(0)], on_event=lambda c, e: seen.append(e),
                   log_root=str(tmp_path), spawn=spawn)
    p.start()
    try:
        made[0].emit(original)
        assert _wait(lambda: seen)
        assert seen[0] == original
    finally:
        p.stop()


def test_no_control_line_ever_reaches_the_event_callback(tmp_path):
    """If one does, EventServer.encode raises ProtocolError, the event is
    dropped, and the only symptom is a UI that never hears about something."""
    seen, made = [], {}

    def spawn(spec, env):
        made[spec.card] = _FakeWorker(spec, env)
        return made[spec.card]

    p = WorkerPool([_spec(0)], on_event=lambda c, e: seen.append(e),
                   log_root=str(tmp_path), spawn=spawn)
    p.start()
    try:
        made[0].emit({"type": CONTROL_READY})
        made[0].emit({"type": CONTROL_IDLE, "job_id": "j1"})
        made[0].emit({"type": "job_done", "job_id": "j1", "cif_path": "/a.cif",
                      "wall_s": 4.4, "mean_plddt": 95.3})
        assert _wait(lambda: seen)
        assert [e["type"] for e in seen] == ["job_done"]
    finally:
        p.stop()


def test_a_junk_line_does_not_kill_the_reader(pool):
    """tt-metal is loud. If any of it ever reaches this fd, the stream must
    survive it -- a dead reader is a chip that silently stops reporting."""
    pool.start()
    pool.workers[0].emit_raw("Metal | INFO | opening device\n")
    pool.workers[0].emit_raw("{truncated\n")
    pool.workers[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [0])


def test_stop_asks_politely_before_killing(pool):
    pool.start()
    pool.workers[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [0])
    pool.stop()
    assert pool.workers[0].terminated
    assert not pool.workers[0].killed


def test_stop_leaves_no_worker_alive(pool):
    """'Never leave a process holding a device.'"""
    pool.start()
    pool.stop()
    assert all(not w.alive for w in pool.workers.values())
```

**Mutations these must catch:** giving every worker the same env (test 2 red); treating a spawned-but-not-ready worker as dispatchable (test 3 red); broadcasting a dispatch to all workers (test 5 red); not marking the card busy on dispatch (test 6 red); not clearing busy on `CONTROL_IDLE` (test 7 red); passing the card as a rewritten field on the event (test 9 red); forwarding control lines to `on_event` (test 10 red); letting a `json.JSONDecodeError` escape the reader loop (test 11 red); killing instead of terminating (test 12 red).

Test 10 is the one that protects the spec's central claim. Verify it by deleting the `is_control` check and watching it go red — not by reading the code.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

One reader thread per worker, blocking on `readline`. `on_event` is called **from that thread**, which is what keeps a slow chip from blocking the other three. `EventServer.broadcast` never raises into its caller and, **as of Task 4**, holds its lock across the whole `sendall` loop — which is what makes four reader threads broadcasting at once safe. Before Task 4 it copied the client list under `_lock` and then sent *outside* it: two concurrent `sendall`s on one client socket interleave partial writes and split a JSON line in half, a corruption a single-fold daemon could never produce and four workers produce routinely. Do not reorder those two tasks, and say all of this in a comment.

**Name constraint:** `WorkerPool` keeps its own worker handles, spawn record and loss record under **private** names (`_workers`, and whatever else it needs), matching this codebase's existing convention (`EventServer._clients`, `JobQueue._items`, `CardPool._busy`). The fixtures above attach *test* handles at `pool.workers` / `pool.spawns` / `pool.lost`, and a public attribute of the same name would be silently clobbered by the fixture — a test that then passes against production state it has overwritten.

Also extract `_FakeWorker`, `_spec`, `_job` and `_wait` into `tests/unit/runner/_workerfakes.py` as part of this task (Task 7 imports them). The leading underscore is what keeps pytest from collecting it, the same convention `tests/unit/_legibility.py` already uses.

---

### Task 7: A worker that dies must not take the booth down [no device]

**Files:** Modify `runner/pool.py`. Test: `tests/unit/runner/test_worker_death.py`

**Why:** The spec names this the highest risk in the change, and it is: "the current single-process design fails closed, four workers must fail **partially**." Every other task in this plan is about making four chips work. This one is about the other 5% of the time.

**Produces:** on a worker's event stream reaching EOF (the process died, however it died), the pool must, in this order:

1. Notice within one reader-thread wakeup — no polling timer, no timeout.
2. If a job was in flight, report it: call `on_worker_lost(card, job_id, target_id)` — the pool has the whole `Job` from `dispatch`, so it can name the target without the daemon looking it up. **The pool never fabricates a protocol event**; the daemon (Task 8) decides what the wire sees. This keeps "who talks to the socket" in one module.
3. Mark the card not-ready and not-busy.
4. Respawn after `WORKER_RESTART_DELAY_S`, unless the card has died `WORKER_RETIRE_AFTER` times consecutively with no completed job in between, in which case retire it for the session with a loud log.
5. Never touch the other three workers.

- [ ] **Step 1: Write the failing tests**

Reuse `_FakeWorker`, `_spec`, `_wait` and `_job` from Task 6 — move them into `tests/unit/runner/_workerfakes.py` as part of this task and import them from both files. (`_`-prefixed, so pytest never collects it, the same convention `tests/unit/_legibility.py` already uses.)

```python
import pytest

from runner.pool import WORKER_RETIRE_AFTER, WorkerPool
from runner.workers import CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY

from _workerfakes import _FakeWorker, _job, _spec, _wait


@pytest.fixture
def pool(tmp_path):
    """A pool whose respawn delay is ~0 and whose losses are recorded."""
    made, spawns, lost = {}, [], []

    def spawn(spec, env):
        w = _FakeWorker(spec, env)
        made[spec.card] = w
        spawns.append(spec.card)
        return w

    p = WorkerPool([_spec(c) for c in (0, 1, 2, 3)], on_event=lambda c, e: None,
                   on_worker_lost=lambda c, j, t: lost.append((c, j)),
                   log_root=str(tmp_path), spawn=spawn, restart_delay_s=0.01)
    p.workers, p.spawns, p.lost = made, spawns, lost
    yield p
    p.stop()


def _ready(pool, *cards):
    for card in cards:
        pool.workers[card].emit({"type": CONTROL_READY})
    assert _wait(lambda: set(pool.ready_cards()) >= set(cards))


def test_a_dead_worker_does_not_stop_the_other_three(pool):
    """The headline requirement. Three chips keep folding."""
    pool.start()
    _ready(pool, 0, 1, 2, 3)
    pool.dispatch(_job("j0"), card=0)
    pool.dispatch(_job("j1"), card=1)
    pool.workers[1].die()
    assert _wait(lambda: 1 not in pool.ready_cards())
    assert pool.busy_job(0) == "j0"
    pool.workers[0].emit({"type": CONTROL_IDLE, "job_id": "j0"})
    assert _wait(lambda: 0 in pool.ready_cards())
    assert set(pool.ready_cards()) >= {0, 2, 3}


def test_the_orphaned_job_is_reported_exactly_once(pool):
    pool.start()
    _ready(pool, 0)
    pool.dispatch(_job("j0"), card=0)
    pool.workers[0].die()
    assert _wait(lambda: pool.lost == [(0, "j0")])


def test_a_worker_that_dies_while_idle_orphans_nothing(pool):
    """A crash between jobs must not invent a failed job."""
    pool.start()
    _ready(pool, 0)
    pool.workers[0].die()
    assert _wait(lambda: 0 not in pool.ready_cards())
    assert pool.lost == []


def test_the_chip_is_freed_not_left_marked_busy(pool):
    """A card left busy forever is one quarter of the booth gone silently."""
    pool.start()
    _ready(pool, 0)
    pool.dispatch(_job("j0"), card=0)
    pool.workers[0].die()
    assert _wait(lambda: pool.busy_job(0) is None)


def test_a_dead_worker_is_respawned(pool):
    pool.start()
    _ready(pool, 0)
    pool.workers[0].die()
    assert _wait(lambda: pool.spawns.count(0) == 2)


def test_a_respawned_worker_folds_again(pool):
    """Respawning is only worth anything if the new one is usable."""
    pool.start()
    _ready(pool, 0)
    pool.workers[0].die()
    assert _wait(lambda: pool.spawns.count(0) == 2)
    pool.workers[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: 0 in pool.ready_cards())
    pool.dispatch(_job("j9"), card=0)
    assert [c["job_id"] for c in pool.workers[0].commands] == ["j9"]


def test_a_chip_that_keeps_dying_is_retired_rather_than_respawned_forever(pool):
    """A chip in a bad state (a raced 'remote-only' bring-up, per tt-bio's
    own device-init notes) would otherwise respawn every 5s all day, each
    time taking a device-init lock the other three workers need."""
    pool.start()
    for _ in range(WORKER_RETIRE_AFTER):
        pool.workers[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: 0 in pool.ready_cards())
        pool.workers[0].die()
        _wait(lambda: 0 not in pool.ready_cards())
    assert _wait(lambda: pool.spawns.count(0) == WORKER_RETIRE_AFTER)
    assert 0 not in pool.ready_cards()
    assert 0 in pool.cards, "a retired chip has not stopped existing"


def test_a_completed_job_resets_the_death_count(pool):
    """One bad fold followed by a crash is not a bad chip. Without this, a
    booth that loses one worker at 9am and another at 2pm retires a
    perfectly good card."""
    pool.start()
    for _ in range(WORKER_RETIRE_AFTER + 2):
        pool.workers[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: 0 in pool.ready_cards())
        pool.dispatch(_job("jx"), card=0)
        pool.workers[0].emit({"type": CONTROL_IDLE, "job_id": "jx"})
        assert _wait(lambda: pool.busy_job(0) is None)
        pool.workers[0].die()
        _wait(lambda: 0 not in pool.ready_cards())
    assert _wait(lambda: 0 in pool.ready_cards() or pool.spawns.count(0) > WORKER_RETIRE_AFTER)


def test_retiring_one_chip_leaves_the_others_alone(pool):
    pool.start()
    _ready(pool, 1, 2, 3)
    for _ in range(WORKER_RETIRE_AFTER):
        pool.workers[0].emit({"type": CONTROL_READY})
        _wait(lambda: 0 in pool.ready_cards())
        pool.workers[0].die()
        _wait(lambda: 0 not in pool.ready_cards())
    assert set(pool.ready_cards()) == {1, 2, 3}


def test_a_fatal_control_line_retires_without_waiting_for_three_deaths(pool):
    """The worker told us it cannot serve. Respawning it twice more to
    confirm is time the booth spends at three chips for no information."""
    pool.start()
    pool.workers[0].emit({"type": CONTROL_FATAL, "reason": "device lease held"})
    pool.workers[0].die()
    assert _wait(lambda: pool.spawns.count(0) == 1)
    assert 0 not in pool.ready_cards()


def test_dispatching_to_a_retired_card_raises_rather_than_vanishing(pool):
    """A silently-dropped job is a target that never folds and never fails."""
    pool.start()
    pool.workers[0].emit({"type": CONTROL_FATAL, "reason": "x"})
    pool.workers[0].die()
    assert _wait(lambda: 0 not in pool.ready_cards())
    with pytest.raises(ValueError):
        pool.dispatch(_job("j1"), card=0)


def test_all_four_dying_does_not_raise_out_of_the_pool(pool):
    """The booth is now unable to fold. It must say so (Task 8's not_ready),
    not crash -- an unattended booth needs a process that stays up."""
    pool.start()
    _ready(pool, 0, 1, 2, 3)
    for card in (0, 1, 2, 3):
        pool.workers[card].die()
    assert _wait(lambda: pool.ready_cards() == [])
    assert not pool.any_ready()
```

**Mutations these must catch:** handling a death by stopping the pool (test 1 red); reporting the loss from a card-level loop over all workers rather than the dying one (test 1 red); reporting a loss when no job was in flight (test 3 red); leaving `busy` set (test 4 red); never respawning (test 5 red); respawning without re-arming readiness (test 6 red); respawning unconditionally forever (test 7 red); never resetting the death counter (test 8 red); ignoring `CONTROL_FATAL` (test 11 red); making `dispatch` to a retired card a silent no-op (test 12 red).

**Test 8 is the one most likely to be written so it cannot fail.** A version that only asserts "card 0 is still alive at the end" passes against a pool with no counter at all. Make it assert the *reset*, and check it by deleting the reset line and watching it go red.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

---

### Task 8: The daemon owns no device [no device]

**Files:** Rewrite `runner/daemon.py`. Test: `tests/unit/runner/test_daemon_multichip.py`; the existing `tests/unit/runner/test_daemon.py` is rewritten with it.

**Why:** This is where the parts meet. The daemon keeps the queue, the `CardPool`, the `EventServer` and the failure/quarantine policy; it loses the `Folder`, the device and `_run_one`'s in-line fold.

**Produces:**

- `DaemonConfig` loses `device_id`, gains `device_ids: str | None = None` (the `--devices 0,1,2,3` CLI flag the spec quotes tt-bio's own docs for).
- `Daemon` has **no `folder` attribute** and never imports `Folder`.
- `Daemon.run()` builds `worker_specs(...)`, constructs `CardPool([s.card for s in specs])`, starts the pool, and loops: for every schedulable card that is also pool-ready, take a job and dispatch it.
- `_hello()` reports `not_ready` until `pool.any_ready()`, then `hello` with `cards = cards.all_indices()`.
- `on_worker_lost(card, job_id)` emits a `job_error` for the orphaned job, marks the card idle, and records the failure against the target.

**Ruling on quarantine, because it is easy to get backwards:** a worker death counts as **one failure for the target** (a target that reliably kills a worker is a target that must eventually be quarantined — `QUARANTINE_AFTER = 3` unchanged) **and separately** against the card (`WORKER_RETIRE_AFTER`, Task 7). The two counters are independent and neither may be derived from the other.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from runner.daemon import Daemon, DaemonConfig
from runner.queue import Job


class _FakePool:
    def __init__(self, cards=(0, 1, 2, 3), ready=None):
        self.cards = list(cards)
        self._ready = list(cards if ready is None else ready)
        self._busy = {}
        self.dispatched = []
        self.started = self.stopped = 0
        self.on_worker_lost = None

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def any_ready(self):
        return bool(self._ready)

    def ready_cards(self):
        return sorted(c for c in self._ready if c not in self._busy)

    def busy_job(self, card):
        return self._busy.get(card)

    def dispatch(self, job, card):
        if card not in self.ready_cards():
            raise ValueError(f"card {card} is not ready")
        self._busy[card] = job.job_id
        self.dispatched.append((card, job.job_id, job.target_id))

    # -- test helpers --
    def finish(self, card):
        self._busy.pop(card, None)


def _daemon(tmp_path, pool, **over):
    config = DaemonConfig(
        socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
        playlist_dir=str(tmp_path / "playlist"), log_root=str(tmp_path / "logs"),
        **over)
    daemon = Daemon(config)
    daemon.pool = pool
    daemon.server = _CollectingServer()
    return daemon


class _CollectingServer:
    def __init__(self):
        self.events = []
        self.started = self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def broadcast(self, event):
        self.events.append(event)
        return 1


def test_the_daemon_holds_no_folder_and_no_device(tmp_path):
    """The parent owns no device. A Folder here is a fifth process's worth
    of model weights and a lease on a chip nobody is folding on."""
    import runner.daemon as mod
    daemon = _daemon(tmp_path, _FakePool())
    assert not hasattr(daemon, "folder")
    assert "Folder" not in dir(mod)


def test_every_idle_card_gets_a_job(tmp_path):
    """The entire point of the phase."""
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    for i in range(4):
        daemon.queue.submit(Job(job_id=f"j{i}", target_id=f"t{i}",
                                input_path=f"/p/t{i}.yaml"))
    daemon.dispatch_once()
    assert sorted(c for c, _j, _t in pool.dispatched) == [0, 1, 2, 3]


def test_one_job_goes_to_exactly_one_card(tmp_path):
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    daemon.queue.submit(Job(job_id="j1", target_id="t", input_path="/p/t.yaml"))
    daemon.dispatch_once()
    assert len(pool.dispatched) == 1


def test_a_quarantined_card_receives_nothing_while_the_others_fold(tmp_path):
    """CardPool's 85C guard has never fired in anger. This is the first time
    it decides which of four chips keeps working."""
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    from runner.cards import CardState
    daemon.cards.update([CardState(index=2, board_type="p300c",
                                   temperature_c=91.0, power_w=60.0,
                                   aiclk_mhz=1350.0)])
    for i in range(4):
        daemon.queue.submit(Job(job_id=f"j{i}", target_id=f"t{i}",
                                input_path=f"/p/t{i}.yaml"))
    daemon.dispatch_once()
    assert sorted(c for c, _j, _t in pool.dispatched) == [0, 1, 3]


def test_a_card_the_pool_is_not_ready_on_is_not_dispatched_to(tmp_path):
    """CardPool knows about heat; the pool knows about processes. Both have
    to agree before a job goes anywhere."""
    pool = _FakePool(ready=[0, 1])
    daemon = _daemon(tmp_path, pool)
    for i in range(4):
        daemon.queue.submit(Job(job_id=f"j{i}", target_id=f"t{i}",
                                input_path=f"/p/t{i}.yaml"))
    daemon.dispatch_once()
    assert sorted(c for c, _j, _t in pool.dispatched) == [0, 1]


def test_a_dispatch_race_requeues_the_job_rather_than_losing_it(tmp_path):
    """The telemetry thread can quarantine a card between the schedulable()
    check and mark_busy(). Today's daemon already handles this; it must not
    be lost in the move."""
    class _RacingPool(_FakePool):
        def dispatch(self, job, card):
            raise ValueError("worker died between the check and the send")

    daemon = _daemon(tmp_path, _RacingPool())
    daemon.queue.submit(Job(job_id="j1", target_id="t", input_path="/p/t.yaml"))
    daemon.dispatch_once()
    assert [j.job_id for j in daemon.queue.pending] == ["j1"]


def test_hello_says_not_ready_until_a_worker_can_actually_fold(tmp_path):
    """Preflight must not report ready before at least one worker can fold
    (spec, 'Feasibility'). Four cold model loads take 6-9s each under
    contention; a UI connecting in that window must see 'preparing'."""
    pool = _FakePool(ready=[])
    daemon = _daemon(tmp_path, pool)
    assert daemon._hello()["type"] == "not_ready"
    pool._ready = [1]
    assert daemon._hello()["type"] == "hello"


def test_hello_reports_every_chip_not_only_the_free_ones(tmp_path):
    """Unchanged behaviour, restated: a card mid-fold has not stopped
    existing."""
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    pool._busy = {0: "j0", 1: "j1"}
    assert daemon._hello()["cards"] == [0, 1, 2, 3]


def test_a_lost_worker_produces_a_job_error_for_its_orphaned_job(tmp_path):
    """Without this the UI sits in `folding` forever: it was told a job
    started and is never told it ended."""
    daemon = _daemon(tmp_path, _FakePool())
    daemon.on_worker_lost(card=2, job_id="j5", target_id="trpcage")
    errors = [e for e in daemon.server.events if e["type"] == "job_error"]
    assert [e["job_id"] for e in errors] == ["j5"]


def test_a_lost_worker_frees_its_card_in_the_pool_bookkeeping(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    daemon.cards.mark_busy(2)
    daemon.on_worker_lost(card=2, job_id="j5", target_id="trpcage")
    assert 2 in daemon.cards.schedulable()


def test_a_lost_worker_counts_against_its_target_not_against_the_others(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    for _ in range(3):
        daemon.on_worker_lost(card=0, job_id="j", target_id="poison")
    assert "poison" in daemon._quarantined
    assert "trpcage" not in daemon._quarantined


def test_a_lost_worker_never_raises_out_of_the_callback(tmp_path):
    """It runs on a pool reader thread. An exception there kills that
    worker's reader and the chip goes silent."""
    class _ExplodingCards:
        def mark_idle(self, index):
            raise RuntimeError("boom")

        def schedulable(self):
            return []

        def all_indices(self):
            return [0]

    daemon = _daemon(tmp_path, _FakePool())
    daemon.cards = _ExplodingCards()
    daemon.on_worker_lost(card=0, job_id="j1", target_id="t")   # must not raise


def test_stopping_the_daemon_stops_every_worker(tmp_path):
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    daemon.stop()
    daemon.run()
    assert pool.stopped >= 1


def test_no_schedulable_cards_idles_rather_than_folding_onto_hot_hardware(tmp_path):
    from runner.cards import CardState
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    daemon.cards.update([CardState(index=i, board_type="p300c",
                                   temperature_c=95.0, power_w=60.0,
                                   aiclk_mhz=1350.0) for i in range(4)])
    daemon.queue.submit(Job(job_id="j1", target_id="t", input_path="/p/t.yaml"))
    daemon.dispatch_once()
    assert pool.dispatched == []
    assert [j.job_id for j in daemon.queue.pending] == ["j1"]
```

**Mutations these must catch:** keeping a `Folder` (test 1 red); dispatching to only the first free card (test 2 red); dispatching one job to several cards (test 3 red); ignoring `CardPool.schedulable()` (tests 4, 15 red); ignoring `pool.ready_cards()` (test 5 red); dropping a job on a dispatch race (test 6 red); reporting `hello` before any worker is ready (test 7 red); reporting only free cards in `hello` (test 8 red); not emitting `job_error` for an orphaned job (test 9 red); not marking the card idle (test 10 red); counting a worker loss against every target (test 11 red); removing the guard around `on_worker_lost` (test 12 red).

`dispatch_once()` is a new, deliberately-extracted method: one pass of "for each dispatchable card, take a job and send it". It exists so the loop body is testable without running `run()`, exactly as `_run_one` was.

Extract `_FakePool`, `_CollectingServer` and `_daemon` into **`tests/unit/runner/_daemonfakes.py`** as part of this task — Tasks 10 and 11 import them from there, and three copies of a fake pool is three places for the fake to drift from the real one.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

Also in this task: `main()` gains `--devices` (default: all), and the long `DaemonConfig.device_id` comment explaining why `--device` was deleted is **replaced**, not deleted — rewrite it to say what is now true (one process per chip, `TT_VISIBLE_DEVICES` set by the parent at spawn, `CardPool` and the hardware finally referring to the same chips). `docs/followups.md`'s "`--device` was removed rather than plumbed" entry moves to a "closed" section with a pointer here.

---

### Task 9: A pick becomes a fold [no device]

**Files:** Modify `runner/queue.py`, `runner/daemon.py`. Test: `tests/unit/runner/test_visitor_pick.py`

**Why, and read this paragraph before writing a line of it:** `runner/queue.py` has had a visitor-priority path since Phase 3a and **it has never once run.** Its own docstring says so — "nothing ever submits at a priority above 0 … the priority path exists for the phase that adds a client→server message". This is that phase. Every line of `JobQueue`'s ordering is therefore code that has been unit-tested in isolation and never exercised end to end, and the tests below are written on the assumption that it is **unproven**, not proven: they assert that the priority *takes effect on a dispatch*, not that a sorted list is sorted.

**The ruling on "immediately", because it is the whole design question in this task.**

With four chips there is usually a free one and a pick starts within a second. The interesting case is the one that will happen at a venue with a queue of visitors: **all four chips busy.** The pick then goes to the **head of the queue and takes the next chip to free. It never preempts a running fold.**

Why not preempt:

- Tearing down a fold mid-device-op is, in `runner/queue.py`'s own words, "a needless source of instability". This phase multiplies device operations by four and adds worker processes that can die; adding deliberate mid-fold teardown to that mix buys seconds and costs the failure mode that Task 7 exists to survive.
- Preemption is visible destruction. The quad shows four cells. Preempting one blanks a structure a visitor is watching — possibly the previous visitor's own pick — to serve the newest tap. The booth would get less trustworthy as it got busier.
- The wait is bounded and small, and bounded by the *earliest*-finishing of four folds rather than the longest: measured warm folds are 4.35–4.45 s for the 20-residue target (`docs/followups.md`, 30-fold soak), so the expected wait with four chips busy is a fraction of one fold. If the curated playlist's larger targets make that materially worse, that is a finding about the playlist, and Task 18 measures it rather than guessing.

Also rejected, explicitly, so nobody re-proposes it: **reserving a chip for visitors.** It idles a quarter of the booth all day — in front of a crowd, on the demo whose entire premise is that all four chips work — to save a few seconds occasionally.

**What is done about the wait instead**, because "a visitor taps and sees nothing for twenty seconds" is a booth that reads as broken no matter how correct the queue is:

1. The UI acknowledges the pick **at tap time**, before any daemon has answered (Task 12's `pick_status`, Task 17's on-screen notice). Nothing about that acknowledgement depends on the socket.
2. The daemon's dispatch loop is **woken by a pick** rather than discovering it at the end of a 5 s or 10 s backoff. Today `run()` sits in `self._stop.wait(5.0)` when no card is schedulable and `self._stop.wait(10.0)` when the playlist is empty; a pick landing one millisecond into either of those is a pick the visitor waits ten seconds for, which is most of the twenty seconds this ruling is about. Add `Daemon._wake` (a `threading.Event` that `stop()` also sets), wait on it instead of on `_stop` in the idle paths, clear it at the top of each pass, and set it from `on_client_message`.
3. The pick is released by the **existing 45 s idle timeout** (Task 17), so a visitor who walks away pins nothing.

**Produces:**

- `runner/queue.py`: `VISITOR_PRIORITY = 10` (any value above 0 works; 10 leaves room for a priority between the playlist and a visitor without renumbering either), and `JobQueue.remove(job_id) -> bool` — thread-safe, returns whether anything was removed.
- `runner/queue.py`'s module docstring rewritten. It currently describes the visitor path in the conditional ("would be submitted") and states in the present tense that nothing can reach it. Both halves stop being true in this task, and the docstring's own closing sentence — "Stated in the past/conditional here on purpose" — is exactly the kind of comment that rots into a lie. A test below pins the rewrite.
- `runner/daemon.py`: `MAX_PENDING_PICKS = 1`, `DISPATCH_POLL_S = 0.25`, `Daemon._wake`, and `Daemon.on_client_message(message)`, wired into `EventServer(..., on_client_message=self.on_client_message)`.

**`on_client_message`'s contract:**

- It runs on a **server reader thread** (Task 4) and therefore never raises. A guard with the `return` outside the `try`, the same shape the GLib callbacks use, for the same reason: an exception here kills that client's reader and the visitor's UI goes deaf.
- Anything that is not a `pick` is ignored. `decode_client_message` has already refused everything malformed; this is the second line, not the first.
- **Target resolution goes through the playlist enumeration**, never through a path join. `target_id` arrives from another process; `Path(playlist_dir) / f"{target_id}.yaml"` with `target_id = "../../../etc/passwd"` is a file read outside the playlist and, worse, a path handed to the folder. Resolve by matching against the same `sorted(Path(playlist_dir).glob("*.yaml"))` stems `_enqueue_playlist` already walks, and ignore anything that does not match one exactly.
- A quarantined target is ignored: `QUARANTINE_AFTER` means "this one has failed three times", and a visitor's tap does not overrule that.
- A target already in flight queues **nothing** — the UI focuses the cell already folding it (Task 12's focus rule), which is what the visitor asked for and is faster than a second fold of the same thing.
- At most `MAX_PENDING_PICKS` visitor jobs are pending. A new pick **removes the previous pending one** and replaces it. One visitor, one pick: this mirrors the UI exactly, which tracks a single `selected_target`, and it is what bounds a child tapping forty targets in ten seconds.

- [ ] **Step 1: Write the failing tests**

```python
import threading

import pytest

from runner.cards import CardState
from runner.queue import VISITOR_PRIORITY, Job, JobQueue

from _daemonfakes import _FakePool, _daemon      # extracted in Task 8


def _playlist(tmp_path, *names):
    directory = tmp_path / "playlist"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / f"{name}.yaml").write_text("sequences: []\n")
    return directory


def _pick(target_id):
    from protocol.events import pick_message
    return pick_message(target_id)


def _busy_daemon(tmp_path, targets=("alpha", "beta", "gamma", "delta")):
    """A daemon with a real playlist and every card already folding."""
    _playlist(tmp_path, *targets, "hemoglobin")
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    daemon._enqueue_playlist()
    daemon.dispatch_once()                      # all four cards now busy
    assert len(pool.dispatched) == 4
    return daemon, pool


# ---- the queue itself ----------------------------------------------------

def test_a_visitor_job_is_taken_before_an_older_attract_job():
    queue = JobQueue()
    for n in range(5):
        queue.submit(Job(job_id=f"a{n}", target_id=f"t{n}", input_path="/p.yaml"))
    queue.submit(Job(job_id="v1", target_id="hemoglobin", input_path="/p.yaml",
                     priority=VISITOR_PRIORITY))
    assert queue.take().job_id == "v1"


def test_two_visitor_jobs_keep_their_own_order():
    """Priority orders between bands; submission order still orders within
    one. A visitor path that reverses itself under load is a visitor path
    that serves the wrong person first."""
    queue = JobQueue()
    for job_id in ("v1", "v2"):
        queue.submit(Job(job_id=job_id, target_id="t", input_path="/p.yaml",
                         priority=VISITOR_PRIORITY))
    assert [queue.take().job_id, queue.take().job_id] == ["v1", "v2"]


def test_remove_takes_out_exactly_the_named_job():
    queue = JobQueue()
    for job_id in ("a", "b", "c"):
        queue.submit(Job(job_id=job_id, target_id="t", input_path="/p.yaml"))
    assert queue.remove("b") is True
    assert [j.job_id for j in queue.pending] == ["a", "c"]


def test_removing_a_job_that_is_already_gone_is_not_an_error():
    """The dispatch loop can take a pending pick between the decision to
    replace it and the removal itself."""
    queue = JobQueue()
    assert queue.remove("never-existed") is False


def test_the_queue_docstring_no_longer_says_the_visitor_path_is_unreachable():
    """It reads, verbatim today: 'nothing ever submits at a priority above
    0'. That sentence was true for a whole phase and is now false; a
    docstring that survives the change it describes is the next reader's
    wrong mental model."""
    import runner.queue as mod
    text = mod.__doc__.lower()
    assert "nothing ever submits at a priority above 0" not in text
    assert "the socket protocol is one-way" not in text


# ---- the daemon: the priority actually taking effect ----------------------

def test_a_pick_is_dispatched_before_the_whole_attract_backlog(tmp_path):
    """THE test for this task. The priority path has never run in
    production; a queue-ordering test proves the list sorts, and proves
    nothing about whether the daemon ever submits above 0. Free one card
    with eight attract jobs waiting and see which target actually goes."""
    daemon, pool = _busy_daemon(tmp_path)
    daemon._enqueue_playlist()                  # a deep backlog behind it
    assert len(daemon.queue) >= 4
    daemon.on_client_message(_pick("hemoglobin"))
    visitor = [j for j in daemon.queue.pending if j.priority == VISITOR_PRIORITY]
    assert len(visitor) == 1
    pool.finish(2)
    daemon.dispatch_once()
    # The JOB, not the target: the playlist contains hemoglobin too, so
    # asserting on target_id alone would pass against a daemon that submits
    # the pick at priority 0 and happens to reach the attract copy of it --
    # which is exactly the mutation this test exists to catch.
    assert pool.dispatched[-1][:2] == (2, visitor[0].job_id)


def test_a_pick_never_cancels_a_fold_that_is_already_running(tmp_path):
    """The ruling, pinned. Preemption would blank a cell a visitor is
    watching and tear down a fold mid-device-op."""
    daemon, pool = _busy_daemon(tmp_path)
    in_flight = {card: pool.busy_job(card) for card in (0, 1, 2, 3)}
    daemon.on_client_message(_pick("hemoglobin"))
    assert {card: pool.busy_job(card) for card in (0, 1, 2, 3)} == in_flight


def test_a_pick_arriving_with_every_card_busy_is_kept_not_dropped(tmp_path):
    daemon, pool = _busy_daemon(tmp_path)
    daemon.on_client_message(_pick("hemoglobin"))
    assert [j.target_id for j in daemon.queue.pending
            if j.priority == VISITOR_PRIORITY] == ["hemoglobin"]


def test_a_playlist_refill_does_not_bury_a_waiting_pick(tmp_path):
    """run() refills the playlist whenever the queue empties. A pick that a
    refill can push behind twenty targets is a pick the visitor never sees."""
    daemon, pool = _busy_daemon(tmp_path)
    daemon.on_client_message(_pick("hemoglobin"))
    daemon._enqueue_playlist()
    head = daemon.queue.pending[0]
    assert head.target_id == "hemoglobin"
    assert head.priority == VISITOR_PRIORITY, (
        "the playlist has a hemoglobin of its own; the head of the queue "
        "must be the VISITOR's job, not the attract job with the same name")


def test_a_pick_for_an_unknown_target_is_ignored(tmp_path):
    daemon, pool = _busy_daemon(tmp_path)
    before = len(daemon.queue)
    daemon.on_client_message(_pick("not-a-real-target"))
    assert len(daemon.queue) == before


def test_a_pick_cannot_name_a_file_outside_the_playlist(tmp_path):
    """`target_id` arrives from another process. Joining it onto a path is
    how a socket message becomes a file read somewhere else on the box."""
    daemon, pool = _busy_daemon(tmp_path)
    outside = tmp_path / "secret.yaml"
    outside.write_text("sequences: []\n")
    before = len(daemon.queue)
    for hostile in ("../secret", "../../etc/passwd", "/etc/passwd",
                    "alpha/../../secret"):
        daemon.on_client_message(_pick(hostile))
    assert len(daemon.queue) == before


def test_a_pick_for_a_quarantined_target_is_ignored(tmp_path):
    """Three failures means three failures. A tap does not overrule the
    guard that stopped the booth failing the same fold all afternoon."""
    daemon, pool = _busy_daemon(tmp_path)
    for _ in range(3):
        daemon._record_failure("hemoglobin")
    before = len(daemon.queue)
    daemon.on_client_message(_pick("hemoglobin"))
    assert len(daemon.queue) == before


def test_a_second_pick_replaces_the_first_rather_than_queueing_both(tmp_path):
    """One visitor, one pick -- the same thing the UI tracks. Without this,
    a child tapping forty targets queues forty folds ahead of the playlist
    and the booth stops being a playlist for the next ten minutes."""
    daemon, pool = _busy_daemon(tmp_path)
    daemon.on_client_message(_pick("hemoglobin"))
    daemon.on_client_message(_pick("alpha"))
    visitor_jobs = [j.target_id for j in daemon.queue.pending
                    if j.priority == VISITOR_PRIORITY]
    assert visitor_jobs == ["alpha"]


def test_a_pick_for_a_target_already_folding_queues_nothing(tmp_path):
    """It is already happening. The UI focuses that cell (Task 12); a second
    fold of the same target would occupy a chip to show the same thing."""
    daemon, pool = _busy_daemon(tmp_path)
    folding = pool.dispatched[0][2]
    before = len(daemon.queue)
    daemon.on_client_message(_pick(folding))
    assert len(daemon.queue) == before


def test_a_pick_wakes_a_loop_that_would_otherwise_sit_out_a_backoff(tmp_path):
    """run() waits 5s with no schedulable card and 10s with an empty
    playlist. A pick that lands one millisecond into either of those is a
    pick the visitor waits ten seconds for -- which is most of the twenty
    seconds after which a booth reads as broken."""
    daemon, pool = _busy_daemon(tmp_path)
    daemon._wake.clear()
    daemon.on_client_message(_pick("hemoglobin"))
    assert daemon._wake.is_set()


def test_on_client_message_never_raises_whatever_arrives(tmp_path):
    """It runs on a server reader thread. An exception there kills that
    client's reader and the UI goes deaf with nothing on screen saying so."""
    daemon, pool = _busy_daemon(tmp_path)

    class _ExplodingQueue:
        def submit(self, job):
            raise RuntimeError("boom")

        def remove(self, job_id):
            raise RuntimeError("boom")

        @property
        def pending(self):
            raise RuntimeError("boom")

    daemon.queue = _ExplodingQueue()
    daemon.on_client_message(_pick("hemoglobin"))        # must not raise
    daemon.on_client_message({"type": "pick"})           # nor this
    daemon.on_client_message({})                         # nor this
    daemon.on_client_message(None)                       # nor this


def test_a_visitor_job_that_fails_counts_against_its_target_like_any_other(tmp_path):
    """A target that kills a worker three times is quarantined whether a
    visitor asked for it or not. The two counters stay independent."""
    daemon, pool = _busy_daemon(tmp_path)
    for _ in range(3):
        daemon.on_worker_lost(card=0, job_id="jv", target_id="hemoglobin")
    assert "hemoglobin" in daemon._quarantined


def test_a_pick_goes_to_the_next_card_to_free_not_a_reserved_one(tmp_path):
    """Explicitly rejected design: holding a chip idle for visitors. All
    four fold the playlist; the pick takes whichever frees first."""
    daemon, pool = _busy_daemon(tmp_path)
    daemon.on_client_message(_pick("hemoglobin"))
    visitor = [j for j in daemon.queue.pending if j.priority == VISITOR_PRIORITY]
    pool.finish(3)
    daemon.dispatch_once()
    assert pool.dispatched[-1][:2] == (3, visitor[0].job_id)
```

**Mutations these must catch:** submitting the pick at priority `0` — the mutation that reproduces today's behaviour exactly (tests 6, 8, 9, 18 red); sorting the queue by priority only, losing submission order within a band (test 2 red); dropping `JobQueue.remove` and letting picks accumulate (test 13 red); making `remove` raise on a missing id (test 4 red); leaving the module docstring alone (test 5 red); cancelling an in-flight job to make room (test 7 red); resolving the target by joining `target_id` onto `playlist_dir` (test 11 red); accepting any string as a target (test 10 red); skipping the quarantine check (test 12 red); queueing a duplicate for a target already in flight (test 14 red); never setting `_wake` (test 15 red); letting `on_client_message` raise (test 16 red); deriving the target's failure count from the card's, or the reverse (test 17 red).

**Test 6 is the one that decides whether this task did anything at all.** Verify it by setting the pick's priority to `0` and watching it go red; if it stays green, the test is measuring the queue's ordering rather than the daemon's dispatch, and it is the wrong test.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

`dispatch_once()` does not change: the pick is just a job with a higher priority, and the whole point of the priority queue is that nothing downstream of `queue.take()` needs to know a visitor exists. If you find yourself special-casing visitor jobs inside the dispatch loop, stop — that is a plan bug, raise it.

The `run()` loop's two idle waits change from `self._stop.wait(N)` to a wait on `_wake` with the same timeout, and `stop()` sets `_wake` as well as `_stop` so shutdown stays prompt. `DISPATCH_POLL_S` is the busy-path poll; the 5 s and 10 s backoffs keep their numbers, they simply become interruptible.

---

### Task 10: Thermal at 4× [no device]

**Files:** Modify `runner/daemon.py` if needed. Test: `tests/unit/runner/test_thermal_four_up.py`

**Why:** `CardPool`'s 85 °C quarantine "already exists and has never fired in anger" (spec). Four chips folding continuously is when it fires. Its unit tests cover the class; nothing covers what the *daemon* does when it fires with three other folds in flight.

**Produces:** no new production surface if Task 8 is right. This task's job is to prove it, and to fix it if it is not.

- [ ] **Step 1: Write the failing tests**

```python
from runner.cards import CardState
from runner.queue import Job

from _daemonfakes import _FakePool, _daemon      # extracted in Task 8


def _hot(index, c=91.0):
    return CardState(index=index, board_type="p300c", temperature_c=c,
                     power_w=60.0, aiclk_mhz=1350.0)


def _cool(index, c=45.0):
    return CardState(index=index, board_type="p300c", temperature_c=c,
                     power_w=18.0, aiclk_mhz=800.0)


def _fill_queue(daemon, n=8):
    for i in range(n):
        daemon.queue.submit(Job(job_id=f"j{i}", target_id=f"t{i}",
                                input_path=f"/p/t{i}.yaml"))


def test_a_chip_that_overheats_mid_fold_keeps_its_job(tmp_path):
    """A fold in flight is not cancelled by heat -- tearing down a fold
    mid-device-op is a needless source of instability (runner/queue.py)."""
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    _fill_queue(daemon)
    daemon.dispatch_once()
    assert pool.busy_job(1) is not None
    daemon.cards.update([_hot(1)])
    assert pool.busy_job(1) is not None


def test_a_hot_chip_receives_no_further_work_while_the_others_do(tmp_path):
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    _fill_queue(daemon)
    daemon.dispatch_once()
    daemon.cards.update([_hot(1)])
    for card in (0, 1, 2, 3):
        pool.finish(card)
    daemon.dispatch_once()
    assert sorted({c for c, _j, _t in pool.dispatched[-3:]}) == [0, 2, 3]


def test_a_cooled_chip_comes_back_into_rotation(tmp_path):
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    _fill_queue(daemon)
    daemon.cards.update([_hot(1)])
    daemon.dispatch_once()
    daemon.cards.update([_cool(1)])
    for card in (0, 2, 3):
        pool.finish(card)
    daemon.dispatch_once()
    assert 1 in {c for c, _j, _t in pool.dispatched}


def test_every_quarantine_transition_reaches_the_wire(tmp_path):
    """The UI dims a quarantined chip. If the event never leaves, a hot chip
    looks healthy on screen for the rest of the day."""
    daemon = _daemon(tmp_path, _FakePool())
    for event in daemon.cards.update([_hot(1)]):
        daemon._emit(event)
    states = [(e["card"], e["state"]) for e in daemon.server.events
              if e["type"] == "card_state"]
    assert (1, "quarantined") in states


def test_all_four_hot_idles_the_booth_without_stopping_it(tmp_path):
    """'Idle calmly and log loudly rather than folding onto a card we have
    just decided is unsafe' -- now with four cards, and a daemon that must
    still be alive when they cool."""
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    _fill_queue(daemon)
    daemon.cards.update([_hot(i) for i in range(4)])
    daemon.dispatch_once()
    assert pool.dispatched == []
    daemon.cards.update([_cool(i) for i in range(4)])
    daemon.dispatch_once()
    assert len(pool.dispatched) == 4
```

**Mutations these must catch:** cancelling an in-flight job on quarantine (test 1 red); quarantining the wrong card / all cards (test 2 red); never un-quarantining (test 3 red); dropping the `card_state` events returned by `update()` (test 4 red); treating "no schedulable cards" as fatal (test 5 red).

- [ ] **Step 2: Implement any fix the tests demand, verify mutations, run `./scripts/test.sh`, commit**

If all five pass without a production change, say so explicitly in the report and name the mutation you used to prove each one can fail. A task that changes no code and claims coverage is exactly the shape `docs/followups.md` warns about.

---

### Task 11: Logs and structures at 4× [no device]

**Files:** Modify `runner/daemon.py`, `runner/pool.py`. Test: `tests/unit/runner/test_janitors_four_up.py`

**Why:** The spec flags this and `docs/followups.md` records what it cost last time: tt-metal's Inspector held an unlinked log open and wrote 13–14 MB/s into it, invisible to the directory walk the daemon trusted, on a tmpfs log root. This phase adds four writers *and* four per-worker stdout/stderr files that the parent itself holds open — the same trap, one layer up.

**Produces:**

- Each worker's stdout/stderr goes to `<log-root>/card-<n>/worker.log`, opened by the parent in append mode. The paths are exposed on the daemon as **`daemon.worker_log_paths`** (a list of `str`), populated from the pool.
- `_prune_logs` **protects** those paths (`prune_log_root`'s `protect=` argument, already there for structures) and instead bounds them by `os.truncate` in place — which is correct with `O_APPEND` and is the one operation that actually frees the blocks while a process holds the fd.
- `_prune_structures` walks **every** worker's structures directory, exposed as **`daemon.structures_dirs`** (a list of `str`, one per card, from `runner.folder._structures_dir_for`).
- **`_emit_and_track(card, event)`** — today's `_emit_and_track(event)` gains the card, and `self._recent_structures` becomes `dict[card] -> deque(maxlen=PROTECTED_STRUCTURE_COUNT)`.

- [ ] **Step 1: Write the failing tests**

```python
import os

import pytest

from runner.env import log_root_size

from _daemonfakes import _FakePool, _daemon


def _write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_each_worker_writes_into_its_own_log_directory(tmp_path):
    """One tree per chip: a crash's evidence is attributable, and an
    oldest-first sweep cannot delete another worker's."""
    from runner.workers import WorkerSpec, worker_environ
    spec = WorkerSpec(card=3, label="q:tt3", visible_devices="3",
                      logical_device_id=0, mesh_graph_descriptor=None)
    env = worker_environ(spec, log_root=str(tmp_path), n_workers=4, base={})
    assert env["TT_METAL_LOGS_PATH"].endswith("card-3")


def test_a_live_worker_log_is_never_unlinked_by_the_pruner(tmp_path):
    """Unlinking a file a process holds open removes its NAME and frees
    nothing -- the exact failure docs/followups.md measured at 13-14 MB/s
    into a tmpfs root. The sweep must refuse to touch these."""
    daemon = _daemon(tmp_path, _FakePool(), log_budget_bytes=1)
    live = [_write(tmp_path / "logs" / f"card-{c}" / "worker.log", 4096)
            for c in range(4)]
    daemon.worker_log_paths = [str(p) for p in live]
    _write(tmp_path / "logs" / "card-0" / "kernels.yaml", 4096)
    daemon._prune_logs()
    assert all(p.exists() for p in live)


def test_an_oversized_worker_log_is_truncated_in_place_not_deleted(tmp_path):
    """Truncation is what actually frees the blocks under a held fd."""
    from runner.pool import WORKER_LOG_CAP_BYTES
    daemon = _daemon(tmp_path, _FakePool())
    log = _write(tmp_path / "logs" / "card-0" / "worker.log",
                 WORKER_LOG_CAP_BYTES + 4096)
    daemon.worker_log_paths = [str(log)]
    daemon._prune_logs()
    assert log.exists()
    assert log.stat().st_size <= WORKER_LOG_CAP_BYTES


def test_a_worker_log_under_the_cap_is_left_alone(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    log = _write(tmp_path / "logs" / "card-0" / "worker.log", 1024)
    daemon.worker_log_paths = [str(log)]
    daemon._prune_logs()
    assert log.stat().st_size == 1024


def test_other_files_under_the_log_root_are_still_pruned(tmp_path):
    """Protecting the worker logs must not turn the whole sweep off."""
    daemon = _daemon(tmp_path, _FakePool(), log_budget_bytes=1024)
    _write(tmp_path / "logs" / "card-0" / "worker.log", 512)
    junk = _write(tmp_path / "logs" / "card-1" / "kernels.yaml", 8192)
    daemon.worker_log_paths = [str(tmp_path / "logs" / "card-0" / "worker.log")]
    daemon._prune_logs()
    assert not junk.exists()


def test_structures_are_pruned_across_every_cards_directory(tmp_path):
    """Four folders, four structure trees. A sweep that only knows about
    card 0's grows the other three forever."""
    daemon = _daemon(tmp_path, _FakePool(), structures_budget_bytes=1024)
    dirs = []
    for card in range(4):
        d = tmp_path / "structures" / f"device-{card}"
        _write(d / "old.cif", 8192)
        dirs.append(d)
    daemon.structures_dirs = [str(d) for d in dirs]
    daemon._prune_structures()
    assert not any((d / "old.cif").exists() for d in dirs)


def test_a_recently_emitted_cif_is_protected_on_every_card(tmp_path):
    """The UI can be more than a second behind the socket in reading a .cif
    (ribbon_from_cif measured 1221ms at 3000 residues). Four cards means
    four such files can be in flight at once."""
    daemon = _daemon(tmp_path, _FakePool(), structures_budget_bytes=1)
    fresh = []
    for card in range(4):
        d = tmp_path / "structures" / f"device-{card}"
        path = _write(d / "fresh.cif", 8192)
        fresh.append(path)
        daemon._emit_and_track(card, {"type": "job_done", "job_id": f"j{card}",
                                      "cif_path": str(path), "wall_s": 4.4,
                                      "mean_plddt": 95.3})
    daemon.structures_dirs = [str(tmp_path / "structures" / f"device-{c}")
                              for c in range(4)]
    daemon._prune_structures()
    assert all(p.exists() for p in fresh)


def test_one_cards_output_does_not_evict_anothers_protected_file(tmp_path):
    """The bug a single shared protected deque of 3 would produce: four
    cards finishing at once evict each other's newest structures."""
    daemon = _daemon(tmp_path, _FakePool(), structures_budget_bytes=1)
    fresh = []
    for card in range(4):
        d = tmp_path / "structures" / f"device-{card}"
        for n in range(3):
            path = _write(d / f"s{n}.cif", 4096)
            daemon._emit_and_track(card, {"type": "job_done",
                                          "job_id": f"j{card}-{n}",
                                          "cif_path": str(path), "wall_s": 4.4,
                                          "mean_plddt": 95.3})
            fresh.append(path)
    daemon.structures_dirs = [str(tmp_path / "structures" / f"device-{c}")
                              for c in range(4)]
    daemon._prune_structures()
    assert all(p.exists() for p in fresh), "each card protects its own three"


def test_a_janitor_failure_never_stops_the_booth(tmp_path, monkeypatch):
    import runner.daemon as mod

    def explode(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(mod, "prune_log_root", explode)
    daemon = _daemon(tmp_path, _FakePool())
    daemon._prune_logs()          # must not raise
    daemon._prune_structures()    # must not raise
```

**Mutations these must catch:** giving all workers one log root (test 1 red); dropping the worker logs from `protect` (test 2 red); deleting instead of truncating an oversized worker log (test 3 red); truncating unconditionally (test 4 red); protecting the whole `card-*` tree instead of the one file (test 5 red); pruning only card 0's structures (test 6 red); a single shared protected deque (test 8 red); removing a janitor's guard (test 9 red).

**Test 8 is the one that cannot be argued from the code.** With `PROTECTED_STRUCTURE_COUNT = 3` and four cards, a single shared deque holds three *total*, so nine of twelve fresh structures lose protection the moment the budget binds. Verify by collapsing the per-card deques into one and watching it go red.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

---

### Task 12: Per-fold state, pure [no device]

**Files:** Create `ui/slots.py`. Modify `ui/states.py`. Test: `tests/unit/test_slots.py`

**Why:** This is the decision layer for the whole UI change, and per the project's own rule the wiring layer makes no decisions. Everything hard about four concurrent folds — which cell an event belongs to, whether this cell's dwell has expired, which cell the booth is following — is answerable here with no GTK and no display.

**Produces:**

- `MAX_SLOTS = 4`
- `PICK_PENDING_WARN_S = 10.0`
- `SlotState(showcase_dwell_s=2.0)` with `.state` in `{"idle", "folding", "showcase"}`, `.job_id`, `.card`, `.on_job_start(event)`, `.on_job_done(event)`, `.on_job_error(event)`, `.on_structure_revealed()`, `.tick(now)`, and the two predicates `.points_are_visible` / `.ribbon_may_be_revealed` as properties.
- `SlotRouter(cards, showcase_dwell_s=2.0)` with `.slots`, `.slot_for_card(card)`, `.slot_for_job(job_id)`, `.on_event(event) -> int | None`, `.tick(now) -> list[int]`, `.select_target(target_id, now=0.0)`, `.release_target()`, `.selected_target`, `.pick_status(now) -> "queued" | "waiting" | "folding" | None`, `.focus_slot`, and `.tracked_jobs` — the job ids the router can still route, exposed publicly **because a test has to be able to assert it is bounded**, and asserting on a private field is the "adjacent to the behaviour" shape `docs/followups.md` names as this project's recurring test defect.

**Reuse:** `ui.states.showcase_ended(previous, current)` is imported and used as-is against `SlotState` values — `SlotState` deliberately spells its showcase state `"showcase"`, the same string `BoothState.SHOWCASE` carries, so the existing tested function works on both. **Do not write a second one.**

**The focus rule**, stated once and tested below: the focus slot is the slot folding (or showcasing) the visitor's selected target if there is one; otherwise the slot that most recently entered `showcase`; otherwise slot 0.

**The pick-status rule**, likewise: `"folding"` once some slot holds the selected target; `"queued"` while it is selected and no slot holds it yet; `"waiting"` once that has been true for `PICK_PENDING_WARN_S`; `None` when there is no pick. It exists because the daemon is allowed to take a few seconds (Task 9 rules out preemption, deliberately) and **a visitor who taps and sees nothing concludes the booth is broken.** This is the pure, testable half of the answer to that; Task 17 puts it on screen. Note that the status is decided here and *not* by asking whether a fold has started — the router already knows both things, and a second source of truth in `ui/app.py` is how the screen and the daemon end up disagreeing.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from ui.slots import MAX_SLOTS, PICK_PENDING_WARN_S, SlotRouter, SlotState
from ui.states import showcase_ended


def _start(job_id="j1", card=0, target_id="trpcage"):
    return {"type": "job_start", "job_id": job_id, "target_id": target_id,
            "model": "protenix-v2", "card": card, "n_residues": 20}


def _done(job_id="j1"):
    return {"type": "job_done", "job_id": job_id, "cif_path": "/a.cif",
            "wall_s": 4.4, "mean_plddt": 95.3}


def _error(job_id="j1"):
    return {"type": "job_error", "job_id": job_id, "target_id": "t",
            "message": "boom"}


# ---- SlotState: one cell's dwell -----------------------------------------

def test_a_slot_starts_idle():
    assert SlotState().state == "idle"


def test_a_job_start_puts_the_slot_into_folding():
    slot = SlotState()
    assert slot.on_job_start(_start()) == "folding"
    assert slot.job_id == "j1"


def test_a_job_done_starts_this_slots_own_showcase():
    slot = SlotState()
    slot.on_job_start(_start())
    assert slot.on_job_done(_done()) == "showcase"


def test_points_are_suppressed_only_while_this_slot_showcases():
    """The whole reason the dwell is per slot: cell 1 is mid-diffusion while
    cell 0 holds a finished structure."""
    slot = SlotState()
    slot.on_job_start(_start())
    assert slot.points_are_visible
    slot.on_job_done(_done())
    assert not slot.points_are_visible


def test_a_ribbon_may_only_be_revealed_while_this_slot_showcases():
    slot = SlotState()
    slot.on_job_start(_start())
    assert not slot.ribbon_may_be_revealed
    slot.on_job_done(_done())
    assert slot.ribbon_may_be_revealed


def test_the_dwell_is_measured_from_the_reveal_not_from_job_done():
    """Unchanged rule, moved: job_done says the daemon finished, not that
    the visitor can see anything. Between them sit the ribbon build (up to
    ~1.2s) and the 0.8s cross-fade."""
    slot = SlotState(showcase_dwell_s=2.0)
    slot.on_job_start(_start())
    slot.on_job_done(_done())
    slot.tick(now=0.0)
    slot.tick(now=1.5)
    slot.on_structure_revealed()
    slot.tick(now=1.5)
    assert slot.tick(now=3.0) == "showcase", "the dwell restarted at the reveal"
    assert slot.tick(now=3.6) == "idle"


def test_a_job_start_for_a_new_fold_does_not_cut_a_dwell_short():
    """The daemon starts the next fold on this chip the instant the last one
    finishes. That ordering is exactly what the dwell exists to survive."""
    slot = SlotState(showcase_dwell_s=2.0)
    slot.on_job_start(_start("j1"))
    slot.on_job_done(_done("j1"))
    slot.tick(now=0.0)
    assert slot.on_job_start(_start("j2")) == "showcase"


def test_the_deferred_job_start_is_applied_when_the_dwell_expires():
    """The clear belongs to job_start; the dwell only delays it."""
    slot = SlotState(showcase_dwell_s=2.0)
    slot.on_job_start(_start("j1"))
    slot.on_job_done(_done("j1"))
    slot.tick(now=0.0)
    slot.on_job_start(_start("j2"))
    assert slot.tick(now=3.0) == "folding"
    assert slot.job_id == "j2"


def test_a_job_error_ends_a_fold_without_a_showcase():
    slot = SlotState()
    slot.on_job_start(_start())
    assert slot.on_job_error(_error()) == "idle"


def test_a_stale_job_error_does_not_disturb_the_current_fold():
    """Events for a job this slot has moved on from must be ignored, not
    applied -- a late job_error for j1 while j2 folds would blank the cell."""
    slot = SlotState()
    slot.on_job_start(_start("j1"))
    slot.on_job_start(_start("j2"))
    assert slot.on_job_error(_error("j1")) == "folding"
    assert slot.job_id == "j2"


def test_showcase_ended_is_the_shared_helper_and_works_on_a_slot():
    slot = SlotState(showcase_dwell_s=1.0)
    slot.on_job_start(_start())
    slot.on_job_done(_done())
    slot.tick(now=0.0)
    previous = slot.state
    assert showcase_ended(previous, slot.tick(now=5.0))


# ---- SlotRouter: which cell does this event belong to ---------------------

def test_one_slot_per_card_in_card_order():
    router = SlotRouter(cards=[0, 1, 2, 3])
    assert len(router.slots) == 4
    assert [router.slot_for_card(c) for c in (0, 1, 2, 3)] == [0, 1, 2, 3]


def test_a_booth_with_fewer_chips_gets_fewer_slots():
    router = SlotRouter(cards=[0, 2])
    assert len(router.slots) == 2
    assert router.slot_for_card(2) == 1
    assert router.slot_for_card(1) is None


def test_more_chips_than_cells_does_not_overflow_the_quad():
    """A five-card machine folds on five and shows four. Better than
    crashing, and better than silently drawing the fifth over the first."""
    router = SlotRouter(cards=[0, 1, 2, 3, 4, 5])
    assert len(router.slots) == MAX_SLOTS
    assert router.slot_for_card(5) is None


def test_a_job_start_binds_its_job_id_to_its_cards_slot():
    router = SlotRouter(cards=[0, 1, 2, 3])
    assert router.on_event(_start("j9", card=2)) == 2
    assert router.slot_for_job("j9") == 2


def test_every_later_event_of_a_job_routes_by_job_id_alone():
    """Only job_start carries `card`. Everything after it carries job_id and
    nothing else -- which is exactly why the UI keys by job_id."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.on_event(_start("j9", card=3))
    assert router.on_event({"type": "stage", "job_id": "j9",
                            "stage": "diffusion", "frac": 0.5}) == 3
    assert router.on_event(_done("j9")) == 3


def test_an_event_for_an_unknown_job_belongs_to_no_slot():
    """A frame that beats its own job_start through the UI's idle queue must
    not be drawn into whichever cell happens to be first."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    assert router.on_event({"type": "frame", "job_id": "ghost", "step": 1,
                            "total": 200, "n_atoms": 20,
                            "coords_b64": "AAAA"}) is None


def test_four_concurrent_folds_stay_in_their_own_cells():
    router = SlotRouter(cards=[0, 1, 2, 3])
    for card in (0, 1, 2, 3):
        router.on_event(_start(f"j{card}", card=card))
    router.on_event(_done("j2"))
    assert [s.state for s in router.slots] == ["folding", "folding",
                                               "showcase", "folding"]


def test_a_cards_second_fold_replaces_the_first_in_the_same_cell():
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.on_event(_start("j1", card=1))
    router.on_event(_start("j2", card=1))
    assert router.slot_for_job("j2") == 1
    assert router.slots[1].job_id == "j2"


def test_the_job_id_map_does_not_grow_without_bound():
    """An all-day booth folds thousands of jobs. A dict that remembers every
    one is a leak with a screen attached."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    for n in range(500):
        router.on_event(_start(f"j{n}", card=n % 4))
    assert len(router.tracked_jobs) <= 4 * MAX_SLOTS


def test_tick_reports_only_the_slots_that_changed():
    router = SlotRouter(cards=[0, 1, 2, 3], showcase_dwell_s=1.0)
    router.on_event(_start("j0", card=0))
    router.on_event(_done("j0"))
    router.slots[0].on_structure_revealed()
    router.tick(now=0.0)
    assert router.tick(now=0.1) == []
    assert router.tick(now=5.0) == [0]


# ---- the focus slot ------------------------------------------------------

def test_with_no_pick_the_focus_follows_the_newest_finished_structure():
    router = SlotRouter(cards=[0, 1, 2, 3])
    for card in (0, 1, 2, 3):
        router.on_event(_start(f"j{card}", card=card))
    router.on_event(_done("j2"))
    assert router.focus_slot == 2


def test_a_visitors_pick_takes_the_focus_when_its_fold_starts():
    """Spec: 'a visitor's pick becomes the hero of the quad while the other
    three chips continue the attract playlist.'"""
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.on_event(_start("j0", card=0, target_id="attract-a"))
    router.select_target("hemoglobin")
    router.on_event(_start("j3", card=3, target_id="hemoglobin"))
    assert router.focus_slot == 3


def test_a_pick_does_not_move_the_focus_before_its_fold_starts():
    """The daemon dispatches the pick to the next chip to free (Task 9), so
    this is usually a wait of seconds -- but it is never zero, and moving
    the focus to a cell folding something else in the meantime would point
    the hero cell at the wrong protein."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.on_event(_start("j0", card=0, target_id="attract-a"))
    router.on_event(_done("j0"))
    router.select_target("hemoglobin")
    assert router.focus_slot == 0


def test_the_picked_focus_survives_other_cells_finishing():
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.select_target("hemoglobin")
    router.on_event(_start("j3", card=3, target_id="hemoglobin"))
    router.on_event(_start("j1", card=1, target_id="attract-b"))
    router.on_event(_done("j1"))
    assert router.focus_slot == 3


def test_the_pick_is_released_when_its_fold_ends():
    """Otherwise the focus stays pinned to a finished cell for the rest of
    the day and the booth stops following the action."""
    router = SlotRouter(cards=[0, 1, 2, 3], showcase_dwell_s=1.0)
    router.select_target("hemoglobin")
    router.on_event(_start("j3", card=3, target_id="hemoglobin"))
    router.on_event(_done("j3"))
    router.slots[3].on_structure_revealed()
    router.tick(now=0.0)
    router.tick(now=5.0)
    assert router.selected_target is None


def test_an_empty_booth_focuses_the_first_cell():
    assert SlotRouter(cards=[0, 1, 2, 3]).focus_slot == 0


# ---- the pick, between the tap and the fold ------------------------------

def test_there_is_no_pick_status_without_a_pick():
    assert SlotRouter(cards=[0, 1, 2, 3]).pick_status(now=0.0) is None


def test_a_pick_is_acknowledgeable_the_instant_it_is_made():
    """Nothing about this may wait on the daemon answering: the socket may
    be down, the daemon may be mid-fold on all four chips, and the visitor
    is standing there either way."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.select_target("hemoglobin", now=0.0)
    assert router.pick_status(now=0.0) == "queued"


def test_a_long_wait_is_named_differently_so_the_booth_can_say_more():
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.select_target("hemoglobin", now=100.0)
    assert router.pick_status(now=100.0 + PICK_PENDING_WARN_S - 0.1) == "queued"
    assert router.pick_status(now=100.0 + PICK_PENDING_WARN_S + 0.1) == "waiting"


def test_the_status_becomes_folding_when_the_picked_target_starts():
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.select_target("hemoglobin", now=0.0)
    router.on_event(_start("j3", card=3, target_id="hemoglobin"))
    assert router.pick_status(now=999.0) == "folding"


def test_a_pick_for_a_target_already_folding_is_folding_at_once():
    """The daemon queues nothing in this case (Task 9). A router that waited
    for a job_start that will never come would leave the booth saying NEXT
    UP forever about something already on screen."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.on_event(_start("j2", card=2, target_id="hemoglobin"))
    router.select_target("hemoglobin", now=0.0)
    assert router.pick_status(now=0.0) == "folding"
    assert router.focus_slot == 2


def test_releasing_the_pick_clears_its_status():
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.select_target("hemoglobin", now=0.0)
    router.release_target()
    assert router.pick_status(now=0.0) is None
```

**Mutations these must catch:** making the dwell global across slots (test 4 red); measuring the dwell from `job_done` (test 6 red); letting `job_start` cut a dwell short (test 7 red); dropping the deferred `job_start` instead of applying it (test 8 red); applying a stale `job_error` (test 10 red); routing by card instead of `job_id` for non-`job_start` events (test 16 red); routing an unknown job to slot 0 (test 17 red); an unbounded job map (test 21 red); reporting every slot from `tick` (test 22 red); a focus that ignores the pick (test 24 red); a focus that follows the pick before its fold starts (test 25 red); never releasing the pick (test 27 red); a `pick_status` that reports `"folding"` only on a `job_start` it witnessed, so a pick for a target already in flight never resolves (the already-folding test red); a warn window applied with `>` where the clock never lands exactly (the long-wait test red).

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

`ui/states.py` changes in this task only by **narrowing its docstring** to say that the showcase dwell now lives in `ui/slots.py` and that `BoothState.SHOWCASE` follows the focus slot. Its own tests must stay green untouched — if any of `tests/unit/test_states.py` needs editing, stop: that means behaviour moved that this plan said would not.

---

### Task 13: A four-fold fixture [no device]

**Files:** Create `tests/fixtures/streams/make_quad_fold.py`, generate `tests/fixtures/streams/quad_fold.jsonl`. Test: `tests/unit/test_quad_fixture.py`

**Why:** Everything from here to Task 17 needs a realistic four-way interleaved stream, and `runner/mock.py` — "the project's core test instrument" — can already replay one to a real UI with no hardware. Building the fixture now is what lets Tasks 14–17 be verified against something that behaves like the daemon rather than against hand-built dicts.

**Produces:** `quad_fold.jsonl` — four folds on cards 0–3, started at staggered offsets, with interleaved `stage` and `frame` events and staggered completions, plus the ordering pathology this project actually measured: **fold N's `job_done` arriving after fold N+1's `job_start` on the same card.**

Follow `make_short_fold.py`'s shape exactly: a generator script committed alongside its output, importing only `protocol.events`, runnable under venv-ui.

- [ ] **Step 1: Write the failing tests**

```python
import collections
import pathlib

from protocol.events import EVENT_TYPES, decode
from runner.mock import load_stream          # stdlib+numpy only; safe in venv-ui

FIXTURE = pathlib.Path("tests/fixtures/streams/quad_fold.jsonl")


def _events():
    return load_stream(FIXTURE)


def test_every_line_decodes_as_a_protocol_event():
    for event in _events():
        assert event["type"] in EVENT_TYPES


def test_all_four_cards_fold():
    cards = {e["card"] for e in _events() if e["type"] == "job_start"}
    assert cards == {0, 1, 2, 3}


def test_more_than_one_fold_is_in_flight_at_once():
    """A fixture that serialises the four folds proves nothing about the
    thing this phase changes."""
    open_jobs, concurrent = set(), 0
    for event in _events():
        if event["type"] == "job_start":
            open_jobs.add(event["job_id"])
        elif event["type"] in ("job_done", "job_error"):
            open_jobs.discard(event["job_id"])
        concurrent = max(concurrent, len(open_jobs))
    assert concurrent >= 3


def test_frames_from_different_jobs_interleave():
    """Consecutive frames belonging to different jobs -- the exact case a
    single global LatestFrame buffer gets wrong."""
    frames = [e["job_id"] for e in _events() if e["type"] == "frame"]
    assert any(a != b for a, b in zip(frames, frames[1:]))


def test_every_job_id_is_unique_across_cards():
    starts = [e["job_id"] for e in _events() if e["type"] == "job_start"]
    assert len(starts) == len(set(starts))


def test_every_started_job_also_ends():
    """A job with no ending strands its cell in `folding` forever, which is
    a UI bug the fixture must be able to expose rather than cause."""
    started = {e["job_id"] for e in _events() if e["type"] == "job_start"}
    ended = {e["job_id"] for e in _events()
             if e["type"] in ("job_done", "job_error")}
    assert started == ended


def test_a_cards_next_job_starts_before_the_previous_ones_ribbon_lands():
    """The measured ordering this whole UI is arranged around:
    job_done(N) ... job_start(N+1) on the SAME card. Reproduced here so the
    per-slot deferred clear is exercised, not just described."""
    by_card = collections.defaultdict(list)
    order = {}
    for index, event in enumerate(_events()):
        if event["type"] == "job_start":
            by_card[event["card"]].append(event["job_id"])
            order[("start", event["job_id"])] = index
        elif event["type"] == "job_done":
            order[("done", event["job_id"])] = index
    overlaps = [(jobs[0], jobs[1]) for jobs in by_card.values() if len(jobs) > 1
                and order.get(("done", jobs[0]), 0) > order[("start", jobs[1])]]
    assert overlaps, "no card starts its second fold before the first finishes"


def test_at_least_one_fold_fails():
    """The booth's failure path is not exotic; it must be in the fixture or
    it is only ever tested by hand-built dicts."""
    assert any(e["type"] == "job_error" for e in _events())


def test_coordinates_are_decodable():
    from protocol.events import unpack_coords
    for event in _events():
        if event["type"] == "frame":
            assert unpack_coords(event["coords_b64"]).shape[1] == 3


def test_the_fixture_matches_what_the_generator_produces(tmp_path):
    """A fixture that has drifted from its generator is a fixture nobody can
    regenerate. make_short_fold.py has the same relationship and no test
    holding it."""
    import subprocess
    import sys
    out = tmp_path / "quad_fold.jsonl"
    subprocess.run([sys.executable, "tests/fixtures/streams/make_quad_fold.py",
                    "--out", str(out)], check=True)
    assert out.read_text() == FIXTURE.read_text()
```

**Mutations these must catch:** generating four serial folds (test 3 red); giving every job the same id (test 5 red); dropping a job's ending (test 6 red); ordering each card's `job_done` before its next `job_start` (test 7 red); an all-success fixture (test 8 red); editing the fixture without regenerating it (test 10 red).

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

Test 10 needs `make_quad_fold.py` to be deterministic (a seeded `default_rng`, exactly as `make_short_fold.py` does) and to take `--out`. Give `make_short_fold.py` the same `--out` treatment while you are there only if it costs nothing; do not turn this into a refactor.

`quad_fold.jsonl`'s `hello` line must interpolate `PROTOCOL_VERSION` rather than a literal `2`, exactly as `make_short_fold.py` already does: Task 3's fixture ratchet globs every `.jsonl` under `tests/fixtures/streams/` and a hardcoded version fails it at the next bump — which is the failure this whole amendment just had to clean up once.

---

### Task 14: The quad view [no device]

**Files:** Create `ui/quad.py`. Test: `tests/unit/test_quad.py`

**Why:** `ui/viewer.py` renders one structure and must keep doing so — everything it learned about camera ownership, blend targets and per-job reset is per-cell machinery already, and reworking it into a multi-viewport renderer would put four folds' worth of state back into one object, which is the defect this phase exists to remove. So the quad is four `StructureViewer`s in a grid, and `ui/viewer.py` is not touched.

**Produces:**

- `grid_position(slot) -> (column, row)` — pure: `0→(0,0)`, `1→(1,0)`, `2→(0,1)`, `3→(1,1)`.
- `QuadView(Gtk.Grid)` built from a card list, with `.viewers`, `.viewer_for_slot(slot)`, `.set_caption(slot, text)`, `.set_focus(slot | None)`, `.set_connection_state(state)`, `.set_notice(text | None)`, `.notice_text()`, `.slot_count`.
- The notice is one line spanning the whole quad, not a fifth caption: it is what the booth says between a visitor's tap and the fold that answers it (Task 17), and it belongs to no cell because at that moment no cell is folding the thing it names.
- Its own `_QUAD_CSS` and `_BACKGROUND_BY_CLASS`, wired into `tests/unit/_legibility.py`'s shared guard.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from _legibility import assert_every_label_is_legible
from ui.panels import MIN_CONTRAST_RATIO, contrast_ratio
from ui.quad import QuadView, grid_position
import ui.quad as quadmod


def test_the_four_cells_are_a_two_by_two_grid():
    assert [grid_position(s) for s in range(4)] == [(0, 0), (1, 0), (0, 1), (1, 1)]


def test_reading_order_is_left_to_right_then_down():
    """Cell order must match the telemetry panel's own left-to-right chip
    order, or 'chip 2' on screen means two different chips in two panels."""
    assert grid_position(1)[1] == grid_position(0)[1]
    assert grid_position(2)[1] > grid_position(0)[1]


def test_one_viewer_per_card():
    quad = QuadView(cards=[0, 1, 2, 3])
    assert quad.slot_count == 4
    assert len({id(v) for v in quad.viewers}) == 4


def test_a_two_card_booth_builds_two_cells():
    quad = QuadView(cards=[0, 1])
    assert quad.slot_count == 2


def test_a_six_card_booth_builds_only_four():
    quad = QuadView(cards=[0, 1, 2, 3, 4, 5])
    assert quad.slot_count == 4


def test_each_cell_names_its_own_chip():
    """The claim the Tensix panel had to be walked back for. Now it is true,
    and each cell says which chip it is."""
    quad = QuadView(cards=[0, 1, 2, 3])
    labels = [quad.chip_label_text(s) for s in range(4)]
    assert labels == ["CHIP 0", "CHIP 1", "CHIP 2", "CHIP 3"]


def test_a_sparse_card_list_labels_the_real_chip_numbers():
    quad = QuadView(cards=[1, 3])
    assert [quad.chip_label_text(s) for s in range(2)] == ["CHIP 1", "CHIP 3"]


def test_the_focus_cell_is_marked_and_only_one_is():
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_focus(2)
    focused = [s for s in range(4) if quad.has_focus_marking(s)]
    assert focused == [2]
    quad.set_focus(0)
    assert [s for s in range(4) if quad.has_focus_marking(s)] == [0]


def test_no_focus_marks_nothing():
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_focus(2)
    quad.set_focus(None)
    assert not any(quad.has_focus_marking(s) for s in range(4))


def test_an_out_of_range_focus_does_not_raise():
    """Wire-shaped data reaches this via a card index."""
    quad = QuadView(cards=[0, 1])
    quad.set_focus(9)
    assert not any(quad.has_focus_marking(s) for s in range(2))


def test_a_caption_reaches_only_its_own_cell():
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_caption(1, "DIFFUSION 62%")
    assert quad.caption_text(1) == "DIFFUSION 62%"
    assert quad.caption_text(0) != "DIFFUSION 62%"


def test_an_out_of_range_caption_does_not_raise():
    quad = QuadView(cards=[0, 1])
    quad.set_caption(7, "nonsense")


def test_the_notice_belongs_to_the_quad_and_not_to_a_cell():
    """It names a protein no cell is folding yet. Rendering it into cell 0's
    caption would label whatever cell 0 IS folding with the wrong name."""
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_notice("HEMOGLOBIN — NEXT UP")
    assert quad.notice_text() == "HEMOGLOBIN — NEXT UP"
    assert all(quad.caption_text(s) != "HEMOGLOBIN — NEXT UP" for s in range(4))


def test_a_cleared_notice_leaves_nothing_behind():
    """It is cleared the moment the picked fold starts. A banner still
    saying NEXT UP over the fold it announced is the booth talking over
    itself."""
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_notice("HEMOGLOBIN — NEXT UP")
    quad.set_notice(None)
    assert not quad.notice_text()


def test_the_connection_state_reaches_every_viewer():
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_connection_state("connected")
    assert all(v.connection_state == "connected" for v in quad.viewers)


def test_an_unknown_connection_state_does_not_raise_out_of_the_quad():
    """StructureViewer's setter deliberately raises on an unknown state.
    That validator must not be able to brick the channel for four cells at
    once -- the guard ui/app.py already carries, applied here."""
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_connection_state("teleporting")


def test_every_label_in_the_quad_is_legible():
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_caption(0, "DIFFUSION 62%")
    quad.set_notice("HEMOGLOBIN — NEXT UP")
    assert_every_label_is_legible(
        quad, context="ui.quad", min_contrast=MIN_CONTRAST_RATIO,
        contrast_ratio_fn=contrast_ratio,
        css_text_fn=lambda: quadmod._QUAD_CSS,
        background_by_class_fn=lambda: quadmod._BACKGROUND_BY_CLASS)
```

**Mutations these must catch:** row-major transposed to column-major (test 1 red); building a fixed four cells regardless of the card list (tests 4, 5 red); labelling cells by slot index instead of card number (test 7 red); leaving the previous focus marking in place (test 8 red); no bounds check on focus or caption (tests 10, 12 red); setting the connection state on one viewer (test 13 red); dropping the guard around the setter (test 14 red); rendering the notice into a cell's caption (the notice test red); leaving a cleared notice on screen (the cleared-notice test red); building a caption or notice label with no colour-bearing class (the legibility test red).

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

Then **look at it**: run the app windowed against `quad_fold.jsonl` via `runner/mock.py` and take a screenshot (`spectacle -b -n -f -o /tmp/quad.png` — `grim` does not work on this KWin box). Four GL contexts in one window is the one thing here that no unit test can tell you about; confirm all four cells actually render before moving on, and put the screenshot in the task report.

---

### Task 15: Wire the quad into the app [no device]

**Files:** Modify `ui/app.py`. Test: `tests/unit/test_app_quad.py`

**Why:** The largest single piece of work in this phase, and the one the per-fold/global table exists to keep honest. `ui/app.py` should get **smaller in responsibility** as it grows in lines: the routing decisions are in `ui/slots.py`, the layout is in `ui/quad.py`, and this file only carries them out.

**Produces:**

- `DemoApp` gains `self.quad`, `self.router` (a `SlotRouter`), and **`attach_cards(cards)`** — build the router and size the quad from a card list. `_handle_event` calls it on `hello`; `do_activate` calls it with a single-card default so a booth with no socket still renders.
- `ui/client.py` gains **`LatestFrameByJob(max_jobs=8)`** — the same latest-wins contract as `LatestFrame`, one slot per `job_id`, oldest job evicted past `max_jobs`, with `__len__` and `.take_all() -> dict[job_id, event]`. It lives beside `LatestFrame` because that is where the one-slot buffer and its "diffusion frames are advisory" argument already live. `self._frames` becomes one of these.
- `_ribbon_generation` / `_pending_ribbon` / `_deferred_clear` all become **per-slot**, per the table above.
- **`self.viewer` is removed, not aliased** — an alias is a place for four folds to quietly become one again. `tests/unit/test_ribbon_async.py`, `test_app_handle_event.py` and `test_app_not_ready.py` are updated with it.
- `_tick_state_at(now)` and `_join_ribbon_workers(timeout)` are the two new test seams (see Step 2).
- `_FakeViewer`, `_FakeQuad`, `_app`, `_start`, `_done` and `_frame` are extracted into **`tests/unit/_appfakes.py`** as part of this task — Task 17's tests import them from there. The leading underscore keeps pytest from collecting it, the same convention `tests/unit/_legibility.py` already uses, and one copy of the fake quad is one place for it to drift from the real one.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from ui.app import DemoApp


class _FakeViewer:
    def __init__(self):
        self.points = 0
        self.ribbons = 0
        self.cleared = 0
        self.crossfades = 0
        self.connection_state = "disconnected"

    def set_points(self, coords, opacity=1.0):
        self.points += 1

    def set_ribbon(self, *a):
        self.ribbons += 1

    def begin_crossfade(self):
        self.crossfades += 1

    def clear_structure(self):
        self.cleared += 1


class _FakeQuad:
    def __init__(self, n=4):
        self.viewers = [_FakeViewer() for _ in range(n)]
        self.slot_count = n
        self.captions = {}
        self.focus = None
        self.notice = None

    def viewer_for_slot(self, slot):
        return self.viewers[slot]

    def set_caption(self, slot, text):
        self.captions[slot] = text

    def set_focus(self, slot):
        self.focus = slot

    def set_notice(self, text):
        self.notice = text

    def set_connection_state(self, state):
        for v in self.viewers:
            v.connection_state = state


def _app(cards=(0, 1, 2, 3)):
    app = DemoApp(socket_path=None)
    app.quad = _FakeQuad(len(cards))
    app.attach_cards(list(cards))          # builds self.router against `cards`
    return app


def _start(job_id, card, target_id="t"):
    return {"type": "job_start", "job_id": job_id, "target_id": target_id,
            "model": "protenix-v2", "card": card, "n_residues": 20}


def _done(job_id):
    return {"type": "job_done", "job_id": job_id, "cif_path": f"/{job_id}.cif",
            "wall_s": 4.4, "mean_plddt": 95.3}


def _frame(job_id):
    from protocol.events import pack_coords
    import numpy as np
    return {"type": "frame", "job_id": job_id, "step": 3, "total": 200,
            "n_atoms": 4, "coords_b64": pack_coords(np.zeros((4, 3)))}


def test_the_card_list_comes_from_hello():
    """A booth on two chips must build two cells, not four empty ones."""
    app = DemoApp(socket_path=None)
    app.quad = _FakeQuad(4)
    app._handle_event({"type": "hello", "version": 1, "cards": [0, 2],
                       "models": ["protenix-v2"], "preflight": "ok"})
    assert app.router.slot_for_card(2) == 1


def test_a_job_start_clears_only_its_own_cell():
    """The defect a global clear produces: chip 3 starting a fold blanks the
    finished structure a visitor is looking at on chip 0."""
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_start("j3", card=3))
    assert app.quad.viewers[0].cleared == 1
    assert app.quad.viewers[3].cleared == 1
    assert app.quad.viewers[1].cleared == 0


def test_a_frame_reaches_only_its_own_cell():
    app = _app()
    app._handle_event(_start("j2", card=2))
    app._on_event(_frame("j2"))
    app._drain_frames()
    assert app.quad.viewers[2].points == 1
    assert sum(v.points for v in app.quad.viewers) == 1


def test_four_frame_streams_do_not_fight_over_one_buffer():
    """The single global LatestFrame's failure mode: every cell shows
    whichever fold happened to be fastest."""
    app = _app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    for card in range(4):
        app._on_event(_frame(f"j{card}"))
    app._drain_frames()
    assert [v.points for v in app.quad.viewers] == [1, 1, 1, 1]


def test_a_frame_for_an_unknown_job_is_dropped_without_disturbing_a_cell():
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._on_event(_frame("ghost"))
    app._drain_frames()
    assert sum(v.points for v in app.quad.viewers) == 0


def test_the_frame_buffer_does_not_grow_with_every_job():
    app = _app()
    for n in range(200):
        app._on_event(_frame(f"ghost{n}"))
    assert len(app._frames) <= 8


def test_a_showcasing_cell_suppresses_its_own_frames_only():
    """Cell 0 holds a finished structure while cell 1 keeps condensing."""
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_start("j1", card=1))
    app._handle_event(_done("j0"))
    app._on_event(_frame("j0"))
    app._on_event(_frame("j1"))
    app._drain_frames()
    assert app.quad.viewers[0].points == 0
    assert app.quad.viewers[1].points == 1


def test_a_suppressed_frame_is_not_discarded():
    """It stays in the latest-wins buffer so the cell cuts straight to live
    diffusion the instant the dwell expires -- unchanged rule, per cell."""
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_done("j0"))
    app._on_event(_frame("j0"))
    app._drain_frames()
    assert app.quad.viewers[0].points == 0
    app.router.slots[0].on_structure_revealed()
    app._tick_state_at(0.0)
    app._tick_state_at(99.0)              # the dwell expires
    assert app.quad.viewers[0].points == 1


def test_a_deferred_clear_applies_to_its_own_cell_when_the_dwell_expires():
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_done("j0"))
    app.router.slots[0].on_structure_revealed()
    app._tick_state_at(0.0)
    app._handle_event(_start("j0b", card=0))
    before = app.quad.viewers[0].cleared
    app._tick_state_at(99.0)
    assert app.quad.viewers[0].cleared == before + 1


def test_a_deferred_clear_never_touches_another_cell():
    app = _app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    app._handle_event(_done("j0"))
    app.router.slots[0].on_structure_revealed()
    app._tick_state_at(0.0)
    app._handle_event(_start("j0b", card=0))
    before = [v.cleared for v in app.quad.viewers]
    app._tick_state_at(99.0)
    after = [v.cleared for v in app.quad.viewers]
    assert after[1:] == before[1:]


def test_a_ribbon_lands_in_its_own_cell(monkeypatch):
    import ui.app as mod
    monkeypatch.setattr(mod, "ribbon_from_cif",
                        lambda path, **kw: ("v", "n", "c", "i"))
    app = _app()
    app._handle_event(_start("j2", card=2))
    app._handle_event(_done("j2"))
    app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()
    assert app.quad.viewers[2].ribbons == 1
    assert sum(v.ribbons for v in app.quad.viewers) == 1


def test_a_fold_on_one_chip_does_not_invalidate_another_chips_ribbon(monkeypatch):
    """THE per-slot generation-counter test. With one global counter, a
    job_done on chip 3 bumps the generation and chip 0's in-flight ribbon is
    dropped as 'stale' -- silently, every cycle, forever."""
    import ui.app as mod
    monkeypatch.setattr(mod, "ribbon_from_cif",
                        lambda path, **kw: ("v", "n", "c", "i"))
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_start("j3", card=3))
    app._handle_event(_done("j0"))
    app._handle_event(_done("j3"))
    app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()
    assert app.quad.viewers[0].ribbons == 1
    assert app.quad.viewers[3].ribbons == 1


def test_a_cells_newer_fold_still_supersedes_its_own_older_one(monkeypatch):
    """The per-slot counter must keep doing what the global one did WITHIN a
    cell -- only the newest ribbon for that cell lands."""
    import ui.app as mod
    monkeypatch.setattr(mod, "ribbon_from_cif",
                        lambda path, **kw: ("v", "n", "c", "i"))
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_done("j0"))
    app._handle_event(_start("j0b", card=0))
    app._handle_event(_done("j0b"))
    app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()
    assert app.quad.viewers[0].ribbons <= 1


def test_a_ribbon_that_outlasts_its_own_cells_dwell_is_dropped(monkeypatch):
    """Unchanged rule, per cell: cross-fading a finished structure over the
    next fold's live diffusion is the headline defect arriving late."""
    import ui.app as mod
    monkeypatch.setattr(mod, "ribbon_from_cif",
                        lambda path, **kw: ("v", "n", "c", "i"))
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_done("j0"))
    app._join_ribbon_workers(timeout=5.0)
    app._tick_state_at(0.0)
    app._tick_state_at(99.0)              # this cell's dwell expires first
    app._drain_pending_ribbon()
    assert app.quad.viewers[0].ribbons == 0


def test_a_geometry_failure_in_one_cell_leaves_the_other_three_alone(monkeypatch):
    import ui.app as mod
    from ui.geometry import GeometryError

    def explode(path, **kw):
        if "j1" in path:
            raise GeometryError("bad cif")
        return ("v", "n", "c", "i")

    monkeypatch.setattr(mod, "ribbon_from_cif", explode)
    app = _app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    for card in range(4):
        app._handle_event(_done(f"j{card}"))
    app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()
    assert app.quad.viewers[1].ribbons == 0
    assert app.quad.viewers[1].cleared == 1, "only its own job_start cleared it"
    assert sum(v.ribbons for v in app.quad.viewers) == 3


def test_the_focus_cell_is_marked_on_screen():
    app = _app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    app._handle_event(_done("j2"))
    assert app.quad.focus == 2


def test_only_the_focus_cells_stages_drive_the_pipeline_panel():
    """One panel, one bar. Two jobs feeding it makes it run backwards."""
    class _Panel:
        def __init__(self):
            self.calls = []

        def set_stage_from_wire(self, stage, frac):
            self.calls.append((stage, frac))

        def reset(self):
            self.calls.append(("reset", 0.0))

        def tick(self):
            pass

    app = _app()
    app.pipeline_panel = _Panel()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_start("j1", card=1))
    app._handle_event(_done("j0"))          # focus becomes slot 0
    app.pipeline_panel.calls.clear()
    app._handle_event({"type": "stage", "job_id": "j1", "stage": "diffusion",
                       "frac": 0.5})
    assert app.pipeline_panel.calls == []
    app._handle_event({"type": "stage", "job_id": "j0", "stage": "diffusion",
                       "frac": 0.6})
    # The WIRE fraction, unconverted: set_stage_from_wire is the one place
    # the whole-fold -> within-stage conversion happens, which is exactly
    # why this call site uses it and never set_stage.
    assert app.pipeline_panel.calls == [("diffusion", pytest.approx(0.6))]


def test_every_cell_gets_its_own_stage_caption():
    app = _app()
    app._handle_event(_start("j1", card=1))
    app._handle_event({"type": "stage", "job_id": "j1", "stage": "diffusion",
                       "frac": 0.55})
    assert "DIFFUSION" in app.quad.captions[1].upper()
    assert 0 not in app.quad.captions


def test_a_malformed_event_still_never_reaches_the_screen_as_text():
    app = _app()
    app._handle_event({"type": "job_error", "job_id": "j0",
                       "target_id": "t", "message": "/secret/path exploded"})
    assert all("/secret/path" not in (t or "")
               for t in app.quad.captions.values())


def test_a_panel_failure_does_not_freeze_the_state_tick():
    class _Exploding:
        def set_stage_from_wire(self, *a):
            raise RuntimeError("boom")

        def reset(self):
            raise RuntimeError("boom")

        def tick(self):
            raise RuntimeError("boom")

    app = _app()
    app.pipeline_panel = _Exploding()
    assert app._tick_state() is True
    app._handle_event(_start("j0", card=0))       # must not raise
```

**Mutations these must catch:** ignoring `hello`'s card list (test 1 red); clearing every viewer on `job_start` (test 2 red); a single global frame buffer (tests 3, 4 red); routing an unknown job to slot 0 (test 5 red); an unbounded frame buffer (test 6 red); reading `points_are_visible` off the booth state instead of the slot (test 7 red); taking-and-discarding a suppressed frame (test 8 red); a global deferred clear (test 10 red); a single global ribbon generation counter (test 12 red); removing the per-slot generation check entirely (test 13 red); ignoring the per-cell dwell when revealing (test 14 red); letting one cell's geometry failure clear or block the others (test 15 red); feeding the pipeline panel from every job (test 18 red); a caption that interpolates `message` (test 20 red); removing a GLib callback's guard (test 21 red).

**Test 12 is the single most important test in this phase's UI half.** Verify it by collapsing the per-slot generations back to one integer and watching it go red. If it stays green, the test is wrong, not the code.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

`_tick_state_at(now)` is a new test seam: what `_tick_state` does, with the clock supplied. Keep `_tick_state` as the GLib-facing wrapper with its guard and its `return True` outside the `try`. `_join_ribbon_workers(timeout)` replaces Task-2-era `_join_ribbon_worker`; update `tests/unit/test_ribbon_async.py` with it rather than keeping both names.

---

### Task 16: Four chips, honestly [no device]

**Files:** Modify `ui/chipviz.py`, `ui/app.py`, `ui/diagnostics.py`. Test: `tests/unit/test_chipviz_multichip.py`, extend `tests/unit/test_app_interaction.py`

**Why:** The spec's second motivation, in its own words: "The Tensix panel had to be walked back to stay honest. It once claimed 'four chips and they are all working'; that was a Critical finding … the panel is now truthful *because* it says less. Making four chips genuinely work is what earns the claim back." Earning it back means the panel and the help copy have to change **in the same commit** as the behaviour, or the booth ships a new lie in the opposite direction.

**Produces:**

- `ChipVizPanel.set_chip_stages(mapping)` — `{card: stage_or_None}` — replacing `set_folding_chip(index)`. `None` for a card means idle.
- `_mode_for_chip` derives each canvas's mode from that card's own stage.
- `_title_text` says what is true of the set: `"TENSIX ACTIVITY · 3 CHIPS FOLDING"`, `"TENSIX ACTIVITY · CHIP 2 · DENOISING"` when exactly one is working, `"TENSIX ACTIVITY · IDLE"` when none is.
- `_HELP_PANELS`' third paragraph and `ui/diagnostics.py`'s teaching copy are rewritten to match.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from ui.chipviz import ChipVizPanel, viz_mode


def _panel(monkeypatch, chips=4):
    monkeypatch.setattr("ui.chipviz.chip_count", lambda: chips)
    panel = ChipVizPanel()
    panel.available = True
    return panel


def test_two_chips_folding_animate_and_two_do_not(monkeypatch):
    panel = _panel(monkeypatch)
    panel.set_chip_stages({0: "diffusion", 2: "trunk", 1: None, 3: None})
    modes = [panel._mode_for_chip(i) for i in range(4)]
    assert modes[0] != "idle" and modes[2] != "idle"
    assert modes[1] == "idle" and modes[3] == "idle"


def test_each_chip_animates_its_own_stage(monkeypatch):
    """A shared mode across four chips is the same untruth as before, just
    four times over."""
    panel = _panel(monkeypatch)
    panel.set_chip_stages({0: "diffusion", 1: "trunk"})
    assert panel._mode_for_chip(0) == viz_mode("folding", "diffusion")
    assert panel._mode_for_chip(1) == viz_mode("folding", "trunk")
    assert panel._mode_for_chip(0) != panel._mode_for_chip(1)


def test_all_four_folding_says_so(monkeypatch):
    panel = _panel(monkeypatch)
    panel.set_chip_stages({c: "diffusion" for c in range(4)})
    assert "4 CHIPS" in panel._title_text().upper()


def test_one_chip_folding_still_names_that_chip(monkeypatch):
    """The Critical-3 fix must survive: with one chip working, the header
    says which."""
    panel = _panel(monkeypatch)
    panel.set_chip_stages({2: "diffusion"})
    title = panel._title_text().upper()
    assert "CHIP 2" in title
    assert "4 CHIPS" not in title


def test_no_chip_folding_claims_nothing(monkeypatch):
    panel = _panel(monkeypatch)
    panel.set_chip_stages({})
    assert "IDLE" in panel._title_text().upper()
    assert all(panel._mode_for_chip(i) == "idle" for i in range(4))


def test_a_stale_stage_stops_claiming_work(monkeypatch):
    """Unchanged rule: a dead daemon must not leave 'denoising' animating in
    front of a visitor. Now it must stop for each chip independently."""
    now = [0.0]
    monkeypatch.setattr("ui.chipviz.chip_count", lambda: 4)
    panel = ChipVizPanel(clock=lambda: now[0])
    panel.available = True
    panel.set_chip_stages({0: "diffusion"})
    now[0] = 1000.0
    panel.tick_staleness()
    assert panel._mode_for_chip(0) == "idle"


def test_a_card_index_outside_the_drawn_canvases_claims_no_canvas(monkeypatch):
    panel = _panel(monkeypatch, chips=2)
    panel.set_chip_stages({7: "diffusion"})
    assert all(panel._mode_for_chip(i) == "idle" for i in range(2))


def test_wire_shaped_junk_costs_the_attribution_not_an_exception(monkeypatch):
    panel = _panel(monkeypatch)
    panel.set_chip_stages({"two": "diffusion", 1: 17})
    panel._title_text()


def test_an_unavailable_panel_ignores_everything(monkeypatch):
    panel = _panel(monkeypatch)
    panel.available = False
    panel.set_chip_stages({0: "diffusion"})     # must not raise
```

And in `tests/unit/test_app_interaction.py`:

```python
def test_the_help_card_no_longer_says_the_fold_runs_on_one_chip():
    """The copy was true when one chip folded. Shipping the behaviour change
    without the copy change ships a new lie in the other direction."""
    from ui.app import _HELP_PANELS
    text = " ".join(_HELP_PANELS).lower()
    assert "runs on one chip" not in text
    assert "the others sit idle" not in text


def test_the_help_card_describes_what_the_quad_actually_shows():
    from ui.app import _HELP_PANELS
    text = " ".join(_HELP_PANELS).lower()
    assert "four" in text and ("at once" in text or "at the same time" in text)


def test_the_help_intro_no_longer_says_one_after_another():
    """It reads, verbatim today: 'The booth works through its proteins one
    after another, all day.' That was true; it is not any more."""
    from ui.app import _HELP_INTRO
    assert "one after another" not in " ".join(_HELP_INTRO).lower()


def test_the_help_intro_still_discloses_that_a_pick_starts_nothing():
    """Deliberately temporary, and deliberately still here. The daemon can
    be picked from as of Task 9, but ui/app.py does not send a pick until
    Task 17 -- so as of THIS commit a tap still starts nothing and the copy
    saying so is still true. Task 17 replaces this test with its inverse, in
    the same commit as the behaviour. Do not delete it early, and do not let
    Task 17 leave both."""
    from ui.app import _HELP_INTRO
    text = " ".join(_HELP_INTRO).lower()
    assert "isn't wired up" in text or "is not wired up" in text


def test_the_diagnostics_teaching_copy_matches_the_help_card():
    """Two places describing the same panel drifted apart once already."""
    from ui.diagnostics import STAGE_TEACHING
    joined = " ".join(str(v) for v in STAGE_TEACHING.values()).lower()
    assert "one chip" not in joined
```

**Mutations these must catch:** one shared mode for every chip (tests 1, 2 red); a header that always claims four (test 4 red); a header that never names a single chip (test 4 red); claiming work with no chips folding (test 5 red); dropping the staleness check (test 6 red); clamping an out-of-range card onto chip 0 (test 7 red); letting wire junk raise (test 8 red); reverting the help copy (help tests red).

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

`test_every_key_the_booth_answers_to_is_listed_in_the_help` must stay green — no keys change in this phase. `tick_staleness()` is the renamed, explicitly-callable form of the staleness check `set_mode` used to fold in; `ui/app.py` calls it from `_tick_state`.

---

### Task 17: The pick, end to end [no device]

**Files:** Modify `ui/app.py`, `ui/gallery.py`. Test: `tests/unit/test_app_pick.py`; extend `tests/unit/test_app_interaction.py`

**Why:** Everything needed for a visitor's pick to become the hero of the quad now exists — the message (Task 3), the two directions of the socket (Tasks 4, 5), the daemon's dispatch (Task 9), the focus rule and the pick status (Task 12). This task connects the tap to it and, **in the same commit**, changes every visitor-facing string that currently says a pick starts nothing. Shipping the behaviour without the copy would leave the booth telling visitors it cannot do the thing it just did — the mirror image of the Critical 2 finding, and no more honest for being generous.

**Produces:**

- `_on_pick(target_id)` additionally: `self.router.select_target(target_id, now=…)`, a guarded `self._client.send_pick(target_id)`, and a refreshed on-screen notice. The existing `_note_input` / `_note_diagnostics` / `states.on_pick` / `_sync_to_state` calls stay exactly as they are.
- `_sync_quad_notice(now)` — a one-line acknowledgement across the quad, driven entirely by `router.pick_status(now)` (Task 12): `"queued"` → the pick is named and told it starts on the next free chip; `"waiting"` (past `PICK_PENDING_WARN_S`) → the same, plus that the booth is finishing the folds already running; `"folding"` or `None` → cleared. Called from `_on_pick` and from `_tick_state_at`.
- The `incompatible` connection state shows the same neutral overlay the booth already shows for `not_ready`. Task 3's ruling, landed here so it arrives with the copy: the version numbers go to the log and the diagnostics rail, never to the glass.
- `ui/gallery.py`: `_CAPTION_BODY`, `_CARD_HINT` and the module docstring. That docstring currently ends "When the protocol grows a client→server message, this is the copy to change back — not before." This is that moment.
- `ui/app.py`: `_HELP_INTRO`'s fourth paragraph — the disclosure — rewritten.

**Copy rules, which the tests enforce:**

- Say what the booth does: a tap puts that protein next. Do **not** promise "instantly" or "now" — with four chips busy it starts when one frees, usually within seconds, and a claim of instant is a claim the booth breaks in front of the person most likely to notice.
- Say that it does not interrupt: the folds already running finish. That is a nicer fact than it sounds — it is why the other three cells keep moving — and stating it is what makes a few seconds of waiting read as deliberate rather than broken.
- No error text, no version numbers, no paths, ever.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_app_pick.py`, using the helpers extracted in Task 15:

```python
import pytest

from _appfakes import _app, _done, _frame, _start        # extracted in Task 15
from ui.slots import PICK_PENDING_WARN_S


class _RecordingClient:
    def __init__(self, ok=True):
        self.picks = []
        self.ok = ok
        self.state = "connected"

    def send_pick(self, target_id):
        self.picks.append(target_id)
        return self.ok


def _picking_app(cards=(0, 1, 2, 3), client=None):
    app = _app(cards)
    app._client = client if client is not None else _RecordingClient()
    return app


def test_a_pick_asks_the_daemon_to_fold_it():
    """The decision this whole amendment exists for. Without this line the
    pick is a nomination again and the booth's copy becomes a lie."""
    app = _picking_app()
    app._on_pick("hemoglobin")
    assert app._client.picks == ["hemoglobin"]


def test_a_pick_with_no_daemon_at_all_does_not_raise():
    """DemoApp(socket_path=None) is how the whole UI suite runs, and how the
    booth comes up before the daemon does. _on_pick runs in a GLib callback:
    an AttributeError here freezes the source and the booth stops answering
    taps for the rest of the day."""
    app = _app()
    app._client = None
    app._on_pick("hemoglobin")


def test_a_failing_send_never_reaches_the_screen():
    class _Exploding:
        state = "connected"

        def send_pick(self, target_id):
            raise OSError("/run/tt-bio-demo/daemon.sock: connection refused")

    app = _picking_app(client=_Exploding())
    app._on_pick("hemoglobin")                      # must not raise
    assert all("/run/" not in (t or "") for t in app.quad.captions.values())
    assert "/run/" not in (app.quad.notice or "")


def test_nothing_is_sent_to_a_daemon_the_ui_has_refused():
    app = _picking_app(client=_RecordingClient())
    app._client.state = "incompatible"
    app._on_state("incompatible")
    app._on_pick("hemoglobin")
    assert app._client.picks == []


def test_the_booth_acknowledges_the_pick_at_tap_time():
    """The failure this designs against: a visitor taps, four chips are
    busy, and for twenty seconds the screen says nothing. The
    acknowledgement must not wait on any daemon answering."""
    app = _picking_app()
    app._on_pick("hemoglobin")
    assert app.quad.notice
    assert "HEMOGLOBIN" in app.quad.notice.upper()


def test_the_acknowledgement_says_more_when_the_wait_runs_long():
    app = _picking_app()
    app._tick_state_at(0.0)
    app._on_pick("hemoglobin")
    early = app.quad.notice
    app._tick_state_at(PICK_PENDING_WARN_S + 1.0)
    assert app.quad.notice != early
    assert app.quad.notice


def test_the_acknowledgement_never_becomes_an_error():
    app = _picking_app()
    app._on_pick("hemoglobin")
    for now in (0.0, PICK_PENDING_WARN_S + 1.0, 44.0):
        app._tick_state_at(now)
        text = (app.quad.notice or "").lower()
        assert "error" not in text and "fail" not in text and "/" not in text


def test_the_notice_clears_when_the_picked_fold_starts():
    """It has become the thing on screen. A banner still saying NEXT UP over
    the fold it was announcing is the booth talking over itself."""
    app = _picking_app()
    app._on_pick("hemoglobin")
    app._handle_event(_start("j3", card=3, target_id="hemoglobin"))
    assert not app.quad.notice


def test_the_focus_moves_to_the_cell_that_folds_the_pick():
    """Spec: 'a visitor's pick becomes the hero of the quad while the other
    three chips continue the attract playlist.'"""
    app = _picking_app()
    app._on_pick("hemoglobin")
    app._handle_event(_start("j0", card=0, target_id="attract-a"))
    app._handle_event(_start("j3", card=3, target_id="hemoglobin"))
    assert app.quad.focus == 3


def test_a_pick_for_a_target_already_folding_takes_the_focus_at_once():
    """The daemon queues nothing in this case (Task 9), so if the UI waited
    for a job_start that will never come, the visitor's pick would silently
    do nothing at all."""
    app = _picking_app()
    app._handle_event(_start("j2", card=2, target_id="hemoglobin"))
    app._on_pick("hemoglobin")
    assert app.quad.focus == 2


def test_the_other_three_cells_keep_folding_the_attract_playlist():
    """Spec: 'the other three chips continue the attract playlist.' A pick
    must not stop, clear or freeze any other cell."""
    app = _picking_app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    before = [v.cleared for v in app.quad.viewers]
    app._on_pick("hemoglobin")
    assert [v.cleared for v in app.quad.viewers] == before


def test_a_pick_does_not_disturb_a_frame_stream_in_flight():
    app = _picking_app()
    app._handle_event(_start("j1", card=1))
    app._on_pick("hemoglobin")
    app._on_event(_frame("j1"))
    app._drain_frames()
    assert app.quad.viewers[1].points == 1


def test_a_pick_still_reaches_the_booth_state_machine():
    """Unchanged: the pick closes the gallery. Regressing this makes the
    booth stop responding to a tap."""
    app = _picking_app()
    app._on_touch()
    app._on_pick("hemoglobin")
    assert app.display_state == "folding"


def test_a_pick_the_daemon_never_folds_expires():
    """A visitor who picks and walks away must not pin the focus, or the
    notice, for the rest of the day."""
    app = _picking_app()
    app._on_pick("hemoglobin")
    app._tick_state_at(0.0)
    app._tick_state_at(9999.0)
    assert app.router.selected_target is None
    assert not app.quad.notice
```

And in `tests/unit/test_app_interaction.py`:

```python
def test_the_help_intro_no_longer_says_picking_is_not_wired_up():
    """It reads, verbatim before this task: 'asking it to fold a particular
    one on demand isn't wired up yet'. It is now, and a booth that
    disclaims a capability it has teaches visitors not to try it."""
    from ui.app import _HELP_INTRO
    text = " ".join(_HELP_INTRO).lower()
    assert "isn't wired up" not in text
    assert "is not wired up" not in text
    assert "one after another" not in text


def test_the_help_intro_says_what_a_tap_now_does():
    from ui.app import _HELP_INTRO
    text = " ".join(_HELP_INTRO).lower()
    assert "next" in text


def test_the_help_intro_does_not_promise_an_instant_fold():
    """With four chips busy the pick starts when one frees. 'Instantly' is
    a claim the booth breaks in front of the one visitor watching for it."""
    from ui.app import _HELP_INTRO
    text = " ".join(_HELP_INTRO).lower()
    assert "instantly" not in text
    assert "straight away" not in text


def test_the_help_intro_says_a_pick_does_not_interrupt_a_running_fold():
    """The reason the wait exists, stated as the feature it is."""
    from ui.app import _HELP_INTRO
    text = " ".join(_HELP_INTRO).lower()
    assert "interrupt" in text or "finish" in text
```

And in the same file, for the gallery:

```python
def test_the_gallery_copy_says_a_tap_folds_it_next():
    from ui.gallery import _CAPTION_BODY, _CAPTION_TITLE, _CARD_HINT
    lowered = f"{_CAPTION_TITLE} {_CAPTION_BODY} {_CARD_HINT}".lower()
    assert isinstance(_CAPTION_BODY, str), "a tuple here would join per-character"
    assert "next" in lowered
    assert "isn't wired up" not in lowered
    assert "is not wired up" not in lowered


def test_the_gallery_copy_no_longer_says_one_after_another():
    """It reads, verbatim before this task: 'It works through these one
    after another, all day.' Four chips is four at a time."""
    from ui.gallery import _CAPTION_BODY
    lowered = _CAPTION_BODY.lower()
    assert "one after another" not in lowered
    assert "the fold that is running right now" not in lowered


def test_the_gallery_module_docstring_no_longer_describes_a_one_way_socket():
    """That docstring is the instruction sheet for anyone editing this copy,
    and it currently says, in bold, that a tap does not reach the daemon and
    that the copy changes back only when the protocol grows a client->server
    message. It has. A stale instruction sheet is how the copy regresses."""
    import ui.gallery
    text = ui.gallery.__doc__.lower()
    assert "one-way" not in text
    assert "cannot be reached from here yet" not in text
```

**Mutations these must catch:** dropping the `send_pick` call, i.e. reverting to a nomination (test 1 red); an unguarded `self._client.send_pick` with no client (test 2 red); letting a send failure escape or reach the screen (test 3 red); sending while `incompatible` (test 4 red); acknowledging only once the daemon answers (test 5 red); one static notice regardless of how long the wait runs (test 6 red); a notice built by interpolating an exception (test 7 red); never clearing the notice (tests 8, 14 red); moving the focus at pick time for a target nobody is folding — which would point the hero cell at the wrong protein (Task 12 test 25 red); failing to focus a target already in flight (test 10 red); clearing or freezing another cell on a pick (tests 11, 12 red); dropping the `states.on_pick` call (test 13 red); a pick that never expires (test 14 red); reverting any of the copy (the copy tests red).

**Test 10 and Task 9's "already folding queues nothing" are one behaviour split across two processes.** If either half is missing, a visitor who picks the protein currently on screen gets nothing at all — the daemon queues no job, and the UI waits for a `job_start` that will never arrive. Verify them together.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

The pick expiry stays where the old plan put it: the existing 45 s idle timeout is the clock source, not a second timer. When the booth returns to `attract`, `router.release_target()` is called and the notice clears. One number for "the visitor has gone".

Then **look at it**: with `runner/mock.py` replaying `quad_fold.jsonl` there is no daemon to answer a pick, so drive this one against the real daemon in Task 18 — but do open the gallery windowed and confirm the notice renders and is legible (`assert_every_label_is_legible` covers contrast; it does not cover a banner that lands under the quad's own captions).

---

### Task 18: Four workers, one booth [hardware]

**Files:** Modify `scripts/run-demo.sh`, `README.md`. Test: manual, on hardware, plus `tests/integration/test_four_workers.py` (opt-in via `--hw`).

**This is the first task that opens a device.** Everything before it is green without one.

- [ ] **Step 1: Bring the booth up on four chips**

Verify with measurements, not impressions:

- `tt-smi -s` before and after. During a fold, **all four chips** should show elevated power. The spike measured chip 1 at 33.0 W against 13–17 W idle for the others when only chip 1 worked; four-way should show four elevated. Record the numbers. **Sample while folds are actually running** — the spike's own caveat is that it sampled at a fixed 25 s by which time everything had finished, which is why its four-way power evidence proves nothing.
- Four cells on screen, four different proteins, four different progress states at the same instant. Screenshot with `spectacle -b -n -f -o /tmp/quad-live.png`.
- Time from daemon start to first `hello` (not `not_ready`). The spike measured model load at 6.4–9.2 s under four-way contention; anything much beyond that is worth understanding before the venue.
- Peak RSS of the four workers together, from `ps`. The spike measured 4.04 GB each, ~16 GB total. A materially larger number means something is not sharing what it should.
- Kill one worker with `kill -9` mid-fold. **The other three must keep folding**, the killed chip's cell must not strand, and a replacement worker must come up and fold again. This is Task 7's contract against real silicon and it is the single most valuable minute of this task.
- **Tap a target in the gallery, twice: once with a chip free, once with all four busy.** Time both from the tap to that target's `job_start` in the daemon log. The free-chip case should be well under a second; the busy case is bounded by the remaining time of the earliest-finishing of the four folds in flight. Record both numbers. Confirm from the log that **no running fold was cancelled** (Task 9's ruling), and on screen that the picked cell becomes the focus and the other three keep folding the playlist. If the busy-case number is anywhere near twenty seconds, that is a finding about the playlist's largest targets and it belongs in `docs/followups.md` with the measurement, not in a shrug.
- **Pick the target that is already folding.** Nothing new should be queued, and the cell already folding it should take the focus immediately. This is the one case where the daemon and the UI have to agree without exchanging a message about it (Task 9 and Task 17).
- **Run the UI against a deliberately mismatched daemon once** — edit `PROTOCOL_VERSION` in one venv's checkout, or run the pre-amendment daemon binary. The booth must show its neutral overlay, log the mismatch, and send nothing. A version guard nobody has ever watched fire is a version guard.
- Then `Ctrl+C` the daemon and confirm with `tt-smi -s` and `ls /dev/tenstorrent` that **no process holds a device**. Check for stale lease files (`tt_bio.device_lease.lease_dir()`).

- [ ] **Step 2: Write the hardware test**

`tests/integration/test_four_workers.py`, opt-in via `--hw`, must be honest about what it costs: it opens every card on the box. Keep it to one test that starts the pool, waits for all four `CONTROL_READY`, folds the one vendored target on each, asserts four distinct `.cif` outputs with plausible pLDDT, and stops the pool cleanly. Assert the pool leaves nothing running.

- [ ] **Step 3: `scripts/run-demo.sh` and the README**

`run-demo.sh` passes `--devices` through. The README says what is now true: four chips, four proteins, one per chip; a single target is still a single-card fold (tt-bio's own documented limit — do not imply otherwise); and a visitor's pick folds **next** — on the next chip to free, without interrupting the folds already running. Do not claim it is instant, and do not leave the old sentence saying a pick starts nothing.

- [ ] **Step 4: Commit**

---

### Task 19: The soak [hardware]

**Files:** Report only, plus whatever it forces.

**Why:** `docs/followups.md`: "Short runs cannot see unbounded growth. Two separate tasks 'verified log containment' with two-fold sessions. A 28-fold run found tt-metal's Inspector holds its log file open and keeps writing ~13–14 MB/s *after* the file is unlinked." This phase quadruples the writers and adds four parent-held log files. A two-fold check would prove nothing.

- [ ] **Step 1: Run for at least one hour, hands off, on four chips**

Sample every five minutes and plot or table the results:

- `tt-smi -s` temperatures for all four chips. **Did the 85 °C quarantine fire?** Either answer is a finding; record which. If it fired, confirm from the log that the other three kept folding and that the chip came back when it cooled.
- Log root size **by `du`** *and* by `lsof -p <each worker pid>`. The two must agree. A `du` that says 40 MB while `lsof` shows a deleted-but-open file at 2 GB is the exact failure from Phase 3a, and the default log root is tmpfs, so the failure mode is an OOM, not a full disk.
- Total RSS across the four workers and the parent. Flat is the expected answer.
- Fold count and mean wall time per chip. Four chips should give roughly 4× throughput on queued targets; if they do not, say by how much and why (host CPU contention is the first suspect — check that `OMP_NUM_THREADS` really is capped in each worker via `/proc/<pid>/environ`).
- The four `worker.log` files: did any exceed `WORKER_LOG_CAP_BYTES`, and did truncation actually free the space?

- [ ] **Step 2: Record the numbers in `docs/followups.md` and fix what the soak found**

An hour that finds nothing is a result worth writing down, with the numbers, so the next person does not have to re-run it to find out.

- [ ] **Step 3: Commit**

---

## Definition of done

1. `./scripts/test.sh` passes both halves, with no hardware.
2. Every task's named mutations were verified red-then-green, with evidence in its report.
3. The daemon holds no device; four worker subprocesses hold one chip each.
4. Four proteins fold simultaneously, one per chip, in a 2×2 quad, each labelled with its chip.
5. Killing one worker mid-fold leaves the other three folding, and the killed chip recovers.
6. The Tensix panel, the help card, the gallery and the diagnostics copy all describe four working chips and a pick that folds next — none of them claims a pick starts nothing, and none of them claims it is instant.
7. A one-hour four-chip soak shows bounded logs (verified with `lsof`, not just `du`), bounded RSS, and a recorded answer to whether the thermal quarantine fired.
8. `tt-smi -s` after shutdown shows no process holding a device.
9. A visitor's pick reaches the daemon, is dispatched ahead of the attract backlog, and becomes the focus cell of the quad — measured on hardware with a chip free and with all four busy, with no running fold cancelled either time.
10. The daemon survives everything a client can send it — malformed JSON, an unknown message type, a line split across reads, a megabyte with no newline, a client that connects and says nothing — without dropping a healthy UI and without any of it reaching the screen.
11. `PROTOCOL_VERSION` is `2` on both sides, no committed fixture advertises a stale version, and a mismatch in either direction refuses cleanly onto a neutral screen.

## What this phase deliberately leaves out

- **Multi-chip within a single fold.** Not available: tt-bio's own documentation states a single target remains a single-card fold, and extra cards raise throughput only across queued targets. There is nothing to measure and no faster-single-fold option to weigh against the quad.
- **Multi-host.**
- **A second client→server message.** The client direction carries exactly one type, `pick`. Anything else — cancel, replay, a target that is not in the playlist, a queue-position query — is another version bump, another decision, and a task of its own.
- **Preempting a running fold to serve a pick.** Ruled out in Task 9, with reasons and numbers. If the wait measured at a venue turns out to be intolerable, revisit it there with the measurement in hand — not here, on the strength of a worry.
- **Pipelined pre-compute** (spec option C). It optimises the thing nobody can see.
- **Per-cell interaction** — tapping a cell to enlarge it. The quad is four equal cells; a hero-scaling layout is a design question, not a plumbing one.
