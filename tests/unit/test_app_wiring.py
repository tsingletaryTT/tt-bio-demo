"""Task 9: the wiring. What `ui/app.py` is actually responsible for.

Everything here runs with NO Tenstorrent hardware, NO live daemon and NO GTK
main loop. `DemoApp` is constructible headlessly (`Gtk.Application.__init__`
touches no display; only `do_activate`'s window does, and nothing here calls
it), so every collaborator the app owns -- viewer, panels, telemetry sampler,
clock -- is replaced with a fake and the app's own callbacks are invoked
directly, exactly the way GLib would invoke them.

The fakes are deliberately dumb recorders, with ONE exception: `FakeViewer`
models `_blend` (0 = the point cloud is what a visitor sees, 1 = the finished
ribbon is). Without that, the headline sequencing defect this task exists to
fix is INVISIBLE to a test: the old code called `set_points()` for every
single frame of every fold and simply drew them at opacity 0 underneath a
ribbon from the *previous* fold. Counting `set_points` calls would have
scored that broken build 100%. Counting frames that landed while the ribbon
was NOT covering them is what tells the two apart -- see
`test_live_diffusion_is_visible_for_a_substantial_share_of_each_cycle`.
"""

import json
import logging
import pathlib

import pytest

from protocol.events import pack_coords
from ui.app import DemoApp
from ui.telemetry import ChipReading

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REAL_FOLD_STREAM = REPO_ROOT / "tests/fixtures/streams/real_fold_trpcage.jsonl"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClock:
    """A monotonic clock the test drives by hand.

    The app takes its clock as a constructor argument for exactly this
    reason: the two time-based transitions (the 45s idle timeout and the
    showcase dwell) are the ones hardest to test against a real clock and
    the ones a booth most depends on.
    """

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


class FakeViewer:
    """Records what the app asks the screen to show, and models the blend.

    `blend` mirrors `ui.viewer.StructureViewer`'s own: `clear_structure()`
    puts it back to 0 (points visible), `begin_crossfade()` drives it to 1
    (ribbon visible, points drawn at opacity 0). The real widget takes
    0.8s to travel; this jumps, which is conservative in the SAME direction
    for every implementation being compared, and keeps the test free of a
    frame clock.
    """

    def __init__(self):
        self.point_frames = []
        self.ribbons = []
        self.clears = 0
        self.crossfades = 0
        self.blend = 0.0
        self.connection_state = "disconnected"

    def clear_structure(self):
        self.clears += 1
        self.blend = 0.0

    def set_points(self, coords, opacity=1.0):
        self.point_frames.append(coords)

    def set_ribbon(self, verts, norms, colors, indices):
        self.ribbons.append(verts)

    def begin_crossfade(self):
        self.crossfades += 1
        self.blend = 1.0


class RecordingPanel:
    """Stands in for both panels; records every call it is handed.

    `boom=True` makes every render call raise, which is how the
    "a panel that raises must not freeze the GLib source" tests get their
    failure without needing a real GTK widget to misbehave.
    """

    def __init__(self, boom=False):
        self.boom = boom
        self.updates = []
        self.stages = []
        self.resets = 0

    def update(self, readings, age_s):
        if self.boom:
            raise RuntimeError("telemetry panel exploded")
        self.updates.append((readings, age_s))

    def set_stage_from_wire(self, stage, wire_frac):
        if self.boom:
            raise RuntimeError("pipeline panel exploded")
        self.stages.append((stage, wire_frac))

    def reset(self):
        if self.boom:
            raise RuntimeError("pipeline panel exploded")
        self.resets += 1


class FakeSampler:
    """A `ui.telemetry.TelemetrySampler` with the tt-smi subprocess removed.

    `latest()` is a genuine tri-state on the real sampler (None / [] /
    [readings]); this keeps it one, so a wiring layer that collapses two of
    the three is catchable here rather than only on a booth with no cards
    plugged in.
    """

    def __init__(self, readings=None, age_s=None):
        self.readings = readings
        self.age = age_s
        self.started = 0
        self.stopped = 0

    def latest(self):
        return self.readings

    def age_s(self):
        return self.age

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class FakeStack:
    """Records which screen the app put in front of the visitor."""

    def __init__(self):
        self.visible = None

    def set_visible_child_name(self, name):
        self.visible = name


