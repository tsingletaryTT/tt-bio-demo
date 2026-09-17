"""The affinity-question rail panel: `QuestionQueuePanel`.

A third rail panel, alongside `ui.panels.PipelinePanel` and
`ui.panels.TelemetryPanel`, showing the affinity Q&A queue's
pending/in-flight/answered state (design spec,
docs/superpowers/specs/2026-09-15-affinity-qa-design.md, section 6). It
mirrors `PipelinePanel`'s construction pattern deliberately -- same
`Gtk.Box` orientation/spacing idioms, same "install CSS once against the
default display, guarded on one existing at all" pattern, same
`_BACKGROUND_BY_CLASS` + `_QUESTIONS_CSS` module-private pair that
tests/unit/_legibility.py's shared legibility walker consumes -- so the
rail's three panels read as one visual language, not three independently
designed widgets bolted together.

Like `ui.panels`, this module keeps the drawing thin and the decisions
pure: `pending_text`/`in_flight_text`/`answered_text`/`error_text`/
`expected_time_text` are plain functions over strings and numbers, directly
tested with no GTK involved at all, and `QuestionQueuePanel` itself owns
only layout, CSS classes, and calling them.

**Content-honesty rule (spec section 7), the reason this module exists in
this exact shape:** an affinity score is a raw number nesso1 produced, and
this booth has a hard-won, repeatedly-paid-for standard against fabricating
claims a model's own output doesn't support (see this project's CLAUDE.md:
the DNA/tRNA blurbs, the FKBP12 pLDDT-spread note, the "expected_s means the
warm state a visitor actually meets" rule -- all the same discipline).
`answered_text` renders the score rounded to 2 decimal places (the same
`.2f` convention `ui/diagnostics.py` already uses for this exact field) plus
one short, fixed, factual gloss of what the number IS -- and nothing else
about it: no "binds tightly/weakly/strongly" tier this project invented,
because tt-bio's own documentation defines no such tiers for nesso1's
output. If a future tt-bio release documents real thresholds, that is a
copy change to make deliberately, with a citation, not a stylistic flourish
to slip in here.

**No fake progress bar for the in-flight state.** nesso1 is a single fast
scalar call, not a multi-stage pipeline like a fold -- it emits no
stage/frame events to drive a progress bar with, and building one anyway
(interpolated against a guessed duration, say) would be exactly the kind of
invented-but-plausible-looking claim the content-honesty rule above exists
to rule out, just aimed at time instead of at the score. A `Gtk.Spinner`
(indeterminate, no fraction) is the honest representation of a call this
project cannot subdivide.
"""

import logging
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand palette -- the same values ui/panels.py and ui/gallery.py each carry
# their own copy of (see ui/gallery.py's own top-of-file comment on why: one
# small, explicit, per-module copy rather than importing another module's
# leading-underscore constants across a boundary that would make one
# module's stylesheet quietly depend on another's internals).
# ---------------------------------------------------------------------------
_DARK_BASE = "#092221"
_BG = "#F1F8F8"
_BG_ALT = "#C7D9D8"
_ACCENT_TEXT = "#3299B9"  # see ui/panels.py's own note: +10% toward white
                           # off the brand accent, so it clears 4.5:1 as TEXT
                           # on _DARK_BASE (ui/panels.py measures 5.06:1).
_RED = "#FF9E8A"
_HAIRLINE = "rgba(199, 217, 216, 0.18)"  # _BG_ALT at 18% opacity


# ---------------------------------------------------------------------------
# Pure decisions: text only, no GTK -- so the content-honesty rule is
# testable without a display, and the widget below is thin assembly on top
# of these, never inventing wording of its own.
# ---------------------------------------------------------------------------

