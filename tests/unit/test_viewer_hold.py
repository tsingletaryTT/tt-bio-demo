"""The viewer is never empty while a fold is in flight.

The defect these tests exist to keep fixed, measured on a 91-second
recording of the real booth scanned frame by frame: **20 of 45 sampled
seconds had an empty viewer**, and a captured frame at t=44s shows trypsin
at `TRUNK ~60%`, diffusion not started, and nothing at all in the 3D view.

The mechanism is not subtle once you look for it. Only the `diffusion` stage
emits `frame` events; `msa`, `prep` and `trunk` emit progress and no
coordinates. Trunk is ten refinement cycles -- about 15 seconds on a
223-residue target. So the old sequencing, which cleared the viewer at
`job_start` (or two seconds later, at the end of the showcase dwell -- the
difference is 2 seconds out of 15), spent most of every long fold showing a
visitor a progress bar and a black field. Measured warm fold times on this
booth: Trp-cage 4.4s, FKBP12 11.7s, DHFR 19.7s, trypsin 22.3s -- three of
the four shipped targets.

What these tests assert, and why it is not a call count
-------------------------------------------------------
Every assertion below is about WHAT THE VIEWER HOLDS -- `FakeViewer.shown`,
which models the real widget's `_point_count`/`_ribbon_index_count` (both
zeroed by `clear_structure`, one set by each upload). Counting
`clear_structure` calls cannot tell "the previous protein is still on
screen" from "the screen is black", and the second is the entire defect. A
test that counted calls would have scored the shipped, broken build green,
which is the failure mode docs/followups.md keeps a list of.

The long silent phase is SYNTHESISED (`_silent_phase`), and deliberately so:
the recorded fixture is a 20-residue Trp-cage whose whole trunk is under a
second, so replaying it cannot reproduce this defect at all. What is
synthesised is stage PROGRESS -- exactly what the daemon really sends during
those stages, and exactly as devoid of coordinates. Nothing here fabricates
geometry, and neither does the code under test: everything the viewer is
asserted to be holding is a structure that was really computed, from the
recorded real fold.
"""

import json
import pathlib

import numpy as np
import pytest

from protocol.events import pack_coords, unpack_coords
from ui import app as app_module
from ui.app import viewer_hold_caption
from ui.playlist import Target

# The wiring tests' fakes and event builders. Reused rather than
# re-declared: what a `FakeViewer` models is one decision (see that file's
# module docstring), and a second copy of it here is exactly the drift
# docs/followups.md warns about.
from test_app_wiring import (  # noqa: F401
    FakeClock, _app, _cell, _job_done, _job_start, _land_ribbon, fake_ribbon,
)

_FIXTURE = (pathlib.Path(__file__).resolve().parents[1]
            / "fixtures" / "streams" / "real_fold_trpcage.jsonl")


def real_frames():
    """The 30 recorded diffusion frames of a real Trp-cage fold on silicon.

    Real coordinates, with the real per-frame noise and the real flyer
    atoms. Used here for the same reason test_viewer_camera.py uses them:
    so "the viewer is holding the previous structure" is a claim about
    something that was actually computed, and so the two folds in each test
    below hold visibly DIFFERENT arrays -- a synthetic constant cloud would
    make "held the old one" and "drew the new one" indistinguishable.
    """
    frames = []
    for line in _FIXTURE.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event["type"] == "frame":
            frames.append(unpack_coords(event["coords_b64"]))
    assert len(frames) == 30, "fixture changed shape"
    return frames


def _frame(job_id, coords):
    """A `frame` event carrying real recorded coordinates."""
    arr = np.asarray(coords, dtype=np.float32)
    return {"type": "frame", "job_id": job_id, "step": 0, "total": 200,
            "n_atoms": len(arr), "coords_b64": pack_coords(arr)}


def _deliver(app, event):
    """Put a frame through the app's real path: the socket-side callback
    (which buffers it) and then the 33ms drain source (which draws it).
    Never `viewer.set_points` directly -- the suppression and the handover
    both live in `_drain_frames`, and reaching past it would test nothing.
    """
    app._on_event(event)
    app._drain_frames()


