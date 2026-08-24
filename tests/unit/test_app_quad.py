"""Task 15: four folds reach four cells, and nothing leaks between them.

Every test here is about ISOLATION -- the thing that was free when the booth
had one viewer and one fold, and that has to be built and held on to now that
it has four of each. The failure mode is always the same shape and always
silent: a field that used to be one thing for one fold is still one thing for
four, so whichever fold happens to be fastest, newest or loudest speaks for
all of them.

Four cells are a SYMMETRIC fixture, which is exactly the shape an index bug
hides in. So nothing below is asserted only in aggregate: every test that
could confuse two cells names both, and `_appfakes._frame` gives each fold a
distinguishable payload for the same reason.

Two deliberate departures from the task brief, both flagged in the task
report:

* the brief's `job_start` tests assert that a `job_start` CLEARS its cell.
  That was the pre-2026-08-13 sequencing; clearing on `job_start` is the
  empty-viewer defect (20 of 45 sampled seconds black on a 91-second
  recording -- see ui/app.py's second docstring section and
  tests/unit/test_viewer_hold.py). The isolation those tests exist to pin is
  kept and driven through the real path instead: the clear belongs to the
  first frame that can replace what is on screen, and it must still touch
  exactly one cell.
* the brief's "deferred clear" tests become "deferred FOLD" tests, for the
  same reason: there is no deferred clear any more, there is a deferred
  `job_start` (`ui.slots.SlotState`), and what must not leak across cells is
  the fold it starts.
"""

import pytest

from _appfakes import (_FakeQuad, _app, _done, _error, _frame, _hello, _stage,
                       _start)
from ui.app import DemoApp


def _deliver(app, event):
    """Put a frame through the app's real path: the socket-side callback
    (which buffers it) and then the drain source (which draws it).

    Never `viewer.set_points` directly -- the routing, the suppression and
    the first-frame handover all live between those two, and reaching past
    them would test nothing.
    """
    app._on_event(event)
    app._drain_frames()


# ── which cell does this belong to ──────────────────────────────────────────

def test_the_card_list_comes_from_hello():
    """A booth on two chips must build two cells, not four empty ones."""
    app = DemoApp(socket_path=None)
    app.quad = _FakeQuad(4)
    app._handle_event(_hello(cards=[0, 2]))
    assert app.router.slot_for_card(2) == 1
    assert app.router.slot_for_card(1) is None
    assert len(app.router.slots) == 2


def test_a_booth_with_no_socket_still_has_a_cell():
    """`attach_cards` is called from construction with a single-card default
    precisely so a booth started with no --socket renders something."""
    app = DemoApp(socket_path=None)
    assert app.router is not None and len(app.router.slots) == 1


def test_a_second_hello_with_the_same_chips_does_not_reset_the_cells():
    """A dropped socket reconnects and says hello again. Rebuilding the
    router there would blank four cells and forget four folds for nothing."""
    app = _app()
    app._handle_event(_start("j2", card=2))
    app._handle_event(_hello(cards=[0, 1, 2, 3]))
    assert app.router.slot_for_job("j2") == 2
    assert app.router.slots[2].state == "folding"


def test_a_booth_that_never_got_a_hello_still_builds_four_cells():
    """FOUND BY LOOKING AT IT, on the real daemon, and by nothing else.

    `hello` is not a promise the booth will ever get. The daemon greets a UI
    that connects during the model-load window with `not_ready` instead
    (runner/daemon.py's `_hello` -- model load stretched to 6.4-9.2s under
    four-way contention), and it only greets at accept time. The first live
    run of the quad logged four folds on four chips and drew ONE CELL: the
    UI connected early, was told `not_ready`, and never learned the card
    list. Every test in this file was green.

    So the chips are learned from the events that name them.
    """
    app = _app(cards=(0,))
    app.quad = _FakeQuad(4)                    # room on screen for four
    app._handle_event({"type": "not_ready", "missing": ["workers: loading"]})
    for card in (3, 1, 2):
        app._handle_event({"type": "card_state", "card": card, "state": "idle"})
    assert sorted(app.router.cards) == [0, 1, 2, 3]
    app._handle_event(_start("j3", card=3))
    assert app.router.slot_for_job("j3") == app.router.slot_for_card(3)


