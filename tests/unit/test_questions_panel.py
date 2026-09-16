"""Tests for ui/questions.py: `QuestionQueuePanel`, the third rail panel
(alongside `ui.panels.PipelinePanel`/`TelemetryPanel`) showing the affinity
Q&A queue's pending/in-flight/answered state.

Constructing the widget needs a live display (it builds real
Gtk.Label/Gtk.Box/Gtk.Spinner children in __init__, same as
`ui.panels.PipelinePanel`) -- this box has one (DISPLAY=:0,
WAYLAND_DISPLAY=wayland-0), so these run against the real thing, matching
tests/unit/test_panels.py's own convention rather than faking a display.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

import pytest
from gi.repository import Gtk

import _legibility
from ui.panels import MIN_CONTRAST_RATIO, contrast_ratio
from ui.playlist import Question
from ui.questions import (
    QuestionQueuePanel,
    answered_text,
    error_text,
    expected_time_text,
    in_flight_text,
    pending_text,
)

# ---------------------------------------------------------------------------
# The brief's own tests, verbatim (test_in_flight_question_shows_its_text and
# test_answered_question_shows_the_score_not_a_fabricated_verdict have their
# placeholder comments filled in per the brief's own instruction: "follow
# PipelinePanel's own existing test pattern").
# ---------------------------------------------------------------------------

def test_hides_entirely_when_not_qa_capable():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(False)
    assert panel.get_visible() is False


def test_shows_when_qa_capable():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    assert panel.get_visible() is True


def test_in_flight_question_shows_its_text():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    panel.on_answer_start("q1", "dhfr")
    assert "dhfr" in panel.get_display_text()


def test_answered_question_shows_the_score_not_a_fabricated_verdict():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    panel.on_answer_done("q1", "dhfr", score=0.5)
    # Assert the rendered text contains the score, rounded to 2 places (the
    # same convention ui/diagnostics.py already uses for this field), and
    # does NOT contain any of the forbidden invented-verdict words -- this
    # is the content-honesty rule from spec section 7, enforced as a test,
    # not just prose. Exact text, not a substring check: "score: 0.5" is
    # ALSO a substring of "score: 0.50", which would let this assertion
    # pass by coincidence against the wrong format entirely.
    assert "score: 0.50" in panel.get_display_text()
    for word in ("tightly", "weakly", "strongly"):
        assert word not in panel.get_display_text().lower()


# ---------------------------------------------------------------------------
# A panel is hidden until proven capable -- the same "no telemetry is not
# fake telemetry" discipline TelemetryPanel already follows: a booth whose
# daemon has no Q&A worker configured must never show a question rail
# implying a capability it doesn't have.
# ---------------------------------------------------------------------------

def test_a_fresh_panel_is_hidden_before_qa_capable_is_ever_called():
    panel = QuestionQueuePanel()
    assert panel.get_visible() is False


def test_set_qa_capable_can_toggle_back_and_forth():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    assert panel.get_visible() is True
    panel.set_qa_capable(False)
    assert panel.get_visible() is False


# ---------------------------------------------------------------------------
# The content-honesty rule, more thoroughly: not just the three words the
# brief names, and not just at score=0.5 -- any score, any of a slightly
# wider set of invented-verdict adjectives this project's history (the
# DNA/tRNA blurbs, the FKBP12 pLDDT-spread note) treats as the same class of
# defect as a fabricated number.
# ---------------------------------------------------------------------------

_FORBIDDEN_VERDICT_WORDS = (
    "tightly", "weakly", "strongly", "loosely", "poorly", "excellent",
    "great", "binds well", "does not bind", "no binding",
)


@pytest.mark.parametrize("score,formatted", [
    (0.0, "0.00"), (0.5, "0.50"), (-3.2, "-3.20"), (12.75, "12.75"),
])
def test_no_answered_score_ever_grows_a_fabricated_verdict(score, formatted):
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    panel.on_answer_done("q1", "dhfr", score=score)
    text = panel.get_display_text().lower()
    # The EXACT rounded text, not a substring of the raw score -- "score:
    # 0.5" is also a substring of "score: 0.50", which would let this
    # assertion pass against a format this test was not actually written to
    # allow. See test_the_score_is_rounded_to_two_decimal_places below for
    # the case (a real captured nesso1 value) this distinction actually
    # matters for.
    assert f"score: {formatted}" in panel.get_display_text()
    for word in _FORBIDDEN_VERDICT_WORDS:
        assert word not in text, f"fabricated verdict word {word!r} in {text!r}"


def test_answered_text_pure_function_contains_the_raw_score_only():
    """The pure function directly, so the content-honesty rule is pinned
    independently of the widget's own assembly -- the same split
    ui.panels's module docstring describes ("keep the drawing thin and the
    decisions pure")."""
    text = answered_text("dhfr", 0.5, "Does methotrexate bind DHFR?")
    assert "score: 0.50" in text
    assert text.count("score:") == 1
    for word in _FORBIDDEN_VERDICT_WORDS:
        assert word not in text.lower()


