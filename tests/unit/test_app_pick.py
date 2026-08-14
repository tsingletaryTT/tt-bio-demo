"""Task 17: the pick, end to end.

Everything a visitor's pick needs already existed -- the message (Task 3),
both directions of the socket (Tasks 4, 5), the daemon's dispatch at the head
of its priority queue (Task 9), the focus rule and the pick status (Task 12).
This is the last hop: `ui/app.py`'s `_on_pick` sending it, and the booth
saying so.

The failure this file is written against, stated once: a visitor taps, four
chips are mid-fold, and for twenty seconds the screen says nothing. The
acknowledgement therefore does NOT wait on any daemon answering -- it happens
at tap time, off the router's own `pick_status`, and it changes if the wait
runs long. The ruling behind that wait is deliberate and is not up for
re-litigation here: a pick waits at the head of the queue and never pre-empts
a running fold, because tearing a fold down mid-device-operation is a
documented instability source and because pre-empting blanks a cell somebody
is watching.

Two things must never reach the glass, and several tests here exist only to
pin them: raw error text (a refused socket, an OSError's message, a path) and
protocol version numbers. Both go to the log and the diagnostics rail.
"""

import pytest

from _appfakes import _app, _done, _error, _frame, _start
from ui.slots import PICK_PENDING_WARN_S


class _RecordingClient:
    """Stands in for `ui.client.EventClient`, recording what was sent.

    `state` is here because ui/app.py reads it nowhere -- it reads its own
    remembered `_connection_state` -- and a fake carrying the field anyway is
    what makes `test_nothing_is_sent_to_a_daemon_the_ui_has_refused` a real
    test: the fake would happily accept the send, so only the app's own guard
    can stop it.
    """

    def __init__(self, ok=True):
        self.picks = []
        self.ok = ok
        self.state = "connected"

    def send_pick(self, target_id):
        self.picks.append(target_id)
        return self.ok


def _picking_app(cards=(0, 1, 2, 3), client=None):
    app = _app(cards)
    app._client = client if client is not None else _RecordingClient()
    return app


# ---------------------------------------------------------------------------
# The send.
# ---------------------------------------------------------------------------

def test_a_pick_asks_the_daemon_to_fold_it():
    """The decision this whole amendment exists for. Without this line the
    pick is a nomination again and the booth's copy becomes a lie."""
    app = _picking_app()
    app._on_pick("hemoglobin")
    assert app._client.picks == ["hemoglobin"]


def test_a_pick_with_no_daemon_at_all_does_not_raise():
    """DemoApp(socket_path=None) is how the whole UI suite runs, and how the
    booth comes up before the daemon does. _on_pick runs in a GLib callback:
    an AttributeError here freezes the source and the booth stops answering
    taps for the rest of the day."""
    app = _app()
    app._client = None
    app._on_pick("hemoglobin")


def test_a_failing_send_never_reaches_the_screen():
    class _Exploding:
        state = "connected"

        def send_pick(self, target_id):
            raise OSError("/run/tt-bio-demo/daemon.sock: connection refused")

    app = _picking_app(client=_Exploding())
    app._on_pick("hemoglobin")                      # must not raise
    assert all("/run/" not in (t or "") for t in app.quad.captions.values())
    assert "/run/" not in (app.quad.notice or "")


def test_a_send_that_fails_still_acknowledges_the_tap():
    """The socket being down is not the visitor's problem and is not
    something the booth can usefully tell them about. What it CAN do is stop
    behaving as though the tap did not happen.

    Mutation this catches: acknowledging only when `send_pick` returns True.
    """
    app = _picking_app(client=_RecordingClient(ok=False))
    app._on_pick("hemoglobin")
    assert app.quad.notice
    assert "HEMOGLOBIN" in app.quad.notice.upper()


def test_nothing_is_sent_to_a_daemon_the_ui_has_refused():
    app = _picking_app(client=_RecordingClient())
    app._client.state = "incompatible"
    app._on_state("incompatible")
    app._on_pick("hemoglobin")
    assert app._client.picks == []


def test_a_refused_daemon_shows_the_same_neutral_overlay_as_not_ready():
    """Task 3's ruling, landed here so it arrives with the copy. A UI that
    cannot interpret this daemon's protocol is, from a visitor's point of
    view, a booth that is not ready -- and it gets the same neutral words.
    The version numbers go to the log and the diagnostics rail, never to the
    glass.

    Mutation this catches: leaving `incompatible` with no overlay at all (a
    booth that looks like it is working and never folds anything), or
    interpolating the two version numbers into the message.
    """
    app = _picking_app()
    app._on_state("incompatible")
    assert app.display_message
    lowered = app.display_message.lower()
    for forbidden in ("version", "protocol", "v1", "v2", "v3", "/"):
        assert forbidden not in lowered, (
            f"{forbidden!r} reached the booth's own screen")


