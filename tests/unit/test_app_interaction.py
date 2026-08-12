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

import pytest

import _legibility
from protocol.events import pack_coords
from ui import app as app_module
from ui import diagnostics as diagnostics_module
from ui import panels as panels_module
from ui.app import DemoApp
from ui.geometry import PLDDT_STOPS
from ui.panels import MIN_CONTRAST_RATIO, contrast_ratio

# The wiring tests' fakes are the right ones here too -- reusing them is
# also what keeps "what a FakeViewer models" a single decision (see
# test_app_wiring.py's module docstring on why FakeViewer models `blend`).
from test_app_wiring import FakeClock, FakeStack, FakeViewer, RecordingPanel


class FakeSampler:
    def latest(self):
        return None

    def age_s(self):
        return None

    def start(self):
        pass

    def stop(self):
        pass


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
    app.viewer = FakeViewer()
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
    condition."""
    app = _app()
    quits = []
    app.quit = lambda: quits.append(True)
    app._handle_key("q")
    assert quits == []
    assert app.states.state == "gallery"      # ...it was just a touch


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

    app._handle_event({"type": "job_start", "job_id": "j1", "target_id": "t",
                       "model": "m", "card": 0, "n_residues": 20})
    app._handle_event({"type": "stage", "stage": "diffusion", "frac": 0.55})
    app._frames.put(_frame_event())
    app._drain_frames()
    app._handle_event({"type": "job_done", "job_id": "j1", "wall_s": 4.4,
                       "mean_plddt": 91.2})
    app._tick_state()               # stamps the showcase dwell
    clock.advance(5.0)
    app._tick_state()               # ...and expires it

    assert app.viewer.point_frames, "diffusion frames stopped reaching the viewer"
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
    assert "trpcage" in blob and "card 2" in blob


def test_drawn_frames_reach_the_diagnostics_log_with_their_geometry():
    """Frames are logged where they are DRAWN, so the log can honestly say
    a visitor saw them."""
    app = _app()
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
    assert app.viewer.point_frames, "the frame never reached the viewer"


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
    from gi.repository import Gtk

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
    }
    listed = " ".join(keys for keys, _meaning in app_module._KEY_HELP).lower()
    for key in app_module._HELP_KEYS | app_module._DIAGNOSTICS_KEYS:
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


def test_the_help_card_explains_both_rail_panels():
    panels_copy = " ".join(app_module._HELP_PANELS).lower()
    assert "pipeline" in panels_copy and "diffusion" in panels_copy
    assert "temperature" in panels_copy


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


def test_every_label_on_the_preparing_overlay_is_legible():
    """Extending the guard to this file's widgets caught a real, shipped
    defect: `.preparing-message` was the raw brand accent #1B8EB1, which
    measures 4.40:1 -- under the AA floor -- on the one screen a visitor
    reads when something has gone wrong. It is now #3299B9 (5.06:1)."""
    app = _app()
    overlay = app._build_preparing_overlay()
    app._preparing_message_label.set_label("Getting the booth ready.")
    _assert_legible(overlay, context="preparing overlay")


def test_every_help_card_class_has_an_explicit_colour_rule():
    """The structural half of the rule: an explicitly-set background implies
    an explicitly-set foreground. A class added to the card with no `color:`
    behind it inherits the desktop theme -- measured at ~1.01:1 on a dark
    machine when this defect last happened (see _legibility.py's docstring)
    -- and the contrast walk above cannot catch what a test never renders."""
    rules = _legibility.color_rules_from_css(app_module._APP_CSS)
    for css_class in ("help-title", "help-body", "help-section", "help-key",
                      "help-desc", "help-note", "booth-hint", "booth-hint-key"):
        assert frozenset({css_class}) in rules, f"{css_class} has no color: rule"