def test_a_job_start_on_an_unknown_chip_is_not_thrown_away():
    """The other event that names a chip. A fold really is running on it."""
    app = _app(cards=(0,))
    app.quad = _FakeQuad(4)
    app._handle_event(_start("j2", card=2))
    assert app.router.slot_for_card(2) is not None
    assert app.router.slots[app.router.slot_for_card(2)].job_id == "j2"


def test_learning_about_a_chip_does_not_disturb_a_cell_mid_fold():
    """Appending, never rebuilding: cell 0 is in the middle of a fold when
    chip 3 is heard of for the first time, and must not notice."""
    app = _app(cards=(0,))
    app.quad = _FakeQuad(4)
    app._handle_event(_start("j0", card=0))
    _deliver(app, _frame("j0", spread=1.0))
    drawn = app.quad.viewers[0].shown

    app._handle_event({"type": "card_state", "card": 3, "state": "idle"})
    assert app.router.slot_for_card(0) == 0
    assert app.router.slots[0].job_id == "j0"
    assert app.quad.viewers[0].shown == drawn
    assert app.quad.viewers[0].cleared == 1, "cell 0 was blanked, or re-cleared"


def test_a_fifth_chip_does_not_grow_a_fifth_cell():
    app = _app()
    app._handle_event({"type": "card_state", "card": 9, "state": "idle"})
    assert len(app.router.slots) == 4
    assert app.router.slot_for_card(9) is None


# ── the clear, and hold-until-superseded ────────────────────────────────────

def test_a_job_start_clears_no_cell_at_all():
    """Hold-until-superseded, per cell. Only `diffusion` emits frames and a
    223-residue target spends ~15s in `trunk` with nothing to draw, so a
    `job_start` that cleared would blank the cell for fifteen seconds."""
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_start("j3", card=3))
    assert [v.cleared for v in app.quad.viewers] == [0, 0, 0, 0]


def test_a_new_folds_first_frame_clears_only_its_own_cell():
    """The defect a global clear produces: chip 3's fold reaching diffusion
    blanks the structure a visitor is looking at on chip 0."""
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_start("j3", card=3))
    _deliver(app, _frame("j3"))
    assert app.quad.viewers[3].cleared == 1
    assert [v.cleared for v in app.quad.viewers] == [0, 0, 0, 1]


# ── the frame stream ────────────────────────────────────────────────────────

def test_a_frame_reaches_only_its_own_cell():
    app = _app()
    app._handle_event(_start("j2", card=2))
    _deliver(app, _frame("j2"))
    assert app.quad.viewers[2].points == 1
    assert sum(v.points for v in app.quad.viewers) == 1


def test_four_frame_streams_do_not_fight_over_one_buffer():
    """The single global LatestFrame's failure mode: every cell shows
    whichever fold happened to be fastest."""
    app = _app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    for card in range(4):
        app._on_event(_frame(f"j{card}", spread=1.0 + card))
    app._drain_frames()
    assert [v.points for v in app.quad.viewers] == [1, 1, 1, 1]
    # ...and each cell drew ITS OWN fold's coordinates. Four equal counts is
    # exactly what one shared buffer would produce if it were drained four
    # times, so the counts alone cannot tell the two apart.
    for card, viewer in enumerate(app.quad.viewers):
        kind, coords = viewer.shown
        assert kind == "points"
        assert coords[1][0] == pytest.approx(1.0 + card), (
            f"cell {card} drew another fold's frame")


def test_a_frame_for_an_unknown_job_is_dropped_without_disturbing_a_cell():
    """Routing an unroutable frame to cell 0 is the tempting shortcut, and it
    puts one fold's coordinates under another fold's caption.

    Cell 0 is deliberately ALREADY STREAMING its own fold here. With a cell
    still waiting for its first frame the straggler check would reject the
    ghost anyway, and the test would pass against a booth that routed
    everything to cell 0 -- which is exactly what it happened to do before
    the mutation sweep caught it.
    """
    app = _app()
    app._handle_event(_start("j0", card=0))
    _deliver(app, _frame("j0", spread=1.0))
    drawn = [v.points for v in app.quad.viewers]

    _deliver(app, _frame("ghost", spread=9.0))
    assert [v.points for v in app.quad.viewers] == drawn
    kind, coords = app.quad.viewers[0].shown
    assert coords[1][0] == pytest.approx(1.0), \
        "cell 0 drew a frame belonging to no fold at all"


