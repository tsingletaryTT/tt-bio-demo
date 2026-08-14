"""The easter egg's descent, computed on a Tenstorrent chip with ttnn.

Why this exists
---------------
`mark.py` pulls a cloud of noise into the Tenstorrent mark by gradient descent
on a signed distance field. It used to run on the host, inside the GTK
process. This module runs **the same descent, on the silicon**, in a worker
that already holds a chip -- because a booth whose every pixel was computed on
the chips in front of you is a better story than one with a CPU-drawn flourish
in it.

**This is not a good use of the hardware and does not pretend to be.** A
Blackhole is a 700-teraflop machine and this is 6,000 points against 17 line
segments; the whole 180-step descent measures under a second, most of which is
kernel dispatch. It is here because the booth's claim is "everything you see
was computed on these chips", and an easter egg that quietly wasn't would be a
small hole in exactly the claim the booth is built on.

What is on the chip and what is not
-----------------------------------
On the chip: every arithmetic operation of the descent -- the seventeen
point-to-segment distances per point per step, the even-odd inside test, the
extrusion field, the gradient, the position update and the swirl. 180 steps of
it, with the cloud living in device memory the whole time and coming back to
the host once per step only to be packed into a frame.

On the host: drawing the run's parameters (`mark.run_parameters` -- one
`default_rng` call, before anything is uploaded) and the per-step scalars its
schedules produce. Those are three floats a step, not a computation.

Two deliberate differences from `mark.py`, both measured
--------------------------------------------------------
1. **float32, not float64.** `mark.py` runs in numpy's float64; this runs in
   the chip's float32. Measured over a full 180-step run, that is a mean
   per-point disagreement of ~1e-3 mark units against a mark 2 units across.
   bfloat16 was tried FIRST and is wrong in a way worth recording: its ulp at
   |x| ~ 1 is 0.008, and the descent's last steps move points by less than
   that, so the cloud STALLS -- 28% of points ended more than 0.02 outside the
   mark, against 0.3% in float64, and the mark rendered with a visible fuzz
   around it. This is that rare case where the cheap dtype does not merely
   lose accuracy, it changes the fixed point.

2. **The nearest edge is chosen with a tolerance, not an argmin.** ttnn's
   `min(dim=)` reduction does not return a value bit-identical to the element
   it reduced (measured: 0.25% low on float32), so the obvious
   `eq(d2, min(d2))` mask selects NOTHING and the gradient comes back as
   0/0. The mask here is `d2 <= 1.02 * min(d2)`, and where more than one edge
   qualifies their offsets are averaged and renormalised -- which is a
   slightly smoothed gradient exactly at the corners, where two edges really
   are equidistant, and identical to the argmin everywhere else.

Nothing in this module opens, closes or otherwise owns a device. It is handed
one that `runner/folder.py` already holds, and it must never be the reason a
chip is not released -- see `runner/worker.py`, which calls it.
"""

import logging
import time

import numpy as np

import mark
from protocol.events import pack_coords

log = logging.getLogger(__name__)

# The tile width every (N, edges) tensor is padded out to. ttnn's tile is
# 32x32, and the mark has 17 edges -- so rather than rely on what a reduction
# does with a tile's padding lanes (which is a property of the op, not of the
# protocol, and would be a silent wrong answer if it ever changed), the edge
# tables below are padded to exactly 32 with DUMMY edges that are far away
# from everything and contribute no crossings. See `_edge_tables`.
_EDGE_LANES = 32

# Where the dummy edges sit, in mark units. Far enough that their squared
# distance (~1e8) can never be the minimum, near enough that squaring it stays
# comfortably inside float32.
_DUMMY_Y = 1.0e4

# How much further than the nearest edge another edge may be and still count
# as "nearest" for the gradient. See the module docstring: this is not a
# tuning knob, it is the tolerance that makes the mask work at all against a
# reduction whose result is not bit-exact. 2% in squared distance is 1% in
# distance.
_NEAREST_TOLERANCE = 1.02

