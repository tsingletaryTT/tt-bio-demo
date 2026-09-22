"""Tests for ui/questioning.py -- the adaptive question selector.

Pure decision logic: no GTK, no I/O. See the module docstring in
ui/questioning.py for the selection policy under test (relevance, freshness,
fallback, no-immediate-repeat, deterministic tie-break).
"""

from ui.playlist import Question
from ui.questioning import select_question


def _q(qid, target_id="t1"):
    """Build a Question with just the fields the selector cares about."""
    return Question(
        id=qid,
        target_id=target_id,
        question=f"Does the ligand bind {qid}?",
        ligand_name=f"ligand-{qid}",
    )


# --- empty / degenerate pools ---------------------------------------------

def test_empty_pool_returns_none():
    assert select_question([]) is None


def test_a_single_question_is_returned_even_if_just_asked():
    # No alternative exists, so the no-immediate-repeat rule cannot apply.
    q = _q("q1")
    assert select_question([q], recently_asked=["q1"]) is q


# --- relevance: prefer the on-screen target -------------------------------

def test_prefers_questions_about_the_on_screen_target():
    a = _q("a", target_id="target-a")
    b = _q("b", target_id="target-b")
    # 'a' has the lower id and would win a pure tie-break, so this proves the
    # on-screen target -- not the id -- is what decided it.
    assert select_question([a, b], on_screen_target_id="target-b") is b


def test_relevance_overrides_freshness():
    # 'a' is on screen but was just asked; 'b' is fresher (never asked).
    # Relevance must win over freshness: we ask about what is on screen.
    a = _q("a", target_id="target-a")
    b = _q("b", target_id="target-b")
    got = select_question(
        [a, b], on_screen_target_id="target-a", recently_asked=["a"]
    )
    assert got is a


# --- fallback to the whole pool -------------------------------------------

def test_falls_back_to_whole_pool_when_nothing_matches_on_screen():
    a = _q("a", target_id="target-a")
    b = _q("b", target_id="target-b")
    # No question about the on-screen target: fall back to the whole pool and
    # break the resulting tie by id ('a' < 'b').
    got = select_question([a, b], on_screen_target_id="target-unknown")
    assert got is a


def test_no_on_screen_target_uses_the_whole_pool():
    a = _q("a", target_id="target-a")
    b = _q("b", target_id="target-b")
    assert select_question([a, b]) is a  # tie broken by id


# --- freshness: least-recently-asked wins ---------------------------------

def test_prefers_the_least_recently_asked():
    a, b = _q("a"), _q("b")
    # 'a' was asked most recently, 'b' is older -> prefer 'b'.
    assert select_question([a, b], recently_asked=["a", "b"]) is b


def test_a_never_asked_question_beats_a_recently_asked_one():
    a, b = _q("a"), _q("b")
    assert select_question([a, b], recently_asked=["a"]) is b


def test_the_oldest_in_the_window_is_preferred():
    a, b, c = _q("a"), _q("b"), _q("c")
    # Most-recent first: a, then b, then c -- so 'c' is the stalest.
    assert select_question([a, b, c], recently_asked=["a", "b", "c"]) is c


# --- no immediate repeat --------------------------------------------------

def test_does_not_repeat_the_immediately_previous_when_an_alternative_exists():
    a, b = _q("a"), _q("b")
    assert select_question([a, b], recently_asked=["a"]) is b


def test_repeats_only_when_there_is_no_alternative():
    a = _q("a")
    assert select_question([a], recently_asked=["a"]) is a


# --- determinism ----------------------------------------------------------

def test_ties_break_by_id():
    # Neither has been asked, so both are equally fresh: lowest id wins.
    z, m = _q("z"), _q("m")
    assert select_question([z, m]) is m