def question_label(question_text, target_id):
    """The text a row shows for one question.

    Degrades honestly when the real question text isn't known -- e.g. an
    `on_answer_start`/`on_answer_done` call naming a `question_id` this
    panel has no matching `playlist/questions.yaml` entry loaded for (the
    brief's own tests construct a bare panel and call these with no prior
    question list at all). The fallback still names the real target being
    checked, which is always known -- it never invents a question.

    Public (no leading underscore) because `ui.qa_spotlight`'s spotlight
    cell needs the exact same fallback sentence for the quad's own empty
    cell -- reusing this function is what keeps that fallback from
    becoming a second hand-typed copy of the same sentence, the drift this
    project's CLAUDE.md has paid for more than once (the manifest/site-copy
    drift, the exit-code/env-var duplication).
    """
    if question_text:
        return question_text
    return f"Does the ligand bind {target_id}?"


def expected_time_text(expected_s):
    """Mirrors `ui.gallery`'s own handling of an unmeasured
    `Target.expected_s` (spec section 7): `None` reads as an explicit "not
    yet timed", never a guessed number standing in for a real measurement.
    """
    if expected_s is None:
        return "not yet timed"
    return f"~{expected_s:.1f}s"


def pending_text(pending):
    """The pending-row's text for a list of `ui.playlist.Question`-like
    objects (anything with `.question`, `.target_id`, and optionally
    `.expected_s`).

    An empty queue reads as an explicit "No questions queued" -- never a
    blank label a visitor (or a future maintainer skimming a screenshot)
    could mistake for a rendering bug rather than real information, the
    same distinction `ui.panels.TelemetryPanel` draws between "no reading
    yet" and "a reading of nothing."
    """
    if not pending:
        return "No questions queued"
    lines = []
    for question in pending:
        text = question_label(getattr(question, "question", None),
                               question.target_id)
        expected = expected_time_text(getattr(question, "expected_s", None))
        lines.append(f"{text} ({expected})")
    return "\n".join(lines)


def in_flight_text(target_id, question_text=None):
    """The in-flight row's text: what is being checked right now, with no
    claim about how far along it is (see the module docstring on why this
    panel has no progress bar at all)."""
    return f"Checking — {question_label(question_text, target_id)}"


def no_question_in_flight_text():
    """The in-flight row's text when nothing is currently being scored --
    pulled into its own pure function for the same reason `pending_text`'s
    own "No questions queued" fallback is: the module's stated design keeps
    every string this panel can show as a pure function, and this one was
    the last inline literal left in `_render`."""
    return "No question in flight"


def no_question_answered_text():
    """The answered row's text before any question has ever been answered
    (or after `on_answer_error` clears back to this state -- it doesn't;
    see `_render`, this is the construction-time default only). Same
    reasoning as `no_question_in_flight_text` above."""
    return "No question answered yet"


# A short, fixed, factual gloss -- spec section 7's "a one-line factual
# gloss, not a fabricated confidence category". States only what tt-bio's
# nesso1 API is documented to return (docs/spike-nesso1-affinity.md section
# 3.3: `affinity_probability_binary`, a literal [0, 1] probability of being
# a binder), never a verdict word this project's content-honesty rule
# forbids. One constant, used everywhere the score is shown, so the wording
# cannot drift between call sites the way the manifest/site-copy drift this
# project has paid for more than once (CLAUDE.md's "nine model families"
# section, the DNA/tRNA blurbs) always starts as two hand-typed copies of
# the same sentence. Public for the same reason `question_label` is: a
# second surface (`ui.qa_spotlight`) shows this exact score, and it must
# read the one constant rather than carry a second hand-typed copy of it.
SCORE_GLOSS = "nesso1's predicted probability the ligand binds"


def format_score(score):
    """Round a raw nesso1 score to 2 decimal places for display, or `None`
    if `score` isn't a real number at all.

    Real captured values look like `0.9769678115844727`
    (`AffinityScorer._score_real`'s own docstring) -- a 16-digit float is
    not a "raw number, no invented claims" rendering, it is noise this
    project's own content-honesty rule was never meant to license. Rounding
    the DISPLAYED value is not fabricating a claim about precision nesso1
    doesn't have; it is the same `.2f` convention `ui/diagnostics.py`
    already uses for this exact field (and for `wall_s`), so the two
    surfaces that show a score agree on how many digits it gets.

    `None`-safe (and malformed-input-safe generally, the same "wire data is
    never trusted" discipline `ui/diagnostics.py`'s own `_num` documents):
    an `answer_done` with a missing/non-numeric `score` must render as an
    honest "not available", never literally the word `None` or a
    traceback.

    Public for the same reason `question_label`/`SCORE_GLOSS` are: the quad
    spotlight cell (`ui.qa_spotlight`) needs the same rounded number, laid
    out on its own line rather than folded into `answered_text`'s single
    string -- reusing this function is what keeps the two surfaces agreeing
    on precision without either re-deriving it.
    """
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return format(value, ".2f")


