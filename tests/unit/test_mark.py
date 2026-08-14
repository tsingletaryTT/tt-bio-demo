"""The easter egg's geometry (mark.py): the field, and the descent onto it.

What these tests are FOR, given the shape is symmetric enough to hide almost
anything:

The mark is nearly, but not exactly, three-fold symmetric, and it is very
nearly mirror-symmetric about the vertical axis. A test that only asks "is
this point inside" against a handful of convenient probes would pass with the
field rotated 120 degrees, mirrored, or (worst) with x and y transposed --
which is precisely the class of bug this project has already lost four
defects to (see docs/followups.md, "Write tests that can fail"). So:

- the field is compared, as a rasterisation, against the REAL 32x32 artwork
  (`LOGO_MASK` below, transcribed from
  `tt-local-generator/app/assets/tenstorrent.png`). That oracle is
  independent of the implementation, and it is asymmetric enough to catch a
  transpose, a mirror, a rotation and an inversion at once.
- every orientation probe below names a point whose SWAPPED and MIRRORED
  partners land on the other side of the boundary, and asserts on both.
- the sign convention is checked at a point that is inside on one axis and
  outside on the other, so "negative inside" cannot be satisfied by a field
  that has simply collapsed to a constant.
"""

import numpy as np
import pytest

import mark

# The shipped 32x32 artwork's alpha mask, at the threshold `> 128`. `#` is
# ink. Transcribed once, verified against the PNG by the test below it, and
# then used as the oracle everything else here leans on.
LOGO_MASK = (
    ".......##..............#........",
    ".....#####.............###......",
    "...#########...........#####....",
    "..############.........######...",
    "###############........########.",
    "#################......########.",
    "###################....########.",
    "####################...########.",
    "######################.########.",
    "###############################.",
    "########..###########..########.",
    "########....#######....########.",
    "########.....#####.....########.",
    "########.......#.......########.",
    ".#######...............#######..",
    "...#####...............#####....",
    ".....###...............###......",
    "......##...............##.......",
    "................................",
    "........##...........##.........",
    "........####........###.........",
    "........#####.....#####.........",
    "........#######.#######.........",
    "........###############.........",
    "........###############.........",
    "........###############.........",
    "........###############.........",
    "........###############.........",
    "..........###########...........",
    "...........#########............",
    ".............#####..............",
    "...............#................",
)

# Where the artwork's own centre and scale sit in that 32x32 grid. Found by a
# scan over both (best IoU), then written down: the icon is fitted to its
# square, so this is not simply 16/16.
_MASK_CENTRE = (15.0, 15.5)
_MASK_SCALE = 15.75


def _mask_array():
    return np.array([[c == "#" for c in row] for row in LOGO_MASK])


def _rasterise():
    """The field, sampled on the artwork's own 32x32 grid."""
    size = len(LOGO_MASK)
    cx, cy = _MASK_CENTRE
    xs = (np.arange(size) - cx) / _MASK_SCALE
    ys = -(np.arange(size) - cy) / _MASK_SCALE       # image y down, mark y up
    grid_x, grid_y = np.meshgrid(xs, ys)
    field = mark.mark_sdf(
        np.stack([grid_x.ravel(), grid_y.ravel()], axis=1)).reshape(size, size)
    return field <= 0.0


def _from_mask(col, row):
    """An artwork pixel's centre, in mark coordinates."""
    return ((col - _MASK_CENTRE[0]) / _MASK_SCALE,
            -(row - _MASK_CENTRE[1]) / _MASK_SCALE)


# ── the field is the mark ───────────────────────────────────────────────────


def test_the_field_rasterises_to_the_shipped_artwork():
    """The claim this whole module makes -- "this is the Tenstorrent mark" --
    measured against the real logo rather than asserted.

    A "three congruent rhombi at 120 degrees" model, which looks right and is
    wrong, scores 0.60 here. The threshold is set well above that and just
    below what the true geometry achieves, so it discriminates between the
    two rather than merely being satisfied by something mark-shaped.
    """
    mine, reference = _rasterise(), _mask_array()
    intersection = np.logical_and(mine, reference).sum()
    union = np.logical_or(mine, reference).sum()
    assert union > 0
    iou = intersection / union
    assert iou > 0.97, f"the field does not match the shipped mark (IoU {iou:.3f})"


