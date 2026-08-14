"""What the daemon does when a chip overheats while three others are folding.

`CardPool`'s 85 C quarantine has existed since Phase 3a and has never fired in
anger: one chip folding never got close. The measured quad run is the closest
this code path has ever been -- **65.4-73.7 C at 1337-1350 MHz drawing
72-91 W**, against 12-17 W idle -- and four chips folding continuously all
conference day, unattended, in front of the public, is the first time it can
matter. `tests/unit/runner/test_cards.py` covers the class. Nothing covered
what the *daemon* does with it while three other folds are in flight.

Task 8 already ruled on the semantics and nothing here may contradict it:

    A hot card takes no NEW work, keeps the fold it already has, comes back
    by itself when it cools, and costs nothing -- no target failure, no
    retirement budget.

**Every test here drives the daemon's own `_telemetry_once()`** (via the
`telemetry` fixture below), never `daemon.cards.update()` plus `daemon._emit()`
by hand. That is not a stylistic preference. A test that emits the events
itself is testing its own loop: it stays green against a daemon that drops
every `card_state` its telemetry produces, which is precisely the failure that
would leave a 91 C chip looking healthy on screen for the rest of the day.
The one place the real thermal decision lives is `sample_tt_smi() ->
CardPool.update() -> _emit()`, and that is the seam these tests cut at.
"""

import pytest

import runner.daemon as mod
from runner.cards import CardState
from runner.queue import Job

from _daemonfakes import _FakePool, _daemon


def _hot(index, c=91.0):
    """A card over the 85 C guard, at the clock and power the quad run showed."""
    return CardState(index=index, board_type="p300c", temperature_c=c,
                     power_w=60.0, aiclk_mhz=1350.0)


def _cool(index, c=45.0):
    return CardState(index=index, board_type="p300c", temperature_c=c,
                     power_w=18.0, aiclk_mhz=800.0)


def _fill_queue(daemon, n=8):
    """More jobs than chips, so a pass is never limited by an empty queue."""
    for i in range(n):
        daemon.queue.submit(Job(job_id=f"j{i}", target_id=f"t{i}",
                                input_path=f"/p/t{i}.yaml"))


def _states(daemon):
    """Every `card_state` that actually reached the wire, in order."""
    return [(e["card"], e["state"]) for e in daemon.server.events
            if e["type"] == "card_state"]


@pytest.fixture
def telemetry(monkeypatch):
    """Feed one tt-smi sample through the daemon's real telemetry path.

    Replaces `runner.daemon.sample_tt_smi` (the daemon calls it with no
    arguments; the default keeps the signature honest) and then calls
    `_telemetry_once()`, so what is under test is the daemon's handling of a
    sample and not the test's own transcription of it.
    """
    sample = []
    monkeypatch.setattr(mod, "sample_tt_smi", lambda timeout=5.0: list(sample))

    def feed(daemon, cards):
        sample[:] = list(cards)
        daemon._telemetry_once()

    return feed


def test_a_chip_that_overheats_mid_fold_keeps_its_job(tmp_path, telemetry):
    """A fold in flight is not cancelled by heat.

    Tearing down a fold mid-device-op is a needless source of instability
    (runner/queue.py), and the chip is going to finish in seconds anyway.

    DEPARTURE from the brief's body, which asserted only `pool.busy_job(1)`
    after calling `daemon.cards.update(...)` directly. That version could not
    fail against the mutation it names: `CardPool.update` has no reference to
    the worker pool, so no edit to it can change what `pool.busy_job` answers,
    and the daemon -- the thing that would do the cancelling -- was not in the
    picture at all. The sample now goes through `_telemetry_once()`, and the
    assertions cover the three ways a cancellation would actually show:
    the pool's reservation, the daemon's own `_in_flight` record, and a
    `job_error` on the wire.
    """
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    _fill_queue(daemon)
    daemon.dispatch_once()
    # The worker announces the fold, which is what marks the card busy in
    # CardPool -- dispatch deliberately does not (Task 8's ruling).
    daemon.on_event(1, {"type": "job_start", "job_id": "j1",
                        "target_id": "t1", "n_residues": 20})
    assert pool.busy_job(1) is not None

    telemetry(daemon, [_hot(1)])

    assert pool.busy_job(1) is not None, "the chip still holds its fold"
    assert daemon._in_flight.get(1) == "t1", (
        "and the daemon still believes it is folding t1 -- a daemon that had "
        "given up on the fold would have cleared this")
    assert [e for e in daemon.server.events if e["type"] == "job_error"] == [], (
        "heat is not a fold failure; the UI must not be told this fold ended")
    assert 1 not in daemon.cards.schedulable(), (
        "it keeps the fold it has AND takes nothing new")


def test_a_hot_chip_receives_no_further_work_while_the_others_do(
        tmp_path, telemetry):
    """The point of the quarantine at four-up: three chips carry on.

    `pool.finish` frees only the POOL's reservation, which is what makes this
    test able to tell "card 1 is quarantined" from "card 1 is merely busy".
    """
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    _fill_queue(daemon)
    daemon.dispatch_once()
    telemetry(daemon, [_hot(1)])
    for card in (0, 1, 2, 3):
        pool.finish(card)

    daemon.dispatch_once()

    assert sorted({c for c, _j, _t in pool.dispatched[-3:]}) == [0, 2, 3]
    assert len(pool.dispatched) == 7, (
        "three of the four chips took work on the second pass, not four and "
        "not one")


