"""The daemon at four chips: what it dispatches, greets and reports.

`test_daemon.py` next door keeps everything that is not about multiplicity --
target quarantine, the playlist, the janitors, main()'s CLI. This file is
about the part the multi-chip phase changed: the parent owns no device, four
chips take work independently, and a worker dying is now a thing that happens
to one chip rather than to the booth.

The fifteen tests the plan specified are here under their own names, with the
plan's own docstrings. Everything after the "beyond the plan" banner was added
while implementing, and each says why.
"""

import pytest

from runner.daemon import Daemon, DaemonConfig
from runner.queue import Job

from _daemonfakes import _CollectingServer, _FakePool, _daemon, _run


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
    to agree before a job goes anywhere.

    DEPARTURE from the plan: the `pool.attempts` assertion is added. Without
    it this test cannot fail against the mutation it names. `WorkerPool`
    defends itself -- `dispatch` to a not-ready card raises, and the daemon
    requeues on exactly that exception -- so a daemon that ignored
    `ready_cards()` and sent to all four produces an IDENTICAL `dispatched`
    list and an identical queue. Verified: with `ready_cards()` replaced by
    `self.pool.cards`, the original body passed. Whether the daemon even
    *tried* is the only observable difference, so that is what is asserted.
    """
    pool = _FakePool(ready=[0, 1])
    daemon = _daemon(tmp_path, pool)
    for i in range(4):
        daemon.queue.submit(Job(job_id=f"j{i}", target_id=f"t{i}",
                                input_path=f"/p/t{i}.yaml"))
    daemon.dispatch_once()
    assert sorted(c for c, _j, _t in pool.dispatched) == [0, 1]
    assert sorted(pool.attempts) == [0, 1], (
        "a card whose worker cannot take work must not even be written to")


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
    existing.

    DEPARTURE from the plan: `daemon.cards.mark_busy(0)` is added. "Busy" is
    now tracked in two places for two different questions -- the pool's
    reservation and CardPool's wire state -- and the plan's body only ever set
    the pool's. That made the test blind to the more likely of the two
    mutations, `cards.all_indices()` replaced by `cards.schedulable()`, which
    left it green. Making card 0 busy in BOTH is what puts both forms of "only
    report the free ones" on the hook.
    """
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    pool._busy = {0: "j0", 1: "j1"}
    daemon.cards.mark_busy(0)
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
    _run(daemon)
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


# ===========================================================================
# Beyond the plan's fifteen. Each of these exists because a mutation survived
# all fifteen, or because a ruling this task was asked to make had nothing
# pinning it.
# ===========================================================================

def _start(card, job_id="j1", target_id="t"):
    return {"type": "job_start", "job_id": job_id, "target_id": target_id,
            "model": "protenix-v2", "card": card, "n_residues": 20}


def _done(job_id="j1", cif_path="/tmp/x.cif"):
    return {"type": "job_done", "job_id": job_id, "cif_path": cif_path,
            "wall_s": 4.4, "mean_plddt": 95.3}


def _error(job_id="j1", target_id="t"):
    return {"type": "job_error", "job_id": job_id, "target_id": target_id,
            "message": "boom"}


def _states(daemon):
    return [(e["card"], e["state"]) for e in daemon.server.events
            if e["type"] == "card_state"]


# --- the two-owners rule, which Task 10 depends on -------------------------

def test_dispatching_does_not_itself_mark_the_card_busy(tmp_path):
    """Two facts, two owners: the POOL reserves the chip at dispatch (which
    is what stops a second job going to it in the same pass), and CardPool
    reports it busy only once its worker says the fold has actually started.

    Pinned because the obvious implementation -- mark_busy() right next to
    pool.dispatch() -- passes every one of the plan's fifteen tests above and
    then quietly breaks the booth: CardPool's busy flag would never be
    cleared by anything the pool does, so after the first pass schedulable()
    is empty forever and the second fold never happens.
    """
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    for i in range(4):
        daemon.queue.submit(Job(job_id=f"j{i}", target_id=f"t{i}",
                                input_path=f"/p/t{i}.yaml"))
    daemon.dispatch_once()
    assert len(pool.dispatched) == 4, "guard: the dispatch must have happened"
    assert daemon.cards.schedulable() == [0, 1, 2, 3], (
        "dispatch reserves the chip in the pool, not in CardPool")
    assert _states(daemon) == [], "and puts no card_state on the wire"

    # ... and then a worker announcing its fold is what does mark it busy.
    daemon.on_event(0, _start(card=0, job_id="j0"))
    assert daemon.cards.schedulable() == [1, 2, 3]
    assert (0, "busy") in _states(daemon)