def answered_text(target_id, score, question_text=None):
    """The answered row's text: the content-honesty rule, as code.

    The model's own score, rounded for display (`format_score`) and
    followed by `SCORE_GLOSS` -- a fixed, factual statement of what the
    number IS (spec section 7) -- and nothing else said about it. No
    invented verdict adjective ("binds tightly", "weak binder", ...) that
    nesso1's own output does not supply; see the module docstring for why
    this is load-bearing rather than a style preference.

    A `score` that isn't a real number (see `format_score`) renders as
    "not available" rather than the literal string "None" -- the same
    "never show wire-shaped data verbatim" rule this file already applies
    to `answer_error`.
    """
    label = question_label(question_text, target_id)
    formatted = format_score(score)
    if formatted is None:
        return f"{label}\nscore: not available"
    return f"{label}\nscore: {formatted} — {SCORE_GLOSS}"


def error_text(target_id, question_text=None):
    """The answered row's text when scoring failed. Honest about what
    happened (nothing was answered) rather than fabricating a score or a
    verdict to fill the space."""
    return f"{question_label(question_text, target_id)} — not answered (error)"


# ---------------------------------------------------------------------------
# CSS, installed once against the default display -- same pattern as
# ui/panels.py and ui/gallery.py (guarded on a live display existing at
# all, so constructing a QuestionQueuePanel never hard-requires one).
#
# `_BACKGROUND_BY_CLASS` is this module's own single source of truth for
# "which CSS class carries an explicitly-set background," read by
# tests/unit/test_questions_panel.py via the same shared
# tests/unit/_legibility.py walker ui/panels.py's and ui/gallery.py's own
# tests use -- one legibility mechanism applied to a third stylesheet, not
# a fourth independently maintained copy of it.
# ---------------------------------------------------------------------------
_CSS_INSTALLED = False

_BACKGROUND_BY_CLASS = {
    "question-queue-panel": _DARK_BASE,
}

_QUESTIONS_CSS = f"""
.question-queue-panel {{
    background-color: {_BACKGROUND_BY_CLASS["question-queue-panel"]};
    padding: 12px 16px;
    border-radius: 6px;
}}
.question-queue-row {{
    padding-bottom: 6px;
    margin-bottom: 6px;
}}
.question-queue-row-divider {{
    border-bottom: 1px solid {_HAIRLINE};
}}
.question-queue-heading {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: {_BG_ALT};
}}
.question-queue-pending-text {{
    font-size: 12px;
    color: {_BG_ALT};
}}
.question-queue-inflight-text {{
    font-size: 13px;
    font-weight: 700;
    color: {_ACCENT_TEXT};
}}
.question-queue-answered-text {{
    font-size: 12px;
    color: {_BG};
}}
.question-queue-answered-text.question-queue-answered-error {{
    color: {_RED};
}}
"""


def _ensure_css_installed():
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        log.debug("no default display; skipping question-queue CSS install")
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_QUESTIONS_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


