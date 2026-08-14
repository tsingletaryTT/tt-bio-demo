r"""The Tenstorrent mark, computed rather than blitted.

What this is
------------
A signed distance field for the Tenstorrent mark, and a gradient descent that
pulls a cloud of Gaussian noise into it. `ui/app.py` renders the result through
the same `StructureViewer.set_points` the diffusion trajectory uses, which is
the whole point: it is the same noise-becomes-structure motion, on the same
widget, driven by real arithmetic rather than by a picture of one.

**It is an easter egg, and it is not a fold.** Nothing here is chemistry and
nothing here is a molecule. The booth says exactly that on screen (see
`_EGG_*` in ui/app.py), because this project's credibility rests on a visitor
being able to trust that the protein they are looking at is the protein the
chips folded. An easter egg that could be mistaken for a structure would spend
that trust for a joke.

Where the arithmetic runs, and why this module is at the repo root
------------------------------------------------------------------
It runs **on a Tenstorrent chip**, in a worker process, whenever a chip is
free -- `runner/egg.py` reimplements the descent below in ttnn, and the points
reach the screen as `egg_frame` events over the same socket a fold's frames
use. This module is the CPU fallback for when every chip is folding, and it is
also the *specification* the device implementation is tested against.

That is why this file sits beside `protocol/` at the repo root rather than
inside `ui/`, which is where it started: it is now imported by BOTH venvs (the
UI's, which has no torch and no ttnn, and the runner's, which has no gi), so
it is subject to `protocol/`'s import rule -- **stdlib and numpy only**. The
runner may not import `ui.*` and the UI may not import `runner.*`; a shared
module at the root is the only place code both halves need can live.

No GTK, no torch, no tt-bio, no image files: this module is arithmetic on numpy
arrays, and it can be tested, rasterised and looked at with no display.

The geometry: an isometric lattice, not a traced bitmap
--------------------------------------------------------
The mark is a cube seen corner-on, cut into three faces, with a chevron notch
bitten out of two of them. Drawn that way, every vertex necessarily lands on
the isometric lattice -- columns one step apart, rows `tan(30 degrees)` of a
step apart -- and the shipped vector artwork
(`tt-vscode-toolkit/assets/img/tt_symbol_purple.svg`) confirms it exactly: its
seventeen vertices use five distinct x values 28 units apart and eight distinct
y values 16.2 units apart, and 16.2 / 28 = 0.5786 against tan(30) = 0.5774.

So the mark is written here as integer lattice coordinates plus the lattice
itself. That is a construction, not a trace: `_ROW_RISE` is derived from
sqrt(3), and `tests/unit/test_mark.py` rasterises this field and compares it
against the real 32x32 artwork, so "is this actually the Tenstorrent mark" is
a measured claim rather than an assertion.

Note what the artwork settles that eyeballing it does not: the three pieces are
NOT congruent and the mark is NOT three-fold symmetric. The bottom face carries
a notch, the top-and-left piece carries a notch and has seven vertices, and the
right face is a plain quadrilateral. A "three rhombi at 120 degrees" model
looks right at a glance and is wrong -- it was built first, and rasterising it
against the artwork is what caught it (0.60 IoU).

Why an exact polygon field and not a stack of half-planes
----------------------------------------------------------
Two of the three pieces are NOT convex -- a notch apex is a reflex vertex -- so
the usual "max of signed half-plane distances" trick, which is only correct for
a convex intersection, would quietly give the wrong field exactly around the
notch that makes this the Tenstorrent mark and not a plain cube.
`_polygon_sdf` is the exact point-to-polygon distance instead (nearest point
over every edge segment, even-odd crossing test for the sign), which is correct
for any simple polygon and hands back the nearest point -- so the GRADIENT is
exact too, and the descent below needs no finite differences.
"""

import numpy as np

