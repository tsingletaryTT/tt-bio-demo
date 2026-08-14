"""Task 10: the two things a visitor can DO to the booth.

`?` (the help card) and `D` (the diagnostics panel) are chrome laid over the
booth, not booth state -- so the tests that matter most here are the ones
that prove they stay chrome: `?` works from every single state without
moving the state machine, the fold keeps running behind the card, and
neither overlay can outlive the visitor who opened it.

Everything runs headless: no GTK main loop, no daemon, no hardware. The app
is constructible without `do_activate` (that is where the display is
needed), so the widget-free half of this file drives `DemoApp`'s own
callbacks exactly as GLib would, and the widget half builds only the two
trees under test (`_build_help_overlay`, `_build_side_rail`) rather than a
whole window.
"""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

import pytest
from gi.repository import Gtk

import _legibility
from _appfakes import _FakeQuad
from protocol.events import pack_coords
from ui import app as app_module
from ui import chipviz as chipviz_module
from ui import diagnostics as diagnostics_module
from ui import panels as panels_module
from ui.app import DemoApp
from ui.geometry import PLDDT_STOPS
from ui.panels import MIN_CONTRAST_RATIO, contrast_ratio
from ui.playlist import Target, load_playlist
from ui.telemetry import ChipReading

# The wiring tests' fakes are the right ones here too -- reusing them is
# also what keeps "what a FakeViewer models" a single decision (see
# test_app_wiring.py's module docstring on why FakeViewer models `blend`).
from test_app_wiring import (FakeClock, FakeStack, FakeViewer,
                             RecordingPanel, _cell)


class FakeSampler:
    def latest(self):
        return None

    def age_s(self):
        return None

    def start(self):
        pass

    def stop(self):
        pass


class _RecordingChipViz:
    """Stands in for `ui.chipviz.ChipVizPanel`, recording every booth state
    and every `{chip: stage}` picture the booth hands it. Deliberately NOT a
    real panel: these tests are about the WIRING, and constructing a real
    `WebKit.WebView` in a test process is its own crash class (see
    test_chipviz.py's note on bwrap/SIGTRAP)."""

    def __init__(self):
        self.states = []
        self.running = None
        # The per-chip picture: `{card: stage_or_None}`, one entry per call.
        # This is what replaced the single `(state, stage)` pair once four
        # chips folded at once -- see ui/chipviz.py's `set_chip_stages`.
        self.chip_stages = []
        self.staleness_ticks = 0
        # The panel is CHROME now (off by default, `T` to open), so the two
        # things ui/app.py's `_set_chipviz_visible` reads and writes have to
        # be here too: `available` is the real panel's "can this machine
        # draw me at all", and `visible` records what the booth did about it.
        self.available = True
        self.visible = None

    def set_visible(self, visible):
        self.visible = visible

    def set_state(self, state):
        self.states.append(state)

    def set_chip_stages(self, stages):
        self.chip_stages.append(dict(stages))

    def tick_staleness(self):
        self.staleness_ticks += 1

    def set_running(self, running):
        self.running = running


class FakeGesture:
    """Just enough of `Gtk.GestureClick` to record whether the hint handler
    claimed its event sequence -- the one line that keeps a click on the
    hint from ALSO opening the gallery behind it."""

    def __init__(self):
        self.states = []

    def set_state(self, state):
        self.states.append(state)


def _app(clock=None, *, stack=True):
    clock = clock if clock is not None else FakeClock()
    app = DemoApp(socket_path=None, clock=clock)
    app.quad = _FakeQuad(1, cards=[0], viewer_factory=FakeViewer)
    app.attach_cards([0])
    app.telemetry_panel = RecordingPanel()
    app.pipeline_panel = RecordingPanel()
    app.sampler = FakeSampler()
    if stack:
        app.screens = FakeStack()
        app.gallery = object()
        app._sync_to_state(force=True)
    return app


def _drive_to(app, state):
    """Put the booth in `state` through its real entry points only."""
    if state == "attract":
        pass
    elif state == "gallery":
        app.states.on_touch()
    elif state == "folding":
        app.states.on_touch()
        app.states.on_pick("trpcage")
    elif state == "showcase":
        app.states.on_touch()
        app.states.on_pick("trpcage")
        app.states.on_event({"type": "job_done", "job_id": "j1"})
    elif state == "preparing":
        app.states.on_event({"type": "not_ready", "missing": ["weights"]})
    else:  # pragma: no cover - a typo in a test is not a booth state
        raise AssertionError(f"no such state {state!r}")
    assert app.states.state == state
    app._sync_to_state()
    return app


def _a_fold_starting(job_id="j1", card=0, target_id="t"):
    """The `job_start` every later event of a fold depends on.

    Not scaffolding: `job_start` is the only event carrying a `card`, so it
    is where the router binds a job id to a cell. A `stage` or a `frame`
    posted without one belongs to no cell at all, which is the daemon's
    behaviour too -- it just never happens on the wire, because a fold
    always starts before it runs.
    """
    return {"type": "job_start", "job_id": job_id, "target_id": target_id,
            "model": "m", "card": card, "n_residues": 20}


def _frame_event(spread=10.0, n=8, step=3):
    coords = [[spread * i, spread * i, spread * i] for i in range(n)]
    return {"type": "frame", "job_id": "j1", "step": step, "total": 200,
            "n_atoms": n, "coords_b64": pack_coords(coords)}


# ---------------------------------------------------------------------------
# `?` works from ANY state, at any time. The user's actual request.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state",
                         ["attract", "gallery", "folding", "showcase", "preparing"])
def test_the_help_card_opens_from_every_booth_state(state):
    """"let's make sure we can hit `?` at any time" -- including mid-fold,
    mid-showcase, and while the daemon is degraded.

    Mutation this catches: gating the help key on `state == "attract"` (or
    on any state at all), which is the shape this would take if help were
    modelled as a sixth booth state instead of as chrome.
    """
    app = _drive_to(_app(), state)
    assert app.help_visible is False
    app._handle_key("question")
    assert app.help_visible is True


@pytest.mark.parametrize("state",
                         ["attract", "gallery", "folding", "showcase", "preparing"])
def test_opening_the_help_card_never_moves_the_booth(state):
    """Chrome, not state: pressing `?` must not open a gallery, cut a
    showcase short, or paper over a degraded daemon.

    Mutation this catches: routing the help key through `_on_touch`.
    """
    app = _drive_to(_app(), state)
    app._handle_key("question")
    assert app.states.state == state


@pytest.mark.parametrize("key", ["question", "f1", "F1", "Help"])
def test_every_documented_way_of_asking_for_help_works(key):
    """`?` is a shifted key on most layouts and missing on some; F1 and the
    dedicated Help key are the same request. An operator should not have to
    know which one this build wanted."""
    app = _app()
    app._handle_key(key)
    assert app.help_visible is True


@pytest.mark.parametrize("key", ["question", "Escape"])
def test_the_help_card_is_dismissed_by_question_and_escape(key):
    app = _app()
    app._handle_key("question")
    app._handle_key(key)
    assert app.help_visible is False


def test_the_help_card_is_dismissed_by_a_click_anywhere():
    app = _app()
    app._handle_key("question")
    app._on_click()
    assert app.help_visible is False


def test_the_click_that_dismisses_the_help_card_does_not_also_open_a_gallery():
    """A visitor tapping to get rid of a wall of text has not asked for a
    pick grid behind it.

    Mutation this catches: `_on_click` falling through to `_on_touch` after
    dismissing the card.
    """
    app = _app()
    app._handle_key("question")
    app._on_click()
    assert app.states.state == "attract"
    assert app.screens.visible == "viewer"


def test_any_key_dismisses_the_help_card_and_does_nothing_else():
    """A visitor pressing something random while the card is up means "get
    rid of this". It must not ALSO be a touch -- finding a gallery behind a
    dismissed help card is exactly the "why did it do that" moment this task
    exists to remove."""
    app = _app()
    app._handle_key("question")
    app._handle_key("space")
    assert app.help_visible is False
    assert app.states.state == "attract"


# ---------------------------------------------------------------------------
# `D` -- the diagnostics panel.
# ---------------------------------------------------------------------------

def test_the_diagnostics_panel_is_hidden_until_it_is_asked_for():
    """The protein is the hero. Mutation this catches: shipping the panel
    visible by default."""
    assert _app().diagnostics_visible is False


def test_the_diagnostics_key_toggles_the_panel():
    app = _app()
    app._handle_key("d")
    assert app.diagnostics_visible is True
    app._handle_key("D")
    assert app.diagnostics_visible is False


def test_the_diagnostics_key_is_not_a_visitor_touch():
    """Mutation this catches: letting `d` fall through to `_on_touch`, which
    would open the gallery every time someone opened the panel."""
    app = _app()
    app._handle_key("d")
    assert app.states.state == "attract"
    assert app.screens.visible == "viewer"


def test_escape_closes_the_diagnostics_panel():
    app = _app()
    app._handle_key("d")
    app._handle_key("Escape")
    assert app.diagnostics_visible is False


def test_escape_with_nothing_open_is_not_a_visitor_touch():
    """Backing out of a screen must never be the same gesture as asking for
    one."""
    app = _app()
    app._handle_key("Escape")
    assert app.states.state == "attract"


def test_an_ordinary_key_is_still_a_visitor_touch():
    """The Task 9 behavior every other test here is carving exceptions out
    of. Mutation this catches: swallowing every key while adding the
    special ones."""
    app = _app()
    app._handle_key("space")
    assert app.states.state == "gallery"
    assert app.screens.visible == "gallery"


def test_a_click_anywhere_is_still_a_visitor_touch():
    app = _app()
    app._on_click()
    assert app.states.state == "gallery"


# ---------------------------------------------------------------------------
# Operator keys.
# ---------------------------------------------------------------------------

def test_the_operator_can_quit_from_anywhere_including_behind_the_help_card():
    """Ctrl+Q is the venue's off switch. It must not be blocked by whatever
    a visitor left on screen."""
    app = _app()
    quits = []
    app.quit = lambda: quits.append(True)
    app._handle_key("question")
    app._handle_key("q", ctrl=True)
    assert quits == [True]


def test_a_bare_q_does_not_quit_the_booth():
    """The reason quit is a CHORD: a visitor mashing the keyboard must not
    be able to close the demo. Mutation this catches: dropping the `ctrl`
    condition.

    Bare `Q` is the quad view (ui/quad.py's `QUAD_KEYS`), so it is no longer
    a plain visitor touch either -- but the thing this test is about is
    unchanged and is if anything sharper: the near-miss for `Ctrl+Q` now
    lands on a toggle a second press undoes, rather than on the gallery.
    """
    app = _app()
    quits = []
    app.quit = lambda: quits.append(True)
    app._handle_key("q")
    assert quits == []
    assert app.quad_visible is True
    app._handle_key("q")
    assert app.quad_visible is False
    assert quits == []


def test_the_fullscreen_chord_is_harmless_with_no_window_yet():
    """Every callback in this app has to survive being called before
    do_activate (headless tests, and shutdown races)."""
    app = _app()
    app._handle_key("f", ctrl=True)           # must not raise


def test_a_stray_control_chord_is_not_a_visitor_touch():
    """Mutation this catches: unbound Ctrl chords falling through to
    `_on_touch`, so Ctrl+Alt+something at a venue opens the gallery."""
    app = _app()
    app._handle_key("s", ctrl=True)
    assert app.states.state == "attract"


# ---------------------------------------------------------------------------
# An overlay a visitor walked away from must not persist forever.
# ---------------------------------------------------------------------------

