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
        self._ready = True

    def _on_unrealize(self, _area):
        self.make_current()
        for program in (self._point_program, self._ribbon_program):
            if program:
                GL.glDeleteProgram(program)
        self._point_program = self._ribbon_program = None
        self._ready = False

    # ── camera ───────────────────────────────────────────────────────────

    def _mvp(self):
        width = max(self.get_width(), 1)
        height = max(self.get_height(), 1)
        distance = self._extent * 2.6

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
        if self._point_count == 0:
            # No frame has ever been framed yet (this is the constructor's
            # placeholder _extent=20.0, not a real prior frame). Easing from
            # it would frame the very first frame against an arbitrary
            # default instead of its actual spread -- fine by luck for a
            # fixture whose first-frame spread happens to be near 20, but
            # wrong in general (e.g. real diffusion noise scaled well past
            # or under that). Snap straight to the real spread instead.
            self._extent = max(spread, 5.0)
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
            # NB: deviates from the brief's literal `self._extent * 3.5`.
            # _mvp() places the eye at distance = self._extent * 2.6, and
            # the point vertex shader divides this uniform by
            # gl_Position.w (~ that same distance) to keep on-screen point
            # size roughly constant across zoom. With 3.5 those two
            # extent-proportional factors nearly cancel (3.5 / 2.6 ~= 1.35),
            # so points render at a near-constant ~1.3px regardless of
            # frame -- confirmed by direct measurement against the mock
            # runner: some frames luck into a full-opacity center pixel per
            # point, but the fixture's final, fully-converged frame (all
            # twelve points collinear, sharing one sub-pixel Y offset) hits
            # a worst case where every point's single rasterized sample
            # lands in the antialiased fringe and none reaches full color
            # -- i.e. the "reveal" moment of the whole demo was nearly
            # invisible. 24.0 keeps the same constant-apparent-size
            # behavior but at ~9px, comfortably visible on a projector.
            self._extent * 24.0)
        GL.glUniform3f(
            GL.glGetUniformLocation(self._point_program, "u_color"), *POINT_COLOR)
        GL.glUniform1f(
            GL.glGetUniformLocation(self._point_program, "u_opacity"), opacity)
        GL.glBindVertexArray(self._point_vao)
        GL.glDrawArrays(GL.GL_POINTS, 0, self._point_count)
        GL.glBindVertexArray(0)

    def _on_render(self, _area, _context):
        GL.glClearColor(*BACKGROUND)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        if not self._ready:
            return True

        if self._pending_points is not None:
            self._upload_points()

        mvp, _model = self._mvp()
        self._draw_points(mvp, self._point_opacity * (1.0 - self._blend))
        return True