# ── the brand ───────────────────────────────────────────────────────────────
#
# Sampled from the shipped artwork rather than typed from memory: it is the
# `fill` of every polygon in `tt_symbol_purple.svg`, and 251 of the 351 opaque
# pixels of `tt-local-generator/app/assets/tenstorrent.png` are exactly it.
#
# It deliberately does NOT live in ui/panels.py's palette. That palette is the
# booth's own dark forest-teal identity; this is the logo's purple, and it is
# used in exactly one place, for exactly one thing, which is the easter egg.
BRAND_PURPLE_HEX = "#7C68FA"
BRAND_PURPLE = (0x7C / 255.0, 0x68 / 255.0, 0xFA / 255.0)

# ── the lattice ─────────────────────────────────────────────────────────────

# One lattice ROW, in mark units, given a lattice COLUMN of 0.5. This is the
# isometric angle and the only reason the three faces meet edge to edge: a cube
# seen corner-on has its edges at 30 degrees to the horizontal, so a step of one
# column is accompanied by a step of tan(30) = 1/sqrt(3) rows.
_COLUMN = 0.5
_ROW_RISE = _COLUMN / np.sqrt(3.0)

# Lattice coordinates (column, row) of every vertex of the mark, read off the
# vector artwork and reduced to integers. Rows count DOWNWARD, as the artwork
# does; `_from_lattice` flips them, because the viewer's y is up.
#
#   columns 0..4 left to right, rows 0..7 top to bottom
#
# Piece 0 -- the bottom face. A chevron: apex down at (2,7), notch apex up at
#   (2,5), squared off at columns 1 and 3.
# Piece 1 -- the top face and the left face, which the artwork draws as one
#   polygon. Seven vertices, and the reflex one is the notch apex at (1,2).
# Piece 2 -- the right face. A plain quadrilateral, no notch.
#
# They meet, but do not overlap: piece 1 reaches column 3 only at row 2, which
# is a single point on piece 2's left edge. That contact is visible in the
# artwork as the one raster row where the mark spans its whole width.
_LATTICE_PIECES = (
    ((2, 5), (1, 4), (1, 6), (2, 7), (3, 6), (3, 4)),
    ((1, 0), (0, 1), (0, 3), (1, 4), (1, 2), (2, 3), (3, 2)),
    ((3, 0), (3, 4), (4, 3), (4, 1)),
)

# The lattice point the mark is centred on: the middle column, half way down.
_LATTICE_CENTRE = (2.0, 3.5)


def _from_lattice(cells):
    """Lattice (column, row) pairs to mark coordinates, y up, centred on 0.

    The mark comes out one unit from centre to left or right edge, and 1.01
    units from centre to the top or bottom vertex -- so `1.0` is a good working
    radius for anything that needs one.
    """
    cells = np.asarray(cells, dtype=np.float64).reshape(-1, 2)
    x = (cells[:, 0] - _LATTICE_CENTRE[0]) * _COLUMN
    y = -(cells[:, 1] - _LATTICE_CENTRE[1]) * _ROW_RISE
    return np.stack([x, y], axis=1)


def mark_polygons():
    """The three faces of the mark, each as an (M, 2) array of vertices.

    Index 0 is always the bottom face (the one whose apex points at -y), so a
    caller that needs a specific piece can name it.
    """
    return [_from_lattice(piece) for piece in _LATTICE_PIECES]


# ── the field ───────────────────────────────────────────────────────────────


