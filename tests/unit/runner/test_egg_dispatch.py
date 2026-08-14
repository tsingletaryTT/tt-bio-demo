"""The easter egg's claim on the hardware: who gets a chip, and who is told no.

The egg (runner/egg.py) is a toy on a machine whose whole point is folding
proteins, so almost every test here is about what it must NOT be able to do:
take a chip from a fold in flight, take a place in the job queue, quarantine a
target, get a chip that is too hot, or leave a UI waiting with nothing on
screen and no answer.

The one thing it IS allowed to do is take the next chip that is already free
for about a second and a half, which is what these tests pin.
"""

import pytest

from runner.cards import CardState
from runner.daemon import EGG_WAIT_S
from runner.queue import Job

from _daemonfakes import _daemon, _FakePool


def _egg(egg_id="e1"):
    return {"type": "egg", "version": 3, "egg_id": egg_id}


class _EggPool(_FakePool):
    """`_FakePool` plus the one method the egg needs.

    A subclass rather than an edit to the shared fake: every other daemon
    test asserts against a pool that has no `dispatch_egg` at all, and that is
    worth keeping -- it is what proves the fold path never grew a dependency
    on the egg's.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.eggs = []
        self.egg_attempts = []
        self.refuse_egg = False

    def dispatch_egg(self, egg_id, card, *, seed=None):
        self.egg_attempts.append(card)
        if self.refuse_egg:
            raise ValueError("nope")
        if card not in self.ready_cards():
            raise ValueError(f"card {card} is not ready")
        self._busy[card] = egg_id
        self.eggs.append((card, egg_id, seed))


def _refusals(daemon):
    return [e for e in daemon.server.events if e["type"] == "egg_refused"]


# ── the request ─────────────────────────────────────────────────────────────


def test_an_egg_request_is_recorded_and_a_pick_is_not(tmp_path):
    """The handler is deliberately narrow: `pick` is Task 9 and answering it
    with half an implementation would be worse than not answering it at all.

    Mutation this catches: a handler that treats every client message the
    same, which would silently make a visitor's tap run an easter egg.
    """
    daemon = _daemon(tmp_path, _EggPool())
    daemon.on_client_message({"type": "pick", "version": 3,
                              "target_id": "trpcage"})
    assert daemon._egg_request is None
    daemon.on_client_message(_egg())
    assert daemon._egg_request[0] == "e1"


def test_a_second_press_replaces_the_first(tmp_path):
    """A visitor pressing the chord twice means "do it again". The UI keys
    its frames on `egg_id`, so an older request that later won a chip would
    stream into a card that is no longer showing it."""
    daemon = _daemon(tmp_path, _EggPool())
    daemon.on_client_message(_egg("first"))
    daemon.on_client_message(_egg("second"))
    assert daemon._egg_request[0] == "second"
    daemon._dispatch_egg()
    assert [e[1] for e in daemon.pool.eggs] == ["second"]


# ── who gets a chip ─────────────────────────────────────────────────────────


def test_a_waiting_egg_gets_the_first_free_chip(tmp_path):
    daemon = _daemon(tmp_path, _EggPool())
    daemon.on_client_message(_egg())
    assert daemon._dispatch_egg() == 0
    assert daemon.pool.eggs == [(0, "e1", None)]
    assert daemon._egg_request is None


def test_the_egg_never_takes_a_chip_that_is_already_folding(tmp_path):
    """The line this must not cross. Three chips are mid-fold; the egg gets
    the fourth and nothing else moves.

    Mutation this catches: dispatching the egg to `cards.schedulable()`
    without intersecting it with the pool's own readiness -- which would
    write a fold command's worth of bytes into a pipe whose worker is busy.
    """
    pool = _EggPool()
    for card in (0, 1, 2):
        pool.dispatch(Job(job_id=f"j{card}", target_id="trpcage",
                          input_path="/x.yaml"), card)
    daemon = _daemon(tmp_path, pool)
    daemon.on_client_message(_egg())
    assert daemon._dispatch_egg() == 3
    assert pool.busy_job(0) == "j0"
    assert pool.busy_job(1) == "j1"
    assert pool.busy_job(2) == "j2"


def test_an_egg_waits_for_a_chip_rather_than_refusing_immediately(tmp_path):
    """The booth folds continuously, so "every chip is busy" is the NORMAL
    state and refusing on the spot would mean the egg essentially never ran
    on hardware. It waits `EGG_WAIT_S` for the next card to come free.

    Mutation this catches: refusing as soon as no card is free.
    """
    pool = _EggPool(ready=[])
    daemon = _daemon(tmp_path, pool)
    daemon.on_client_message(_egg(), now=100.0)
    assert daemon._dispatch_egg(now=100.0 + EGG_WAIT_S / 2) is None
    assert _refusals(daemon) == []
    assert daemon._egg_request is not None, "the request must survive the wait"


def test_a_chip_that_frees_up_inside_the_wait_gets_the_egg(tmp_path):
    pool = _EggPool()
    for card in pool.cards:
        pool.dispatch(Job(job_id=f"j{card}", target_id="t", input_path="/x"),
                      card)
    daemon = _daemon(tmp_path, pool)
    daemon.on_client_message(_egg(), now=100.0)
    assert daemon._dispatch_egg(now=101.0) is None
    pool.finish(2)
    assert daemon._dispatch_egg(now=101.5) == 2
    assert _refusals(daemon) == []


def test_a_busy_booth_refuses_the_egg_once_its_patience_runs_out(tmp_path):
    """And the refusal says WHICH kind of no it is. The UI puts a different
    sentence on the card for "the booth is working" than for "something went
    wrong", and it can only do that if the daemon distinguishes them.
    """
    daemon = _daemon(tmp_path, _EggPool(ready=[]))
    daemon.on_client_message(_egg(), now=100.0)
    assert daemon._dispatch_egg(now=100.0 + EGG_WAIT_S + 0.01) is None
    assert _refusals(daemon) == [{"type": "egg_refused", "egg_id": "e1",
                                  "reason": "busy"}]
    assert daemon._egg_request is None, "a refused egg must not be retried"


def test_a_hot_chip_is_not_offered_to_the_egg(tmp_path):
    """The thermal guard is not something a toy gets an exception from. A
    card over `max_temp_c` drops out of `CardPool.schedulable()`, and the egg
    checks the same two gates a fold does.

    Mutation this catches: `_free_card_for_egg` reading `pool.ready_cards()`
    only, which is exactly the shortcut "it's only a few milliseconds of
    arithmetic" would justify -- onto the one chip the guard is protecting.
    """
    daemon = _daemon(tmp_path, _EggPool(cards=[0, 1]))
    daemon.cards.update([
        CardState(index=0, board_type="p150", temperature_c=95.0,
                  power_w=100.0, aiclk_mhz=1000.0),
        CardState(index=1, board_type="p150", temperature_c=40.0,
                  power_w=100.0, aiclk_mhz=1000.0)])
    daemon.on_client_message(_egg())
    assert daemon._dispatch_egg() == 1


def test_a_card_that_refuses_the_egg_is_reported_not_retried(tmp_path):
    """The same window `dispatch_once` guards for a fold: the card stopped
    being dispatchable between the readiness check and the write. There is
    nothing to requeue -- an egg is not a job -- so the visitor is told."""
    pool = _EggPool()
    pool.refuse_egg = True
    daemon = _daemon(tmp_path, pool)
    daemon.on_client_message(_egg())
    assert daemon._dispatch_egg() is None
    assert _refusals(daemon)[0]["reason"] == "busy"
    assert daemon._egg_request is None


# ── the egg and the fold loop ───────────────────────────────────────────────


def test_the_egg_is_dispatched_before_folds_and_its_chip_is_not_reused(tmp_path):
    """One pass of `dispatch_once` with an egg waiting and four free chips:
    the egg takes one, three folds take the rest, and NOTHING is sent twice.

    Mutation this catches: reading `ready_cards()` before dispatching the
    egg, which leaves the egg's card in the fold loop's snapshot and sends a
    fold command to a worker that has just been given an egg.
    """
    daemon = _daemon(tmp_path, _EggPool())
    for i in range(6):
        daemon.queue.submit(Job(job_id=f"j{i}", target_id=f"t{i}",
                                input_path="/x.yaml"))
    daemon.on_client_message(_egg())
    daemon.dispatch_once()
    egg_card = daemon.pool.eggs[0][0]
    fold_cards = [card for card, _job, _t in daemon.pool.dispatched]
    assert egg_card not in fold_cards
    assert sorted(fold_cards + [egg_card]) == [0, 1, 2, 3]


def test_no_egg_means_dispatch_once_behaves_exactly_as_before(tmp_path):
    """The egg is an addition, not a change: with nothing waiting, every free
    chip still gets a fold."""
    daemon = _daemon(tmp_path, _EggPool())
    for i in range(4):
        daemon.queue.submit(Job(job_id=f"j{i}", target_id=f"t{i}",
                                input_path="/x.yaml"))
    daemon.dispatch_once()
    assert sorted(c for c, _j, _t in daemon.pool.dispatched) == [0, 1, 2, 3]
    assert daemon.pool.eggs == []


def test_an_egg_never_enters_the_job_queue(tmp_path):
    """It is not a job and must never be scheduled like one -- a queued egg
    would sit in front of a visitor's pick once Task 9 lands."""
    daemon = _daemon(tmp_path, _EggPool())
    daemon.on_client_message(_egg())
    daemon._dispatch_egg()
    assert len(daemon.queue) == 0


