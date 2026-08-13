"""A worker that dies must not take the booth down (Phase 5 Task 7).

The brief's twelve tests are all here. Seven of them are DEPARTURES from the
text, each because the test as written could not fail against the mutation it
is named for, or could pass by luck. Every departure is flagged `DEPARTURE`
in the test's own docstring with what was wrong and what replaced it; the
recurring shapes are:

1. **A barrier that is not a barrier.** `_wait(lambda: 1 not in
   pool.ready_cards())` (brief test 1) is already true before card 1 dies --
   card 1 is BUSY, and `ready_cards()` excludes busy cards -- so the
   assertions after it ran against a pool that might not have processed the
   death at all. A "stop the whole pool on any death" mutation goes red only
   if the death has actually been handled by the time we look.
2. **A wait that returns before the mutation can show.** `_wait(lambda:
   pool.spawns.count(0) == 1)` (brief test 11) is true the instant it is
   called -- the pool has spawned card 0 exactly once since `start()` -- so a
   pool that ignored `worker.fatal` and respawned 10 ms later passed it every
   time. Replaced with a NEGATIVE wait: watch for the extra spawn for far
   longer than the restart delay and require that it never comes.

3. **A cleanup somewhere else doing the test's work for it.** Brief test 4
   ("the chip is freed, not left marked busy") passed with the death path's
   `_busy[card] = None` DELETED, because `_spawn_worker` initialises
   `_busy[card]` for the worker it installs and the respawn was ten
   milliseconds away. It now runs on a pool whose respawn is half a minute
   away. Brief test 12 likewise passed with the retirement branch of
   `dispatch` deleted, because a retired card is also not-ready and the
   ordinary readiness check raised the same exception type.

The loops in brief tests 7, 8 and 9 have a further problem, a real race rather
than a weak assertion: they re-emit `worker.ready` on `pool.workers[0]`
immediately after a death, but the respawn that replaces `pool.workers[0]` is
`restart_delay_s` away, so the emit regularly landed on the corpse and the
next `assert _wait(...)` timed out. Those loops now wait for the respawn they
depend on.

Six tests are ADDED beyond the brief, each pinning something the twelve state
in prose and observe nowhere; see each one's docstring.
"""

import logging

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


def _die_and_respawn(pool, card, expected_spawns):
    """Kill card's worker and wait until its replacement is in place.

    The loops in brief tests 7/8/9 need this. `pool.workers[card]` is rebound
    by the fixture's `spawn`, so a test that emits on it before the respawn
    has landed is emitting into a corpse -- and then waits three seconds for a
    readiness that can never arrive. Waiting on the SPAWN COUNT rather than on
    `pool.workers[card] is not old` also catches the case the loops care
    about most: no respawn at all.
    """
    pool.workers[card].die()
    assert _wait(lambda: card not in pool.ready_cards())
    assert _wait(lambda: pool.spawns.count(card) == expected_spawns), (
        f"card {card} was not respawned: "
        f"{pool.spawns.count(card)} spawns, expected {expected_spawns}")


# ---------------------------------------------------------------------------
# The brief's twelve.
# ---------------------------------------------------------------------------

def test_a_dead_worker_does_not_stop_the_other_three(pool):
    """The headline requirement. Three chips keep folding.

    DEPARTURE. The brief's `_wait(lambda: 1 not in pool.ready_cards())` is
    ALREADY TRUE before card 1 dies -- card 1 was just dispatched to, and
    `ready_cards()` excludes busy cards -- so it returned instantly and the
    assertions below could run against a pool that had not yet noticed the
    death. Both named mutations (stopping the pool on a death; sweeping every
    card rather than the dying one) would then have had nothing to be red
    about. The barrier is now `pool.lost`, which is only written by the death
    path, and it doubles as the assertion that the sweep mutation reported
    card 0's live job as lost too.
    """
    pool.start()
    _ready(pool, 0, 1, 2, 3)
    pool.dispatch(_job("j0"), card=0)
    pool.dispatch(_job("j1"), card=1)
    pool.workers[1].die()
    assert _wait(lambda: pool.lost == [(1, "j1")]), pool.lost
    assert 1 not in pool.ready_cards()
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
    # "Exactly once" needs a second look after everything the death path does
    # has finished -- the respawn is the last of it.
    assert _wait(lambda: pool.spawns.count(0) == 2)
    assert pool.lost == [(0, "j0")]


