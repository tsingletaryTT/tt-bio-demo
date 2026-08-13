# tt-bio-demo Phase 5: Multi-chip folding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four Blackhole chips fold four proteins at once, one per chip, shown as a 2×2 quad. The booth stops being a four-chip machine with one chip working.

**Architecture:** The daemon stops owning a device. It becomes a parent that owns the queue, the card pool, the socket and four subprocesses — one per chip, each holding its own `Folder` and its own resident model. Worker events are multiplexed onto the existing socket **unchanged**: the wire protocol does not move. The UI stops assuming one fold in flight and keys its per-fold state by `job_id`.

**Tech Stack:** Python 3.12; `tt_bio.runtime` / `tt_bio.main` for device assignment; GTK 4.14 via PyGObject; the project's own `protocol/events.py`.

**Spec:** [`../specs/2026-08-13-multi-chip-folding.md`](../specs/2026-08-13-multi-chip-folding.md) — it is authoritative and it is already grounded in a hardware spike run on 2026-08-13. Do not re-derive its findings; build on them.
**Read before starting:** [`../../followups.md`](../../followups.md), especially "Short runs cannot see unbounded growth" and "Write tests that can fail". Both are load-bearing here: this phase multiplies every log writer by four and rewrites the most heavily-tested module in the runner.

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
- `protocol/events.py` does not change in this phase. `PROTOCOL_VERSION` stays `1`; `EVENT_TYPES` gains nothing. Task 3 pins this.
- Runner-side tests go in `tests/unit/runner/`; everything else in `tests/unit/`. The split is by directory (see `scripts/test.sh`'s header).

**Hardware**

- Never `tt-smi -r`. Never leave a process holding a device.
- The machine is shared. Hardware is available **today** and may be taken back at any time. Every task below is marked **[no device]** or **[hardware]**. All fourteen implementation tasks are **[no device]**; only Tasks 15 and 16 need silicon, and they are last on purpose.
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
| `_SHOWCASE_DWELL_S` | `2.0` | `ui/app.py` | Unchanged. It is now a **per-slot** dwell (Task 9). |
| worker log root | `<log-root>/card-<n>` | `runner/workers.py` | One `TT_METAL_LOGS_PATH` per worker, so four writers cannot interleave into one tree and a crash's logs are attributable. |
| worker structures dir | `/tmp/tt-bio-demo/structures/device-<n>` | already in `runner/folder.py` | Already namespaced by `device_id`. No change; Task 8 makes the pruner walk all four. |

**Commit after every task**, with conventional-commit prefixes.

---

## Two corrections to the spec, declared up front

A reviewer should judge these rather than report them as gaps.

**1. `tt_bio.main` is NOT importable without importing ttnn.** The spec says `tt_bio.runtime` is ttnn-free — that is **true and verified**:

```
tt_bio.runtime -> ttnn imported: False | torch: False
tt_bio.main    -> ttnn imported: True  | torch: True
```

`_build_worker_device_assignments`, `_detect_p300_devices` and `_find_ttnn_mesh_graph_descriptor` are *themselves* ttnn-free — they read `/sys/class/tenstorrent` and use `importlib.util.find_spec("ttnn")`, which does not import it — but the **module they live in** imports `ttnn` and `torch` at module scope. Importing them in the parent therefore pulls ttnn into the parent process.

This does not break the design, and the reason is worth stating because it is what makes the whole architecture safe: **importing ttnn opens no device — only `get_device()` does** — and the parent computes the assignments and hands them to each child through `subprocess.Popen(env=...)`. The child's environment is therefore complete *before its interpreter starts*, which is a stronger ordering guarantee than "set the variable before the import" could ever be. The parent already imports tt-bio today (`runner/preflight.py`'s tap check imports `tt_bio.protenix`), so this is the status quo, not a new cost.

Consequence: `runner/workers.py` imports tt-bio **lazily, inside functions**, the same discipline `runner/folder.py` and `runner/daemon.py` already follow — so the module itself, and the 134 existing runner tests, never pay for ttnn. Task 1 pins this with a test.

