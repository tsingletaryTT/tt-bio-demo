"""The Q&A spotlight: the quad's own empty fourth cell, put to use.

Affinity Q&A permanently reserves one chip when 2+ are detected
(`runner.workers.split_for_qa`), so a 4-physical-chip booth folds on three
-- and `ui/quad.py`'s 2x2 grid, built from that shorter card list, leaves
its fourth grid slot with nothing attached to it at all. Reported directly:
the rail panel that already shows the Q&A queue (`ui.questions.
QuestionQueuePanel`) reads small at booth distance, and that whole quarter
of the screen sits empty while it does.

This module is that cell's content, not a replacement for the rail panel --
both are wired to the same `on_answer_start`/`on_answer_done`/
`on_answer_error` events (`ui/app.py`'s `_call_question_panel`) and simply
show the same facts at two different sizes. Where the rail panel is a
three-row ledger (pending, in flight, answered, all at once), this cell
shows ONE thing at a time, as large as a cell affords: the question in
flight, or the most recent answer -- the same "one honest statement, not a
dashboard" shape `ui.quad.QuadView.set_notice` already uses for the quad's
own between-folds banner.

**No new wording.** Every string this cell shows comes from
`ui.questions`'s own pure functions (`question_label`, `in_flight_text`,
`error_text`, `format_score`, `SCORE_GLOSS`, `no_question_answered_text`)
rather than a second hand-typed copy of them -- the exact drift this
project's CLAUDE.md has paid for more than once (the manifest/site-copy
mismatch, the exit-code/env-var duplication, the weights-cache dual
derivation). The one thing this module adds that `ui.questions` does not
already say is layout: the score gets its own large line instead of being
folded into `answered_text`'s single string, which is why `format_score`/
`SCORE_GLOSS` are read separately here rather than parsing that string.

**Content-honesty rule, same as `ui.questions`:** no color-coded verdict on
the score (a high number does not turn green, a low one does not turn red)
-- tt-bio's own documentation defines no such thresholds for nesso1's
output, and inventing one here would be exactly the kind of unsupported
claim `ui.questions`'s own module docstring already refuses.

**Hidden until proven capable, same as `ui.questions.QuestionQueuePanel`:**
`set_qa_capable(False)` hides this cell entirely, and a freshly constructed
one starts hidden -- a booth whose daemon has no Q&A worker configured
must not show "AFFINITY QUESTION / No question answered yet" in a cell
implying a capability that isn't there.
"""

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

from ui.questions import (SCORE_GLOSS, BouncingLabel, error_text, format_score,
                           in_flight_text, no_question_answered_text,
                           question_label)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand palette -- this module's own explicit copy, same values as
# ui/panels.py, ui/gallery.py and ui/questions.py each already carry (see
# ui/questions.py's own top-of-file note on why: one small, explicit,
# per-module copy rather than importing another module's leading-underscore
# constants across a boundary).
# ---------------------------------------------------------------------------
_DARK_BASE = "#092221"
_BG = "#F1F8F8"
_BG_ALT = "#C7D9D8"
_ACCENT_TEXT = "#3299B9"
_RED = "#FF9E8A"
_HAIRLINE = "rgba(199, 217, 216, 0.18)"


# ---------------------------------------------------------------------------
# CSS, installed once against the default display -- same guarded-on-a-live-
# -display pattern as ui/panels.py, ui/gallery.py, ui/questions.py and
# ui/quad.py, so constructing this cell never hard-requires a display.
#
# The border/radius/background match `ui.quad`'s own `.quad-cell` rule
# exactly (same hex values, independently declared -- ui.quad's are its own
# module-private constants, not reached into) so the spotlight reads as a
# fifth sibling of the three real cells rather than a foreign panel dropped
# into their grid.
# ---------------------------------------------------------------------------
_CSS_INSTALLED = False

_BACKGROUND_BY_CLASS = {
    "qa-spotlight": _DARK_BASE,
}

