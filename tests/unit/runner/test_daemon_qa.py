"""The daemon's affinity-question wiring (Task 6 of the affinity-questions
plan).

Split into its own file rather than folded into test_daemon.py or
test_daemon_multichip.py, for the same reason those two are already split:
this is its own concern (a dedicated Q&A chip and worker, entirely separate
from the fold pool's scheduling), not "how many chips there are" and not
"the parts that survived the move to one worker per chip."

Every daemon here is built the same way test_daemon.py's are
(`_daemonfakes._daemon`/`_FakePool`), except where a test needs to observe
what `run()`/`_build_pool()` themselves construct -- those follow
test_daemon_multichip.py's `_run_with_a_built_pool` pattern instead, because
`split_for_qa` is applied INSIDE `_build_pool`, which a directly-injected
fake pool bypasses entirely.

Real interfaces confirmed by earlier tasks in this plan (see
.superpowers/sdd/2026-09-15-affinity-questions/progress.md), trusted over
this task's own brief where the two differ:

- `runner.workers.split_for_qa(specs) -> (fold_specs, qa_spec)` already
  exists (Task 5) and reserves the HIGHEST-card WorkerSpec, or (specs, None)
  on one chip.
- `runner.affinity.AffinityScorer(device_id).score(question_id, target_id,
  input_path, emit)` never raises; emits answer_start then
  answer_done/answer_error. No `.close()` -- device lifecycle is owned by
  process exit (Task 4).
- `protocol.events.CLIENT_MESSAGE_FIELDS["question"] == ("target_id",
  "question_id")` -- a `question` message carries only
  `{question_id, target_id}`, never the question text itself (that lives in
  playlist/questions.yaml, loaded UI-side). Updated (whole-branch review,
  Important 6): both fields are validated at the protocol boundary now, not
  just `target_id` -- see `protocol/events.py`'s own comment on why
  `question_id` needed the same bound `target_id` always had.
- The design spec (docs/superpowers/specs/2026-09-15-affinity-qa-design.md,
  section 4) states that a `question` message "Enqueues the underlying fold
  pick (if the target isn't already in flight/recent) exactly as `pick`
  does, AND enqueues an affinity job on the dedicated Q&A worker" -- NOT,
  as this task's brief's own draft code comment claimed, that
  `_accept_question` "does NOT touch self.queue... at all". The brief's
  comment is read as the brief's own paraphrase error (flagged as expected
  in the task instructions), not as authoritative: Task 11's own brief
  confirms the UI's two trigger paths (a visitor's tap, the attract-loop
  cadence) send ONLY a `question` message, never a separate `pick` alongside
  it -- so if the daemon does not enqueue the fold itself, nothing else in
  this plan ever will, and a visitor asking a question would never see the
  structure it is about. `_accept_question` below calls `_accept_pick`
  internally for exactly this reason, and the design decision is recorded
  here rather than only in the implementation.
"""

import threading
import types
from pathlib import Path

import pytest

from runner.daemon import Daemon, DaemonConfig
from runner.queue import Job
from runner.workers import WorkerSpec

from _daemonfakes import _CollectingServer, _FakePool, _daemon, _run


def _playlist_with(tmp_path, *target_ids):
    playlist = tmp_path / "playlist"
    playlist.mkdir(exist_ok=True)
    for target_id in target_ids:
        (playlist / f"{target_id}.yaml").write_text("version: 1\n")
    return playlist


def _qa_spec(card=3):
    return WorkerSpec(card=card, label=f"h:tt:{card}", visible_devices=str(card),
                      logical_device_id=0, mesh_graph_descriptor=None)


# ---------------------------------------------------------------------------
# hello's qa_capable field
# ---------------------------------------------------------------------------