class FakeGLib:
    """Captures the GLib sources the app registers, without a main loop."""

    def __init__(self):
        self.timeouts = []
        self.idles = []

    def timeout_add(self, interval_ms, callback, *args):
        self.timeouts.append((interval_ms, callback))
        return len(self.timeouts)

    def idle_add(self, callback, *args):
        self.idles.append((callback, args))
        return len(self.idles)


def _reading(index=0, temperature_c=41.0):
    return ChipReading(index=index, board_type="p300c",
                       temperature_c=temperature_c, power_w=17.0,
                       aiclk_mhz=800.0)


def _app(clock=None, *, viewer=True, sampler=None, stack=False):
    """A `DemoApp` with fakes in every collaborator slot."""
    clock = clock if clock is not None else FakeClock()
    app = DemoApp(socket_path=None, clock=clock)
    app.viewer = FakeViewer() if viewer else None
    app.telemetry_panel = RecordingPanel()
    app.pipeline_panel = RecordingPanel()
    app.sampler = sampler if sampler is not None else FakeSampler()
    if stack:
        # What do_activate builds: a two-page stack and a gallery to put on
        # the second page. `_sync_to_state(force=True)` is the same call
        # do_activate makes once the widgets exist, so the screen starts out
        # agreeing with the machine.
        app.screens = FakeStack()
        app.gallery = object()
        app._sync_to_state(force=True)
    return app


def _frame_event(job_id="j1", spread=10.0, n=8):
    """One `frame` event carrying a real, decodable coordinate payload."""
    coords = [[spread * (i % 3), spread * ((i + 1) % 3), spread * ((i + 2) % 3)]
              for i in range(n)]
    return {"type": "frame", "job_id": job_id, "step": 0, "total": 200,
            "n_atoms": n, "coords_b64": pack_coords(coords)}


def _job_start(job_id="j1", target_id="trpcage"):
    return {"type": "job_start", "job_id": job_id, "target_id": target_id,
            "model": "protenix-v2", "card": 0, "n_residues": 20}


def _job_done(job_id="j1", cif_path="/tmp/fake.cif"):
    return {"type": "job_done", "job_id": job_id, "cif_path": cif_path,
            "wall_s": 4.4, "mean_plddt": 95.3}


def _land_ribbon(app, monkeypatch=None):
    """Let whatever ribbon worker is in flight finish and apply its result.

    In production `_ribbon_worker_main` wakes the main loop with
    `GLib.idle_add(self._drain_pending_ribbon)`; with no main loop running,
    the test plays that part itself.
    """
    for thread in list(app._ribbon_threads):
        thread.join(timeout=5.0)
    app._drain_pending_ribbon()


@pytest.fixture
def fake_ribbon(monkeypatch):
    """Replace `ribbon_from_cif` so no CIF parsing happens in these tests."""
    import ui.app

    def _build(cif_path):
        return ("verts", "norms", "colors", "indices")

    monkeypatch.setattr(ui.app, "ribbon_from_cif", _build)
    return _build


# ---------------------------------------------------------------------------
# The five tests the brief names
# ---------------------------------------------------------------------------

def test_events_reach_the_state_machine():
    """A regression here means the booth stops responding to the daemon.

    Mutation this catches: `_handle_event` not calling
    `self.states.on_event(event)` at all (the booth would sit in `attract`
    forever, no showcase, no preparing screen).
    """
    app = _app()
    assert app.states.state == "attract"

    app._handle_event({"type": "not_ready", "missing": ["weights"]})
    assert app.states.state == "preparing"

    app._handle_event(_job_start())
    assert app.states.state == "attract"

    app._handle_event(_job_done())
    assert app.states.state == "showcase"