def test_a_finished_fold_frees_its_card_for_the_next_pass(tmp_path):
    """The other half of the same rule: without this the booth folds exactly
    four targets and then reports every chip busy for the rest of the day."""
    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    daemon.on_event(1, _start(card=1))
    assert 1 not in daemon.cards.schedulable(), "guard: the card was claimed"
    daemon.on_event(1, _done())
    assert 1 in daemon.cards.schedulable()
    assert (1, "idle") in _states(daemon)


def test_a_worker_event_reaches_the_wire_unchanged(tmp_path):
    """The pool passes protocol events through untouched and the daemon is
    the only thing that talks to the socket -- so if the daemon drops or
    rewrites one, nothing else will notice."""
    daemon = _daemon(tmp_path, _FakePool())
    frame = {"type": "frame", "job_id": "j1", "step": 3, "coords_xyz": [1.0]}
    daemon.on_event(2, frame)
    assert frame in daemon.server.events


def test_a_card_bookkeeping_failure_does_not_cost_the_event(tmp_path):
    """on_event runs on a pool reader thread. The screen needs the event far
    more than it needs the card_state that accompanies it, so a CardPool bug
    must cost the bookkeeping and not the frame."""
    class _ExplodingBusy:
        def mark_busy(self, index):
            raise RuntimeError("boom")

        def mark_idle(self, index):
            raise RuntimeError("boom")

        def all_indices(self):
            return [0]

    daemon = _daemon(tmp_path, _FakePool())
    daemon.cards = _ExplodingBusy()
    daemon.on_event(0, _start(card=0))      # must not raise
    daemon.on_event(0, _done())             # must not raise
    assert [e["type"] for e in daemon.server.events] == ["job_start", "job_done"]


# --- the target failure counter, driven from the wire ----------------------

def test_a_workers_job_error_counts_against_its_target(tmp_path):
    """The fold now fails in another process, so the only evidence the daemon
    has that a target is bad is the job_error coming back off the pipe. If
    that is not counted, QUARANTINE_AFTER can never be reached by an ordinary
    fold failure -- only by a worker actually dying."""
    daemon = _daemon(tmp_path, _FakePool())
    for n in range(3):
        daemon.on_event(0, _start(card=0, job_id=f"j{n}", target_id="bad"))
        daemon.on_event(0, _error(job_id=f"j{n}", target_id="bad"))
    assert "bad" in daemon._quarantined


def test_a_target_that_recovers_is_not_quarantined(tmp_path):
    """Two failures then a success must reset the count, not creep toward
    three over a whole conference day."""
    daemon = _daemon(tmp_path, _FakePool())
    for n in range(2):
        daemon.on_event(0, _start(card=0, job_id=f"j{n}", target_id="flaky"))
        daemon.on_event(0, _error(job_id=f"j{n}", target_id="flaky"))
    daemon.on_event(0, _start(card=0, job_id="j2", target_id="flaky"))
    daemon.on_event(0, _done(job_id="j2"))
    daemon.on_event(0, _start(card=0, job_id="j3", target_id="flaky"))
    daemon.on_event(0, _error(job_id="j3", target_id="flaky"))
    assert "flaky" not in daemon._quarantined


# --- the quarantine ruling this task was asked to make ---------------------

