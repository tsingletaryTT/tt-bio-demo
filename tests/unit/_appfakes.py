"""The fakes and event builders every quad-era `DemoApp` test shares.

Not collected by pytest -- the leading underscore is what keeps it out, the
same convention `tests/unit/_legibility.py` already uses. It is a helper
module, not a test file.

It exists for one reason: ONE copy of the fake quad. Four cells is a
symmetric shape, and a second hand-written copy of `_FakeQuad` in another
test file is a second place for it to drift from `ui/quad.py` -- which is
precisely the drift that would let a test go green against a real widget
that no longer behaves that way. Task 17's tests import from here too.

The fakes are deliberately dumb recorders with one deliberate exception:
`_FakeQuad.viewer_for_slot` bounds-checks and returns `None`, exactly as
`QuadView._cell` does, because slot indices reach the quad from wire-shaped
data (a `card` field the router turned into an index) and a fake that raised
`IndexError` where the real widget returns `None` would make every
out-of-range test prove the opposite of what it claims.
"""

from protocol.events import pack_coords

from ui.app import DemoApp


class _FakeViewer:
    """Stands in for one cell's `ui.viewer.StructureViewer`.

    Counts what would have been drawn, with no GL context. Every method the
    app calls on a cell's viewer is here, so a headless `DemoApp` never hits
    an `AttributeError` on the draw path -- which would be caught by one of
    the app's broad guards and turn a real failure into a silent one.
    """

    def __init__(self, card=None):
        # Which chip this cell shows. Carried so a test that confuses two
        # cells has something to notice it WITH: four byte-identical fakes
        # are exactly the shape an index bug hides in.
        self.card = card
        self.points = 0
        self.ribbons = 0
        self.cleared = 0
        self.crossfades = 0
        self.held = False
        # `(kind, payload)` or None -- what is ON SCREEN, not how many calls
        # were made. Counting calls cannot tell "the previous protein is
        # still up" from "this cell is black", and that distinction is the
        # whole of hold-until-superseded.
        self.shown = None
        self.connection_state = "disconnected"

    def set_points(self, coords, opacity=1.0):
        self.points += 1
        self.shown = ("points", coords)

    def set_ribbon(self, *ribbon):
        self.ribbons += 1
        self.shown = ("ribbon", ribbon[0] if ribbon else None)

    def begin_crossfade(self):
        self.crossfades += 1

    def set_held(self, held):
        self.held = bool(held)

    def clear_structure(self):
        self.cleared += 1
        self.held = False
        self.shown = None

    def start_animation(self):
        pass


class _FakeQuad:
    """Stands in for `ui.quad.QuadView`: four cells, and what they were told.

    `captions` is a dict rather than a list on purpose -- a test can then
    assert that a cell was never captioned AT ALL (`0 not in q.captions`),
    which is a different and stronger claim than "its caption is empty".
    """

    def __init__(self, n=4, cards=None, viewer_factory=_FakeViewer):
        cards = list(cards) if cards is not None else list(range(n))
        self.cards = cards[:n]
        self.viewers = []
        for card in self.cards:
            viewer = viewer_factory()
            # Stamped rather than constructed with, so any recorder can be
            # used as the factory -- test_app_wiring's richer `FakeViewer`,
            # for one.
            viewer.card = card
            self.viewers.append(viewer)
        self.slot_count = len(self.viewers)
        self.captions = {}
        self.focus = None
        self.notice = None
        self.solo_mode = True
        self.solo_calls = []

    def viewer_for_slot(self, slot):
        """The cell's viewer, or None -- see this module's docstring."""
        if not isinstance(slot, int) or isinstance(slot, bool):
            return None
        if 0 <= slot < len(self.viewers):
            return self.viewers[slot]
        return None

    def set_caption(self, slot, text):
        self.captions[slot] = text

    def set_focus(self, slot):
        self.focus = slot

    def set_notice(self, text):
        self.notice = text

    def set_solo_mode(self, solo):
        self.solo_mode = bool(solo)
        self.solo_calls.append(bool(solo))

    def set_connection_state(self, state):
        for viewer in self.viewers:
            viewer.connection_state = state


def _app(cards=(0, 1, 2, 3), clock=None, viewer_factory=_FakeViewer):
    """A headless `DemoApp` whose quad is a recorder.

    The quad is installed BEFORE `attach_cards`, because attaching is what
    reconciles the cells with the card list -- and because `_ensure_quad`
    only ever rebuilds a real `QuadView`, so a fake installed here survives.
    """
    app = DemoApp(socket_path=None, clock=clock)
    app.quad = _FakeQuad(len(cards), cards=list(cards),
                         viewer_factory=viewer_factory)
    app.attach_cards(list(cards))
    return app


def _hello(cards=(0, 1, 2, 3)):
    return {"type": "hello", "version": 1, "cards": list(cards),
            "models": ["protenix-v2"], "preflight": "ok"}


def _start(job_id, card, target_id="t"):
    return {"type": "job_start", "job_id": job_id, "target_id": target_id,
            "model": "protenix-v2", "card": card, "n_residues": 20}


def _done(job_id):
    return {"type": "job_done", "job_id": job_id, "cif_path": f"/{job_id}.cif",
            "wall_s": 4.4, "mean_plddt": 95.3}


def _error(job_id, message="boom"):
    return {"type": "job_error", "job_id": job_id, "target_id": "t",
            "message": message}


def _stage(job_id, stage="diffusion", frac=0.5):
    return {"type": "stage", "job_id": job_id, "stage": stage, "frac": frac}


def _frame(job_id, n_atoms=4, spread=1.0):
    """A `frame` event carrying a real, decodable payload.

    The coordinates are keyed to `spread` so two folds' frames are
    distinguishable: a constant cloud would make "cell 2 drew ITS fold" and
    "cell 2 drew whatever arrived last" the same assertion.
    """
    coords = [[spread * i, spread * (i + 1), spread * (i + 2)]
              for i in range(n_atoms)]
    return {"type": "frame", "job_id": job_id, "step": 3, "total": 200,
            "n_atoms": n_atoms, "coords_b64": pack_coords(coords)}
