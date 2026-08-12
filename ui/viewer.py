"""The structure viewer: a GtkGLArea that draws points and ribbons."""

import logging
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

import numpy as np
from gi.repository import Gdk, Gtk
from OpenGL import GL

from ui import mathutil, shaders
from ui.glutil import GLError, compile_program

log = logging.getLogger(__name__)

# Tenstorrent dark base — the background the whole demo sits on.
BACKGROUND = (0x09 / 255.0, 0x22 / 255.0, 0x21 / 255.0, 1.0)
# Teal, used for the diffusion point cloud before confidence data exists.
POINT_COLOR = (0x74 / 255.0, 0xC5 / 255.0, 0xDF / 255.0)

# _mvp() places the camera at distance = self._extent * _CAMERA_DISTANCE_FACTOR,
# and the point vertex shader (ui/shaders.py POINT_VERT) divides its
# u_point_size uniform by gl_Position.w, which for a point near the origin is
# approximately that same distance. Both scale with self._extent, so their
# *ratio* -- not either value alone -- determines the point's apparent
# on-screen size, independent of zoom. _POINT_SIZE_FACTOR is derived from
# _CAMERA_DISTANCE_FACTOR and a target pixel size here, in code, rather than
# left as a comment at the call site: a future change to the camera distance
# now automatically keeps the point size in the same visible range, instead
# of silently drifting back toward invisibility with nothing to catch it.
# (The brief's original, unrelated 3.5 multiplier nearly canceled the 2.6
# distance factor and rendered points at a near-constant ~1.3px regardless
# of frame -- measured directly against a live GL context.)
_CAMERA_DISTANCE_FACTOR = 2.6
_TARGET_POINT_PX = 9.0
_POINT_SIZE_FACTOR = _TARGET_POINT_PX * _CAMERA_DISTANCE_FACTOR

_TWO_PI = 2.0 * math.pi

# ── camera framing ───────────────────────────────────────────────────────
#
# Two numbers decide how the camera behaves while a fold streams in, and
# they pull against each other:
#
#   * If the camera tracked the cloud exactly, the cloud would occupy the
#     same fraction of the frame in every frame and the visitor would
#     perceive NO contraction at all -- the single most important thing
#     this demo has to show.
#   * If it lags, the structure is small. Lag too much and it is a postage
#     stamp: the shipped 0.8/0.2 *linear* ease did exactly that. Measured
#     against the recorded Trp-cage trajectory (30 frames, spread 10364 A
#     -> 10 A, tests/fixtures/streams/real_fold_trpcage.jsonl) it left the
#     structure filling a median of 14% of the available height, and ended
#     the fold with the camera at 43.9 against a true spread of 10.0.
#
# The fix is to ease in the LOG domain, asymmetrically:
#
#   log(extent) <- (1-a)*log(extent) + a*log(target)
#
# Log-domain easing is the right shape for this data because diffusion
# contracts geometrically (~0.79x per frame here), so a constant weight
# produces a constant *ratio* of lag rather than the runaway linear lag
# above. The steady-state lag works out to (1-a)/a times the per-frame log
# contraction rate, which is a genuinely nice property: the camera trails
# in proportion to how fast the structure is currently collapsing. A fast
# collapse is therefore still visible as a shrink, and a structure that has
# settled ends up correctly framed with no special-casing.
#
# _EASE_IN is used when the structure is smaller than the current frame
# (the normal case: the cloud is contracting) and _EASE_OUT when it is
# bigger. Zooming out is deliberately made ~7x more reluctant than zooming
# in: within one job the cloud only ever contracts, so an outward step is
# almost always a noisy frame or a stray atom, and the cost of resisting is
# one or two frames of slight overflow. A genuinely new, larger structure
# never has to creep out through this ease at all -- clear_structure()
# resets _camera_framed and the first frame of the new job snaps.
#
# Measured over the recorded trajectory's 30 frames with these values: the
# camera trails the true spread by a median 1.37x and at worst 1.93x (the
# shipped linear ease: median 6.58x, worst 18.87x), which puts the
# structure at a median 68% of the frame height and never below 48% -- and
# it still trails by up to ~1.9x during the fast collapse, which is what
# keeps the contraction visible as a contraction rather than a blob that
# changes texture. The fold ends with the camera at 10.58 against a true
# spread of 9.99 (the shipped ease ended it at 43.86).
_EASE_IN = 0.40
_EASE_OUT = 0.06

