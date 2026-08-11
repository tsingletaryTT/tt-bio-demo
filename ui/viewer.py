"""The structure viewer: a GtkGLArea that draws points and ribbons."""

import logging

import gi

gi.require_version("Gtk", "4.0")

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
        # context rejects outright. GDK will otherwise happily hand us a
        # GLES context on backends where one is available (observed on this
        # Wayland/Mesa/radeonsi stack, which supports both) — pin to desktop
        # GL so realize() gets a context our shaders can actually compile
        # against, on any platform this runs on.
        self.set_allowed_apis(Gdk.GLAPI.GL)

        self._point_program = None
        self._ribbon_program = None
        self._ready = False

        self._spin = 0.0
        self._blend = 0.0          # 0 = points only, 1 = ribbon only
        self._center = np.zeros(3, dtype=np.float32)
        self._extent = 20.0

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
        eye = np.array([0.0, self._extent * 0.35, distance])
        view = mathutil.look_at(eye, np.zeros(3), np.array([0.0, 1.0, 0.0]))
        proj = mathutil.perspective(45.0, width / height, 0.5, distance * 4.0)

        # Column-major storage means composition reads right-to-left when
        # transposed, so multiply in this order to get proj * view * model.
        return (model @ view @ proj).astype(np.float32), model

    def _on_render(self, _area, _context):
        GL.glClearColor(*BACKGROUND)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        return True
