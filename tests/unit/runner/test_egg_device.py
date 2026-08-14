"""`runner/egg.py`, everything about it that does not need a chip.

The descent itself is checked against a real device in
`tests/integration/test_egg_on_device.py` (hardware-gated, `--hw`). What is
here is the part that would still be wrong on hardware and much harder to see
there: the edge tables the whole field is built from, the event shape, and the
pacing.

`_edge_tables` is worth this much attention because it is the one place the
device implementation restates something `mark.py` already knows. Everything
downstream of it -- seventeen distances, an even-odd parity, a gradient -- is
arithmetic on those numbers, so a table that is subtly wrong produces a
plausible-looking blob rather than an error.
"""

import numpy as np
import pytest

import mark
from runner import egg


def _tables():
    return egg._edge_tables(mark.mark_polygons())


def test_the_edge_table_is_the_marks_own_edges_and_then_padding():
    """One row per lane, 17 real edges, and the rest filler.

    Mutation this catches: building the table from a subset of the polygons
    (e.g. forgetting the third face), which leaves a mark missing a whole
    side and no exception anywhere.
    """
    vx, vy, ex, ey, ny, inv = _tables()
    for row in (vx, vy, ex, ey, ny, inv):
        assert row.shape == (1, egg._EDGE_LANES)
        assert row.dtype == np.float32
    real = sum(len(p) for p in mark.mark_polygons())
    assert real == 17
    starts = np.stack([vx[0, :real], vy[0, :real]], axis=1)
    expected = np.concatenate([np.asarray(p) for p in mark.mark_polygons()])
    assert np.allclose(starts, expected, atol=1e-6)
    edges = np.stack([ex[0, :real], ey[0, :real]], axis=1)
    expected_edges = np.concatenate(
        [np.roll(np.asarray(p), -1, axis=0) - np.asarray(p)
         for p in mark.mark_polygons()])
    assert np.allclose(edges, expected_edges, atol=1e-6)


def test_each_edge_wraps_within_its_own_face_and_not_into_the_next():
    """`next_y` is what the even-odd crossing test walks, so an edge that
    wrapped from the end of one face into the start of the next would join
    three closed polygons into one open path -- and the inside test would be
    wrong in a way that only shows up near the notches.
    """
    _vx, vy, _ex, _ey, ny, _inv = _tables()
    at = 0
    for poly in mark.mark_polygons():
        count = len(poly)
        assert np.allclose(ny[0, at:at + count],
                           np.roll(vy[0, at:at + count], -1), atol=1e-6)
        at += count


def test_the_padding_lanes_are_far_away_and_cross_nothing():
    """The lanes exist so no reduction here depends on what ttnn does with a
    tile's padding -- but only if they are inert. They must never be the
    nearest edge, must not divide by zero, and must contribute exactly zero
    crossings for any point anywhere near the mark.
    """
    vx, vy, ex, ey, ny, inv = _tables()
    real = sum(len(p) for p in mark.mark_polygons())
    pad = slice(real, egg._EDGE_LANES)
    assert np.all(inv[0, pad] == 0.0), "a degenerate edge must not be inverted"
    assert np.all(ex[0, pad] == 0.0) and np.all(ey[0, pad] == 0.0)

    # For every point in a generous box around the mark: the padding is
    # further than any real edge, and its crossing terms are both zero.
    rng = np.random.default_rng(0)
    probes = rng.uniform(-3.0, 3.0, size=(400, 2))
    relx = probes[:, 0:1] - vx
    rely = probes[:, 1:2] - vy
    dist2 = relx ** 2 + rely ** 2
    assert dist2[:, pad].min() > dist2[:, :real].max()

    above = probes[:, 1:2] >= vy
    below = probes[:, 1:2] < ny
    left = (ex * rely) > (ey * relx)
    crossings = (above & below & left) | (~above & ~below & ~left)
    assert not crossings[:, pad].any()


def test_the_flat_table_reproduces_the_marks_own_inside_test():
    """The claim `_edge_tables`' docstring makes, measured: one even-odd
    parity over ALL seventeen edges answers "inside the mark?" -- which is
    only true because the three faces never overlap.

    Mutation this catches: the argument being wrong, which would be invisible
    on hardware (a slightly different blob) and is decisive here.
    """
    vx, vy, ex, ey, ny, _inv = _tables()
    rng = np.random.default_rng(1)
    probes = rng.uniform(-1.3, 1.3, size=(3000, 2))
    relx = probes[:, 0:1] - vx
    rely = probes[:, 1:2] - vy
    above = probes[:, 1:2] >= vy
    below = probes[:, 1:2] < ny
    left = (ex * rely) > (ey * relx)
    crossings = ((above & below & left) | (~above & ~below & ~left)).sum(axis=1)
    inside_flat = crossings % 2 == 1
    inside_mark = mark.mark_sdf(probes) < 0.0
    disagree = inside_flat != inside_mark
    # Points sitting within float noise of an edge may legitimately differ.
    boundary = np.abs(mark.mark_sdf(probes)) < 1e-9
    assert not np.any(disagree & ~boundary)