def test_the_help_card_closes_itself_after_a_visitor_walks_away():
    """Mutation this catches: no idle handling for the overlays at all --
    the booth's own 45s timeout covers the state machine's screens, not
    this chrome, so without `_tick_overlays` a help card left open at 11am
    is still up at 6pm."""
    clock = FakeClock()
    app = _app(clock)
    app._handle_key("question")
    clock.advance(app_module._HELP_IDLE_S - 0.1)
    app._tick_state()
    assert app.help_visible is True
    clock.advance(0.2)
    app._tick_state()
    assert app.help_visible is False


def test_the_help_card_does_not_close_while_someone_is_using_the_booth():
    """A visitor reading the card slowly, pressing nothing, is a visitor --
    but one who keeps touching the booth definitely is. The timer is
    measured from the last input of any kind."""
    clock = FakeClock()
    app = _app(clock)
    app._handle_key("question")
    for _ in range(10):
        clock.advance(app_module._HELP_IDLE_S * 0.9)
        app._handle_key("question")           # dismiss
        app._handle_key("question")           # and reopen
        app._tick_state()
    assert app.help_visible is True


def test_the_diagnostics_panel_is_more_patient_than_the_help_card():
    """Deliberately different timeouts: the diagnostics panel is the one WE
    want up while talking to someone at the booth, and having it vanish
    mid-sentence would be worse than useless. It still goes eventually.

    Mutation this catches: sharing one timeout between the two overlays.
    """
    clock = FakeClock()
    app = _app(clock)
    app._handle_key("d")
    clock.advance(app_module._HELP_IDLE_S + 1.0)
    app._tick_state()
    assert app.diagnostics_visible is True
    clock.advance(app_module._DIAGNOSTICS_IDLE_S)
    app._tick_state()
    assert app.diagnostics_visible is False


def test_nothing_times_out_before_anyone_has_touched_the_booth():
    """`_last_input_at` starts as None -- an attract loop running for hours
    before the first visitor must not be treated as an idle visitor.

    Mutation this catches: initialising `_last_input_at` to the clock at
    construction and then subtracting, which is fine here but would close
    an overlay the moment one was opened by something other than input.
    """
    clock = FakeClock()
    app = _app(clock)
    clock.advance(10_000.0)
    app._tick_state()                          # must not raise, must do nothing
    assert app.help_visible is False


# ---------------------------------------------------------------------------
# The overlay must not freeze the demo.
# ---------------------------------------------------------------------------

def test_the_fold_keeps_running_behind_the_help_card():
    """The card is a widget, not a modal loop: every GLib source underneath
    it keeps firing. Pinned by driving a whole fold's worth of callbacks
    with the card up and checking that all three of the booth's moving
    parts moved.

    Mutation this catches: an early `return` in `_handle_event`,
    `_drain_frames` or `_tick_state` while an overlay is up -- the obvious
    "pause the demo behind the help" implementation.
    """
    clock = FakeClock()
    app = _app(clock)
    app._handle_key("question")

    app._handle_event(_a_fold_starting())
    app._handle_event({"type": "stage", "job_id": "j1",
                       "stage": "diffusion", "frac": 0.55})
    app._frames.put(_frame_event())
    app._drain_frames()
    app._handle_event({"type": "job_done", "job_id": "j1", "wall_s": 4.4,
                       "mean_plddt": 91.2})
    app._tick_state()               # stamps the showcase dwell
    clock.advance(5.0)
    app._tick_state()               # ...and expires it

    assert _cell(app).point_frames, "diffusion frames stopped reaching the viewer"
    assert app.pipeline_panel.stages, "the pipeline panel stopped being driven"
    assert app.states.state == "attract", "the showcase dwell never expired"
    assert app.help_visible is True, "the card closed itself for no reason"


# ---------------------------------------------------------------------------
# Real traffic reaches the log -- and error text still never reaches a screen.
# ---------------------------------------------------------------------------

def test_protocol_events_reach_the_diagnostics_log():
    app = _app()
    app._handle_event({"type": "job_start", "job_id": "j1", "target_id": "trpcage",
                       "model": "protenix-v2", "card": 2, "n_residues": 20})
    blob = "\n".join(text for _s, _k, text in app.diagnostics.tail(50))
    # The WIRE field is still `card` (a protocol contract), but the LINE says
    # chip -- one tt-smi/daemon device is one chip, not one board. See
    # ui/telemetry.ChipReading and this task's report.
    assert "trpcage" in blob and "chip 2" in blob
    assert "card 2" not in blob, (
        "the diagnostics panel must not call a chip a card -- a visitor "
        "reading it in front of a QB2 would conclude the box holds four "
        "boards when it holds two")


def test_drawn_frames_reach_the_diagnostics_log_with_their_geometry():
    """Frames are logged where they are DRAWN, so the log can honestly say
    a visitor saw them."""
    app = _app()
    # Routable: a frame reaches a cell (and therefore the log) only once its
    # own fold has started -- `job_start` is the only event carrying a card.
    app._handle_event(_a_fold_starting())
    app._frames.put(_frame_event(step=42))
    app._drain_frames()
    blob = "\n".join(text for _s, _k, text in app.diagnostics.tail(50))
    assert "42/200" in blob and "Rg" in blob


def test_a_frame_suppressed_by_a_showcase_is_not_claimed_as_seen():
    """The suppression Task 9 built: during a showcase the newest frame
    stays buffered and is NOT drawn. A log that reported it anyway would be
    lying about what a visitor saw.

    Mutation this catches: logging frames in `_on_event` (where they
    arrive) rather than in `_drain_frames` (where they land).
    """
    app = _drive_to(_app(), "showcase")
    app._frames.put(_frame_event(step=77))
    app._drain_frames()
    blob = "\n".join(text for _s, _k, text in app.diagnostics.tail(50))
    assert "77/200" not in blob


def test_a_job_error_reaching_the_app_never_reaches_the_panel_as_text():
    """The end-to-end half of ui/diagnostics.py's own rule test: the app
    hands the whole event to the log, so the log's refusal to read
    `message` is the only thing standing between a daemon traceback and the
    booth's screen."""
    app = _app()
    app._handle_event({"type": "job_error", "job_id": "j9", "target_id": "t",
                       "message": "RuntimeError: TT_FATAL @ device.cpp:212"})
    blob = "\n".join(text for _s, _k, text in app.diagnostics.tail(50))
    assert "TT_FATAL" not in blob and "RuntimeError" not in blob
    assert "did not finish" in blob


def test_a_diagnostics_failure_cannot_cost_the_booth_an_event():
    """The panel is the least important thing in any of these callbacks. If
    it somehow raises, the fold must still render.

    Mutation this catches: calling the log directly instead of through
    `_note_diagnostics`' guard -- the exception would be caught by
    `_handle_event`'s outer guard instead, which ALSO skips every line of
    rendering below it and logs the event as malformed.
    """
    class ExplodingLog:
        def note_event(self, event):
            raise RuntimeError("boom")

        def note_frame(self, event, coords):
            raise RuntimeError("boom")

    app = _app()
    app.diagnostics = ExplodingLog()
    app._handle_event({"type": "job_start", "job_id": "j1", "target_id": "t",
                       "model": "m", "card": 0, "n_residues": 20})
    assert app.pipeline_panel.resets == 1, "the pipeline panel was never reset"
    app._frames.put(_frame_event())
    app._drain_frames()
    assert _cell(app).point_frames, "the frame never reached the viewer"


# ---------------------------------------------------------------------------
# Repaint policy: only while open, and immediately on opening.
# ---------------------------------------------------------------------------

class RecordingDiagnosticsPanel:
    def __init__(self):
        self.refreshes = 0
        self.visible = None

    def set_visible(self, visible):
        self.visible = visible

    def refresh(self, diag_log, force=False):
        self.refreshes += 1
        return True


def test_a_closed_diagnostics_panel_is_not_repainted():
    """30Hz of frames must not re-label twenty hidden rows forever.
    Mutation this catches: dropping the `diagnostics_visible` guard on the
    tick's refresh."""
    app = _app()
    app.diagnostics_panel = RecordingDiagnosticsPanel()
    for _ in range(20):
        app._tick_state()
    assert app.diagnostics_panel.refreshes == 0


def test_opening_the_diagnostics_panel_repaints_it_at_once():
    """It must never appear empty -- or showing what it held when it was
    last closed -- in the instant a visitor opens it."""
    app = _app()
    app.diagnostics_panel = RecordingDiagnosticsPanel()
    app._handle_key("d")
    assert app.diagnostics_panel.refreshes == 1
    assert app.diagnostics_panel.visible is True


def test_an_open_diagnostics_panel_is_repainted_by_the_tick():
    app = _app()
    app.diagnostics_panel = RecordingDiagnosticsPanel()
    app._handle_key("d")
    app._tick_state()
    assert app.diagnostics_panel.refreshes == 2


# ---------------------------------------------------------------------------
# The hint labels: clickable, and claiming their clicks.
# ---------------------------------------------------------------------------

def test_clicking_a_hint_claims_the_click_so_it_is_not_also_a_touch():
    """Both gestures sit in the bubble phase. Without the claim, a visitor
    pressing "DIAGNOSTICS" gets a gallery on top of the panel they asked
    for.

    Mutation this catches: dropping `gesture.set_state(CLAIMED)`.
    """

    app = _app()
    gesture = FakeGesture()
    app._on_hint_pressed(gesture, app._toggle_diagnostics)
    assert gesture.states == [Gtk.EventSequenceState.CLAIMED]
    assert app.diagnostics_visible is True
    assert app.states.state == "attract"


def test_the_hint_label_says_which_way_the_panel_will_go():
    app = _app()
    closed = app._diagnostics_hint_text()
    app._set_diagnostics_visible(True)
    assert app._diagnostics_hint_text() != closed


# ---------------------------------------------------------------------------
# The help card tells the truth about the booth.
# ---------------------------------------------------------------------------

def test_every_key_the_booth_answers_to_is_listed_in_the_help_card():
    """A binding that exists but is not on the card is folklore; a binding
    on the card that does not exist is a lie. Both are caught by walking the
    handler's OWN key sets against the card's text.

    Mutation this catches: adding a key to `_handle_key` without adding a
    row to `_KEY_HELP`.
    """
    # GDK key NAMES (what `_handle_key` matches on) against the way each one
    # is printed on the card. Anything in the handler's key sets with no
    # entry here fails immediately -- which is the point: adding a binding
    # forces a decision about how a visitor is told it exists.
    printed_as = {
        "question": "?",
        "f1": "f1",
        "help": "f1",      # the dedicated Help key some keyboards carry,
                           # documented as F1 rather than as a third spelling
        "d": "d",
        "t": "t",
    }
    listed = " ".join(keys for keys, _meaning in app_module._KEY_HELP).lower()
    for key in (app_module._HELP_KEYS | app_module._DIAGNOSTICS_KEYS
                | app_module._TENSIX_KEYS):
        assert key in printed_as, f"{key!r} is bound but not documented anywhere"
        assert printed_as[key] in listed, f"{key!r} is missing from the card"
    for phrase in ("esc", "ctrl + f", "ctrl + q", "any other key"):
        assert phrase in listed, f"{phrase} undocumented"


def test_the_help_card_explains_what_a_visitor_is_actually_looking_at():
    """Not a placeholder check: the card has to say that this is running
    here, now, on Tenstorrent silicon, and is not a recording -- which is
    the single most common question at a booth."""
    intro = " ".join(app_module._HELP_INTRO).lower()
    assert "tenstorrent" in intro
    assert "not a recording" in intro
    assert "denois" in intro           # what the model is actually doing
    assert "200" in intro              # ...over how many steps