def test_the_transcribed_mask_matches_the_shipped_png_if_it_is_present():
    """The oracle above is a transcription, and a transcription can rot. If
    the artwork is on this machine, check it; if it is not (a build box, a
    fresh clone), skip rather than fail -- the mask is committed here exactly
    so the rest of this file does not depend on an external file.
    """
    png = ("/home/ttuser/code/tt-local-generator/app/assets/tenstorrent.png")
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    try:
        pixels = np.asarray(Image.open(png).convert("RGBA"))
    except OSError:
        pytest.skip("the reference artwork is not on this machine")
    assert np.array_equal(pixels[..., 3] > 128, _mask_array())


# ── orientation: the tests that can actually fail ───────────────────────────
#
# Each case is a pixel of the artwork, its transpose, and its vertical
# mirror, chosen so the three do not agree. Any transposed, mirrored or
# rotated field fails at least one of them.
_ORIENTATION_PROBES = (
    # (col, row), inside?  -- read straight off `LOGO_MASK` above
    ((15, 30), True),      # the bottom face, near its apex
    ((30, 15), False),     # its TRANSPOSE: past the right face, empty
    ((15, 1), False),      # its MIRROR: the notch at the top, empty
    ((25, 5), True),       # the right face
    ((5, 25), False),      # its TRANSPOSE: left of the bottom face, empty
    ((25, 26), False),     # its MIRROR: below the right face, empty
    ((29, 10), True),      # the right face's far edge
    ((10, 29), False),     # its TRANSPOSE: outside the bottom apex, empty
    ((29, 21), False),     # its MIRROR: empty
    ((2, 8), True),        # the left face
    ((2, 23), False),      # its MIRROR: empty
    ((15, 16), False),     # the middle of the mark: the notches, not the mark
)


@pytest.mark.parametrize("cell,expected_inside", _ORIENTATION_PROBES)
def test_the_field_agrees_with_the_artwork_at_an_asymmetric_probe(
        cell, expected_inside):
    point = _from_mask(*cell)
    inside = mark.mark_sdf([point])[0] <= 0.0
    assert bool(inside) is expected_inside, (
        f"artwork pixel {cell} is "
        f"{'ink' if expected_inside else 'empty'} but the field disagrees")


def test_the_probes_would_actually_notice_a_transpose():
    """Guards the premise of the table above rather than the code: if the
    probe pairs ever stopped disagreeing, every case in it would pass under a
    transposed field and this file would be decorative.
    """
    swaps = [((15, 30), (30, 15)), ((25, 5), (5, 25)), ((29, 10), (10, 29))]
    mirrors = [((15, 30), (15, 1)), ((25, 5), (25, 26)), ((2, 8), (2, 23))]
    for label, pairs in (("swap", swaps), ("mirror", mirrors)):
        for first, second in pairs:
            a = mark.mark_sdf([_from_mask(*first)])[0] <= 0.0
            b = mark.mark_sdf([_from_mask(*second)])[0] <= 0.0
            assert a != b, (
                f"{first} and {second} agree; they cannot catch a {label}")


def test_the_hole_in_the_middle_is_really_a_hole():
    """The notch is the thing that makes this the Tenstorrent mark rather than
    a plain isometric cube, and it is exactly what a convex-only field (max of
    half-planes) would fill in. The centre of the mark is OUTSIDE it."""
    assert mark.mark_sdf([(0.0, 0.0)])[0] > 0.0


# ── the field is a distance field ───────────────────────────────────────────


def test_the_sign_is_negative_inside_and_positive_outside():
    inside = mark.mark_sdf([_from_mask(15, 25)])[0]
    outside = mark.mark_sdf([(3.0, 3.0)])[0]
    assert inside < 0.0 < outside