_QA_SPOTLIGHT_CSS = f"""
.qa-spotlight {{
    background-color: {_BACKGROUND_BY_CLASS["qa-spotlight"]};
    border: 2px solid {_HAIRLINE};
    border-radius: 6px;
    padding: 10px 12px;
}}
.qa-spotlight-heading {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: {_BG_ALT};
}}
.qa-spotlight-question {{
    font-size: 14px;
    font-weight: 600;
    color: {_BG};
}}
.qa-spotlight-question.qa-spotlight-error {{
    color: {_RED};
}}
.qa-spotlight-score {{
    font-size: 30px;
    font-weight: 500;
    color: {_ACCENT_TEXT};
}}
.qa-spotlight-gloss {{
    font-size: 11px;
    color: {_BG_ALT};
}}
.qa-spotlight-pending {{
    font-size: 10px;
    color: {_BG_ALT};
}}
"""


def _ensure_css_installed():
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        log.debug("no default display; skipping qa-spotlight CSS install")
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_QA_SPOTLIGHT_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


class QASpotlightCell(Gtk.Box):
    """One quad cell's worth of Q&A state: a heading, one centered
    statement, and a pending count tucked in the corner.

    Same construction and event-handling shape as
    `ui.questions.QuestionQueuePanel` deliberately (a `questions` list at
    construction, `on_answer_start`/`on_answer_done`/`on_answer_error` as
    the only way state changes) -- `ui/app.py` drives both from the same
    `_call_question_panel` fan-out, so the two must accept the same calls
    or one of them silently stops updating.
    """

    def __init__(self, questions=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        _ensure_css_installed()
        self.add_css_class("qa-spotlight")
        self.set_hexpand(True)
        self.set_vexpand(True)

        # Same lookup-by-id shape as QuestionQueuePanel, and the same
        # reason: on_answer_start/_done/_error carry a bare question_id,
        # and the real question text (for the label a visitor reads) lives
        # only in the playlist this panel was constructed with.
        self._by_id = {question.id: question for question in (questions or [])}
        self._questions = list(questions or [])
        self._in_flight = None  # dict: question_id, target_id, question_text
        self._answered = None   # dict: question_id, target_id, score, question_text, error

        heading = Gtk.Label(label="AFFINITY QUESTION", xalign=0.0)
        heading.add_css_class("qa-spotlight-heading")
        self.append(heading)

        # The one centered statement -- question line, then EITHER the
        # score (answered) or a spinner (in flight). Never both: a spinner
        # next to a stale score would read as "still checking" over an
        # answer that already arrived.
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        center.set_valign(Gtk.Align.CENTER)
        center.set_vexpand(True)
        center.set_halign(Gtk.Align.FILL)

        # BouncingLabel (ui/questions.py), not a plain Gtk.Label: this is the
        # one place in the quad that shows a question actually being scored,
        # so it gets the same in-flight bounce the rail's own
        # QuestionQueuePanel does -- one shared implementation, not a second
        # hand-typed animation. `set_halign(CENTER)` overrides that class's
        # own FILL default, matching this cell's centered layout; animated
        # only in the in-flight branch of `_render` below, same as there.
        self._question_label = BouncingLabel("qa-spotlight-question")
        self._question_label.set_halign(Gtk.Align.CENTER)
        center.append(self._question_label)

        self._spinner = Gtk.Spinner()
        self._spinner.set_halign(Gtk.Align.CENTER)
        center.append(self._spinner)

        self._score_label = Gtk.Label(xalign=0.5)
        self._score_label.add_css_class("qa-spotlight-score")
        center.append(self._score_label)

        self._gloss_label = Gtk.Label(xalign=0.5)
        self._gloss_label.set_wrap(True)
        self._gloss_label.set_justify(Gtk.Justification.CENTER)
        self._gloss_label.add_css_class("qa-spotlight-gloss")
        center.append(self._gloss_label)

        self.append(center)

        self._pending_label = Gtk.Label(xalign=1.0)
        self._pending_label.add_css_class("qa-spotlight-pending")
        self._pending_label.set_halign(Gtk.Align.END)
        self.append(self._pending_label)

        # Hidden until proven capable -- see the module docstring.
        self.set_visible(False)

        self._render()

    def set_qa_capable(self, capable):
        """Show or hide the whole cell. `False` hides it entirely -- a
        booth whose daemon has no Q&A worker configured must not show this
        cell implying a capability that isn't there."""
        self.set_visible(bool(capable))

    def on_answer_start(self, question_id, target_id):
        """A question moved from pending to in-flight -- shows the
        question, large, with an indeterminate spinner (no fake progress
        bar; nesso1 is a single fast call, not a staged pipeline, the same
        reasoning `ui.questions`'s module docstring gives)."""
        question = self._by_id.get(question_id)
        self._in_flight = {
            "question_id": question_id,
            "target_id": target_id,
            "question_text": getattr(question, "question", None),
        }
        self._render()

    def on_answer_done(self, question_id, target_id, score):
        """nesso1 returned a real score. Clears the in-flight state if this
        was the question in flight and shows the question plus the score,
        large, exactly as `on_answer_error` shows an honest failure
        instead."""
        question = self._by_id.get(question_id)
        self._clear_in_flight_if_matches(question_id)
        self._answered = {
            "question_id": question_id,
            "target_id": target_id,
            "score": score,
            "question_text": getattr(question, "question", None),
            "error": False,
        }
        self._render()

    def on_answer_error(self, question_id, target_id):
        """Scoring failed. Shows an honest "not answered" -- never a
        fabricated score or a silently-stale previous answer left looking
        current."""
        question = self._by_id.get(question_id)
        self._clear_in_flight_if_matches(question_id)
        self._answered = {
            "question_id": question_id,
            "target_id": target_id,
            "score": None,
            "question_text": getattr(question, "question", None),
            "error": True,
        }
        self._render()

    def get_display_text(self):
        """Every label's rendered text, joined -- what a test reads to
        check what this cell actually SHOWS, the same reason
        `QuestionQueuePanel.get_display_text` exists: read what was
        rendered, not internal state that might disagree with the widget."""
        return "\n".join([
            self._question_label.get_label(),
            self._score_label.get_label(),
            self._gloss_label.get_label(),
            self._pending_label.get_label(),
        ])

    def _clear_in_flight_if_matches(self, question_id):
        if self._in_flight is not None and self._in_flight["question_id"] == question_id:
            self._in_flight = None

    def _pending_count(self):
        """How many of this booth's questions are neither in flight nor the
        most recent answer -- the same derivation `QuestionQueuePanel.
        _pending_questions` makes (recomputed every render, against a fixed
        question list that is asked on repeat forever), reduced here to a
        count because this cell has room for one small line, not a list."""
        exclude = set()
        if self._in_flight is not None:
            exclude.add(self._in_flight["question_id"])
        if self._answered is not None:
            exclude.add(self._answered["question_id"])
        return len([q for q in self._questions if q.id not in exclude])

    def _render(self):
        pending = self._pending_count()
        self._pending_label.set_label(
            "" if pending == 0 else f"{pending} more queued")

        self._question_label.remove_css_class("qa-spotlight-error")

        if self._in_flight is not None:
            self._question_label.set_text(
                in_flight_text(self._in_flight["target_id"],
                               self._in_flight["question_text"]),
                animated=True)
            self._spinner.set_visible(True)
            self._spinner.start()
            self._score_label.set_visible(False)
            self._score_label.set_label("")
            self._gloss_label.set_label("")
            return

        self._spinner.set_visible(False)
        self._spinner.stop()

        if self._answered is None:
            self._question_label.set_text(no_question_answered_text(), animated=False)
            self._score_label.set_visible(False)
            self._score_label.set_label("")
            self._gloss_label.set_label("")
            return

        target_id = self._answered["target_id"]
        question_text = self._answered["question_text"]
        if self._answered["error"]:
            self._question_label.add_css_class("qa-spotlight-error")
            self._question_label.set_text(error_text(target_id, question_text), animated=False)
            self._score_label.set_visible(False)
            self._score_label.set_label("")
            self._gloss_label.set_label("")
            return

        self._question_label.set_text(question_label(question_text, target_id), animated=False)
        formatted = format_score(self._answered["score"])
        if formatted is None:
            self._score_label.set_visible(False)
            self._score_label.set_label("")
            self._gloss_label.set_label("score: not available")
        else:
            self._score_label.set_visible(True)
            self._score_label.set_label(formatted)
            self._gloss_label.set_label(SCORE_GLOSS)