def _target(target_id, name):
    return Target(id=target_id, input_path=pathlib.Path(f"{target_id}.yaml"),
                  model="protenix-v2", name=name, blurb="")


def _silent_phase(app, clock, *, job_id, seconds, stage="trunk",
                  tick_ms=100, stage_every_ms=500):
    """Replay a long stage-only phase: progress, and NOT ONE COORDINATE.

    This is the shape of the defect, and it is not in any recorded fixture
    this project has -- a 20-residue peptide's trunk is under a second. It
    is synthesised from the measured numbers in the brief instead: the
    daemon emits `stage` events with a whole-fold `frac` throughout msa,
    prep and trunk, and emits no `frame` at all until diffusion starts.

    Runs the app's own 100ms state tick throughout, because that tick is
    what ends the showcase dwell -- the exact moment the old code went
    black. Yields the clock reading after every tick so a caller can assert
    on EVERY sampled instant rather than only on the end state.
    """
    started_ms = clock.t * 1000.0
    elapsed_ms = 0.0
    next_stage_ms = 0.0
    while elapsed_ms < seconds * 1000.0:
        clock.advance(tick_ms / 1000.0)
        elapsed_ms += tick_ms
        if elapsed_ms >= next_stage_ms:
            next_stage_ms += stage_every_ms
            app._handle_event({
                "type": "stage", "job_id": job_id, "stage": stage,
                # A whole-fold fraction, as the wire carries it, ramping
                # across the silent phase the way a real trunk does.
                "frac": 0.10 + 0.55 * (elapsed_ms / (seconds * 1000.0)),
            })
        app._tick_state()
        yield (started_ms + elapsed_ms) / 1000.0


def _fold_to_a_finished_ribbon(app, *, job_id, target_id, coords):
    """Drive one complete fold: start, one real diffusion frame, done, and
    the ribbon landing on screen. Returns what the viewer ends up holding.
    """
    app._handle_event(_job_start(job_id, target_id=target_id))
    _deliver(app, _frame(job_id, coords))
    app._handle_event(_job_done(job_id))
    _land_ribbon(app)
    assert _cell(app).shown is not None and _cell(app).shown[0] == "ribbon", \
        "setup failed: the fold did not end with a ribbon on screen"
    return _cell(app).shown


# ---------------------------------------------------------------------------
# The four the brief names
# ---------------------------------------------------------------------------

def test_a_long_silent_trunk_holds_the_previous_structure_instead_of_going_black(
        fake_ribbon):
    """The headline defect, reproduced at the measured duration.

    Fifteen seconds of trunk with no coordinates at all, on a booth that
    has a finished structure from the previous fold already on screen. The
    viewer must be showing that structure at EVERY sampled instant --
    dimmed and captioned, but never empty.

    Mutation this catches: clearing the viewer at `job_start`, in either of
    the two shapes the old code had it -- immediately (`clear_structure()`
    in the `job_start` branch) or deferred to the end of the showcase dwell
    (`_deferred_clear`, applied from `_sync_to_state`). Both leave the
    assertion below failing from about the 2-second mark onward, which is
    also precisely how the shipped booth failed.
    """
    clock = FakeClock()
    app = _app(clock)
    app.targets = [_target("trpcage", "Trp-cage"), _target("trypsin", "Trypsin")]

    ribbon = _fold_to_a_finished_ribbon(
        app, job_id="n", target_id="trpcage", coords=real_frames()[-1])

    app._handle_event(_job_start("n+1", target_id="trypsin"))

    blank_at = []
    wrong_at = []
    for now in _silent_phase(app, clock, job_id="n+1", seconds=15.0):
        if _cell(app).shown is None:
            blank_at.append(round(now, 1))
        elif _cell(app).shown != ribbon:
            wrong_at.append(round(now, 1))

    assert not blank_at, (
        f"the viewer was EMPTY at {len(blank_at)} of 150 sampled instants "
        f"during a silent trunk (first at t={blank_at[0]}s) -- this is the "
        "booth defect: a progress bar and a black field")
    assert not wrong_at, (
        f"the held structure was replaced by something else at {wrong_at[:3]} "
        "with no new coordinates having arrived")
    assert _cell(app).shown == ribbon


