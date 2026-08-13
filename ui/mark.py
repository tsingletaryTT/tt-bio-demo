r"""The Tenstorrent mark, computed rather than blitted.

What this is
------------
A signed distance field for the Tenstorrent mark, and a gradient descent that
pulls a cloud of Gaussian noise into it. `ui/app.py` renders the result through
the same `StructureViewer.set_points` the diffusion trajectory uses, which is
the whole point: it is the same noise-becomes-structure motion, on the same
widget, driven by real arithmetic rather than by a picture of one.

**It is an easter egg, and it is not a fold.** Nothing here is chemistry,
nothing here came off the socket, and nothing here ran on a Tenstorrent chip.
The booth says exactly that on screen (see `_EGG_*` in ui/app.py), because this
project's credibility rests on a visitor being able to trust that the protein
they are looking at is the protein the chips folded. An easter egg that could
be mistaken for a structure would spend that trust for a joke.

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


class MarkCondensation:
    """Gaussian noise descending onto the mark, one step per `step()` call.

    Deterministic given `seed`, so a test can assert on where the cloud
    actually ends up rather than merely on the fact that it moved.

    Holds no GTK and starts no timers: the caller owns the clock. That is what
    lets ui/app.py drive it from a plain `GLib.timeout` on the main loop, and
    lets the tests run it to completion instantly.
    """

    def __init__(self, count=POINTS, seed=20260813, steps=STEPS, scale=SCALE):
        self.steps = int(steps)
        self.scale = float(scale)
        self.completed = 0
        rng = np.random.default_rng(seed)
        self._points = rng.normal(0.0, SEED_SIGMA, size=(int(count), 3))
        # Per-point target depth. Uniform in [0, MAX_DEPTH]: a point whose
        # target is deeper than the mark is thick where it lands simply stops
        # at the deepest place it can reach, which falls out of the descent
        # and needs no special case.
        self._depth = rng.uniform(0.0, MAX_DEPTH, size=int(count))

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
        """
        if not self.done:
            distance, gradient = slab_sdf_gradient(self._points,
                                                   HALF_THICKNESS)
            excess = np.maximum(distance + self._depth, 0.0)
            self._points = self._points - STEP * excess[:, None] * gradient
            self.completed += 1
        return self.points()
