"""QaTracker: server-side affinity-Q&A queue state, rendered with
ui.questions' real pure text functions.

Mirrors ui/questions.py's QuestionQueuePanel exactly (same pending/
in-flight/answered derivation), running here instead of in a GTK label
setter -- so the browser's Q&A panel shows text computed by the SAME code
the native panel calls, never a second hand-typed copy in JS. See
docs/superpowers/specs/2026-09-24-webview-parity-design.md section 6.
"""
import logging

from ui.questions import (answered_text, error_text, in_flight_text,
                           no_question_answered_text, no_question_in_flight_text,
                           pending_text)

log = logging.getLogger(__name__)


class QaTracker:
    def __init__(self, questions):
        self._questions = list(questions)
        self._by_id = {q.id: q for q in self._questions}
        self._in_flight = None   # dict: question_id, target_id, question_text
        self._answered = None    # dict: question_id, target_id, score, question_text, error

    def _pending_questions(self):
        exclude = set()
        if self._in_flight is not None:
            exclude.add(self._in_flight["question_id"])
        if self._answered is not None:
            exclude.add(self._answered["question_id"])
        return [q for q in self._questions if q.id not in exclude]

    def _render(self):
        pending = pending_text(self._pending_questions())
        if self._in_flight is not None:
            in_flight = in_flight_text(self._in_flight["target_id"],
                                       self._in_flight["question_text"])
        else:
            in_flight = no_question_in_flight_text()
        if self._answered is not None:
            if self._answered["error"]:
                answered = error_text(self._answered["target_id"],
                                      self._answered["question_text"])
                is_error = True
            else:
                answered = answered_text(self._answered["target_id"],
                                         self._answered["score"],
                                         self._answered["question_text"])
                is_error = False
        else:
            answered = no_question_answered_text()
            is_error = False
        return {"type": "qa_queue", "pending": pending, "in_flight": in_flight,
                "answered": answered, "answered_is_error": is_error}

    def _clear_in_flight_if_matches(self, question_id):
        if self._in_flight is not None and self._in_flight["question_id"] == question_id:
            self._in_flight = None

    def on_event(self, event):
        kind = event.get("type")
        question_id = event.get("question_id")
        target_id = event.get("target_id")
        question = self._by_id.get(question_id)
        question_text = getattr(question, "question", None)
        if kind == "answer_start":
            self._in_flight = {"question_id": question_id, "target_id": target_id,
                               "question_text": question_text}
        elif kind == "answer_done":
            self._clear_in_flight_if_matches(question_id)
            self._answered = {"question_id": question_id, "target_id": target_id,
                              "score": event.get("score"), "question_text": question_text,
                              "error": False}
        elif kind == "answer_error":
            self._clear_in_flight_if_matches(question_id)
            self._answered = {"question_id": question_id, "target_id": target_id,
                              "score": None, "question_text": question_text,
                              "error": True}
        else:
            return None
        return self._render()
