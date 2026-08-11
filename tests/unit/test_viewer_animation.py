"""Pins the `_blend_target` bug/fix and the tick's dt-handling permanently.

These exercise the REAL, unmodified `StructureViewer.begin_crossfade`,
`clear_structure`, `set_blend`, and `_on_tick` methods -- not
reimplementations of their logic -- so a future edit that reintroduces the
bug (or removes the dt clamp/spin wrap) fails these tests, not just a
throwaway verification script that no longer exists.

None of these methods touch anything beyond plain attributes,
`self.queue_render()`, and (for `_on_tick`) `frame_clock.get_frame_time()`.
Constructing a real `StructureViewer` requires a live GTK display/GL
context (this project's GL behavior is verified separately, by
glReadPixels-after-render harnesses against a live display -- see the
task reports; that's deliberately not what this file does). So instead of
`StructureViewer()`, a tiny duck-typed stand-in binds the real methods to a
lightweight object that only has the handful of attributes those methods
actually read or write, plus a no-op `queue_render`. This runs anywhere
`ui.viewer` can be imported (already established as headless-safe --
`test_blend.py`'s own note) with no display, no GL, no GTK widget
construction at all.
"""
import math

import pytest

from ui.viewer import StructureViewer, blend_step


class _FakeFrameClock:
    """Just enough of Gdk.FrameClock for `_on_tick`: a controllable
    monotonic timestamp in microseconds, matching `get_frame_time()`'s
    real contract."""

    def __init__(self, time_us):
        self.time_us = time_us

    def get_frame_time(self):
        return self.time_us


class _FakeViewer:
    """Duck-typed stand-in for StructureViewer -- see module docstring."""

    CROSSFADE_SECONDS = StructureViewer.CROSSFADE_SECONDS
    SPIN_RATE = StructureViewer.SPIN_RATE

    # Bind the real, unmodified methods under test. These are plain
    # functions (Python has no separate "unbound method" type), so
    # attaching them to this unrelated class makes them real instance
    # methods here too, running their actual production bodies against
    # whatever `self` they're called on.
    begin_crossfade = StructureViewer.begin_crossfade
    clear_structure = StructureViewer.clear_structure
    set_blend = StructureViewer.set_blend
    _on_tick = StructureViewer._on_tick

    def __init__(self):
        self._blend = 0.0
        self._blend_target = 0.0
        self._spin = 0.0
        self._last_frame_time = None
        self._point_count = 0
        self._ribbon_index_count = 0
        self._pending_points = None
        self._pending_ribbon = None
        self._camera_framed = True
        self.render_calls = 0

    def queue_render(self):
        self.render_calls += 1


# ── the _blend_target bug/fix ───────────────────────────────────────────


def test_clear_structure_resets_blend_target_not_just_blend():
    """Direct field-level pin: clear_structure() must not leave a fold's
    completed blend_target (1.0) behind for the next fold to inherit."""
    viewer = _FakeViewer()
    viewer.begin_crossfade()  # fold 1 finishing: target -> 1.0
    viewer._blend = 1.0  # ... and blend eased all the way there

    viewer.clear_structure()  # fold 2 starts

    assert viewer._blend == 0.0
    assert viewer._blend_target == 0.0


def test_clear_structure_prevents_next_tick_from_dragging_blend_upward():
    """Behavioral pin, one level up from the field check above: with a
    stale target left behind (the bug, reproduced here by NOT calling the
    fixed clear_structure -- see the inverse assertion below), the very
    next tick would immediately start chasing the discarded ribbon. With
    the shipped fix, a tick right after clear_structure() must leave
    _blend at 0.0, not creeping upward.
    """
    viewer = _FakeViewer()
    viewer.begin_crossfade()
    viewer._blend = 1.0
    viewer.clear_structure()

    next_blend = blend_step(
        viewer._blend, viewer._blend_target, dt=0.1, duration=viewer.CROSSFADE_SECONDS)

    assert next_blend == 0.0


def test_the_bug_this_pins_would_actually_creep_upward():
    """Sanity-checks the previous test's premise by reproducing the bug
    directly against the *unfixed* field values (a stale target of 1.0,
    as the brief's literal clear_structure would have left behind) --
    confirms the assertion above is actually discriminating, not vacuous.
    """
    blend_with_stale_target = 0.0
    stale_target = 1.0  # what _blend_target would still be, pre-fix

    crept_blend = blend_step(
        blend_with_stale_target, stale_target, dt=0.1, duration=StructureViewer.CROSSFADE_SECONDS)

    assert crept_blend > 0.0, (
        "expected the pre-fix scenario to creep upward -- if this fails, "
        "the two tests above may no longer be exercising anything")