def test_the_help_card_still_fits_the_booth_s_own_screen():
    """Every line added to this card costs vertical space, and a card taller
    than the glass silently loses its last rows -- the operator keys are at
    the bottom of the KEYS column, so `Ctrl + Q` is the first thing to go.

    Measured at the booth's real fullscreen size, which is the only size that
    matters: 1920x1080 (see `_SIDE_RAIL_WIDTH_PX`'s own comment and the
    windowed default of 1280x800, which this card has never fitted and does
    not have to).

    Found by looking at it: adding the quad's key row and its paragraph took
    the card from 838px to 913px.
    """
    app = _app()
    card = app_module.DemoApp._build_help_overlay(app)

    # A widget measures 0 while it is hidden, and the card is built hidden.
    def show(widget):
        widget.set_visible(True)
        child = widget.get_first_child()
        while child is not None:
            show(child)
            child = child.get_next_sibling()

    show(card)
    _minimum, natural, _, _ = card.measure(Gtk.Orientation.VERTICAL, 1920)
    assert natural <= 1080, (
        f"the help card wants {natural}px of a 1080px screen; its last rows "
        f"(the operator's Ctrl+Q among them) are off the bottom")


def test_the_help_card_explains_both_rail_panels():
    panels_copy = " ".join(app_module._HELP_PANELS).lower()
    assert "pipeline" in panels_copy and "diffusion" in panels_copy
    assert "temperature" in panels_copy


# ---------------------------------------------------------------------------
# Task 16: four chips, honestly.
#
# The Tensix panel and this card were BOTH walked back to say less when only
# card 0 folded (whole-branch review, Critical 3 and Critical 2). Four chips
# fold now, so the copy is allowed to say so -- and shipping the behaviour
# change without the copy change would ship a new lie pointing the other way,
# which is why these live in the same commit as ui/chipviz.py's rewrite.
# ---------------------------------------------------------------------------

def test_the_help_card_no_longer_says_the_fold_runs_on_one_chip():
    """The copy was true when one chip folded. Shipping the behaviour change
    without the copy change ships a new lie in the other direction."""
    from ui.app import _HELP_PANELS
    text = " ".join(_HELP_PANELS).lower()
    assert "runs on one chip" not in text
    assert "the others sit idle" not in text


def test_the_help_card_describes_what_the_quad_actually_shows():
    from ui.app import _HELP_PANELS
    text = " ".join(_HELP_PANELS).lower()
    assert "four" in text and ("at once" in text or "at the same time" in text)


def test_the_tensix_paragraph_itself_says_four_chips_animate():
    """Sharper than the test above it, which the quad's own help line
    (`ui.quad.QUAD_HELP_LINE`, first entry of `_HELP_PANELS`) already
    satisfies on its own. The paragraph that has to change is the one about
    the TENSIX PANEL, and this is the one assertion that fails if it is left
    alone.

    Mutation this catches: reverting only the Tensix paragraph.
    """
    from ui.app import _HELP_PANELS
    tensix = [p for p in _HELP_PANELS if "tensix activity" in p.lower()]
    assert len(tensix) == 1, "the Tensix paragraph moved; retarget this test"
    text = tensix[0].lower()
    assert "four" in text
    assert "one chip" not in text
    assert "sit idle" not in text


def test_the_tensix_paragraph_still_says_a_resting_chip_is_drawn_resting():
    """The claim the panel earns back is "as many as are working", not
    "four". A card that dropped the resting case would be back to promising
    four grids of motion the moment three chips are between folds."""
    from ui.app import _HELP_PANELS
    tensix = [p for p in _HELP_PANELS if "tensix activity" in p.lower()][0]
    lowered = tensix.lower()
    assert "rest" in lowered or "idle" in lowered or "quiet" in lowered


def test_the_help_intro_no_longer_says_one_after_another():
    """It reads, verbatim before Task 16: 'The booth works through its
    proteins one after another, all day.' That was true; it is not any
    more."""
    from ui.app import _HELP_INTRO
    assert "one after another" not in " ".join(_HELP_INTRO).lower()


# ---------------------------------------------------------------------------
# Task 17: the pick, end to end -- the copy half.
#
# The behaviour and every visitor-facing string that contradicts it ship in
# ONE commit. Shipping the behaviour alone would leave the booth telling
# visitors it cannot do the thing it just did -- the mirror image of the
# Critical 2 finding, and no more honest for being generous.
#
# `test_the_help_intro_still_discloses_that_a_pick_starts_nothing`, which
# Task 16 added and this task was told not to leave alongside its inverse,
# is deleted here, in the same commit that makes it false.
# ---------------------------------------------------------------------------

def test_the_help_intro_no_longer_says_picking_is_not_wired_up():
    """It reads, verbatim before this task: 'asking it to fold a particular
    one on demand isn't wired up yet'. It is now, and a booth that disclaims
    a capability it has teaches visitors not to try it."""
    from ui.app import _HELP_INTRO
    text = " ".join(_HELP_INTRO).lower()
    assert "isn't wired up" not in text
    assert "is not wired up" not in text
    assert "one after another" not in text


def test_the_help_intro_says_what_a_tap_now_does():
    from ui.app import _HELP_INTRO
    text = " ".join(_HELP_INTRO).lower()
    assert "next" in text


def test_the_help_intro_does_not_promise_an_instant_fold():
    """With four chips busy the pick starts when one frees. 'Instantly' is
    a claim the booth breaks in front of the one visitor watching for it."""
    from ui.app import _HELP_INTRO
    text = " ".join(_HELP_INTRO).lower()
    assert "instantly" not in text
    assert "straight away" not in text


def test_the_help_intro_says_a_pick_does_not_interrupt_a_running_fold():
    """The reason the wait exists, stated as the feature it is."""
    from ui.app import _HELP_INTRO
    text = " ".join(_HELP_INTRO).lower()
    assert "interrupt" in text or "finish" in text


def test_the_gallery_copy_says_a_tap_folds_it_next():
    from ui.gallery import _CAPTION_BODY, _CAPTION_TITLE, _CARD_HINT
    lowered = f"{_CAPTION_TITLE} {_CAPTION_BODY} {_CARD_HINT}".lower()
    assert isinstance(_CAPTION_BODY, str), "a tuple here would join per-character"
    assert "next" in lowered
    assert "isn't wired up" not in lowered
    assert "is not wired up" not in lowered


def test_the_gallery_copy_no_longer_says_one_after_another():
    """It reads, verbatim before this task: 'It works through these one
    after another, all day.' Four chips is four at a time."""
    from ui.gallery import _CAPTION_BODY
    lowered = _CAPTION_BODY.lower()
    assert "one after another" not in lowered
    assert "the fold that is running right now" not in lowered


def test_the_card_hint_no_longer_says_a_tap_only_puts_it_in_a_queue_it_owns():
    """`_CARD_HINT` is the line that sat where "TAP TO FOLD" used to, and it
    was changed to "IN THE ROTATION" precisely because a tap did nothing. It
    does something now, and this is the string a visitor reads on the card
    they are about to touch.

    Mutation this catches: leaving `_CARD_HINT` alone. The test above it
    passes on `_CAPTION_BODY` alone, so this is what makes the per-card line
    load-bearing.
    """
    from ui.gallery import _CARD_HINT
    lowered = _CARD_HINT.lower()
    assert "in the rotation" not in lowered
    assert "next" in lowered or "fold" in lowered


def test_the_gallery_copy_does_not_promise_an_instant_fold_either():
    """Same rule as the help card and the notice, in the place a visitor is
    standing when they decide to tap."""
    from ui.gallery import _CAPTION_BODY, _CARD_HINT
    lowered = f"{_CAPTION_BODY} {_CARD_HINT}".lower()
    for forbidden in ("instantly", "straight away", "right now",
                      "immediately"):
        assert forbidden not in lowered


def test_the_gallery_copy_says_the_folds_already_running_finish():
    """The wait, stated as the feature it is -- in the one place a visitor
    reads before tapping rather than after."""
    from ui.gallery import _CAPTION_BODY
    lowered = _CAPTION_BODY.lower()
    assert "finish" in lowered or "interrupt" in lowered


def test_the_gallery_module_docstring_no_longer_describes_a_one_way_socket():
    """That docstring is the instruction sheet for anyone editing this copy,
    and it currently says, in bold, that a tap does not reach the daemon and
    that the copy changes back only when the protocol grows a client->server
    message. It has. A stale instruction sheet is how the copy regresses."""
    import ui.gallery
    text = ui.gallery.__doc__.lower()
    assert "one-way" not in text
    assert "cannot be reached from here yet" not in text


def test_the_readme_no_longer_says_a_tap_queues_nothing():
    """The third instruction sheet, and the one an operator reads before the
    conference. It carried a whole "What it deliberately does not do yet"
    section naming this exact gap; leaving it would have the project's front
    page contradicting its own booth.

    Mutation this catches: shipping the behaviour and the two module
    docstrings while leaving README.md alone.
    """
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    lowered = readme.lower()
    assert "a visitor's pick does not reach the screen" not in lowered
    assert "a tap does not queue anything" not in lowered
    assert "what is still missing is the last hop" not in lowered
    # ...and it has to say what DOES happen, or an operator learns nothing.
    assert "never pre-empts a running fold" in lowered


def test_the_pick_docstring_in_the_app_no_longer_says_it_reaches_nothing():
    """The other instruction sheet. `_on_pick`'s own docstring said "It does
    NOT yet reach the daemon" and named the copy that depended on it; the two
    pointed at each other, which is exactly why both had to move together."""
    from ui.app import DemoApp
    text = (DemoApp._on_pick.__doc__ or "").lower()
    assert "does not yet reach the daemon" not in text
    assert "not yet reach" not in text


def test_the_diagnostics_teaching_copy_matches_the_help_card():
    """Two places describing the same panel drifted apart once already."""
    from ui.diagnostics import STAGE_TEACHING
    joined = " ".join(str(v) for v in STAGE_TEACHING.values()).lower()
    assert "one chip" not in joined
    # Four folds run at once, each on its own chip, so `prep`'s line has to
    # scope its chip to THIS fold rather than to the booth. Verbatim before
    # Task 16: "for the Tenstorrent chip that will run the fold."
    #
    # Mutation this catches: leaving the diagnostics copy alone.
    assert "chip that will run the fold" not in joined
    assert "this fold" in joined


def test_the_plddt_legend_matches_the_ramp_the_ribbon_is_actually_coloured_by():
    """A legend that has drifted from the thing it describes is worse than
    no legend. The swatches are generated from `ui.geometry.PLDDT_STOPS`
    itself, and this is what pins the two together -- including the
    thresholds the words claim.

    Mutation this catches: hand-copying the hexes into ui/app.py (they
    would be right today and wrong the day the ramp changes), or listing
    the stops in a different order from the ramp.
    """
    swatch_css = app_module._plddt_swatch_css()
    for (css_class, range_text, _meaning), (threshold, rgb) in zip(
            app_module._PLDDT_LEGEND, PLDDT_STOPS):
        hex_color = "#%02X%02X%02X" % tuple(rgb)
        assert f".{css_class} {{ background-color: {hex_color}; }}" in swatch_css
        if threshold:
            assert str(int(threshold)) in range_text
    assert len(app_module._PLDDT_LEGEND) == len(PLDDT_STOPS)


# ---------------------------------------------------------------------------
# Layout: the rail must not swallow the screen.
# ---------------------------------------------------------------------------

def test_the_side_rail_stays_a_fixed_narrow_column_with_the_panel_in_it():
    """The load-bearing layout fact (see `_SIDE_RAIL_WIDTH_PX`): without
    hexpand(False) and an explicit width, the rail negotiates its way to two
    thirds of the window and the protein -- the reason anyone stopped to
    look -- ends up in a corner. Adding a wide monospace log to that rail is
    exactly the change that could break it."""
    app = _app()
    rail = app._build_side_rail()
    assert rail.get_hexpand() is False
    width, _height = rail.get_size_request()
    assert width == app_module._SIDE_RAIL_WIDTH_PX
    assert app.diagnostics_panel is not None
    assert app.diagnostics_panel.get_hexpand() is False