# ---------------------------------------------------------------------------
# Important 2 (whole-branch review): a real captured nesso1 score is a
# 16-digit float (`AffinityScorer._score_real`'s own docstring:
# 0.9769678115844727), and neither `answered_text` nor `QuestionQueuePanel`
# rounded it before this fix -- every test/fixture up to this point used a
# hand-rounded literal (0.5, 0.87), which is exactly why this went unnoticed.
# ---------------------------------------------------------------------------

def test_the_score_is_rounded_to_two_decimal_places():
    text = answered_text("dhfr", 0.9769678115844727)
    assert "score: 0.98" in text
    assert "0.9769678115844727" not in text


def test_the_panel_rounds_a_real_captured_score_too():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    panel.on_answer_done("q1", "dhfr", score=0.9769678115844727)
    text = panel.get_display_text()
    assert "score: 0.98" in text
    assert "0.9769678115844727" not in text


def test_the_score_carries_a_one_line_factual_gloss():
    """Spec section 7 / whole-branch review finding 5: the score needs "a
    one-line factual gloss" alongside the raw number -- units/scale/what it
    IS, never a verdict. Checked as a real assertion (the exact gloss text
    is a constant this module owns), not just "some text is present"."""
    text = answered_text("dhfr", 0.87)
    assert "nesso1's predicted probability the ligand binds" in text


def test_a_missing_or_malformed_score_never_renders_the_word_none():
    """`on_answer_done`'s `score` is wire-sourced (see ui/diagnostics.py's
    own `_num` for the same discipline applied to this exact field) -- a
    missing or non-numeric score must degrade to a neutral, honest
    "not available", never the literal Python string "None" and never a
    traceback."""
    text = answered_text("dhfr", None)
    assert "score: not available" in text
    assert "None" not in text


def test_the_panel_never_shows_the_literal_word_none_for_a_missing_score():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    panel.on_answer_done("q1", "dhfr", score=None)
    text = panel.get_display_text()
    assert "None" not in text
    assert "score: not available" in text


# ---------------------------------------------------------------------------
# In-flight state: no fake progress bar (spec section 6) -- an indeterminate
# Gtk.Spinner is the honest representation of a call this project cannot
# subdivide into stages.
# ---------------------------------------------------------------------------

def test_in_flight_uses_a_spinner_not_a_progress_bar():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    assert _find_descendant(panel, Gtk.ProgressBar) is None, (
        "nesso1 is a single fast scalar call, not a multi-stage pipeline -- "
        "a progress bar here would imply stages that don't exist")
    spinner = _find_descendant(panel, Gtk.Spinner)
    assert spinner is not None