def test_far_away_the_distance_is_the_distance():
    """A point a long way off should report roughly how far away it actually
    is -- which a field built from unnormalised half-plane tests would not.
    The mark's own radius is ~1, so 10 units out is 9-ish away in any
    direction, and never more than 10."""
    for point in ((10.0, 0.0), (0.0, 10.0), (-10.0, 0.0), (0.0, -10.0)):
        distance = mark.mark_sdf([point])[0]
        assert 8.5 < distance < 10.0


def test_the_gradient_is_a_unit_vector_that_points_the_right_way():
    """The descent moves along this, so it has to be a direction and not just
    a number. Checked both sides of the boundary: outside it points AWAY
    (the field grows), inside it points TOWARDS the nearest edge."""
    probes = np.array([(2.0, 0.0), (0.0, 2.0), (-1.5, -1.5),
                       _from_mask(15, 25), _from_mask(2, 8)])
    distance, gradient = mark.mark_sdf_gradient(probes)
    assert np.allclose(np.linalg.norm(gradient, axis=1), 1.0, atol=1e-6)
    # A small step along the gradient must increase the distance, everywhere.
    stepped = mark.mark_sdf(probes + 1e-4 * gradient)
    assert np.all(stepped > distance)


def test_the_slab_is_the_mark_with_a_thickness():
    """The 3-D field is the mark extruded. On the plane it must agree with the
    2-D field, and a point directly above the mark must be outside by exactly
    how far above the slab it is."""
    on_plane = np.array([[*_from_mask(15, 25), 0.0]])
    distance, _ = mark.slab_sdf_gradient(on_plane, 0.05)
    assert distance[0] == pytest.approx(
        max(mark.mark_sdf(on_plane[:, :2])[0], -0.05), abs=1e-9)

    above = np.array([[*_from_mask(15, 25), 0.55]])
    distance, gradient = mark.slab_sdf_gradient(above, 0.05)
    assert distance[0] == pytest.approx(0.5, abs=1e-9)
    assert gradient[0][2] == pytest.approx(1.0, abs=1e-9)


# ── the descent ─────────────────────────────────────────────────────────────


def test_the_cloud_starts_as_noise_and_ends_as_the_mark():
    """The whole feature in one assertion: noise in, mark out.

    "Ends as the mark" is measured as "every point is inside it", which a
    cloud that merely shrank towards the origin would fail -- the centre of
    the mark is a hole (see the notch test above).
    """
    condensation = mark.MarkCondensation(count=800, seed=1)
    start = condensation.points() / condensation.scale
    assert (mark.mark_sdf(start[:, :2]) > 0.0).mean() > 0.6, (
        "the first frame should read as noise, not as a blurred logo")

    while not condensation.done:
        condensation.step()
    settled = condensation.points() / condensation.scale
    distance = mark.mark_sdf(settled[:, :2])
    # The descent decays geometrically, so the Gaussian's last stragglers are
    # still a hair outside when it stops. Measured over 3000 points: 99.2%
    # strictly inside, worst 0.003 against a mark one unit in radius.
    assert np.all(distance <= 0.02)
    assert (distance <= 0.0).mean() > 0.95
    # Same geometric residual on the third axis; the noise started with a
    # sigma of 1.4, so this still rejects any descent that leaves z alone.
    assert np.all(np.abs(settled[:, 2]) <= mark.HALF_THICKNESS + 0.01)


def test_the_cloud_fills_the_mark_rather_than_outlining_it():
    """Per-point target depths are what turn a wire outline into a filled
    mark (mark.py's `MAX_DEPTH`). Dropping them -- aiming every point at
    the zero level set -- leaves the whole cloud within a hair of the
    boundary, which is what this measures.
    """
    condensation = mark.MarkCondensation(count=2000, seed=2)
    while not condensation.done:
        condensation.step()
    depth = -mark.mark_sdf(condensation.points()[:, :2] / condensation.scale)
    # Measured 0.050 as shipped; an outline-only descent (every target depth
    # zero) leaves this at ~0.
    assert np.median(depth) > 0.035, (
        f"the cloud is hugging the outline (median depth {np.median(depth):.3f})")