def test_the_telemetry_panel_is_fed_from_the_sampler_not_the_socket():
    """Spec section 6: a dead daemon must leave the silicon visibly breathing.

    Mutation this catches: feeding the panel from a socket event (e.g.
    `card_state`) instead of from `TelemetrySampler`. The socket is the one
    thing that goes away when the daemon dies; the sampler is the whole
    reason the UI polls `tt-smi` itself.
    """
    sampler = FakeSampler(readings=[_reading()], age_s=1.5)
    app = _app(sampler=sampler)

    # A socket event carrying card telemetry must NOT reach the panel.
    app._handle_event({"type": "card_state", "card": 0, "state": "idle",
                       "temperature_c": 99.0})
    assert app.telemetry_panel.updates == []

    app._tick_telemetry()
    assert app.telemetry_panel.updates == [([_reading()], 1.5)]


def test_telemetry_keeps_updating_while_the_daemon_is_disconnected():
    """Mutation this catches: gating the telemetry tick on the connection
    state (`if self.viewer.connection_state == "connected"`), which is
    exactly the coupling ui/telemetry.py exists to avoid."""
    sampler = FakeSampler(readings=[_reading(temperature_c=40.0)], age_s=0.5)
    app = _app(sampler=sampler)

    app._on_state("disconnected")
    app._tick_telemetry()

    sampler.readings = [_reading(temperature_c=44.0)]
    sampler.age = 0.5
    app._tick_telemetry()

    assert [u[0][0].temperature_c for u in app.telemetry_panel.updates] == [40.0, 44.0]


def test_the_idle_timer_is_ticked_by_something(monkeypatch):
    """Without a tick source the booth never returns to attract.

    Two halves, because two different mutations break this: never
    REGISTERING a repeating source that calls `_tick_state`, and
    `_tick_state` never handing the machine a clock reading.
    """
    import ui.app

    clock = FakeClock()
    app = _app(clock)

    fake_glib = FakeGLib()
    monkeypatch.setattr(ui.app, "GLib", fake_glib)
    app._start_timers()
    registered = [callback for _interval, callback in fake_glib.timeouts]
    assert app._tick_state in registered, "nothing ever calls _tick_state"

    # And the tick genuinely advances the machine's clock-driven transitions.
    app._on_touch()
    assert app.states.state == "gallery"
    app._tick_state()                     # stamps the idle baseline
    clock.advance(46.0)
    app._tick_state()
    assert app.states.state == "attract"


def test_a_panel_that_raises_does_not_freeze_the_glib_source():
    """An unhandled exception inside a GLib callback silently removes that
    source forever on this stack -- a repeating timer that stops is a frozen
    booth with nothing on screen saying so.

    Mutations this catches: no guard around the panel call at all; a guard
    whose `return True` sits INSIDE the `try` (so the raising path returns
    None, which GLib reads as False and removes the source just the same).
    """
    app = _app()
    app.telemetry_panel = RecordingPanel(boom=True)
    assert app._tick_telemetry() is True
    assert app._tick_telemetry() is True

    app.pipeline_panel = RecordingPanel(boom=True)
    assert app._handle_event({"type": "stage", "stage": "diffusion",
                              "frac": 0.5}) is False
    # The state machine still saw the event: a broken panel must not cost
    # the booth its state.
    assert app.states.state == "attract"

    app2 = _app()
    app2.telemetry_panel = RecordingPanel(boom=True)
    assert app2._tick_state() is True


# ---------------------------------------------------------------------------
# The tri-state must survive the wiring layer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("readings,age", [
    (None, None),
    ([], 0.4),
    ([_reading()], 0.4),
])
def test_the_telemetry_tri_state_reaches_the_panel_intact(readings, age):
    """`None` ("tt-smi never answered") and `[]` ("tt-smi answered: no
    cards") are DIFFERENT answers and the panel renders them differently.

    Mutation this catches: `self.sampler.latest() or []` in the wiring,
    which collapses the first into the second and turns "we cannot read the
    hardware" into "there is no hardware".
    """
    app = _app(sampler=FakeSampler(readings=readings, age_s=age))
    app._tick_telemetry()
    got_readings, got_age = app.telemetry_panel.updates[0]
    assert got_readings is readings
    assert got_age == age