# ---------------------------------------------------------------------------
# The acknowledgement.
# ---------------------------------------------------------------------------

def test_the_booth_acknowledges_the_pick_at_tap_time():
    """The failure this designs against: a visitor taps, four chips are
    busy, and for twenty seconds the screen says nothing. The
    acknowledgement must not wait on any daemon answering."""
    app = _picking_app()
    app._on_pick("hemoglobin")
    assert app.quad.notice
    assert "HEMOGLOBIN" in app.quad.notice.upper()


def test_the_acknowledgement_says_more_when_the_wait_runs_long():
    app = _picking_app()
    app._tick_state_at(0.0)
    app._on_pick("hemoglobin")
    early = app.quad.notice
    assert early, "there was nothing to say more THAN"
    app._tick_state_at(PICK_PENDING_WARN_S + 1.0)
    assert app.quad.notice != early
    assert app.quad.notice


def test_the_longer_wait_explains_itself_as_the_feature_it_is():
    """"The folds already running finish" is a nicer fact than it sounds --
    it is why the other three cells keep moving -- and stating it is what
    makes a few seconds of waiting read as deliberate rather than broken.

    Mutation this catches: a second notice that is merely louder rather than
    more informative.
    """
    app = _picking_app()
    app._tick_state_at(0.0)
    app._on_pick("hemoglobin")
    app._tick_state_at(PICK_PENDING_WARN_S + 1.0)
    lowered = app.quad.notice.lower()
    assert "finish" in lowered or "interrupt" in lowered


def test_the_acknowledgement_does_not_promise_an_instant_fold():
    """With four chips busy the pick starts when one frees. "Instantly" is a
    claim the booth breaks in front of the one visitor watching for it."""
    app = _picking_app()
    app._tick_state_at(0.0)
    app._on_pick("hemoglobin")
    for now in (0.0, PICK_PENDING_WARN_S + 1.0):
        app._tick_state_at(now)
        lowered = (app.quad.notice or "").lower()
        for forbidden in ("instantly", "straight away", "right now",
                          "immediately"):
            assert forbidden not in lowered


def test_the_acknowledgement_never_becomes_an_error():
    app = _picking_app()
    app._on_pick("hemoglobin")
    for now in (0.0, PICK_PENDING_WARN_S + 1.0, 44.0):
        app._tick_state_at(now)
        text = (app.quad.notice or "").lower()
        assert "error" not in text and "fail" not in text and "/" not in text


def test_the_notice_clears_when_the_picked_fold_starts():
    """It has become the thing on screen. A banner still saying NEXT UP over
    the fold it was announcing is the booth talking over itself."""
    app = _picking_app()
    app._on_pick("hemoglobin")
    app._handle_event(_start("j3", card=3, target_id="hemoglobin"))
    assert not app.quad.notice


def test_the_notice_is_not_raised_for_a_pick_nobody_made():
    """The quiet half. `pick_status` is None with no pick, and a booth that
    put a banner up anyway would cover the quad all day."""
    app = _picking_app()
    for now in (0.0, 5.0, PICK_PENDING_WARN_S + 1.0):
        app._tick_state_at(now)
        assert not app.quad.notice


# ---------------------------------------------------------------------------
# The focus.
# ---------------------------------------------------------------------------

def test_the_focus_moves_to_the_cell_that_folds_the_pick():
    """Spec: 'a visitor's pick becomes the hero of the quad while the other
    three chips continue the attract playlist.'"""
    app = _picking_app()
    app._on_pick("hemoglobin")
    app._handle_event(_start("j0", card=0, target_id="attract-a"))
    app._handle_event(_start("j3", card=3, target_id="hemoglobin"))
    assert app.quad.focus == 3


def test_a_pick_for_a_target_already_folding_takes_the_focus_at_once():
    """The daemon queues nothing in this case (Task 9), so if the UI waited
    for a job_start that will never come, the visitor's pick would silently
    do nothing at all."""
    app = _picking_app()
    app._handle_event(_start("j2", card=2, target_id="hemoglobin"))
    app._on_pick("hemoglobin")
    assert app.quad.focus == 2


