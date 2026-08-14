"""The pool: spawn, dispatch, multiplex (Phase 5 Task 6).

The thirteen tests the plan's brief specifies are here verbatim in intent --
`_FakeWorker`, `_spec`, `_job` and `_wait` moved into `_workerfakes.py` as
the brief directs, so Task 7's death tests import the same fake.

NINE tests are added beyond the brief, each because something the brief
states in prose was pinned by none of its thirteen. They are marked
`ADDED.` below with the mutation each one exists to catch. In order of how
much they matter:

1. `test_a_parked_callback_on_one_card_does_not_stall_another` -- the
   brief's Step 2 says "one reader thread per worker ... which is what keeps
   a slow chip from blocking the other three", and NOTHING in the thirteen
   observes it. A pool with ONE thread round-robining `readline()` over four
   handles passes all thirteen and wedges the booth the first time a chip is
   slow; so does a pool that forwards events while holding its own lock.
2. `test_a_card_is_not_dispatchable_after_stop` -- "never hand work to a
   dead worker". The thirteen check that `stop()` terminates the workers,
   never that the pool stops considering them dispatchable afterwards.
3. `test_the_booth_can_still_fold_while_every_ready_card_is_busy` -- pins
   `any_ready()` as "at least one worker has announced ready", NOT
   "`ready_cards()` is non-empty". Task 8 gates `hello` vs `not_ready` on
   it, so the naive definition would report a fully-busy booth as not ready
   to every UI that connected mid-fold.
4. `test_each_worker_gets_the_whole_worker_environment` -- brief test 2
   checks one variable, which a pool that hand-rolled
   `{"TT_VISIBLE_DEVICES": ...}` also passes, losing the per-card log tree
   and the four-way host thread cap.
5. `test_each_chip_is_spawned_exactly_once` -- brief test 1 reads a dict
   keyed by card, so spawning card 0 twice is invisible to it.
6. `test_a_flood_of_junk_lines_is_logged_at_most_once_per_interval` -- the
   multiplexing contract says junk is dropped with a RATE-LIMITED log, and
   the `clock=` constructor argument exists for exactly that; neither is
   exercised by the thirteen.
7. `test_an_unknown_event_type_is_dropped_rather_than_forwarded` -- the
   contract's first clause is "a line whose type is in EVENT_TYPES", not
   "any line that is not control".
8. `test_the_pool_lists_every_card_it_manages` -- `.cards` is in the brief's
   produced API and is asserted by Task 7's tests, not by Task 6's.
9. `test_the_real_worker_handle_speaks_over_its_own_fd` -- `spawn=None` is
   the production path and the thirteen never touch it, so an fd that is
   not inherited or a parent that keeps the pipe's write end open would be
   discovered on hardware. Driven against a shell script standing in for
   the interpreter: no device, no tt-bio, no torch.
"""

import json
import logging
import os
import threading
from pathlib import Path

import pytest

from runner.pool import WorkerPool
from runner.workers import CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY

from _workerfakes import _FakeWorker, _job, _spec, _wait


@pytest.fixture
def pool(tmp_path):
    made, spawns = {}, []

    def spawn(spec, env):
        worker = _FakeWorker(spec, env)
        made[spec.card] = worker
        spawns.append(spec.card)
        return worker

    p = WorkerPool([_spec(c) for c in (0, 1, 2, 3)], on_event=lambda c, e: None,
                   log_root=str(tmp_path), spawn=spawn)
    # Test handles. The pool keeps its own bookkeeping under private names
    # (`_workers` and friends), so these cannot silently overwrite production
    # state and leave a test asserting against what it just clobbered.
    p.workers = made
    p.spawns = spawns
    yield p
    p.stop()


