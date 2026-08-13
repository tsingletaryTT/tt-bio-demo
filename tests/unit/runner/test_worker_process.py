"""The worker process: one Folder, one chip, a command stream in, events out.

Every test below drives `WorkerSession` against a fake `Folder`, so nothing
here opens a device -- which is the point of splitting `WorkerSession` (pure,
takes a folder and a list of lines) out of `main()` (argv, fds, stdin) in the
first place.

THREE DELIBERATE DEPARTURES from the reference tests in the task brief, all
recorded here rather than silently applied:

1. `test_a_non_FoldError_exception_is_also_reported_and_survived` gained an
   assertion on CONTROL_IDLE. The brief names it as one of the two tests that
   must go red for the mutation "skipping CONTROL_IDLE on the error path" --
   but as written it bound `controls` and never looked at it, so that mutation
   left it green (verified: with IDLE skipped on the error path, only test 6
   failed). A test that cannot fail against its named mutation is not
   finished, so the assertion the brief's mutation table already assumed was
   there is now actually there.

2. Three tests for `main()` were added (the brief specifies main but gives no
   tests for it). They matter for one specific reason: `main` is where the
   `--card` argument becomes both `Folder(device_id=...)` and the `card=`
   every `job_start` carries, and a `main` that hardcoded either to 0 would
   pass every WorkerSession test in this file while making the entire
   one-process-per-chip design a lie -- four workers all folding on chip 0,
   which is precisely the silent-fallback failure the hardware spike existed
   to rule out. They use a real `os.pipe()` for the event fd rather than a
   monkeypatched seam, so what they exercise is the actual `os.fdopen` path
   a spawned worker takes.

3. `_FakeFolder.fold` raises on any `BaseException` outcome, where the brief
   wrote `Exception`. `KeyboardInterrupt` does not subclass `Exception`, so
   under the brief's version the fake never raised the interrupt that
   `test_the_device_is_released_even_when_a_fold_was_in_flight` exists to
   inject -- it fell through and emitted a normal `job_done` instead. That
   test could therefore not pass against ANY implementation (observed:
   "Failed: DID NOT RAISE KeyboardInterrupt" against the finished worker),
   and the behaviour it is supposed to pin -- a chip released by a worker
   killed mid-fold, the ordinary case at booth shutdown -- had no coverage
   at all.
"""

import io
import json
import os
import sys

import pytest

import runner.worker as worker_mod
from runner.folder import FoldError
from runner.worker import WorkerSession, main
from runner.workers import CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY


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
        # BaseException, not Exception -- see departure #3 in the module
        # docstring. `isinstance(KeyboardInterrupt(), Exception)` is False,
        # so with the narrower check this fake quietly returned a SUCCESSFUL
        # fold for the one test whose entire subject is a worker interrupted
        # mid-fold.
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
    backstop and it must not be lost in the move.

    The CONTROL_IDLE assertion is this file's departure #1 (see the module
    docstring): without it this test is green against "skipping CONTROL_IDLE
    on the error path", the mutation the brief names it for.
    """
    folder = _FakeFolder(outcomes=[RuntimeError("contract violated")])
    session, events, controls = _session(folder)
    session.run([_fold("j1"), _fold("j2"), json.dumps({"cmd": "stop"})])
    assert [e["type"] for e in events].count("job_error") == 1
    assert len(folder.folds) == 2
    assert [c["type"] for c in controls].count(CONTROL_IDLE) == 2


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


# ---------------------------------------------------------------------------
# main(): argv -> a Folder on the right chip, stdin -> commands, EVENT_FD ->
# events. See departure #2 in the module docstring for why these exist.


class _RecordingFolder(_FakeFolder):
    """A _FakeFolder that records the device_id main constructed it with."""

    def __init__(self, device_id=0, model="protenix-v2"):
        super().__init__()
        self.device_id = device_id
        self.model = model


@pytest.fixture
def worker_main(monkeypatch):
    """Run main() with a fake Folder, a real pipe for the event fd, and a
    canned stdin. Returns (folders, events) -- the Folders main constructed,
    and every JSON line that came out of the event fd."""
    folders = []

    def _factory(device_id=0, model="protenix-v2"):
        folder = _RecordingFolder(device_id=device_id, model=model)
        folders.append(folder)
        return folder

    monkeypatch.setattr(worker_mod, "Folder", _factory)

    def _run(argv_card, lines):
        read_fd, write_fd = os.pipe()
        monkeypatch.setattr(sys, "stdin",
                            io.StringIO("".join(f"{line}\n" for line in lines)))
        # main owns write_fd from here: it wraps it with os.fdopen and closes
        # it on the way out, which is what gives the read below an EOF.
        rc = main(["--card", str(argv_card), "--event-fd", str(write_fd)])
        with os.fdopen(read_fd, "r") as stream:
            emitted = [json.loads(line) for line in stream if line.strip()]
        return rc, folders, emitted

    return _run


def test_main_gives_its_folder_and_its_folds_the_card_it_was_told_to_use(worker_main):
    """The one thing main must not get wrong. A `Folder(device_id=0)` or a
    `WorkerSession(..., card=0)` here would fold every job in the booth on
    chip 0 while four workers reported four different cards on the wire."""
    _rc, folders, events = worker_main(3, [_fold("j1"), json.dumps({"cmd": "stop"})])
    assert [f.device_id for f in folders] == [3]
    start = [e for e in events if e["type"] == "job_start"][0]
    assert start["card"] == 3


def test_main_writes_events_and_control_lines_to_the_event_fd(worker_main):
    """Both streams share the fd; the parent splits them with is_control."""
    _rc, _folders, events = worker_main(1, [_fold("j1"), json.dumps({"cmd": "stop"})])
    kinds = [e["type"] for e in events]
    assert kinds == [CONTROL_READY, "job_start", "job_done", CONTROL_IDLE]


def test_main_never_writes_a_single_byte_of_the_event_stream_to_stdout(capfd,
                                                                      worker_main):
    """fd 1 belongs to tt-metal's C++ logging, which writes to it during
    device bring-up and kernel compilation. An event stream sharing it would
    be shredded mid-line -- which is why EVENT_FD exists at all."""
    _rc, _folders, events = worker_main(2, [_fold("j1"), json.dumps({"cmd": "stop"})])
    assert events, "the event fd should have carried the whole stream"
    assert capfd.readouterr().out == ""