def test_heat_costs_a_target_no_failure_and_a_card_no_retirement(
        tmp_path, monkeypatch):
    """The ruling, pinned. `CardPool`'s 85C quarantine has existed since
    Phase 3a and has never once fired in anger; four chips folding
    continuously is the first time it can. It means exactly one thing -- no
    NEW work goes to that chip -- and it is not a failure of anything:

    - no target is blamed (nothing about a hot chip says the target it
      happened to be folding is bad), and
    - no worker is retired (retirement is for the session and is counted in
      runner/pool.py against processes that DIE; a chip that is merely warm
      has a perfectly healthy worker and comes back by itself once telemetry
      says it has cooled).

    Getting this backwards in either direction is a booth that loses a
    quarter of its hardware for the day the first time a chip gets warm.

    Driven through the daemon's own `_telemetry_once()`, not by calling
    `cards.update()` and `_emit()` by hand: a test that emits the events
    itself is testing its own loop, and would stay green against a daemon that
    dropped every card_state its telemetry produced.
    """
    import runner.daemon as mod
    from runner.cards import CardState

    def _at(index, celsius):
        return CardState(index=index, board_type="p300c",
                         temperature_c=celsius, power_w=60.0, aiclk_mhz=1350.0)

    sample = [_at(1, 91.0)]
    monkeypatch.setattr(mod, "sample_tt_smi", lambda timeout=5.0: list(sample))

    pool = _FakePool()
    daemon = _daemon(tmp_path, pool)
    daemon.on_event(1, _start(card=1, job_id="j1", target_id="hot-target"))
    daemon._telemetry_once()

    assert daemon._failures == {}, "heat is not the target's fault"
    assert daemon._quarantined == set()
    assert pool.stopped == 0 and pool.dispatched == [], (
        "and the daemon asks the pool for nothing at all on a heat event")
    assert (1, "quarantined") in _states(daemon), (
        "the UI dims a hot chip; if the event never leaves, it looks healthy")

    # It comes back on its own, with no intervention and no respawn.
    daemon.on_event(1, _done(job_id="j1"))
    sample[:] = [_at(1, 45.0)]
    daemon._telemetry_once()
    assert 1 in daemon.cards.schedulable()


# --- the loss path, in the shapes the pool can actually produce ------------

def test_a_lost_worker_that_never_named_its_target_still_frees_the_card(tmp_path):
    """runner/pool.py calls on_worker_lost with whatever Job it held; a
    worker that died before the daemon recorded a target is a None here, and
    a None must cost the chip nothing."""
    daemon = _daemon(tmp_path, _FakePool())
    daemon.cards.mark_busy(3)
    daemon.on_worker_lost(card=3, job_id="j9", target_id=None)
    assert 3 in daemon.cards.schedulable()
    assert daemon._failures == {}, "there is nothing to blame"
    assert [e["type"] for e in daemon.server.events].count("job_error") == 1


def test_a_lost_worker_does_not_requeue_the_job_that_killed_it(tmp_path):
    """Deliberate, and the opposite of what runner/pool.py's own docstring
    guessed Task 8 would do. Immediately resubmitting the target that has
    just killed a worker is how a crash loop gets built out of a policy meant
    to survive one death; the attract loop re-enqueues the playlist whenever
    the queue drains, so an ordinary target comes back around anyway."""
    daemon = _daemon(tmp_path, _FakePool())
    daemon.on_worker_lost(card=0, job_id="j1", target_id="poison")
    assert daemon.queue.pending == []


def test_one_dead_chip_does_not_cost_the_other_three_their_pass(tmp_path):
    """A requeue on a dispatch race must `continue`, not `break`. With four
    chips, breaking out of the pass means one dead worker idles the whole
    booth until the next poll -- and if it stays dead, forever."""
    class _OneBadCard(_FakePool):
        def dispatch(self, job, card):
            if card == 0:
                raise ValueError("card 0's worker is gone")
            super().dispatch(job, card)

    pool = _OneBadCard()
    daemon = _daemon(tmp_path, pool)
    for i in range(4):
        daemon.queue.submit(Job(job_id=f"j{i}", target_id=f"t{i}",
                                input_path=f"/p/t{i}.yaml"))
    daemon.dispatch_once()
    assert sorted(c for c, _j, _t in pool.dispatched) == [1, 2, 3]
    assert len(daemon.queue.pending) == 1, "the refused job is kept, not lost"


# --- what run() builds when nobody has injected a pool ---------------------