def test_the_pipeline_panel_is_driven_through_the_wire_converter():
    """Ruling 2 (Task 5): the wire carries a WHOLE-FOLD fraction and the
    panel wants a within-stage one, so the wiring layer must call
    `set_stage_from_wire` -- never `set_stage` with a raw wire value.

    Mutation this catches: calling `set_stage(stage, frac)` here, which
    renders a plausible, wrong bar (diffusion sitting at 15% the instant it
    starts and never reaching 100%).
    """
    app = _app()
    app._handle_event({"type": "stage", "stage": "diffusion", "frac": 0.55})
    assert app.pipeline_panel.stages == [("diffusion", 0.55)]

    app._handle_event(_job_start())
    assert app.pipeline_panel.resets == 1


# ---------------------------------------------------------------------------
# The headline sequencing fix
# ---------------------------------------------------------------------------

def test_a_job_start_during_a_showcase_does_not_clear_the_finished_structure(fake_ribbon):
    """The daemon starts fold N+1 before the UI has finished showing fold
    N's result -- measured on hardware. Clearing the screen on that
    `job_start` is what threw fold N's ribbon away.

    Mutation this catches: `clear_structure()` called unconditionally from
    the `job_start` branch.
    """
    app = _app()
    app._handle_event(_job_done("n"))
    _land_ribbon(app)
    assert app.viewer.crossfades == 1

    clears_before = app.viewer.clears
    app._handle_event(_job_start("n+1"))
    assert app.states.state == "showcase"
    assert app.viewer.clears == clears_before, \
        "the next fold's job_start wiped the structure being showcased"


def test_point_frames_are_suppressed_while_a_finished_structure_is_showcased(fake_ribbon):
    """Fold N+1's opening noise cloud is ~1000x the size of fold N's
    finished structure; letting it through during the showcase is what put
    the point cloud beyond the far plane and made the ribbon fade in over
    nothing.

    Mutation this catches: `_drain_frames` ignoring the booth state.
    """
    app = _app()
    app._handle_event(_job_done("n"))
    _land_ribbon(app)

    app._on_event(_frame_event("n+1", spread=10000.0))
    app._drain_frames()
    assert app.viewer.point_frames == [], \
        "the next fold's noise was drawn under the structure being showcased"


def test_the_buffered_frame_lands_the_moment_the_dwell_expires(fake_ribbon):
    """Suppressed, not discarded: the newest frame stays in the one-slot
    buffer, so when the dwell expires the booth cuts straight to live
    diffusion instead of to a blank screen.

    Mutations this catches: dropping suppressed frames on the floor
    (`self._frames.take()` before the state check), and clearing the
    structure at the end of the dwell without pulling the buffered frame.
    """
    clock = FakeClock()
    app = _app(clock)
    app._handle_event(_job_done("n"))
    _land_ribbon(app)
    app._handle_event(_job_start("n+1"))
    app._on_event(_frame_event("n+1"))
    app._drain_frames()
    assert app.viewer.point_frames == []

    app._tick_state()
    clock.advance(app.states.showcase_dwell_s + 0.5)
    app._tick_state()

    assert app.states.state == "attract"
    assert app.viewer.clears == 1, "the deferred clear never happened"
    assert len(app.viewer.point_frames) == 1, \
        "the buffered frame did not land when the showcase ended"
    assert app.viewer.blend == 0.0


def test_the_finished_structure_stays_up_when_no_new_fold_has_started(fake_ribbon):
    """The clear belongs to `job_start`, deferred -- not to the end of the
    dwell. With no next fold in flight (an idle or dead daemon), blanking
    the screen would be strictly worse than holding the last structure.

    Mutation this catches: clearing unconditionally on leaving showcase.
    """
    clock = FakeClock()
    app = _app(clock)
    app._handle_event(_job_done("n"))
    _land_ribbon(app)

    app._tick_state()
    clock.advance(app.states.showcase_dwell_s + 0.5)
    app._tick_state()

    assert app.states.state == "attract"
    assert app.viewer.clears == 0
    assert app.viewer.blend == 1.0, "the finished structure was blanked for nothing"