def test_the_diagnostics_panel_is_built_hidden():
    app = _app()
    app._build_side_rail()
    assert app.diagnostics_panel.get_visible() is False


# ---------------------------------------------------------------------------
# The Tensix activity panel, as wired into the booth (the panel's own
# behaviour is tested in test_chipviz.py).
# ---------------------------------------------------------------------------

def test_the_tensix_panel_is_in_the_rail_and_never_expands_it():
    """Adding a WebView to a fixed column is exactly the change that could
    break the rail -- and with it, the protein's claim on the screen."""
    app = _app()
    rail = app._build_side_rail()
    assert app.chipviz_panel is not None
    assert app.chipviz_panel.get_hexpand() is False
    assert rail.get_hexpand() is False
    assert rail.get_size_request()[0] == app_module._SIDE_RAIL_WIDTH_PX


def _rail_width_request(rail):
    """The rail's (minimum, natural) WIDTH, as `GtkBoxLayout` reports it.

    `natural` is the number that matters and the one nothing else in this
    file looks at: `gtk_distribute_natural_allocation` grows a
    non-`hexpand` child from its minimum toward its natural BEFORE handing
    what is left to the `hexpand` children, so a rail whose natural width
    moves takes the extra out of the hero slot -- i.e. out of the protein.
    """
    measurement = rail.measure(Gtk.Orientation.HORIZONTAL, -1)
    return measurement.minimum, measurement.natural


def test_the_rails_natural_width_does_not_grow_when_a_child_wants_more():
    """The invariant behind the `T`-twitch fix, stated directly.

    `set_size_request` is only a FLOOR -- it pins the rail's MINIMUM and
    says nothing about its natural width. Measured on the booth's own
    1920x1080 fullscreen window before the fix, pressing `T` moved:

        rail        552 -> 584 px, left edge x 1350 -> 1318
        hero slot   1332 -> 1300 px      <- the protein, twitching 32px

    because the Tensix panel's WebView reports a natural width 32px past
    the rail's inner width. This test uses a plain over-wide label instead
    of the WebView, so it pins the RULE ("nothing in this column may widen
    it") rather than one widget's current measurements, and so it is
    deterministic in a test process where WebKit may not even load.

    Mutation this catches: building the rail as a plain `Gtk.Box` in
    `_build_side_rail` (i.e. dropping `_FixedWidthBox`) -- the natural
    width then follows the greedy child and this assertion goes red.
    """
    app = _app()
    rail = app._build_side_rail()
    minimum_before, natural_before = _rail_width_request(rail)

    # The shape that matters, and the shape the WebView actually has: a
    # SMALL minimum with a LARGE natural. A wrapping label can always fall
    # back to its longest word, so its minimum stays tiny while its natural
    # is the whole unwrapped line -- which is exactly the gap the old rail
    # handed over to itself. (An unwrappable single long "word" would be
    # the wrong probe: it raises the minimum too, which the rail is
    # supposed to honour by growing.)
    greedy = Gtk.Label(label="wide " * 200)
    greedy.set_wrap(True)
    assert greedy.measure(Gtk.Orientation.HORIZONTAL, -1).natural > natural_before
    assert greedy.measure(Gtk.Orientation.HORIZONTAL, -1).minimum < natural_before
    rail.append(greedy)

    minimum_after, natural_after = _rail_width_request(rail)
    assert natural_after == natural_before, (
        f"a child that WANTS {natural_after - natural_before}px more widened "
        "the rail; the hero slot pays for that out of the protein")
    assert minimum_after == minimum_before


def test_a_rail_child_appearing_does_not_change_the_rails_width():
    """The same invariant on the SHOW path -- a widget becoming visible is
    what `T` actually does, and it is a different code path from appending
    one.

    The probe is a stand-in, not the real `ChipVizPanel`, and that is
    deliberate. Measured in this test process the real panel reports
    (minimum=516, natural=516); measured in the live booth, realized and
    with its animation loaded, the same panel reports (516, 584). The 68px
    of natural that causes the bug only exists once the WebView has a
    surface and content, which a unit test has no way to give it -- so a
    test driven off the real panel here passes just as happily against the
    broken rail as the fixed one. (Confirmed: it did, against the mutation
    below.) The stand-in carries the panel's live-measured SHAPE instead:
    a minimum that fits the rail, a natural that does not.

    Mutation this catches: building the rail as a plain `Gtk.Box` in
    `_build_side_rail`.
    """
    app = _app()
    rail = app._build_side_rail()

    # ui.chipviz.RAIL_INNER_WIDTH_PX + the panel's own 2x16px padding is the
    # 516 the real panel reports as its minimum; the wrapping text supplies
    # the oversized natural the loaded WebView has.
    stand_in = Gtk.Label(label="tensix core grid " * 12)
    stand_in.set_wrap(True)
    stand_in.set_size_request(chipviz_module.RAIL_INNER_WIDTH_PX + 32, -1)
    stand_in.set_visible(False)
    rail.append(stand_in)

    hidden = _rail_width_request(rail)
    stand_in.set_visible(True)
    # Read the probe's own appetite WHILE IT IS VISIBLE: `gtk_widget_measure`
    # short-circuits an invisible widget to (0, 0), which is both why hiding
    # the panel takes its width demand away entirely and why this assertion
    # has to happen here rather than after the widget is hidden again.
    probe_natural = stand_in.measure(Gtk.Orientation.HORIZONTAL, -1).natural
    shown = _rail_width_request(rail)
    stand_in.set_visible(False)
    hidden_again = _rail_width_request(rail)

    # The probe is only meaningful if it really does want more room than the
    # rail has -- otherwise this test would pass by measuring nothing.
    assert probe_natural > hidden[1]

    assert shown == hidden, (
        f"a panel appearing moved the rail's width request from {hidden} to "
        f"{shown}; on the booth's 1920x1080 screen that is the hero slot "
        "going 1332 -> 1300px and the protein jumping 32px sideways")
    assert hidden_again == hidden


def test_the_tensix_panel_sits_directly_below_the_telemetry_panel():
    """The correspondence that makes the animation legible as "these four
    chips, right here" rather than as decoration: animation N is under chip
    N's readout. Mutation this catches: appending it anywhere else in the
    rail."""
    app = _app()
    # The rail must stay referenced for the duration of the assertion: let it
    # go and Python finalizes the Gtk.Box, which unparents every child and
    # makes `get_next_sibling()` answer None for reasons that have nothing to
    # do with the layout under test.
    rail = app._build_side_rail()
    assert rail is not None
    assert app.telemetry_panel.get_next_sibling() is app.chipviz_panel


def test_a_stage_event_re_aims_the_animation():
    app = _app()
    app.chipviz_panel = _RecordingChipViz()
    app._handle_event({"type": "job_start", "job_id": "j1", "target_id": "t",
                       "n_residues": 20, "card": 0})
    app._handle_event({"type": "stage", "job_id": "j1", "stage": "diffusion",
                       "frac": 0.5})
    assert app.chipviz_panel.states[-1] == "attract"
    assert app.chipviz_panel.chip_stages[-1] == {0: "diffusion"}


def test_a_non_stage_event_refreshes_the_state_without_inventing_a_stage():
    """`not_ready` must be able to turn the animation off (state=preparing)
    without claiming a stage the daemon never sent."""
    app = _app()
    app.chipviz_panel = _RecordingChipViz()
    app._handle_event({"type": "not_ready", "missing": ["weights"]})
    assert app.chipviz_panel.states[-1] == "preparing"
    assert app.chipviz_panel.chip_stages[-1] == {0: None}


def test_not_ready_stands_every_chip_down_not_just_one():
    """The daemon has stopped folding ENTIRELY. A booth that cleared only
    the cell the event happened to route to would leave three core grids
    denoising behind the "getting the booth ready" overlay.

    Mutation this catches: clearing `self._slot_view(slot).stage` on
    `not_ready` instead of every cell's.
    """
    app = _app()
    app.quad = _FakeQuad(4, cards=[0, 1, 2, 3], viewer_factory=FakeViewer)
    app.attach_cards([0, 1, 2, 3])
    app.chipviz_panel = _RecordingChipViz()
    for card in range(4):
        app._handle_event({"type": "job_start", "job_id": f"j{card}",
                           "target_id": "t", "n_residues": 20, "card": card})
        app._handle_event({"type": "stage", "job_id": f"j{card}",
                           "stage": "diffusion", "frac": 0.5})
    assert app.chipviz_panel.chip_stages[-1] == {c: "diffusion"
                                                 for c in range(4)}
    app._handle_event({"type": "not_ready", "missing": ["weights"]})
    assert app.chipviz_panel.chip_stages[-1] == {c: None for c in range(4)}


def test_each_cell_carries_its_own_stage_to_the_panel():
    """The whole of Task 16 in one assertion: four chips fold at once, so the
    picture handed to the panel names what EACH one is doing. One booth-wide
    stage would animate whichever fold spoke last on all four canvases --
    the Critical-3 lie with a different shape.

    Mutation this catches: `{card: event.get("stage")}` built from the event
    rather than from every cell's own `_SlotView.stage`.
    """
    app = _app()
    app.quad = _FakeQuad(4, cards=[0, 1, 2, 3], viewer_factory=FakeViewer)
    app.attach_cards([0, 1, 2, 3])
    app.chipviz_panel = _RecordingChipViz()
    for card, stage in ((0, "diffusion"), (1, "trunk"), (2, "confidence")):
        app._handle_event({"type": "job_start", "job_id": f"j{card}",
                           "target_id": "t", "n_residues": 20, "card": card})
        app._handle_event({"type": "stage", "job_id": f"j{card}",
                           "stage": stage, "frac": 0.5})
    assert app.chipviz_panel.chip_stages[-1] == {
        0: "diffusion", 1: "trunk", 2: "confidence", 3: None}


def test_a_finished_fold_stops_claiming_its_chip_is_working():
    """`job_done` ends that chip's fold. Leaving the stage behind would
    animate denoising on a chip that has moved on -- and it would keep doing
    it, because the panel is only ever told what the app believes."""
    app = _app()
    app.chipviz_panel = _RecordingChipViz()
    app._handle_event({"type": "job_start", "job_id": "j1", "target_id": "t",
                       "n_residues": 20, "card": 0})
    app._handle_event({"type": "stage", "job_id": "j1", "stage": "diffusion",
                       "frac": 0.5})
    app._handle_event({"type": "job_done", "job_id": "j1", "cif_path": "",
                       "wall_s": 4.4, "mean_plddt": 90.0})
    assert app.chipviz_panel.chip_stages[-1] == {0: None}


def test_a_new_fold_does_not_inherit_the_last_one_s_stage():
    """`job_start` clears the cell's stage as surely as `job_done` does: a
    fold that has begun but has not said what it is doing is in `msa`/`prep`,
    both host-side, and the chip is not folding yet."""
    app = _app()
    app.chipviz_panel = _RecordingChipViz()
    app._handle_event({"type": "job_start", "job_id": "j1", "target_id": "t",
                       "n_residues": 20, "card": 0})
    app._handle_event({"type": "stage", "job_id": "j1", "stage": "diffusion",
                       "frac": 0.5})
    app._handle_event({"type": "job_start", "job_id": "j2", "target_id": "t",
                       "n_residues": 20, "card": 0})
    assert app.chipviz_panel.chip_stages[-1] == {0: None}