class _RecordingPool(_FakePool):
    """Captures the constructor arguments `run()` builds a real pool with."""

    def __init__(self, specs, on_event, *, log_root, on_worker_lost=None,
                 **kwargs):
        super().__init__(cards=[s.card for s in specs])
        self.specs = specs
        self.built_on_event = on_event
        self.built_on_worker_lost = on_worker_lost
        self.log_root = log_root


def _spec(card):
    from runner.workers import WorkerSpec
    return WorkerSpec(card=card, label=f"h:tt:{card}", visible_devices=str(card),
                      logical_device_id=0, mesh_graph_descriptor=None)


def _run_with_a_built_pool(tmp_path, monkeypatch, device_ids=None):
    """Drive run() once, with worker_specs and WorkerPool replaced.

    Nothing here spawns a process or enumerates a device: the point is to see
    what run() *asks* for.

    The daemon is stopped from inside the fake pool's own `start()` rather
    than before `run()` is called. Pre-stopping would not do: `run()`
    deliberately declines to build a pool at all once `stop()` has been seen
    (spawning four worker processes onto chips during shutdown is a chip
    nobody will close a device for), so a pre-stopped daemon never reaches the
    line this helper exists to observe.
    """
    import runner.daemon as mod

    requested = []
    holder = {}

    class _StopsTheLoop(_RecordingPool):
        def start(self):
            super().start()
            # The loop below has not begun yet; this makes its first
            # `while not self._stop.is_set()` false, so run() returns
            # deterministically instead of polling forever.
            holder["daemon"].stop()

    def _fake_specs(device_ids=None, **kwargs):
        requested.append(device_ids)
        return [_spec(c) for c in (0, 1, 2, 3)]

    monkeypatch.setattr(mod, "worker_specs", _fake_specs)
    monkeypatch.setattr(mod, "WorkerPool", _StopsTheLoop)

    config = DaemonConfig(
        socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
        playlist_dir=str(tmp_path / "playlist"), log_root=str(tmp_path / "logs"),
        device_ids=device_ids)
    daemon = Daemon(config)
    holder["daemon"] = daemon
    daemon.server = _CollectingServer()
    _run(daemon)
    assert daemon.pool is not None, "guard: run() must have built a pool"
    return daemon, requested


def test_the_pool_run_builds_reports_worker_deaths_back_to_the_daemon(
        tmp_path, monkeypatch):
    """runner/pool.py will not fabricate a protocol event, so if this callback
    is never wired the orphaned job is reported to nobody and the UI sits in
    `folding` forever -- with every one of the on_worker_lost tests above
    still green, because they all call it directly."""
    daemon, _requested = _run_with_a_built_pool(tmp_path, monkeypatch)
    assert daemon.pool.built_on_worker_lost == daemon.on_worker_lost
    assert daemon.pool.built_on_event == daemon.on_event


def test_the_devices_flag_decides_which_chips_the_booth_folds_on(
        tmp_path, monkeypatch):
    """`--devices 1,3` on a four-card box must reach tt-bio's own device
    detection unchanged -- that is what turns a typo into its clear error
    instead of a silently smaller booth."""
    _daemon_, requested = _run_with_a_built_pool(tmp_path, monkeypatch,
                                                device_ids="1,3")
    assert requested == ["1,3"]


def test_run_uses_a_pool_it_was_given_rather_than_building_a_second(
        tmp_path, monkeypatch):
    """The same discipline the pre-multi-chip run() needed for Folder, after
    it was found opening a real device in a test that only ever touched a
    fake. Here the cost would be enumerating /dev/tenstorrent and spawning
    four real worker processes, from a test that thought it had substituted
    all of it.
    """
    import runner.daemon as mod

    class _BuiltItsOwnPool(BaseException):
        """Not an Exception: `_build_pool` catches those and retries (right
        for a booth, wrong for a tripwire), which would turn this into a
        three-second watchdog wait instead of a failure at the offending
        line. Same reasoning as tests/unit/runner/conftest.py's own guard,
        which is the second line of this defence."""

    def _must_not_be_called(*args, **kwargs):
        raise _BuiltItsOwnPool("run() built its own pool despite being given one")

    monkeypatch.setattr(mod, "worker_specs", _must_not_be_called)
    monkeypatch.setattr(mod, "WorkerPool", _must_not_be_called)

    holder = {}

    class _StopsTheLoop(_FakePool):
        def start(self):
            super().start()
            holder["daemon"].stop()

    pool = _StopsTheLoop()
    daemon = _daemon(tmp_path, pool)
    holder["daemon"] = daemon
    _run(daemon)
    assert daemon.pool is pool
    assert (pool.started, pool.stopped) == (1, 1)


