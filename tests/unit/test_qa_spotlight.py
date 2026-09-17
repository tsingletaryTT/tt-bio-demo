"""Tests for ui/qa_spotlight.py: `QASpotlightCell`, the quad's own empty
fourth cell put to use for the affinity Q&A state.

Same display assumption as tests/unit/test_questions_panel.py and
tests/unit/test_quad.py: constructing these widgets builds real
Gtk.Label/Gtk.Box/Gtk.Spinner children, so these run against the real
display this box has (DISPLAY=:0, WAYLAND_DISPLAY=wayland-0) rather than
faking one.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

import pytest
from gi.repository import Gtk

import _legibility
from ui.panels import MIN_CONTRAST_RATIO, contrast_ratio
from ui.playlist import Question
from ui.qa_spotlight import QASpotlightCell
from ui.quad import MAX_SLOTS, QuadView, grid_position


def _question(id="q1", target_id="dhfr", question="Does the ligand bind DHFR?",
               ligand_name="Methotrexate", expected_s=None):
    return Question(id=id, target_id=target_id, question=question,
                     ligand_name=ligand_name, expected_s=expected_s)


# ---------------------------------------------------------------------------
# Hidden until proven capable -- the same discipline
# ui.questions.QuestionQueuePanel already follows, applied to this cell.
# ---------------------------------------------------------------------------

def test_a_fresh_cell_is_hidden_before_qa_capable_is_ever_called():
    cell = QASpotlightCell()
    assert cell.get_visible() is False


def test_hides_entirely_when_not_qa_capable():
    cell = QASpotlightCell()
    cell.set_qa_capable(True)
    cell.set_qa_capable(False)
    assert cell.get_visible() is False


def test_shows_when_qa_capable():
    cell = QASpotlightCell()
    cell.set_qa_capable(True)
    assert cell.get_visible() is True


# ---------------------------------------------------------------------------
# The three states, and the content-honesty rule -- same wording this
# module reuses from ui.questions rather than a second hand-typed copy of
# it, so these tests are really checking the WIRING, not re-deriving the
# wording rules test_questions_panel.py already pins.
# ---------------------------------------------------------------------------

def test_in_flight_question_shows_its_text_and_spins():
    cell = QASpotlightCell([_question()])
    cell.set_qa_capable(True)
    cell.on_answer_start("q1", "dhfr")
    assert "dhfr" in cell.get_display_text().lower() or \
        "Does the ligand bind DHFR?" in cell.get_display_text()
    assert cell._spinner.get_visible() is True


def test_answered_question_shows_the_score_not_a_fabricated_verdict():
    cell = QASpotlightCell([_question()])
    cell.set_qa_capable(True)
    cell.on_answer_start("q1", "dhfr")
    cell.on_answer_done("q1", "dhfr", score=0.5)
    text = cell.get_display_text()
    # Exact rounded value, not a substring that would also match "0.50":
    # "score: 0.5" is a substring of "score: 0.50" too, so this checks the
    # real format rather than passing by coincidence.
    assert "0.50" in text
    for word in ("tightly", "weakly", "strongly"):
        assert word not in text.lower()
    # The spinner stops once an answer lands -- a spinning cell next to a
    # settled score would read as "still checking" over an answer that
    # already arrived.
    assert cell._spinner.get_visible() is False


def test_answered_error_shows_not_answered_not_a_stale_score():
    cell = QASpotlightCell([_question()])
    cell.set_qa_capable(True)
    cell.on_answer_done("q1", "dhfr", score=0.9)
    cell.on_answer_error("q1", "dhfr")
    text = cell.get_display_text()
    assert "not answered" in text
    assert "0.9" not in text


def test_a_non_numeric_score_renders_as_not_available_not_the_word_none():
    cell = QASpotlightCell([_question()])
    cell.set_qa_capable(True)
    cell.on_answer_done("q1", "dhfr", score=None)
    text = cell.get_display_text()
    assert "not available" in text
    assert "None" not in text


def test_before_any_answer_the_cell_says_so_honestly():
    cell = QASpotlightCell([_question()])
    cell.set_qa_capable(True)
    assert "No question answered yet" in cell.get_display_text()


# ---------------------------------------------------------------------------
# The pending count -- a number, not the rail panel's full list (this cell
# has room for one small line, not a ledger).
# ---------------------------------------------------------------------------

def test_pending_count_excludes_the_in_flight_and_answered_question():
    questions = [_question(id="q1", target_id="dhfr"),
                 _question(id="q2", target_id="trypsin",
                           question="Does it bind trypsin?")]
    cell = QASpotlightCell(questions)
    cell.set_qa_capable(True)
    assert "2 more queued" in cell.get_display_text()
    cell.on_answer_start("q1", "dhfr")
    assert "1 more queued" in cell.get_display_text()
    cell.on_answer_done("q1", "dhfr", score=0.5)
    assert "1 more queued" in cell.get_display_text()


def test_no_pending_line_when_every_question_is_in_flight_or_answered():
    cell = QASpotlightCell([_question()])
    cell.set_qa_capable(True)
    cell.on_answer_start("q1", "dhfr")
    assert "more queued" not in cell.get_display_text()


# ---------------------------------------------------------------------------
# Legibility, in the same generalized, theme-independent style as
# tests/unit/test_questions_panel.py's own guard for QuestionQueuePanel --
# the shared tests/unit/_legibility.py machinery applied to a FOURTH
# stylesheet, not a fifth independently maintained copy of the same logic.
# ---------------------------------------------------------------------------

def test_every_label_carries_a_class_with_an_explicit_color_rule():
    import ui.qa_spotlight as ui_qa_spotlight

    color_rules = _legibility.color_rules_from_css(
        ui_qa_spotlight._QA_SPOTLIGHT_CSS)
    cell = QASpotlightCell([_question()])
    cell.set_qa_capable(True)
    cell.on_answer_start("q1", "dhfr")
    cell.on_answer_done("q1", "dhfr", score=0.5)

    failures = [
        f"label {label.get_label()!r} carries classes "
        f"{sorted(label.get_css_classes())!r}, none of which has a "
        "matching `color:` rule in the real "
        "ui.qa_spotlight._QA_SPOTLIGHT_CSS stylesheet"
        for label in _legibility.iter_labels(cell)
        if not _legibility.label_has_an_explicit_color_rule(label, color_rules)
    ]
    assert not failures, "\n".join(failures)


def test_qa_spotlight_labels_are_legible():
    import ui.qa_spotlight as ui_qa_spotlight

    cell = QASpotlightCell([_question()])
    cell.set_qa_capable(True)
    cell.on_answer_start("q1", "dhfr")
    cell.on_answer_done("q1", "dhfr", score=0.5)

    _legibility.assert_every_label_is_legible(
        cell, context="qa spotlight",
        min_contrast=MIN_CONTRAST_RATIO,
        contrast_ratio_fn=contrast_ratio,
        css_text_fn=lambda: ui_qa_spotlight._QA_SPOTLIGHT_CSS,
        background_by_class_fn=lambda: ui_qa_spotlight._BACKGROUND_BY_CLASS,
    )


# ---------------------------------------------------------------------------
# QuadView.set_extra_cell -- the quad-side half of "put the empty cell to
# use": placement, the no-free-slot case, solo-mode visibility, and that a
# card-list rebuild (a fresh QuadView, same long-lived widget -- see
# ui/app.py's `_ensure_quad`) reparents cleanly rather than raising because
# the widget already belongs to a different grid.
# ---------------------------------------------------------------------------

def test_extra_cell_lands_in_the_slot_just_past_the_last_real_cell():
    quad = QuadView(cards=[0, 1, 2])
    cell = QASpotlightCell()
    quad.set_extra_cell(cell)
    assert quad._extra_wrapper is not None
    assert quad._extra_wrapper.get_parent() is quad
    assert cell.get_parent() is quad._extra_wrapper
    # attached at grid_position(3) -- the slot a 3-card quad leaves empty.
    column, row = grid_position(3)
    assert quad.get_child_at(column, row) is quad._extra_wrapper


def test_a_full_quad_has_no_free_slot_and_does_not_raise():
    quad = QuadView(cards=[0, 1, 2, 3])
    assert quad.slot_count == MAX_SLOTS
    cell = QASpotlightCell()
    quad.set_extra_cell(cell)  # must not raise
    assert quad._extra_wrapper is None
    assert cell.get_parent() is None


def test_extra_cell_is_hidden_in_solo_mode_and_shown_in_quad_mode():
    quad = QuadView(cards=[0, 1, 2])
    cell = QASpotlightCell()
    quad.set_extra_cell(cell)
    quad.set_solo_mode(True)
    assert quad._extra_wrapper.get_visible() is False
    quad.set_solo_mode(False)
    assert quad._extra_wrapper.get_visible() is True
    quad.set_solo_mode(True)
    assert quad._extra_wrapper.get_visible() is False


def test_solo_mode_toggling_never_touches_the_widgets_own_visibility():
    """The wrapper is what QuadView owns; the widget's OWN `visible`
    property is `set_qa_capable`'s alone to set (ui/app.py's
    `_call_question_panel` fan-out drives it independently of the quad).
    If `_apply_solo` ever called `.set_visible()` on the widget itself
    instead of the wrapper, a booth with Q&A disabled (cell hidden by
    `set_qa_capable(False)`) would flash it visible the moment `Q` was
    pressed -- exactly the "capability implied that isn't there" defect
    this cell's whole hidden-until-capable design exists to prevent.
    """
    quad = QuadView(cards=[0, 1, 2])
    cell = QASpotlightCell()
    cell.set_qa_capable(False)
    quad.set_extra_cell(cell)
    for solo in (False, True, False):
        quad.set_solo_mode(solo)
        assert cell.get_visible() is False


def test_extra_cell_survives_a_card_list_rebuild_into_a_new_quadview():
    cell = QASpotlightCell([_question()])
    cell.set_qa_capable(True)
    cell.on_answer_done("q1", "dhfr", score=0.5)

    quad_a = QuadView(cards=[0, 1, 2])
    quad_a.set_extra_cell(cell)
    assert cell.get_parent() is quad_a._extra_wrapper

    # A fresh QuadView, the same one long-lived widget -- ui/app.py's
    # `_ensure_quad` never constructs a second QASpotlightCell.
    quad_b = QuadView(cards=[0, 1])
    quad_b.set_extra_cell(cell)  # must not raise despite the old parent
    assert cell.get_parent() is quad_b._extra_wrapper
    assert cell.get_parent() is not quad_a._extra_wrapper
    # Its remembered answer is untouched by the move.
    assert "0.50" in cell.get_display_text()