def test_the_frame_buffer_does_not_grow_with_every_job():
    app = _app()
    for n in range(200):
        app._on_event(_frame(f"ghost{n}"))
    assert len(app._frames) <= 8


def test_a_showcasing_cell_suppresses_its_own_frames_only():
    """Cell 0 holds a finished structure while cell 1 keeps condensing."""
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_start("j1", card=1))
    app._handle_event(_done("j0"))
    app._on_event(_frame("j0"))
    app._on_event(_frame("j1"))
    app._drain_frames()
    assert app.quad.viewers[0].points == 0
    assert app.quad.viewers[1].points == 1


def test_a_suppressed_frame_is_not_discarded():
    """It stays in the latest-wins buffer so the cell cuts straight to live
    diffusion the instant the dwell expires -- unchanged rule, per cell."""
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_done("j0"))
    app._on_event(_frame("j0"))
    app._drain_frames()
    assert app.quad.viewers[0].points == 0
    app.router.slots[0].on_structure_revealed()
    app._tick_state_at(0.0)
    app._tick_state_at(99.0)              # the dwell expires
    assert app.quad.viewers[0].points == 1


# ── the deferred fold ───────────────────────────────────────────────────────

def test_a_deferred_fold_takes_over_its_own_cell_when_the_dwell_expires():
    """The daemon starts the next fold on a chip the instant the last one
    finishes, so a `job_start` arriving mid-showcase is deferred behind the
    dwell (`ui.slots.SlotState`) -- and then really applied."""
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_done("j0"))
    app.router.slots[0].on_structure_revealed()
    app._tick_state_at(0.0)
    app._handle_event(_start("j0b", card=0))
    assert app.router.slots[0].job_id == "j0", "the dwell was cut short"
    app._tick_state_at(99.0)
    assert app.router.slots[0].job_id == "j0b"
    assert app.router.slots[0].state == "folding"


def test_a_deferred_fold_never_touches_another_cell():
    app = _app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    app._handle_event(_done("j0"))
    app.router.slots[0].on_structure_revealed()
    app._tick_state_at(0.0)
    app._handle_event(_start("j0b", card=0))
    before = [(v.cleared, v.points) for v in app.quad.viewers]
    app._tick_state_at(99.0)
    after = [(v.cleared, v.points) for v in app.quad.viewers]
    assert after[1:] == before[1:]
    assert [app.router.slots[i].job_id for i in (1, 2, 3)] == ["j1", "j2", "j3"]


# ── the ribbon, per cell ────────────────────────────────────────────────────

def test_a_ribbon_lands_in_its_own_cell(monkeypatch):
    import ui.app as mod
    monkeypatch.setattr(mod, "structure_mesh",
                        lambda path, **kw: ("v", "n", "c", "i"))
    app = _app()
    app._handle_event(_start("j2", card=2))
    app._handle_event(_done("j2"))
    app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()
    assert app.quad.viewers[2].ribbons == 1
    assert sum(v.ribbons for v in app.quad.viewers) == 1


def test_a_fold_on_one_chip_does_not_invalidate_another_chips_ribbon(monkeypatch):
    """THE per-slot generation-counter test. With one global counter, a
    job_done on chip 3 bumps the generation and chip 0's in-flight ribbon is
    dropped as 'stale' -- silently, every cycle, forever."""
    import ui.app as mod
    monkeypatch.setattr(mod, "structure_mesh",
                        lambda path, **kw: ("v", "n", "c", "i"))
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_start("j3", card=3))
    app._handle_event(_done("j0"))
    app._handle_event(_done("j3"))
    app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()
    assert app.quad.viewers[0].ribbons == 1
    assert app.quad.viewers[3].ribbons == 1


