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
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from string import Template

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


# ---------------------------------------------------------------------------
# SIGTERM: a polite shutdown must still close the device.
#
# `runner/pool.py`'s `terminate()` closes the worker's stdin (the clean path
# `iter(sys.stdin.readline, "")` exits through) and then sends SIGTERM. A
# worker mid-fold is not reading stdin at all, so it never sees that EOF --
# it can ONLY be reached by the signal. Without a handler, a Python child
# installs no SIGTERM disposition of its own, so the OS's default (terminate
# immediately) kills the process without running so much as one more line of
# Python -- in particular, WITHOUT running `WorkerSession.run`'s
# `finally: self.folder.close()`. The chip is then released by the kernel
# tearing the process's fds down, not by tt-bio's own `cleanup()`.
#
# This matters beyond tidiness: earlier the same booth needed `tt-smi -r`
# after a device was left in a state where every subsequent open failed
# ("Timed out while waiting for active ethernet core ... to become active
# again"), and that reset is under a standing prohibition here. A shutdown
# path that relies on the kernel to free four chips at once is a plausible
# way to reproduce that four times over on a machine nobody can reset.
#
# These tests drive `main()` in a REAL, separate subprocess rather than
# in-process. That is not paranoia -- it is required: raising SystemExit from
# a signal handler only unwinds the MAIN THREAD of the process that actually
# received the signal, and the only way to prove a real SIGTERM travels
# through a real handler into a real `finally` is to send a real signal to a
# process that is not the one running pytest. Sending SIGTERM to the test
# runner itself to "prove" this would risk aborting the whole suite instead
# of failing one test.
#
# "Fold in progress" and "close in progress" are made externally observable
# through marker files the fake Folder writes, so the parent test never
# guesses at timing with a bare sleep-and-hope: it waits for the marker,
# THEN sends the signal, so the signal provably lands inside the state named
# in each test.

_SIGTERM_WORKER_TEMPLATE = Template('''\
import sys
import time

sys.path.insert(0, $repo_root)

import runner.worker as worker_mod

_CLOSE_MARKER = $close_marker
_CLOSING_MARKER = $closing_marker
_FOLD_STARTED_MARKER = $fold_started_marker
_FOLD_HANG_S = $fold_hang_s
_CLOSE_HANG_S = $close_hang_s


class _SignalTestFolder:
    """Stands in for runner.folder.Folder -- no device, no ttnn, no torch.
    Its only job is to make "a fold is in progress" and "close is in
    progress" observable from OUTSIDE this process, deterministically."""

    def __init__(self, device_id=0, model="protenix-v2"):
        self.device_id = device_id

    def load(self):
        pass

    def close(self):
        with open(_CLOSING_MARKER, "w") as fh:
            fh.write("closing")
        # Gives a test room to fire a SECOND signal while unwind is already
        # under way, without the two SIGTERMs racing each other.
        time.sleep(_CLOSE_HANG_S)
        with open(_CLOSE_MARKER, "w") as fh:
            fh.write("closed")

    def fold(self, job_id, input_path, emit, *, target_id, n_residues, card=0,
             n_step=200):
        emit({"type": "job_start", "job_id": job_id, "target_id": target_id,
              "model": "protenix-v2", "card": card, "n_residues": n_residues})
        with open(_FOLD_STARTED_MARKER, "w") as fh:
            fh.write("started")
        # A real fold spends nearly all of this time inside a ttnn/torch C
        # call this test cannot reach or interrupt -- that is a real,
        # permanent limit on any Python-level fix (see the SIGTERM handler's
        # own docstring). What sleep() stands in for honestly is the
        # ordinary case of the interpreter sitting in PYTHON bytecode
        # between device ops -- a progress callback, msa/prep bracketing --
        # which is exactly where a delivered signal's handler gets to run.
        # If this sleep ever completes and job_done gets emitted, the
        # process was NOT interrupted: that is this test's failure mode,
        # not a disguised success.
        time.sleep(_FOLD_HANG_S)
        emit({"type": "job_done", "job_id": job_id, "cif_path": "/tmp/x.cif",
              "wall_s": _FOLD_HANG_S, "mean_plddt": 90.0})


worker_mod.Folder = _SignalTestFolder
sys.exit(worker_mod.main(sys.argv[1:]))
''')

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _readline_within(stream, seconds=5.0):
    """One `readline()` on a raw stream, bounded. `None` if it never came
    back -- unbounded reads on a worker's event fd are exactly how a
    mutation that hangs (rather than fails) the fix would go unnoticed."""
    out = []
    reader = threading.Thread(target=lambda: out.append(stream.readline()),
                              daemon=True)
    reader.start()
    reader.join(timeout=seconds)
    return out[0] if out else None