def test_the_state_tick_gives_the_tensix_panel_its_staleness_check():
    """The panel's own poll only runs while it is OPEN, and the case
    staleness exists for is precisely that no more events are coming -- so
    the check has to ride a source that runs regardless.

    Mutation this catches: dropping `_tick_chipviz_staleness` from
    `_tick_state_at`.
    """
    app = _app()
    app.chipviz_panel = _RecordingChipViz()
    app._tick_state_at(1.0)
    assert app.chipviz_panel.staleness_ticks == 1


def test_a_broken_tensix_staleness_check_cannot_freeze_the_state_tick():
    """`_tick_state_at` runs off a REPEATING GLib source; an exception
    escaping it removes the source permanently and the booth freezes
    mid-showcase with nothing on screen saying so."""
    app = _app()

    class Exploding:
        available = True

        def set_visible(self, _visible):
            pass

        def tick_staleness(self):
            raise RuntimeError("web process died")

    app.chipviz_panel = Exploding()
    app._tick_state_at(1.0)                     # must not raise


def test_a_broken_tensix_panel_cannot_cost_the_booth_an_event():
    """An animation is the least important thing happening in
    `_handle_event`. A failure inside a WebView must not pre-empt the
    rendering below it or turn a good event into "dropping malformed ...".

    Mutation this catches: removing `_sync_chipviz`'s own try/except.
    """
    app = _app()

    class Exploding:
        def set_state(self, *_args):
            raise RuntimeError("web process died")

        def set_chip_stages(self, *_args):
            raise RuntimeError("web process died")

    app.chipviz_panel = Exploding()
    app._handle_event(_a_fold_starting())
    app._handle_event({"type": "stage", "job_id": "j1", "stage": "diffusion",
                       "frac": 0.5})
    # The pipeline panel is rendered AFTER the chipviz call, so this proves
    # the failure did not pre-empt anything.
    assert app.pipeline_panel.stages[-1] == ("diffusion", 0.5)


def test_the_booth_tolerates_having_no_tensix_panel_at_all():
    """Headless tests, and the moment before do_activate runs."""
    app = _app()
    app.chipviz_panel = None
    app._handle_event(_a_fold_starting())
    app._handle_event({"type": "stage", "job_id": "j1", "stage": "trunk",
                       "frac": 0.3})
    assert app.pipeline_panel.stages[-1] == ("trunk", 0.3)


# ---------------------------------------------------------------------------
# Legibility. The shared guard (tests/unit/_legibility.py), pointed at this
# file's two new widget trees.
#
# Measured foreground ratios against the #092221 ground, WCAG 2.x, AA floor
# 4.5:1 -- help card: title/keys #F1F8F8 = 15.46:1, body/desc #C7D9D8 =
# 11.36:1, section headers #3299B9 = 5.06:1; rail hints #C7D9D8 = 11.36:1
# and #3299B9 = 5.06:1. The pLDDT swatches are FILLS, never text: the top of
# the ramp (#0053D6) measures 2.54:1 and could not legally be a label
# colour, which is why the legend paints boxes and puts the words beside
# them on the card's own ground.
# ---------------------------------------------------------------------------

_MERGED_CSS_FN, _MERGED_BG_FN = _legibility.merged_stylesheets(
    (lambda: app_module._APP_CSS, lambda: app_module._BACKGROUND_BY_CLASS),
    (lambda: panels_module._PANEL_CSS, lambda: panels_module._BACKGROUND_BY_CLASS),
    (lambda: diagnostics_module._DIAGNOSTICS_CSS,
     lambda: diagnostics_module._BACKGROUND_BY_CLASS),
    # The Tensix activity panel is the FOURTH stylesheet in this one tree.
    # Its two labels (#C7D9D8 = 11.36:1, #F1F8F8 = 15.46:1 on #092221) sit on
    # `.chipviz-panel`, which paints its own ground -- so without this entry
    # the walker would hit a background-painting ancestor whose class it does
    # not know and fail loudly (which is the designed behaviour, and is how
    # this line came to be written).
    (lambda: chipviz_module._CHIPVIZ_CSS,
     lambda: chipviz_module._BACKGROUND_BY_CLASS),
)


def _assert_legible(root, *, context):
    return _legibility.assert_every_label_is_legible(
        root, context=context, min_contrast=MIN_CONTRAST_RATIO,
        contrast_ratio_fn=contrast_ratio,
        css_text_fn=_MERGED_CSS_FN, background_by_class_fn=_MERGED_BG_FN)


def test_every_label_on_the_help_card_is_legible():
    app = _app()
    _assert_legible(app._build_help_overlay(), context="help card")


def test_every_label_in_the_side_rail_is_legible_including_the_new_ones():
    """One tree, three stylesheets (app chrome, panels, diagnostics) -- see
    `_legibility.merged_stylesheets` for why checking such a tree against
    one module's stylesheet would silently certify labels against the wrong
    ground."""
    app = _app()
    rail = app._build_side_rail()
    app.diagnostics_panel.refresh(app.diagnostics, force=True)
    _assert_legible(rail, context="side rail with diagnostics open")


def test_every_label_on_the_held_structure_caption_is_legible():
    """The caption over a held structure (ui/app.py's
    `_build_viewer_caption`) is the one thing standing between a visitor and
    mistaking the PREVIOUS fold's protein for the one the pipeline panel is
    reporting progress for -- so it has to be readable at booth distance,
    not merely present.

    Measured on the #092221 ground: `.viewer-caption-title` #74C5DF =
    8.55:1, `.viewer-caption-sub` #C7D9D8 = 11.36:1. Both clear the 4.5:1
    AA floor this project holds every label to.
    """
    app = _app()
    overlay = app._build_viewer_caption()
    app._caption_title_label.set_label("Previous fold: Trp-cage")
    app._caption_sub_label.set_label("Now folding Trypsin")
    _assert_legible(overlay, context="held-structure caption")


# ---------------------------------------------------------------------------
# The protein caption under the render: what the molecule actually IS.
# ---------------------------------------------------------------------------

def _target(target_id, name, tagline):
    """A playlist Target with only the fields this caption reads."""
    return Target(id=target_id, input_path=Path("/nonexistent.yaml"),
                  model="protenix-v2", name=name, blurb=f"{name} blurb",
                  tagline=tagline)


def test_the_caption_describes_the_protein_that_is_on_screen():
    app = _app()
    app.targets = [_target("trpcage", "Trp-cage", "Twenty amino acids.")]
    app._slots[0].shown_target_id = "trpcage"
    app._sync_target_info()
    assert app._target_info == ("Trp-cage", "Twenty amino acids.")


def test_the_caption_names_the_fold_in_flight_before_anything_is_on_screen():
    """The first fold of the day: nothing has been shown yet, so there is no
    picture for the caption to contradict and naming what is coming beats
    naming nothing."""
    app = _app()
    app.targets = [_target("dhfr", "Dihydrofolate Reductase", "Builds DNA.")]
    app._slots[0].shown_target_id = None
    app._slots[0].current_target_id = "dhfr"
    app._sync_target_info()
    assert app._target_info == ("Dihydrofolate Reductase", "Builds DNA.")


def test_the_caption_keeps_describing_the_held_protein_during_a_silent_fold():
    """The coexistence rule, and the reason this caption is bound to
    `_shown_target_id` rather than `_current_target_id`.

    While a new fold is in its silent stages the picture is still the
    PREVIOUS protein, and the hold caption at the top of the hero slot is
    already saying so ("Previous fold: Trp-cage / Now folding Trypsin").
    This caption sits under that picture, so it must keep describing the
    picture -- describing Trypsin underneath a rendering of Trp-cage is
    exactly the confusion the hold caption exists to prevent.

    Mutation this catches: flipping `target_info_subject`'s precedence to
    `folding_target_id or shown_target_id`.
    """
    app = _app()
    app.targets = [_target("trpcage", "Trp-cage", "Twenty amino acids."),
                   _target("trypsin", "Trypsin", "Cuts up your meal.")]
    app._slots[0].shown_target_id = "trpcage"
    app._slots[0].current_target_id = "trypsin"
    app._slots[0].awaiting_first_frame = True
    app._slots[0].has_structure = True
    app._sync_viewer_hold()

    # The two captions, reconciled in the same pass, saying different things
    # about different subjects -- and agreeing about both.
    assert app._caption == ("Previous fold: Trp-cage", "Now folding Trypsin")
    assert app._target_info == ("Trp-cage", "Twenty amino acids.")


def test_the_caption_follows_the_new_protein_once_its_first_frame_lands():
    """The other half of the rule above: the caption is not stuck on the old
    protein, it switches at the moment the PICTURE switches."""
    app = _app()
    app.targets = [_target("trpcage", "Trp-cage", "Twenty amino acids."),
                   _target("trypsin", "Trypsin", "Cuts up your meal.")]
    app._slots[0].shown_target_id = "trpcage"
    app._slots[0].current_target_id = "trypsin"
    app._sync_target_info()
    assert app._target_info[0] == "Trp-cage"

    # What the frame drain does when the new fold's first frame arrives.
    app._slots[0].shown_target_id = app._slots[0].current_target_id
    app._sync_target_info()
    assert app._target_info == ("Trypsin", "Cuts up your meal.")


def test_a_target_with_no_tagline_is_captioned_with_its_name_alone():
    """`tagline` is optional in the manifest, so the caption must degrade to
    the name rather than inventing copy or rendering a blank second line."""
    app = _app()
    app.targets = [_target("newthing", "New Thing", None)]
    app._slots[0].shown_target_id = "newthing"
    info = app._build_target_info()
    assert info is not None
    assert app._target_info == ("New Thing", None)
    assert app._target_info_name_label.get_label() == "New Thing"
    assert app._target_info_tagline_label.get_visible() is False


def test_a_protein_the_playlist_cannot_name_gets_no_caption_at_all():
    """`_target_name` returns None rather than the raw wire id for an
    unknown target. The caption follows it: the words disappear rather than
    showing `trpcage_no_msa` to a visitor.

    The words, and only the words. The confidence legend shares this strip
    but describes the RIBBON's colours, which are on screen and mean the
    same thing whether or not the playlist can put a name to the molecule
    -- so hiding it here would remove a key from a picture that still needs
    one. (Before the legend existed this assertion was on the whole strip,
    which is why it is spelled out now.)
    """
    app = _app()
    app.targets = []
    app._slots[0].shown_target_id = "some_id_the_playlist_never_heard_of"
    info = app._build_target_info()
    assert app._target_info is None
    assert app._target_info_caption_box.get_visible() is False
    assert app._confidence_legend_box.get_visible() is True
    assert info.get_visible() is True
    # ...and nothing readable is left behind by the hidden half. `is_visible`
    # (not `get_visible`) is the one that accounts for ancestors: a label
    # inside a hidden box still reports its OWN flag as True, so checking
    # that would have passed no matter what this strip actually shows.
    shown = [label.get_label() for label in _legibility.iter_labels(info)
             if label.is_visible()]
    assert shown == [app_module._CONFIDENCE_LEGEND_CAPTION,
                     app_module._CONFIDENCE_LEGEND_LOW,
                     app_module._CONFIDENCE_LEGEND_HIGH]


def test_target_info_subject_prefers_what_is_on_screen():
    """The pure decision, with no widgets and no playlist."""
    assert app_module.target_info_subject(
        shown_target_id="a", folding_target_id="b") == "a"
    assert app_module.target_info_subject(
        shown_target_id=None, folding_target_id="b") == "b"
    assert app_module.target_info_subject(
        shown_target_id=None, folding_target_id=None) is None