def test_a_cells_newer_fold_still_supersedes_its_own_older_one(monkeypatch):
    """The per-slot counter must keep doing what the global one did WITHIN a
    cell -- only the newest ribbon for that cell lands, and it does land.

    The OLD fold is the SLOW one, deliberately. With both builds equally
    fast, "the newest generation won" and "the last writer won" are the same
    outcome and the test cannot fail -- the mistake tests/unit/
    test_ribbon_async.py's `_VariableGeometry` was written to fix, repeated
    here per cell. Delays are keyed by path rather than by call order,
    because which of two threads reaches the callable first is not something
    a test should be asserting on by accident.
    """
    import time

    import ui.app as mod

    delays = {"/j0.cif": 0.40, "/j0b.cif": 0.01}

    def build(path, **kw):
        time.sleep(delays[path])
        return (path, "n", "c", "i")

    monkeypatch.setattr(mod, "structure_mesh", build)
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_done("j0"))
    app._handle_event(_start("j0b", card=0))
    app._handle_event(_done("j0b"))
    app._join_ribbon_workers(timeout=10.0)
    app._drain_pending_ribbon()
    # Exactly one, and identifiably the newer fold's. A bare "<= 1" cannot
    # tell "the newest landed" from "the straggler clobbered the slot and
    # then the drain threw the whole thing away", which is what removing the
    # worker's own generation check actually does.
    assert app.quad.viewers[0].ribbons == 1
    assert app.quad.viewers[0].shown == ("ribbon", "/j0b.cif")


def test_a_pending_ribbon_is_dropped_when_its_cell_moves_on_first(monkeypatch):
    """The DRAIN-side half of the generation check, which nothing else covers.

    A worker can store its result and then sit in GLib's idle queue while the
    next `job_done` on that same chip arrives -- the daemon does not wait for
    the UI. By the time the main loop runs, the stored result is a generation
    behind. Without this check it lands, and the cell shows the fold before
    last.

    The newer fold's own worker is held OPEN (it never returns), so nothing
    but the drain-side check can be what drops the older result.
    """
    import threading

    import ui.app as mod
    gate = threading.Event()

    def build(path, **kw):
        if "slow" in path:
            gate.wait(5.0)
        return (path, "n", "c", "i")

    monkeypatch.setattr(mod, "structure_mesh", build)
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_done("j0"))
    app._join_ribbon_workers(timeout=5.0)         # generation 1 is now pending

    app._handle_event(_start("j0b", card=0))
    app._handle_event({"type": "job_done", "job_id": "j0b",
                       "cif_path": "/slow.cif", "wall_s": 1.0,
                       "mean_plddt": 90.0})       # bumps this cell to gen 2
    app._drain_pending_ribbon()
    try:
        assert app.quad.viewers[0].ribbons == 0, \
            "a ribbon from the fold before last reached the screen"
    finally:
        gate.set()
        app._join_ribbon_workers(timeout=5.0)


def test_a_ribbon_that_outlasts_its_own_cells_dwell_is_dropped(monkeypatch):
    """Unchanged rule, per cell: cross-fading a finished structure over the
    next fold's live diffusion is the headline defect arriving late."""
    import ui.app as mod
    monkeypatch.setattr(mod, "structure_mesh",
                        lambda path, **kw: ("v", "n", "c", "i"))
    app = _app()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_done("j0"))
    app._join_ribbon_workers(timeout=5.0)
    app._tick_state_at(0.0)
    app._tick_state_at(99.0)              # this cell's dwell expires first
    app._drain_pending_ribbon()
    assert app.quad.viewers[0].ribbons == 0


def test_a_geometry_failure_in_one_cell_leaves_the_other_three_alone(monkeypatch):
    import ui.app as mod
    from ui.geometry import GeometryError

    def explode(path, **kw):
        if "j1" in path:
            raise GeometryError("bad cif")
        return ("v", "n", "c", "i")

    monkeypatch.setattr(mod, "structure_mesh", explode)
    app = _app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    for card in range(4):
        app._handle_event(_done(f"j{card}"))
    app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()
    assert app.quad.viewers[1].ribbons == 0
    assert app.quad.viewers[1].cleared == 0, \
        "a failed ribbon must not blank its own cell either"
    assert sum(v.ribbons for v in app.quad.viewers) == 3