def _polygon_sdf(points, verts):
    """Exact signed distance from each of `points` to a simple polygon.

    Returns `(distance, outward)`: `distance` is negative inside, and `outward`
    is the unit gradient of that distance -- the direction of steepest
    INCREASE, which is away from the polygon outside it and towards the nearest
    edge inside it.

    `points` is (N, 2); `verts` is (M, 2), in either winding order.
    """
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    verts = np.asarray(verts, dtype=np.float64)
    edges = np.roll(verts, -1, axis=0) - verts               # (M, 2)
    rel = points[:, None, :] - verts[None, :, :]             # (N, M, 2)

    # Nearest point on each edge SEGMENT -- clamped to its ends, which is what
    # makes this exact around corners instead of only near the edges.
    edge_len2 = np.einsum("md,md->m", edges, edges)
    edge_len2 = np.where(edge_len2 > 0.0, edge_len2, 1.0)    # degenerate guard
    along = np.clip(np.einsum("nmd,md->nm", rel, edges) / edge_len2, 0.0, 1.0)
    offset = rel - along[:, :, None] * edges[None, :, :]     # (N, M, 2)
    dist2 = np.einsum("nmd,nmd->nm", offset, offset)

    nearest_edge = dist2.argmin(axis=1)
    rows = np.arange(points.shape[0])
    closest = offset[rows, nearest_edge]                     # (N, 2)
    distance = np.sqrt(dist2[rows, nearest_edge])

    # Even-odd crossing test (Quilez). Three predicates per edge: the point is
    # at or above this vertex, below the next one, and to the left of the edge.
    # All three, or none of the three, counts as one crossing; an odd number of
    # crossings means inside. Taking "none of the three" as a crossing is what
    # makes it work for edges running either way -- dropping that half inverts
    # the whole field, which is exactly what it did on the first attempt here.
    next_y = np.roll(verts[:, 1], -1)
    above = points[:, 1:2] >= verts[None, :, 1]
    below_next = points[:, 1:2] < next_y[None, :]
    left_of = (edges[None, :, 0] * rel[:, :, 1]
               > edges[None, :, 1] * rel[:, :, 0])
    crossings = ((above & below_next & left_of)
                 | (~above & ~below_next & ~left_of))
    inside = crossings.sum(axis=1) % 2 == 1
    sign = np.where(inside, -1.0, 1.0)

    # `closest` runs from the nearest boundary point to the query point, so it
    # already points outward; flipping it inside gives the gradient for both.
    safe = np.where(distance > 0.0, distance, 1.0)
    outward = sign[:, None] * closest / safe[:, None]
    return sign * distance, outward


def mark_sdf(points_xy):
    """Signed distance from each (x, y) to the mark. Negative inside."""
    return mark_sdf_gradient(points_xy)[0]


def mark_sdf_gradient(points_xy):
    """`(distance, gradient)` for the mark: the union of its three faces.

    A union's distance is the minimum of its parts', so the gradient is the
    gradient of whichever part is nearest -- exact here, because the faces
    touch but never overlap.
    """
    points_xy = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
    fields = [_polygon_sdf(points_xy, poly) for poly in mark_polygons()]
    distances = np.stack([f[0] for f in fields])             # (3, N)
    gradients = np.stack([f[1] for f in fields])             # (3, N, 2)
    nearest = distances.argmin(axis=0)
    rows = np.arange(points_xy.shape[0])
    return distances[nearest, rows], gradients[nearest, rows]


def slab_sdf_gradient(points_xyz, half_thickness):
    """The mark extruded to a thin slab: `(distance, gradient)` in 3-D.

    The exact extrusion field (Quilez): with `p` the in-plane distance and `q`
    the out-of-plane one,

        d = min(max(p, q), 0) + |(max(p, 0), max(q, 0))|

    Inside both, the distance is the larger (less negative) of the two; outside
    either, it is the length of the positive part -- which rounds the slab's
    rim correctly instead of squaring it off. The gradient follows term by
    term, so a point starting well off-axis travels diagonally in towards the
    mark rather than in two separate legs.
    """
    points_xyz = np.asarray(points_xyz, dtype=np.float64).reshape(-1, 3)
    plane, plane_grad = mark_sdf_gradient(points_xyz[:, :2])
    z = points_xyz[:, 2]
    depth = np.abs(z) - float(half_thickness)
    z_dir = np.where(z >= 0.0, 1.0, -1.0)

    outside_plane = np.maximum(plane, 0.0)
    outside_depth = np.maximum(depth, 0.0)
    outer = np.hypot(outside_plane, outside_depth)
    distance = np.minimum(np.maximum(plane, depth), 0.0) + outer

    gradient = np.zeros_like(points_xyz)
    # Outside in at least one direction: blend the two by how far outside each.
    out = outer > 0.0
    safe = np.where(out, outer, 1.0)
    gradient[out, :2] = (plane_grad[out]
                         * (outside_plane[out] / safe[out])[:, None])
    gradient[out, 2] = z_dir[out] * outside_depth[out] / safe[out]
    # Fully inside: the field is whichever wall is nearer.
    deep = ~out
    by_plane = deep & (plane >= depth)
    by_face = deep & (plane < depth)
    gradient[by_plane, :2] = plane_grad[by_plane]
    gradient[by_face, 2] = z_dir[by_face]
    return distance, gradient