def test_spinner_is_spinning_while_a_question_is_in_flight():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    spinner = _find_descendant(panel, Gtk.Spinner)
    panel.on_answer_start("q1", "dhfr")
    assert spinner.get_spinning() is True


def test_spinner_stops_once_the_in_flight_question_is_answered():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    spinner = _find_descendant(panel, Gtk.Spinner)
    panel.on_answer_start("q1", "dhfr")
    assert spinner.get_spinning() is True
    panel.on_answer_done("q1", "dhfr", score=0.5)
    assert spinner.get_spinning() is False


def test_spinner_stops_on_an_answer_error_too():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    spinner = _find_descendant(panel, Gtk.Spinner)
    panel.on_answer_start("q1", "dhfr")
    panel.on_answer_error("q1", "dhfr")
    assert spinner.get_spinning() is False


# ---------------------------------------------------------------------------
# on_answer_error: honest, not a fabricated result -- no score, no verdict.
# ---------------------------------------------------------------------------

def test_answer_error_never_shows_a_score():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    panel.on_answer_done("q1", "dhfr", score=0.5)  # a real prior answer
    panel.on_answer_start("q2", "trypsin")
    panel.on_answer_error("q2", "trypsin")
    text = panel.get_display_text().lower()
    assert "score:" not in text.split("\n")[-1], (
        "an errored question must not report a score at all")


def test_error_text_pure_function_names_the_target_honestly():
    text = error_text("trypsin", "Does benzamidine bind trypsin?")
    assert "trypsin" in text
    for word in _FORBIDDEN_VERDICT_WORDS:
        assert word not in text.lower()


# ---------------------------------------------------------------------------
# Pending queue: an honest "not yet timed" (spec section 7), mirroring
# ui.gallery's own handling of an unmeasured Target.expected_s -- never a
# fabricated number for a question nobody has measured yet.
# ---------------------------------------------------------------------------

def test_expected_time_text_of_an_unmeasured_question_says_not_yet_timed():
    assert expected_time_text(None) == "not yet timed"


def test_expected_time_text_of_a_measured_question_shows_the_number():
    assert expected_time_text(12.3) == "~12.3s"


def test_pending_queue_is_populated_from_constructor_questions():
    questions = [
        Question(id="dhfr_mtx", target_id="dhfr",
                 question="Does methotrexate block dihydrofolate reductase?",
                 ligand_name="Methotrexate", expected_s=None),
    ]
    panel = QuestionQueuePanel(questions=questions)
    panel.set_qa_capable(True)
    assert "methotrexate" in panel.get_display_text().lower()
    assert "not yet timed" in panel.get_display_text()


def test_starting_a_pending_question_removes_it_from_the_pending_list():
    questions = [
        Question(id="dhfr_mtx", target_id="dhfr",
                 question="Does methotrexate block dihydrofolate reductase?",
                 ligand_name="Methotrexate", expected_s=None),
    ]
    panel = QuestionQueuePanel(questions=questions)
    panel.set_qa_capable(True)
    panel.on_answer_start("dhfr_mtx", "dhfr")
    assert "No questions queued" in panel.get_display_text()


def test_empty_pending_queue_says_so_explicitly():
    """An empty pending list must read as an explicit "no questions
    queued" -- never a blank label indistinguishable from a rendering
    bug."""
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    assert pending_text([]) == "No questions queued"
    assert "No questions queued" in panel.get_display_text()


# ---------------------------------------------------------------------------
# Important 4 (whole-branch review): "pending" is a live derivation (every
# known question minus whichever is in-flight and minus the most recent
# answer), not a list that only ever drains -- see `_pending_questions`'s
# docstring in ui/questions.py for the full reasoning. Three scenarios the
# original `self._pending = list(questions)` / "on_answer_start removes,
# nothing ever adds" design got wrong.
# ---------------------------------------------------------------------------

