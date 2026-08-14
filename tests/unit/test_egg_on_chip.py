"""The easter egg's other half: asking a chip for it, and what the card says.

The geometry is tested in `test_mark.py` and the scheduling in
`tests/unit/runner/test_egg_dispatch.py`. What is left -- and what this file
is entirely about -- is the honesty of one line of copy.

The booth's credibility rests on a visitor being able to believe that what it
says was computed really was. The egg is the one surface where that could go
wrong quietly: it looks the same whether a chip drew it or this laptop did, so
if the provenance line ever claims a chip it did not get, nothing on screen
would give it away. Every test below is a way for that claim to be wrong.

Headless throughout: no GTK main loop, no daemon, no socket, no hardware. The
egg's own timer is driven by hand, exactly as GLib would.
"""

import re
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

import pytest

import mark
from protocol.events import pack_coords
from ui import app as app_module
from ui.app import DemoApp

from test_app_wiring import FakeClock, FakeStack, FakeViewer, RecordingPanel


class _FakeClient:
    """An `EventClient` that records what the booth asked the daemon for."""

    def __init__(self, accept=True):
        self.accept = accept
        self.eggs = []
        self.picks = []

    def send_egg(self, egg_id):
        self.eggs.append(egg_id)
        return self.accept

    def send_pick(self, target_id):
        self.picks.append(target_id)
        return True


class _FakeLabel:
    def __init__(self):
        self.text = None

    def set_label(self, text):
        self.text = text


class _Sampler:
    def latest(self):
        return None

    def age_s(self):
        return None

    def start(self):
        pass

    def stop(self):
        pass


def _app(client=None, clock=None):
    app = DemoApp(socket_path=None, clock=clock or FakeClock())
    app.viewer = FakeViewer()
    app.telemetry_panel = RecordingPanel()
    app.pipeline_panel = RecordingPanel()
    app.sampler = _Sampler()
    app.screens = FakeStack()
    app.gallery = object()
    app._sync_to_state(force=True)
    # The egg's own widgets, stood in for: `_build_egg_overlay` makes a real
    # GL `StructureViewer`, and these tests never open a display.
    app.egg_viewer = FakeViewer()
    app._egg_provenance_label = _FakeLabel()
    if client is not None:
        app._client = client
    return app


def _egg_frame(app, step, total=mark.STEPS, card=2, seed=1234, egg_id=None,
               spread=1.0):
    return {"type": "egg_frame", "egg_id": egg_id or app._egg_id,
            "card": card, "seed": seed, "step": step, "total": total,
            "n_points": 3,
            "coords_b64": pack_coords([[spread, 0.0, 0.0],
                                       [0.0, spread, 0.0],
                                       [0.0, 0.0, spread]])}


# ── asking ──────────────────────────────────────────────────────────────────


def test_opening_the_egg_asks_the_daemon_for_a_chip():
    """The whole feature in one assertion: the arithmetic is not done here.

    Mutation this catches: reverting to a local `MarkCondensation()` on open,
    which is what this used to be and which still LOOKS right on screen.
    """
    client = _FakeClient()
    app = _app(client)
    app._handle_key("g", ctrl=True)
    assert client.eggs == [app._egg_id]
    assert app._egg_id


def test_no_descent_of_its_own_is_started_while_it_is_asking():
    """A cloud of noise is DRAWN so the card is not an empty box for up to
    six seconds -- but nothing steps it, so nothing on screen is descending
    until a chip's frames arrive or the fallback begins.

    Mutation this catches: stepping the preview, which would show a locally
    computed collapse under a card that says it is still asking, and then
    jump when the chip's own frames landed.
    """
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    assert app.egg_source == "asking"
    assert app._egg is None
    assert app._egg_preview is not None
    assert app._egg_preview.completed == 0
    assert len(app.egg_viewer.point_frames) == 1
    for _ in range(5):
        app._tick_egg()
    assert app._egg_preview.completed == 0
    assert len(app.egg_viewer.point_frames) == 1


def test_the_fallback_continues_the_cloud_that_was_already_on_screen():
    """No cut. The descent that starts when nobody answers begins from the
    exact cloud the visitor has been looking at, not a fresh draw of the same
    distribution."""
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    preview = app._egg_preview
    app._egg_deadline = 0.0
    app._tick_egg()
    assert app._egg is preview
    assert app._egg_preview is None


def test_while_asking_the_card_claims_no_chip():
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    assert app._egg_provenance_label.text == app_module._EGG_PROVENANCE["asking"]
    assert "computed on chip" not in app._egg_provenance_label.text.lower()


def test_the_timer_keeps_running_while_the_booth_waits_for_a_chip():
    """A tick that returned False here would remove the source and leave the
    card up forever with nothing on it -- the frames would arrive and nobody
    would draw them."""
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    for _ in range(5):
        assert app._tick_egg() is True
    assert app._egg is None