def test_the_descent_converges_rather_than_arriving_in_one_jump():
    """The collapse is the point of the animation, so it has to take time.
    An over-eager step (the first version used 0.16 and was 99.5% done in 30
    of its 130 steps) makes the egg a snap followed by a still picture."""
    condensation = mark.MarkCondensation(count=500, seed=3)
    outside = []
    for _ in range(condensation.steps):
        condensation.step()
        points = condensation.points() / condensation.scale
        outside.append(np.maximum(mark.mark_sdf(points[:, :2]), 0.0).mean())
    quarter = condensation.steps // 4
    assert outside[quarter] > 0.02, (
        "a quarter of the way through, the cloud should still visibly be "
        "on its way in")
    assert outside[-1] < 5e-3
    assert all(b <= a + 1e-9 for a, b in zip(outside, outside[1:])), (
        "the descent must never move the cloud back out")


def test_a_point_that_has_already_arrived_stops_moving():
    """The `relu` in the descent is what makes this a SETTLING motion.

    A point already at (or past) its own target level set has nothing left to
    do, so it stops. Drop the relu and the same point is pushed back OUT
    until it sits exactly on the target isosurface -- a cloud that hunts
    towards a shell rather than one that lands and stays, and one that
    thins the mark's interior out as it goes.

    Asserted on a point placed deep inside with a target depth of zero,
    because that is the only configuration where the two differ by more than
    rounding.
    """
    condensation = mark.MarkCondensation(count=1, seed=6)
    deep = np.array([[*_from_mask(15, 25), 0.0]])
    assert mark.mark_sdf(deep[:, :2])[0] < -0.1, "probe is not actually inside"
    condensation._points = deep.copy()
    condensation._depth = np.zeros(1)
    # The run's swirl is a RIGID ROTATION of the whole cloud, so it moves a
    # point that has stopped descending -- which is correct, and is what
    # `test_the_swirl_is_a_rigid_rotation_that_ends_before_the_run_does`
    # covers. Zeroed here so that what is left to move this point is the
    # descent alone, which is the thing under test.
    condensation._swirl[:] = 0.0

    condensation.step()
    assert np.array_equal(condensation._points, deep)


def test_the_same_seed_gives_the_same_cloud():
    first = mark.MarkCondensation(count=200, seed=99).step()
    second = mark.MarkCondensation(count=200, seed=99).step()
    assert np.array_equal(first, second)


def test_the_cloud_is_what_the_viewer_wants():
    condensation = mark.MarkCondensation(count=64, seed=4)
    points = condensation.step()
    assert points.dtype == np.float32
    assert points.shape == (64, 3)
    assert points.flags["C_CONTIGUOUS"]


def test_stepping_past_the_end_does_nothing():
    """`done` stops the work, not just the clock: ui/app.py drops the timer
    when the descent finishes, and a stray extra call must not restart it or
    move anything."""
    condensation = mark.MarkCondensation(count=100, seed=5, steps=3)
    for _ in range(3):
        condensation.step()
    settled = condensation.step()
    assert condensation.completed == 3
    assert np.array_equal(condensation.step(), settled)


# ── the brand ───────────────────────────────────────────────────────────────


def test_the_mark_is_drawn_in_the_brand_purple():
    """Not the booth's teal, and not something adjacent to the logo colour."""
    assert mark.BRAND_PURPLE_HEX.upper() == "#7C68FA"
    assert mark.BRAND_PURPLE == pytest.approx(
        (0x7C / 255.0, 0x68 / 255.0, 0xFA / 255.0))


# ── different every time, and the same mark every time ──────────────────────
#
# "It is different every time" is a genuinely hard property to test, because
# each of the two obvious assertions passes for a broken implementation:
#
#   * "two runs differ" passes for one that returns pure noise;
#   * "the mark is recognisable" passes for one that ignores the seed.
#
# So both edges are asserted, and they are asserted with DIFFERENT
# measurements rather than two readings of the same number: the journeys are
# compared point by point in mid-descent, and the destination is compared
# against the shipped artwork's own 32x32 mask -- the same independent oracle
# the field tests above lean on, not against another run of this code.
#
# `test_the_recognisability_test_can_actually_fail` is the control: it feeds
# the mark test a cloud that is still noise and requires it to REJECT it. A
# threshold nothing can fail is not a threshold.