def _wait_for_marker(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def sigterm_worker(tmp_path):
    """Spawn one real `runner.worker` child (a real subprocess; a fake,
    marker-writing Folder) per call, and make sure none of them survives the
    test -- a stray child spinning in `time.sleep()` after a failed
    assertion is exactly the kind of leftover process this whole fix exists
    to prevent."""
    procs = []

    def _make(*, fold_hang_s=30.0, close_hang_s=0.0):
        close_marker = tmp_path / f"closed-{len(procs)}.marker"
        closing_marker = tmp_path / f"closing-{len(procs)}.marker"
        fold_started_marker = tmp_path / f"fold-started-{len(procs)}.marker"
        script_path = tmp_path / f"sigterm_worker_{len(procs)}.py"
        script_path.write_text(_SIGTERM_WORKER_TEMPLATE.substitute(
            repo_root=repr(str(_REPO_ROOT)),
            close_marker=repr(str(close_marker)),
            closing_marker=repr(str(closing_marker)),
            fold_started_marker=repr(str(fold_started_marker)),
            fold_hang_s=repr(fold_hang_s),
            close_hang_s=repr(close_hang_s)))

        read_fd, write_fd = os.pipe()
        # sys.executable, not a bare "python3": whatever interpreter is
        # running this test suite (venv-runner's, per scripts/test.sh) is
        # the one the child should run under too -- the same choice
        # runner/pool.py's own _SubprocessWorker makes for the production
        # spawn.
        proc = subprocess.Popen(
            [sys.executable, str(script_path), "--card", "0",
             "--event-fd", str(write_fd)],
            stdin=subprocess.PIPE, pass_fds=(write_fd,), text=True)
        # The parent must not keep the write end open, or EOF on `events`
        # never arrives even after the child exits.
        os.close(write_fd)
        events = os.fdopen(read_fd, "r")
        procs.append(proc)
        return proc, events, close_marker, closing_marker, fold_started_marker

    yield _make

    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)


def _dispatch_fold(proc, job_id="j1"):
    proc.stdin.write(json.dumps({"cmd": "fold", "job_id": job_id,
                                 "target_id": "trpcage",
                                 "input_path": "/p/trpcage.yaml",
                                 "n_residues": 10}) + "\n")
    proc.stdin.flush()


def test_sigterm_mid_fold_closes_the_folder_before_exiting(sigterm_worker):
    """The central claim of this fix. Mutation this catches: no SIGTERM
    handler at all -- today's code. Verified red in the report: the process
    dies (proc.wait() returns quickly) but `close_marker` never appears,
    because the OS's default disposition never runs a single further line of
    Python."""
    proc, _events, close_marker, _closing, fold_started = sigterm_worker(
        fold_hang_s=30.0)
    _dispatch_fold(proc)

    assert _wait_for_marker(fold_started, timeout=5.0), \
        "the fake fold never signalled it had started"

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pytest.fail("worker did not exit within 5s of a mid-fold SIGTERM "
                    "(it may have ignored the signal entirely)")

    assert close_marker.exists(), (
        "Folder.close() never ran -- the worker exited without releasing "
        "its device")


