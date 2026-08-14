"""Pins `_handle_event`'s logging behavior for event types it does not
render, so a future protocol addition or a missing-field runner is visible
in the logs instead of silently dropped -- the bug fix1 exists to close.

Constructing a real `DemoApp` needs no live display: `Gtk.Application.__init__`
itself doesn't touch GTK's display/GL machinery (only `do_activate`'s window
does, and that's never called here). `_handle_event` only reaches `self.viewer`
on the success path of `job_done` with a `cif_path`, which none of these
cases exercise, so `app.viewer` is left `None` -- a real attribute access
would raise loudly if a future change accidentally started touching it.
"""
import logging

from ui.app import DemoApp


def _app():
    app = DemoApp()
    app.viewer = None
    return app


def test_job_error_is_logged_with_message_detail(caplog):
    """The failure must be diagnosable from the logs (spec: 'logged with
    detail'), and this path must never touch self.viewer -- there is
    nothing here that could put `message` on screen."""
    with caplog.at_level(logging.ERROR, logger="ui.app"):
        _app()._handle_event({
            "type": "job_error", "job_id": "j1", "target_id": "t1",
            "message": "card 2 quarantined mid-fold",
        })
    assert any(
        "j1" in r.message and "card 2 quarantined mid-fold" in r.message
        for r in caplog.records
    )


def test_unhandled_event_type_logs_a_warning(caplog):
    """A future protocol addition must be visible in the logs rather than
    silently dropped like job_error was before this fix.

    `card_state` used to be the example here, and is no longer one: the
    booth reads the chip number off it to size the quad (ui/app.py's
    `_note_card`), so it has a branch of its own. Any type this build has
    never heard of does just as well, and does not go stale the day another
    event grows a handler.
    """
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        _app()._handle_event({"type": "a_future_protocol_addition"})
    assert any("a_future_protocol_addition" in r.message
               for r in caplog.records)


def test_a_card_state_is_not_logged_as_unhandled(caplog):
    """It is handled, and an all-day booth emitting one per fold per chip
    would otherwise write thousands of warnings about an event it acts on."""
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        _app()._handle_event({"type": "card_state", "card": 0, "state": "idle"})
    assert not [r for r in caplog.records if "unhandled" in r.message]


def test_job_done_without_cif_path_logs_a_warning(caplog):
    """A misconfigured runner sending job_done with no cif_path used to
    no-op silently (`if cif_path:` with no else) -- must now be
    diagnosable from the logs."""
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        _app()._handle_event({"type": "job_done", "job_id": "j1", "wall_s": 1.0})
    assert any(
        "j1" in r.message and "cif_path" in r.message for r in caplog.records
    )


def test_malformed_event_is_still_dropped_not_raised(caplog):
    """The broad except Exception guard around the whole branch must stay
    intact: a non-numeric `frac` (or any other wire-shaped surprise) must
    be logged and swallowed, never propagated out of a GLib callback."""
    with caplog.at_level(logging.ERROR, logger="ui.app"):
        _app()._handle_event({"type": "stage", "stage": "trunk", "frac": "oops"})
    assert any("stage" in r.message for r in caplog.records)