def test_the_held_structure_is_replaced_by_the_first_real_frame_and_not_before(
        fake_ribbon):
    """Not before: no coordinates, no clear -- the previous structure keeps
    the screen for the whole silent phase. Not later: the very first frame
    that arrives is the one that replaces it, in the same instant, with no
    blank between them.

    Mutations this catches: clearing on `job_start` (fails "not before");
    dropping the `clear_structure()` from `_drain_frames`'s first-frame
    handover, so the new fold's cloud is drawn under a stale ribbon that
    never leaves (fails "not later" -- `shown` stays the ribbon because the
    blend is still 1); and deferring the handover past the first frame.
    """
    clock = FakeClock()
    app = _app(clock)
    frames = real_frames()

    ribbon = _fold_to_a_finished_ribbon(
        app, job_id="n", target_id="trpcage", coords=frames[-1])
    clears_after_setup = _cell(app).clears

    app._handle_event(_job_start("n+1", target_id="trypsin"))
    for _ in _silent_phase(app, clock, job_id="n+1", seconds=15.0):
        pass

    # Not before.
    assert _cell(app).shown == ribbon
    assert _cell(app).clears == clears_after_setup, \
        "the viewer was cleared while there was nothing to put in its place"

    # Not later: the FIRST frame does it.
    opening_cloud = frames[0]
    _deliver(app, _frame("n+1", opening_cloud))

    assert _cell(app).clears == clears_after_setup + 1, \
        "the first real frame did not retire the held structure"
    kind, payload = _cell(app).shown
    assert kind == "points"
    assert np.array_equal(payload, opening_cloud), \
        "the viewer is not showing the new fold's own first frame"
    assert _cell(app).blend == 0.0, \
        "the new fold's cloud is being drawn under the old fold's ribbon"
    assert _cell(app).held is False, \
        "the new fold's live diffusion is still being dimmed as a leftover"


def test_the_very_first_fold_of_the_day_says_what_it_is_doing(fake_ribbon):
    """Nothing has been folded yet, so there is nothing honest to hold. The
    requirement then is not a picture -- inventing one would be a lie -- but
    that the booth says what it is doing and when the view will fill, rather
    than showing a bare black field for fifteen seconds.

    Mutations this catches: no caption at all (the empty branch of
    `viewer_hold_caption` returning None); the caption naming the target it
    is HOLDING rather than the one it is folding, which is nothing here.
    """
    clock = FakeClock()
    app = _app(clock)
    app.targets = [_target("trypsin", "Trypsin")]

    app._handle_event(_job_start("first", target_id="trypsin"))

    for _ in _silent_phase(app, clock, job_id="first", seconds=15.0):
        assert _cell(app).shown is None, \
            "nothing has been computed yet -- anything on screen is invented"
        assert app._caption == ("Folding Trypsin",
                                app_module._CAPTION_EMPTY_SUB), \
            "the first fold's empty viewer says nothing about what it is doing"

    # And it fills, with the fold's own first real coordinates.
    opening_cloud = real_frames()[0]
    _deliver(app, _frame("first", opening_cloud))
    assert _cell(app).shown[0] == "points"
    assert np.array_equal(_cell(app).shown[1], opening_cloud)
    assert app._caption is None, "the caption outlived the empty screen"