# The camera never zooms closer than this, so a tiny peptide (or a
# collapsed-to-a-point degenerate frame) can't put the near plane inside
# the structure.
_MIN_EXTENT = 5.0

# What counts as "how big is this thing" for a streaming diffusion frame.
# `max` -- what this used to use unconditionally -- is the least robust
# statistic available: one atom flung far from the rest (which early
# diffusion frames genuinely produce) drags the whole camera out with it
# and shrinks the real structure to nothing. A high percentile of the
# per-atom Chebyshev radius ignores a handful of flyers; the headroom
# factor then puts the framing back to roughly where a well-behaved cloud's
# `max` would have been (measured: max/p96 is ~1.2 on average across the
# recorded trajectory), so the ordinary case is framed the same as before
# and only the outlier case differs.
#
# Deliberately NOT applied to the finished ribbon -- see set_ribbon(): that
# is a compact mesh with no flyers, it is the hero image, and clipping even
# 4% of it is a defect rather than a robustness win.
_SPREAD_PERCENTILE = 96.0
_SPREAD_HEADROOM = 1.2

# Which structure the camera is currently framing. The viewer shows a point
# cloud, then a finished ribbon, then (on the next job) a point cloud
# again, and the camera must follow whatever is actually ON SCREEN.
#
# The bug this exists to kill: ui/app.py builds the ribbon on a worker
# thread, so `job_done` for fold N routinely lands AFTER `job_start` for
# fold N+1 (measured on the live daemon). The sequence is therefore
# clear_structure() -> set_ribbon(fold N) -> a stream of set_points() calls
# carrying fold N+1's initial NOISE, while fold N's finished ribbon is
# still the only thing being drawn. Letting those point frames re-frame the
# camera shrank the hero image to a postage stamp within one frame -- the
# visitor watched the finished protein collapse to a dot, once per cycle,
# forever.
_SUBJECT_POINTS = "points"
_SUBJECT_RIBBON = "ribbon"

# A real GTK frame clock normally hands _on_tick a dt in the low tens of
# milliseconds. Two situations can make it far larger instead: the very
# first tick after start_animation() (nothing to diff against yet -- see
# _on_tick, which short-circuits that case explicitly) and a wall-clock gap
# with no ticks at all, e.g. the system suspending or the window being
# backgrounded for a while. blend_step already clamps at its target so a
# huge dt can't overshoot the cross-fade, but SPIN_RATE * dt has no such
# clamp -- an unclamped multi-minute dt would spin the model through
# thousands of radians in a single frame, which looks identical to the
# model jumping to a random new orientation. Cap dt at a ceiling well above
# any real frame interval so a resumed/foregrounded app just continues
# spinning smoothly from where it left off instead of jumping.
_MAX_TICK_DT = 0.5  # seconds

# The states EventClient reports via on_state_change (ui/client.py). Kept
# here, not just accepted on faith from callers, so a typo'd or unexpected
# string is caught where it's set rather than silently stored and never
# noticed.
_CONNECTION_STATES = frozenset({"connected", "disconnected", "incompatible"})


def blend_step(current, target, dt, duration):
    """Advance a 0-1 blend value toward `target`, never overshooting."""
    if duration <= 0.0:
        return target
    delta = dt / duration
    if target > current:
        return min(current + delta, target)
    return max(current - delta, target)