# ── the descent ─────────────────────────────────────────────────────────────

# How wide the starting noise is, in mark units (the mark itself is ~1 from
# centre to edge). Wide enough that the first frame reads as noise rather than
# as a slightly blurred logo.
#
# Kept as the single number the geometry above was tuned against, and used as
# the midpoint of `SEED_SIGMA_RANGE` below -- the run-to-run variation is a
# spread AROUND this, not a replacement for it.
SEED_SIGMA = 1.4

# Half the slab's thickness. The mark is a plane figure, so the cloud settles
# into a sheet rather than a volume -- but not a mathematical plane: a little
# thickness gives the points something to be scattered through.
HALF_THICKNESS = 0.05

# Each point descends to its OWN depth inside the mark rather than all of them
# to the surface. Aiming everything at the zero level set draws a wire outline,
# which is thin and hard to read across a room; spreading the targets through
# the interior fills the mark in. The faces are ~0.5 units thick across the
# arms, so 0.24 is just inside half of that: the deepest points settle on the
# spine rather than piling past it, and the band of filled depths reaches
# nearly all the way in. Rendered at 0.20 and 0.24, and at a sqrt-biased
# distribution, and looked at side by side -- 0.24 uniform fills best; the
# biased one is MORE rim-heavy, not less.
MAX_DEPTH = 0.24

# Newton-ish: a point `d` short of its target level set moves `STEP * d` along
# the gradient, so STEP = 1 would arrive in a single step. Anything less makes
# the remaining distance decay geometrically, at (1 - STEP) per step -- which
# is a collapse that is fast at the start and decelerates as it lands, the same
# shape the diffusion trajectory next door has and for the same reason (that
# one measures ~0.79x per frame; see ui/viewer.py's camera-easing note).
#
# Set against the clock, not by feel: 0.16 was tried first and is 99.5% done
# after 30 steps, so the "few hundred steps" were a one-second snap followed by
# three seconds of a still picture. 0.035 over the 180 steps below is a
# six-second collapse that is still visibly moving in its third second.
STEP = 0.035

# Mark units are ~1, and the viewer clamps its camera at a minimum extent of 5
# (ui/viewer.py's `_MIN_EXTENT`), so a unit-sized mark would be framed as a
# speck in the middle of the screen. This puts it in the same size range as a
# folded protein, which is what the camera is tuned for.
SCALE = 24.0

# How many descent steps before the cloud has settled -- after which the source
# stops entirely and the mark holds until it is dismissed. At the 33 ms cadence
# ui/app.py drives it with, this is six seconds.
#
# Chosen against how CLEANLY it lands rather than by feel, because the decay is
# geometric and the Gaussian's tails are the last to arrive: measured over 3000
# points, 120 steps leaves 91% of them inside the mark and the worst straggler
# 0.10 off the slab, 150 leaves 97%, and 180 leaves 99.2% with the worst at
# 0.005 -- which is where the halo of late arrivals stops being visible.
STEPS = 180

# How many points. The same order as a small protein's atom count, so the
# viewer's own point-size formula produces something that reads at booth
# distance without being retuned -- and enough of them that the mark reads as
# filled rather than as a wire outline (3000 was tried first and is visibly
# thin). Costs ~3.9 ms per descent step on this box, against the 33 ms tick
# that drives it, so the fold streaming in underneath keeps its main loop.
POINTS = 6000