# Guards a divide-by-zero for a point sitting exactly on an edge (distance 0,
# so no gradient direction) and for a fully-inside point whose outer distance
# is 0. Both are real: the second happens to most of the cloud by the end.
_EPS = 1.0e-9

# How many descent steps go into one `egg_frame`. 1 means every step is a
# frame -- 180 frames of 6,000 points is 17 MB across the socket, which
# measured well under a second on this box and is bounded by
# `EGG_EMIT_GAP_S` below so it cannot monopolise the broadcast lock.
EGG_FRAME_EVERY = 1

# The minimum gap between two `egg_frame` broadcasts. Not pacing for the
# screen -- the UI buffers these and plays them on its own clock, which is the
# whole reason the animation does not stutter when the chip is fast. This is
# purely so a chip that produces 180 frames in half a second does not hand
# `EventServer.broadcast` 17 MB in one go while a fold's own frames are trying
# to get out. At 5 ms the whole egg takes ~0.9 s of socket time.
EGG_EMIT_GAP_S = 0.005


def _edge_tables(polygons, lanes=_EDGE_LANES):
    """Flatten the mark's polygons into one padded row of edges.

    Returns `(vx, vy, ex, ey, next_y, inv_len2)`, each a (1, lanes) float32
    row: the start vertex of each edge, the edge vector, the y of the NEXT
    vertex (which the even-odd crossing test needs) and 1/|edge|^2.

    **All three faces go into one flat table**, which is not an optimisation
    but a correctness argument worth stating. `mark.mark_sdf_gradient` takes
    the minimum over the three pieces' SIGNED distances. That equals "unsigned
    distance to the nearest of all 17 edges, made negative if the point is
    inside any piece", because (a) the pieces touch but never overlap, so a
    point is inside at most one, and (b) a segment from inside one piece to
    any point on another piece's boundary must cross the first piece's own
    boundary, so the nearest edge overall is always the nearest edge of the
    piece you are in. And because a point is inside at most one piece, the
    per-piece even-odd parities sum to a parity over ALL the edges -- so one
    reduction answers "inside?" for the whole mark, with no per-piece
    bookkeeping on the device at all.

    The padding lanes are degenerate edges parked at y = `_DUMMY_Y`: their
    squared distance is ~1e8 (never the minimum), their `inv_len2` is 0 (so
    the projection clamps to the start vertex rather than dividing by zero),
    and the crossing test's two branches are `above=0, below=1` -- which makes
    both the all-three and none-of-three terms zero, so they contribute
    exactly no crossings.
    """
    vx, vy, ex, ey, ny = [], [], [], [], []
    for poly in polygons:
        verts = np.asarray(poly, dtype=np.float64)
        edges = np.roll(verts, -1, axis=0) - verts
        vx.extend(verts[:, 0])
        vy.extend(verts[:, 1])
        ex.extend(edges[:, 0])
        ey.extend(edges[:, 1])
        ny.extend(np.roll(verts[:, 1], -1))
    used = len(vx)
    if used > lanes:
        raise ValueError(f"the mark has {used} edges; only {lanes} lanes")
    pad = lanes - used
    vx += [0.0] * pad
    vy += [_DUMMY_Y] * pad
    ex += [0.0] * pad
    ey += [0.0] * pad
    ny += [_DUMMY_Y] * pad
    length2 = np.square(ex) + np.square(ey)
    inv = np.where(length2 > 0.0, 1.0 / np.where(length2 > 0.0, length2, 1.0), 0.0)
    rows = [np.asarray(a, dtype=np.float32).reshape(1, lanes)
            for a in (vx, vy, ex, ey, ny, inv)]
    return tuple(rows)