def test_set_blend_also_pins_the_target_against_the_same_bug():
    """set_blend() jumps _blend immediately; it must pin _blend_target to
    the same value too; otherwise a stray earlier begin_crossfade() call
    could make the very next tick immediately undo the jump."""
    viewer = _FakeViewer()
    viewer.begin_crossfade()  # target left at 1.0
    viewer.set_blend(0.0)  # e.g. some future caller forcing points-only

    assert viewer._blend == 0.0
    assert viewer._blend_target == 0.0

    next_blend = blend_step(
        viewer._blend, viewer._blend_target, dt=0.1, duration=viewer.CROSSFADE_SECONDS)
    assert next_blend == 0.0


# ── _on_tick's dt handling (judgment point 1) ───────────────────────────


def test_on_tick_first_call_establishes_baseline_and_advances_nothing():
    viewer = _FakeViewer()
    viewer.begin_crossfade()
    clock = _FakeFrameClock(time_us=5_000_000)

    viewer._on_tick(None, clock)

    assert viewer._last_frame_time == pytest.approx(5.0)
    assert viewer._blend == 0.0
    assert viewer._spin == 0.0


def test_on_tick_advances_blend_and_spin_by_a_normal_frame_interval():
    viewer = _FakeViewer()
    viewer.begin_crossfade()
    viewer._on_tick(None, _FakeFrameClock(time_us=0))
    viewer._on_tick(None, _FakeFrameClock(time_us=16_667))  # ~one 60Hz frame

    assert viewer._blend == pytest.approx(16_667e-6 / viewer.CROSSFADE_SECONDS)
    assert viewer._spin == pytest.approx(viewer.SPIN_RATE * 16_667e-6)


def test_on_tick_clamps_a_large_time_jump_instead_of_a_giant_spin_jump():
    """Pins the suspend/backgrounding guard: a huge gap between ticks must
    not spin the model through a huge angle in a single frame."""
    viewer = _FakeViewer()
    viewer._on_tick(None, _FakeFrameClock(time_us=0))  # baseline

    # A 10-minute gap -- system suspend, or the window backgrounded.
    viewer._on_tick(None, _FakeFrameClock(time_us=10 * 60 * 1_000_000))

    # Without a clamp this would be SPIN_RATE * 600 =~ 210 radians.
    # With the shipped clamp (_MAX_TICK_DT = 0.5s) it's bounded tightly.
    assert viewer._spin <= viewer.SPIN_RATE * 0.5 + 1e-9
    assert viewer._spin == pytest.approx(viewer.SPIN_RATE * 0.5)


def test_on_tick_wraps_spin_into_0_2pi_without_changing_the_math():
    """Pins the unbounded-growth guard: spin should never exceed 2*pi,
    wrapping around rather than accumulating forever."""
    viewer = _FakeViewer()
    start_spin = 2.0 * math.pi - 0.05
    viewer._spin = start_spin
    viewer._on_tick(None, _FakeFrameClock(time_us=0))  # baseline, no advance

    # Pick an increment comfortably under the dt clamp's max per-tick
    # increment (SPIN_RATE * _MAX_TICK_DT == 0.175) so this test is only
    # exercising the wrap, not also tripping the separate dt-clamp test
    # above.
    increment = 0.1
    dt = increment / viewer.SPIN_RATE
    dt_us = int(dt * 1e6)
    viewer._on_tick(None, _FakeFrameClock(time_us=dt_us))

    # Compare against the exact same dt actually used (post int-truncation
    # of dt_us) rather than a hand-rounded literal, so this isn't sensitive
    # to unrelated floating-point rounding.
    actual_dt = dt_us / 1e6
    expected = (start_spin + viewer.SPIN_RATE * actual_dt) % (2.0 * math.pi)

    assert 0.0 <= viewer._spin < 2.0 * math.pi
    assert viewer._spin == pytest.approx(expected)
    # And confirm it actually wrapped -- i.e. the new value is smaller than
    # where it started, because it crossed back through zero, not just
    # increased toward (but staying under) 2*pi.
    assert viewer._spin < start_spin