def test_a_landing_ribbon_is_not_labelled_with_the_next_folds_name(monkeypatch):
    """FOUND BY LOOKING AT IT, on the real booth.

    The daemon starts the next fold on a chip the instant the last one
    finishes, so by the time a ribbon reaches the screen `current_target_id`
    usually names the fold that is STARTING. A reveal that copied it into
    `shown_target_id` put the wrong protein's name under the picture -- the
    caption said "DNA double helix" over a structure that was not one.

    `shown_target_id` belongs to the moment the picture really changes,
    which is when a frame is drawn.
    """
    import ui.app as mod
    monkeypatch.setattr(mod, "structure_mesh",
                        lambda path, **kw: ("v", "n", "c", "i"))
    app = _app()
    app._handle_event(_start("j0", card=0, target_id="trpcage"))
    _deliver(app, _frame("j0"))
    assert app._slots[0].shown_target_id == "trpcage"

    app._handle_event(_done("j0"))
    # The next fold on the same chip, before the ribbon has been built.
    app._handle_event(_start("j0b", card=0, target_id="dna"))
    app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()

    assert app._slots[0].shown_target_id == "trpcage", (
        "the structure on screen was relabelled with the next fold's name")
    assert app._slots[0].has_structure is True


# ── the focus, and the one pipeline bar ─────────────────────────────────────

def test_the_focus_cell_is_marked_on_screen():
    app = _app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    app._handle_event(_done("j2"))
    assert app.quad.focus == 2


def test_only_the_focus_cells_stages_drive_the_pipeline_panel():
    """One panel, one bar. Two jobs feeding it makes it run backwards."""
    class _Panel:
        def __init__(self):
            self.calls = []

        def set_stage_from_wire(self, stage, frac):
            self.calls.append((stage, frac))

        def reset(self):
            self.calls.append(("reset", 0.0))

        def tick(self):
            pass

    app = _app()
    app.pipeline_panel = _Panel()
    app._handle_event(_start("j0", card=0))
    app._handle_event(_start("j1", card=1))
    app._handle_event(_done("j0"))          # focus becomes slot 0
    app.pipeline_panel.calls.clear()
    app._handle_event(_stage("j1", "diffusion", 0.5))
    assert app.pipeline_panel.calls == []
    app._handle_event(_stage("j0", "diffusion", 0.6))
    # The WIRE fraction, unconverted: set_stage_from_wire is the one place
    # the whole-fold -> within-stage conversion happens, which is exactly
    # why this call site uses it and never set_stage.
    assert app.pipeline_panel.calls == [("diffusion", pytest.approx(0.6))]


# ── what a cell says ────────────────────────────────────────────────────────

def test_every_cell_gets_its_own_stage_caption():
    app = _app()
    app._handle_event(_start("j1", card=1))
    app._handle_event(_stage("j1", "diffusion", 0.55))
    assert "DIFFUSION" in app.quad.captions[1].upper()
    assert 0 not in app.quad.captions


def test_a_malformed_event_still_never_reaches_the_screen_as_text():
    app = _app()
    app._handle_event(_error("j0", message="/secret/path exploded"))
    assert all("/secret/path" not in (t or "")
               for t in app.quad.captions.values())


def test_a_stage_name_the_protocol_does_not_know_is_not_printed():
    """`stage` is wire data. Anything outside STAGE_ORDER is dropped rather
    than rendered, which is what keeps a runner-supplied string off the
    screen even on the one path that shows a word from the wire.

    Compared case-INSENSITIVELY: the caption upper-cases the stage, so a
    case-sensitive check passes against an implementation that prints the
    whole string. (Found by the mutation sweep; the first version of this
    test could not fail.)
    """
    app = _app()
    app._handle_event(_start("j1", card=1))
    app._handle_event(_stage("j1", "/var/run/secret exploded", 0.5))
    printed = (app.quad.captions.get(1) or "").lower()
    assert "secret" not in printed and "/var/run" not in printed


def test_a_panel_failure_does_not_freeze_the_state_tick():
    class _Exploding:
        def set_stage_from_wire(self, *a):
            raise RuntimeError("boom")

        def reset(self):
            raise RuntimeError("boom")

        def tick(self):
            raise RuntimeError("boom")

    app = _app()
    app.pipeline_panel = _Exploding()
    assert app._tick_state() is True
    app._handle_event(_start("j0", card=0))       # must not raise

    # The panel is reset BEFORE the cell's own bookkeeping and captions, so
    # an unguarded panel call does not merely log -- it costs the fold
    # everything after it in the branch. The router runs first and would
    # still look right, which is why asserting on it proves nothing (the
    # mutation sweep caught exactly that).
    assert app._slots[0].awaiting_first_frame is True, \
        "the exploding panel cost the fold its 'now folding' state"
    assert 0 in app.quad.captions, "...and its caption"
    assert app.quad.focus == 0