def test_a_job_error_mid_flight_leaves_no_in_flight_claim_over_the_held_structure(
        fake_ribbon):
    """A fold that fails will never produce a frame, so it can never
    supersede what is on screen by the ordinary route. The held structure
    stays -- it is real and was really computed, and blanking it would trade
    an honest old protein for the empty viewer this whole change removes --
    but the booth must stop asserting that a fold is running, because one is
    not. Otherwise a daemon that dies here leaves a stale structure under a
    permanent "Now folding X".

    Mutations this catches: `job_error` not calling `_end_fold_in_flight`
    (the caption keeps claiming a fold that is over -- checked ten seconds
    later, long after the failure); and `job_error` blanking the viewer
    instead, which is the defect wearing the opposite hat.
    """
    clock = FakeClock()
    app = _app(clock)
    app.targets = [_target("trpcage", "Trp-cage"), _target("trypsin", "Trypsin")]

    ribbon = _fold_to_a_finished_ribbon(
        app, job_id="n", target_id="trpcage", coords=real_frames()[-1])

    app._handle_event(_job_start("n+1", target_id="trypsin"))
    for _ in _silent_phase(app, clock, job_id="n+1", seconds=4.0):
        pass
    assert app._caption == ("Previous fold: Trp-cage", "Now folding Trypsin"), \
        "setup failed: the in-flight claim was never made in the first place"

    app._handle_event({
        "type": "job_error", "job_id": "n+1", "target_id": "trypsin",
        # Deliberately the shape of the thing that must never reach a
        # screen: a runner-supplied message with a traceback in it.
        "message": 'Traceback (most recent call last):\n  RuntimeError: '
                   'card 2 quarantined mid-fold',
    })

    assert app._caption is None, \
        "the booth is still claiming to fold a job that failed"
    assert _cell(app).held is False, \
        "the structure is still dimmed as a leftover with nothing superseding it"

    # Ten more seconds with a dead daemon: still honest, still not blank.
    for _ in _silent_phase(app, clock, job_id="n+1", seconds=10.0,
                           stage_every_ms=10_000_000):
        assert _cell(app).shown == ribbon, \
            "a failed fold blanked the last real structure the booth had"
        assert app._caption is None

    assert "Traceback" not in repr(app._caption)
    assert "quarantined" not in repr(app._caption)


# ---------------------------------------------------------------------------
# The straggler: whose frame is this, actually?
# ---------------------------------------------------------------------------

def test_a_straggler_frame_from_the_previous_fold_cannot_pose_as_the_new_folds_first(
        fake_ribbon):
    """The daemon does not stop the world between folds, so a frame emitted
    by fold N can arrive after fold N+1's `job_start`. Treating it as fold
    N+1's first frame would retire a finished structure in favour of the
    OLDER fold's noise cloud -- an honest picture of the wrong thing, and
    strictly worse than what it replaced.

    The two frames here carry DIFFERENT recorded coordinates (the fixture's
    first and last), so "held the ribbon", "drew the straggler" and "drew
    the new fold's frame" are three distinguishable outcomes rather than
    two indistinguishable ones.

    Mutation this catches: dropping the `job_id` comparison from
    `_drain_frames`'s first-frame handover, i.e. letting any frame at all
    perform it.
    """
    clock = FakeClock()
    app = _app(clock)
    frames = real_frames()

    ribbon = _fold_to_a_finished_ribbon(
        app, job_id="n", target_id="trpcage", coords=frames[-1])
    clears_after_setup = _cell(app).clears

    app._handle_event(_job_start("n+1", target_id="trypsin"))
    for _ in _silent_phase(app, clock, job_id="n+1", seconds=3.0):
        pass

    # Fold N's straggler, arriving late.
    _deliver(app, _frame("n", frames[-2]))
    assert _cell(app).shown == ribbon, \
        "a frame from the PREVIOUS fold retired the finished structure"
    assert _cell(app).clears == clears_after_setup

    # Fold N+1's own first frame, arriving next.
    _deliver(app, _frame("n+1", frames[0]))
    assert _cell(app).shown[0] == "points"
    assert np.array_equal(_cell(app).shown[1], frames[0])
    assert _cell(app).clears == clears_after_setup + 1


