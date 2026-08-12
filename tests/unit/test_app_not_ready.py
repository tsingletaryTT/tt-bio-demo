import pytest

from ui.app import DemoApp


def _app():
    app = DemoApp(socket_path=None)
    app.viewer = None                      # no GTK widget needed for these
    return app


def test_not_ready_puts_the_app_into_preparing():
    app = _app()
    app._handle_event({"type": "not_ready", "missing": ["model weights: /w/x.pt"]})
    assert app.display_state == "preparing"


def test_not_ready_records_what_is_missing_for_the_log():
    app = _app()
    app._handle_event({"type": "not_ready",
                       "missing": ["model weights: /w/x.pt", "playlist: none"]})
    assert app.missing == ["model weights: /w/x.pt", "playlist: none"]


def test_a_job_start_clears_preparing():
    """The daemon recovered; the booth must stop saying it is preparing."""
    app = _app()
    app._handle_event({"type": "not_ready", "missing": ["x"]})
    app._handle_event({"type": "job_start", "job_id": "j1", "target_id": "t",
                       "model": "protenix-v2", "card": 0, "n_residues": 20})
    assert app.display_state != "preparing"


def test_a_malformed_not_ready_does_not_raise():
    app = _app()
    app._handle_event({"type": "not_ready"})          # no 'missing' key
    assert app.display_state == "preparing"


def test_not_ready_message_is_neutral_and_carries_no_paths():
    """Global constraint: the display never shows raw error text."""
    app = _app()
    app._handle_event({"type": "not_ready", "missing": ["model weights: /w/secret.pt"]})
    assert "/w/secret.pt" not in app.display_message
    assert app.display_message.strip() != ""