class QuestionQueuePanel(Gtk.Box):
    """One rail panel, three rows -- pending, in-flight, answered -- for the
    affinity Q&A queue (spec section 6). Thin assembly over the pure
    functions above: this class owns layout, CSS classes, and calling them
    with the right arguments, and nothing about wording.

    - **Pending**: every question this panel knows about that is not
      RIGHT NOW the one in flight or the one just answered -- see
      `_pending_questions`. This booth's playlist of questions is small and
      FIXED, and the attract loop cycles through it forever rather than
      draining a one-shot queue, so "pending" is a live derivation
      (recomputed on every render) rather than a list that only ever
      shrinks -- Important 4, whole-branch review, replacing an earlier
      design that started full, drained to permanently empty once every
      question had been asked once, and never reflected a question bumped
      straight to `answer_error` without ever going through
      `on_answer_start`.
    - **In flight**: the one question currently being scored -- a
      `Gtk.Spinner` (indeterminate; no fake progress bar, see the module
      docstring) plus the question text.
    - **Answered**: the most recent result -- the question text plus the
      score (rounded, with a one-line factual gloss -- see `answered_text`)
      per the content-honesty rule. An `on_answer_error` renders an honest
      "not answered" instead of a score.

    `set_qa_capable(False)` hides the whole panel (`self.set_visible`) --
    a booth whose daemon has no Q&A worker configured must never show an
    empty question rail implying a capability it doesn't have, the same
    "no telemetry is not fake telemetry" discipline
    `ui.panels.TelemetryPanel` already follows. A freshly constructed panel
    starts hidden for the same reason: it has not yet been TOLD the daemon
    can answer anything.
    """

    def __init__(self, questions=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _ensure_css_installed()
        self.add_css_class("question-queue-panel")

        # Keyed by Question.id, for on_answer_start/_done/_error to look up
        # the real question text (and expected_s) a bare `question_id` alone
        # doesn't carry. Empty when no questions were supplied -- every
        # lookup then honestly falls through to `question_label`'s target-
        # only fallback rather than raising.
        self._by_id = {question.id: question for question in (questions or [])}
        # The full, fixed set this booth knows how to ask -- in the order
        # they were supplied. Kept once, at construction, and never mutated
        # afterwards: see `_pending_questions` for why "pending" is DERIVED
        # from this list every render rather than being a second, separately
        # mutated list of its own (Important 4, whole-branch review).
        self._questions = list(questions or [])
        self._in_flight = None  # dict: question_id, target_id, question_text
        self._answered = None   # dict: question_id, target_id, score, question_text, error

        heading = Gtk.Label(label="AFFINITY QUESTIONS", xalign=0.0)
        heading.add_css_class("question-queue-heading")
        self.append(heading)

        pending_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        pending_row.add_css_class("question-queue-row")
        pending_row.add_css_class("question-queue-row-divider")
        self._pending_label = Gtk.Label(xalign=0.0)
        self._pending_label.set_wrap(True)
        self._pending_label.add_css_class("question-queue-pending-text")
        pending_row.append(self._pending_label)
        self.append(pending_row)

        in_flight_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        in_flight_row.add_css_class("question-queue-row")
        in_flight_row.add_css_class("question-queue-row-divider")
        self._spinner = Gtk.Spinner()
        in_flight_row.append(self._spinner)
        self._in_flight_label = Gtk.Label(xalign=0.0)
        self._in_flight_label.set_wrap(True)
        self._in_flight_label.set_hexpand(True)
        self._in_flight_label.add_css_class("question-queue-inflight-text")
        in_flight_row.append(self._in_flight_label)
        self.append(in_flight_row)

        answered_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        answered_row.add_css_class("question-queue-row")
        self._answered_label = Gtk.Label(xalign=0.0)
        self._answered_label.set_wrap(True)
        self._answered_label.add_css_class("question-queue-answered-text")
        answered_row.append(self._answered_label)
        self.append(answered_row)

        # Hidden until proven capable -- see the class docstring.
        self.set_visible(False)

        self._render()

    def set_qa_capable(self, capable):
        """Show or hide the whole panel. `False` hides it entirely: a booth
        whose daemon has no Q&A worker configured must not show an empty
        question rail implying a capability that isn't there."""
        self.set_visible(bool(capable))

    def _pending_questions(self):
        """The questions that read as "pending" right now: every question
        this panel knows about, MINUS whichever one is currently in flight
        and MINUS whichever one is the most recent answer -- recomputed on
        every render, never a list that only ever drains.

        Why a derivation and not a stored, mutated list (Important 4,
        whole-branch review, replacing the original `self._pending = list
        (questions)` / "remove on `on_answer_start`, never add back"
        design): this booth has a small, FIXED set of questions
        (`playlist/questions.yaml`'s three entries) that the attract loop
        asks on repeat forever -- it is not a one-shot queue that starts
        full and empties as each item is consumed. A list that only ever
        shrinks gets three things wrong, all from the same root cause (it
        answers "what have we not yet started, ever" instead of "what is
        not the current activity"):

        - At startup, before anything has ever been asked, it claimed all
          three were "queued" -- true only in the sense that they will
          eventually be asked, which is true of literally every question
          this panel will ever show, forever, and is not what a visitor
          reading "queued" understands by the word.
        - Once every question has been asked once (~4.5 minutes at the
          attract loop's cadence), it was permanently empty -- "No
          questions queued" -- even though the loop keeps asking the same
          three on repeat indefinitely. A drained list has no way to ever
          refill itself.
        - A question BUMPED out of the daemon's queue
          (`runner.daemon.MAX_PENDING_QUESTIONS`) before ever reaching
          `on_answer_start` -- it goes straight to `on_answer_error` -- was
          never removed at all, so it stayed listed as "pending" forever,
          even while its own row below correctly showed it as errored.

        Deriving fixes all three at once: nothing is ever "pending" before
        it has genuinely not started, nothing is ever incorrectly claimed
        empty (there are always at least `len(self._questions) - 2`
        pending, and with the shipped three questions and one in-flight/one
        answered at a time, that is genuinely nearly always at least one),
        and a bumped-and-errored question is excluded for exactly as long
        as ITS error is the most recent result -- the same "not the current
        activity" rule applied to the error case, not a special case bolted
        on for it.
        """
        exclude = set()
        if self._in_flight is not None:
            exclude.add(self._in_flight["question_id"])
        if self._answered is not None:
            exclude.add(self._answered["question_id"])
        return [q for q in self._questions if q.id not in exclude]

    def on_answer_start(self, question_id, target_id):
        """A question moved from pending to in-flight. Starts the
        indeterminate spinner (see the module docstring for why there is
        no progress bar). Nothing to remove from a stored pending list any
        more -- `_pending_questions` derives it fresh from `self._in_flight`
        every render (see that method's docstring)."""
        question = self._by_id.get(question_id)
        self._in_flight = {
            "question_id": question_id,
            "target_id": target_id,
            "question_text": getattr(question, "question", None),
        }
        self._render()

    def on_answer_done(self, question_id, target_id, score):
        """nesso1 returned a real score. Renders it plainly (see
        `answered_text`) and clears the in-flight state if this was the
        question in flight."""
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
        """Scoring failed. Renders an honest "not answered" -- never a
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
        """All three rows' rendered text, joined -- what a test (and the
        content-honesty check in particular) reads to check what the panel
        actually SHOWS, mirroring how `ui.panels.TelemetryPanel` exposes
        `last_status`/`ui.panels.PipelinePanel` exposes `last_rows` for the
        same reason: a test should read what was rendered, not re-derive it
        from internal state that might disagree with the widget."""
        return "\n".join([
            self._pending_label.get_label(),
            self._in_flight_label.get_label(),
            self._answered_label.get_label(),
        ])

    def _clear_in_flight_if_matches(self, question_id):
        if self._in_flight is not None and self._in_flight["question_id"] == question_id:
            self._in_flight = None

    def _render(self):
        self._pending_label.set_label(pending_text(self._pending_questions()))

        if self._in_flight is not None:
            self._in_flight_label.set_label(
                in_flight_text(self._in_flight["target_id"],
                               self._in_flight["question_text"]))
            self._spinner.start()
        else:
            self._in_flight_label.set_label(no_question_in_flight_text())
            self._spinner.stop()

        if self._answered is not None:
            if self._answered["error"]:
                text = error_text(self._answered["target_id"],
                                   self._answered["question_text"])
                self._answered_label.add_css_class("question-queue-answered-error")
            else:
                text = answered_text(self._answered["target_id"],
                                      self._answered["score"],
                                      self._answered["question_text"])
                self._answered_label.remove_css_class("question-queue-answered-error")
            self._answered_label.set_label(text)
        else:
            self._answered_label.remove_css_class("question-queue-answered-error")
            self._answered_label.set_label(no_question_answered_text())