def _three_questions():
    return [
        Question(id="dhfr_mtx", target_id="dhfr",
                 question="Does methotrexate block dihydrofolate reductase?",
                 ligand_name="Methotrexate", expected_s=None),
        Question(id="trypsin_bam", target_id="trypsin",
                 question="Does benzamidine block trypsin?",
                 ligand_name="Benzamidine", expected_s=None),
        Question(id="fkbp12_sb3", target_id="fkbp12",
                 question="Does SB3 bind FKBP12?",
                 ligand_name="SB3", expected_s=None),
    ]


def test_a_full_ask_cycle_still_shows_an_ongoing_rotation_not_empty():
    """The bug this project's own history would recognize immediately: a
    list that only ever drains reads "No questions queued" forever once
    every question has been asked once, even though the attract loop keeps
    asking the same three questions on repeat indefinitely. Driving one
    full cycle (start -> done, for all three) must NOT end on an empty
    pending list."""
    panel = QuestionQueuePanel(questions=_three_questions())
    panel.set_qa_capable(True)
    for question in _three_questions():
        panel.on_answer_start(question.id, question.target_id)
        panel.on_answer_done(question.id, question.target_id, score=0.5)
    text = panel.get_display_text()
    assert "No questions queued" not in text, (
        "a full cycle through every known question must not permanently "
        "empty the pending list -- the attract loop keeps asking them "
        "forever")
    # Exactly the two NOT most-recently-answered should read as pending;
    # the third (fkbp12_sb3, the last one done) is the current answer, not
    # "pending" any more.
    assert "methotrexate" in text.lower()
    assert "benzamidine" in text.lower()


def test_a_bumped_and_errored_question_is_not_a_phantom_pending_entry():
    """A question bumped out of the daemon's queue
    (`runner.daemon.MAX_PENDING_QUESTIONS`) goes straight to
    `on_answer_error`, never through `on_answer_start` -- the original
    design's `_pending` never removed it, so it stayed listed as "pending"
    forever even while its own row correctly showed it as errored."""
    panel = QuestionQueuePanel(questions=_three_questions())
    panel.set_qa_capable(True)
    panel.on_answer_error("trypsin_bam", "trypsin")
    text = panel.get_display_text()
    # It must not appear in the pending list (the top label) while it is
    # the current answered/errored result -- checked against the PENDING
    # label specifically, not the whole display text, since the errored
    # row itself legitimately names "trypsin" too.
    assert "benzamidine" not in panel._pending_label.get_label().lower(), (
        "a bumped/errored question must not linger as a phantom pending "
        "entry")
    assert "not answered (error)" in text.lower()


def test_a_superseded_error_returns_to_pending():
    """Once a NEWER answer supersedes a bumped/errored question's slot as
    "the most recent result", that older question is neither in flight nor
    the most recent answer any more -- so it correctly reads as pending
    again, exactly like any other question waiting for its next turn in
    the rotation."""
    panel = QuestionQueuePanel(questions=_three_questions())
    panel.set_qa_capable(True)
    panel.on_answer_error("trypsin_bam", "trypsin")
    panel.on_answer_start("dhfr_mtx", "dhfr")
    panel.on_answer_done("dhfr_mtx", "dhfr", score=0.5)
    assert "benzamidine" in panel._pending_label.get_label().lower()


def test_nothing_has_been_asked_yet_at_startup_shows_the_known_questions():
    """At startup, nothing is in flight and nothing has been answered, so
    every known question reads as pending -- an honest statement of "these
    are the questions this booth asks", not a claim that any of them is
    literally sitting in the daemon's dispatch queue at this instant (which
    the row's own text never asserts -- it names each question and its
    timing, nothing about being enqueued)."""
    panel = QuestionQueuePanel(questions=_three_questions())
    panel.set_qa_capable(True)
    pending = panel._pending_questions()
    assert {q.id for q in pending} == {"dhfr_mtx", "trypsin_bam", "fkbp12_sb3"}


