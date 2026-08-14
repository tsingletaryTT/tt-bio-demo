"""The booth demonstrating its own panels when nobody is there.

Every test drives `Choreography` directly with a fake clock. The three rules
in the module docstring are the three things that can actually hurt -- a
choreography that fights a visitor, one that leaves the booth changed, and one
that closes a panel somebody is reading -- so each has tests of its own rather
than being asserted in passing.
"""

import pytest

from ui.attract import (CLOSE_DIAGNOSTICS, CLOSE_TENSIX, CYCLE_S,
                        OPEN_DIAGNOSTICS, OPEN_TENSIX, SCORE,
                        START_AFTER_IDLE_S, Choreography)


def _run(ch, start=0.0, until=200.0, step=1.0, idle_from=0.0):
    """Tick from `start` to `until`, treating the booth as idle since
    `idle_from`. Returns [(t, action), ...]."""
    out, t = [], start
    while t <= until:
        for a in ch.tick(t, idle_s=t - idle_from):
            out.append((t, a))
        t += step
    return out


# ── rule 1: it never starts while somebody is there ─────────────────────────

def test_nothing_happens_before_the_booth_has_been_left_alone():
    ch = Choreography()
    fired = _run(ch, until=START_AFTER_IDLE_S - 1.0)
    assert fired == [], f"choreography moved while a visitor was present: {fired}"
    assert not ch.running


def test_it_starts_only_after_the_idle_threshold():
    ch = Choreography()
    fired = _run(ch, until=START_AFTER_IDLE_S + 1.0)
    assert fired, "choreography never started"
    assert fired[0][0] >= START_AFTER_IDLE_S
    assert fired[0][1] == OPEN_DIAGNOSTICS


def test_the_threshold_is_longer_than_the_booths_own_idle_timeout():
    """The state machine returns to attract after 45s. Starting before that
    would mean the panels move while the booth is still showing a visitor
    the screen they were on."""
    assert START_AFTER_IDLE_S > 45.0


# ── the score itself ────────────────────────────────────────────────────────

def test_it_opens_and_then_closes_both_panels():
    panels = {OPEN_DIAGNOSTICS, CLOSE_DIAGNOSTICS, OPEN_TENSIX, CLOSE_TENSIX}
    ch = Choreography()
    seq = [a for _, a in _run(ch, until=START_AFTER_IDLE_S + CYCLE_S - 1)
           if a in panels]
    assert seq == [OPEN_DIAGNOSTICS, CLOSE_DIAGNOSTICS, OPEN_TENSIX, CLOSE_TENSIX], seq


def test_every_open_in_the_score_has_a_close_before_the_cycle_ends():
    """Rule 2, checked against the score rather than against one run: an open
    with no close would leave a panel up for the rest of the day."""
    opens = {OPEN_DIAGNOSTICS: CLOSE_DIAGNOSTICS, OPEN_TENSIX: CLOSE_TENSIX}
    pending = {}
    for offset, action in sorted(SCORE):
        if action in opens:
            pending[opens[action]] = offset
        elif action in pending:
            del pending[action]
    assert pending == {}, f"opened without closing: {list(pending)}"
    assert max(o for o, _ in SCORE) < CYCLE_S, "a cue lands after the cycle ends"


def test_the_booth_spends_most_of_its_idle_time_showing_the_protein():
    """A booth flickering panels is one nobody can photograph. Panels should
    be up for well under half the cycle."""
    open_at, shown = {}, 0.0
    for offset, action in sorted(SCORE):
        if action == OPEN_DIAGNOSTICS: open_at["d"] = offset
        elif action == CLOSE_DIAGNOSTICS: shown += offset - open_at.pop("d")
        elif action == OPEN_TENSIX: open_at["t"] = offset
        elif action == CLOSE_TENSIX: shown += offset - open_at.pop("t")
    assert shown / CYCLE_S < 0.5, f"panels are up {100*shown/CYCLE_S:.0f}% of the cycle"


def test_it_repeats():
    ch = Choreography()
    seq = [a for _, a in _run(ch, until=START_AFTER_IDLE_S + 2 * CYCLE_S + 1)]
    assert seq.count(OPEN_DIAGNOSTICS) >= 2, seq


def test_a_late_tick_still_fires_the_cues_it_skipped_over():
    """A stalled frame must not swallow a close -- that is exactly how a panel
    gets stuck open for the rest of the day."""
    ch = Choreography()
    assert ch.tick(START_AFTER_IDLE_S, idle_s=START_AFTER_IDLE_S) == [OPEN_DIAGNOSTICS]
    # One tick, arriving 40s late, straddling both the close and the next open.
    late = ch.tick(START_AFTER_IDLE_S + 40, idle_s=START_AFTER_IDLE_S + 40)
    assert CLOSE_DIAGNOSTICS in late, late
    assert OPEN_TENSIX in late, late


# ── rule 2: it never leaves the booth changed ───────────────────────────────

