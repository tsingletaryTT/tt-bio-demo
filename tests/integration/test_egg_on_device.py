"""The easter egg's descent, on real silicon, against the host as the oracle.

Hardware-gated like everything in this directory. Run with:
    ./scripts/test.sh --hw     (or)
    .venvs/venv-runner/bin/python3 -m pytest tests/integration -v -k egg

What makes these tests worth having rather than decorative: `runner/egg.py`
reimplements `mark.py` in ttnn, in float32, with a tolerance-based nearest-edge
mask instead of an argmin -- three chances for the chip to compute something
that looks vaguely mark-shaped and is not the same descent. Both are driven
from the SAME `RunParameters` here, so "the chip computed the mark" is a
comparison against an independent implementation rather than against itself.

This module opens a device and closes it in a `finally`. Nothing in this repo
may leave a chip held.
"""

import numpy as np
import pytest

import mark
from protocol.events import unpack_coords
from runner.egg import DeviceCondensation, run_egg

# The booth's own point count. Not reduced "to keep the test quick": the
# rasterised-IoU floor these tests lean on is a function of how densely the
# cloud fills the artwork's 465 ink cells, and at 3000 points a perfectly
# correct settled cloud scores 0.57 -- under the floor, for no reason but
# sparseness. A whole run is ~0.3 s on the chip, so there is nothing to save.
COUNT = mark.POINTS
SEED = 20260813


@pytest.fixture(scope="module")
def device(tt_device):
    from tt_bio.tenstorrent import cleanup, get_device
    handle = get_device()
    try:
        yield handle
    finally:
        cleanup()


@pytest.fixture(scope="module")
def both(device):
    """One set of run parameters, descended twice: on the chip and on the host."""
    params = mark.run_parameters(seed=SEED, count=COUNT)
    on_chip = DeviceCondensation(device, params)
    while not on_chip.done:
        on_chip.step()
    on_host = mark.MarkCondensation(params=params)
    while not on_host.done:
        on_host.step()
    return (on_chip.points() / params.scale, on_host._points, params)


def test_the_chip_and_the_host_agree_on_where_the_cloud_lands(both):
    """Not point for point -- a handful of points sit on a watershed between
    two faces and float32 sends them one way where float64 sends them the
    other, which is a property of the geometry and not a bug. What must agree
    is the CLOUD: how far it settled, and how much of it is inside.
    """
    chip, host, _params = both
    separation = np.linalg.norm(chip - host, axis=1)
    assert np.median(separation) < 0.01
    assert np.percentile(separation, 99) < 0.05

    chip_d, _ = mark.slab_sdf_gradient(chip, mark.HALF_THICKNESS)
    host_d, _ = mark.slab_sdf_gradient(host, mark.HALF_THICKNESS)
    assert chip_d.max() == pytest.approx(host_d.max(), abs=0.005)
    assert (chip_d <= 1e-3).mean() == pytest.approx((host_d <= 1e-3).mean(),
                                                    abs=0.01)


def test_the_cloud_the_chip_produced_is_the_tenstorrent_mark(both):
    """The independent oracle: the shipped 32x32 artwork, the same one
    `tests/unit/test_mark.py` rasterises the field against.

    Measured: a settled cloud scores 0.73-0.81 and the same cloud before it
    has descended scores 0.38-0.42, so the floor sits in the gap -- and the
    second assertion here proves it by requiring the STARTING cloud to fail
    it. A threshold nothing can fail is not a threshold.
    """
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve()
                           .parents[1] / "unit"))
    from test_mark import MARK_IOU_FLOOR, _looks_like_the_mark

    chip, _host, params = both
    assert _looks_like_the_mark(chip) >= MARK_IOU_FLOOR
    assert _looks_like_the_mark(params.positions) < MARK_IOU_FLOOR


def test_the_chip_settles_the_cloud_inside_the_mark(both):
    chip, _host, _params = both
    distance, _ = mark.slab_sdf_gradient(chip, mark.HALF_THICKNESS)
    assert (distance <= 1e-3).mean() > 0.98
    assert distance.max() < 0.05


def test_the_descent_is_a_settling_motion_and_never_reverses(device):
    """The `relu` on the device, checked the way it is checked on the host:
    the cloud's mean distance outside the mark must fall monotonically. A
    sign error anywhere in the field would show up here as a step that pushes
    points back out, and nowhere else -- the final cloud would just be wrong
    in a way that still looks like a blob.
    """
    params = mark.run_parameters(seed=7, count=512)
    run = DeviceCondensation(device, params)
    outside = []
    while not run.done:
        run.step()
        points = run.points() / params.scale
        outside.append(float(np.maximum(mark.mark_sdf(points[:, :2]), 0.0).mean()))
    quarter = len(outside) // 4
    assert outside[quarter] > 0.02, "a quarter in, the cloud should still be moving"
    assert outside[-1] < 5e-3
    # The swirl is a rigid rotation, so it can nudge this up by a hair while
    # it is still running; the tolerance covers that and nothing larger.
    assert all(b <= a + 0.01 for a, b in zip(outside, outside[1:]))


def test_two_runs_on_the_chip_take_different_paths_to_the_same_mark(device):
    """The whole user-facing point, measured on the hardware that produces
    it: same destination, different journey, no seed supplied."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve()
                           .parents[1] / "unit"))
    from test_mark import MARK_IOU_FLOOR, _looks_like_the_mark

    midpoints, finals, seeds = [], [], []
    for _ in range(2):
        params = mark.run_parameters(count=4000)
        seeds.append(params.seed)
        run = DeviceCondensation(device, params)
        while not run.done:
            run.step()
            if run.completed == params.steps // 2:
                midpoints.append(run.points() / params.scale)
        finals.append(run.points() / params.scale)

    assert seeds[0] != seeds[1], "a fresh run must draw a fresh seed"
    assert np.linalg.norm(midpoints[0] - midpoints[1], axis=1).mean() > 0.5
    for final in finals:
        assert _looks_like_the_mark(final) >= MARK_IOU_FLOOR


def test_a_whole_run_streams_frames_a_ui_could_render(device):
    """`run_egg` end to end: the events the socket carries, decoded the way
    ui/app.py decodes them."""
    got = []
    seed = run_egg(device, got.append, egg_id="hw-test", card=0,
                   count=1024, steps=24, gap_s=0.0)
    assert [e["step"] for e in got] == list(range(25))
    assert {e["seed"] for e in got} == {seed}
    first = unpack_coords(got[0]["coords_b64"])
    last = unpack_coords(got[-1]["coords_b64"])
    assert first.shape == last.shape == (1024, 3)
    # Scaled for the viewer on the DEVICE side, so the UI renders what
    # arrives rather than having to know the mark's units.
    assert np.abs(last).max() > 5.0
    assert np.linalg.norm(first - last, axis=1).mean() > 1.0