# ── what varies from one run to the next ────────────────────────────────────
#
# The brief: "I'd love to see it transform from the dots into some kind of
# form, even if it isn't the same every time." So the DESTINATION is fixed and
# the JOURNEY is not.
#
# Fixed, deliberately, and asserted by tests rather than left to good
# intentions: the polygons, `_ROW_RISE`, `MAX_DEPTH`, `HALF_THICKNESS`,
# `SCALE`, `POINTS`, `STEPS` and the duration they imply. A visitor who sees
# this twice must see the same mark at the same size for the same six seconds
# -- the thing that varies is how the cloud gets there. That split is what
# lets "it is different every time" be tested from BOTH edges: an
# implementation that ignored the seed fails the first edge, and one that
# returned noise fails the second, and neither test alone can catch both.
#
# Three knobs, in the order a visitor notices them:
#
# 1. the SHAPE of the starting cloud (`SEED_SIGMA_RANGE`, `SEED_ANISOTROPY`)
#    -- a wide flat haze one time, a tall narrow column the next;
# 2. the CURVE of the paths (`SWIRL_RADIANS`) -- the cloud turns as it falls
#    in, by up to ~80 degrees, one way or the other;
# 3. the ORDER of arrival (`STEP_GAIN_RANGE`) -- which points land early and
#    which are still drifting in at the end.
#
# Everything below is derived from ONE integer seed, so a run that looked
# interesting can be replayed exactly by passing that seed back (the worker
# logs it and puts it in the first `egg_frame`).

# The spread of the starting cloud, in mark units, drawn per run. Centred on
# SEED_SIGMA -- the value the geometry above was tuned against -- and narrow
# enough at the bottom end that the first frame is still unmistakably noise.
SEED_SIGMA_RANGE = (1.20, 1.65)

# How anisotropic the starting cloud may be, as the half-width of a uniform
# draw in LOG scale per axis: e^0.35 = 1.42, so one axis may be up to ~2x
# another. The three axis scales are then divided by their own geometric mean,
# so this changes the cloud's SHAPE and never its overall size -- without that
# normalisation a run could draw three large scales at once and start with a
# cloud that needs more than STEPS steps to come in.
SEED_ANISOTROPY = 0.35

# Total in-plane rotation applied to the cloud while it descends, in radians,
# drawn uniformly in +/- this. 1.4 rad is ~80 degrees: enough that two runs
# side by side are obviously not the same descent, small enough that the cloud
# never reads as "spinning" rather than "settling".
#
# Spent over the FIRST `SWIRL_FRACTION` of the steps and then exactly zero --
# see `swirl_schedule`. That hard zero is the whole reason the mark still
# lands: a swirl is a rigid rotation about the mark's centre, and the mark is
# not rotationally symmetric, so a swirl that merely decayed asymptotically
# would still be dragging points off the shape on the last step.
SWIRL_RADIANS = 1.4
SWIRL_FRACTION = 0.45

# Per-point multiplier on STEP, drawn per point. Points with a low gain lag
# behind and arrive last, which is what makes the middle of the descent look
# different from run to run rather than merely differently seeded.
#
# The bottom of the range is the number that decides whether the mark lands
# CLEANLY: a point moving at 0.8 * STEP has (1 - 0.028)^180 = 0.6% of its
# starting distance left at the end, against 0.17% at full step. That is why
# the ramp below exists.
STEP_GAIN_RANGE = (0.80, 1.30)

# Over the last quarter of the descent every point's gain is blended back to
# 1.0, so the run ends on the same schedule it always did no matter what was
# drawn. Without it the slowest points leave a faint halo that IS visible --
# measured at up to 0.06 mark units (3% of the mark's radius) against 0.005
# for the unvaried descent.
GAIN_RAMP_FRACTION = 0.25