def test_a_frame_with_no_job_id_holds_rather_than_guessing(fake_ribbon):
    """The one rule in this file that MULTI-CHIP FOLDING CHANGED, and it is
    written down here rather than quietly deleted.

    While the booth folded on one chip there was only one cell a frame could
    belong to, so a frame with no `job_id` was accepted: "cannot tell" must
    not become "show nothing". With four cells that argument runs the other
    way. The router binds a job to a cell at `job_start` -- the only event
    carrying a `card` -- so an anonymous frame belongs to NO cell, and the
    only way to draw it is to pick one, which means drawing one fold's
    coordinates under another fold's caption in front of a visitor. Holding
    the last real structure is the honest answer, and it is the answer this
    whole file exists to make the default.

    What is NOT allowed is for it to blank anything: the previous structure
    stays exactly as it was, dimmed and captioned.

    Mutation this catches: routing an unroutable frame to slot 0.
    """
    clock = FakeClock()
    app = _app(clock)
    frames = real_frames()

    ribbon = _fold_to_a_finished_ribbon(app, job_id="n", target_id="trpcage",
                                        coords=frames[-1])
    app._handle_event(_job_start("n+1", target_id="trypsin"))
    for _ in _silent_phase(app, clock, job_id="n+1", seconds=3.0):
        pass
    clears_before = _cell(app).clears

    anonymous = _frame("n+1", frames[0])
    del anonymous["job_id"]
    _deliver(app, anonymous)

    assert _cell(app).shown == ribbon, \
        "the held structure was replaced by a frame belonging to no cell"
    assert _cell(app).clears == clears_before, \
        "an unroutable frame blanked a cell"

    # ...and the fold's OWN next frame, which does carry its job id, still
    # performs the handover. Without this the test above would also pass
    # against a booth that had stopped drawing frames altogether.
    _deliver(app, _frame("n+1", frames[0]))
    assert _cell(app).shown[0] == "points"
    assert np.array_equal(_cell(app).shown[1], frames[0])


# ---------------------------------------------------------------------------
# The hero moment is still the hero moment
# ---------------------------------------------------------------------------

def test_the_showcase_dwell_is_neither_dimmed_nor_captioned(fake_ribbon):
    """The two seconds a finished structure is guaranteed
    (`_SHOWCASE_DWELL_S`) are the payoff the booth is built around. During
    them the structure IS the current subject: full brightness, nothing
    written over it. The caption and the dim appear exactly where the old
    code went black -- when the dwell ends and the structure becomes a
    leftover being held.

    Mutation this catches: `viewer_hold_caption` ignoring `showcasing`, so
    the hero image is demoted to "Previous fold: ..." the instant it
    finishes fading in -- while it is still the newest thing the booth has.
    """
    clock = FakeClock()
    app = _app(clock)
    app.targets = [_target("trpcage", "Trp-cage"), _target("trypsin", "Trypsin")]

    _fold_to_a_finished_ribbon(app, job_id="n", target_id="trpcage",
                              coords=real_frames()[-1])
    app._handle_event(_job_start("n+1", target_id="trypsin"))

    # Inside the dwell.
    app._tick_state()
    # effective_dwell_s, not showcase_dwell_s: the latter is the MAXIMUM a
    # showcase may take, and this fixture's incoming target carries no
    # measured first_frame_s, so its hold is capped to the floor.
    clock.advance(app.states.effective_dwell_s - 0.3)
    app._tick_state()
    assert app.states.state == "showcase"
    assert app._caption is None, "the hero image was captioned as a leftover"
    assert _cell(app).held is False, "the hero image was dimmed"

    # Past it.
    clock.advance(0.5)
    app._tick_state()
    assert app.states.state == "attract"
    assert app._caption == ("Previous fold: Trp-cage", "Now folding Trypsin")
    assert _cell(app).held is True, \
        "a held leftover is being shown at the same brightness as a live fold"


def test_not_ready_takes_the_in_flight_claim_down_with_it(fake_ribbon):
    """`not_ready` means the daemon cannot fold at all. Whatever the viewer
    holds stays (blanking it only adds an empty screen behind the preparing
    overlay), but "Now folding X" stops being true the moment it arrives.

    Mutation this catches: `not_ready` not calling `_end_fold_in_flight`,
    leaving a fold-in-progress claim printed over a booth that has told the
    visitor it is not ready.
    """
    clock = FakeClock()
    app = _app(clock)
    app.targets = [_target("trpcage", "Trp-cage"), _target("trypsin", "Trypsin")]

    ribbon = _fold_to_a_finished_ribbon(app, job_id="n", target_id="trpcage",
                                        coords=real_frames()[-1])
    app._handle_event(_job_start("n+1", target_id="trypsin"))
    for _ in _silent_phase(app, clock, job_id="n+1", seconds=3.0):
        pass
    assert app._caption is not None

    app._handle_event({"type": "not_ready", "missing": ["/opt/weights/protenix"]})

    assert app.states.state == "preparing"
    assert app._caption is None
    assert _cell(app).shown == ribbon
    assert _cell(app).held is False


