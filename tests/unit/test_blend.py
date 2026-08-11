import pytest

from ui.viewer import blend_step


def test_blend_advances_toward_target():
    assert blend_step(0.0, 1.0, dt=0.25, duration=1.0) == pytest.approx(0.25)


def test_blend_reaches_target_exactly():
    assert blend_step(0.9, 1.0, dt=1.0, duration=1.0) == pytest.approx(1.0)


def test_blend_never_overshoots():
    assert blend_step(0.95, 1.0, dt=10.0, duration=1.0) == pytest.approx(1.0)


def test_blend_can_run_backwards():
    assert blend_step(1.0, 0.0, dt=0.5, duration=1.0) == pytest.approx(0.5)


def test_blend_backwards_clamps_at_zero():
    assert blend_step(0.1, 0.0, dt=5.0, duration=1.0) == pytest.approx(0.0)


def test_blend_holds_when_already_at_target():
    assert blend_step(1.0, 1.0, dt=0.5, duration=1.0) == pytest.approx(1.0)


def test_zero_duration_snaps_immediately():
    assert blend_step(0.0, 1.0, dt=0.001, duration=0.0) == pytest.approx(1.0)