def test_sigterm_while_idle_also_closes_the_folder(sigterm_worker):
    """A worker that has announced `worker.ready` and is blocked in
    `readline()` on stdin -- not folding at all -- must ALSO close the
    device on SIGTERM. This is what tells apart "the fold path alone was
    patched" from a handler installed once for the whole process, effective
    no matter what Python happens to be doing when the signal lands."""
    proc, events, close_marker, _closing, _fold_started = sigterm_worker(
        fold_hang_s=30.0)

    ready_line = _readline_within(events)
    assert ready_line, "the worker never announced worker.ready"
    assert json.loads(ready_line)["type"] == CONTROL_READY

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pytest.fail("worker did not exit within 5s of an idle SIGTERM")

    assert close_marker.exists(), (
        "Folder.close() never ran -- an idle worker was killed by the "
        "kernel instead of exiting through its own cleanup")


def test_a_second_sigterm_during_unwind_does_not_abort_the_close(sigterm_worker):
    """Idempotency. A second SIGTERM landing while the FIRST is already
    inside `folder.close()` must not raise again there: `close()` is guarded
    by `except Exception`, not `except BaseException` (SystemExit
    deliberately passes through the FIRST time, matching KeyboardInterrupt),
    so a second raise from inside it would escape that guard and abort
    cleanup partway through -- observable here as `_CLOSING_MARKER` existing
    but `_CLOSE_MARKER` never appearing, i.e. a truncated close.

    Mutation this catches: a handler that raises on every call rather than
    only the first."""
    proc, _events, close_marker, closing_marker, fold_started = sigterm_worker(
        fold_hang_s=30.0, close_hang_s=1.0)
    _dispatch_fold(proc)

    assert _wait_for_marker(fold_started, timeout=5.0), \
        "the fake fold never signalled it had started"

    proc.send_signal(signal.SIGTERM)
    assert _wait_for_marker(closing_marker, timeout=5.0), \
        "the worker never entered close() after the first SIGTERM"

    # The unwind is now provably inside close()'s own 1s sleep. A second
    # SIGTERM here is the scenario a slow/parked close can actually produce
    # at booth shutdown (an operator or runner/pool.py's own escalation
    # sending another signal before the first has finished).
    proc.send_signal(signal.SIGTERM)

    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pytest.fail("worker did not exit within 5s of a second, "
                    "mid-close SIGTERM")

    assert close_marker.exists(), (
        "close() was aborted partway through by a second SIGTERM -- "
        "closing_marker exists but close_marker does not, so cleanup "
        "started but never finished")


def test_constructing_a_worker_session_does_not_touch_sigterm_disposition():
    """The handler belongs to `main()` -- the worker CHILD's entry point --
    never to `WorkerSession` or to import time. `runner.pool.WorkerPool`
    (the parent) never calls `main()` in-process; it always spawns a
    genuinely separate `python -m runner.worker` (see
    `runner.pool._SubprocessWorker`). If `WorkerSession` itself (or
    importing this module) reinstalled SIGTERM, every test in THIS file that
    drives `WorkerSession` directly in the pytest process would be silently
    rewriting the test runner's own signal disposition -- and so would the
    real daemon, which imports `runner.worker` only for tests, never for
    production use, but must never be put at risk if that ever changed."""
    before = signal.getsignal(signal.SIGTERM)
    folder = _FakeFolder()
    session, _e, _c = _session(folder)
    session.run([json.dumps({"cmd": "stop"})])
    assert signal.getsignal(signal.SIGTERM) is before


# ---------------------------------------------------------------------------
# The easter egg command (runner/egg.py). Not a fold, and the point of most
# of these tests is that it cannot be mistaken for one.
# ---------------------------------------------------------------------------

class _FakeEgg:
    """Stands in for `runner.egg.run_egg`, which needs a real chip."""

    def __init__(self, outcome=None):
        self.outcome = outcome
        self.calls = []

    def __call__(self, device, emit, *, egg_id, card, seed=None):
        self.calls.append((device, egg_id, card, seed))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        emit({"type": "egg_frame", "egg_id": egg_id, "card": card,
              "step": 1, "total": 1, "seed": 7, "coords_b64": "AAA="})
        return 7


class _FolderWithDevice(_FakeFolder):
    def __init__(self, device="the-chip", **kw):
        super().__init__(**kw)
        self.device = device