def test_pending_derivation_pure_function():
    """`_pending_questions` directly, so Important 4's fix is pinned
    independently of the widget's rendering (the same split this module's
    docstring establishes for `answered_text`/`pending_text`/etc.)."""
    panel = QuestionQueuePanel(questions=_three_questions())
    panel.on_answer_start("dhfr_mtx", "dhfr")
    assert {q.id for q in panel._pending_questions()} == {
        "trypsin_bam", "fkbp12_sb3"}
    panel.on_answer_done("dhfr_mtx", "dhfr", score=0.9)
    # dhfr_mtx is now "the most recent answer", not in flight -- still
    # excluded, for the same "not the current activity" reason.
    assert {q.id for q in panel._pending_questions()} == {
        "trypsin_bam", "fkbp12_sb3"}


# ---------------------------------------------------------------------------
# Pure-function coverage, independent of the widget -- so a text-formatting
# regression is caught even if the widget-assembly tests above somehow
# missed it.
# ---------------------------------------------------------------------------

def test_in_flight_text_falls_back_to_naming_the_target_honestly():
    """No matching question text (e.g. a bare (question_id, target_id)
    the panel has no playlist/questions.yaml entry loaded for) must not
    fabricate a question -- it names the real target it's checking,
    nothing invented beyond that."""
    text = in_flight_text("dhfr", None)
    assert "dhfr" in text


def test_pending_text_includes_every_question():
    questions = [
        Question(id="a", target_id="dhfr", question="Q1?",
                 ligand_name="L1", expected_s=5.0),
        Question(id="b", target_id="trypsin", question="Q2?",
                 ligand_name="L2", expected_s=None),
    ]
    text = pending_text(questions)
    assert "Q1?" in text
    assert "Q2?" in text
    assert "~5.0s" in text
    assert "not yet timed" in text


# ---------------------------------------------------------------------------
# Legibility, in the same generalized, theme-independent style as
# tests/unit/test_panels.py's own guard for TelemetryPanel/PipelinePanel and
# tests/unit/test_gallery.py's for Gallery -- the shared tests/unit/
# _legibility.py machinery applied to a THIRD stylesheet, not a fourth,
# independently maintained copy of the same logic.
# ---------------------------------------------------------------------------

def test_every_label_carries_a_class_with_an_explicit_color_rule():
    import ui.questions as ui_questions

    color_rules = _legibility.color_rules_from_css(ui_questions._QUESTIONS_CSS)
    questions = [
        Question(id="dhfr_mtx", target_id="dhfr", question="Does it bind?",
                 ligand_name="Methotrexate", expected_s=None),
    ]
    panel = QuestionQueuePanel(questions=questions)
    panel.set_qa_capable(True)
    panel.on_answer_start("dhfr_mtx", "dhfr")
    panel.on_answer_done("dhfr_mtx", "dhfr", score=0.5)

    failures = [
        f"label {label.get_label()!r} carries classes "
        f"{sorted(label.get_css_classes())!r}, none of which has a "
        "matching `color:` rule in the real ui.questions._QUESTIONS_CSS "
        "stylesheet"
        for label in _legibility.iter_labels(panel)
        if not _legibility.label_has_an_explicit_color_rule(label, color_rules)
    ]
    assert not failures, "\n".join(failures)


def test_question_queue_labels_are_legible():
    import ui.questions as ui_questions

    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    panel.on_answer_start("q1", "dhfr")
    panel.on_answer_done("q1", "dhfr", score=0.5)

    _legibility.assert_every_label_is_legible(
        panel, context="question queue",
        min_contrast=MIN_CONTRAST_RATIO,
        contrast_ratio_fn=contrast_ratio,
        css_text_fn=lambda: ui_questions._QUESTIONS_CSS,
        background_by_class_fn=lambda: ui_questions._BACKGROUND_BY_CLASS,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_descendant(widget, widget_type):
    if isinstance(widget, widget_type):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find_descendant(child, widget_type)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None