def _occupancy(points):
    """Which of the artwork's 32x32 cells the cloud puts at least one point in."""
    size = len(LOGO_MASK)
    col = np.round(points[:, 0] * _MASK_SCALE + _MASK_CENTRE[0]).astype(int)
    row = np.round(-points[:, 1] * _MASK_SCALE + _MASK_CENTRE[1]).astype(int)
    keep = (col >= 0) & (col < size) & (row >= 0) & (row < size)
    grid = np.zeros((size, size), dtype=bool)
    grid[row[keep], col[keep]] = True
    return grid


def _mark_scores(points):
    """`(precision, coverage)` of a cloud against the shipped artwork's mask.

    * **precision** -- what fraction of the POINTS land on an ink cell. This
      is the "is it the mark" number.
    * **coverage** -- what fraction of the ink CELLS have a point in them.
      This is the "does it fill the mark rather than collapse into a corner
      of it" number.

    Measured over 25 random runs each at 3,000 / 4,000 / 6,000 points:

    | | settled | starting noise |
    |---|---|---|
    | precision | 0.92 - 0.96 | 0.09 - 0.17 |
    | coverage  | 0.61 - 0.94 | 0.44 - 0.89 |

    **Occupancy IoU was tried first and is not a usable threshold**, which is
    worth recording because it looked fine on fixed seeds. IoU charges the
    cloud for every ink cell it happens to miss, so it depends on the point
    count and on where the draw fell: 0.616 to 0.772 over 30 random draws at
    4,000 points, against a floor of 0.65. That is a test that fails about one
    run in three, and it did -- on hardware, where it is slowest to diagnose.
    Precision has no such dependence, and coverage is only ever used as the
    second half of a pair, never as the discriminator (noise covers plenty of
    ink cells simply by being everywhere).
    """
    truth = _mask_array()
    size = len(LOGO_MASK)
    col = np.round(points[:, 0] * _MASK_SCALE + _MASK_CENTRE[0]).astype(int)
    row = np.round(-points[:, 1] * _MASK_SCALE + _MASK_CENTRE[1]).astype(int)
    inside = (col >= 0) & (col < size) & (row >= 0) & (row < size)
    on_ink = np.zeros(len(points), dtype=bool)
    on_ink[inside] = truth[row[inside], col[inside]]
    occupied = _occupancy(points)
    return float(on_ink.mean()), float((occupied & truth).sum() / truth.sum())


# Both floors sit far below what a settled cloud measures and far above what
# noise does -- see the table in `_mark_scores`.
MARK_PRECISION_FLOOR = 0.85
MARK_COVERAGE_FLOOR = 0.50


def _looks_like_the_mark(points):
    """True if `points` are on the mark AND spread over it."""
    precision, coverage = _mark_scores(points)
    return precision >= MARK_PRECISION_FLOOR and coverage >= MARK_COVERAGE_FLOOR


def _settled(seed, count=4000):
    condensation = mark.MarkCondensation(count=count, seed=seed)
    while not condensation.done:
        condensation.step()
    return condensation._points


def test_the_recognisability_test_can_actually_fail():
    """The control for the two tests below it.

    Both of them assert `_looks_like_the_mark(...) >= MARK_IOU_FLOOR`, and
    that assertion is worth exactly nothing unless something can fail it. The
    starting cloud -- which is the thing a broken descent would leave on
    screen -- must be rejected.
    """
    start = mark.run_parameters(seed=4242, count=4000).positions
    assert not _looks_like_the_mark(start)
    precision, _coverage = _mark_scores(start)
    assert precision < 0.25, (
        f"undescended noise scored {precision:.3f} on the mark; the floor is "
        f"{MARK_PRECISION_FLOOR}")


