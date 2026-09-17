"""The Q&A worker process: one AffinityScorer, one chip, a command stream
in, events out.

Mirrors tests/unit/runner/test_worker_process.py's shape for
runner.worker.WorkerSession -- QaSession is the identical split (pure session
class + thin main()), so it gets the identical test treatment: everything
below drives QaSession against a fake AffinityScorer, so nothing here opens a
device.

Deliberately NOT mirrored from that file: the SIGTERM-mid-fold subprocess
tests. AffinityScorer has no close() (runner/affinity.py's own module
docstring -- device lifecycle here is owned by process exit), so
runner/affinity_worker.py installs no SIGTERM handler at all and there is no
`finally: scorer.close()` for one to protect. Those tests exist in the fold
file to prove a *custom* handler's unwind path reaches a real cleanup call;
there is no equivalent claim to prove here.
"""

import io
import json
import os
import sys

import pytest

import runner.affinity_worker as worker_mod
from runner.affinity_worker import QaSession, main
from runner.workers import CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY


class _FakeScorer:
    """An AffinityScorer that emits a plausible event sequence, no device."""

    def __init__(self, outcomes=None, on_load=None):
        self.outcomes = list(outcomes or [])
        self.on_load = on_load
        self.loaded = 0
        self.questions = []

    def load(self):
        self.loaded += 1
        if self.on_load is not None:
            self.on_load()

    def score(self, question_id, target_id, input_path, emit):
        self.questions.append((question_id, target_id, input_path))
        emit({"type": "answer_start", "question_id": question_id,
              "target_id": target_id})
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        emit({"type": "answer_done", "question_id": question_id,
              "target_id": target_id, "score": 0.5,
              "affinity_pred_value": -1.2})


def _session(scorer, card=2):
    events, controls = [], []
    return (QaSession(scorer, events.append, controls.append, card=card),
            events, controls)


def _question(question_id, target_id="dhfr"):
    return json.dumps({"cmd": "question", "question_id": question_id,
                       "target_id": target_id,
                       "input_path": f"/p/{target_id}.yaml"})


def test_the_model_loads_once_and_stays_resident():
    scorer = _FakeScorer()
    session, _e, _c = _session(scorer)
    session.run([_question("q1"), _question("q2"), json.dumps({"cmd": "stop"})])
    assert scorer.loaded == 1
    assert len(scorer.questions) == 2


def test_ready_is_announced_only_after_load_succeeds():
    order = []
    scorer = _FakeScorer(on_load=lambda: order.append("load"))
    session, _e, controls = _session(scorer)
    session.control_emit = lambda ev: order.append(ev["type"])
    session.run([json.dumps({"cmd": "stop"})])
    assert order[:2] == ["load", CONTROL_READY]


def test_every_question_answers_on_this_workers_own_card():
    """The whole point of one process per reserved chip."""
    scorer = _FakeScorer()
    session, events, _c = _session(scorer, card=3)
    session.run([_question("q1"), json.dumps({"cmd": "stop"})])
    start = [e for e in events if e["type"] == "answer_start"][0]
    assert start["target_id"] == "dhfr"
    assert scorer.questions[0][1] == "dhfr"
    assert session.card == 3


def test_protocol_events_are_forwarded_unchanged():
    scorer = _FakeScorer()
    session, events, _c = _session(scorer)
    session.run([_question("q1"), json.dumps({"cmd": "stop"})])
    done = [e for e in events if e["type"] == "answer_done"][0]
    assert set(done) == {"type", "question_id", "target_id", "score",
                         "affinity_pred_value"}


def test_idle_follows_every_question():
    scorer = _FakeScorer()
    session, _e, controls = _session(scorer)
    session.run([_question("q1"), _question("q2"), json.dumps({"cmd": "stop"})])
    assert [c["type"] for c in controls].count(CONTROL_IDLE) == 2