class StructureViewer(Gtk.GLArea):
    """Renders a diffusion point cloud, a finished ribbon, or a blend."""

    def __init__(self):
        super().__init__()
        self.set_has_depth_buffer(True)
        self.set_auto_render(True)
        # Our shaders are desktop GLSL ("#version 330 core"), which an ES
        # context rejects outright. GDK will pick a GLES context on stacks
        # where one is available even though desktop GL is too (observed on
        # this Wayland/Mesa/radeonsi stack) — pin to desktop GL so realize()
        # gets a context our shaders can actually compile against.
        # set_allowed_apis()/Gdk.GLAPI are a GTK 4.12+ addition, absent on
        # older-but-still-circulating GTK4 (e.g. Ubuntu 22.04 ships 4.6).
        # Where it's missing, fall back to GDK's own default negotiation
        # rather than crashing the widget's construction, and log so a
        # resulting shader-compile failure downstream is diagnosable instead
        # of a silent blank viewer with no clue why.
        if hasattr(self, "set_allowed_apis") and hasattr(Gdk, "GLAPI"):
            self.set_allowed_apis(Gdk.GLAPI.GL)
        else:
            log.warning(
                "Gtk.GLArea.set_allowed_apis is unavailable on this GTK "
                "version; cannot steer the context away from GLES. If the "
                "shaders below fail to compile, this is likely why.")

        self._point_program = None
        self._ribbon_program = None
        self._ready = False

        self._spin = 0.0
        self._blend = 0.0          # 0 = points only, 1 = ribbon only
        self._blend_target = 0.0
        self._tick_id = None
        self._last_frame_time = None
        self._connection_state = "disconnected"
        self._center = np.zeros(3, dtype=np.float32)
        self._extent = 20.0

        self._point_vao = None
        self._point_vbo = None
        self._point_count = 0
        self._point_opacity = 1.0
        self._pending_points = None

        self._ribbon_vao = None
        self._ribbon_buffers = None
        self._ribbon_index_count = 0
        self._pending_ribbon = None
        # True once _frame_camera has snapped the camera to a real frame at
        # least once. Deliberately separate from _point_count (which tracks
        # how many vertices are uploaded to the GPU right now, and gets
        # reset on unrealize/realize) -- this flag must NOT reset just
        # because the GL context was recreated; it should only go back to
        # False when a genuinely new structure/job starts. clear_structure()
        # below is that reset path: it sets this back to False so the next
        # job's first frame snaps fresh instead of easing in from this job's
        # leftover extent/center.
        self._camera_framed = False
        # Which structure the camera is framing right now -- see the
        # _SUBJECT_* constants above for the full argument. A new viewer has
        # never been handed a ribbon, so the point cloud owns the camera.
        self._camera_subject = _SUBJECT_POINTS

        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.connect("render", self._on_render)

    # ── connection state ─────────────────────────────────────────────────
    #
    # Exposed as a real property (not a bare attribute) so a typo'd or
    # otherwise unexpected string from EventClient is caught right where
    # it's assigned, rather than being stored silently and discovered much
    # later by whatever eventually reads it. As of this task nothing reads
    # it for rendering -- Phase 3's four-state machine and telemetry panel
    # (see the plan's "what this phase deliberately leaves out") are what's
    # expected to consume it. Recording it here regardless is still useful:
    # it is the one piece of live connection status the app already has in
    # hand, and it costs nothing to keep it findable on the viewer instead
    # of dropping it on the floor.

    @property
    def connection_state(self):
        return self._connection_state

    @connection_state.setter
    def connection_state(self, value):
        if value not in _CONNECTION_STATES:
            raise ValueError(f"unknown connection state: {value!r}")
        self._connection_state = value

    # ── animation ────────────────────────────────────────────────────────

    CROSSFADE_SECONDS = 0.8
    SPIN_RATE = 0.35  # radians per second

    def start_animation(self):
        """Drive spin and cross-fade from GTK's frame clock."""
        if self._tick_id is None:
            self._tick_id = self.add_tick_callback(self._on_tick)

    def stop_animation(self):
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None

    def begin_crossfade(self):
        """Fade from the point cloud to the ribbon."""
        self._blend_target = 1.0

    def _on_tick(self, _widget, frame_clock):
        now = frame_clock.get_frame_time() / 1e6  # microseconds to seconds
        if self._last_frame_time is None:
            # Nothing to diff against yet -- record this instant as the
            # baseline and advance nothing this frame. Without this, the
            # very first tick would measure dt against frame time 0 (or
            # whatever _last_frame_time was left at), producing a bogus
            # multi-second-or-worse dt on frame one.
            self._last_frame_time = now
            return True
        dt = now - self._last_frame_time
        self._last_frame_time = now
        # Clamp dt into a sane range: guards a large gap (system suspend,
        # the window being backgrounded -- see _MAX_TICK_DT's module-level
        # comment) and, defensively, a clock that ever appears to move
        # backwards (not observed on this stack, but a negative dt would
        # run the cross-fade a step in reverse for one frame).
        dt = max(0.0, min(dt, _MAX_TICK_DT))

        # Wrap _spin into [0, 2*pi) so it stays bounded across an all-day
        # (or longer) unattended run instead of growing forever. rotation_y
        # is periodic in 2*pi, so this changes nothing about what's drawn.
        self._spin = (self._spin + self.SPIN_RATE * dt) % _TWO_PI
        self._blend = blend_step(
            self._blend, self._blend_target, dt, self.CROSSFADE_SECONDS)
        self.queue_render()
        return True

    # ── lifecycle ────────────────────────────────────────────────────────

    def _on_realize(self, _area):
        self.make_current()
        if self.get_error() is not None:
            log.error("GL area failed to realize: %s", self.get_error())
            return
        try:
            self._point_program = compile_program(
                shaders.POINT_VERT, shaders.POINT_FRAG)
            self._ribbon_program = compile_program(
                shaders.RIBBON_VERT, shaders.RIBBON_FRAG)
        except GLError:
            log.exception("shader setup failed; viewer will stay blank")
            # Tidy up a partial success (e.g. point program compiled, then
            # the ribbon program failed) so we don't leave a live handle
            # dangling outside of _on_unrealize's cleanup.
            if self._point_program:
                GL.glDeleteProgram(self._point_program)
            self._point_program = self._ribbon_program = None
            return

        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glEnable(GL.GL_PROGRAM_POINT_SIZE)
        # The ribbon is a closed tube, so from outside almost every screen
        # pixel is hit by two of its triangles (the near wall and the far
        # wall). That's harmless while the ribbon draws fully opaque with
        # depth writes on (near wins depth, far is correctly discarded) --
        # but _draw_ribbon turns depth writes OFF while translucent (see its
        # comment), which is required so a fading ribbon can't occlude the
        # points behind it, but as a side effect leaves near vs. far
        # decided by triangle index order instead of actual depth for that
        # whole draw. Since RIBBON_FRAG's diffuse term depends on
        # `dot(n, light)`, the near and far walls shade differently, so the
        # tube could shimmer between correct and backwards-lit across its
        # entire surface for the whole duration of Task 10's cross-fade.
        # tube_mesh's winding is outward/CCW as seen from the camera
        # (verified in tests/unit/test_geometry_mesh.py and, at the pixel
        # level, by a glReadPixels harness: forcing this exact culling
        # produced an identical pixel-for-pixel readback), so enabling
        # backface culling here removes the far wall from the
        # rasterizer entirely: only the true near surface ever draws,
        # independent of depth-write state or triangle order. Enabled
        # globally (once, here) rather than scoped around _draw_ribbon
        # because face culling only applies to polygons -- GL_TRIANGLES --
        # and never to GL_POINTS, so it cannot affect _draw_points either
        # way; there is no draw call in this file it would be wrong for.
        GL.glEnable(GL.GL_CULL_FACE)
        GL.glCullFace(GL.GL_BACK)
        self._ready = True

        # Resume the tick callback on every realize, not just the first one.
        # This widget's own comments elsewhere (see _camera_framed and the
        # point/ribbon buffer resets in _on_unrealize below) already treat
        # realize/unrealize as something that can happen more than once in
        # a session, not just at construction and final teardown. Pairs
        # with the stop_animation() call in _on_unrealize just below:
        # without resuming here, a widget that unrealizes and re-realizes
        # mid-session (GL context loss, monitor change, etc.) would lose
        # its spin/cross-fade permanently, since app.py only calls
        # start_animation() once, right after the window is first
        # presented. start_animation() is idempotent (guarded by
        # `_tick_id is None`) so this is a no-op on the ordinary path where
        # realize only ever happens once.
        self.start_animation()

    def _on_unrealize(self, _area):
        # Stop driving spin/cross-fade before tearing down GL state below.
        # start_animation()/stop_animation() were added as a pair (Task 10)
        # but nothing called the teardown half -- completing it here means
        # the widget doesn't keep ticking (and calling queue_render() on
        # itself) across a context loss it can't currently render through
        # anyway, and it restores the pair's symmetry with the resume in
        # _on_realize above.
        self.stop_animation()

        self.make_current()
        for program in (self._point_program, self._ribbon_program):
            if program:
                GL.glDeleteProgram(program)
        self._point_program = self._ribbon_program = None
        if self._point_vao is not None:
            GL.glDeleteVertexArrays(1, [self._point_vao])
            GL.glDeleteBuffers(1, [self._point_vbo])
        self._point_vao = None
        self._point_vbo = None
        # The VBO backing this count is gone (or about to be, on a future
        # realize, regenerated fresh) -- without resetting this, _draw_points
        # would see a stale nonzero count and try to bind the now-None VAO.
        # Left deliberately distinct from _camera_framed (see __init__):
        # this is GPU-buffer bookkeeping, tied to the GL context's lifetime;
        # _camera_framed is about which *job's* data the camera has framed,
        # unrelated to context recreation.
        self._point_count = 0

        # Same lifecycle contract as the point buffers just above: the
        # ribbon VAO/VBOs/EBO die with this GL context, so the handles and
        # the index count that gates _draw_ribbon must be dropped together
        # here, or a future realize would hand out fresh buffer ids while
        # _ribbon_index_count still claims the old (now-deleted) VAO has
        # geometry to draw.
        if self._ribbon_vao is not None:
            GL.glDeleteVertexArrays(1, [self._ribbon_vao])
            GL.glDeleteBuffers(len(self._ribbon_buffers), self._ribbon_buffers)
        self._ribbon_vao = None
        self._ribbon_buffers = None
        self._ribbon_index_count = 0

        self._ready = False

    # ── camera ───────────────────────────────────────────────────────────

    def _mvp(self):
        width = max(self.get_width(), 1)
        height = max(self.get_height(), 1)
        distance = self._extent * _CAMERA_DISTANCE_FACTOR

        model = mathutil.rotation_y(self._spin)
        model[3, :3] -= self._center @ model[:3, :3]
        eye = np.array([0.0, self._extent * 0.35, distance])
        view = mathutil.look_at(eye, np.zeros(3), np.array([0.0, 1.0, 0.0]))
        proj = mathutil.perspective(45.0, width / height, 0.5, distance * 4.0)

        # Column-major storage means composition reads right-to-left when
        # transposed, so multiply in this order to get proj * view * model.
        return (model @ view @ proj).astype(np.float32), model

    # ── point cloud ──────────────────────────────────────────────────────

    def set_points(self, coords, opacity=1.0):
        """Upload a diffusion frame. Safe to call before GL is realized.

        The frame is ALWAYS uploaded -- it is what the cross-fade fades out
        of, and dropping it would blank the cloud. Whether it also moves the
        camera depends on what is currently on screen: a point frame may not
        re-frame a finished ribbon that is still being displayed (see the
        _SUBJECT_* constants for the ordering that makes this a live bug and
        not a hypothetical one).
        """
        arr = np.ascontiguousarray(coords, dtype=np.float32).reshape(-1, 3)
        self._pending_points = arr
        self._point_opacity = opacity
        if self._camera_subject == _SUBJECT_POINTS:
            self._frame_camera(arr)
        self.queue_render()

    @staticmethod
    def _spread(coords, center, robust):
        """How far the structure reaches from its center, in world units.

        Chebyshev (per-axis max) rather than Euclidean radius because the
        camera is fitting an axis-aligned box to the screen, not a sphere.

        `robust=True` (a sampled, noisy diffusion frame) takes a high
        percentile of the per-atom radius plus a headroom factor, so a
        handful of flyers cannot dictate the zoom; `robust=False` (a
        finished mesh) takes the true maximum so every last vertex of the
        hero image is inside the frame. See _SPREAD_PERCENTILE.
        """
        offsets = np.abs(coords - center)
        if not robust:
            return float(offsets.max())
        per_atom = offsets.max(axis=1)
        return float(np.percentile(per_atom, _SPREAD_PERCENTILE)) * _SPREAD_HEADROOM

    def _frame_camera(self, coords, snap=False, robust=True):
        """Center and scale the camera to fit the given coordinates.

        By default eases toward the new extent (right for a stream of
        diffusion frames, where a noisy frame shouldn't snap the camera
        around as the cloud contracts). Pass `snap=True` for a one-shot
        upload -- e.g. the finished ribbon -- that will not be followed by
        further calls to ease the rest of the way in; see set_ribbon().

        `robust` selects the spread statistic (see _spread) and is
        deliberately independent of `snap`: it describes what KIND of data
        this is (a noisy sampled cloud vs. a finished mesh), not when it
        arrives. In particular the first frame of a job both snaps *and*
        wants the robust statistic, or a single flyer atom in that one
        frame would set the whole job's opening zoom.
        """
        if len(coords) == 0:
            return
        self._center = coords.mean(axis=0)
        # Clamped here, before the ease, as well as after it. Clamping the
        # target is what keeps log() away from a zero spread on a degenerate
        # frame where every atom landed in the same place; the algebra then
        # says the weighted *geometric* mean of two values that are both
        # >= _MIN_EXTENT is itself >= _MIN_EXTENT, so the second clamp is
        # only mopping up float rounding -- exp(log(5.0)) is 4.999999999999999
        # on this stack, and an _extent one ulp under the floor every frame
        # is exactly the kind of thing that goes unnoticed until it doesn't.
        target = max(self._spread(coords, self._center, robust), _MIN_EXTENT)

        if snap or not self._camera_framed:
            # Either explicitly requested, or no frame has ever been framed
            # yet (this is the constructor's placeholder _extent=20.0, not a
            # real prior frame) -- easing from it would frame the very first
            # frame against an arbitrary default instead of its actual
            # spread. Snap straight to the real spread instead.
            self._extent = target
            self._camera_framed = True
            return

        # Asymmetric, log-domain ease: quick to close in on a contracting
        # structure, reluctant to give ground back. See _EASE_IN/_EASE_OUT.
        weight = _EASE_IN if target < self._extent else _EASE_OUT
        self._extent = max(math.exp(
            math.log(self._extent) * (1.0 - weight) + math.log(target) * weight),
            _MIN_EXTENT)

    def _upload_points(self):
        coords = self._pending_points
        self._pending_points = None

        if self._point_vao is None:
            self._point_vao = GL.glGenVertexArrays(1)
            self._point_vbo = GL.glGenBuffers(1)

        GL.glBindVertexArray(self._point_vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._point_vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, coords.nbytes, coords, GL.GL_DYNAMIC_DRAW)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        GL.glBindVertexArray(0)
        self._point_count = len(coords)

    def _draw_points(self, mvp, opacity):
        if not self._point_count or opacity <= 0.0:
            return
        GL.glUseProgram(self._point_program)
        GL.glUniformMatrix4fv(
            GL.glGetUniformLocation(self._point_program, "u_mvp"),
            1, GL.GL_FALSE, mvp)
        GL.glUniform1f(
            GL.glGetUniformLocation(self._point_program, "u_point_size"),
            # NB: deviates from the brief's literal `self._extent * 3.5`,
            # which nearly canceled against _mvp's distance factor and
            # rendered points at a near-constant ~1.3px regardless of
            # frame (see the module-level comment on _POINT_SIZE_FACTOR).
            self._extent * _POINT_SIZE_FACTOR)
        GL.glUniform3f(
            GL.glGetUniformLocation(self._point_program, "u_color"), *POINT_COLOR)
        GL.glUniform1f(
            GL.glGetUniformLocation(self._point_program, "u_opacity"), opacity)
        GL.glBindVertexArray(self._point_vao)
        GL.glDrawArrays(GL.GL_POINTS, 0, self._point_count)
        GL.glBindVertexArray(0)

    # ── ribbon ───────────────────────────────────────────────────────────

    def set_ribbon(self, vertices, normals, colors, indices):
        """Upload a finished structure. Safe to call before GL is realized."""
        self._pending_ribbon = (
            np.ascontiguousarray(vertices, dtype=np.float32),
            np.ascontiguousarray(normals, dtype=np.float32),
            np.ascontiguousarray(colors, dtype=np.float32),
            np.ascontiguousarray(indices, dtype=np.uint32),
        )
        # Unlike set_points(), this is called exactly once per job -- there
        # is no subsequent frame to ease the rest of the way in. _frame_camera
        # defaults to an 80/20 ease meant for a stream; a single eased step
        # here would leave the camera framed against a blend of the last
        # diffusion frame's extent and the ribbon's own, permanently (the
        # finished structure is what stays on screen for the rest of the
        # attract cycle). Snap straight to the ribbon's actual spread instead.
        #
        # robust=False for the same reason: this mesh is the hero image and
        # every vertex of it must be inside the frame, so it is fitted with
        # a true max rather than the percentile used for noisy point frames.
        self._frame_camera(self._pending_ribbon[0], snap=True, robust=False)
        # The ribbon is now what the viewer is about, and it holds the
        # camera until the next job explicitly starts (clear_structure) or a
        # caller forces points-only (set_blend(0)). Without this, the very
        # next diffusion frame -- which in production belongs to the NEXT
        # fold and arrives while this ribbon is still the only thing drawn
        # -- would immediately re-frame the camera around a noise cloud
        # hundreds of times this structure's size.
        self._camera_subject = _SUBJECT_RIBBON
        self.queue_render()

    def clear_structure(self):
        """Drop whatever's currently shown so a new job starts from blank.

        Resets every piece of per-job state, not just the counts:

        - `_point_count` / `_ribbon_index_count` / `_pending_points` /
          `_pending_ribbon`: GPU-buffer and not-yet-uploaded state. Any
          not-yet-uploaded data from the job that's ending must not surface
          on a later render -- otherwise a race between this call and the
          next `_on_render` could resurrect the old structure right after
          we asked for it to disappear.
        - `_camera_framed`: set back to False, the reset path called out in
          the field's own comment in __init__ -- without it, the next job's
          first frame would ease the camera in from this job's leftover
          extent/center instead of snapping fresh, since `_frame_camera`
          only snaps when `_camera_framed` is False.
        - `_blend` *and* `_blend_target`: both, not just `_blend`. Without
          resetting the target too, a second fold in the same session would
          inherit `_blend_target == 1.0` from the first job's completed
          cross-fade, and the very next tick would immediately start easing
          `_blend` back toward 1.0 -- fading the new point cloud straight
          into an empty ribbon-shaped hole instead of showing it. This is
          exactly the bug the brief calls out by name; the fix is resetting
          both fields here, together, every time.
        - `_camera_subject`: back to the point cloud. This is the ONE place
          a ribbon gives the camera back (see set_ribbon), and it is the
          right one: a job_start is the app's own statement that the ribbon
          on screen is finished with and the incoming diffusion frames are
          the subject now. Paired with the `_camera_framed` reset just
          above, the next job's first frame snaps to its own spread rather
          than either easing from, or being locked out by, the last job.
        """
        self._point_count = 0
        self._ribbon_index_count = 0
        self._pending_points = None
        self._pending_ribbon = None
        self._blend = 0.0
        self._blend_target = 0.0
        self._camera_framed = False
        self._camera_subject = _SUBJECT_POINTS
        self.queue_render()

    def _upload_ribbon(self):
        verts, norms, colors, indices = self._pending_ribbon
        self._pending_ribbon = None

        if self._ribbon_vao is None:
            self._ribbon_vao = GL.glGenVertexArrays(1)
            self._ribbon_buffers = GL.glGenBuffers(4)

        vbo_pos, vbo_norm, vbo_color, ebo = self._ribbon_buffers
        GL.glBindVertexArray(self._ribbon_vao)

        for location, buffer, data in (
            (0, vbo_pos, verts), (1, vbo_norm, norms), (2, vbo_color, colors)
        ):
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, buffer)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
            GL.glEnableVertexAttribArray(location)
            GL.glVertexAttribPointer(location, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, None)

        GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, ebo)
        GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices,
                        GL.GL_STATIC_DRAW)
        GL.glBindVertexArray(0)
        self._ribbon_index_count = len(indices)

    def _draw_ribbon(self, mvp, model, opacity):
        if not self._ribbon_index_count or opacity <= 0.0:
            return
        GL.glUseProgram(self._ribbon_program)
        GL.glUniformMatrix4fv(
            GL.glGetUniformLocation(self._ribbon_program, "u_mvp"),
            1, GL.GL_FALSE, mvp)
        GL.glUniformMatrix4fv(
            GL.glGetUniformLocation(self._ribbon_program, "u_model"),
            1, GL.GL_FALSE, model)
        GL.glUniform1f(
            GL.glGetUniformLocation(self._ribbon_program, "u_opacity"), opacity)
        # Depth *test* stays on (enabled once, globally, in _on_realize) so
        # the ribbon still respects whatever's already in the depth buffer.
        # Depth *write* is conditional: only latch depth when the ribbon is
        # the sole, fully-opaque visual (opacity == 1, the steady state once
        # begin_crossfade()'s fade-in reaches 1.0). While opacity < 1 -- the
        # cross-fade itself -- a translucent ribbon must NOT write a solid
        # depth value, or it would still fully occlude the points drawn
        # right after it via the depth test, even though its own color
        # contribution is only partial. That would make the points vanish
        # behind a half-see-through ribbon instead of blending through it,
        # defeating the entire point of a cross-fade. Always restored to
        # GL_TRUE below so this doesn't leak into _draw_points or the next
        # frame.
        GL.glDepthMask(GL.GL_TRUE if opacity >= 1.0 else GL.GL_FALSE)
        GL.glBindVertexArray(self._ribbon_vao)
        GL.glDrawElements(GL.GL_TRIANGLES, self._ribbon_index_count,
                          GL.GL_UNSIGNED_INT, None)
        GL.glBindVertexArray(0)
        GL.glDepthMask(GL.GL_TRUE)

    def set_blend(self, t):
        """Jump the blend immediately; prefer begin_crossfade() for transitions.

        Also pins `_blend_target` to the same value, so a later tick (which
        eases `_blend` toward `_blend_target` every frame) can't immediately
        undo this jump by continuing to chase whatever target was set
        before -- see clear_structure() for why a stale target is exactly
        the bug that would reintroduce.

        t == 0 means "points only, the ribbon is not being displayed", so it
        also hands the camera back to the point cloud -- the invariant this
        viewer keeps is that the camera frames whatever is actually on
        screen. A non-zero t only ever makes a ribbon *more* visible, and
        ownership already moved to the ribbon when set_ribbon() supplied it,
        so nothing needs to change in that direction.
        """
        self._blend = self._blend_target = float(np.clip(t, 0.0, 1.0))
        if self._blend == 0.0:
            self._camera_subject = _SUBJECT_POINTS
        self.queue_render()

    def _on_render(self, _area, _context):
        GL.glClearColor(*BACKGROUND)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        if not self._ready:
            return True

        if self._pending_points is not None:
            self._upload_points()
        if self._pending_ribbon is not None:
            self._upload_ribbon()

        mvp, model = self._mvp()
        # Ribbon first, points second: translucent points should composite
        # over the ribbon's color. This ordering is safe with depth testing
        # on because _draw_ribbon only writes depth while fully opaque (see
        # its comment) -- so a translucent ribbon never hides points behind
        # it via the depth test during a cross-fade, only via alpha.
        self._draw_ribbon(mvp, model, self._blend)
        self._draw_points(mvp, self._point_opacity * (1.0 - self._blend))
        return True