def test_a_pick_for_a_target_already_folding_says_so_rather_than_next_up():
    """The other half of the same behaviour. `pick_status` resolves straight
    to "folding" here, so there is nothing to announce -- and announcing
    NEXT UP about the protein already on screen is the booth talking over
    itself in its loudest possible voice."""
    app = _picking_app()
    app._handle_event(_start("j2", card=2, target_id="hemoglobin"))
    app._on_pick("hemoglobin")
    assert not app.quad.notice


def test_the_focus_does_not_move_at_pick_time_for_a_target_nobody_folds():
    """Task 12's rule, restated from the other side: the hero cell must not
    be pointed at a protein no cell is folding. Moving it at tap time would
    make the hero the wrong fold for however long the wait lasts.

    Cell 2 is deliberately put in showcase first so the focus is a
    NON-DEFAULT slot: with the focus sitting at 0 anyway, a mutation that
    slammed it to 0 on every pick would pass this test while breaking the
    booth.
    """
    app = _picking_app()
    app._handle_event(_start("j2", card=2, target_id="attract-a"))
    app._handle_event(_done("j2"))
    app._tick_state_at(0.0)
    assert app.quad.focus == 2, "the fixture no longer sets up a moved focus"
    app._on_pick("hemoglobin")
    assert app.quad.focus == 2


# ---------------------------------------------------------------------------
# The other three cells keep going.
# ---------------------------------------------------------------------------

def test_the_other_three_cells_keep_folding_the_attract_playlist():
    """Spec: 'the other three chips continue the attract playlist.' A pick
    must not stop, clear or freeze any other cell."""
    app = _picking_app()
    for card in range(4):
        app._handle_event(_start(f"j{card}", card=card))
    before = [v.cleared for v in app.quad.viewers]
    app._on_pick("hemoglobin")
    assert [v.cleared for v in app.quad.viewers] == before


def test_a_pick_does_not_disturb_a_frame_stream_in_flight():
    app = _picking_app()
    app._handle_event(_start("j1", card=1))
    app._on_pick("hemoglobin")
    app._on_event(_frame("j1"))
    app._drain_frames()
    assert app.quad.viewers[1].points == 1


def test_a_pick_still_reaches_the_booth_state_machine():
    """Unchanged: the pick closes the gallery. Regressing this makes the
    booth stop responding to a tap."""
    app = _picking_app()
    app._on_touch()
    app._on_pick("hemoglobin")
    assert app.display_state == "folding"


# ---------------------------------------------------------------------------
# The pick expires with the visitor.
# ---------------------------------------------------------------------------

def test_a_pick_the_daemon_never_folds_expires():
    """A visitor who picks and walks away must not pin the focus, or the
    notice, for the rest of the day."""
    app = _picking_app()
    app._on_pick("hemoglobin")
    app._tick_state_at(0.0)
    app._tick_state_at(9999.0)
    assert app.router.selected_target is None
    assert not app.quad.notice


def test_a_pick_does_not_expire_while_the_visitor_is_still_waiting():
    """The other half: the expiry number is the booth's own idle timeout, and
    a pick that expired in the ten seconds it legitimately takes four busy
    chips to free one would cancel itself in front of the person who made
    it.

    Mutation this catches: an expiry measured in a couple of seconds, or
    against the raw clock reading rather than against the pick.
    """
    app = _picking_app()
    app._tick_state_at(0.0)
    app._on_pick("hemoglobin")
    app._tick_state_at(PICK_PENDING_WARN_S + 1.0)
    assert app.router.selected_target == "hemoglobin"
    assert app.quad.notice


def test_the_expiry_is_the_booths_own_idle_number_and_not_a_second_one():
    """One number for "the visitor has gone". A second timeout here would be
    a second thing to get wrong, and the two would drift."""
    app = _picking_app()
    app._tick_state_at(0.0)
    app._on_pick("hemoglobin")
    app._tick_state_at(app.states.idle_timeout_s - 1.0)
    assert app.router.selected_target == "hemoglobin"
    app._tick_state_at(app.states.idle_timeout_s + 1.0)
    assert app.router.selected_target is None


def test_a_pick_that_is_being_folded_does_not_expire_underneath_itself():
    """Expiry is for a pick nothing ever picked up. A pick that IS folding is
    released by ui/slots.py when its cell stops (Task 12), and expiring it
    early would drop the focus off the hero mid-fold."""
    app = _picking_app()
    app._tick_state_at(0.0)
    app._on_pick("hemoglobin")
    app._handle_event(_start("j2", card=2, target_id="hemoglobin"))
    app._tick_state_at(9999.0)
    assert app.router.selected_target == "hemoglobin"
    assert app.quad.focus == 2