def test_hello_reports_qa_capable_when_a_chip_is_reserved(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    assert daemon._hello()["qa_capable"] is True


def test_hello_reports_not_qa_capable_with_no_reserved_chip(tmp_path):
    """The one-chip case (split_for_qa returns qa_spec=None) and the "nobody
    has built a pool yet" case both look like this -- _qa_spec stays None
    either way, and qa_capable must say so rather than default to True.
    """
    daemon = _daemon(tmp_path, _FakePool())
    assert daemon._qa_spec is None, "guard: nothing set it yet"
    assert daemon._hello()["qa_capable"] is False


# ---------------------------------------------------------------------------
# hello's qa_card field (item 8 of the deferred-nits batch): names the
# physical chip reserved for Q&A explicitly, rather than leaving the UI to
# infer it from `cards` being N-1. No current consumer in ui/app.py -- this
# is deliberate future-proofing, safe to add because `hello`'s payload has
# no per-field wire schema (decode() only checks `type`), so an optional key
# needs no PROTOCOL_VERSION bump.
# ---------------------------------------------------------------------------

def test_hello_names_the_reserved_qa_card_explicitly(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec(card=3)
    assert daemon._hello()["qa_card"] == 3


def test_hello_reports_no_qa_card_with_no_reserved_chip(tmp_path):
    """The mutation guard for the test above: an implementation that always
    returned some fixed card id (e.g. 0) regardless of reservation state
    fails this one."""
    daemon = _daemon(tmp_path, _FakePool())
    assert daemon._qa_spec is None, "guard: nothing set it yet"
    assert daemon._hello()["qa_card"] is None


# ---------------------------------------------------------------------------
# _accept_question
# ---------------------------------------------------------------------------

def test_accept_question_rejects_unknown_target(tmp_path, caplog):
    _playlist_with(tmp_path, "dhfr")
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    import logging
    with caplog.at_level(logging.INFO, logger="tt-bio-demod"):
        daemon._accept_question({"type": "question", "question_id": "q1",
                                 "target_id": "not_a_real_target"})
    assert "no such target" in caplog.text
    assert daemon._qa_queue == []


def test_accept_question_queues_a_real_target(tmp_path):
    _playlist_with(tmp_path, "dhfr")
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    daemon._accept_question({"type": "question", "question_id": "q1",
                             "target_id": "dhfr"})
    assert len(daemon._qa_queue) == 1
    question_id, target_id, input_path = daemon._qa_queue[0]
    assert (question_id, target_id) == ("q1", "dhfr")
    assert input_path.endswith("dhfr.yaml")


def test_accept_question_is_refused_with_no_reserved_chip(tmp_path, caplog):
    """A one-chip box: split_for_qa left _qa_spec None, so the booth has
    nowhere to run nesso1. Refused silently on the wire (the UI hides the
    question feature entirely via qa_capable), same shape as every other
    _accept_* refusal.
    """
    _playlist_with(tmp_path, "dhfr")
    daemon = _daemon(tmp_path, _FakePool())
    assert daemon._qa_spec is None
    import logging
    with caplog.at_level(logging.INFO, logger="tt-bio-demod"):
        daemon._accept_question({"type": "question", "question_id": "q1",
                                 "target_id": "dhfr"})
    assert daemon._qa_queue == []
    assert "no chip reserved" in caplog.text or "Q&A" in caplog.text


def test_accept_question_also_enqueues_the_underlying_fold(tmp_path):
    """Spec section 4: asking a question enqueues the fold pick too, exactly
    as a visitor's pick does -- a question's answer is shown next to the
    structure it is about, and nothing downstream of the daemon (see this
    file's module docstring) ever sends a separate `pick`.
    """
    _playlist_with(tmp_path, "dhfr")
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    daemon._accept_question({"type": "question", "question_id": "q1",
                             "target_id": "dhfr"})
    from runner.queue import VISITOR_PRIORITY
    assert [j.target_id for j in daemon.queue.pending] == ["dhfr"]
    assert daemon.queue.pending[0].priority == VISITOR_PRIORITY


def test_accept_question_does_not_duplicate_an_already_folding_target(tmp_path):
    """_accept_pick's own dedup rule (already folding -> queue nothing) must
    still apply when reached through a question -- a question must not be
    able to double-queue a fold any more than a second tap of the same pick
    tile could.
    """
    _playlist_with(tmp_path, "dhfr")
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    daemon._in_flight[0] = "dhfr"
    daemon._accept_question({"type": "question", "question_id": "q1",
                             "target_id": "dhfr"})
    assert daemon.queue.pending == [], "already folding; nothing to queue"
    assert len(daemon._qa_queue) == 1, "the score is independent of the fold"


def test_accept_question_still_scores_a_quarantined_target(tmp_path):
    """nesso1 needs no fold at all (runner/affinity.py's own module
    docstring) -- a target too unreliable to fold is not too unreliable to
    score, so a quarantined target's fold pick is refused but its question is
    still queued.
    """
    _playlist_with(tmp_path, "dhfr")
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    daemon._quarantined.add("dhfr")
    daemon._accept_question({"type": "question", "question_id": "q1",
                             "target_id": "dhfr"})
    assert daemon.queue.pending == [], "quarantined; no fold"
    assert len(daemon._qa_queue) == 1, "but the question is unaffected"


def test_accept_question_wakes_the_dispatch_loop(tmp_path):
    _playlist_with(tmp_path, "dhfr")
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    assert not daemon._wake.is_set()
    daemon._accept_question({"type": "question", "question_id": "q1",
                             "target_id": "dhfr"})
    assert daemon._wake.is_set()


# ---------------------------------------------------------------------------
# _enqueue_question: MAX_PENDING_QUESTIONS bounds the queue (Finding 2,
# task-6 review). Mirrors MAX_PENDING_PICKS' own replacement test for
# _accept_pick, with the one deliberate difference: a bumped QUESTION is
# answered with `answer_error`, where a bumped PICK is silently dropped
# (see _enqueue_question's own docstring for why the two must differ).
# ---------------------------------------------------------------------------

def test_a_second_question_replaces_the_first_still_waiting_one(tmp_path):
    _playlist_with(tmp_path, "dhfr", "trypsin")
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    daemon._accept_question({"type": "question", "question_id": "q1",
                             "target_id": "dhfr"})
    daemon._accept_question({"type": "question", "question_id": "q2",
                             "target_id": "trypsin"})
    assert len(daemon._qa_queue) == 1, "MAX_PENDING_QUESTIONS == 1"
    assert daemon._qa_queue[0][0] == "q2", "the newest question waits"


def test_the_replaced_question_gets_an_answer_error(tmp_path):
    """The bumped question must not vanish with no wire event -- Task 11's
    UI tracks a pending answer per question_id, and a silently dropped one
    is a pending state that never resolves."""
    _playlist_with(tmp_path, "dhfr", "trypsin")
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    daemon._accept_question({"type": "question", "question_id": "q1",
                             "target_id": "dhfr"})
    daemon._accept_question({"type": "question", "question_id": "q2",
                             "target_id": "trypsin"})
    errors = [e for e in daemon.server.events if e["type"] == "answer_error"]
    assert len(errors) == 1
    assert errors[0]["question_id"] == "q1"
    assert errors[0]["target_id"] == "dhfr"


def test_a_third_question_does_not_resurrect_the_first_two(tmp_path):
    """Bounding must hold under more than one eviction, not just the first
    -- a mutation that only handled 2-in-a-row would still pass a test that
    never tried a third."""
    _playlist_with(tmp_path, "dhfr", "trypsin", "fkbp12")
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    for question_id, target_id in (("q1", "dhfr"), ("q2", "trypsin"),
                                   ("q3", "fkbp12")):
        daemon._accept_question({"type": "question", "question_id": question_id,
                                 "target_id": target_id})
    assert [q[0] for q in daemon._qa_queue] == ["q3"]
    errored_ids = {e["question_id"] for e in daemon.server.events
                  if e["type"] == "answer_error"}
    assert errored_ids == {"q1", "q2"}


def test_enqueue_question_is_safe_under_concurrent_callers(tmp_path):
    """Fix round 2 (task-6 review): `_qa_queue` must never lose a question
    under concurrent callers.

    `_enqueue_question` runs on whichever client's reader thread received
    the `question` message -- `EventServer._accept_loop` spawns one
    `_reader_loop` thread per connected client, with no serializing lock
    between them, and the protocol allows several simultaneous clients. So
    two questions queued at the same instant on two different sockets is a
    real scenario this daemon must survive, not a hypothetical one.

    Before this fix, `_enqueue_question` was three unlocked list operations:
    read `stale = self._qa_queue[:n]`, `del self._qa_queue[:len(stale)]`,
    then `append`. Two threads racing through this can both compute `stale`
    from the SAME queue state before either mutates it; the second thread's
    `del` then removes whatever the FIRST thread just appended (rather than
    the entry `stale` was actually computed from), so that appended question
    is dropped from the queue with no `answer_error` EVER emitted for it --
    reproducing the exact silent-pileup bug `_enqueue_question`'s own bound
    exists to close, one layer down.

    This drives many concurrent callers through the real (un-mocked)
    `_enqueue_question` and checks the invariant the lock restores: every
    question submitted ends the run accounted for -- still in the queue, or
    answered with `answer_error` -- and never both, and never neither.
    Confirmed against the pre-fix code (the lock removed, by hand, for a
    local run only -- see the fix report) to fail within a handful of runs;
    it is inherently a race, so a single green run does not by itself prove
    the fix, but a failure here is unambiguous evidence the bug is back.
    """
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()

    n = 300
    barrier = threading.Barrier(n)

    def submit(i):
        barrier.wait()          # every thread starts its call in the same instant
        daemon._enqueue_question(f"q{i}", "dhfr", "/p/dhfr.yaml")

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    queued_ids = {q[0] for q in daemon._qa_queue}
    errored_ids = {e["question_id"] for e in daemon.server.events
                  if e["type"] == "answer_error"}
    submitted_ids = {f"q{i}" for i in range(n)}

    assert not (queued_ids & errored_ids), (
        "a question must not be both still queued and reported as replaced")
    assert queued_ids | errored_ids == submitted_ids, (
        "every submitted question must be accounted for -- still queued, "
        "or answered with answer_error -- never silently dropped: missing "
        f"{submitted_ids - (queued_ids | errored_ids)}")
    assert len(daemon._qa_queue) <= 1, "MAX_PENDING_QUESTIONS == 1"


def test_on_client_message_dispatches_a_question(tmp_path):
    """The real end-to-end path: on_client_message's kind dispatch, not
    _accept_question called directly."""
    _playlist_with(tmp_path, "dhfr")
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    daemon.on_client_message({"type": "question", "question_id": "q1",
                              "target_id": "dhfr"})
    assert len(daemon._qa_queue) == 1


def test_on_client_message_never_raises_for_a_malformed_question(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    daemon.on_client_message({"type": "question"})   # no question_id/target_id
    daemon.on_client_message("not even a dict")


# ---------------------------------------------------------------------------
# _dispatch_qa_once: draining the queue onto the Q&A pool
# ---------------------------------------------------------------------------

class _FakeQaPool:
    """Mirrors _FakePool's shape for the two things _dispatch_qa_once needs
    beyond what a fold pool already has: dispatch_question and all_retired.
    """

    def __init__(self, cards=(3,), ready=None, retired=False):
        self.cards = list(cards)
        self._ready = list(cards if ready is None else ready)
        self._busy = {}
        self.dispatched = []
        # Mirrors the real WorkerPool.all_retired(): permanently True once
        # CONTROL_FATAL (or WORKER_RETIRE_AFTER deaths) has retired every
        # card this pool manages -- which for the one-spec Q&A pool means
        # its one card. A plain constructor flag rather than deriving it
        # from `ready`/`cards`, because the real distinction is orthogonal
        # to readiness: a pool can be simultaneously not-ready (still
        # loading, or mid-question) and not retired at all.
        self._retired = retired

    def ready_cards(self):
        return sorted(c for c in self._ready if c not in self._busy)

    def all_retired(self):
        return self._retired

    def dispatch_question(self, question_id, target_id, input_path, card):
        if card not in self.ready_cards():
            raise ValueError(f"card {card} is not ready")
        self._busy[card] = question_id
        self.dispatched.append((question_id, target_id, input_path, card))

    def finish(self, card):
        self._busy.pop(card, None)


def test_dispatch_qa_once_sends_the_next_queued_question(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_spec = _qa_spec()
    daemon._qa_pool = _FakeQaPool()
    daemon._qa_queue.append(("q1", "dhfr", "/p/dhfr.yaml"))
    daemon._dispatch_qa_once()
    assert daemon._qa_pool.dispatched == [("q1", "dhfr", "/p/dhfr.yaml", 3)]
    assert daemon._qa_queue == []


def test_dispatch_qa_once_is_a_no_op_with_nothing_queued(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_pool = _FakeQaPool()
    daemon._dispatch_qa_once()
    assert daemon._qa_pool.dispatched == []


def test_dispatch_qa_once_is_a_no_op_with_no_qa_pool(tmp_path):
    """The one-chip case: _qa_pool stays None forever. Must not raise."""
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_queue.append(("q1", "dhfr", "/p/dhfr.yaml"))
    daemon._dispatch_qa_once()   # must not raise
    assert daemon._qa_queue == [("q1", "dhfr", "/p/dhfr.yaml")]


def test_dispatch_qa_once_waits_while_the_worker_is_busy(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_pool = _FakeQaPool(ready=[])
    daemon._qa_queue.append(("q1", "dhfr", "/p/dhfr.yaml"))
    daemon._dispatch_qa_once()
    assert daemon._qa_pool.dispatched == []
    assert len(daemon._qa_queue) == 1, "the question is kept, not lost"


def test_dispatch_qa_once_sends_only_one_question_per_pass(tmp_path):
    """One reserved chip answers one question at a time -- the queue drains
    at the worker's own pace, not all at once."""
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_pool = _FakeQaPool()
    daemon._qa_queue.append(("q1", "dhfr", "/p/dhfr.yaml"))
    daemon._qa_queue.append(("q2", "trypsin", "/p/trypsin.yaml"))
    daemon._dispatch_qa_once()
    assert len(daemon._qa_pool.dispatched) == 1
    assert len(daemon._qa_queue) == 1


def test_dispatch_qa_once_fails_every_queued_question_when_the_pool_is_retired(
        tmp_path):
    """Finding 2 (task-6 review): if nesso1's weights are missing, broken,
    or unreachable to the worker on a given install, the Q&A worker's
    load() fails at startup, CONTROL_FATAL retires its one card
    permanently, and ready_cards() for this pool is `[]` forever -- not
    "still starting up", not "still scoring the last one". (Both source
    setup and the Debian postinst do attempt to provision these weights
    now -- see docs/followups.md for a real, separate gap in that
    provisioning that can still leave them unreachable.)
    Every question ever queued from that point on must be failed loudly, not
    left to pile up silently: the attract-loop cadence (a later task) mints a
    fresh question_id every cycle with no dedup, so an unbounded silent
    pileup here is a booth advertising a feature (`qa_capable`) it can never
    deliver.
    """
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_pool = _FakeQaPool(retired=True)
    daemon._qa_queue.append(("q1", "dhfr", "/p/dhfr.yaml"))
    daemon._qa_queue.append(("q2", "trypsin", "/p/trypsin.yaml"))

    daemon._dispatch_qa_once()

    assert daemon._qa_queue == [], "nothing left to pile up"
    assert daemon._qa_pool.dispatched == [], "a retired card is never sent to"
    errors = {e["question_id"]: e["target_id"] for e in daemon.server.events
              if e["type"] == "answer_error"}
    assert errors == {"q1": "dhfr", "q2": "trypsin"}


def test_dispatch_qa_once_is_still_just_a_wait_when_merely_not_ready_yet(
        tmp_path):
    """GUARD, so the fix above cannot be a mutation that always drains the
    queue: a pool that is temporarily not-ready (still loading its model, or
    mid-question) is NOT `all_retired()`, and must still be left alone --
    see test_dispatch_qa_once_waits_while_the_worker_is_busy for the existing
    version of this without a retirement check in the picture at all."""
    daemon = _daemon(tmp_path, _FakePool())
    daemon._qa_pool = _FakeQaPool(ready=[], retired=False)
    daemon._qa_queue.append(("q1", "dhfr", "/p/dhfr.yaml"))
    daemon._dispatch_qa_once()
    assert daemon._qa_queue == [("q1", "dhfr", "/p/dhfr.yaml")], (
        "not retired -- just busy or still loading -- so it waits")
    assert not [e for e in daemon.server.events if e["type"] == "answer_error"]


def test_dispatch_qa_once_requeues_on_a_dispatch_race(tmp_path):
    """Same shape as dispatch_once's own race guard: the worker can die (or
    the pool can otherwise refuse) between the readiness check and the send.
    """
    daemon = _daemon(tmp_path, _FakePool())

    class _RacingQaPool(_FakeQaPool):
        def dispatch_question(self, question_id, target_id, input_path, card):
            raise ValueError("worker died between the check and the send")

    daemon._qa_pool = _RacingQaPool()
    daemon._qa_queue.append(("q1", "dhfr", "/p/dhfr.yaml"))
    daemon._dispatch_qa_once()   # must not raise
    assert daemon._qa_queue == [("q1", "dhfr", "/p/dhfr.yaml")]


# ---------------------------------------------------------------------------
# on_qa_event / on_qa_worker_lost
# ---------------------------------------------------------------------------

def test_qa_events_reach_the_wire_unchanged(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    event = {"type": "answer_done", "question_id": "q1", "target_id": "dhfr",
             "score": 0.5, "affinity_pred_value": -1.2}
    daemon.on_qa_event(3, event)
    assert event in daemon.server.events


def test_qa_events_touch_no_fold_bookkeeping(tmp_path):
    """answer_* events must not claim a card, free a card, or touch the fold
    failure counter -- the qa chip is not part of self.cards at all."""
    daemon = _daemon(tmp_path, _FakePool())
    daemon.on_qa_event(3, {"type": "answer_start", "question_id": "q1",
                          "target_id": "dhfr"})
    assert daemon._failures == {}
    assert daemon._in_flight == {}


def test_a_lost_qa_worker_reports_an_answer_error(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    daemon.on_qa_worker_lost(3, "q1", "dhfr")
    errors = [e for e in daemon.server.events if e["type"] == "answer_error"]
    assert len(errors) == 1
    assert errors[0]["question_id"] == "q1"
    assert errors[0]["target_id"] == "dhfr"


def test_a_lost_qa_worker_never_raises(tmp_path):
    """Runs on the qa pool's own reader thread; an exception here would kill
    that thread and the reserved chip would stop reporting anything, forever.
    """
    daemon = _daemon(tmp_path, _FakePool())

    class _ExplodingServer:
        def broadcast(self, event):
            raise RuntimeError("boom")

    daemon.server = _ExplodingServer()
    daemon.on_qa_worker_lost(3, "q1", "dhfr")   # must not raise


def test_a_lost_qa_worker_does_not_touch_the_fold_failure_counter(tmp_path):
    """A question's failure is not a fold's failure -- QUARANTINE_AFTER must
    never be reachable through a dead Q&A worker."""
    daemon = _daemon(tmp_path, _FakePool())
    for _ in range(5):
        daemon.on_qa_worker_lost(3, "q", "dhfr")
    assert daemon._failures == {}
    assert daemon._quarantined == set()


# ---------------------------------------------------------------------------
# _build_pool: split_for_qa wired into pool construction
# ---------------------------------------------------------------------------

def _spec(card):
    return WorkerSpec(card=card, label=f"h:tt:{card}", visible_devices=str(card),
                      logical_device_id=0, mesh_graph_descriptor=None)


def test_build_pool_reserves_the_highest_card_for_qa(tmp_path, monkeypatch):
    import runner.daemon as mod

    monkeypatch.setattr(mod, "worker_specs",
                        lambda *a, **k: [_spec(c) for c in (0, 1, 2, 3)])
    monkeypatch.setattr(mod, "WorkerPool", _RecordingPoolNoStart)

    config = DaemonConfig(socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
                          playlist_dir=str(tmp_path / "playlist"),
                          log_root=str(tmp_path / "logs"),
                          questions_enabled=True)
    daemon = Daemon(config)
    assert daemon._build_pool() is True
    assert daemon._qa_spec.card == 3
    assert sorted(s.card for s in daemon.pool.specs) == [0, 1, 2]


def test_build_pool_reserves_no_chip_on_a_single_chip_box(tmp_path, monkeypatch):
    import runner.daemon as mod

    monkeypatch.setattr(mod, "worker_specs", lambda *a, **k: [_spec(0)])
    monkeypatch.setattr(mod, "WorkerPool", _RecordingPoolNoStart)

    config = DaemonConfig(socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
                          playlist_dir=str(tmp_path / "playlist"),
                          log_root=str(tmp_path / "logs"))
    daemon = Daemon(config)
    assert daemon._build_pool() is True
    assert daemon._qa_spec is None
    assert [s.card for s in daemon.pool.specs] == [0]


# ---------------------------------------------------------------------------
# _build_pool: questions_enabled=False (the default -- no `--questions`)
# skips split_for_qa entirely, even with 2+ chips detected
# ---------------------------------------------------------------------------

def test_build_pool_with_questions_disabled_folds_on_every_chip(tmp_path, monkeypatch):
    """The default behavior: no chip held back even at four chips -- every
    detected chip folds, no reservation, unless an operator opts in with
    `--questions`."""
    import runner.daemon as mod

    monkeypatch.setattr(mod, "worker_specs",
                        lambda *a, **k: [_spec(c) for c in (0, 1, 2, 3)])
    monkeypatch.setattr(mod, "WorkerPool", _RecordingPoolNoStart)

    config = DaemonConfig(socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
                          playlist_dir=str(tmp_path / "playlist"),
                          log_root=str(tmp_path / "logs"),
                          questions_enabled=False)
    daemon = Daemon(config)
    assert daemon._build_pool() is True
    assert daemon._qa_spec is None
    assert sorted(s.card for s in daemon.pool.specs) == [0, 1, 2, 3]


def test_build_pool_with_questions_disabled_never_calls_split_for_qa(tmp_path, monkeypatch):
    """Not "call it and discard the result": split_for_qa's own docstring
    says it exists to make a PERMANENT, deterministic reservation decision
    at startup, so `questions_enabled=False` (no `--questions`, the default)
    must skip the call outright rather than invoke it and override what it
    returns -- calling it and discarding the
    reservation would still be making the decision, just hiding it from a
    reader who would have to trace all the way into WorkerPool's
    construction to discover it was overridden.
    """
    import runner.daemon as mod

    def _must_not_be_called(specs):
        raise AssertionError("split_for_qa must not be called when "
                              "questions_enabled is False")

    monkeypatch.setattr(mod, "worker_specs",
                        lambda *a, **k: [_spec(c) for c in (0, 1, 2, 3)])
    monkeypatch.setattr(mod, "split_for_qa", _must_not_be_called)
    monkeypatch.setattr(mod, "WorkerPool", _RecordingPoolNoStart)

    config = DaemonConfig(socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
                          playlist_dir=str(tmp_path / "playlist"),
                          log_root=str(tmp_path / "logs"),
                          questions_enabled=False)
    daemon = Daemon(config)
    # Would have raised _must_not_be_called's AssertionError already if
    # _build_pool called split_for_qa at all.
    assert daemon._build_pool() is True


def test_hello_reports_not_qa_capable_with_questions_disabled_at_four_chips(
        tmp_path, monkeypatch):
    """This is what makes the off-by-default `questions_enabled` free to
    have built: ui/app.py already hides the question queue panel, the
    gallery "ask" strip and the attract-loop question cue whenever `hello`
    reports `qa_capable: false` -- exactly what a chip-less booth already
    reports, and exactly what THIS booth (four real chips, questions never
    turned on) reports too. No UI change needed.
    """
    import runner.daemon as mod

    monkeypatch.setattr(mod, "worker_specs",
                        lambda *a, **k: [_spec(c) for c in (0, 1, 2, 3)])
    monkeypatch.setattr(mod, "WorkerPool", _RecordingPoolNoStart)

    config = DaemonConfig(socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
                          playlist_dir=str(tmp_path / "playlist"),
                          log_root=str(tmp_path / "logs"),
                          questions_enabled=False)
    daemon = Daemon(config)
    assert daemon._build_pool() is True
    assert len(daemon.pool.cards) == 4
    assert daemon._hello()["qa_capable"] is False


class _RecordingPoolNoStart(_FakePool):
    """Captures the specs run()/_build_pool() constructed a real pool with,
    without spawning anything -- mirrors test_daemon_multichip.py's own
    _RecordingPool, redefined here rather than imported (that one is a
    module-local test helper in a different file, by the existing
    convention).
    """

    def __init__(self, specs, on_event, *, log_root, on_worker_lost=None,
                 spawn=None, total_workers=None, **kwargs):
        super().__init__(cards=[s.card for s in specs])
        self.specs = specs
        self.on_event = on_event
        self.on_worker_lost = on_worker_lost
        self.spawn = spawn
        self.log_root = log_root
        # None here would be indistinguishable from "the daemon never passed
        # it" -- recorded as-given (never defaulted to len(specs) the way the
        # real WorkerPool does) so a test can tell the two apart.
        self.total_workers = total_workers


# ---------------------------------------------------------------------------
# run(): building and starting a dedicated Q&A pool
# ---------------------------------------------------------------------------

def test_run_builds_a_dedicated_qa_pool_when_a_chip_is_reserved(tmp_path, monkeypatch):
    import runner.daemon as mod

    built = []
    holder = {}

    class _RecordingPool(_RecordingPoolNoStart):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            built.append(self)

        def start(self):
            super().start()
            if len(built) == 1:
                # Let the qa pool get built too before stopping -- both
                # happen sequentially in run(), before the poll loop.
                holder["daemon"].stop()

    monkeypatch.setattr(mod, "worker_specs",
                        lambda *a, **k: [_spec(c) for c in (0, 1)])
    monkeypatch.setattr(mod, "WorkerPool", _RecordingPool)

    # questions_enabled=True: the default is now OFF, and this test is
    # specifically about the reserved-Q&A-pool path, not the default.
    config = DaemonConfig(socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
                          playlist_dir=str(tmp_path / "playlist"),
                          log_root=str(tmp_path / "logs"),
                          questions_enabled=True)
    daemon = Daemon(config)
    holder["daemon"] = daemon
    daemon.server = _CollectingServer()
    _run(daemon)

    assert len(built) == 2, "one pool for folding, one dedicated to Q&A"
    fold_pool, qa_pool = built
    assert [s.card for s in fold_pool.specs] == [0]
    assert [s.card for s in qa_pool.specs] == [1]
    assert qa_pool.on_event == daemon.on_qa_event
    assert qa_pool.on_worker_lost == daemon.on_qa_worker_lost
    assert daemon._qa_pool is qa_pool
    assert qa_pool.started == 1
    # Finding 3 (task-6 review): each pool must be told the TRUE total
    # worker count across BOTH pools -- 2 chips detected here (card 0 goes to
    # the fold pool, card 1 is reserved for Q&A), so `worker_environ`'s host
    # thread cap must be sized against 2 for each of them, not against each
    # pool's own spec count (1 and 1), which would let each claim the whole
    # box as though the other pool did not exist.
    assert fold_pool.total_workers == 2
    assert qa_pool.total_workers == 2


def test_run_builds_no_qa_pool_on_a_single_chip_box(tmp_path, monkeypatch):
    import runner.daemon as mod

    built = []
    holder = {}

    class _RecordingPool(_RecordingPoolNoStart):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            built.append(self)

        def start(self):
            super().start()
            holder["daemon"].stop()

    monkeypatch.setattr(mod, "worker_specs", lambda *a, **k: [_spec(0)])
    monkeypatch.setattr(mod, "WorkerPool", _RecordingPool)

    config = DaemonConfig(socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
                          playlist_dir=str(tmp_path / "playlist"),
                          log_root=str(tmp_path / "logs"))
    daemon = Daemon(config)
    holder["daemon"] = daemon
    daemon.server = _CollectingServer()
    _run(daemon)

    assert len(built) == 1, "no chip to reserve; no second pool"
    assert daemon._qa_pool is None


def test_stopping_the_daemon_stops_the_qa_pool_too(tmp_path, monkeypatch):
    import runner.daemon as mod

    built = []
    holder = {}

    class _RecordingPool(_RecordingPoolNoStart):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            built.append(self)

        def start(self):
            super().start()
            if len(built) == 1:
                holder["daemon"].stop()

    monkeypatch.setattr(mod, "worker_specs",
                        lambda *a, **k: [_spec(c) for c in (0, 1)])
    monkeypatch.setattr(mod, "WorkerPool", _RecordingPool)

    # questions_enabled=True: the default is now OFF, and this test is
    # specifically about the reserved-Q&A-pool path, not the default.
    config = DaemonConfig(socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
                          playlist_dir=str(tmp_path / "playlist"),
                          log_root=str(tmp_path / "logs"),
                          questions_enabled=True)
    daemon = Daemon(config)
    holder["daemon"] = daemon
    daemon.server = _CollectingServer()
    _run(daemon)

    fold_pool, qa_pool = built
    assert fold_pool.stopped == 1
    assert qa_pool.stopped == 1


def test_run_uses_a_qa_pool_it_was_given_rather_than_building_a_second(
        tmp_path, monkeypatch):
    """Same discipline as the fold pool's own test of this rule: a pool
    already assigned to self._qa_pool is used as-is and never replaced."""
    import runner.daemon as mod

    class _MustNotBeCalled(BaseException):
        pass

    def _must_not_be_called(*args, **kwargs):
        raise _MustNotBeCalled("run() built a second qa pool")

    holder = {}

    class _StopsTheLoop(_FakePool):
        def start(self):
            super().start()
            holder["daemon"].stop()

    daemon = _daemon(tmp_path, _StopsTheLoop())
    daemon._qa_spec = _qa_spec()
    injected_qa_pool = _StopsTheLoop()
    daemon._qa_pool = injected_qa_pool
    holder["daemon"] = daemon
    monkeypatch.setattr(mod, "WorkerPool", _must_not_be_called)
    _run(daemon)
    assert daemon._qa_pool is injected_qa_pool
