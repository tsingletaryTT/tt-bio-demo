"""The one model the booth folds with.

Folder, the daemon's greeting, the job default and preflight all import this
name, so they cannot disagree. The UI runs in another venv and cannot import
it; tests/unit/runner/test_booth_model_agrees.py pins ui.playlist.DEFAULT_MODEL
to this value.
"""

BOOTH_MODEL = "protenix-v1"