def test_a_ribbon_that_arrives_after_the_dwell_is_not_thrown_over_live_diffusion(fake_ribbon):
    """A slow ribbon build (up to ~1.2s at 3000 residues) can outlast the
    dwell. By then the booth has moved on to the next fold's live
    diffusion, and cross-fading the OLD structure in over it is precisely
    the headline defect, arriving by a different route.

    Mutation this catches: `_drain_pending_ribbon` applying its result with
    no regard for what the booth is currently showing.
    """
    clock = FakeClock()
    app = _app(clock)
    app._handle_event(_job_done("n"))          # ribbon worker spawned
    app._handle_event(_job_start("n+1"))
    app._tick_state()
    clock.advance(app.states.showcase_dwell_s + 0.5)
    app._tick_state()
    assert app.states.state == "attract"

    app._on_event(_frame_event("n+1"))
    app._drain_frames()
    assert len(app.viewer.point_frames) == 1

    _land_ribbon(app)                          # fold N's ribbon, far too late
    assert app.viewer.ribbons == []
    assert app.viewer.blend == 0.0, \
        "a stale ribbon was cross-faded over the next fold's live diffusion"


def test_the_showcase_dwell_is_measured_from_the_reveal_not_from_job_done(fake_ribbon):
    """`job_done` is when the daemon finished; the reveal is when a visitor
    can actually SEE the structure, and those differ by the ribbon build
    plus the 0.8s cross-fade. A dwell measured from `job_done` shrinks by
    exactly that much -- to nothing, for a large structure.

    Mutation this catches: not calling `states.on_structure_revealed()`
    when the ribbon lands.
    """
    clock = FakeClock()
    app = _app(clock)
    app._handle_event(_job_done("n"))
    app._tick_state()                        # dwell would start here...
    clock.advance(app.states.showcase_dwell_s - 0.2)
    app._tick_state()
    _land_ribbon(app)                        # ...but the reveal is only now

    clock.advance(0.5)
    app._tick_state()
    assert app.states.state == "showcase", \
        "the structure's dwell expired almost the instant it became visible"

    clock.advance(app.states.showcase_dwell_s)
    app._tick_state()
    assert app.states.state == "attract"


def test_live_diffusion_is_visible_for_a_substantial_share_of_each_cycle(fake_ribbon):
    """The headline measurement, on a replayed multi-fold run.

    Before this task: fold N's ribbon landed after fold N+1's `job_start`,
    drove the blend to 1, and NOTHING put it back until the fold after that
    -- so fold N+1's remaining frames were all drawn at opacity 0 and only
    ~27% of each collapse ever reached a visitor's eye.

    This replays the recorded real fold three times back to back, at its
    recorded pace, and asks two questions of every cycle: how many of the
    30 diffusion frames landed while the ribbon was NOT covering them, and
    how much of the cycle's wall clock was spent holding a finished
    structure. Both must be substantial; that is the whole booth.
    """
    clock = FakeClock()
    app = _app(clock)
    stream = [json.loads(line) for line in
              REAL_FOLD_STREAM.read_text().splitlines() if line.strip()]

    cycles = _replay(app, clock, stream, repeats=3)

    # The first cycle is a cold start (nothing to showcase yet), so its
    # showcase share is structurally low; every cycle after it is the
    # steady state a visitor actually watches.
    for index, cycle in enumerate(cycles):
        visible_share = cycle["visible_frames"] / cycle["frames"]
        assert visible_share >= 0.40, (
            f"cycle {index}: only {visible_share:.0%} of the collapse was "
            f"ever visible ({cycle['visible_frames']}/{cycle['frames']} frames)")
    for index, cycle in enumerate(cycles[1:], start=1):
        showcase_share = cycle["showcase_ms"] / cycle["duration_ms"]
        assert showcase_share >= 0.30, (
            f"cycle {index}: the finished structure held the screen for only "
            f"{showcase_share:.0%} of the cycle")

    assert app.viewer.crossfades == len(cycles), \
        "every fold must reveal its own structure exactly once"


