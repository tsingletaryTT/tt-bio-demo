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
    # Assert the rendered text contains the raw score and does NOT contain
    # any of the forbidden invented-verdict words -- this is the content-
    # honesty rule from spec section 7, enforced as a test, not just prose.
    assert "score: 0.5" in panel.get_display_text()
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


@pytest.mark.parametrize("score", [0.0, 0.5, -3.2, 12.75])
def test_no_answered_score_ever_grows_a_fabricated_verdict(score):
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    panel.on_answer_done("q1", "dhfr", score=score)
    text = panel.get_display_text().lower()
    assert f"score: {score}" in panel.get_display_text()
    for word in _FORBIDDEN_VERDICT_WORDS:
        assert word not in text, f"fabricated verdict word {word!r} in {text!r}"


def test_answered_text_pure_function_contains_the_raw_score_only():
    """The pure function directly, so the content-honesty rule is pinned
    independently of the widget's own assembly -- the same split
    ui.panels's module docstring describes ("keep the drawing thin and the
    decisions pure")."""
    text = answered_text("dhfr", 0.5, "Does methotrexate bind DHFR?")
    assert "score: 0.5" in text
    assert text.count("score:") == 1
    for word in _FORBIDDEN_VERDICT_WORDS:
        assert word not in text.lower()


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