# ── it really ran on a chip ─────────────────────────────────────────────────


def test_a_frame_from_a_chip_is_drawn_and_the_card_says_which_chip():
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    drawn = len(app.egg_viewer.point_frames)
    app._on_event(_egg_frame(app, 0, card=3))
    assert app._tick_egg() is True
    assert app.egg_source == "device"
    assert app.egg_card == 3
    assert app._egg_preview is None, "the waiting cloud must not linger"
    assert app._egg_provenance_label.text == (
        "Computed on chip 3 — like everything else here.")
    assert len(app.egg_viewer.point_frames) == drawn + 1


def test_the_chip_is_claimed_only_once_a_frame_has_actually_been_drawn():
    """Not when the request was sent, and not when the event arrived. There
    must be no window in which the card names a chip that has produced
    nothing.

    Mutation this catches: flipping the label in `_on_event` (on the reader
    thread, before anything is on screen) instead of in `_draw_egg_frame`.
    """
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._on_event(_egg_frame(app, 0, card=1))
    assert app.egg_source == "asking", "the event alone must not make the claim"
    assert "computed on chip" not in app._egg_provenance_label.text.lower()
    app._tick_egg()
    assert app.egg_source == "device"


def test_the_frames_are_played_one_per_tick_not_all_at_once():
    """The chip delivers a whole run in about a second; the animation is six.
    Playing them as they arrive would be a collapse over in a blink.

    Mutation this catches: draining the buffer inside one tick.
    """
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    for step in range(6):
        app._on_event(_egg_frame(app, step, total=100, spread=1.0 + step))
    drawn = len(app.egg_viewer.point_frames)
    for expected in range(1, 7):
        app._tick_egg()
        assert len(app.egg_viewer.point_frames) == drawn + expected


def test_the_egg_stops_its_own_timer_after_the_last_frame_from_the_chip():
    """It is a 30-per-second source over an all-day booth: it has to end."""
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._on_event(_egg_frame(app, 5, total=6))
    app._on_event(_egg_frame(app, 6, total=6))
    assert app._tick_egg() is True      # the fifth frame; one still buffered
    assert app._tick_egg() is False     # the last frame, and nothing behind it
    assert app._egg_source_id is None


def test_a_chip_that_pauses_mid_run_is_waited_for_not_given_up_on():
    """THE BUG THE FIRST LIVE RUN FOUND, in one test.

    A worker's first egg compiles ttnn kernels -- measured at 10.2 s against
    0.94 s for every run after it -- so its frames arrive in bursts with long
    gaps. The timer used to end on "the buffer is empty", which retired it
    during the first such gap; the remaining 180 frames then arrived with
    nothing left to draw them, and the booth sat on a cloud of noise captioned
    "Computed on chip 0" until the visitor pressed a key.

    Mutation this catches: ending the run on an empty buffer rather than on
    the frame whose `step` equals its `total`.
    """
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._on_event(_egg_frame(app, 0, total=6))
    assert app._tick_egg() is True      # draws frame 0
    for _ in range(10):                 # ... and then a long silence
        assert app._tick_egg() is True, "the timer must survive the gap"
    app._on_event(_egg_frame(app, 6, total=6))
    drawn = len(app.egg_viewer.point_frames)
    assert app._tick_egg() is False
    assert len(app.egg_viewer.point_frames) == drawn + 1


def test_a_chip_that_goes_silent_for_good_stops_the_timer():
    """The other half of waiting: a daemon that dies mid-run sends no
    `egg_refused` either, and a 30 Hz source must not spin against it for the
    rest of the day. What is on screen -- and its caption -- stay, because
    both are still true of what a chip drew."""
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._on_event(_egg_frame(app, 3, total=180))
    assert app._tick_egg() is True
    assert app._egg_deadline is not None, "each frame renews the patience"
    app._egg_deadline = 0.0
    assert app._tick_egg() is False
    assert app.egg_source == "device", "it really did run on a chip"
    assert app._egg is None, "and must not restart as a CPU run half way"


def test_frames_that_arrive_after_the_fallback_has_started_are_dropped():
    """The cold-cache case again, from the other end: the booth gives up at
    six seconds and the chip's frames turn up at ten. Cutting to them would
    jump mid-animation AND change the caption from CPU to chip half way
    through a descent that started on the CPU.

    Mutation this catches: letting device frames win unconditionally.
    """
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._egg_deadline = 0.0
    app._tick_egg()                     # the fallback takes over
    assert app.egg_source == "cpu"
    steps = app._egg.completed
    app._on_event(_egg_frame(app, 0, total=180))
    app._tick_egg()
    assert app.egg_source == "cpu"
    assert app._egg.completed == steps + 1, "the CPU descent kept going"
    assert app._egg_provenance_label.text == app_module._EGG_FALLBACK_DEFAULT