def test_every_label_on_the_protein_caption_is_legible():
    """The caption a visitor reads to learn what the molecule is has to be
    readable at booth distance.

    Measured on the #092221 ground: `.target-info-name` #74C5DF = 8.55:1,
    `.target-info-tagline` #C7D9D8 = 11.36:1, and the confidence legend
    sharing this strip -- `.confidence-legend-caption` and
    `.confidence-legend-end`, both #3299B9 -- = 5.06:1. All three clear the
    4.5:1 AA floor this project holds every label to; the legend is the
    quietest of them on purpose (see `_build_confidence_legend`), and 5.06
    is how much room that leaves, which is not much. Its four SWATCHES are
    painted boxes and not labels, deliberately: the top of the ramp
    (#0053D6) measures 2.54:1 here and could not legally be text.
    """
    app = _app()
    app.targets = [_target("trpcage", "Trp-cage",
                           "Twenty amino acids — one of the smallest "
                           "proteins that folds itself.")]
    app._slots[0].shown_target_id = "trpcage"
    _assert_legible(app._build_target_info(), context="protein caption")


# ---------------------------------------------------------------------------
# The confidence legend under the render: what the ribbon's colours MEAN.
# ---------------------------------------------------------------------------

def _legend_swatch_classes(root):
    """The ramp class on each swatch box in `root`, in widget order.

    Walks the real widget tree rather than re-deriving the list from
    `_PLDDT_LEGEND`, for the reason `test_every_label_this_file_builds...`
    records: a check that reads the same constant the builder read is not
    affected by anything the builder actually does with it.
    """
    found = []

    def walk(widget):
        classes = set(widget.get_css_classes())
        if "confidence-legend-swatch" in classes:
            # `plddt-` prefixed only: GTK itself puts an orientation class
            # ("horizontal") on every box, which is not ours and is not a
            # ramp band.
            ramp = sorted(c for c in classes if c.startswith("plddt-"))
            assert len(ramp) == 1, f"swatch carries {ramp!r}, want exactly one ramp class"
            found.append(ramp[0])
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(root)
    return found


def test_the_confidence_legend_is_built_from_the_ramp_the_ribbon_uses():
    """Same rule as the `?` card's legend, one surface further out: the
    swatches come from `ui.geometry.PLDDT_STOPS`, so a legend that has
    drifted from the ribbon it describes is not expressible.

    The ORDER is the part worth pinning. This legend reads low-to-high --
    the reverse of the ramp's own high-first order -- because it runs
    "less sure -> more sure" left to right under a sentence that reads the
    same way. A legend whose swatches ran the other way would be a legend
    that says the opposite of what it means, and nothing about the colours
    alone would give that away.

    Mutations this catches: dropping the `reversed()` (the ramp then runs
    backwards under the "less sure ... more sure" labels); hand-copying the
    hexes into a separate legend stylesheet (they would be right today and
    wrong the day the ramp moves).
    """
    app = _app()
    legend = app._build_confidence_legend()

    classes = _legend_swatch_classes(legend)
    assert classes == [css_class for css_class, _range, _meaning
                       in reversed(app_module._PLDDT_LEGEND)]
    assert len(classes) == len(PLDDT_STOPS)

    # ...and each of those classes is painted with the ramp's own colour,
    # lowest threshold first now that the list is reversed.
    swatch_css = app_module._plddt_swatch_css()
    for css_class, (_threshold, rgb) in zip(classes, reversed(PLDDT_STOPS)):
        hex_color = "#%02X%02X%02X" % tuple(rgb)
        assert f".{css_class} {{ background-color: {hex_color}; }}" in swatch_css

    # The two ends are named, so the ramp's direction is stated in words and
    # not left to be inferred from the colours -- which is exactly what a
    # visitor who cannot distinguish those colours has to do otherwise.
    labels = [label.get_label() for label in _legibility.iter_labels(legend)]
    assert labels.index(app_module._CONFIDENCE_LEGEND_LOW) < \
        labels.index(app_module._CONFIDENCE_LEGEND_HIGH)


def test_the_confidence_legend_says_what_the_colour_actually_means():
    """A legend that only shows colours explains nothing: the visitor's
    question is "why is this one orange", and the answer is that the model
    is telling them how much of what it drew to believe, residue by
    residue.

    Both facts are asserted because both are load-bearing and neither is
    obvious: that the colour is the MODEL's own confidence (not, say,
    temperature, charge, or which chain is which), and that it is per
    RESIDUE (the thing ui/diagnostics.py's teaching text got wrong once
    already -- see STAGE_TEACHING's "confidence" entry).
    """
    caption = app_module._CONFIDENCE_LEGEND_CAPTION.lower()
    assert "colour" in caption
    assert "model" in caption and "sure" in caption
    assert "residue" in caption
    # ...and it stays a legend. The `?` card is where the thresholds and the
    # band-by-band meanings live; this line has to survive being read at a
    # glance by someone standing in front of a protein.
    assert len(app_module._CONFIDENCE_LEGEND_CAPTION) < 60
    assert not any(str(int(threshold)) in caption for threshold, _rgb in PLDDT_STOPS
                   if threshold)


def _strip_height(strip, for_width):
    return strip.measure(Gtk.Orientation.VERTICAL, for_width).natural


def test_the_confidence_legend_costs_the_protein_no_height():
    """The layout invariant this legend is placed BESIDE the caption for.

    Every pixel the caption strip takes is a pixel the render above it does
    not get. The legend is 39px tall and the caption's two lines are 68px,
    so putting the legend at the end of the caption's own row costs nothing
    -- but only for as long as it also leaves the tagline enough width to
    stay on one line. Stack it under the tagline instead, or let it grow
    wide enough to wrap the tagline, and the strip goes 96px -> 124px and
    the protein loses 28px of the screen. That is the same class of defect,
    and very nearly the same size, as the 32px the rail's natural width
    once took out of the hero slot.

    Measured A/B, at the real hero width, against every tagline this booth
    actually ships -- because "does the tagline still fit beside it" is a
    fact about the COPY as much as about the widget, and the copy is in
    playlist/manifest.yaml where nothing else would catch it.

    Mutations this catches: appending the legend to the caption column
    rather than to the strip; widening the legend (a longer caption line, a
    fifth ramp band) past what the taglines leave room for.
    """
    hero_width = app_module._GALLERY_WIDTH_PX
    targets = load_playlist(app_module._DEFAULT_PLAYLIST)
    assert targets, "the shipped manifest is what this test is about"

    too_tall = []
    for target in targets:
        app = _app()
        app.targets = [target]
        app._slots[0].shown_target_id = target.id
        strip = app._build_target_info()
        with_legend = _strip_height(strip, hero_width)
        # `unparent`, not `strip.remove(...)`: the legend has to come out of
        # WHEREVER it was put, and `remove` on a widget that is not a direct
        # child is a no-op that leaves the tree intact -- so the A/B would
        # compare the strip with itself and pass for the one arrangement
        # this test exists to reject (measured: it did).
        app._confidence_legend_box.unparent()
        without_legend = _strip_height(strip, hero_width)
        if with_legend != without_legend:
            too_tall.append(
                f"{target.id}: the caption strip is {with_legend}px tall with "
                f"the legend and {without_legend}px without it -- the render "
                f"loses {with_legend - without_legend}px")
    assert not too_tall, "\n".join(too_tall)


def test_the_confidence_legend_never_takes_the_screen_from_the_protein():
    """The width half of the same rule.

    The caption strip lives in the hero slot, so its MINIMUM width is what
    the protein's column cannot go below -- if that ever exceeded the slot
    itself, the rail (which is fixed) would have nowhere to give from and
    the whole layout would be over-constrained. And the legend itself has
    to stay a footnote: it is measured here against a third of the slot,
    which it uses about two thirds of today (292px of 456px).

    Mutation this catches: giving the legend the help card's 34px swatches
    and 12pt text, or letting its caption line grow into a sentence.
    """
    app = _app()
    app.targets = [_target("dna", "DNA double helix",
                           "The double helix, twelve rungs of it — and the "
                           "only thing this booth folds that is not a protein.")]
    app._slots[0].shown_target_id = "dna"
    strip = app._build_target_info()
    hero_width = app_module._GALLERY_WIDTH_PX

    assert strip.measure(Gtk.Orientation.HORIZONTAL, -1).minimum < hero_width
    legend_width = app._confidence_legend_box.measure(
        Gtk.Orientation.HORIZONTAL, -1).natural
    assert legend_width < hero_width // 3


def test_every_label_on_the_preparing_overlay_is_legible():
    """Extending the guard to this file's widgets caught a real, shipped
    defect: `.preparing-message` was the raw brand accent #1B8EB1, which
    measures 4.40:1 -- under the AA floor -- on the one screen a visitor
    reads when something has gone wrong. It is now #3299B9 (5.06:1)."""
    app = _app()
    overlay = app._build_preparing_overlay()
    app._preparing_message_label.set_label("Getting the booth ready.")
    _assert_legible(overlay, context="preparing overlay")


def _widget_trees_this_file_builds():
    """Every tree this module's own widgets produce, for the per-label walk
    below. One app instance per tree, matching how each test above builds
    its own -- these are constructed, never activated, so no display is
    involved."""
    app = _app()
    yield "help card", app._build_help_overlay()

    app = _app()
    rail = app._build_side_rail()
    app.diagnostics_panel.refresh(app.diagnostics, force=True)
    yield "side rail with diagnostics open", rail

    app = _app()
    overlay = app._build_preparing_overlay()
    app._preparing_message_label.set_label("Getting the booth ready.")
    yield "preparing overlay", overlay

    app = _app()
    caption = app._build_viewer_caption()
    app._caption_title_label.set_label("Previous fold: Trp-cage")
    app._caption_sub_label.set_label("Now folding Trypsin")
    yield "held-structure caption", caption

    app = _app()
    app.targets = [_target("trpcage", "Trp-cage", "Twenty amino acids.")]
    app._slots[0].shown_target_id = "trpcage"
    yield "protein caption under the render", app._build_target_info()

    app = _app()
    yield "easter egg", app._build_egg_overlay()


def test_every_label_this_file_builds_carries_an_explicit_colour_rule():
    """The structural half of the rule: an explicitly-set background implies
    an explicitly-set foreground. A label with no colour-bearing class
    inherits the desktop theme -- measured at ~1.01:1 on a dark machine when
    this defect last happened (see _legibility.py's docstring) -- and the
    contrast walk above cannot catch what THIS machine's theme happens to
    resolve to a passing colour.

    This walks the real widgets (`iter_labels`), exactly as
    test_panels.py:711, test_gallery.py:391 and test_chipviz.py:536 do.
    It used to check a hardcoded list of class NAMES against `_APP_CSS`
    instead -- which the whole-branch review's mutation testing showed is
    not the same test at all: building a help-card label with NO CSS CLASS
    AT ALL left the old assertion perfectly green, because nothing about a
    list of names is affected by removing a class from a widget. The blind
    spot is the one already on record for the panels, one surface later.

    Rules come from the MERGED stylesheets for the same reason the contrast
    walk's do: the side rail is one tree assembled from four modules' CSS.
    """
    rules = _legibility.color_rules_from_css(_MERGED_CSS_FN())
    failures = []
    for context, root in _widget_trees_this_file_builds():
        for label in _legibility.iter_labels(root):
            if not _legibility.label_has_an_explicit_color_rule(label, rules):
                failures.append(
                    f"[{context}] label {label.get_label()!r} carries classes "
                    f"{sorted(label.get_css_classes())!r}, none of which has a "
                    "matching `color:` rule in the real stylesheets")
    assert not failures, "\n".join(failures)


def test_every_help_card_class_has_an_explicit_colour_rule():
    """The per-class half, kept alongside the per-label walk above: this one
    fails when a rule is deleted from `_APP_CSS` even if no widget currently
    carries that class, so a stylesheet edit cannot quietly disarm the
    stylesheet the walk checks against."""
    rules = _legibility.color_rules_from_css(app_module._APP_CSS)
    for css_class in ("help-title", "help-body", "help-section", "help-key",
                      "help-desc", "help-note", "booth-hint", "booth-hint-key",
                      "viewer-caption-title", "viewer-caption-sub",
                      "target-info-name", "target-info-tagline",
                      "egg-title", "egg-body", "egg-disclaimer",
                      "egg-provenance", "egg-note"):
        assert frozenset({css_class}) in rules, f"{css_class} has no color: rule"