def test_a_cooled_chip_comes_back_into_rotation(tmp_path, telemetry):
    """No intervention, no respawn, no operator: the next sample is enough.

    A quarantine that never lifted would take a quarter of the booth off the
    board for the rest of the conference day the first time a chip touched
    85 C.
    """
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    _fill_queue(daemon)
    telemetry(daemon, [_hot(1)])
    daemon.dispatch_once()
    assert 1 not in {c for c, _j, _t in pool.dispatched}, (
        "precondition: the hot chip got nothing on the first pass")

    telemetry(daemon, [_cool(1)])
    for card in (0, 2, 3):
        pool.finish(card)
    daemon.dispatch_once()

    assert 1 in {c for c, _j, _t in pool.dispatched}


def test_every_quarantine_transition_reaches_the_wire(tmp_path, telemetry):
    """The UI dims a quarantined chip. If the event never leaves the daemon, a
    hot chip looks healthy on screen for the rest of the day -- and the one
    person who would notice is not standing at the booth.

    Both directions, because a daemon that only forwarded the alarming half
    would leave a cell dimmed forever after the chip recovered.
    """
    daemon = _daemon(tmp_path, _FakePool())

    telemetry(daemon, [_hot(1)])
    assert (1, "quarantined") in _states(daemon)

    telemetry(daemon, [_cool(1)])
    assert (1, "idle") in _states(daemon)


def test_a_chip_that_stays_hot_does_not_re_announce_itself_every_sample(
        tmp_path, telemetry):
    """One event per TRANSITION, not one per sample.

    Arithmetic, at four-up: telemetry runs every TELEMETRY_PERIOD_S (2 s), so
    a booth with all four chips parked over the threshold for an eight-hour
    conference day would put ~57,600 `card_state` events on a socket whose
    other end is a GTK main loop that also has to draw four live ribbons. The
    de-duplication is in `CardPool.update`; nothing else in the chain would
    catch its loss.
    """
    daemon = _daemon(tmp_path, _FakePool())

    for _ in range(5):
        telemetry(daemon, [_hot(1)])
    assert _states(daemon) == [(1, "quarantined")]

    for _ in range(5):
        telemetry(daemon, [_cool(1)])
    assert _states(daemon) == [(1, "quarantined"), (1, "idle")]


def test_a_fold_finishing_on_a_still_hot_chip_is_not_reported_idle(
        tmp_path, telemetry):
    """The ordering that only exists once folds and heat overlap.

    A chip that went hot mid-fold and is still hot when that fold lands is
    quarantined, not idle -- `CardPool.mark_idle` answers None for exactly
    this, and the daemon must not invent an `idle` of its own. Reporting idle
    here would dim-then-undim the cell while the chip is still over the
    threshold, and would tell the UI the opposite of what the scheduler
    believes.
    """
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    _fill_queue(daemon)
    daemon.dispatch_once()
    daemon.on_event(1, {"type": "job_start", "job_id": "j1",
                        "target_id": "t1", "n_residues": 20})
    telemetry(daemon, [_hot(1)])

    daemon.on_event(1, {"type": "job_done", "job_id": "j1",
                        "cif_path": str(tmp_path / "s.cif"),
                        "wall_s": 4.4, "mean_plddt": 95.3})

    assert (1, "idle") not in _states(daemon), (
        "the fold ended, the heat did not")
    assert _states(daemon)[-1] == (1, "quarantined")
    assert 1 not in daemon.cards.schedulable()


def test_all_four_hot_idles_the_booth_without_stopping_it(tmp_path, telemetry):
    """"Idle calmly and log loudly rather than folding onto a card we have
    just decided is unsafe" -- now with four cards, and a daemon that must
    still be alive when they cool.

    "No schedulable cards" is an ordinary state of the world here, not an
    error: `dispatch_once` has to hand out nothing, raise nothing, and lose
    nothing from the queue.
    """
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    _fill_queue(daemon)
    telemetry(daemon, [_hot(i) for i in range(4)])

    daemon.dispatch_once()
    assert pool.dispatched == []
    assert len(daemon.queue) == 8, "and no job was taken and dropped"

    telemetry(daemon, [_cool(i) for i in range(4)])
    daemon.dispatch_once()
    assert len(pool.dispatched) == 4


def test_the_measured_live_quad_temperatures_quarantine_nothing(
        tmp_path, telemetry):
    """The headroom this guard actually has, pinned to measurement.

    A live four-chip quad run measured 65.4-73.7 C at 1337-1350 MHz drawing
    72-91 W. Those are the temperatures the booth will sit at all day, and
    they must leave every chip schedulable -- a threshold tightened to "feel
    safer" would quarantine the whole booth at its normal working temperature
    and the demo would spend the conference idle. The gap between 73.7 and
    85 is the entire safety margin, and it is small enough to be worth a test
    that notices if someone closes it.
    """
    daemon = _daemon(tmp_path, _FakePool())
    measured = [65.4, 69.1, 71.8, 73.7]

    telemetry(daemon, [CardState(index=i, board_type="p300c",
                                 temperature_c=c, power_w=w, aiclk_mhz=1350.0)
                       for i, (c, w) in enumerate(zip(measured,
                                                      (72.0, 80.0, 85.0, 91.0)))])

    assert daemon.cards.schedulable() == [0, 1, 2, 3]
    assert _states(daemon) == [], "nothing changed, so nothing was announced"