def test_frames_from_a_dismissed_egg_are_never_drawn_into_the_next_one():
    """A visitor closes the card and opens it again while the first run's
    frames are still on the wire. Those frames belong to a run that is over.

    Mutation this catches: dropping the `egg_id` check in `_take_egg_frame`,
    which would splice the tail of one descent into the head of another.
    """
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    stale = app._egg_id
    app._handle_key("space")            # dismissed
    app._handle_key("g", ctrl=True)     # and asked again
    assert app._egg_id != stale

    drawn = len(app.egg_viewer.point_frames)
    app._on_event(_egg_frame(app, 3, egg_id=stale))
    assert app._tick_egg() is True
    assert len(app.egg_viewer.point_frames) == drawn
    assert app.egg_source == "asking"


def test_an_egg_frame_never_reaches_the_protein_viewer():
    """`egg_frame` is a separate wire type precisely so this is structural
    rather than conditional. A fold in flight must be untouched."""
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._on_event(_egg_frame(app, 0))
    app._tick_egg()
    app._drain_frames()
    assert app.viewer.point_frames == []
    assert app.viewer.clears == 0


# ── and when it did not ─────────────────────────────────────────────────────


def test_a_busy_booth_falls_back_to_the_cpu_and_says_so():
    """The fallback is fine. The fallback claiming a chip is not.

    Mutation this catches: leaving the provenance line alone on refusal --
    which shows "Computed on chip N" over a descent that ran on this laptop
    if a device frame had already arrived, and the asking line forever if not.
    """
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._handle_event({"type": "egg_refused", "egg_id": app._egg_id,
                       "reason": "busy"})
    assert app._tick_egg() is True
    assert app.egg_source == "cpu"
    assert app.egg_card is None
    assert app._egg is not None
    assert app._egg_provenance_label.text == (
        "Every chip is busy folding, so this one ran on the host CPU.")


def test_a_device_failure_gets_a_different_sentence_from_a_busy_booth():
    """"The booth is working" and "something went wrong" are different facts
    and a visitor standing at a booth can tell them apart.

    This is also the test that found a real bug: the daemon's refusal reason
    for a chip that FAILED is the string "device", and the provenance table's
    key for a chip that SUCCEEDED was the same string -- so a worker dying
    mid-egg put "Computed on chip N" over a descent that had just fallen back
    to the host. See `_EGG_FALLBACK`, which is a separate table now.
    """
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._handle_event({"type": "egg_refused", "egg_id": app._egg_id,
                       "reason": "device"})
    app._tick_egg()
    assert app._egg_provenance_label.text == (
        "No chip answered, so this one ran on the host CPU.")


def test_no_daemon_means_the_cpu_immediately_rather_than_after_a_wait():
    """Nothing is ever coming, so the visitor must not sit out a timeout to
    learn it."""
    app = _app()                        # no client at all
    app._handle_key("g", ctrl=True)
    assert app.egg_source == "cpu"
    assert app._egg is not None
    assert app.egg_viewer.point_frames, "the fallback draws its own first frame"
    assert app._egg_provenance_label.text == app_module._EGG_FALLBACK_DEFAULT


def test_a_daemon_that_refuses_the_message_is_the_same_as_no_daemon():
    """`send_egg` returns False for a protocol version this build will not
    speak to, among others. Same answer: run it here, say so."""
    app = _app(_FakeClient(accept=False))
    app._handle_key("g", ctrl=True)
    assert app.egg_source == "cpu"


def test_silence_from_the_daemon_falls_back_once_the_wait_runs_out():
    """The case nothing else covers: the daemon took the request and then
    died, so no refusal is ever coming either.

    Mutation this catches: removing the deadline, which leaves the booth
    showing an empty card with "Asking the booth for a chip…" until the
    visitor walks away.
    """
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    assert app._tick_egg() is True
    assert app.egg_source == "asking"
    app._egg_deadline = 0.0             # as if the wait had elapsed
    assert app._tick_egg() is True
    assert app.egg_source == "cpu"
    assert app._egg is not None


def test_a_refusal_arriving_after_the_fallback_started_does_not_restart_it():
    """Idempotence, and it matters: a restarted descent is a visible jump
    back to noise halfway through the animation."""
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._egg_deadline = 0.0
    app._tick_egg()
    for _ in range(20):
        app._tick_egg()
    part_way = app._egg.completed
    assert part_way > 0
    app._handle_event({"type": "egg_refused", "egg_id": app._egg_id,
                       "reason": "busy"})
    app._tick_egg()
    assert app._egg.completed > part_way