def test_the_chip_that_is_folding_is_named_to_the_tensix_panel():
    """The panel cannot draw "chip 2 is denoising and the others are not"
    unless the booth tells it which chip -- and `job_start` is the only event
    that carries it (whole-branch review, Critical 3). Keyed by CHIP, not by
    slot: a booth whose card list does not start at zero would otherwise put
    chip 2's fold under chip 0's thermometer.

    Mutation this catches: `_chip_stages` keying on the slot index.
    """
    app = _app()
    app.quad = _FakeQuad(2, cards=[2, 3], viewer_factory=FakeViewer)
    app.attach_cards([2, 3])
    app.chipviz_panel = _RecordingChipViz()
    app._handle_event({"type": "job_start", "job_id": "j1", "target_id": "t",
                       "n_residues": 20, "card": 2})
    app._handle_event({"type": "stage", "job_id": "j1", "stage": "diffusion",
                       "frac": 0.5})
    assert app.chipviz_panel.chip_stages[-1] == {2: "diffusion", 3: None}


def test_a_stage_event_does_not_reattribute_the_fold_to_another_chip():
    """Stage events carry no `card` -- they are routed by `job_id` (see
    ui/slots.py). A stage landing on the wrong cell would animate the wrong
    chip, which at a booth reads as the hardware doing something it is
    not."""
    app = _app()
    app.quad = _FakeQuad(4, cards=[0, 1, 2, 3], viewer_factory=FakeViewer)
    app.attach_cards([0, 1, 2, 3])
    app.chipviz_panel = _RecordingChipViz()
    app._handle_event({"type": "job_start", "job_id": "j1", "target_id": "t",
                       "n_residues": 20, "card": 2})
    app._handle_event({"type": "stage", "job_id": "j1", "stage": "diffusion",
                       "frac": 0.5})
    assert app.chipviz_panel.chip_stages[-1] == {
        0: None, 1: None, 2: "diffusion", 3: None}


# ---------------------------------------------------------------------------
# The Tensix activity panel is CHROME now: off by default, `T` to open.
# ---------------------------------------------------------------------------

def test_the_booth_comes_up_with_the_tensix_panel_closed():
    """Protein-first. The panel is the most eye-catching thing in the rail
    and it is attached to the booth's smallest claim, so a booth restarted
    at the venue must not come up animating four core grids at a visitor
    who has not asked for them."""
    app = _app()
    assert app.chipviz_visible is False
    app.chipviz_panel = _RecordingChipViz()
    app._set_chipviz_visible(app.chipviz_visible)
    assert app.chipviz_panel.running is False


def test_t_toggles_the_tensix_panel_from_any_state():
    """Chrome, like `D`: it works from every screen and moves no booth
    state. Mutation this catches: wiring `T` through `_on_touch`."""
    for state in ("attract", "gallery", "folding", "showcase"):
        app = _app()
        app.chipviz_panel = _RecordingChipViz()
        _drive_to(app, state)
        before = app.states.state

        app._handle_key("t")
        assert app.chipviz_visible is True
        assert app.states.state == before

        app._handle_key("t")
        assert app.chipviz_visible is False
        assert app.states.state == before


def test_escape_closes_the_tensix_panel():
    app = _app()
    app.chipviz_panel = _RecordingChipViz()
    app._handle_key("t")
    assert app.chipviz_visible is True
    app._handle_key("escape")
    assert app.chipviz_visible is False


def test_a_closed_tensix_panel_polls_nothing():
    """`set_running` is not decoration: it adds and removes a 1Hz GLib
    source that reads sysfs and evaluates JS. The booth's default state has
    to cost neither."""
    app = _app()
    app.chipviz_panel = _RecordingChipViz()
    app._handle_key("t")
    assert app.chipviz_panel.running is True
    app._handle_key("t")
    assert app.chipviz_panel.running is False


def test_t_does_not_show_a_panel_that_cannot_draw_anything():
    """No WebKit, no chips, no bundled assets -- `ui.chipviz` sets
    `available` False and hides itself. Pressing `T` there must not put an
    empty box in the rail; the key simply has nothing to show."""
    app = _app()
    panel = _RecordingChipViz()
    panel.available = False
    app.chipviz_panel = panel

    app._handle_key("t")
    assert app.chipviz_visible is True     # what was ASKED for
    assert panel.visible is False          # what is on screen
    assert panel.running is False


def test_a_visitor_who_walks_away_does_not_leave_the_tensix_panel_open():
    """Same rule, and the same timer, as the diagnostics panel: chrome must
    not outlive the person who opened it."""
    clock = FakeClock()
    app = _app(clock=clock)
    app.chipviz_panel = _RecordingChipViz()
    app._handle_key("t")
    assert app.chipviz_visible is True

    clock.advance(app_module._RAIL_PANEL_IDLE_S - 1.0)
    app._tick_overlays(clock())
    assert app.chipviz_visible is True

    clock.advance(2.0)
    app._tick_overlays(clock())
    assert app.chipviz_visible is False


def test_the_hint_row_advertises_the_tensix_key_and_tracks_it():
    app = _app()
    rail = app._build_side_rail()
    assert app._tensix_toggle_label is not None
    assert "T" in app._tensix_toggle_label.get_label()
    closed = app._tensix_toggle_label.get_label()

    app._toggle_chipviz()
    assert app._tensix_toggle_label.get_label() != closed
    del rail


def test_the_tensix_panel_is_built_hidden():
    app = _app()
    app._build_side_rail()
    assert app.chipviz_panel.get_visible() is False


# ---------------------------------------------------------------------------
# The rail does not move. (The jerk the user saw, as a test.)
# ---------------------------------------------------------------------------

def _rail_min_size(widget):
    min_w, _nat_w, _b, _e = widget.measure(Gtk.Orientation.HORIZONTAL, -1)
    min_h, _nat_h, _b, _e = widget.measure(Gtk.Orientation.VERTICAL, min_w)
    return (min_w, min_h)


