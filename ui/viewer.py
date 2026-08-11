"""The structure viewer: a GtkGLArea that draws points and ribbons."""

import logging

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
# of frame -- see task-8-report.md for the measurements.)
_CAMERA_DISTANCE_FACTOR = 2.6
_TARGET_POINT_PX = 9.0
_POINT_SIZE_FACTOR = _TARGET_POINT_PX * _CAMERA_DISTANCE_FACTOR


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
        # False when a genuinely new structure/job starts. There is no such
        # reset path yet: a future `clear_structure()` (Task 10, for
        # starting a second fold) MUST set this back to False, or that
        # job's first frame will ease in from the previous job's leftover
        # extent/center instead of snapping fresh -- reintroducing the exact
        # bad-first-frame bug this flag exists to prevent.
        self._camera_framed = False

        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.connect("render", self._on_render)

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
        # level, in task-9-report.md's Harness 1 -- forcing this exact
        # culling produced an identical pixel-for-pixel readback), so
        # enabling backface culling here removes the far wall from the
        # rasterizer entirely: only the true near surface ever draws,
        # independent of depth-write state or triangle order. Enabled
        # globally (once, here) rather than scoped around _draw_ribbon
        # because face culling only applies to polygons -- GL_TRIANGLES --
        # and never to GL_POINTS, so it cannot affect _draw_points either
        # way; there is no draw call in this file it would be wrong for.
        GL.glEnable(GL.GL_CULL_FACE)
        GL.glCullFace(GL.GL_BACK)
        self._ready = True

    def _on_unrealize(self, _area):
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
        """Upload a diffusion frame. Safe to call before GL is realized."""
        arr = np.ascontiguousarray(coords, dtype=np.float32).reshape(-1, 3)
        self._pending_points = arr
        self._point_opacity = opacity
        self._frame_camera(arr)
        self.queue_render()

    def _frame_camera(self, coords):
        """Center and scale the camera to fit the given coordinates."""
        if len(coords) == 0:
            return
        self._center = coords.mean(axis=0)
        spread = float(np.abs(coords - self._center).max())
        if not self._camera_framed:
            # No frame has ever been framed yet (this is the constructor's
            # placeholder _extent=20.0, not a real prior frame). Easing from
            # it would frame the very first frame against an arbitrary
            # default instead of its actual spread -- fine by luck for a
            # fixture whose first-frame spread happens to be near 20, but
            # wrong in general (e.g. real diffusion noise scaled well past
            # or under that). Snap straight to the real spread instead.
            self._extent = max(spread, 5.0)
            self._camera_framed = True
        else:
            # Ease toward the new extent so a noisy frame doesn't snap the
            # camera around as the cloud contracts.
            self._extent = max(self._extent * 0.8 + spread * 0.2, 5.0)

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
            # frame (see the module-level comment on _POINT_SIZE_FACTOR and
            # task-8-report.md for the measurements that caught this).
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
        self._frame_camera(self._pending_ribbon[0])
        self.queue_render()

    def clear_structure(self):
        """Drop whatever's currently shown so a new job starts from blank.

        Resets `_camera_framed` to False -- this is the reset path called
        out in the field's own comment in __init__: without it, the next
        job's first frame would ease the camera in from this job's leftover
        extent/center instead of snapping fresh, since `_frame_camera` only
        snaps when `_camera_framed` is False.
        """
        self._point_count = 0
        self._ribbon_index_count = 0
        # Any not-yet-uploaded data from the job that's ending must not
        # surface on a later render -- otherwise a race between this call
        # and the next _on_render could resurrect the old structure right
        # after we asked for it to disappear.
        self._pending_points = None
        self._pending_ribbon = None
        self._blend = 0.0
        self._camera_framed = False
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
        # the sole, fully-opaque visual (opacity == 1, the steady state
        # right after job_done snaps blend straight to 1.0 with no fade in
        # play). While opacity < 1 -- Task 10's cross-fade -- a translucent
        # ribbon must NOT write a solid depth value, or it would still fully
        # occlude the points drawn right after it via the depth test, even
        # though its own color contribution is only partial. That would
        # make the points vanish behind a half-see-through ribbon instead of
        # blending through it, defeating the entire point of a cross-fade.
        # Always restored to GL_TRUE below so this doesn't leak into
        # _draw_points or the next frame.
        GL.glDepthMask(GL.GL_TRUE if opacity >= 1.0 else GL.GL_FALSE)
        GL.glBindVertexArray(self._ribbon_vao)
        GL.glDrawElements(GL.GL_TRIANGLES, self._ribbon_index_count,
                          GL.GL_UNSIGNED_INT, None)
        GL.glBindVertexArray(0)
        GL.glDepthMask(GL.GL_TRUE)

    def set_blend(self, t):
        """0 shows only points, 1 only the ribbon.

        Temporary: Task 10 replaces this with an animated tick-driven
        version that eases between the two instead of snapping.
        """
        self._blend = float(np.clip(t, 0.0, 1.0))
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
