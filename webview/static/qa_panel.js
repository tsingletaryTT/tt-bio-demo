"use strict";

// QaPanel.render -- pure DOM update for the three-row affinity Q&A queue
// panel (pending / in-flight / answered), driven entirely by the `qa_queue`
// event webview/qa_tracker.py publishes. All three strings arrive already
// rendered by ui.questions' real pure text functions (see qa_tracker.py's
// module docstring) -- this file does no text composition of its own, only
// DOM plumbing, so there is never a second, hand-typed copy of the Q&A
// wording to drift from the native GTK booth's own panel.
function render(dom, event) {
  dom.pending.textContent = event.pending;
  if (typeof setQuestionText === "function") {
    // setQuestionText already exists in app.js (the oompa-bounce
    // animation); animated only while a question is genuinely checking --
    // "motion means pending, stillness means here's the answer." Node's
    // test harness for this file has no app.js loaded, so `setQuestionText`
    // is undefined there and the plain-text fallback below runs instead --
    // both paths are exercised (this file's own test covers the fallback;
    // app.js's real page load covers the animated one).
    const isChecking = event.in_flight !== "No question in flight";
    setQuestionText(dom.inFlight, event.in_flight, { animated: isChecking });
  } else {
    dom.inFlight.textContent = event.in_flight;
  }
  dom.answered.textContent = event.answered;
  dom.answered.classList.toggle("qa-error", !!event.answered_is_error);
}

const QaPanel = { render };
if (typeof module !== "undefined") module.exports = { QaPanel };
if (typeof window !== "undefined") window.QaPanel = QaPanel;