def _replay(app, clock, stream, repeats, *, ribbon_delay_ms=150, tick_ms=100):
    """Drive the app through `repeats` copies of a recorded stream.

    Advances `clock` by each event's recorded `_delay_ms`, runs the state
    tick every `tick_ms` in between (the app's own cadence), and lands each
    fold's ribbon `ribbon_delay_ms` after its `job_done` -- i.e. after the
    NEXT fold has already started, which is the ordering measured on real
    hardware and the reason this whole task exists.

    Returns one dict per fold cycle: frames that arrived, frames that
    actually reached the viewer while the ribbon was not covering them, how
    long the cycle ran, and how much of it was spent showcasing.
    """
    cycles = []
    now_ms = clock.t * 1000.0
    ribbon_due = None

    def _tick_to(target_ms):
        nonlocal now_ms, ribbon_due
        while now_ms + tick_ms <= target_ms:
            now_ms += tick_ms
            clock.t = now_ms / 1000.0
            if ribbon_due is not None and now_ms >= ribbon_due:
                ribbon_due = None
                _land_ribbon(app)
            app._tick_state()
            if cycles and app.states.state == "showcase":
                cycles[-1]["showcase_ms"] += tick_ms
        now_ms = target_ms
        clock.t = now_ms / 1000.0

    for _ in range(repeats):
        for event in stream:
            _tick_to(now_ms + event.get("_delay_ms", 0))
            payload = {k: v for k, v in event.items() if k != "_delay_ms"}
            kind = payload["type"]
            if kind == "frame":
                before = len(app.viewer.point_frames)
                app._on_event(payload)
                app._drain_frames()
                if cycles:
                    cycles[-1]["frames"] += 1
                    if (len(app.viewer.point_frames) > before
                            and app.viewer.blend == 0.0):
                        cycles[-1]["visible_frames"] += 1
                continue
            if kind == "job_start":
                if cycles:
                    cycles[-1]["duration_ms"] = now_ms - cycles[-1]["start_ms"]
                cycles.append({"start_ms": now_ms, "duration_ms": 0.0,
                               "frames": 0, "visible_frames": 0,
                               "showcase_ms": 0.0})
            app._handle_event(payload)
            if kind == "job_done":
                ribbon_due = now_ms + ribbon_delay_ms

    _tick_to(now_ms + 4000)                 # let the last showcase play out
    if cycles:
        cycles[-1]["duration_ms"] = now_ms - cycles[-1]["start_ms"]
    return cycles


# ---------------------------------------------------------------------------
# Gallery, touch, and the one source of truth for "preparing"
# ---------------------------------------------------------------------------

def test_a_touch_shows_the_gallery_and_a_pick_starts_a_fold():
    """Mutation this catches: the touch handler driving the machine but
    never swapping the screen (a booth that responds to nothing a visitor
    does)."""
    app = _app(stack=True)
    assert app.screens.visible == "viewer"

    app._on_touch()
    assert app.states.state == "gallery"
    assert app.screens.visible == "gallery"

    app._on_pick("trpcage")
    assert app.states.state == "folding"
    assert app.states.selected_target == "trpcage"
    assert app.screens.visible == "viewer"


def test_display_state_reads_through_to_the_state_machine():
    """The plan's named integration seam: Task 1's `display_state` and Task
    7's `StateMachine` must not be two places that can disagree about
    whether the booth is preparing.

    Mutation this catches: `display_state` kept as its own field, set
    independently of the machine.
    """
    app = _app()
    app._handle_event({"type": "not_ready", "missing": ["weights"]})
    assert app.display_state == "preparing"
    assert app.display_state == app.states.state

    app._handle_event(_job_start())
    assert app.display_state != "preparing"
    assert app.display_state == app.states.state


def test_the_not_ready_message_never_carries_wire_detail(caplog):
    """The degrade path stays visible to a visitor and stays neutral --
    Task 1's constraint, re-checked now that the state machine owns the
    state."""
    app = _app()
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        app._handle_event({"type": "not_ready",
                           "missing": ["model weights: /w/secret.pt"]})
    assert app.states.state == "preparing"
    assert app.display_message.strip() != ""
    assert "/w/secret.pt" not in app.display_message
    assert any("/w/secret.pt" in record.message for record in caplog.records)