def test_the_recognisability_test_also_rejects_a_cloud_that_collapsed():
    """The other way to score well without being the mark: put every point in
    one cell. Precision alone would call that a perfect mark, which is why
    `_looks_like_the_mark` needs coverage as well."""
    heap = np.zeros((2000, 3))
    heap[:, :2] = _from_mask(15, 25)
    precision, coverage = _mark_scores(heap)
    assert precision > MARK_PRECISION_FLOOR, "the probe must be ON the mark"
    assert coverage < MARK_COVERAGE_FLOOR
    assert not _looks_like_the_mark(heap)


def test_every_run_lands_on_the_same_mark():
    """Edge one: whatever was drawn, the DESTINATION is the Tenstorrent mark.

    Mutation this catches: any change that lets the run's parameters move
    where the cloud ends up rather than how it gets there -- a swirl that
    never decays to zero, an anisotropy applied to the settled cloud instead
    of the starting one, a per-point gain so low the cloud has not arrived by
    the last step.
    """
    for seed in (11, 22, 33, 44, 55):
        settled = _settled(seed)
        assert _looks_like_the_mark(settled), (
            f"seed {seed} did not land on the mark: {_mark_scores(settled)}")
        distance, _ = mark.slab_sdf_gradient(settled, mark.HALF_THICKNESS)
        assert (distance <= 1e-3).mean() > 0.98, (
            f"seed {seed} left too much of the cloud outside the slab")