# ── when it goes wrong ──────────────────────────────────────────────────────


def test_a_worker_that_dies_running_an_egg_reports_a_refused_egg(tmp_path):
    """NOT a `job_error`. The UI's `job_error` branch takes down the "now
    folding X" caption, and no fold was running -- so a `job_error` here
    would make a dying easter egg look like a dying protein.

    Mutation this catches: dropping the `_egg_on_card` check from
    `on_worker_lost`.
    """
    daemon = _daemon(tmp_path, _EggPool())
    daemon.on_client_message(_egg())
    card = daemon._dispatch_egg()
    daemon.on_worker_lost(card, "e1", None)
    kinds = [e["type"] for e in daemon.server.events]
    assert "job_error" not in kinds
    assert _refusals(daemon)[-1] == {"type": "egg_refused", "egg_id": "e1",
                                     "reason": "device"}


def test_a_dying_egg_costs_no_target_a_failure(tmp_path):
    """A toy must never be able to quarantine a protein. Three worker deaths
    on the same target is what quarantines it, and an egg's death is not one
    of them."""
    daemon = _daemon(tmp_path, _EggPool())
    for _ in range(4):
        daemon.on_client_message(_egg())
        card = daemon._dispatch_egg()
        daemon.on_worker_lost(card, "e1", None)
        daemon.pool.finish(card)
        daemon._egg_settled(card)
    assert daemon._failures == {}
    assert daemon._quarantined == set()


