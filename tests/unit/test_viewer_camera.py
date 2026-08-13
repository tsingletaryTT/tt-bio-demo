"""Pins the camera-framing behaviour of StructureViewer.

Two defects live here, both measured against the real daemon before this
file existed (a `_extent` probe logged once a second across an attract
loop), and both reproduced below against the REAL recorded trajectory in
tests/fixtures/streams/real_fold_trpcage.jsonl rather than against invented
numbers:

  1. An incoming diffusion frame re-framed the camera even while a FINISHED
     RIBBON was the only thing on screen. ui/app.py builds the ribbon on a
     worker thread, so `job_done` for fold N routinely lands after
     `job_start` for fold N+1 -- meaning fold N+1's initial noise cloud
     (spread ~7400 A) arrives while fold N's ribbon (spread ~9.7 A) is the
     hero image. The observed result: `extent=10.42 ribbons=9120 pts=0`
     followed one second later by `extent=1737.75 ribbons=9120 pts=0` --
     the finished protein shrinking to a dot, once per cycle.

  2. The convergence ease (`_extent*0.8 + spread*0.2`, linear) was far too
     slow for data that contracts geometrically by ~1000x over ~30 frames.
     Measured over the recorded trajectory it left the structure filling a
     median 15% of the frame, and ended the fold with the camera at 43.9
     against a true spread of 10.0.

As in test_viewer_animation.py, constructing a real StructureViewer needs a
live display and GL context, so the real, unmodified methods are bound to a
duck-typed stand-in that carries only the plain attributes they touch. The
methods under test run their actual production bodies; nothing here is a
reimplementation.
"""
import json
import pathlib

import numpy as np
import pytest

from protocol.events import unpack_coords
from ui.viewer import StructureViewer

_FIXTURE = (pathlib.Path(__file__).resolve().parents[1]
            / "fixtures" / "streams" / "real_fold_trpcage.jsonl")


def real_frames():
    """The 30 recorded diffusion frames of a real Trp-cage fold on silicon.

    Real coordinates, not a synthetic geometric sequence: they carry the
    actual per-frame noise, the actual flyer atoms, and the actual
    non-monotonic wobble near the end (frames 22-29 hover around 10-12 A
    and occasionally grow), all of which a hand-rolled `spread * 0.8**t`
    sequence would quietly smooth away -- and smoothing exactly those away
    is what would let a too-eager or too-timid camera look fine here and
    still be wrong on the booth floor.
    """
    frames = []
    for line in _FIXTURE.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event["type"] == "frame":
            frames.append(unpack_coords(event["coords_b64"]))
    return frames


def true_spread(coords):
    """The honest bounding radius of a frame: what MUST fit on screen.

    Deliberately the plain max, never the viewer's own robust statistic --
    a test that scored the camera against the same percentile the camera
    optimises would be scoring it against its own opinion and would accept
    any framing at all as long as it was self-consistent.
    """
    return float(np.abs(coords - coords.mean(axis=0)).max())