def test_a_worker_that_dies_while_idle_orphans_nothing(pool):
    """A crash between jobs must not invent a failed job.

    DEPARTURE (addition). `pool.lost == []` is a MUST-NOT-HAPPEN, and the
    brief checks it the moment readiness clears -- which an implementation
    that reported the loss just after clearing would still pass. The respawn
    is strictly the last thing the death path does, so waiting for it makes
    the emptiness a fact about a finished death rather than a half-run one.
    """
    pool.start()
    _ready(pool, 0)
    pool.workers[0].die()
    assert _wait(lambda: 0 not in pool.ready_cards())
    assert _wait(lambda: pool.spawns.count(0) == 2)   # the death path is done
    assert pool.lost == []


def test_the_chip_is_freed_not_left_marked_busy(tmp_path):
    """A card left busy forever is one quarter of the booth gone silently.

    DEPARTURE, and this one was found by the mutation sweep rather than by
    reading. On the brief's fixture (`restart_delay_s=0.01`) this test PASSED
    with the death path's `self._busy[card] = None` deleted: `_spawn_worker`
    initialises `_busy[card]` to None for the worker it is about to install,
    so the respawn ten milliseconds later cleared the card anyway and the
    test never saw the window it exists to measure. It now runs on its own
    pool whose respawn is half a minute away, so the only thing that can free
    this card inside the test is the death path itself.
    """
    p, made, spawns = _build(tmp_path, restart_delay_s=30.0)
    p.start()
    try:
        made[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: p.ready_cards() == [0])
        p.dispatch(_job("j0"), card=0)
        assert p.busy_job(0) == "j0"                  # GUARD: it really is busy
        made[0].die()
        assert _wait(lambda: p.busy_job(0) is None)
        # GUARD: no respawn has happened, so nothing but the death path can
        # have cleared it.
        assert spawns.count(0) == 1
    finally:
        p.stop()


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
    time taking a device-init lock the other three workers need.

    DEPARTURE. The brief's loop emits `worker.ready` on `pool.workers[0]`
    straight after a death, but the respawn that rebinds `pool.workers[0]` is
    `restart_delay_s` away -- so the emit landed on the dead fake and the very
    next `assert _wait(...)` spent three seconds failing. `_die_and_respawn`
    waits for the replacement. It also turns the brief's single end-of-test
    spawn count into a per-iteration one, which is strictly stronger: it
    pins WHEN each respawn happened, not just how many there were.
    """
    pool.start()
    for death in range(1, WORKER_RETIRE_AFTER + 1):
        pool.workers[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: 0 in pool.ready_cards())
        if death < WORKER_RETIRE_AFTER:
            _die_and_respawn(pool, 0, expected_spawns=death + 1)
        else:
            # The last death is the one that must NOT be followed by a spawn.
            pool.workers[0].die()
            assert _wait(lambda: 0 not in pool.ready_cards())
    assert not _wait(lambda: pool.spawns.count(0) > WORKER_RETIRE_AFTER,
                     timeout=0.5), "a retired chip was respawned anyway"
    assert _wait(lambda: pool.spawns.count(0) == WORKER_RETIRE_AFTER)
    assert 0 not in pool.ready_cards()
    assert 0 in pool.cards, "a retired chip has not stopped existing"


def test_a_completed_job_resets_the_death_count(pool):
    """One bad fold followed by a crash is not a bad chip. Without this, a
    booth that loses one worker at 9am and another at 2pm retires a
    perfectly good card.

    DEPARTURE. Same respawn race as the test above, plus the brief's closing
    assertion (`0 in ready_cards() OR spawns.count(0) > WORKER_RETIRE_AFTER`)
    is weaker than the loop it follows: the reset is what lets EVERY death be
    respawned, so the exact count is the thing to assert. The mutation the
    brief warns about -- deleting the reset -- makes iteration
    `WORKER_RETIRE_AFTER + 1` fail inside `_die_and_respawn` with "card 0 was
    not respawned", which is the reset and nothing else.
    """
    pool.start()
    deaths = WORKER_RETIRE_AFTER + 2
    for death in range(1, deaths + 1):
        pool.workers[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: 0 in pool.ready_cards())
        pool.dispatch(_job("jx"), card=0)
        pool.workers[0].emit({"type": CONTROL_IDLE, "job_id": "jx"})
        assert _wait(lambda: pool.busy_job(0) is None)
        _die_and_respawn(pool, 0, expected_spawns=death + 1)
    # Every one of them was respawned: `start()` plus one per death.
    assert pool.spawns.count(0) == deaths + 1
    assert pool.spawns.count(0) > WORKER_RETIRE_AFTER
    # And the card is still usable, which is the point of not retiring it.
    pool.workers[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: 0 in pool.ready_cards())


def test_retiring_one_chip_leaves_the_others_alone(pool):
    """DEPARTURE: the same respawn race as the two tests above."""
    pool.start()
    _ready(pool, 1, 2, 3)
    for death in range(1, WORKER_RETIRE_AFTER + 1):
        pool.workers[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: 0 in pool.ready_cards())
        if death < WORKER_RETIRE_AFTER:
            _die_and_respawn(pool, 0, expected_spawns=death + 1)
        else:
            pool.workers[0].die()
            assert _wait(lambda: 0 not in pool.ready_cards())
    assert set(pool.ready_cards()) == {1, 2, 3}
    assert pool.any_ready()
    assert pool.spawns.count(1) == pool.spawns.count(2) == 1


def test_a_fatal_control_line_retires_without_waiting_for_three_deaths(pool):
    """The worker told us it cannot serve. Respawning it twice more to
    confirm is time the booth spends at three chips for no information.

    DEPARTURE. The brief's `_wait(lambda: pool.spawns.count(0) == 1)` is true
    the instant it is evaluated -- `start()` spawned card 0 exactly once --
    so it returned immediately and a pool that ignored `worker.fatal` (and
    respawned 10 ms later) passed this test every single time. The named
    mutation could not make it red. It is now a NEGATIVE wait: half a second,
    fifty times the restart delay, in which the second spawn must not appear.
    """
    pool.start()
    pool.workers[0].emit({"type": CONTROL_FATAL, "reason": "device lease held"})
    pool.workers[0].die()
    assert _wait(lambda: 0 not in pool.ready_cards())
    assert not _wait(lambda: pool.spawns.count(0) > 1, timeout=0.5), \
        "a worker that said worker.fatal was respawned"
    assert pool.spawns.count(0) == 1
    assert 0 not in pool.ready_cards()


def test_dispatching_to_a_retired_card_raises_rather_than_vanishing(pool):
    """A silently-dropped job is a target that never folds and never fails.

    DEPARTURE: `match="retired"`. A bare `pytest.raises(ValueError)` here
    cannot tell the retirement branch from the ordinary readiness check --
    a retired card is also not-ready, so `dispatch` raises either way and
    deleting the retirement branch entirely leaves this test green (verified
    in the sweep). The daemon requeueing this job onto another chip wants
    "this card is never coming back", not "not ready yet", so the message is
    part of what is under test.

    DEPARTURE 2, and the two go together: the brief's barrier here
    (`_wait(lambda: 0 not in pool.ready_cards())`) is true before the pool has
    read a single line -- card 0 never announced ready in the first place --
    so `dispatch` ran against a pool that had not yet seen the fatal line at
    all, and got "not ready yet" from the plain readiness check. Card 0 is
    made dispatchable first, so losing it is something the barrier can
    actually observe.
    """
    pool.start()
    _ready(pool, 0)                                   # GUARD: it WAS dispatchable
    pool.workers[0].emit({"type": CONTROL_FATAL, "reason": "x"})
    pool.workers[0].die()
    assert _wait(lambda: 0 not in pool.ready_cards())
    with pytest.raises(ValueError, match="retired"):
        pool.dispatch(_job("j1"), card=0)
    assert pool.workers[0].commands == [], \
        "a fold command was written to a retired card's dead worker"


def test_all_four_dying_does_not_raise_out_of_the_pool(pool):
    """The booth is now unable to fold. It must say so (Task 8's not_ready),
    not crash -- an unattended booth needs a process that stays up."""
    pool.start()
    _ready(pool, 0, 1, 2, 3)
    for card in (0, 1, 2, 3):
        pool.workers[card].die()
    assert _wait(lambda: pool.ready_cards() == [])
    assert not pool.any_ready()


# ---------------------------------------------------------------------------
# ADDED beyond the brief -- see each docstring for the hole it fills.
# ---------------------------------------------------------------------------

def _build(tmp_path, cards=(0,), **kwargs):
    """One pool with the fixture's plumbing, for tests that need their own.

    Returns `(pool, made, spawns)`. The fixture above is the brief's, verbatim
    down to its `on_worker_lost` signature, so the tests that need a different
    callback or a different restart delay build their own here rather than
    quietly changing the one twelve other tests depend on.
    """
    made, spawns = {}, []

    def spawn(spec, env):
        w = _FakeWorker(spec, env)
        made[spec.card] = w
        spawns.append(spec.card)
        return w

    kwargs.setdefault("restart_delay_s", 0.01)
    p = WorkerPool([_spec(c) for c in cards], on_event=lambda c, e: None,
                   log_root=str(tmp_path), spawn=spawn, **kwargs)
    return p, made, spawns


def test_the_loss_names_the_target_the_pool_already_had(tmp_path):
    """ADDED. `on_worker_lost(card, job_id, target_id)` has three arguments
    for a stated reason -- 'the pool has the whole Job from dispatch, so it
    can name the target without the daemon looking it up'. The brief's
    fixture drops the third (`lambda c, j, t: lost.append((c, j))`), so all
    twelve tests pass against a pool that passes `None`, or the job_id, or
    the card's label as the target -- and Task 8 puts that value on the wire
    as the thing that failed to fold.

    Catches: `on_worker_lost(card, job.job_id, None)`, and a two-argument
    call (which would be a TypeError swallowed by `_report_loss`, leaving the
    daemon never told at all).
    """
    lost = []
    p, made, _ = _build(tmp_path, on_worker_lost=lambda c, j, t: lost.append(
        (c, j, t)))
    p.start()
    try:
        made[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: p.ready_cards() == [0])
        p.dispatch(_job("j4", target_id="villin"), card=0)
        made[0].die()
        assert _wait(lambda: lost), "the loss was never reported"
        assert lost == [(0, "j4", "villin")]
    finally:
        p.stop()


def test_the_card_is_already_undispatchable_when_the_loss_is_reported(tmp_path):
    """ADDED, and it pins this task's one deliberate departure from the
    brief's numbered order (report, THEN mark not-ready). Task 8's
    `on_worker_lost` requeues the orphan and then looks for a free chip. If
    the dying card still looked ready at that instant, the orphaned job would
    be dispatched straight back into the pipe whose far end is the corpse
    that just dropped it -- and would be lost again, forever, one card at a
    time.

    Catches: clearing `_ready`/`_busy` after the callback instead of before.
    """
    seen = []
    holder = {}

    def on_worker_lost(card, job_id, target_id):
        p = holder["pool"]
        seen.append((p.ready_cards(), p.busy_job(card), p.any_ready()))

    p, made, _ = _build(tmp_path, cards=(0, 1), on_worker_lost=on_worker_lost)
    holder["pool"] = p
    p.start()
    try:
        for card in (0, 1):
            made[card].emit({"type": CONTROL_READY})
        assert _wait(lambda: p.ready_cards() == [0, 1])
        p.dispatch(_job("j0"), card=0)
        made[0].die()
        assert _wait(lambda: seen), "the loss was never reported"
        ready_then, busy_then, any_then = seen[0]
        assert 0 not in ready_then, \
            "card 0 still looked dispatchable while its loss was being reported"
        assert busy_then is None, "card 0 was still marked busy"
        # ... and card 1, which is fine, was still available to take the
        # requeued job. That is the whole reason the daemon is being told.
        assert ready_then == [1]
        assert any_then
    finally:
        p.stop()


def test_a_raising_on_worker_lost_does_not_cost_the_chip_its_respawn(tmp_path):
    """ADDED. `on_worker_lost` is the DAEMON's code running on this pool's
    reader thread, exactly like `on_event`. An exception escaping it must not
    end the death path half-done: that would turn one lost job into one dark
    chip for the rest of the conference day, and the booth is unattended.

    Catches: calling `on_worker_lost` outside a try/except.
    """
    def on_worker_lost(card, job_id, target_id):
        raise RuntimeError("the daemon has a bug")

    p, made, spawns = _build(tmp_path, on_worker_lost=on_worker_lost)
    p.start()
    try:
        made[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: p.ready_cards() == [0])
        p.dispatch(_job("j0"), card=0)
        made[0].die()
        assert _wait(lambda: spawns.count(0) == 2), \
            "a raising on_worker_lost swallowed the respawn"
        made[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: p.ready_cards() == [0])
    finally:
        p.stop()


def test_stop_does_not_race_a_new_worker_onto_a_chip(tmp_path):
    """ADDED. A death during shutdown schedules a respawn, and `stop()`
    snapshots the workers it intends to terminate. A process created after
    that snapshot is one nothing will ever reap -- 'never leave a process
    holding a device', which on this shared machine is the standing rule.

    The restart delay here is long enough that the respawn is provably still
    pending when `stop()` is called, so this measures the shutdown guard and
    not a race that happened to resolve the convenient way.

    Catches: a respawn that only checks `_stopping` before its wait, or not
    at all; and a respawn scheduled on a timer that outlives `stop()`.
    """
    p, made, spawns = _build(tmp_path, restart_delay_s=0.3)
    p.start()
    made[0].emit({"type": CONTROL_READY})
    assert _wait(lambda: p.ready_cards() == [0])
    made[0].die()
    # GUARD: the death has been handled and the respawn is still pending.
    assert _wait(lambda: 0 not in p.ready_cards())
    assert spawns.count(0) == 1
    p.stop()
    assert not _wait(lambda: spawns.count(0) > 1, timeout=1.0), \
        "stop() left a respawn in flight and a worker was created after it"
    assert all(not w.alive for w in made.values())


def test_a_stale_eof_does_not_disturb_the_worker_that_replaced_it(tmp_path):
    """ADDED. The identity check in `_worker_exited` is what makes a respawn
    safe: the dead worker's reader thread and the new worker's are both
    alive for a moment, and an EOF attributed to the CARD rather than to the
    HANDLE would free a chip that had just started folding -- reporting a
    perfectly live job as lost and spawning a third process onto the chip.

    Defensive today (one reader thread runs `_worker_exited` once, after the
    replacement is in place), which is exactly why it is worth pinning: a
    guard nothing exercises is a guard that quietly stops working.

    Catches: `if card not in self._workers: return`, and no check at all.
    """
    p, made, spawns = _build(tmp_path)
    p.start()
    old = made[0]
    try:
        old.die()
        assert _wait(lambda: spawns.count(0) == 2)
        new = made[0]
        assert new is not old                          # GUARD: really replaced
        new.emit({"type": CONTROL_READY})
        assert _wait(lambda: p.ready_cards() == [0])
        p.dispatch(_job("j5"), card=0)

        p._worker_exited(0, old)                       # the late, stale EOF

        assert p.busy_job(0) == "j5", "a stale EOF orphaned a live job"
        assert p.any_ready()
        assert spawns.count(0) == 2, "a stale EOF spawned a third process"
    finally:
        p.stop()


def test_a_failed_respawn_is_retried_rather_than_leaving_the_chip_dark(
        tmp_path, caplog):
    """ADDED. A spawn that raises leaves no process, so no EOF will ever
    bring the pool back to this card: one transient EMFILE or ENOMEM at the
    wrong moment and that chip is dark until the daemon restarts. The retry
    is bounded by the same `WORKER_RETIRE_AFTER` a crash loop is, so a chip
    that genuinely cannot be spawned still stops being retried.

    Catches: a respawn that gives up on a failed `_spawn_worker`.
    """
    made, spawns = {}, []

    def spawn(spec, env):
        spawns.append(spec.card)
        if len(spawns) == 2:                 # the first RESPAWN, not the first spawn
            raise OSError("EMFILE: too many open files")
        made[spec.card] = _FakeWorker(spec, env)
        return made[spec.card]

    caplog.set_level(logging.INFO, logger="runner.pool")
    p = WorkerPool([_spec(0)], on_event=lambda c, e: None,
                   log_root=str(tmp_path), spawn=spawn, restart_delay_s=0.01)
    p.start()
    first = made[0]
    try:
        first.die()
        assert _wait(lambda: made[0] is not first), \
            "the chip stayed dark after one failed respawn"
        assert len(spawns) == 3              # start, the failure, the retry
        made[0].emit({"type": CONTROL_READY})
        assert _wait(lambda: p.ready_cards() == [0])
        # The failed attempt counted as a death, so the retry budget really is
        # bounded: one more death now retires the card rather than looping.
        made[0].die()
        assert _wait(lambda: 0 not in p.ready_cards())
        assert not _wait(lambda: len(spawns) > 3, timeout=0.5), \
            "a card past its retirement budget was respawned anyway"
    finally:
        p.stop()