def test_a_refusal_for_a_different_egg_is_ignored():
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._handle_event({"type": "egg_refused", "egg_id": "somebody-elses",
                       "reason": "busy"})
    assert app._tick_egg() is True
    assert app.egg_source == "asking"


def test_the_fallback_runs_the_same_law_the_chip_would_have():
    """The CPU path is not a lesser animation: same descent, same length,
    same six seconds -- only the arithmetic moved."""
    app = _app()
    app._handle_key("g", ctrl=True)
    assert app._egg.steps == mark.STEPS
    while not app._egg.done:
        app._tick_egg()
    settled = app._egg.points() / app._egg.scale
    distance, _ = mark.slab_sdf_gradient(settled, mark.HALF_THICKNESS)
    assert (distance <= 1e-3).mean() > 0.98


# ── the copy ────────────────────────────────────────────────────────────────


def test_the_provenance_line_never_names_a_chip_unless_one_computed_it():
    """Every state of the label, in one place. The `device` sentence is the
    ONLY one that may mention a chip, and it may only appear when a card
    number came off the wire.

    Mutation this catches: a default that falls through to the device
    sentence -- e.g. `.get(state, _EGG_PROVENANCE["device"])`.
    """
    app = _app()
    for source, card, refusal in (("asking", None, None),
                                  ("cpu", None, "busy"),
                                  ("cpu", None, "device"),
                                  (None, None, None),
                                  ("nonsense", None, None),
                                  # a card number with no device frame drawn
                                  ("asking", 3, None),
                                  ("cpu", 3, "busy")):
        app.egg_source, app.egg_card, app._egg_refusal = source, card, refusal
        text = app._egg_provenance_text()
        assert "computed on chip" not in text.lower(), (
            f"{source}/{card}/{refusal}: {text}")
    app.egg_source, app.egg_card = "device", 0
    assert "chip 0" in app._egg_provenance_text()


def test_the_disclaimer_stopped_claiming_the_chips_were_uninvolved():
    """It used to read "nothing off the chips", which stopped being true.
    What it must keep saying is that this is not a structure -- that claim is
    about chemistry, not about hardware, and it did not change."""
    disclaimer = app_module._EGG_DISCLAIMER.lower()
    assert "not a folded structure" in disclaimer
    assert "no chemistry" in disclaimer
    assert "nothing off the chips" not in disclaimer
    assert "not a fold" in app_module._EGG_TITLE.lower()


def test_the_egg_is_still_a_chord_and_still_undocumented():
    """Nothing about moving onto the hardware makes this a feature. A plain
    key would take a letter away from the visitor surface, and a line on the
    `?` card would turn the booth's one hidden thing into a listed one."""
    listed = " ".join(keys for keys, _m in app_module._KEY_HELP).lower()
    assert "ctrl + g" not in listed
    app = _app(_FakeClient())
    app._handle_key("g")
    assert app.egg_visible is False


def test_closing_the_egg_forgets_everything_about_the_run():
    app = _app(_FakeClient())
    app._handle_key("g", ctrl=True)
    app._on_event(_egg_frame(app, 1))
    app._set_egg_visible(False)
    assert app._egg_id is None
    assert app.egg_source is None
    assert app.egg_card is None
    assert app._egg is None
    assert app._egg_preview is None
    assert not app._egg_frames
    assert app._egg_source_id is None


# ── the two timeouts, which live in two venvs ───────────────────────────────


def test_the_daemon_gives_up_before_the_booth_stops_waiting():
    """`EGG_WAIT_S` (runner/daemon.py) must be shorter than
    `_EGG_DEVICE_WAIT_MS` (ui/app.py), or the ordinary busy-booth case ends
    with the UI timing out on silence instead of with the daemon saying, in
    as many words, that it is busy -- and the visitor waits the longer of the
    two for a worse answer.

    Read out of the SOURCE rather than imported, and that is not laziness:
    these two constants live in modules that cannot be imported into one
    interpreter (the UI venv has no torch, the runner venv has no gi), so
    there is nowhere a normal test could stand to compare them. The
    duplicated protocol-freeze files next door exist for the same reason.
    """
    source = (Path(__file__).resolve().parents[2] / "runner" / "daemon.py"
              ).read_text()
    match = re.search(r"^EGG_WAIT_S = ([0-9.]+)$", source, re.MULTILINE)
    assert match, "runner/daemon.py no longer defines EGG_WAIT_S"
    daemon_wait = float(match.group(1))
    ui_wait = app_module._EGG_DEVICE_WAIT_MS / 1000.0
    assert daemon_wait < ui_wait, (
        f"the daemon waits {daemon_wait}s and the booth only {ui_wait}s")