def ribbon_like_mesh(radius=9.70, n=400, seed=0):
    """Stand-in for a finished ribbon: a compact shell of `radius` A.

    9.70 A is the measured `_extent` of the real Trp-cage ribbon mesh, so
    the numbers in these tests are the numbers from the booth.

    Built as a point-symmetric shell (every direction paired with its
    negation) so the mesh's mean is exactly the origin and its Chebyshev
    radius is exactly `radius` -- which lets the assertions below be exact
    equalities instead of loose bands that would also accept a camera off
    by a few percent for the wrong reason.
    """
    rng = np.random.default_rng(seed)
    directions = rng.normal(size=(n // 2, 3))
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)
    # Pin three of them to the axes so the extreme really is `radius`.
    directions[:3] = np.eye(3)
    shell = np.vstack([directions, -directions]) * radius
    return shell.astype(np.float32)


class _FakeViewer:
    """Duck-typed stand-in for StructureViewer -- see module docstring."""

    # `_spread` is a @staticmethod on the real class; re-wrapping it here is
    # load-bearing. Assigned bare, Python would rebind the plain function as
    # an instance method of this class and silently pass `self` as `coords`.
    _spread = staticmethod(StructureViewer._spread)
    _frame_camera = StructureViewer._frame_camera
    set_points = StructureViewer.set_points
    set_ribbon = StructureViewer.set_ribbon
    clear_structure = StructureViewer.clear_structure
    set_blend = StructureViewer.set_blend
    begin_crossfade = StructureViewer.begin_crossfade

    def __init__(self):
        self._center = np.zeros(3, dtype=np.float32)
        self._extent = 20.0
        self._camera_framed = False
        self._camera_subject = "points"
        self._pending_points = None
        self._pending_ribbon = None
        self._point_opacity = 1.0
        self._point_count = 0
        self._ribbon_index_count = 0
        self._blend = 0.0
        self._blend_target = 0.0
        self.renders = 0

    def queue_render(self):
        self.renders += 1

    def show_ribbon(self, verts):
        """The exact pair of calls ui/app.py's _drain_pending_ribbon makes."""
        n = len(verts)
        self.set_ribbon(verts, verts, verts, np.arange(n, dtype=np.uint32))
        self.begin_crossfade()


# ── bug 1: a displayed ribbon owns the camera ────────────────────────────


def test_noise_frames_cannot_rescale_a_displayed_ribbon():
    """MUTATION CAUGHT: dropping the `_camera_subject` guard in set_points
    (i.e. calling `_frame_camera(arr)` unconditionally, as shipped).

    This is the production ordering, not a contrived one: clear_structure()
    for fold N+1, then fold N's late ribbon, then fold N+1's noise frames.
    """
    viewer = _FakeViewer()
    viewer.clear_structure()          # job_start for the next fold
    viewer.show_ribbon(ribbon_like_mesh())

    framed_extent = viewer._extent
    framed_center = viewer._center.copy()
    assert framed_extent == pytest.approx(9.70, rel=1e-5)

    frames = real_frames()[:10]       # the next fold's initial noise cloud
    # The input has to be able to move the camera, or this test proves
    # nothing: the first of these frames is ~1000x the ribbon's size.
    assert true_spread(frames[0]) > 100.0 * framed_extent

    for frame in frames:
        viewer.set_points(frame)
        # Exact, not "close to": nothing may nudge the hero image's framing
        # at all while it is the thing being displayed.
        assert viewer._extent == framed_extent
        assert np.array_equal(viewer._center, framed_center)

    # ...and the frames must still have been UPLOADED. A "fix" that simply
    # ignored point frames while a ribbon exists would pass every assertion
    # above and leave the cross-fade with nothing to fade out of.
    assert viewer._pending_points is not None
    assert np.array_equal(viewer._pending_points, frames[-1])


def test_the_next_job_start_hands_the_camera_back_to_the_point_cloud():
    """MUTATION CAUGHT: omitting the `_camera_subject` reset in
    clear_structure() -- which would leave the camera welded to a ribbon
    that has been dropped, so every subsequent fold would render its noise
    cloud at the previous structure's scale, forever."""
    viewer = _FakeViewer()
    viewer.show_ribbon(ribbon_like_mesh())
    assert viewer._extent == pytest.approx(9.70, rel=1e-6)

    viewer.clear_structure()          # job_start
    first = real_frames()[0]
    viewer.set_points(first)

    # The first frame of a new job snaps (there is no earlier frame of this
    # job to ease from), so the camera must now be sized to the noise cloud
    # -- thousands of angstroms -- and not still to the discarded ribbon.
    assert viewer._extent > 1000.0
    assert viewer._extent == pytest.approx(
        max(StructureViewer._spread(first, first.mean(axis=0), True), 5.0))


def test_set_blend_back_to_points_only_releases_the_camera():
    """MUTATION CAUGHT: making the ribbon's camera ownership permanent
    (releasing it only in clear_structure). set_blend(0) is a caller
    declaring that the ribbon is not on screen, so the invariant "the
    camera frames what is displayed" requires handing it back."""
    viewer = _FakeViewer()
    viewer.show_ribbon(ribbon_like_mesh(radius=40.0))
    assert viewer._extent == pytest.approx(40.0, rel=1e-5)

    viewer.set_blend(0.0)
    # A much smaller cloud, so the (fast) inward ease makes the release
    # unmistakable: still locked to the ribbon, this would read exactly
    # 40.0; released, it eases to ~18.
    viewer.set_points(_ball(6.0, seed=7))
    assert viewer._extent < 25.0


# ── bug 2: convergence during diffusion ──────────────────────────────────
#
# The two tests below are a matched pair and must be read together: one
# caps how far the camera may lag (or the protein is a postage stamp), the
# other requires that it lags at all (or the fold shows no contraction).
# Either one alone is satisfiable by a camera that is obviously wrong.

# Camera distance is _extent * 2.6 and the visible half-height at that
# distance is _extent * 2.6 * tan(22.5 deg) = _extent * 1.077, so a
# structure whose true spread equals _extent fills ~93% of the frame
# height. A factor of 2.0 therefore means "fills at least ~46% of the
# height" -- still unmistakably the subject of the shot, and the loosest
# framing worth calling acceptable on a booth screen viewed from a few
# metres away.
_MAX_LAG_FACTOR = 2.0


def test_camera_stays_within_a_factor_of_two_through_a_real_fold():
    """MUTATION CAUGHT: the shipped linear ease
    `_extent = max(_extent*0.8 + spread*0.2, 5.0)`.

    Measured, that ease holds only 4 of these 30 frames inside the factor
    (median lag 6.7x, worst 20x) and finishes the fold at 43.9 against a
    true spread of 10.0 -- the postage stamp this whole file is about.
    """
    viewer = _FakeViewer()
    frames = real_frames()
    lags = []
    for frame in frames:
        viewer.set_points(frame)
        lags.append(viewer._extent / true_spread(frame))

    within = sum(1 for lag in lags if lag <= _MAX_LAG_FACTOR)
    assert within >= 0.9 * len(frames), (
        f"only {within}/{len(frames)} frames framed within "
        f"{_MAX_LAG_FACTOR}x; worst lag {max(lags):.2f}x")

    # And it must ARRIVE, not just spend the fold roughly in the area: by
    # the last frame the structure has settled, and a camera still zoomed
    # out there would hand a mis-scaled view straight to the ribbon reveal.
    assert viewer._extent == pytest.approx(true_spread(frames[-1]), rel=0.25)


def test_the_contraction_is_still_visible_as_a_contraction():
    """MUTATION CAUGHT: easing so hard it becomes tracking (_EASE_IN -> 1.0,
    or reverting _frame_camera to snap on every frame).

    A camera that tracks the cloud exactly renders it at the same size in
    every frame: the visitor sees a blob change texture and never sees the
    1000x collapse that is the entire point of the demo. So the camera is
    REQUIRED to lag during the fast phase. Under a tracking mutation the
    lag is pinned at the spread statistic's own headroom (<= 1.2x) and this
    fails; under the shipped ease it reaches 1.93x.
    """
    viewer = _FakeViewer()
    frames = real_frames()
    lags = []
    for frame in frames:
        viewer.set_points(frame)
        lags.append(viewer._extent / true_spread(frame))

    # Not "at least one frame" -- a single lagging frame could be noise.
    # The lag has to be a sustained property of the collapse phase.
    lagging = sum(1 for lag in lags if lag >= 1.4)
    assert lagging >= 0.3 * len(frames), (
        f"only {lagging}/{len(frames)} frames show the camera trailing the "
        f"collapse; max lag {max(lags):.2f}x -- the fold will read as a "
        f"static blob")


def test_zooming_out_is_far_more_reluctant_than_zooming_in():
    """MUTATION CAUGHT: making the ease symmetric (_EASE_OUT = _EASE_IN).

    Within a job the cloud only contracts, so an outward step is almost
    always a bad frame; a symmetric ease lets one such frame throw the
    camera out by ~6x and cost several frames to recover.
    """
    # Radii chosen so every target here stays well clear of the 5.0 floor:
    # a clamped target would make this test measure the clamp, not the ease.
    viewer = _FakeViewer()
    viewer.set_points(_ball(5000.0, seed=1))      # snaps
    settled = viewer._extent

    viewer.set_points(_ball(500000.0, seed=2))    # a 100x outward jump
    assert viewer._extent < 1.5 * settled

    viewer = _FakeViewer()
    viewer.set_points(_ball(5000.0, seed=1))
    viewer.set_points(_ball(50.0, seed=2))        # a 100x inward jump
    # Inward, the same single frame must buy real ground -- at least a 4x
    # move -- or the camera can never keep up with a geometric collapse.
    assert viewer._extent < settled / 4.0


# ── the spread statistic ─────────────────────────────────────────────────


def _ball(radius, n=200, seed=0):
    """A dense, isotropic cloud of the given Chebyshev radius."""
    rng = np.random.default_rng(seed)
    pts = rng.normal(size=(n, 3))
    pts /= np.abs(pts).max()
    return (pts * radius).astype(np.float32)


def test_one_flyer_atom_does_not_shrink_the_structure_to_nothing():
    """MUTATION CAUGHT: `robust=False` for point frames (i.e. the original
    `np.abs(coords - center).max()`), which lets a single stray atom set
    the zoom for the whole frame."""
    cloud = _ball(10.0, n=500, seed=3)
    with_flyer = np.vstack([cloud, np.array([[900.0, 0.0, 0.0]],
                                            dtype=np.float32)])

    viewer = _FakeViewer()
    viewer.set_points(with_flyer)

    # The bulk of the structure is ~10 A across; the camera must frame that,
    # not the one atom 900 A away. (`max` gives 898.2 here -- measured.)
    assert viewer._extent < 15.0

    # Guard against the opposite over-correction -- a statistic so
    # aggressive it clips the real structure away. 500 of the 501 atoms are
    # genuine, and the frame must still be built around them (measured:
    # 9.34 against a genuine 10.07, so the outermost few percent sit just
    # off the edge and everything else is comfortably inside).
    assert viewer._extent >= 8.0


def test_the_finished_ribbon_is_framed_to_its_furthest_vertex():
    """MUTATION CAUGHT: using the robust percentile for the ribbon too
    (dropping `robust=False` in set_ribbon), which would clip ~4% of the
    hero image off the edges of the screen.

    Uses a mesh with a genuine protruding feature -- a long helix sticking
    out of a compact core is exactly the shape a real ribbon has, and it is
    precisely the part a percentile discards.
    """
    core = ribbon_like_mesh(radius=9.70, n=400)
    protrusion = np.array([[0.0, 0.0, 22.0]], dtype=np.float32)
    verts = np.vstack([core, protrusion])

    viewer = _FakeViewer()
    viewer.show_ribbon(verts)

    # Exactly the furthest vertex: nothing of the hero image off-screen.
    assert viewer._extent == pytest.approx(true_spread(verts), rel=1e-5)
    # And discriminating: the percentile statistic would frame this at
    # ~11-12 A, throwing the protrusion clean off the edge of the screen.
    assert viewer._extent > 20.0


# ── properties this change must not regress ──────────────────────────────


def test_first_frame_snaps_instead_of_easing_from_the_placeholder():
    """MUTATION CAUGHT: removing the `not self._camera_framed` snap, which
    would frame the very first real frame of a session against the
    constructor's placeholder _extent = 20.0."""
    viewer = _FakeViewer()
    assert viewer._extent == 20.0
    first = real_frames()[0]
    viewer.set_points(first)
    # Nowhere near 20.0, and nowhere near an 80/20 blend with it either.
    assert viewer._extent > 1000.0


def test_set_ribbon_snaps_rather_than_easing():
    """MUTATION CAUGHT: reusing the streaming ease for the ribbon (dropping
    `snap=True`), which would leave the finished structure -- the thing
    that stays on screen for the rest of the attract cycle -- permanently
    framed against a blend of the last noise frame's extent and its own."""
    viewer = _FakeViewer()
    for frame in real_frames()[:3]:
        viewer.set_points(frame)
    assert viewer._extent > 1000.0

    viewer.show_ribbon(ribbon_like_mesh(radius=9.70))
    assert viewer._extent == pytest.approx(9.70, rel=1e-6)


def test_camera_never_zooms_closer_than_the_minimum_extent():
    """MUTATION CAUGHT: dropping the 5.0 floor, letting a tiny or
    degenerate (all-atoms-coincident) frame put the near plane inside the
    structure."""
    viewer = _FakeViewer()
    viewer.set_points(np.zeros((32, 3), dtype=np.float32))
    assert viewer._extent == pytest.approx(5.0)

    viewer.set_points(_ball(0.2, seed=5))
    assert viewer._extent >= 5.0


def test_empty_frame_leaves_the_camera_alone():
    """MUTATION CAUGHT: removing the `len(coords) == 0` guard -- an empty
    frame would make `coords.mean()` NaN and poison the camera permanently."""
    viewer = _FakeViewer()
    viewer.set_points(_ball(40.0, seed=6))
    before = viewer._extent
    viewer.set_points(np.zeros((0, 3), dtype=np.float32))
    assert viewer._extent == before
    assert not np.isnan(viewer._center).any()


# ---------------------------------------------------------------------------
# A GL context loss must hand the camera back.
#
# Whole-branch review, Important 5: `_on_unrealize` zeroed the ribbon buffers
# but left `_camera_subject == "ribbon"`. `set_points` only re-frames the
# camera while the subject is the point cloud, so after a context
# recreation NOTHING could ever re-frame it again -- the next fold's
# diffusion cloud (spread ~7400 A) would be drawn at the vanished ribbon's
# ~9.7 A extent: defect 1 in this module's docstring, reintroduced by a
# route the fix did not cover.
# ---------------------------------------------------------------------------

class _NoGL:
    """Every `GL.gl*` call `_on_unrealize` makes, as no-ops. Attribute
    access returns a callable for any name, so this cannot silently miss a
    call the real body makes (it would just do nothing), and no GL context
    is needed to run the real method body."""

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _RealizableViewer(_FakeViewer):
    """`_FakeViewer` plus the handful of attributes and methods the real
    `_on_unrealize` touches, so its ACTUAL body can be run headless."""

    _on_unrealize = StructureViewer._on_unrealize

    def __init__(self):
        super().__init__()
        self.animation_stopped = False
        self.made_current = False
        self._ready = True
        self._point_program = 1
        self._ribbon_program = 2
        self._point_vao = 3
        self._point_vbo = 4
        self._ribbon_vao = 5
        self._ribbon_buffers = [6, 7, 8]

    def stop_animation(self):
        self.animation_stopped = True

    def make_current(self):
        self.made_current = True


def test_losing_the_gl_context_hands_the_camera_back_to_the_points(monkeypatch):
    """The behavioural pin, not a field check: after unrealize, an incoming
    diffusion frame must be able to re-frame the camera again.

    Mutation this catches: deleting the `_camera_subject = _SUBJECT_POINTS`
    line from `_on_unrealize`.
    """
    import ui.viewer as viewer_module
    monkeypatch.setattr(viewer_module, "GL", _NoGL())

    viewer = _RealizableViewer()
    viewer.show_ribbon(ribbon_like_mesh())
    assert viewer._camera_subject == "ribbon", "precondition"
    ribbon_extent = viewer._extent

    viewer._on_unrealize(None)

    cloud = real_frames()[0]
    viewer.set_points(cloud)
    assert viewer._extent > ribbon_extent * 10, (
        "after a GL context loss the camera is still framed on a ribbon "
        "that no longer exists -- every later fold renders as a postage "
        "stamp in the middle of the screen")


def test_the_first_frame_after_a_context_loss_snaps_rather_than_easing(monkeypatch):
    """`_camera_framed` has to go with the subject: left True, the first
    frame would ease in from the dead ribbon's extent (`_EASE_OUT` is 0.06 --
    ~50 frames to converge on a fold that only sends 30) instead of snapping
    to its own spread."""
    import ui.viewer as viewer_module
    monkeypatch.setattr(viewer_module, "GL", _NoGL())

    viewer = _RealizableViewer()
    viewer.show_ribbon(ribbon_like_mesh())
    viewer._on_unrealize(None)

    cloud = real_frames()[0]
    snapped = viewer._spread(cloud, cloud.mean(axis=0), True)
    viewer.set_points(cloud)
    # Compared against the camera's OWN robust statistic on purpose, unlike
    # every other test in this file: the question here is "did it snap or
    # did it ease", not "is that statistic the right one to snap to" (which
    # the tests above already pin against `true_spread`). An eased first
    # frame would land between the dead ribbon's 9.7 A and this, nowhere
    # near either end.
    assert viewer._extent == pytest.approx(snapped, rel=1e-6), (
        "the first frame after a context loss must snap to its own spread, "
        "not ease in from the extent of a ribbon that no longer exists")
