"""QaTracker: server-side Q&A queue state, rendered with ui.questions'
real pure text functions -- so the browser's Q&A panel shows the SAME
text the native QuestionQueuePanel does, computed by the same code."""
from ui.playlist import Question
from webview.qa_tracker import QaTracker


def _questions():
    return [
        Question(id="dhfr_mtx", target_id="dhfr",
                 question="Does methotrexate block dihydrofolate reductase?",
                 ligand_name="Methotrexate", expected_s=None),
        Question(id="fkbp12_sb3", target_id="fkbp12",
                 question="Does SB3 bind FKBP12?", ligand_name="SB3", expected_s=None),
    ]


def test_a_non_qa_event_returns_none():
    tracker = QaTracker(_questions())
    assert tracker.on_event({"type": "job_start", "job_id": "j1"}) is None


def test_answer_start_moves_a_question_to_in_flight():
    tracker = QaTracker(_questions())
    result = tracker.on_event({"type": "answer_start", "question_id": "fkbp12_sb3",
                                "target_id": "fkbp12"})
    assert result["type"] == "qa_queue"
    assert "SB3 bind FKBP12" in result["in_flight"]
    assert "dihydrofolate reductase" in result["pending"]
    assert result["answered"] == "No question answered yet"
    assert result["answered_is_error"] is False


def test_answer_done_moves_it_to_answered_and_clears_in_flight():
    tracker = QaTracker(_questions())
    tracker.on_event({"type": "answer_start", "question_id": "fkbp12_sb3",
                       "target_id": "fkbp12"})
    result = tracker.on_event({"type": "answer_done", "question_id": "fkbp12_sb3",
                                "target_id": "fkbp12", "score": 0.94})
    assert "0.94" in result["answered"]
    assert result["answered_is_error"] is False
    assert result["in_flight"] == "No question in flight"


def test_answer_error_shows_an_honest_failure_not_a_score():
    tracker = QaTracker(_questions())
    tracker.on_event({"type": "answer_start", "question_id": "dhfr_mtx", "target_id": "dhfr"})
    result = tracker.on_event({"type": "answer_error", "question_id": "dhfr_mtx",
                                "target_id": "dhfr"})
    assert result["answered_is_error"] is True
    assert "0." not in result["answered"]


def test_an_unknown_question_id_falls_back_honestly_never_raises():
    """Review Focus: a question_id absent from the loaded catalog (a stale
    questions.yaml, or a race) must degrade to question_label's own
    fallback text, never crash the tracker or the SSE loop."""
    tracker = QaTracker(_questions())
    result = tracker.on_event({"type": "answer_start", "question_id": "not-a-real-id",
                                "target_id": "trypsin"})
    assert "trypsin" in result["in_flight"]