def test_run_may_not_be_called_twice_on_one_instance(tmp_path):
    """run() is what spawns the worker processes; a second call would put a
    second set onto chips the first call's teardown has just released."""
    daemon = _daemon(tmp_path, _FakePool())
    daemon.stop()
    _run(daemon)
    with pytest.raises(RuntimeError, match="once"):
        daemon.run()


def test_run_serves_the_socket_before_it_looks_for_any_hardware(tmp_path):
    """A UI that connects during startup must find a socket answering
    not_ready, not a dead one and an endless reconnect loop."""
    pool = _FakePool(ready=[])
    daemon = _daemon(tmp_path, pool)
    daemon.stop()
    _run(daemon)
    assert daemon.server.started == 1
    assert daemon.server.stopped == 1


# ── the device-scan retry's log, which is unbounded on a broken booth ───────

def test_a_permanent_device_scan_failure_logs_one_traceback_not_one_per_retry(
        tmp_path, monkeypatch, caplog):
    """A booth that cannot see its chips retries forever by design -- and
    logged a full traceback every 5 s while doing it, roughly 25 MB/day into
    `daemon.log`, which `--log-budget-gb` does not cover (that governs the
    tt-metal log root only). See docs/followups.md, from Phase 3a.

    Mutation: restoring the unconditional `log.exception`. Red -- five
    tracebacks instead of one.
    """
    import logging

    import runner.daemon as mod

    daemon = _daemon(tmp_path, _FakePool([0]))
    daemon.pool = None

    attempts = {"n": 0}

    def always_fails(*a, **kw):
        attempts["n"] += 1
        if attempts["n"] >= 5:
            daemon._stop.set()          # let the loop end
        raise RuntimeError("no chips detected")

    monkeypatch.setattr(mod, "worker_specs", always_fails)
    monkeypatch.setattr(daemon._stop, "wait", lambda *_a, **_k: False)

    with caplog.at_level(logging.INFO, logger="runner.daemon"):
        assert daemon._build_pool() is False

    assert attempts["n"] >= 5, "the retry loop did not actually retry"
    tracebacks = [r for r in caplog.records if r.exc_info]
    assert len(tracebacks) == 1, (
        f"{attempts['n']} identical failures produced {len(tracebacks)} "
        f"tracebacks; a booth left in this state fills the disk with them")
    # And it must still say something each time, or an operator cannot tell a
    # stuck booth from a quiet one.
    assert len([r for r in caplog.records if "chips to fold" in r.getMessage()]) \
        >= attempts["n"], "later retries went entirely unlogged"


def test_a_different_scan_failure_gets_its_own_traceback(
        tmp_path, monkeypatch, caplog):
    """De-duplication is per failure, not "one traceback ever": a new fault
    is new information and prints in full.

    Mutation: keying the de-duplication on nothing (a plain `logged_once`
    flag). Red.
    """
    import logging

    import runner.daemon as mod

    daemon = _daemon(tmp_path, _FakePool([0]))
    daemon.pool = None

    faults = iter([RuntimeError("no chips detected"),
                   RuntimeError("no chips detected"),
                   ValueError("driver is mid-reload")])

    def failing(*a, **kw):
        try:
            raise next(faults)
        except StopIteration:
            daemon._stop.set()
            raise RuntimeError("done")

    monkeypatch.setattr(mod, "worker_specs", failing)
    monkeypatch.setattr(daemon._stop, "wait", lambda *_a, **_k: False)

    with caplog.at_level(logging.INFO, logger="runner.daemon"):
        daemon._build_pool()

    tracebacks = [r for r in caplog.records if r.exc_info]
    assert len(tracebacks) >= 2, \
        "a genuinely different failure was swallowed as a repeat"