def test_two_runs_take_visibly_different_paths():
    """Edge two: the JOURNEY does not repeat.

    Measured in mid-descent, where a difference is a difference in the
    animation rather than merely in which point ended up where. The mark is
    two units across, so a mean per-point separation of half a unit at the
    halfway mark is a plainly different picture on screen -- and the same
    measurement taken between two runs of the SAME seed is asserted to be
    zero, which is what stops this passing for an implementation that has
    simply become nondeterministic noise.

    Mutations this catches: pinning the seed to a constant; drawing fresh
    positions but reusing one fixed swirl/gain/anisotropy draw.
    """
    def midway(seed):
        run = mark.MarkCondensation(count=2000, seed=seed)
        for _ in range(run.steps // 2):
            run.step()
        return run._points.copy()

    a, b, again = midway(101), midway(202), midway(101)
    assert np.array_equal(a, again), "the same seed must give the same run"
    separation = np.linalg.norm(a - b, axis=1).mean()
    assert separation > 0.5, (
        f"two runs are only {separation:.3f} apart halfway down")


def test_a_fresh_egg_draws_a_new_seed_every_time():
    """No seed means a NEW seed, from the OS, not a default written in the
    module. That default is what the egg had before this change, and it is
    the reason it landed the same way every time.
    """
    seeds = {mark.MarkCondensation(count=8).seed for _ in range(6)}
    assert len(seeds) == 6


def test_the_swirl_is_spent_early_and_then_exactly_zero():
    """The swirl is a rigid rotation about the mark's centre, and the mark is
    not rotationally symmetric -- so a swirl still running on the last step
    is a mark still being dragged off itself when the animation stops.

    Mutation this catches: an exponential (asymptotic) decay in place of the
    hard cutoff. That version leaves a small but nonzero rotation on every
    step, which this rejects and an "is the mark recognisable" test would
    not.
    """
    params = mark.run_parameters(seed=8, count=4)
    schedule = params.swirl_schedule()
    assert schedule.shape == (params.steps,)
    assert schedule.sum() == pytest.approx(params.swirl)
    cut = int(round(params.steps * mark.SWIRL_FRACTION))
    assert np.all(schedule[cut:] == 0.0)
    assert np.any(schedule[:cut] != 0.0)


def test_the_swirl_turns_the_cloud_without_moving_it_in_or_out():
    """What makes a swirl safe to add to a descent: it is a rotation, so it
    changes the path and not the distance from the axis it turns about.

    Measured on the REAL step, with the descent switched off (every target
    depth set far inside, so `relu(distance + depth)` is zero for every point
    and nothing but the swirl moves). An earlier version of this test
    re-derived the rotation itself and compared numpy against numpy, which
    tested nothing: the shipped code writes the rotated x back into the array
    before reading x for y, and dropping the `.copy()` that prevents that
    turns the rotation into a SHEAR -- a mutation that version could not
    catch, and this one does.
    """
    params = mark.run_parameters(seed=9, count=500)
    run = mark.MarkCondensation(params=params)
    run._depth = np.full(params.count, -10.0)     # nothing has anywhere to go
    assert float(run._swirl[0]) != 0.0
    before = np.hypot(run._points[:, 0], run._points[:, 1])
    z_before = run._points[:, 2].copy()

    run.step()

    after = np.hypot(run._points[:, 0], run._points[:, 1])
    assert np.allclose(after, before), "the swirl is not a rotation"
    assert not np.allclose(run._points[:, 0],
                           mark.MarkCondensation(params=params)._points[:, 0]), (
        "the swirl did not turn the cloud at all")
    assert np.array_equal(run._points[:, 2], z_before), "the swirl is in-plane"


def test_the_per_point_gains_actually_stagger_the_descent():
    """The gains are what make one run's middle look different from
    another's: some points land early, some are still drifting in.

    Two points at the SAME place with the same target, differing only in
    gain, must move by different amounts -- which is the whole of what the
    gain does, and is invisible to any test that only looks at where the
    cloud ends up (the ramp brings them all home either way).

    Mutation this catches: drawing the gains and then not using them, or
    drawing them all equal to 1.0.
    """
    params = mark.run_parameters(seed=12, count=2)
    params.gains = np.array([0.8, 1.3])
    run = mark.MarkCondensation(params=params)
    run._points = np.array([[1.6, 0.9, 0.0], [1.6, 0.9, 0.0]])
    run._depth = np.zeros(2)
    run._swirl[:] = 0.0
    start = run._points.copy()

    run.step()

    moved = np.linalg.norm(run._points - start, axis=1)
    assert moved[0] > 0.0
    assert moved[1] / moved[0] == pytest.approx(1.3 / 0.8, rel=1e-6)


def test_the_gains_are_drawn_across_their_whole_range():
    """A range nothing samples is a range that is not doing anything."""
    gains = mark.run_parameters(seed=13, count=4000).gains
    assert gains.min() < 0.9 and gains.max() > 1.2
    assert mark.STEP_GAIN_RANGE[0] <= gains.min()
    assert gains.max() <= mark.STEP_GAIN_RANGE[1]


def test_the_gain_ramp_brings_every_point_back_to_full_step_by_the_end():
    """The per-point gains are what stagger arrival; the ramp is what stops
    the slowest of them leaving a halo. Measured without it: stragglers up to
    0.06 mark units outside, against 0.016 with it."""
    params = mark.run_parameters(seed=10, count=4)
    blend = params.gain_schedule()
    assert blend[0] == 0.0
    assert blend[-1] == pytest.approx(1.0)
    start = int(params.steps * (1.0 - mark.GAIN_RAMP_FRACTION))
    assert blend[start - 1] == 0.0, "the ramp must not start early"
    assert np.all(np.diff(blend) >= -1e-12), "the ramp must never run backwards"


def test_the_starting_cloud_changes_shape_but_not_overall_size():
    """The anisotropy is normalised to geometric mean 1 on purpose: without
    it a run can draw three large axis scales at once and start with a cloud
    too far out to come in within `STEPS`.
    """
    for seed in range(20):
        params = mark.run_parameters(seed=seed, count=4)
        assert np.exp(np.log(params.axes).mean()) == pytest.approx(1.0)
        assert params.axes.min() < params.axes.max(), "no anisotropy at all"
        assert mark.SEED_SIGMA_RANGE[0] <= params.sigma <= mark.SEED_SIGMA_RANGE[1]


def test_a_run_can_be_replayed_from_the_seed_it_reports():
    """The seed goes into the log and onto the wire (`egg_frame`) so a run
    someone liked can be reproduced -- and so the device implementation can
    be compared against this one on identical parameters."""
    once = mark.MarkCondensation(count=64)
    replay = mark.MarkCondensation(count=64, seed=once.seed)
    assert np.array_equal(once.step(), replay.step())