def test_a_fold_dying_on_a_chip_that_ran_an_egg_earlier_is_still_a_fold(tmp_path):
    """The stale-state test. `_egg_on_card` is what makes the branch above
    fire, so a run that is over must clear it -- otherwise the NEXT fold to
    die on that chip is reported as a refused egg and the UI sits in
    `folding` forever.

    Mutation this catches: never clearing `_egg_on_card`.
    """
    daemon = _daemon(tmp_path, _EggPool())
    daemon.on_client_message(_egg())
    card = daemon._dispatch_egg()
    # The egg's last frame comes back off that card's worker.
    daemon.on_event(card, {"type": "egg_frame", "egg_id": "e1", "card": card,
                           "step": 180, "total": 180, "seed": 1,
                           "coords_b64": ""})
    daemon.pool.finish(card)

    daemon.on_worker_lost(card, "j9", "trpcage")
    kinds = [e["type"] for e in daemon.server.events]
    assert "job_error" in kinds
    assert daemon._failures == {"trpcage": 1}


def test_an_egg_frame_reaches_the_ui_unchanged(tmp_path):
    """The daemon adds no fields and rewrites none, exactly as for a fold's
    events -- and it does NOT mark the card busy, because the UI's chip cells
    mean "folding" and this chip is not folding."""
    daemon = _daemon(tmp_path, _EggPool())
    frame = {"type": "egg_frame", "egg_id": "e1", "card": 2, "step": 4,
             "total": 180, "seed": 77, "coords_b64": "AAA="}
    daemon.on_event(2, dict(frame))
    assert frame in daemon.server.events
    assert not any(e["type"] == "card_state" for e in daemon.server.events)