# ---------------------------------------------------------------------------
# What the caption is allowed to say
# ---------------------------------------------------------------------------

def test_the_caption_is_silent_when_there_is_nothing_to_explain():
    """No fold awaiting its first frame means the viewer is showing the
    current thing, and a caption over it would be noise at best and a false
    claim at worst.

    Mutation this catches: `viewer_hold_caption` ignoring
    `awaiting_first_frame`, which would leave "Previous fold: X" printed
    over X itself for the whole of its own showcase and beyond.
    """
    assert viewer_hold_caption(
        awaiting_first_frame=False, has_structure=True, showcasing=False,
        folding_name="Trypsin", held_name="Trp-cage") is None


def test_the_caption_names_the_held_fold_and_the_running_one_separately():
    """Both halves of the honesty requirement in one string pair: what you
    are looking at, and what the booth is actually computing. The pipeline
    panel beside it is reporting the RUNNING fold's stage, so a caption that
    named only one of the two would make those two surfaces read as one
    claim about one protein.

    Mutation this catches: swapping the two names, or dropping either line.
    """
    assert viewer_hold_caption(
        awaiting_first_frame=True, has_structure=True, showcasing=False,
        folding_name="Trypsin", held_name="Trp-cage",
    ) == ("Previous fold: Trp-cage", "Now folding Trypsin")


def test_the_caption_claims_less_rather_than_guessing_when_a_name_is_unknown():
    """`target_id` is wire data: a daemon folding something outside this
    booth's playlist has no display name here. Every line degrades to a
    claims-less form -- never to a raw id, never to a name for the wrong
    protein.

    Mutation this catches: falling back to the `target_id` itself, which
    puts `trpcage_no_msa` on a conference screen; or f-string-ing a None
    into the copy ("Now folding None").
    """
    held = viewer_hold_caption(
        awaiting_first_frame=True, has_structure=True, showcasing=False,
        folding_name=None, held_name=None)
    assert held == ("Previous fold", "Now folding the next target")

    empty = viewer_hold_caption(
        awaiting_first_frame=True, has_structure=False, showcasing=False,
        folding_name=None, held_name=None)
    assert empty == ("Folding", app_module._CAPTION_EMPTY_SUB)

    for line in held + empty:
        assert "None" not in line


def test_an_unknown_target_id_never_reaches_the_screen(fake_ribbon):
    """The same rule, through the app rather than the pure function: a fold
    of something this booth's playlist does not list must not print its id.

    Mutation this catches: `_target_name` returning `target_id` as its
    fallback instead of None.
    """
    clock = FakeClock()
    app = _app(clock)
    app.targets = [_target("trpcage", "Trp-cage")]

    app._handle_event(_job_start("x", target_id="7ahl_chain_a_no_msa"))
    for _ in _silent_phase(app, clock, job_id="x", seconds=3.0):
        pass

    assert app._caption == ("Folding", app_module._CAPTION_EMPTY_SUB)
    assert "7ahl" not in repr(app._caption)


# ---------------------------------------------------------------------------
# The viewer's half: a held structure is dimmed, and nothing else changes
# ---------------------------------------------------------------------------

class _RenderProbe:
    """Duck-typed stand-in for StructureViewer, carrying only what
    `_on_render` touches, with the two draw calls replaced by recorders.

    Same approach as test_viewer_camera.py's `_FakeViewer` and for the same
    reason: constructing a real StructureViewer needs a live display and a
    GL context. `_on_render` itself is the real, unmodified production
    body.
    """

    from ui.viewer import StructureViewer as _real

    _on_render = _real._on_render

    def __init__(self, *, blend, held, point_opacity=1.0):
        self._ready = True
        self._pending_points = None
        self._pending_ribbon = None
        self._blend = blend
        self._held = held
        self._point_opacity = point_opacity
        self.ribbon_calls = []
        self.point_calls = []

    def _mvp(self):
        return "mvp", "model"

    def _draw_ribbon(self, mvp, model, opacity, depth_write):
        self.ribbon_calls.append((opacity, depth_write))

    def _draw_points(self, mvp, opacity):
        self.point_calls.append(opacity)