def test_a_scoring_exception_that_escapes_score_is_still_reported_and_survived():
    """AffinityScorer.score() is documented to never raise -- but this worker
    must not bet the booth's Q&A feature on that promise forever (same
    reasoning as runner/worker.py's own FoldError backstop)."""
    scorer = _FakeScorer(outcomes=[RuntimeError("contract violated")])
    session, events, controls = _session(scorer)
    session.run([_question("q1"), _question("q2"), json.dumps({"cmd": "stop"})])
    assert [e["type"] for e in events].count("answer_error") == 1
    assert len(scorer.questions) == 2, "a raising question must not end the worker"
    assert [c["type"] for c in controls].count(CONTROL_IDLE) == 2


def test_an_error_never_omits_the_message_field():
    scorer = _FakeScorer(outcomes=[RuntimeError("/secret/path exploded")])
    session, events, _c = _session(scorer)
    session.run([_question("q1"), json.dumps({"cmd": "stop"})])
    error = [e for e in events if e["type"] == "answer_error"][0]
    assert isinstance(error["message"], str) and error["message"]


def test_a_load_failure_is_fatal_and_says_so_before_exiting():
    scorer = _FakeScorer(on_load=lambda: (_ for _ in ()).throw(
        RuntimeError("device already leased")))
    session, _e, controls = _session(scorer)
    with pytest.raises(SystemExit):
        session.run([_question("q1")])
    assert controls[-1]["type"] == CONTROL_FATAL
    assert CONTROL_READY not in [c["type"] for c in controls]


def test_a_malformed_command_line_is_survived():
    scorer = _FakeScorer()
    session, _e, _c = _session(scorer)
    session.run(["not json{", json.dumps({"cmd": "nonsense"}), _question("q1"),
                json.dumps({"cmd": "stop"})])
    assert len(scorer.questions) == 1


def test_end_of_stdin_ends_the_worker_cleanly():
    """No `.close()` to call here, unlike runner/worker.py's Folder -- but
    `run()` must still return normally rather than raise on plain EOF."""
    scorer = _FakeScorer()
    session, _e, _c = _session(scorer)
    session.run([])   # EOF immediately; must not raise


# ---------------------------------------------------------------------------
# main(): argv -> an AffinityScorer on the right chip, stdin -> commands,
# EVENT_FD -> events. Mirrors test_worker_process.py's departure #2 exactly:
# the brief gives no tests for main(), and this is the one place `--card`
# becomes both the scorer's device_id and every event's implicit card.

class _RecordingScorer(_FakeScorer):
    def __init__(self, device_id=0):
        super().__init__()
        self.device_id = device_id


@pytest.fixture
def qa_worker_main(monkeypatch):
    scorers = []

    def _factory(device_id=0):
        scorer = _RecordingScorer(device_id=device_id)
        scorers.append(scorer)
        return scorer

    monkeypatch.setattr(worker_mod, "AffinityScorer", _factory)

    def _run(argv_card, lines):
        read_fd, write_fd = os.pipe()
        monkeypatch.setattr(sys, "stdin",
                            io.StringIO("".join(f"{line}\n" for line in lines)))
        rc = main(["--card", str(argv_card), "--event-fd", str(write_fd)])
        with os.fdopen(read_fd, "r") as stream:
            emitted = [json.loads(line) for line in stream if line.strip()]
        return rc, scorers, emitted

    return _run


def test_main_gives_its_scorer_the_card_it_was_told_to_use(qa_worker_main):
    _rc, scorers, _events = qa_worker_main(3, [_question("q1"),
                                              json.dumps({"cmd": "stop"})])
    assert [s.device_id for s in scorers] == [3]


def test_main_writes_events_and_control_lines_to_the_event_fd(qa_worker_main):
    _rc, _scorers, events = qa_worker_main(1, [_question("q1"),
                                              json.dumps({"cmd": "stop"})])
    kinds = [e["type"] for e in events]
    assert kinds == [CONTROL_READY, "answer_start", "answer_done", CONTROL_IDLE]


def test_main_never_writes_a_single_byte_of_the_event_stream_to_stdout(
        capfd, qa_worker_main):
    _rc, _scorers, events = qa_worker_main(2, [_question("q1"),
                                              json.dumps({"cmd": "stop"})])
    assert events, "the event fd should have carried the whole stream"
    assert capfd.readouterr().out == ""