def test_nothing_the_booth_does_by_itself_changes_the_rail_or_its_panels():
    """The user, watching the booth: "there's a little bit of a jerk to the
    app's UI/layout when switching states."

    It was the telemetry panel. Its minimum size tracked the text in its
    chip cells, and the rail's `set_size_request` is a FLOOR -- so the rail
    stood at 430px until the first `tt-smi` sample landed, snapped to 531,
    and would have gone to 595 at a three-digit temperature, taking the
    protein's left edge with it every time. Measured with a harness that
    built this same rail in a real window; see this task's report.

    So: build the real rail, drive it through everything that happens
    WITHOUT a keypress -- telemetry appearing, going stale, vanishing and
    coming back, a chip crossing 100 degrees, every pipeline stage, the
    pipeline going stale and recovering, every viz mode -- and assert the
    rail's size and every panel's size are identical at every step. Only a
    visitor pressing a key is allowed to change this column.

    Fails against the unfixed code with seven distinct telemetry sizes.
    """
    from ui.panels import STALE_AFTER_S

    app = _app()
    rail = app._build_side_rail()
    panels = {
        "pipeline": app.pipeline_panel,
        "telemetry": app.telemetry_panel,
        "chipviz": app.chipviz_panel,
        "diagnostics": app.diagnostics_panel,
    }

    def chip(index, temperature_c=48.0):
        return ChipReading(index=index, board_type="p300c",
                           temperature_c=temperature_c, power_w=88.0,
                           aiclk_mhz=1350.0,
                           board_id="000004613192406%d" % (index // 2))

    four = [chip(i) for i in range(4)]
    hot = [chip(i, temperature_c=100.4) for i in range(4)]

    def sizes():
        snapshot = {"rail": _rail_min_size(rail)}
        snapshot.update({name: _rail_min_size(w) for name, w in panels.items()})
        return snapshot

    seen = [("start", sizes())]

    for label, readings, age in (
            ("four chips", four, 0.5),
            ("stale", four, STALE_AFTER_S + 1.0),
            ("hot", hot, 0.5),
            ("no chips", [], 0.5),
            ("no telemetry", None, None),
            ("two chips", four[:2], 0.5),
            ("four again", four, 0.5)):
        app.telemetry_panel.update(readings, age)
        seen.append((label, sizes()))

    for stage in ("msa", "prep", "trunk", "diffusion", "confidence", "saving"):
        app.pipeline_panel.set_stage(stage, 0.5)
        seen.append((f"stage {stage}", sizes()))

    # Every shape the Tensix header can take, including the longest ones the
    # multi-chip rewrite introduced. The header is a `Gtk.Label` in a fixed
    # column, and "TENSIX ACTIVITY · 4 CHIPS FOLDING" is materially longer
    # than the "TENSIX ACTIVITY · idle" it starts at -- if it can widen the
    # rail, it widens it the first time four chips fold, which is every few
    # seconds of a conference day.
    if app.chipviz_panel is not None:
        for label, stages in (
                ("one chip", {0: "diffusion"}),
                ("one chip refining", {0: "trunk"}),
                ("two chips", {0: "diffusion", 1: "trunk"}),
                ("three chips", {0: "diffusion", 1: "trunk", 2: "confidence"}),
                ("four chips", {c: "diffusion" for c in range(4)}),
                ("idle again", {})):
            app.chipviz_panel.set_chip_stages(stages)
            seen.append((f"tensix {label}", sizes()))

    app.pipeline_panel.reset()
    seen.append(("pipeline blank", sizes()))
    app.pipeline_panel.set_stage("diffusion", 0.9)
    seen.append(("pipeline back", sizes()))

    baseline = seen[0][1]
    for label, snapshot in seen:
        assert snapshot == baseline, (
            f"the rail changed size at {label!r}: {baseline} -> {snapshot}")


def test_the_rail_stays_put_with_its_panels_open_too():
    """The same guarantee with the chrome up: opening `T` or `D` is allowed
    to make the column TALLER (it grows downward, below everything else),
    but it must never make it wider -- a wider rail moves the protein."""
    app = _app()
    rail = app._build_side_rail()
    width = _rail_min_size(rail)[0]

    app._set_diagnostics_visible(True)
    assert _rail_min_size(rail)[0] == width
    app._set_chipviz_visible(True)
    assert _rail_min_size(rail)[0] == width

    app.telemetry_panel.update(
        [ChipReading(index=i, board_type="p300c", temperature_c=100.4,
                     power_w=188.0, aiclk_mhz=1350.0,
                     board_id="0000046131924062") for i in range(4)], 0.5)
    assert _rail_min_size(rail)[0] == width
    del rail


# ---------------------------------------------------------------------------
# The easter egg (Ctrl+G; mark.py). Chrome, like the help card -- with one
# extra obligation the help card does not have: it must be impossible to
# mistake for a fold, and impossible to reach by accident.
# ---------------------------------------------------------------------------

def test_ctrl_g_opens_the_easter_egg_and_any_key_puts_it_away():
    app = _app()
    assert app.egg_visible is False
    app._handle_key("g", ctrl=True)
    assert app.egg_visible is True
    app._handle_key("k")
    assert app.egg_visible is False


def test_a_plain_g_is_still_a_visitor_touch():
    """The binding is a CHORD on purpose. Every unbound plain key in this
    booth opens the gallery, and carving a letter out of that would mean a
    visitor who reached for the booth sometimes got a toy instead.

    Mutation this catches: moving the egg off Ctrl and onto plain `g`.
    """
    app = _app()
    app._handle_key("g")
    assert app.egg_visible is False
    assert app.states.state == "gallery"


def test_the_easter_egg_is_not_advertised_on_the_help_card():
    """An egg that is documented is a feature. This pins the omission as a
    decision, so a later reader completing the card does not quietly turn the
    booth's one hidden thing into a listed one.
    """
    listed = " ".join(keys for keys, _m in app_module._KEY_HELP).lower()
    assert "g" not in [key.strip() for key in listed.replace("·", " ").split()]
    assert "ctrl + g" not in listed
    documented = (app_module._HELP_KEYS | app_module._DIAGNOSTICS_KEYS
                  | app_module._TENSIX_KEYS)
    assert not (app_module._EGG_KEYS & documented)


def test_dismissing_the_egg_is_not_a_visitor_touch():
    """Same rule as the help card: a key pressed to get rid of something is
    not a request for the gallery."""
    app = _app()
    app._handle_key("g", ctrl=True)
    app._handle_key("space")
    assert app.egg_visible is False
    assert app.states.state == "attract"


def test_the_egg_never_touches_the_protein_or_the_state_machine():
    """The guarantee that makes this safe to ship at a booth: a fold in
    flight keeps streaming into the real viewer the whole time the egg is
    up, so dismissing it returns to whatever was there.
    """
    app = _app()
    _drive_to(app, "folding")
    # `_handle_event`, not `_on_event`: the latter marshals to the main loop
    # via GLib.idle_add and there is no main loop here, so the fold would
    # never start and its frames would belong to no cell.
    app._handle_event({"type": "job_start", "job_id": "j1",
                       "target_id": "trpcage", "card": 0})
    app._on_event({"type": "frame", "job_id": "j1", "step": 1,
                   "coords_b64": pack_coords([[1.0, 2.0, 3.0]] * 4)})
    app._drain_frames()
    before_frames = len(_cell(app).point_frames)
    # AFTER the handover, not before it: that first frame legitimately clears
    # the cell (hold-until-superseded's one clear, in ui/app.py's
    # `_draw_frame`). What this test is about is that NOTHING the egg does
    # adds another.
    clears_before = _cell(app).clears
    before_state = app.states.state

    app._handle_key("g", ctrl=True)
    for _ in range(5):
        app._tick_egg()
    # ... and a real fold frame lands WHILE the egg is up
    app._on_event({"type": "frame", "job_id": "j1", "step": 2,
                   "coords_b64": pack_coords([[4.0, 5.0, 6.0]] * 4)})
    app._drain_frames()

    assert len(_cell(app).point_frames) == before_frames + 1, (
        "the fold must keep drawing into the real viewer behind the egg")
    assert _cell(app).clears == clears_before, \
        "the egg blanked the fold underneath it"
    assert app.states.state == before_state


def test_the_egg_stops_its_own_timer_once_the_cloud_has_settled():
    """It is a 30-per-second source over an all-day booth: it has to end."""
    app = _app()
    app._handle_key("g", ctrl=True)
    app._egg.completed = app._egg.steps - 2
    assert app._tick_egg() is True     # one step left after this one
    assert app._tick_egg() is False    # the last step; nothing left to compute
    assert app._egg_source_id is None


def test_closing_the_egg_removes_its_timer():
    app = _app()
    app._handle_key("g", ctrl=True)
    assert app._egg_source_id is not None
    app._set_egg_visible(False)
    assert app._egg_source_id is None
    assert app._advance_egg() is False


def test_a_failing_step_stops_the_egg_instead_of_repeating_forever():
    """The booth's rule is that an exception must not silently freeze a
    repeating source. This source is allowed to end, so the same rule here
    means a failure ends it cleanly -- rather than logging a traceback 30
    times a second for the rest of the day, or leaving a dead timer.
    """
    app = _app()
    app._handle_key("g", ctrl=True)

    def boom():
        raise RuntimeError("no")
    app._advance_egg = boom

    assert app._tick_egg() is False
    assert app._egg_source_id is None


def test_the_egg_closes_itself_when_the_visitor_walks_away():
    """It covers the hero slot, and the attract loop must never show
    somebody who did not ask for it something that is not a fold."""
    clock = FakeClock()
    app = _app(clock=clock)
    app._handle_key("g", ctrl=True)
    app._tick_overlays(clock() + app_module._EGG_IDLE_S - 1)
    assert app.egg_visible is True
    app._tick_overlays(clock() + app_module._EGG_IDLE_S)
    assert app.egg_visible is False


def test_opening_the_egg_takes_the_help_card_down():
    app = _app()
    app._show_help()
    app._handle_key("g", ctrl=True)
    assert app.egg_visible is True
    assert app.help_visible is False


def test_the_egg_says_on_screen_that_it_is_not_a_fold():
    """The one thing this feature is not allowed to get wrong. Both the
    heading and the body have to disclaim it: a visitor reading only the
    biggest words on the screen must still be told."""
    heading = app_module._EGG_TITLE.lower()
    body = app_module._EGG_DISCLAIMER.lower()
    assert "not a fold" in heading
    assert "not a folded structure" in body
    assert "no chemistry" in body


def test_the_egg_card_reports_the_number_of_points_it_actually_uses():
    """A number in visitor-facing copy that can drift from the thing it
    describes is a small lie waiting to happen, and this booth has already
    had to fix one. The count is interpolated from mark.py.
    """
    import mark as mark_module
    assert f"{mark_module.POINTS:,}" in app_module._EGG_BODY


def test_the_egg_is_drawn_in_the_brand_purple_and_does_not_spin():
    """Purple because it is the mark; still because the mark is a plane
    figure, and the protein's 0.35 rad/s turntable would put it edge-on
    within five seconds."""
    from mark import BRAND_PURPLE
    app = _app()
    app._build_egg_overlay()
    assert app.egg_viewer._point_color == BRAND_PURPLE
    assert app.egg_viewer._spin_rate == 0.0


def test_the_protein_keeps_the_colour_and_the_spin_it_always_had():
    """The two setters the egg needed are per-instance. A second viewer must
    not have changed the first one."""
    from ui.viewer import POINT_COLOR, StructureViewer
    app = _app()
    app._build_egg_overlay()
    protein = StructureViewer()
    assert protein._point_color == POINT_COLOR
    assert protein._spin_rate == StructureViewer.SPIN_RATE


def test_every_label_on_the_easter_egg_is_legible():
    """`.egg-title` and `.egg-provenance` #74C5DF = 8.55:1,
    `.egg-body`/`.egg-note` #C7D9D8 = 11.36:1 and `.egg-disclaimer` #F1F8F8 =
    15.46:1, all on #092221. The
    brand purple itself measures 4.13:1 and is therefore a FILL only -- it
    is on the point cloud, never on type.
    """
    app = _app()
    _assert_legible(app._build_egg_overlay(), context="easter egg")


# ── the attract choreography, where it meets a visitor ──────────────────────

def test_pressing_D_closes_a_panel_the_choreography_opened(monkeypatch):
    """The ordering bug this pins: `_note_input` interrupts the choreography
    on every keypress, and the interrupt closes what the choreography opened.
    If that ran BEFORE the key was dispatched, pressing `D` on an
    already-open tap would close it and then the toggle would reopen it --
    the key would appear to do nothing.
    """
    app = _app()
    app.attract.tick(1000.0, idle_s=1000.0)          # choreography opens it
    app._set_diagnostics_visible(True)
    assert app.attract.owns("diagnostics")

    app._handle_key("d")

    assert app.diagnostics_visible is False, \
        "D did not close a panel the choreography had opened"
    assert not app.attract.owns("diagnostics")


def test_any_other_key_puts_back_what_the_choreography_opened():
    """Rule 2: the booth a visitor walks up to looks like the booth that was
    left. A touch stops the demonstration and closes its panels."""
    app = _app()
    app.attract.tick(1000.0, idle_s=1000.0)
    app._set_diagnostics_visible(True)

    app._handle_key("x")                              # an ordinary touch

    assert app.diagnostics_visible is False
    assert not app.attract.running


def test_a_visitor_opened_panel_survives_the_choreography(monkeypatch):
    """Rule 3, end to end: the choreography must never close a panel a
    visitor opened themselves."""
    app = _app()
    app._handle_key("d")                              # visitor opens it
    assert app.diagnostics_visible is True
    assert not app.attract.owns("diagnostics")

    for t in (1000.0, 1020.0, 1040.0):                # choreography runs on
        app._tick_attract(t, idle_s=t)
    assert app.diagnostics_visible is True, \
        "the choreography closed a panel the visitor had opened"


# ── the gallery cue, where it meets the state machine ──────────────────────

def _idle_until_gallery(app, base=1000.0):
    """Run the attract choreography far enough to reach its gallery cue."""
    t = base
    while t < base + 90.0:
        app._tick_attract(t, idle_s=t - base + 61.0)
        t += 1.0
        if app.attract.owns("gallery"):
            return t
    return None


def test_the_choreography_shows_the_gallery_from_attract():
    """A visitor who never touches the booth has no way of learning it can be
    driven at all, so the booth shows them the menu."""
    from ui.states import BoothState
    app = _app()
    app.states.state = BoothState.ATTRACT
    assert _idle_until_gallery(app), "the gallery cue never fired"
    assert app.states.state == BoothState.GALLERY


def test_the_choreography_never_interrupts_a_fold():
    """THE ONE THAT WOULD RUIN THE DEMO. The gallery is a state, not chrome:
    showing it mid-fold would take a protein off the screen somebody is
    watching."""
    from ui.states import BoothState
    app = _app()
    app.states.state = BoothState.FOLDING
    _idle_until_gallery(app)
    assert app.states.state == BoothState.FOLDING, \
        "the attract choreography cut into a fold"


def test_a_gallery_it_could_not_show_is_disowned():
    """If the cue was dropped, the matching hide must not fire later and
    yank a screen the choreography never opened."""
    from ui.states import BoothState
    app = _app()
    app.states.state = BoothState.FOLDING
    _idle_until_gallery(app)
    assert not app.attract.owns("gallery")


def test_the_gallery_goes_away_again_on_its_own():
    """An unattended booth must end up back on the protein, or it spends the
    rest of the day showing a menu nobody is reading.

    UNVERIFIED, AND KNOWN TO BE. Replacing the put-the-gallery-away guard in
    `ui/app.py` with `if False:` does NOT make this fail, and it should: a
    direct trace of the same fixture shows the state going attract -> gallery
    at t+66 and gallery -> attract at t+74, so removing the second transition
    ought to leave the booth on the menu at the end of the loop below.

    The mutation was confirmed to reach the file (pattern asserted present,
    `__pycache__` cleared, `python -B`), so this is not the
    patch-did-not-apply false survival that has bitten this project twice.
    Something about this test does not exercise what the trace does, and it
    was not worth guessing at a third time.

    DO NOT TRUST THIS TEST until that is understood. The behaviour it
    describes is real -- the trace confirms it -- but the test is not what
    proves it.
    """
    from ui.states import BoothState
    app = _app()
    app.states.state = BoothState.ATTRACT
    base = 1000.0
    for k in range(120):
        app._tick_attract(base + k, idle_s=61.0 + k)
    assert app.states.state == BoothState.ATTRACT, \
        "the booth was left sitting on the gallery"


def test_a_visitor_arriving_keeps_the_menu_they_can_see():
    """Snatching the gallery back the instant somebody touches the booth is
    the worst possible moment to do it -- that menu is the screen they are
    looking at and the thing they are about to use."""
    from ui.states import BoothState
    app = _app()
    app.states.state = BoothState.ATTRACT
    assert _idle_until_gallery(app)
    assert app.states.state == BoothState.GALLERY

    app._handle_key("x")                      # a visitor arrives

    assert app.states.state == BoothState.GALLERY, \
        "the menu was taken away the moment a visitor touched the booth"