# ---------------------------------------------------------------------------
# The brief's thirteen.
# ---------------------------------------------------------------------------

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
    dropped, and the only symptom is a UI that never hears about something.

    DEPARTURE from the brief, which says to verify this test by deleting the
    `is_control` check: as written it could not go red against that
    deletion. This pool also drops anything whose type is not in
    `EVENT_TYPES`, and `worker.ready` is not, so an unrouted control line
    falls out of the multiplexer instead of reaching `on_event` -- the
    callback still sees exactly `["job_done"]`. The readiness assertion
    below is what makes the deletion visible here: a control line that was
    never CONSUMED is one the pool never acted on, and card 0 never becomes
    dispatchable. Verified both ways -- see this task's report.
    """
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
        # Consumed, not merely absent: the pool ACTED on both control lines.
        assert _wait(lambda: p.ready_cards() == [0])
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


# ---------------------------------------------------------------------------
# ADDED beyond the brief -- see this module's docstring for why each exists.
# ---------------------------------------------------------------------------

def test_a_parked_callback_on_one_card_does_not_stall_another(tmp_path):
    """ADDED. One reader thread per worker, and `on_event` called OUTSIDE the
    pool's lock. A slow chip -- or a slow UI, since `on_event` ends up in
    `EventServer.broadcast`, which may block up to `client_send_timeout` per
    client -- must not stop the other three chips reporting or becoming
    dispatchable.

    Catches: one reader thread round-robining `readline()` over four handles
    (which blocks on card 0 forever), and forwarding events while holding
    `WorkerPool._lock` (which blocks card 1's control line instead).
    """
    parked = threading.Event()
    release = threading.Event()
    seen, made = [], {}

    def spawn(spec, env):
        made[spec.card] = _FakeWorker(spec, env)
        return made[spec.card]

    def on_event(card, event):
        seen.append((card, event["type"]))
        if card == 0:
            parked.set()
            release.wait(timeout=5.0)

    p = WorkerPool([_spec(c) for c in (0, 1)], on_event=on_event,
                   log_root=str(tmp_path), spawn=spawn)
    p.start()
    try:
        made[0].emit({"type": "frame", "job_id": "j0", "step": 1, "total": 2,
                      "n_atoms": 3, "coords_b64": "AAAA"})
        # GUARD: without this the rest of the test proves nothing -- it would
        # be measuring an unblocked pool against an unblocked pool.
        assert parked.wait(timeout=3.0), "card 0's callback never parked"
        assert not release.is_set()

        made[1].emit({"type": CONTROL_READY})
        made[1].emit({"type": "job_start", "job_id": "j1", "target_id": "t",
                      "model": "protenix-v2", "card": 1, "n_residues": 20})
        assert _wait(lambda: (1, "job_start") in seen), \
            "card 1's event never arrived while card 0 was parked"
        assert _wait(lambda: p.ready_cards() == [1]), \
            "card 1 never became dispatchable while card 0 was parked"
        # Still parked at the moment of the assertions above, i.e. card 1 was
        # genuinely served concurrently rather than after card 0 finished.
        assert parked.is_set() and not release.is_set()
    finally:
        release.set()
        p.stop()


def test_a_card_is_not_dispatchable_after_stop(tmp_path):
    """ADDED. 'Never hand work to a dead worker.' `stop()` terminates the
    subprocesses; a pool that left them marked ready would happily write a
    fold command into a pipe whose far end is a corpse.

    Catches: `stop()` that tears down processes without clearing readiness.

    Written the first time with an ordinary `_FakeWorker` and it COULD NOT
    FAIL: terminating that fake reaches EOF, the reader thread runs the death
    path, and the death path clears readiness -- so the test measured the EOF
    path and never the line it names. It now drives the schedule with a
    worker that ignores both `terminate()` and `kill()` (a real one whose
    event pipe is held open by an inherited fd behaves exactly this way), so
    the reader thread is still parked when `stop()` returns and the explicit
    clearing is the only thing left that can be under test. The clock jumps,
    so the two bounded joins inside `stop()` collapse instead of costing this
    test six real seconds.
    """
    made = {}

    class _Zombie(_FakeWorker):
        def terminate(self):
            self.terminated = True        # ... and does NOT die

        def kill(self):
            self.killed = True            # ... and still does not die

    def spawn(spec, env):
        made[spec.card] = _Zombie(spec, env)
        return made[spec.card]

    ticks = iter(range(0, 100_000, 10))
    p = WorkerPool([_spec(0)], on_event=lambda c, e: None,
                   log_root=str(tmp_path), spawn=spawn,
                   clock=lambda: float(next(ticks)))
    p.start()
    try:
        made[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: p.ready_cards() == [0])  # GUARD: it WAS dispatchable
        p.stop()
        # GUARD: the worker never reached EOF, so nothing but stop() itself
        # can have made the card undispatchable below.
        assert made[0].alive and made[0].terminated
        assert p.ready_cards() == []
        assert not p.any_ready()
        with pytest.raises(ValueError):
            p.dispatch(_job("j1"), card=0)
        assert made[0].commands == []
    finally:
        made[0].die()                     # release the parked reader thread


def test_the_booth_can_still_fold_while_every_ready_card_is_busy(pool):
    """ADDED. `any_ready()` answers 'can this booth fold at all', which Task 8
    turns into `hello` vs `not_ready` for every UI that connects. A booth
    whose one ready chip is mid-fold is folding -- reporting `not_ready`
    there would blank the screen for the length of every fold.

    Catches: `any_ready()` defined as `bool(self.ready_cards())`.
    """
    pool.start()
    assert not pool.any_ready()                       # GUARD: nothing ready yet
    pool.workers[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [0])
    assert pool.any_ready()
    pool.dispatch(_job("j1"), card=0)
    assert pool.ready_cards() == []                   # GUARD: it really is busy
    assert pool.any_ready()


def test_each_worker_gets_the_whole_worker_environment(pool, monkeypatch):
    """ADDED. The pool builds each child's environment with
    `workers.worker_environ`, not by hand. Two things ride on that and
    neither is visible to the brief's TT_VISIBLE_DEVICES check: one
    `TT_METAL_LOGS_PATH` per card (four writers into one tree makes a crash
    unattributable and lets the pruner delete another worker's evidence),
    and a host thread cap sized for FOUR co-resident workers rather than one
    (each child otherwise sizes its torch/OMP/BLAS pools to every core).

    Catches: a hand-rolled `{"TT_VISIBLE_DEVICES": ...}`, and passing
    `n_workers=1`.
    """
    from tt_bio.runtime import host_thread_cap
    solo, four_up = host_thread_cap(1), host_thread_cap(4)
    # GUARD: on a single-core box these collapse and the OMP assertion below
    # would pass against n_workers=1. Loud, not skipped.
    assert solo != four_up, (
        f"this box cannot tell n_workers=1 ({solo}) from n_workers=4 "
        f"({four_up}); the thread-cap assertion below is vacuous here")
    # An ambient value would be honoured by worker_environ's setdefault, which
    # is deliberate but would make this test read the operator's environment
    # instead of the pool's decision.
    monkeypatch.delenv("TT_METAL_LOGS_PATH", raising=False)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)

    pool.start()
    for card, worker in pool.workers.items():
        assert worker.env["TT_METAL_LOGS_PATH"].endswith(f"card-{card}")
        assert worker.env["OMP_NUM_THREADS"] == str(four_up)
    # And the file the parent holds open for each worker's stdout/stderr sits
    # inside that same per-card tree. Task 11's janitor protects these BY
    # PATH, so a pool that reported them from somewhere other than where the
    # children actually write would protect the wrong files.
    assert pool.worker_log_paths == [
        str(Path(pool.workers[card].env["TT_METAL_LOGS_PATH"]) / "worker.log")
        for card in (0, 1, 2, 3)]


def test_each_chip_is_spawned_exactly_once(pool):
    """ADDED. The brief's test 1 reads a dict keyed by card, so a pool that
    spawned card 0 twice -- two processes contending for one chip, the exact
    thing `TT_VISIBLE_DEVICES` pinning exists to prevent -- passes it.

    Catches: spawning per spec AND per card, or a start() that is not
    idempotent about what it has already spawned.
    """
    pool.start()
    assert pool.spawns == [0, 1, 2, 3]


def test_a_flood_of_junk_lines_is_logged_at_most_once_per_interval(tmp_path, caplog):
    """ADDED. The multiplexing contract's third clause is 'dropped with a
    RATE-LIMITED log'. tt-metal is loud and a worker that starts spraying
    fd 3 could otherwise write one warning per line into the same log root
    the janitor is trying to hold under a budget -- the failure this project
    has already paid for once (docs/followups.md, 13-14 MB/s).

    Catches: logging every bad line, and ignoring the injected `clock`.
    """
    made = {}
    now = [1000.0]

    def spawn(spec, env):
        made[spec.card] = _FakeWorker(spec, env)
        return made[spec.card]

    p = WorkerPool([_spec(0)], on_event=lambda c, e: None,
                   log_root=str(tmp_path), spawn=spawn, clock=lambda: now[0])
    caplog.set_level(logging.WARNING, logger="runner.pool")
    p.start()
    try:
        for i in range(20):
            made[0].emit_raw(f"Metal | INFO | line {i}\n")
        made[0].emit({"type": CONTROL_READY})
        # GUARD: the reader consumed all of it and is still alive, so the low
        # log count below is suppression rather than lines never read.
        assert _wait(lambda: p.ready_cards() == [0])
        assert made[0].drained
        records = [r for r in caplog.records if r.name == "runner.pool"]
        assert len(records) == 1, [r.getMessage() for r in records]
        assert "line 0" in records[0].getMessage()
    finally:
        p.stop()


def test_an_unknown_event_type_is_dropped_rather_than_forwarded(tmp_path):
    """ADDED. The contract forwards lines whose type is in `EVENT_TYPES`; it
    does not forward everything that merely fails the control-prefix check.
    An unknown type reaching `on_event` reaches `EventServer.broadcast`,
    where `encode` raises `ProtocolError` and the event is dropped anyway --
    one layer further from the card that produced it.

    Catches: `if is_control(...): continue` followed by an unconditional
    forward.
    """
    seen, made = [], {}

    def spawn(spec, env):
        made[spec.card] = _FakeWorker(spec, env)
        return made[spec.card]

    p = WorkerPool([_spec(0)], on_event=lambda c, e: seen.append(e),
                   log_root=str(tmp_path), spawn=spawn)
    p.start()
    try:
        made[0].emit({"type": "worker_debug", "note": "not a protocol event"})
        made[0].emit({"type": "job_done", "job_id": "j1", "cif_path": "/a.cif",
                      "wall_s": 4.4, "mean_plddt": 95.3})
        assert _wait(lambda: seen)
        assert made[0].drained                       # GUARD: both lines read
        assert [e["type"] for e in seen] == ["job_done"]
    finally:
        p.stop()


_STAND_IN_INTERPRETER = """#!/bin/sh
# Stands in for `python3 -m runner.worker`, so the real handle can be
# exercised with no tt-bio, no torch and no device. Parses --event-fd the
# same way the worker's argparse does, writes one control line to THAT fd
# (never stdout), is loud on stdout the way tt-metal's C++ is, echoes one
# command back, and exits.
fd=""
while [ $# -gt 0 ]; do
  case "$1" in
    --event-fd) fd="$2"; shift 2 ;;
    *) shift ;;
  esac
done
echo "Metal | INFO | opening device"
# Written through /dev/fd/N rather than `>&N`: POSIX sh only accepts a single
# DIGIT after `>&`, and the pipe this handle creates lands wherever the next
# free descriptor happens to be -- which under pytest is regularly 10 or
# higher. (That is a limitation of this stand-in, not of the worker, which
# just writes to the fd argparse handed it.)
printf '%s\\n' '{"type": "worker.ready", "card": 0}' > "/dev/fd/$fd"
read -r line
printf '%s\\n' "$line" > "/dev/fd/$fd"
exit 0
"""


def test_the_real_worker_handle_speaks_over_its_own_fd(tmp_path):
    """ADDED. `spawn=None` means `subprocess.Popen`, and until the hardware
    tasks nothing runs that path at all -- a typo in the argv, an fd that is
    not inherited, or a parent that keeps its copy of the pipe's write end
    would first be discovered on a booth in front of people.

    Exercised against a stand-in interpreter (a shell script), so this test
    opens no device and imports no tt-bio. It pins the four things the handle
    is actually responsible for: the event fd is a pipe named on the command
    line (`pass_fds` inherits fds at their OWN number, which is exactly why
    `runner.worker` takes `--event-fd` instead of hardcoding 3); stdout goes
    to that card's `worker.log`, not into the event stream; a command written
    to stdin arrives; and EOF is seen when the child exits -- which requires
    the parent to have closed its own copy of the write end.
    """
    from runner.pool import _SubprocessWorker

    interpreter = tmp_path / "stand-in-python"
    interpreter.write_text(_STAND_IN_INTERPRETER)
    interpreter.chmod(0o755)
    log_path = tmp_path / "card-0" / "worker.log"

    handle = _SubprocessWorker(_spec(0), {"PATH": os.environ["PATH"]},
                               log_path=log_path, python=str(interpreter))
    try:
        # Every read is bounded, and that is not defensiveness for its own
        # sake: the two ways this handle can be wrong -- naming the wrong fd
        # on the command line, and keeping the parent's copy of the write end
        # open -- both present as a `readline` that never returns. Bounded,
        # they fail this test; unbounded, they HANG it, which is a mutation
        # that cannot be watched go red.
        first = _readline_within(handle)
        assert first is not None, "the event fd never produced a line"
        assert json.loads(first)["type"] == CONTROL_READY
        handle.send({"cmd": "fold", "job_id": "j1"})
        echoed = _readline_within(handle)
        assert echoed is not None and json.loads(echoed)["job_id"] == "j1"
        assert _readline_within(handle) == "", \
            "the child exited but its stream never reached EOF"
        assert not handle.alive
        assert "Metal | INFO | opening device" in log_path.read_text()
    finally:
        handle.terminate()
        handle.kill()


def _readline_within(handle, seconds=5.0):
    """One `readline`, bounded. Returns None if it never came back."""
    out = []
    reader = threading.Thread(target=lambda: out.append(handle.readline()),
                              daemon=True)
    reader.start()
    reader.join(timeout=seconds)
    return out[0] if out else None


def test_the_pool_lists_every_card_it_manages(pool):
    """ADDED. `.cards` is 'every card the pool manages, retired ones
    included' -- it is what Task 8's `hello` reports as the booth's hardware
    inventory, so it must not narrow to whatever happens to be free.

    Catches: `cards` derived from `ready_cards()`.
    """
    pool.start()
    assert pool.cards == [0, 1, 2, 3]
    pool.workers[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [0])
    pool.dispatch(_job("j1"), card=0)
    assert pool.busy_job(0) == "j1"                   # GUARD: card 0 is busy
    assert pool.cards == [0, 1, 2, 3], "a card mid-fold has not stopped existing"
    # A worker that has declared itself unable to serve is likewise still a
    # card this pool manages (Task 7 retires it; it never leaves `.cards`).
    pool.workers[1].emit({"type": CONTROL_FATAL, "reason": "device lease held"})
    assert _wait(lambda: pool.workers[1].drained)     # GUARD: the line was read
    assert pool.cards == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# The easter egg's dispatch (runner/egg.py). It borrows a chip for about a
# second and a half, so it must reserve one exactly as a fold does -- and
# must not be able to cost a fold anything when it goes wrong.
# ---------------------------------------------------------------------------

def test_an_egg_is_sent_as_its_own_command_not_as_a_fold(pool):
    """The worker branches on `cmd`. An egg arriving as a `fold` would send a
    Folder looking for an input file that does not exist."""
    pool.start()
    pool.workers[1].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [1])
    pool.dispatch_egg("e1", card=1, seed=99)
    assert pool.workers[1].commands == [
        {"cmd": "egg", "egg_id": "e1", "seed": 99}]


def test_an_egg_reserves_its_card_exactly_as_a_fold_does(pool):
    """For the second or so it takes, the chip really is occupied.

    Mutation this catches: dispatching the egg without reserving, which would
    let the daemon's very next pass hand that same worker a fold -- two
    commands queued on a process that reads them one at a time, and a fold
    that does not start until the toy has finished.
    """
    pool.start()
    pool.workers[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [0])
    pool.dispatch_egg("e1", card=0)
    assert pool.ready_cards() == []
    assert pool.busy_job(0) == "e1"
    with pytest.raises(ValueError):
        pool.dispatch(_job("j1"), card=0)
    pool.workers[0].emit({"type": CONTROL_IDLE, "job_id": "e1"})
    assert _wait(lambda: pool.ready_cards() == [0])


def test_an_egg_is_refused_by_a_card_that_is_not_ready(pool):
    """The same exception `dispatch` raises, so the daemon has one thing to
    catch for "that chip would not take it"."""
    pool.start()
    with pytest.raises(ValueError):
        pool.dispatch_egg("e1", card=0)
    pool.workers[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: pool.ready_cards() == [0])
    pool.dispatch(_job("j1"), card=0)
    with pytest.raises(ValueError):
        pool.dispatch_egg("e1", card=0)


def test_an_egg_lost_with_its_worker_names_no_target(tmp_path):
    """The reservation an egg makes carries `target_id=None` on purpose: the
    daemon counts a worker death against the TARGET that was folding, and an
    egg is not a target. Three of these must not quarantine anything.

    Mutation this catches: reserving with a placeholder target_id (say
    "easter-egg"), which would silently accumulate failures against a name
    that is not in the playlist -- harmless today, and exactly the kind of
    thing that becomes a mysterious quarantine later.
    """
    made, lost = {}, []

    def spawn(spec, env):
        made[spec.card] = _FakeWorker(spec, env)
        return made[spec.card]

    p = WorkerPool([_spec(0)], on_event=lambda c, e: None,
                   log_root=str(tmp_path), spawn=spawn, restart_delay_s=30.0,
                   on_worker_lost=lambda *a: lost.append(a))
    try:
        p.start()
        made[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: p.ready_cards() == [0])
        p.dispatch_egg("e1", card=0)
        made[0].die()
        assert _wait(lambda: bool(lost))
        assert lost == [(0, "e1", None)]
    finally:
        p.stop()