**2. The gallery pick still cannot reach the daemon.** The spec says "a visitor's pick becomes the *hero* of the quad while the other three chips continue the attract playlist." The socket protocol is one-way (`runner/server.py` broadcasts; `ui/client.py` never sends), so a pick cannot cause a fold, and adding a client→server message is a separate piece of work with its own protocol-version implications. This plan therefore implements the honest half: **a pick nominates a target, and the cell that folds that target becomes the focus cell when the attract loop reaches it.** The visitor-facing copy already says picking on demand is not wired up (`_HELP_INTRO`, `ui/gallery.py`'s docstring) and stays true. Task 14 covers this and says so on screen. **If this is not acceptable, stop and say so before Task 14** — the alternative is a protocol change that belongs in its own phase.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `runner/workers.py` | **new** | What a worker is: `WorkerSpec`, the device assignment, the environment, the control vocabulary. No subprocess management, no threads. |
| `runner/worker.py` | **new** | The worker process itself: `python3 -m runner.worker`. Holds one `Folder`, reads commands on stdin, writes events on `EVENT_FD`. |
| `runner/pool.py` | **new** | `WorkerPool`: spawn, dispatch, multiplex, notice death, respawn, retire. The only module that owns subprocesses or reader threads. |
| `runner/daemon.py` | rewritten | Parent: queue, `CardPool`, `EventServer`, pool. Owns no `Folder` and no device. |
| `runner/folder.py` | unchanged | Now instantiated inside a worker instead of inside the daemon. Its `device_id` finally means something. |
| `runner/cards.py` | unchanged | Already multi-index. Task 7 exercises it for the first time. |
| `runner/queue.py` | unchanged | Already priority-ordered and thread-safe. |
| `protocol/events.py` | **unchanged, pinned** | The wire does not move. |
| `ui/slots.py` | **new** | Per-fold state, pure: `SlotState` (one cell's dwell), `SlotRouter` (job_id → slot, focus slot). No GTK. |
| `ui/quad.py` | **new** | `QuadView`: four `StructureViewer`s in a 2×2 grid with per-cell captions. Assembly only. |
| `ui/viewer.py` | unchanged | Stays a **single-structure** renderer. That is the point: everything it learned about camera ownership, blend targets and per-job reset is per-cell machinery already. |
| `ui/states.py` | narrowed | Keeps `attract`/`gallery`/`folding`/`preparing`, the deferred touch and the idle timeout. Its `showcase` now follows the **focus slot**. |
| `ui/app.py` | rewritten in parts | Wiring: per-slot frame buffers, per-slot ribbon generations, per-slot deferred clears. Still makes no decisions of its own. |
| `ui/client.py` | extended | Gains `LatestFrameByJob` beside `LatestFrame` — same latest-wins contract, one slot per `job_id`. |
| `ui/chipviz.py` | extended | Per-chip modes instead of one folding chip. |
| `ui/gallery.py` | copy only | `_CAPTION_BODY` stops saying the booth folds "one after another". The disclosure that a pick starts nothing stays. |
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
| 45 s idle timeout, `_last_input_at` | global | **stays global** | Ditto. |
| Help / diagnostics / Tensix overlays and their idle timers | global | **stays global** | Chrome, laid over whatever the booth is doing. Unchanged. |
| `TelemetrySampler` + `TelemetryPanel` | global, 4 chips | **stays global, unchanged** | It already shows all four chips and is already independent of the socket. Do not couple it to the pool. |
| `missing` / `display_message` / preparing overlay | global | **stays global** | A daemon that cannot fold is a booth-wide fact. |
| `_drop_counts`, `DiagnosticsLog` | global | **stays global** | One log for the booth. Diagnostics lines gain the card. |
| Tensix chip modes (`set_folding_chip`) | one chip | **per chip** (`set_chip_stages`) | The whole reason the panel had to be walked back to stay honest. |

---

## Task order and hardware exposure

Tasks 1–14 need **no device** and can be completed if the hardware is taken away tomorrow. Tasks 15 and 16 are the only ones that need silicon, and they are deliberately last and self-contained: if hardware disappears mid-phase, everything up to Task 14 still lands, `./scripts/test.sh` is still green, and the branch is still mergeable behind the existing single-card `--devices 0` path.

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
        if isinstance(outcome, Exception):
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
    """The wire does not change. A worker that decorates its events is a
    worker whose events the UI has to learn about."""
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

`main()` is thin: parse `--card`/`--event-fd`, build a `Folder(device_id=card)`, wrap `os.fdopen(event_fd, "w")`, and drive `WorkerSession` from `sys.stdin`. Do **not** call anything like tt-bio's `_silence_subprocess_output` — the parent (Task 4) owns where fd 1 and 2 go, and a worker that redirects them to `/dev/null` throws away the only diagnostic an operator has when a chip fails to come up.

---

### Task 3: The wire does not change [no device]

**Files:** Test only: `tests/unit/test_protocol_is_frozen.py` and `tests/unit/runner/test_protocol_is_frozen_runner.py`

**Why:** The spec's central claim about the UI is "the wire protocol does not change — `job_start` already carries `card`, every event carries `job_id`." That claim is doing a lot of work in this plan and it is currently held up by prose. A worker or a pool that leaks a control line onto the socket breaks it silently: `EventServer.broadcast` would log and drop it, and the only symptom is an event the UI never sees.

**Produces:** two tiny test files (one per venv, because both sides make the claim) that fail the instant the protocol moves.

- [ ] **Step 1: Write the failing tests**

The same body goes in both files; the duplication is deliberate and is the point — each half must hold the constraint in the interpreter that actually runs it.

```python
from protocol.events import EVENT_TYPES, PROTOCOL_VERSION


def test_the_protocol_version_is_unchanged_by_multi_chip():
    """Multi-chip is a scheduling change, not a protocol change. If this
    fails, either a genuine protocol addition happened -- in which case bump
    the version, teach ui/client.py, and change this number deliberately --
    or something leaked onto the wire that should not have."""
    assert PROTOCOL_VERSION == 1


def test_the_event_vocabulary_is_unchanged_by_multi_chip():
    assert EVENT_TYPES == frozenset(
        {"hello", "not_ready", "job_start", "stage", "frame",
         "job_done", "job_error", "card_state"})
```

**Mutations these must catch:** adding any event type for multi-chip (test 2 red); bumping the version without deciding to (test 1 red).

- [ ] **Step 2: Run, confirm green against today's code, commit**

This task is unusual: its tests pass immediately. That is correct — they are a **ratchet**, not a red-green cycle. Verify them by mutating `protocol/events.py` (add `"worker.ready"` to `EVENT_TYPES`) and watching both halves go red. Report that.

---

### Task 4: The pool — spawn, dispatch, multiplex [no device]

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

Two more constructor keywords — `on_worker_lost=` and `restart_delay_s=` — arrive in **Task 5**, which owns death and respawn. Build the constructor so adding them there is a keyword, not a rewrite.

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

One reader thread per worker, blocking on `readline`. `on_event` is called **from that thread** — `EventServer.broadcast` is already thread-safe (it holds `_lock` and never raises into its caller), so this is safe, and it is what keeps a slow chip from blocking the other three. Say so in a comment.

**Name constraint:** `WorkerPool` keeps its own worker handles, spawn record and loss record under **private** names (`_workers`, and whatever else it needs), matching this codebase's existing convention (`EventServer._clients`, `JobQueue._items`, `CardPool._busy`). The fixtures above attach *test* handles at `pool.workers` / `pool.spawns` / `pool.lost`, and a public attribute of the same name would be silently clobbered by the fixture — a test that then passes against production state it has overwritten.

Also extract `_FakeWorker`, `_spec`, `_job` and `_wait` into `tests/unit/runner/_workerfakes.py` as part of this task (Task 5 imports them). The leading underscore is what keeps pytest from collecting it, the same convention `tests/unit/_legibility.py` already uses.

---

### Task 5: A worker that dies must not take the booth down [no device]

**Files:** Modify `runner/pool.py`. Test: `tests/unit/runner/test_worker_death.py`

**Why:** The spec names this the highest risk in the change, and it is: "the current single-process design fails closed, four workers must fail **partially**." Every other task in this plan is about making four chips work. This one is about the other 5% of the time.

**Produces:** on a worker's event stream reaching EOF (the process died, however it died), the pool must, in this order:

1. Notice within one reader-thread wakeup — no polling timer, no timeout.
2. If a job was in flight, report it: call `on_worker_lost(card, job_id, target_id)` — the pool has the whole `Job` from `dispatch`, so it can name the target without the daemon looking it up. **The pool never fabricates a protocol event**; the daemon (Task 6) decides what the wire sees. This keeps "who talks to the socket" in one module.
3. Mark the card not-ready and not-busy.
4. Respawn after `WORKER_RESTART_DELAY_S`, unless the card has died `WORKER_RETIRE_AFTER` times consecutively with no completed job in between, in which case retire it for the session with a loud log.
5. Never touch the other three workers.

- [ ] **Step 1: Write the failing tests**

Reuse `_FakeWorker`, `_spec`, `_wait` and `_job` from Task 4 — move them into `tests/unit/runner/_workerfakes.py` as part of this task and import them from both files. (`_`-prefixed, so pytest never collects it, the same convention `tests/unit/_legibility.py` already uses.)

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
    """The booth is now unable to fold. It must say so (Task 6's not_ready),
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

### Task 6: The daemon owns no device [no device]

**Files:** Rewrite `runner/daemon.py`. Test: `tests/unit/runner/test_daemon_multichip.py`; the existing `tests/unit/runner/test_daemon.py` is rewritten with it.

**Why:** This is where the parts meet. The daemon keeps the queue, the `CardPool`, the `EventServer` and the failure/quarantine policy; it loses the `Folder`, the device and `_run_one`'s in-line fold.

**Produces:**

- `DaemonConfig` loses `device_id`, gains `device_ids: str | None = None` (the `--devices 0,1,2,3` CLI flag the spec quotes tt-bio's own docs for).
- `Daemon` has **no `folder` attribute** and never imports `Folder`.
- `Daemon.run()` builds `worker_specs(...)`, constructs `CardPool([s.card for s in specs])`, starts the pool, and loops: for every schedulable card that is also pool-ready, take a job and dispatch it.
- `_hello()` reports `not_ready` until `pool.any_ready()`, then `hello` with `cards = cards.all_indices()`.
- `on_worker_lost(card, job_id)` emits a `job_error` for the orphaned job, marks the card idle, and records the failure against the target.

**Ruling on quarantine, because it is easy to get backwards:** a worker death counts as **one failure for the target** (a target that reliably kills a worker is a target that must eventually be quarantined — `QUARANTINE_AFTER = 3` unchanged) **and separately** against the card (`WORKER_RETIRE_AFTER`, Task 5). The two counters are independent and neither may be derived from the other.

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

Extract `_FakePool`, `_CollectingServer` and `_daemon` into **`tests/unit/runner/_daemonfakes.py`** as part of this task — Tasks 7 and 8 import them from there, and three copies of a fake pool is three places for the fake to drift from the real one.

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

Also in this task: `main()` gains `--devices` (default: all), and the long `DaemonConfig.device_id` comment explaining why `--device` was deleted is **replaced**, not deleted — rewrite it to say what is now true (one process per chip, `TT_VISIBLE_DEVICES` set by the parent at spawn, `CardPool` and the hardware finally referring to the same chips). `docs/followups.md`'s "`--device` was removed rather than plumbed" entry moves to a "closed" section with a pointer here.

---

### Task 7: Thermal at 4× [no device]

**Files:** Modify `runner/daemon.py` if needed. Test: `tests/unit/runner/test_thermal_four_up.py`

**Why:** `CardPool`'s 85 °C quarantine "already exists and has never fired in anger" (spec). Four chips folding continuously is when it fires. Its unit tests cover the class; nothing covers what the *daemon* does when it fires with three other folds in flight.

**Produces:** no new production surface if Task 6 is right. This task's job is to prove it, and to fix it if it is not.

- [ ] **Step 1: Write the failing tests**

```python
from runner.cards import CardState
from runner.queue import Job

from _daemonfakes import _FakePool, _daemon      # extracted in Task 6


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

### Task 8: Logs and structures at 4× [no device]

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

### Task 9: Per-fold state, pure [no device]

**Files:** Create `ui/slots.py`. Modify `ui/states.py`. Test: `tests/unit/test_slots.py`

**Why:** This is the decision layer for the whole UI change, and per the project's own rule the wiring layer makes no decisions. Everything hard about four concurrent folds — which cell an event belongs to, whether this cell's dwell has expired, which cell the booth is following — is answerable here with no GTK and no display.

**Produces:**

- `MAX_SLOTS = 4`
- `SlotState(showcase_dwell_s=2.0)` with `.state` in `{"idle", "folding", "showcase"}`, `.job_id`, `.card`, `.on_job_start(event)`, `.on_job_done(event)`, `.on_job_error(event)`, `.on_structure_revealed()`, `.tick(now)`, and the two predicates `.points_are_visible` / `.ribbon_may_be_revealed` as properties.
- `SlotRouter(cards, showcase_dwell_s=2.0)` with `.slots`, `.slot_for_card(card)`, `.slot_for_job(job_id)`, `.on_event(event) -> int | None`, `.tick(now) -> list[int]`, `.select_target(target_id)`, `.release_target()`, `.selected_target`, `.focus_slot`, and `.tracked_jobs` — the job ids the router can still route, exposed publicly **because a test has to be able to assert it is bounded**, and asserting on a private field is the "adjacent to the behaviour" shape `docs/followups.md` names as this project's recurring test defect.

**Reuse:** `ui.states.showcase_ended(previous, current)` is imported and used as-is against `SlotState` values — `SlotState` deliberately spells its showcase state `"showcase"`, the same string `BoothState.SHOWCASE` carries, so the existing tested function works on both. **Do not write a second one.**

**The focus rule**, stated once and tested below: the focus slot is the slot folding (or showcasing) the visitor's selected target if there is one; otherwise the slot that most recently entered `showcase`; otherwise slot 0.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from ui.slots import MAX_SLOTS, SlotRouter, SlotState
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
    """The daemon cannot be asked for a target (the socket is one-way), so a
    pick may wait a whole playlist cycle. Moving the focus to a cell folding
    something else would be a lie."""
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
```

**Mutations these must catch:** making the dwell global across slots (test 4 red); measuring the dwell from `job_done` (test 6 red); letting `job_start` cut a dwell short (test 7 red); dropping the deferred `job_start` instead of applying it (test 8 red); applying a stale `job_error` (test 10 red); routing by card instead of `job_id` for non-`job_start` events (test 16 red); routing an unknown job to slot 0 (test 17 red); an unbounded job map (test 21 red); reporting every slot from `tick` (test 22 red); a focus that ignores the pick (test 24 red); a focus that follows the pick before its fold starts (test 25 red); never releasing the pick (test 27 red).

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

`ui/states.py` changes in this task only by **narrowing its docstring** to say that the showcase dwell now lives in `ui/slots.py` and that `BoothState.SHOWCASE` follows the focus slot. Its own tests must stay green untouched — if any of `tests/unit/test_states.py` needs editing, stop: that means behaviour moved that this plan said would not.

---

### Task 10: A four-fold fixture [no device]

**Files:** Create `tests/fixtures/streams/make_quad_fold.py`, generate `tests/fixtures/streams/quad_fold.jsonl`. Test: `tests/unit/test_quad_fixture.py`

**Why:** Everything from here to Task 14 needs a realistic four-way interleaved stream, and `runner/mock.py` — "the project's core test instrument" — can already replay one to a real UI with no hardware. Building the fixture now is what lets Tasks 11–14 be verified against something that behaves like the daemon rather than against hand-built dicts.

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

---

### Task 11: The quad view [no device]

**Files:** Create `ui/quad.py`. Test: `tests/unit/test_quad.py`

**Why:** `ui/viewer.py` renders one structure and must keep doing so — everything it learned about camera ownership, blend targets and per-job reset is per-cell machinery already, and reworking it into a multi-viewport renderer would put four folds' worth of state back into one object, which is the defect this phase exists to remove. So the quad is four `StructureViewer`s in a grid, and `ui/viewer.py` is not touched.

**Produces:**

- `grid_position(slot) -> (column, row)` — pure: `0→(0,0)`, `1→(1,0)`, `2→(0,1)`, `3→(1,1)`.
- `QuadView(Gtk.Grid)` built from a card list, with `.viewers`, `.viewer_for_slot(slot)`, `.set_caption(slot, text)`, `.set_focus(slot | None)`, `.set_connection_state(state)`, `.slot_count`.
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
    assert_every_label_is_legible(
        quad, context="ui.quad", min_contrast=MIN_CONTRAST_RATIO,
        contrast_ratio_fn=contrast_ratio,
        css_text_fn=lambda: quadmod._QUAD_CSS,
        background_by_class_fn=lambda: quadmod._BACKGROUND_BY_CLASS)
```

**Mutations these must catch:** row-major transposed to column-major (test 1 red); building a fixed four cells regardless of the card list (tests 4, 5 red); labelling cells by slot index instead of card number (test 7 red); leaving the previous focus marking in place (test 8 red); no bounds check on focus or caption (tests 10, 12 red); setting the connection state on one viewer (test 13 red); dropping the guard around the setter (test 14 red); building a caption label with no colour-bearing class (test 15 red).

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

Then **look at it**: run the app windowed against `quad_fold.jsonl` via `runner/mock.py` and take a screenshot (`spectacle -b -n -f -o /tmp/quad.png` — `grim` does not work on this KWin box). Four GL contexts in one window is the one thing here that no unit test can tell you about; confirm all four cells actually render before moving on, and put the screenshot in the task report.

---

### Task 12: Wire the quad into the app [no device]

**Files:** Modify `ui/app.py`. Test: `tests/unit/test_app_quad.py`

**Why:** The largest single piece of work in this phase, and the one the per-fold/global table exists to keep honest. `ui/app.py` should get **smaller in responsibility** as it grows in lines: the routing decisions are in `ui/slots.py`, the layout is in `ui/quad.py`, and this file only carries them out.

**Produces:**

- `DemoApp` gains `self.quad`, `self.router` (a `SlotRouter`), and **`attach_cards(cards)`** — build the router and size the quad from a card list. `_handle_event` calls it on `hello`; `do_activate` calls it with a single-card default so a booth with no socket still renders.
- `ui/client.py` gains **`LatestFrameByJob(max_jobs=8)`** — the same latest-wins contract as `LatestFrame`, one slot per `job_id`, oldest job evicted past `max_jobs`, with `__len__` and `.take_all() -> dict[job_id, event]`. It lives beside `LatestFrame` because that is where the one-slot buffer and its "diffusion frames are advisory" argument already live. `self._frames` becomes one of these.
- `_ribbon_generation` / `_pending_ribbon` / `_deferred_clear` all become **per-slot**, per the table above.
- **`self.viewer` is removed, not aliased** — an alias is a place for four folds to quietly become one again. `tests/unit/test_ribbon_async.py`, `test_app_handle_event.py` and `test_app_not_ready.py` are updated with it.
- `_tick_state_at(now)` and `_join_ribbon_workers(timeout)` are the two new test seams (see Step 2).

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

    def viewer_for_slot(self, slot):
        return self.viewers[slot]

    def set_caption(self, slot, text):
        self.captions[slot] = text

    def set_focus(self, slot):
        self.focus = slot

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

### Task 13: Four chips, honestly [no device]

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
    """The one claim that must NOT change: the socket is still one-way."""
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

### Task 14: The gallery pick, and what the booth says about it [no device]

**Files:** Modify `ui/app.py`, `ui/gallery.py`. Test: `tests/unit/test_app_pick.py`

**Why:** The spec asks that a visitor's pick become the hero of the quad. Per the declared deviation at the top of this plan, the pick still cannot cause a fold — so what it can honestly do is nominate a target and take the focus when the loop reaches it, and the copy must say exactly that.

**Produces:** `_on_pick` additionally calls `self.router.select_target(target_id)`; the focus cell is marked when that target's fold starts; the gallery's copy and `_HELP_INTRO`'s disclosure paragraph are updated to describe the quad rather than a single fold.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_pick_nominates_a_target_without_claiming_to_start_it():
    app = _app()
    app._on_pick("hemoglobin")
    assert app.router.selected_target == "hemoglobin"
    assert app.quad.focus is None, "nothing is folding it yet"


def test_the_focus_moves_when_the_picked_target_starts_folding():
    app = _app()
    app._on_pick("hemoglobin")
    app._handle_event(_start("j0", card=0, target_id="attract-a"))
    assert app.quad.focus != 0 or app.router.selected_target == "hemoglobin"
    app._handle_event(_start("j3", card=3, target_id="hemoglobin"))
    assert app.quad.focus == 3


def test_the_other_three_cells_keep_folding_the_attract_playlist():
    """Spec: 'the other three chips continue the attract playlist.' A pick
    must not stop, clear or freeze any other cell."""
    app = _app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    before = [v.cleared for v in app.quad.viewers]
    app._on_pick("hemoglobin")
    assert [v.cleared for v in app.quad.viewers] == before


def test_a_pick_still_reaches_the_booth_state_machine():
    """Unchanged: the pick closes the gallery. Regressing this makes the
    booth stop responding to a tap."""
    app = _app()
    app._on_touch()
    app._on_pick("hemoglobin")
    assert app.display_state == "folding"


def test_a_pick_for_a_target_the_loop_never_reaches_expires():
    """A visitor who picks and walks away must not pin the focus for the
    rest of the day."""
    app = _app()
    app._on_pick("hemoglobin")
    app._tick_state_at(0.0)
    app._tick_state_at(9999.0)
    assert app.router.selected_target is None


def test_the_gallery_copy_does_not_promise_an_on_demand_fold():
    """The whole-branch review's Critical 2. The gallery says what it IS."""
    from ui.gallery import _CAPTION_BODY, _CAPTION_TITLE, _CARD_HINT
    lowered = f"{_CAPTION_TITLE} {_CAPTION_BODY} {_CARD_HINT}".lower()
    assert isinstance(_CAPTION_BODY, str), "a tuple here would join per-character"
    assert "tap to fold" not in lowered
    assert "isn't wired up" in lowered or "is not wired up" in lowered


def test_the_gallery_copy_no_longer_says_one_after_another():
    """It reads, verbatim today: 'It works through these one after another,
    all day.' Four chips is four at a time, and 'the fold that is running
    right now' is now four folds. Both have to change with the behaviour."""
    from ui.gallery import _CAPTION_BODY
    lowered = _CAPTION_BODY.lower()
    assert "one after another" not in lowered
    assert "the fold that is running right now" not in lowered


def test_the_help_disclosure_still_tells_the_truth_about_picking():
    from ui.app import _HELP_INTRO
    text = " ".join(_HELP_INTRO).lower()
    assert "isn't wired up" in text or "is not wired up" in text
```

**Mutations these must catch:** moving the focus at pick time rather than at `job_start` (test 1 red); never moving it (test 2 red); clearing other cells on a pick (test 3 red); dropping the `states.on_pick` call (test 4 red); a pick that never expires (test 5 red); reverting the honest copy (tests 6, 7 red).

- [ ] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

Test 5 needs a pick expiry. Use the existing 45 s idle timeout as the clock source rather than inventing a second timer: when the booth returns to `attract` from the idle timeout, the selected target is released. That keeps one number for "the visitor has gone".

---

### Task 15: Four workers, one booth [hardware]

**Files:** Modify `scripts/run-demo.sh`, `README.md`. Test: manual, on hardware, plus `tests/integration/test_four_workers.py` (opt-in via `--hw`).

**This is the first task that opens a device.** Everything before it is green without one.

- [ ] **Step 1: Bring the booth up on four chips**

Verify with measurements, not impressions:

- `tt-smi -s` before and after. During a fold, **all four chips** should show elevated power. The spike measured chip 1 at 33.0 W against 13–17 W idle for the others when only chip 1 worked; four-way should show four elevated. Record the numbers. **Sample while folds are actually running** — the spike's own caveat is that it sampled at a fixed 25 s by which time everything had finished, which is why its four-way power evidence proves nothing.
- Four cells on screen, four different proteins, four different progress states at the same instant. Screenshot with `spectacle -b -n -f -o /tmp/quad-live.png`.
- Time from daemon start to first `hello` (not `not_ready`). The spike measured model load at 6.4–9.2 s under four-way contention; anything much beyond that is worth understanding before the venue.
- Peak RSS of the four workers together, from `ps`. The spike measured 4.04 GB each, ~16 GB total. A materially larger number means something is not sharing what it should.
- Kill one worker with `kill -9` mid-fold. **The other three must keep folding**, the killed chip's cell must not strand, and a replacement worker must come up and fold again. This is Task 5's contract against real silicon and it is the single most valuable minute of this task.
- Then `Ctrl+C` the daemon and confirm with `tt-smi -s` and `ls /dev/tenstorrent` that **no process holds a device**. Check for stale lease files (`tt_bio.device_lease.lease_dir()`).

- [ ] **Step 2: Write the hardware test**

`tests/integration/test_four_workers.py`, opt-in via `--hw`, must be honest about what it costs: it opens every card on the box. Keep it to one test that starts the pool, waits for all four `CONTROL_READY`, folds the one vendored target on each, asserts four distinct `.cif` outputs with plausible pLDDT, and stops the pool cleanly. Assert the pool leaves nothing running.

- [ ] **Step 3: `scripts/run-demo.sh` and the README**

`run-demo.sh` passes `--devices` through. The README says what is now true: four chips, four proteins, one per chip; a single target is still a single-card fold (tt-bio's own documented limit — do not imply otherwise). Do not claim the pick starts a fold.

- [ ] **Step 4: Commit**

---

### Task 16: The soak [hardware]

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
6. The Tensix panel, the help card and the diagnostics copy all describe four working chips, and none of them claims a pick starts a fold.
7. A one-hour four-chip soak shows bounded logs (verified with `lsof`, not just `du`), bounded RSS, and a recorded answer to whether the thermal quarantine fired.
8. `tt-smi -s` after shutdown shows no process holding a device.

## What this phase deliberately leaves out

- **Multi-chip within a single fold.** Not available: tt-bio's own documentation states a single target remains a single-card fold, and extra cards raise throughput only across queued targets. There is nothing to measure and no faster-single-fold option to weigh against the quad.
- **Multi-host.**
- **A client→server protocol message**, and therefore a pick that actually causes a fold. See the declared deviation at the top of this plan. It needs a protocol version bump and belongs in its own phase.
- **Pipelined pre-compute** (spec option C). It optimises the thing nobody can see.
- **Per-cell interaction** — tapping a cell to enlarge it. The quad is four equal cells; a hero-scaling layout is a design question, not a plumbing one.