def _random_rotation(rng):
    """A uniformly-random proper rotation matrix, via QR of a Gaussian matrix.

    The sign fix is not optional: raw QR gives an ORTHOGONAL matrix, which is
    a rotation half the time and a rotation-plus-reflection the other half.
    A reflection would mirror the starting cloud -- invisible here, since the
    cloud is symmetric noise, but it would also mirror any future use of this
    helper, so it is corrected at the source rather than relied upon not to
    matter.
    """
    q, r = np.linalg.qr(rng.normal(size=(3, 3)))
    q = q * np.sign(np.diag(r))
    if np.linalg.det(q) < 0:
        q[:, 0] = -q[:, 0]
    return q


def fresh_seed():
    """A new 32-bit seed from the OS entropy pool.

    A plain `default_rng()` would also be fresh, but it could not be written
    down. This booth logs the seed and puts it on the wire in every
    `egg_frame`, so a run someone liked can be replayed exactly -- which is
    also how the device implementation is compared against this one.
    """
    return int(np.random.SeedSequence().generate_state(1, dtype=np.uint32)[0])


class RunParameters:
    """Everything one run of the descent draws before it starts.

    Held as plain numpy arrays and floats, computed on the host, and shared
    verbatim by both implementations: `MarkCondensation` below (numpy, the
    fallback) and `runner/egg.py` (ttnn, on the chip). That sharing is the
    point -- it is what makes "the chip computed the same descent the CPU
    would have" a claim a test can check rather than an assertion.
    """

    __slots__ = ("seed", "positions", "depths", "gains", "swirl", "sigma",
                 "axes", "steps", "scale")

    def __init__(self, seed, positions, depths, gains, swirl, sigma, axes,
                 steps, scale):
        self.seed = int(seed)
        self.positions = positions
        self.depths = depths
        self.gains = gains
        self.swirl = float(swirl)
        self.sigma = float(sigma)
        self.axes = axes
        self.steps = int(steps)
        self.scale = float(scale)

    @property
    def count(self):
        return int(self.positions.shape[0])

    def swirl_schedule(self):
        """The in-plane rotation applied at each step, in radians. Length `steps`.

        Weighted as (1 - i/swirl_steps)^2 over the first `SWIRL_FRACTION` of
        the run and EXACTLY zero after it, normalised so the whole schedule
        sums to `self.swirl`. Two properties matter and both are tested:

        * it sums to the drawn total, so `swirl` means what it says;
        * every entry after `swirl_steps` is exactly 0.0, so the descent has
          the rest of the run to settle onto a mark nothing is still turning.
        """
        steps = self.steps
        cut = max(1, int(round(steps * SWIRL_FRACTION)))
        i = np.arange(steps, dtype=np.float64)
        weights = np.where(i < cut, np.square(1.0 - np.minimum(i, cut) / cut), 0.0)
        total = weights.sum()
        if total <= 0.0:
            return np.zeros(steps)
        return self.swirl * weights / total

    def gain_schedule(self):
        """Per-step blend of the per-point gains back towards 1.0. Length `steps`.

        0.0 for most of the run (each point moves at its own drawn gain), then
        a linear ramp to 1.0 across the last `GAIN_RAMP_FRACTION`, so however
        the gains fell out the run finishes on the schedule `STEPS` was chosen
        against. See `STEP_GAIN_RANGE` for the measurement that made this
        necessary.
        """
        steps = self.steps
        start = 1.0 - GAIN_RAMP_FRACTION
        t = np.arange(steps, dtype=np.float64) / max(1, steps - 1)
        return np.clip((t - start) / max(1e-9, GAIN_RAMP_FRACTION), 0.0, 1.0)