def test_the_connection_state_reaches_every_cell():
    app = _app()
    app._on_state("connected")
    assert all(v.connection_state == "connected" for v in app.quad.viewers)


# ── the toggle ──────────────────────────────────────────────────────────────

def test_the_booth_starts_on_the_single_large_view():
    """One protein, large, is what a visitor walks up to -- ON A ONE-CHIP
    BOOTH.

    This used to be unconditional. It stopped being the default on
    2026-08-24: a booth with several chips now comes up in the grid, by
    request, because coming up solo meant a four-chip booth showed one
    protein unless somebody remembered `--quad`. The single-chip case is
    unchanged, and is what this now pins; the multi-chip decision is covered
    under "the start view decides itself" below.
    """
    app = _app(cards=(0,))
    assert app.quad_visible is False


def test_q_toggles_the_quad_and_toggles_it_back():
    """Written against the START STATE rather than a literal, so it keeps
    testing the toggle if the default view changes again -- which it did once
    already (a multi-chip booth now starts in the grid)."""
    app = _app()
    start = app.quad_visible
    app._handle_key("q")
    assert app.quad_visible is not start
    assert app.quad.solo_calls[-1] is start, \
        "solo_mode should be the opposite of what is now visible"
    app._handle_key("q")
    assert app.quad_visible is start
    assert app.quad.solo_calls[-1] is not start


def test_q_is_documented_on_the_help_card():
    """A key that exists but is not on the card is folklore. Task 14 shipped
    the copy in ui/quad.py and flagged exactly this as the handoff."""
    from ui.app import _HELP_PANELS, _KEY_HELP
    from ui.quad import QUAD_HELP_LINE
    assert QUAD_HELP_LINE in _HELP_PANELS
    assert any(keys.strip().lower() == "q" for keys, _meaning in _KEY_HELP)


def test_q_with_ctrl_still_quits_rather_than_toggling(monkeypatch):
    """The near-miss that matters: `Ctrl+Q` is quit and must not be caught by
    the plain-letter branch."""
    app = _app()
    quits = []
    monkeypatch.setattr(type(app), "quit", lambda self: quits.append(True))
    before = app.quad_visible
    app._handle_key("q", ctrl=True)
    assert quits == [True]
    assert app.quad_visible is before, "Ctrl+Q touched the view"


def test_q_does_not_open_the_gallery():
    """Every unbound plain key is a visitor touch. `Q` is bound, so it must
    not also be one -- a booth operator checking the chips has not asked for
    the gallery."""
    app = _app()
    before = app.display_state
    app._handle_key("q")
    assert app.display_state == before


def test_the_quad_is_not_closed_by_escape():
    """Deliberate, and pinned so it is a decision rather than an omission:
    the quad is a VIEW of the same four folds, not chrome laid over them, so
    there is nothing for Escape to get out of the way of."""
    app = _app()
    if not app.quad_visible:            # a one-chip booth starts solo
        app._handle_key("q")
    assert app.quad_visible is True, "setup failed: the quad is not open"
    app._handle_key("escape")
    assert app.quad_visible is True


# ── starting in the quad (`--quad`) ─────────────────────────────────────────
#
# `Q` toggles the grid, which is right for a visitor and wrong for two other
# people: an operator running a four-chip booth who wants the grid up all
# day, and anyone recording the booth, for whom "press a key at the right
# moment" is a step that fails silently. Both want the START state to be a
# choice rather than a fixed default -- so it is a flag, and these tests pin
# that the flag reaches the WIDGET and not merely the bool beside it.

def test_the_booth_can_be_started_in_the_quad():
    app = DemoApp(socket_path=None, quad=True)
    assert app.quad_visible is True


def test_the_flag_defaults_off_so_the_booth_is_unchanged():
    """The single large protein stays what a visitor walks up to."""
    assert DemoApp(socket_path=None).quad_visible is False


