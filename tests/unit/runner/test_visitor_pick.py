"""A visitor's tap becomes a fold: the priority path, exercised end to end.

`runner/queue.py` has had a visitor-priority band since Phase 3a and **it had
never once run** -- its own docstring said so. Every line of `JobQueue`'s
ordering was therefore code unit-tested in isolation and never exercised by a
real producer, so the tests here are written on the assumption that it is
*unproven*: the ones that matter assert the priority **takes effect on a
dispatch**, not that a sorted list sorts.

The ruling these pin, because it is the whole design question: a pick goes to
the **head of the queue and takes the next chip to free**. It never pre-empts
a fold in flight. See `runner/queue.py`'s module docstring for why.
"""

import threading

from runner.daemon import DISPATCH_POLL_S, EMPTY_PLAYLIST_IDLE_S
from runner.queue import VISITOR_PRIORITY, Job, JobQueue

from _daemonfakes import _FakePool, _daemon, _run


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
    """A daemon with a real playlist and every card already folding.

    Note which targets end up in flight, because several tests below depend on
    it: `_enqueue_playlist` walks `sorted(glob("*.yaml"))` and `dispatch_once`
    hands the first four to the four cards, so with the default targets plus
    "hemoglobin" the chips are folding alpha/beta/delta/gamma and hemoglobin is
    the one playlist entry left in the queue -- and therefore the one a test
    can pick without tripping the already-folding rule.
    """
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
    """It read, verbatim before this task: 'nothing ever submits at a priority
    above 0'. That sentence was true for a whole phase and is now false; a
    docstring that survives the change it describes is the next reader's wrong
    mental model."""
    import runner.queue as mod
    text = mod.__doc__.lower()
    assert "nothing ever submits at a priority above 0" not in text
    assert "the socket protocol is one-way" not in text


# ---- the daemon: the priority actually taking effect ----------------------

def test_a_pick_is_dispatched_before_the_whole_attract_backlog(tmp_path):
    """THE test for this task. The priority path has never run in
    production; a queue-ordering test proves the list sorts, and proves
    nothing about whether the daemon ever submits above 0. Free one card
    with a deep backlog waiting and see which JOB actually goes."""
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
    and the booth stops being a playlist for the next ten minutes.

    Both picks name a target that is NOT already folding, on purpose. The
    plan's version of this test picked "alpha", which `_busy_daemon` has on a
    chip -- so it would have been refused by the already-folding rule below
    and this test would have been measuring that rule instead of this one.
    "zeta" sorts after the four that get dispatched, so it stays in the queue
    and reaches the replacement path. (Reported as a plan bug.)
    """
    daemon, pool = _busy_daemon(
        tmp_path, targets=("alpha", "beta", "gamma", "delta", "zeta"))
    assert "zeta" not in set(daemon._in_flight.values()), (
        "this test only exercises the replacement rule if its second pick "
        "names a target no chip is folding")
    daemon.on_client_message(_pick("hemoglobin"))
    daemon.on_client_message(_pick("zeta"))
    visitor_jobs = [j.target_id for j in daemon.queue.pending
                    if j.priority == VISITOR_PRIORITY]
    assert visitor_jobs == ["zeta"]


def test_a_pick_for_a_target_already_folding_queues_nothing(tmp_path):
    """It is already happening. The UI focuses that cell (Task 12); a second
    fold of the same target would occupy a chip to show the same thing."""
    daemon, pool = _busy_daemon(tmp_path)
    folding = pool.dispatched[0][2]
    before = len(daemon.queue)
    daemon.on_client_message(_pick(folding))
    assert len(daemon.queue) == before


def test_a_pick_wakes_a_loop_that_would_otherwise_sit_out_a_backoff(tmp_path):
    """run() waits DISPATCH_POLL_S with a card to fill and
    EMPTY_PLAYLIST_IDLE_S with nothing to fold. A pick that lands one
    millisecond into either of those is a pick the visitor waits the whole
    backoff for -- which is most of the twenty seconds after which a booth
    reads as broken."""
    daemon, pool = _busy_daemon(tmp_path)
    daemon._wake.clear()
    daemon.on_client_message(_pick("hemoglobin"))
    assert daemon._wake.is_set()


def _timeouts_run_waits_on_wake_with(tmp_path, playlist):
    """Run one pass of `run()` and report what `_wake` was waited on.

    Setting the flag is only half the mechanism; the half that actually saves
    the visitor's seconds is `run()` **waiting on `_wake` instead of `_stop`**,
    and no assertion about `_wake.is_set()` can see that. So this substitutes
    an Event that records its own `wait` calls -- if the loop still waits on
    `_stop`, nothing is recorded at all.

    The recorder ends the loop from inside on its first wait (it stops the
    daemon, which sets this very Event, so the wait returns at once and the
    `while not self._stop` above it exits). Deterministic, and no test here
    sleeps out a real backoff.
    """
    holder = {}

    class _RecordingWake(threading.Event):
        def __init__(self):
            super().__init__()
            self.timeouts = []

        def wait(self, timeout=None):
            self.timeouts.append(timeout)
            holder["daemon"].stop()
            return super().wait(timeout)

    if playlist:
        _playlist(tmp_path, "alpha")
    daemon = _daemon(tmp_path, _FakePool())
    holder["daemon"] = daemon
    daemon._wake = _RecordingWake()
    _run(daemon)
    return daemon._wake.timeouts


def test_the_dispatch_loop_waits_on_wake_so_a_pick_can_cut_a_backoff_short(tmp_path):
    """Both of run()'s idle waits keep their numbers and become
    interruptible. The 5s one is the one this task is really about: a pick
    landing one millisecond into it is five seconds of a visitor watching a
    booth do nothing, on top of the fold they must already wait out."""
    assert _timeouts_run_waits_on_wake_with(tmp_path, playlist=False) == [
        EMPTY_PLAYLIST_IDLE_S], "the empty-playlist backoff is not interruptible"
    assert _timeouts_run_waits_on_wake_with(tmp_path, playlist=True) == [
        DISPATCH_POLL_S], "the busy-path poll is not interruptible"


def test_stop_wakes_the_loop_out_of_its_idle_wait(tmp_path):
    """The other half of `_wake`. run()'s backoffs used to be waits on
    `_stop`; moving them to `_wake` without this would mean a systemd
    stop/restart noticed no sooner than the end of the current backoff, with
    four workers still holding four chips."""
    daemon, pool = _busy_daemon(tmp_path)
    daemon._wake.clear()
    daemon.stop()
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