def test_an_interruption_closes_whatever_it_had_opened():
    ch = Choreography()
    ch.tick(START_AFTER_IDLE_S, idle_s=START_AFTER_IDLE_S)      # opens diagnostics
    assert ch.owns("diagnostics")
    assert ch.interrupted() == [CLOSE_DIAGNOSTICS]
    assert not ch.running
    assert ch.owned == frozenset()


def test_an_interruption_with_nothing_open_closes_nothing():
    ch = Choreography()
    assert ch.interrupted() == []


def test_interrupting_twice_does_not_close_twice():
    ch = Choreography()
    ch.tick(START_AFTER_IDLE_S, idle_s=START_AFTER_IDLE_S)
    assert ch.interrupted() == [CLOSE_DIAGNOSTICS]
    assert ch.interrupted() == [], "a second interruption re-emitted a close"


def test_it_restarts_cleanly_after_an_interruption():
    ch = Choreography()
    ch.tick(START_AFTER_IDLE_S, idle_s=START_AFTER_IDLE_S)
    ch.interrupted()
    # The visitor leaves; the idle clock restarts from 100.
    fired = _run(ch, start=100.0, until=100.0 + START_AFTER_IDLE_S + 1, idle_from=100.0)
    assert [a for _, a in fired][:1] == [OPEN_DIAGNOSTICS]


# ── rule 3: it never takes a panel away from a visitor ──────────────────────

def test_it_will_not_close_a_panel_a_visitor_opened():
    """THE ONE THAT WOULD FEEL BROKEN. If a visitor opens the tap themselves,
    the choreography's pending close must not fire -- a panel shutting while
    you are reading it is worse than never having opened."""
    ch = Choreography()
    ch.tick(START_AFTER_IDLE_S, idle_s=START_AFTER_IDLE_S)      # we opened it
    ch.disown("diagnostics")                                     # visitor took it
    later = ch.tick(START_AFTER_IDLE_S + 20, idle_s=START_AFTER_IDLE_S + 20)
    assert CLOSE_DIAGNOSTICS not in later, later


def test_a_disowned_panel_is_not_closed_on_interruption_either():
    ch = Choreography()
    ch.tick(START_AFTER_IDLE_S, idle_s=START_AFTER_IDLE_S)
    ch.disown("diagnostics")
    assert ch.interrupted() == []


def test_ownership_is_reported_honestly():
    ch = Choreography()
    assert not ch.owns("diagnostics")
    ch.tick(START_AFTER_IDLE_S, idle_s=START_AFTER_IDLE_S)
    assert ch.owns("diagnostics")
    assert not ch.owns("tensix")


# ── clocks behaving badly ───────────────────────────────────────────────────

def test_a_clock_that_steps_backwards_does_not_dump_the_whole_score():
    ch = Choreography()
    ch.tick(START_AFTER_IDLE_S, idle_s=START_AFTER_IDLE_S)
    out = ch.tick(START_AFTER_IDLE_S - 30, idle_s=START_AFTER_IDLE_S)
    assert out == [], f"a backwards clock emitted {out}"


def test_ticking_costs_nothing_when_the_booth_is_busy():
    """The common case by far: this runs on the booth's ordinary tick."""
    ch = Choreography()
    for t in range(0, 40):
        assert ch.tick(float(t), idle_s=0.0) == []


# ── the menu ────────────────────────────────────────────────────────────────

def test_the_gallery_is_shown_and_then_put_away():
    from ui.attract import HIDE_GALLERY, SHOW_GALLERY
    ch = Choreography()
    seq = [a for _, a in _run(ch, until=START_AFTER_IDLE_S + CYCLE_S - 1)]
    assert SHOW_GALLERY in seq, seq
    assert seq.index(HIDE_GALLERY) > seq.index(SHOW_GALLERY), seq


def test_the_gallery_is_the_briefest_cue():
    """It is the only one that REPLACES the protein rather than sitting
    beside it, and the protein is what people stop for."""
    from ui.attract import (CLOSE_DIAGNOSTICS, HIDE_GALLERY, OPEN_DIAGNOSTICS,
                            SHOW_GALLERY)
    at = dict((a, o) for o, a in SCORE)
    gallery = at[HIDE_GALLERY] - at[SHOW_GALLERY]
    panel = at[CLOSE_DIAGNOSTICS] - at[OPEN_DIAGNOSTICS]
    assert gallery < panel, f"gallery {gallery}s is not briefer than {panel}s"


def test_an_interruption_puts_the_gallery_away_too():
    from ui.attract import HIDE_GALLERY
    ch = Choreography()
    # Run the cycle rather than jumping to the cue: the first tick after the
    # idle threshold STARTS the cycle, so a single late tick would only fire
    # the cue at offset zero.
    _run(ch, until=START_AFTER_IDLE_S + 70)
    assert ch.owns("gallery")
    assert HIDE_GALLERY in ch.interrupted()