def test_a_quad_built_after_the_flag_comes_up_showing_all_four():
    """The one that can actually fail: the widget is built later, on the
    daemon's `hello`, long after the flag was read. If the flag only sets the
    bool, `--quad` gives you a booth that says it is in the grid and shows
    one protein -- and `Q` would then have to be pressed twice to fix it.

    Driven through the REAL `_ensure_quad` with a REAL `QuadView`, because a
    fake quad is deliberately never replaced by that method, so a fake here
    would assert nothing.
    """
    from gi.repository import Gtk
    from ui.quad import QuadView

    app = DemoApp(socket_path=None, quad=True)
    app._viewer_page = Gtk.Overlay()
    app._ensure_quad([0, 1, 2, 3])

    assert isinstance(app.quad, QuadView)
    assert app.quad.solo_mode is False, \
        "--quad built a quad that is still showing one cell"


def test_q_still_toggles_from_a_booth_started_in_the_quad():
    """The flag chooses the START, not a lock: the first press must turn the
    grid OFF. A flag that set the bool without the widget agreeing would need
    two presses, which is the same defect as above seen from the keyboard."""
    app = _app()
    app.quad_visible = True          # as `--quad` leaves it
    app._handle_key("q")
    assert app.quad_visible is False
    assert app.quad.solo_calls[-1] is True


# ── a held structure must not wear the incoming fold's name ─────────────────
#
# `ui/quad.py`'s own module docstring already states this rule, for the notice
# row: rendering it into a cell's caption "would label whatever cell 0 actually
# IS folding with the wrong protein's name". This is that rule from the other
# direction, and the quad screenshots caught it.
#
# Only `diffusion` produces coordinates, so a cell spends the whole `trunk`
# stage drawing the PREVIOUS fold on that chip (the hold-until-superseded
# behaviour -- deliberate, and the alternative is a black cell). But the
# caption named `current_target_id` first, so the screen read
# "Dihydrofolate Reductase - TRUNK" underneath a picture of trypsin.
#
# In solo view the notice row disambiguates ("Previous fold: X / Now folding
# Y"). A quad cell has one line and four cells need four different answers, so
# the line itself has to carry both.

def test_cell_caption_names_the_structure_that_is_actually_on_screen():
    from ui.app import cell_caption
    text = cell_caption(name="Dihydrofolate Reductase", stage="trunk",
                        showing="Trypsin")
    assert text.startswith("Trypsin"), \
        f"the caption leads with the fold that is NOT drawn: {text!r}"
    assert "Dihydrofolate Reductase" in text, "what is computing went missing"
    assert "TRUNK" in text


def test_cell_caption_is_unchanged_when_the_cell_shows_its_own_fold():
    """The common case must not grow a clause. Once diffusion starts, the
    structure on screen IS the current fold and there is nothing to explain."""
    from ui.app import cell_caption
    assert cell_caption(name="Trypsin", stage="diffusion",
                        showing="Trypsin") == "Trypsin · DIFFUSION"
    assert cell_caption(name="Trypsin", stage="diffusion") == "Trypsin · DIFFUSION"


def test_a_cell_in_trunk_says_which_protein_it_is_drawing():
    """The real path, not the pure function: chip 1 finishes trypsin, then
    starts DHFR. Through the whole of DHFR's trunk the cell draws trypsin, so
    the cell must not claim to be showing DHFR."""
    import pathlib as _pathlib

    from ui.playlist import Target

    def _t(tid, name):
        return Target(id=tid, input_path=_pathlib.Path(f"{tid}.yaml"),
                      model="protenix-v2", name=name, blurb="")

    app = _app()
    app.targets = [_t("trypsin", "Trypsin"), _t("dhfr", "Dihydrofolate Reductase")]
    app._handle_event(_start("j1", card=1, target_id="trypsin"))
    _deliver(app, _frame("j1"))
    app._handle_event(_done("j1"))
    app._handle_event(_start("j2", card=1, target_id="dhfr"))
    app._handle_event(_stage("j2", "trunk", 0.12))

    caption = app.quad.captions[1]
    assert "trypsin" in caption.lower(), \
        f"the cell is drawing trypsin and does not say so: {caption!r}"
    assert "TRUNK" in caption.upper()