def run_parameters(seed=None, count=POINTS, steps=STEPS, scale=SCALE):
    """Draw one run's parameters from one seed. Pure, and cheap.

    `seed=None` means a fresh one from the OS (`fresh_seed`), which is what
    makes the egg different every time it is asked for; the seed actually used
    is recorded on the result either way.
    """
    if seed is None:
        seed = fresh_seed()
    count = int(count)
    rng = np.random.default_rng(int(seed))

    sigma = float(rng.uniform(*SEED_SIGMA_RANGE))
    # Normalised to geometric mean 1 so this changes shape, never size.
    axes = np.exp(rng.uniform(-SEED_ANISOTROPY, SEED_ANISOTROPY, size=3))
    axes = axes / np.exp(np.log(axes).mean())
    rotation = _random_rotation(rng)
    positions = (rng.normal(0.0, 1.0, size=(count, 3)) * (sigma * axes)) @ rotation.T

    # Per-point target depth. Uniform in [0, MAX_DEPTH]: a point whose target
    # is deeper than the mark is thick where it lands simply stops at the
    # deepest place it can reach, which falls out of the descent and needs no
    # special case.
    depths = rng.uniform(0.0, MAX_DEPTH, size=count)
    gains = rng.uniform(*STEP_GAIN_RANGE, size=count)
    swirl = float(rng.uniform(-SWIRL_RADIANS, SWIRL_RADIANS))
    return RunParameters(seed=seed, positions=positions, depths=depths,
                         gains=gains, swirl=swirl, sigma=sigma, axes=axes,
                         steps=steps, scale=scale)


class MarkCondensation:
    """Gaussian noise descending onto the mark, one step per `step()` call.

    The CPU implementation, and the specification `runner/egg.py` reimplements
    in ttnn. Deterministic given `seed`, so a test can assert on where the
    cloud actually ends up rather than merely on the fact that it moved --
    and NON-deterministic without one, which is the feature.

    Holds no GTK and starts no timers: the caller owns the clock. That is what
    lets ui/app.py drive it from a plain `GLib.timeout` on the main loop, and
    lets the tests run it to completion instantly.
    """

    def __init__(self, count=POINTS, seed=None, steps=STEPS, scale=SCALE,
                 params=None):
        self.params = (params if params is not None
                       else run_parameters(seed, count, steps, scale))
        self.seed = self.params.seed
        self.steps = self.params.steps
        self.scale = self.params.scale
        self.completed = 0
        self._points = np.array(self.params.positions, dtype=np.float64,
                                copy=True)
        self._depth = self.params.depths
        self._gain = self.params.gains
        self._swirl = self.params.swirl_schedule()
        self._blend = self.params.gain_schedule()

    @property
    def done(self):
        return self.completed >= self.steps

    def points(self):
        """The cloud as the viewer wants it: (N, 3) float32, at `scale`."""
        return np.ascontiguousarray(self._points * self.scale,
                                    dtype=np.float32)

    def step(self):
        """Advance one gradient step and return the cloud.

        Each point descends on `relu(sdf + depth)` -- how far it still is from
        its own target level set -- so a point that has arrived stops moving
        and the cloud settles instead of oscillating around the surface.

        Then the whole cloud is turned, in plane, by this step's share of the
        run's swirl -- a rigid rotation, so it changes the PATH a point takes
        and not where the path ends. The rotation is exactly zero for the last
        55% of the run (`swirl_schedule`), which is what lets the descent
        finish on a mark nothing is still moving.
        """
        if not self.done:
            i = self.completed
            distance, gradient = slab_sdf_gradient(self._points,
                                                   HALF_THICKNESS)
            excess = np.maximum(distance + self._depth, 0.0)
            blend = self._blend[i]
            gain = self._gain * (1.0 - blend) + blend
            self._points = (self._points
                            - (STEP * gain * excess)[:, None] * gradient)
            angle = self._swirl[i]
            if angle:
                cos, sin = np.cos(angle), np.sin(angle)
                x = self._points[:, 0].copy()
                y = self._points[:, 1]
                self._points[:, 0] = cos * x - sin * y
                self._points[:, 1] = sin * x + cos * y
            self.completed += 1
        return self.points()