# ── the event ───────────────────────────────────────────────────────────────


def test_an_egg_frame_carries_where_it_ran_and_what_it_was_seeded_with():
    """`card` is what the booth puts on screen ("Computed on chip 2"), so it
    must come from the event rather than from anything the UI assumes; `seed`
    is what makes a run someone liked reproducible."""
    coords = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    event = egg.egg_frame_event("e1", 7, 180, coords, card=2, seed=99)
    assert event["type"] == "egg_frame"
    assert (event["egg_id"], event["card"], event["seed"]) == ("e1", 2, 99)
    assert (event["step"], event["total"], event["n_points"]) == (7, 180, 2)
    from protocol.events import unpack_coords
    assert np.allclose(unpack_coords(event["coords_b64"]), coords)


def test_an_egg_frame_is_not_a_fold_frame():
    """They are routed to different viewers by type alone. If this ever
    became a `frame` with an extra field, an easter egg could put a logo in
    the middle of a visitor's protein."""
    from runner.shaping import frame_event
    fold = frame_event("j1", 1, 200, [[0.0, 0.0, 0.0]])
    egg_frame = egg.egg_frame_event("e1", 1, 180, [[0.0, 0.0, 0.0]],
                                    card=0, seed=1)
    assert fold["type"] != egg_frame["type"]
    assert "job_id" not in egg_frame


# ── the run ─────────────────────────────────────────────────────────────────


class _FakeRun:
    """`DeviceCondensation` with the device taken out.

    Substituted for the real class so `run_egg`'s own logic -- the step-0
    frame, the frame count, the pacing floor, the reported seed -- is tested
    without a chip. What it does NOT stand in for is the arithmetic; that is
    the hardware test's job, and pretending otherwise here would be exactly
    the "parallel model of the code" defect runner/folder.py's docstring
    warns about.
    """

    def __init__(self, device, params):
        self.params = params
        self.steps = params.steps
        self.count = params.count
        self.completed = 0

    def step(self):
        self.completed += 1

    def points(self):
        return np.full((self.count, 3), float(self.completed), dtype=np.float32)


@pytest.fixture
def fake_run(monkeypatch):
    monkeypatch.setattr(egg, "DeviceCondensation", _FakeRun)


def test_a_run_emits_the_starting_noise_and_then_every_step(fake_run):
    """Step 0 is the untouched draw. Without it the first thing on screen is
    a cloud that has already moved, and the visitor never sees where the chip
    actually started."""
    got = []
    egg.run_egg(object(), got.append, egg_id="e1", card=1, seed=5,
                count=8, steps=6, gap_s=0.0)
    assert [e["step"] for e in got] == [0, 1, 2, 3, 4, 5, 6]
    assert all(e["total"] == 6 and e["card"] == 1 for e in got)
    assert got[0]["n_points"] == 8


def test_a_run_reports_the_seed_it_used_and_a_fresh_one_each_time(fake_run):
    """`seed=None` is the booth's normal case -- it is what makes the egg
    different every time -- and the seed still has to come back so the log
    and the wire can say which run this was."""
    first = egg.run_egg(object(), lambda e: None, egg_id="a", card=0,
                        count=4, steps=1, gap_s=0.0)
    second = egg.run_egg(object(), lambda e: None, egg_id="b", card=0,
                         count=4, steps=1, gap_s=0.0)
    assert first != second

    got = []
    used = egg.run_egg(object(), got.append, egg_id="c", card=0, seed=1234,
                       count=4, steps=1, gap_s=0.0)
    assert used == 1234
    assert {e["seed"] for e in got} == {1234}


def test_the_frames_are_spaced_so_a_fast_chip_cannot_flood_the_socket(fake_run):
    """A whole run is 17 MB. `EventServer.broadcast` holds a lock across the
    send loop, so handing it all of that as fast as the chip produces it
    would sit in front of a fold's own frames.

    Mutation this catches: dropping the gap entirely. The UI buffers these
    and plays them on its own clock, so nothing on screen would look wrong --
    which is exactly why this needs a test rather than an eye.
    """
    slept = []
    clock = iter(np.arange(0, 100, 0.001))
    egg.run_egg(object(), lambda e: None, egg_id="e1", card=0, seed=1,
                count=4, steps=5, gap_s=0.02,
                clock=lambda: float(next(clock)), sleep=slept.append)
    assert slept, "a chip producing frames faster than the floor must be paced"
    assert max(slept) <= 0.02 + 1e-9