def test_an_empty_cell_does_not_claim_to_be_showing_a_structure():
    """The guard on `has_structure`, pinned directly.

    `shown_target_id` outlives the geometry it names -- it is a plain id, and
    nothing clears it -- so the caption asks whether anything is ACTUALLY
    drawn before naming what. Without that, a cleared cell would caption
    itself "Trypsin — now folding DHFR" over empty space, which is a worse
    lie than the one this whole section exists to fix.

    The state is driven directly because it is currently UNREACHABLE: the one
    path that clears a cell (`ui/app.py`, the `clear_structure()` in the frame
    handler) calls `set_points` and restores `has_structure` inside the same
    call, so no observer ever sees the gap. That makes the corresponding
    mutation equivalent, and this test is what stops the guard being deleted
    as dead code the first time someone adds a second clearing path.
    """
    import pathlib as _pathlib

    from ui.playlist import Target

    def _t(tid, name):
        return Target(id=tid, input_path=_pathlib.Path(f"{tid}.yaml"),
                      model="protenix-v2", name=name, blurb="")

    app = _app()
    app.targets = [_t("trypsin", "Trypsin"), _t("dhfr", "Dihydrofolate Reductase")]
    app._handle_event(_start("j1", card=1, target_id="trypsin"))
    _deliver(app, _frame("j1"))
    app._handle_event(_done("j1"))
    app._handle_event(_start("j2", card=1, target_id="dhfr"))

    view = app._slot_view(1)
    assert view.shown_target_id == "trypsin", "fixture did not reach the state"
    assert view.has_structure is True, \
        "invariant changed: shown_target_id no longer implies has_structure"

    # The cell loses its geometry while the id it was drawn from remains.
    view.has_structure = False
    app._sync_cell_caption(1, stage="trunk")

    caption = app.quad.captions[1]
    assert "Trypsin" not in caption, \
        f"empty cell claims to be showing trypsin: {caption!r}"
    assert "Dihydrofolate Reductase" in caption, "lost what IS computing"


# ── the start view decides itself ───────────────────────────────────────────
#
# Asked for on 2026-08-24: "I like 4 chip by default when available." Solo was
# the default and `--quad` was the only way to change it, which meant a
# four-chip booth came up showing one protein unless somebody remembered a
# flag. The chip count cannot be known at construction -- chips register one
# at a time as the daemon names them (`_note_card`) -- so this is decided as
# they arrive, and decided ONCE.

def test_a_multi_chip_booth_comes_up_in_the_quad():
    app = DemoApp(socket_path=None)
    assert app.quad_visible is False, "nothing known yet; solo is right"
    app.attach_cards([0, 1, 2, 3])
    assert app.quad_visible is True


def test_a_single_chip_booth_stays_solo():
    """One chip has nothing to put in the other three cells."""
    app = DemoApp(socket_path=None)
    app.attach_cards([0])
    assert app.quad_visible is False


def test_a_one_chip_booth_that_grows_still_gets_the_quad():
    """The real startup order: the router hears about chip 0, then the daemon
    names the rest one at a time. A single known chip must NOT count as a
    decision, or a four-chip booth that registers gradually stays solo."""
    app = DemoApp(socket_path=None)
    app.attach_cards([0])
    assert app.quad_visible is False
    app._note_card(1)
    assert app.quad_visible is True, \
        "the second chip should have flipped the booth into the quad"


def test_pressing_q_beats_a_chip_arriving_later():
    """A visitor or operator who has chosen a view keeps it. Without this the
    next chip to register would yank the view back."""
    app = DemoApp(socket_path=None)
    app.attach_cards([0])
    app._set_quad_visible(True)          # somebody pressed Q
    app._set_quad_visible(False)         # ...and pressed it again
    app._note_card(1)
    assert app.quad_visible is False, "auto overruled a manual choice"


def test_solo_is_forced_when_asked_for():
    """`--solo` on a booth that would otherwise pick the quad."""
    app = DemoApp(socket_path=None, quad=False)
    app.attach_cards([0, 1, 2, 3])
    assert app.quad_visible is False


def test_quad_is_forced_even_on_one_chip():
    """`--quad` still means what it meant: the grid, whatever the chip count.
    One cell in a 2x2 is a legitimate thing to want when recording."""
    app = DemoApp(socket_path=None, quad=True)
    app.attach_cards([0])
    assert app.quad_visible is True


def test_auto_decides_only_once():
    """Having chosen the quad, a further chip must not re-run the decision --
    it would stamp on a `Q` press made in between."""
    app = DemoApp(socket_path=None)
    app.attach_cards([0, 1])
    assert app.quad_visible is True
    app._set_quad_visible(False)
    app._note_card(2)
    assert app.quad_visible is False