class DeviceCondensation:
    """`mark.MarkCondensation`, step for step, in ttnn on one chip.

    Same `RunParameters`, same schedules, same law -- so a test can run both
    and compare, which is the only way "the chip computed the mark" is a
    checkable claim rather than a hopeful one.

    `ttnn` and `torch` are imported inside `__init__`, not at module scope, so
    that importing this module costs a unit test nothing. Every other module
    on the runner side that touches tt-bio does the same.
    """

    def __init__(self, device, params):
        import torch
        import ttnn

        self._ttnn = ttnn
        self._torch = torch
        self.params = params
        self.steps = params.steps
        self.scale = params.scale
        self.count = params.count
        self.completed = 0
        self._swirl = params.swirl_schedule()
        self._blend = params.gain_schedule()

        def row(a):
            return ttnn.from_torch(torch.from_numpy(np.ascontiguousarray(a)),
                                   dtype=ttnn.float32,
                                   layout=ttnn.TILE_LAYOUT, device=device)

        def col(a):
            arr = np.ascontiguousarray(np.asarray(a, dtype=np.float32)
                                       .reshape(-1, 1))
            return ttnn.from_torch(torch.from_numpy(arr), dtype=ttnn.float32,
                                   layout=ttnn.TILE_LAYOUT, device=device)

        (self._vx, self._vy, self._ex, self._ey,
         self._ny, self._inv) = (row(t) for t in _edge_tables(mark.mark_polygons()))
        start = np.asarray(params.positions, dtype=np.float32)
        self._px = col(start[:, 0])
        self._py = col(start[:, 1])
        self._pz = col(start[:, 2])
        self._depth = col(params.depths)
        self._gain = col(params.gains)

    @property
    def done(self):
        return self.completed >= self.steps

    def points(self):
        """The cloud as the wire wants it: (N, 3) float32, at `scale`.

        One device-to-host copy of three columns, 72 KB. Deliberately the only
        readback in the loop: everything else stays resident, so "computed on
        the chip" is a statement about the whole descent and not just about
        one operation in it.
        """
        ttnn = self._ttnn
        cols = [ttnn.to_torch(t).float().numpy().reshape(-1)[:self.count]
                for t in (self._px, self._py, self._pz)]
        return np.ascontiguousarray(np.stack(cols, axis=1) * self.scale,
                                    dtype=np.float32)

    # -- the arithmetic ----------------------------------------------------

    def _one_minus(self, t):
        """1 - t for a 0/1 mask. ttnn has no scalar-minus-tensor overload."""
        ttnn = self._ttnn
        return ttnn.add(ttnn.multiply(t, -1.0), 1.0)

    def step(self):
        """Advance one gradient step on the device. Returns nothing.

        Mirrors `mark.MarkCondensation.step` line for line; read that one
        first. The differences are all mechanical: no fancy indexing (a mask
        and two reductions stand in for `argmin` plus a gather), no
        `np.where` on a boolean array (a 0/1 mask multiplied through instead),
        and `1 - x` spelled out because the binding has no reflected scalar
        overload.
        """
        if self.done:
            return
        ttnn = self._ttnn
        i = self.completed
        px, py, pz = self._px, self._py, self._pz

        # -- the in-plane polygon field, all 17 edges at once ---------------
        relx = ttnn.subtract(px, self._vx)                 # (N, lanes)
        rely = ttnn.subtract(py, self._vy)
        dot = ttnn.add(ttnn.multiply(relx, self._ex),
                       ttnn.multiply(rely, self._ey))
        along = ttnn.clamp(ttnn.multiply(dot, self._inv), 0.0, 1.0)
        offx = ttnn.subtract(relx, ttnn.multiply(along, self._ex))
        offy = ttnn.subtract(rely, ttnn.multiply(along, self._ey))
        dist2 = ttnn.add(ttnn.multiply(offx, offx), ttnn.multiply(offy, offy))

        # The nearest edge, by tolerance rather than by argmin -- see the
        # module docstring for why an exact-equality mask returns nothing.
        floor = ttnn.min(dist2, dim=1, keepdim=True)
        near = ttnn.add(ttnn.multiply(floor, _NEAREST_TOLERANCE), _EPS)
        mask = ttnn.le(dist2, near)
        # At least one lane always qualifies (the minimum itself does), so
        # this clamp is belt and braces against a 0/0 rather than a real case
        # -- but a NaN here would spread through the whole cloud in one step.
        count = ttnn.clamp(ttnn.sum(mask, dim=1, keepdim=True),
                           1.0, float(_EDGE_LANES))
        # The distance is taken from the SELECTED elements, not from the
        # reduction's own output, for the same bit-exactness reason.
        chosen2 = ttnn.div(ttnn.sum(ttnn.multiply(mask, dist2), dim=1,
                                    keepdim=True), count)
        distance = ttnn.sqrt(ttnn.add(chosen2, _EPS))
        gx = ttnn.div(ttnn.sum(ttnn.multiply(mask, offx), dim=1, keepdim=True),
                      count)
        gy = ttnn.div(ttnn.sum(ttnn.multiply(mask, offy), dim=1, keepdim=True),
                      count)
        glen = ttnn.sqrt(ttnn.add(ttnn.add(ttnn.multiply(gx, gx),
                                           ttnn.multiply(gy, gy)), _EPS))
        ux = ttnn.div(gx, glen)
        uy = ttnn.div(gy, glen)

        # -- inside or outside: one even-odd parity over every edge ---------
        above = ttnn.ge(py, self._vy)
        below = ttnn.lt(py, self._ny)
        left = ttnn.gt(ttnn.multiply(self._ex, rely),
                       ttnn.multiply(self._ey, relx))
        both = ttnn.multiply(ttnn.multiply(above, below), left)
        neither = ttnn.multiply(
            ttnn.multiply(self._one_minus(above), self._one_minus(below)),
            self._one_minus(left))
        crossings = ttnn.sum(ttnn.add(both, neither), dim=1, keepdim=True)
        parity = ttnn.subtract(
            crossings, ttnn.multiply(ttnn.floor(ttnn.multiply(crossings, 0.5)),
                                     2.0))
        sign = ttnn.add(ttnn.multiply(parity, -2.0), 1.0)   # +1 out, -1 in

        plane = ttnn.multiply(sign, distance)
        pgx = ttnn.multiply(sign, ux)
        pgy = ttnn.multiply(sign, uy)

        # -- the extrusion: the mark as a thin slab -------------------------
        face = ttnn.subtract(ttnn.abs(pz), mark.HALF_THICKNESS)
        zdir = ttnn.add(ttnn.multiply(ttnn.ge(pz, 0.0), 2.0), -1.0)
        out_plane = ttnn.relu(plane)
        out_face = ttnn.relu(face)
        # `outer2` is the raw sum of squares and `outer` the epsilon-guarded
        # root. They are kept apart on purpose: `is_out` has to be decided on
        # the RAW value, because the guard makes the root strictly positive
        # and a test against it would say "outside" for every point in the
        # cloud -- including the ones that have already landed.
        outer2 = ttnn.add(ttnn.multiply(out_plane, out_plane),
                          ttnn.multiply(out_face, out_face))
        outer = ttnn.sqrt(ttnn.add(outer2, _EPS))
        # min(max(plane, face), 0), spelled with relu because ttnn.min is a
        # reduction and there is no elementwise "min against a scalar".
        deepest = ttnn.maximum(plane, face)
        inner = ttnn.multiply(ttnn.relu(ttnn.multiply(deepest, -1.0)), -1.0)
        total = ttnn.add(inner, outer)

        is_out = ttnn.gt(outer2, 0.0)
        by_plane = ttnn.ge(plane, face)
        share_plane = ttnn.div(out_plane, outer)
        share_face = ttnn.div(out_face, outer)
        in_mask = self._one_minus(is_out)
        gX = ttnn.add(ttnn.multiply(is_out, ttnn.multiply(pgx, share_plane)),
                      ttnn.multiply(in_mask, ttnn.multiply(pgx, by_plane)))
        gY = ttnn.add(ttnn.multiply(is_out, ttnn.multiply(pgy, share_plane)),
                      ttnn.multiply(in_mask, ttnn.multiply(pgy, by_plane)))
        gZ = ttnn.add(ttnn.multiply(is_out, ttnn.multiply(zdir, share_face)),
                      ttnn.multiply(in_mask,
                                    ttnn.multiply(zdir,
                                                  self._one_minus(by_plane))))

        # -- the step, and this run's own share of the swirl ----------------
        blend = float(self._blend[i])
        gain = ttnn.add(ttnn.multiply(self._gain, 1.0 - blend), blend)
        excess = ttnn.relu(ttnn.add(total, self._depth))
        move = ttnn.multiply(ttnn.multiply(excess, gain), mark.STEP)
        nx = ttnn.subtract(px, ttnn.multiply(move, gX))
        ny_ = ttnn.subtract(py, ttnn.multiply(move, gY))
        nz = ttnn.subtract(pz, ttnn.multiply(move, gZ))

        angle = float(self._swirl[i])
        if angle:
            cos, sin = float(np.cos(angle)), float(np.sin(angle))
            rx = ttnn.subtract(ttnn.multiply(nx, cos), ttnn.multiply(ny_, sin))
            ry = ttnn.add(ttnn.multiply(nx, sin), ttnn.multiply(ny_, cos))
            nx, ny_ = rx, ry

        self._px, self._py, self._pz = nx, ny_, nz
        self.completed += 1