def _install_egg(monkeypatch, fake):
    """Replace `run_egg` where the worker imports it from.

    `WorkerSession._egg` does `from runner.egg import run_egg` INSIDE the
    method (ttnn is not something the parent's tests may import), so the name
    has to be patched on the module the import resolves against.
    """
    import runner.egg as egg_module
    monkeypatch.setattr(egg_module, "run_egg", fake)


def test_an_egg_command_runs_on_this_workers_own_chip(monkeypatch):
    """`card` and the device handle both come from this worker, not from the
    command. A worker that took either from the wire could be told to report
    a chip it is not holding -- the exact class of lie the whole
    one-process-per-chip design exists to make impossible."""
    fake = _FakeEgg()
    _install_egg(monkeypatch, fake)
    folder = _FolderWithDevice()
    session, events, controls = _session(folder, card=3)

    session.run([json.dumps({"cmd": "egg", "egg_id": "e1", "seed": None})])

    assert fake.calls == [("the-chip", "e1", 3, None)]
    assert [e["type"] for e in events] == ["egg_frame"]
    assert events[0]["card"] == 3


def test_an_egg_emits_no_job_start_and_no_job_done(monkeypatch):
    """It is not a fold. A `job_start` would light a chip cell in the UI and
    put "now folding" under the protein; a `job_done` would start a showcase
    dwell over a structure that does not exist.

    Mutation this catches: implementing the egg by reusing `Folder.fold`'s
    event bracket.
    """
    _install_egg(monkeypatch, _FakeEgg())
    session, events, _controls = _session(_FolderWithDevice(), card=0)
    session.run([json.dumps({"cmd": "egg", "egg_id": "e1"})])
    assert not [e for e in events
                if e["type"] in ("job_start", "job_done", "stage")]


def test_an_egg_frees_the_worker_afterwards(monkeypatch):
    """`worker.idle` is the authoritative dispatch signal. Without it the
    chip is marked busy in the pool for the rest of the day.

    Mutation this catches: skipping CONTROL_IDLE on the egg path.
    """
    _install_egg(monkeypatch, _FakeEgg())
    session, _events, controls = _session(_FolderWithDevice(), card=1)
    session.run([json.dumps({"cmd": "egg", "egg_id": "e1"})])
    assert controls[-1] == {"type": CONTROL_IDLE, "card": 1, "job_id": "e1"}


def test_an_egg_that_fails_is_refused_out_loud_and_the_worker_survives(
        monkeypatch):
    """A visitor is standing in front of the screen waiting. Silence would
    cost them the UI's whole device-wait timeout before the fallback started
    -- and, far worse, an unhandled exception here would take the chip out of
    the booth for a toy.
    """
    _install_egg(monkeypatch, _FakeEgg(RuntimeError("ttnn said no")))
    folder = _FolderWithDevice()
    session, events, controls = _session(folder, card=2)

    session.run([json.dumps({"cmd": "egg", "egg_id": "e1"}),
                 json.dumps({"cmd": "fold", "job_id": "j1",
                             "target_id": "trpcage", "input_path": "/x.yaml"})])

    refusals = [e for e in events if e["type"] == "egg_refused"]
    assert refusals == [{"type": "egg_refused", "egg_id": "e1",
                         "reason": "device", "message": "ttnn said no"}]
    assert controls[-1]["job_id"] == "j1", "the worker must still serve folds"
    assert folder.folds == [("j1", "trpcage", 2)]


def test_an_egg_before_load_is_refused_rather_than_crashing(monkeypatch):
    """`Folder.device` is None until `load()` succeeds. Reaching ttnn with it
    would be a `NoneType` traceback out of a worker that was serving fine."""
    _install_egg(monkeypatch, _FakeEgg())
    folder = _FolderWithDevice(device=None)
    session, events, _controls = _session(folder, card=0)
    session.run([json.dumps({"cmd": "egg", "egg_id": "e1"})])
    assert [e["type"] for e in events] == ["egg_refused"]
    assert events[0]["reason"] == "device"