@pytest.fixture
def no_gl(monkeypatch):
    """`_on_render` clears the framebuffer before it draws anything, so the
    GL module has to be answerable even though nothing here has a context.
    """
    class _Stub:
        def __getattr__(self, name):
            # `GL_*` names are bit constants the body ORs together; every
            # other name is a call. Getting this backwards is how the first
            # draft of this stub failed, loudly, which is the right way for
            # a stub to be wrong.
            if name.startswith("GL_"):
                return 1
            return lambda *args, **kwargs: None

    import ui.viewer
    monkeypatch.setattr(ui.viewer, "GL", _Stub())


def test_a_held_structure_is_drawn_dimmer_and_a_live_one_is_not(no_gl):
    """The de-emphasis a visitor actually sees: same geometry, same blend,
    less light. Both draws take the SAME factor, so the cross-fade between
    them is untouched and only the brightness differs.

    Mutation this catches: dimming the ribbon but not the points (a held
    point cloud -- what is on screen whenever a ribbon build failed --
    would then be held at full brightness, indistinguishable from live
    diffusion); or dimming by changing `_blend`, which would alter WHICH
    structure is visible rather than how brightly.
    """
    from ui.viewer import _HELD_DIM

    live = _RenderProbe(blend=0.4, held=False)
    live._on_render(None, None)
    assert live.ribbon_calls == [(pytest.approx(0.4), False)]
    assert live.point_calls == [pytest.approx(0.6)]

    held = _RenderProbe(blend=0.4, held=True)
    held._on_render(None, None)
    assert held.ribbon_calls == [(pytest.approx(0.4 * _HELD_DIM), False)]
    assert held.point_calls == [pytest.approx(0.6 * _HELD_DIM)]


def test_a_held_ribbon_still_writes_depth(no_gl):
    """`_draw_ribbon`'s depth-write decision must come from the cross-fade
    blend, NOT from the alpha the ribbon is finally drawn at. They used to
    be the same question and stopped being one the moment a fully
    cross-faded ribbon could be drawn at 0.55.

    Why it matters: the ribbon is a closed tube whose segments overlap on
    screen wherever the chain crosses itself. With depth writes off, those
    overlaps resolve by triangle index order instead of by depth -- so a
    helix behind another would draw in front of it, for the entire fifteen
    seconds of the hold.

    Mutation this catches: `depth_write=opacity >= 1.0` (i.e. deriving it
    from the dimmed value), which is exactly what the code did before the
    parameter existed.
    """
    held = _RenderProbe(blend=1.0, held=True)
    held._on_render(None, None)
    (opacity, depth_write), = held.ribbon_calls
    assert opacity < 1.0, "setup failed: this ribbon is not actually dimmed"
    assert depth_write is True, \
        "a fully cross-faded ribbon stopped writing depth just because it is dim"

    mid_crossfade = _RenderProbe(blend=0.9, held=False)
    mid_crossfade._on_render(None, None)
    assert mid_crossfade.ribbon_calls == [(pytest.approx(0.9), False)], \
        "a ribbon still fading in must not occlude the points underneath it"


def test_clearing_the_viewer_stops_it_being_held():
    """`clear_structure` is called immediately before the new fold's first
    frame is drawn. A `_held` flag surviving that call would dim the new
    fold's live opening cloud as though it were the leftover it just
    replaced.

    Mutation this catches: omitting the `_held` reset from
    `clear_structure`.
    """
    from ui.viewer import StructureViewer

    class _Probe:
        clear_structure = StructureViewer.clear_structure
        set_held = StructureViewer.set_held

        def __init__(self):
            self._point_count = 3
            self._ribbon_index_count = 9
            self._pending_points = None
            self._pending_ribbon = None
            self._blend = 1.0
            self._blend_target = 1.0
            self._camera_framed = True
            self._camera_subject = "ribbon"
            self._held = False

        def queue_render(self):
            pass

    probe = _Probe()
    probe.set_held(True)
    assert probe._held is True
    # Nothing else moved: holding is presentation, not content.
    assert probe._blend == 1.0
    assert probe._camera_subject == "ribbon"
    assert probe._ribbon_index_count == 9

    probe.clear_structure()
    assert probe._held is False