def egg_frame_event(egg_id, step, total, coords, *, card, seed):
    """Build one `egg_frame`.

    Shaped like `runner/shaping.py`'s `frame_event` and deliberately NOT that
    event: a `frame` carries a `job_id` the UI routes into a fold's slot, and
    an egg has no job. `card` and `seed` ride along because the screen says
    which chip computed this (which it may only say if it is true) and the log
    needs the seed to replay a run.
    """
    arr = np.asarray(coords, dtype=np.float32).reshape(-1, 3)
    return {
        "type": "egg_frame",
        "egg_id": egg_id,
        "card": card,
        "seed": int(seed),
        "step": int(step),
        "total": int(total),
        "n_points": int(arr.shape[0]),
        "coords_b64": pack_coords(arr),
    }


def run_egg(device, emit, *, egg_id, card, seed=None, count=mark.POINTS,
            steps=mark.STEPS, gap_s=EGG_EMIT_GAP_S, clock=time.monotonic,
            sleep=time.sleep):
    """Run one whole descent on `device`, emitting `egg_frame` events.

    Returns the seed actually used. Raises whatever ttnn raises -- the caller
    (`runner/worker.py`) is the one place that decides what a failed egg looks
    like on the wire, exactly as it is for a failed fold.

    The frames are emitted as fast as the chip produces them, floored at
    `gap_s` apart. That is a deliberate non-decision about pacing: the UI
    buffers these and plays them on its own 33 ms clock, so how fast they
    arrive changes nothing on screen. What it does change is how long this
    worker holds a chip -- and the answer, measured, is about a second and a
    half for a six-second animation, which is the whole argument for computing
    the trajectory up front instead of in real time.
    """
    params = mark.run_parameters(seed, count=count, steps=steps)
    run = DeviceCondensation(device, params)
    log.info("egg %s: %d points, %d steps on card %s (seed %d, swirl %+.2f)",
             egg_id, params.count, params.steps, card, params.seed,
             params.swirl)
    t0 = clock()
    # Step 0 is the untouched noise draw, so a visitor sees the cloud the chip
    # actually started from rather than a first frame that has already moved.
    emit(egg_frame_event(egg_id, 0, params.steps, run.points(),
                         card=card, seed=params.seed))
    last = clock()
    for index in range(1, params.steps + 1):
        run.step()
        if index % EGG_FRAME_EVERY and index != params.steps:
            continue
        frame = egg_frame_event(egg_id, index, params.steps, run.points(),
                                card=card, seed=params.seed)
        wait = gap_s - (clock() - last)
        if wait > 0:
            sleep(wait)
        emit(frame)
        last = clock()
    log.info("egg %s: finished on card %s in %.2fs", egg_id, card, clock() - t0)
    return params.seed
