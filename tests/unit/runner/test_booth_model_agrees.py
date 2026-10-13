"""The booth names its model in several places that run in different venvs.

This pins them to one answer, and to an answer tt-bio's registry can actually
download. It lives under tests/unit/runner/ because it imports runner.*; the
UI's `ui.playlist` is plain Python and imports fine in that venv.
"""
import re
from pathlib import Path

from runner import preflight
from runner.queue import Job
from ui import playlist

ROOT = Path(__file__).resolve().parents[3]

# Only the directories this task owns. Provisioning (scripts/, debian/) joins
# the scan when its own model id moves.
_SCANNED = ("runner", "ui", "protocol", "playlist", "webview")
_SUFFIXES = {".py", ".sh", ".yaml", ".postinst", ".templates"}
_RETIRED = "protenix-v2"


def test_every_shipped_default_names_the_same_model():
    assert (preflight.MODEL == playlist.DEFAULT_MODEL
            == Job.__dataclass_fields__["model"].default)


def test_the_booth_model_is_one_tt_bio_can_download():
    from tt_bio import weights
    sources = {a.key: a.source for a in weights.artifacts_for(preflight.MODEL)}
    # Look the model up by key. mols comes first in the list; position means nothing.
    assert sources[preflight.MODEL] == "file", (
        f"{preflight.MODEL} is {sources[preflight.MODEL]!r} in tt-bio's registry: "
        "a fresh install cannot fetch it")


def _line_is_allowed(line):
    """A line naming the retired model passes only as an explanation of it.

    The explanation has to say "not shipped" or "retired". A line that sets a
    model (`model=` or `model:` followed by the name) never passes.
    """
    low = line.lower()
    if re.search(r"model\s*[=:]\s*[\"']?" + _RETIRED, low):
        return False
    return "not shipped" in low or "retired" in low


def _explains_retirement(path):
    return all(_line_is_allowed(line)
               for line in path.read_text(errors="ignore").splitlines()
               if _RETIRED in line.lower())


def test_no_shipped_code_still_names_the_retired_model():
    offenders = []
    for sub in _SCANNED:
        for p in (ROOT / sub).rglob("*"):
            if (p.is_file() and p.suffix in _SUFFIXES
                    and _RETIRED in p.read_text(errors="ignore").lower()
                    and not _explains_retirement(p)):
                offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, offenders


# --- the other places the booth model appears --------------------------------

def test_the_folder_default_is_the_booth_model():
    from runner.folder import Folder
    assert Folder().model == preflight.MODEL      # constructed, never loaded


def test_the_folder_guard_and_fetch_key_use_the_shared_constant():
    # load() needs a device, so check the source: no model literal may sit in
    # the guard or the fetch call, and both must name the shared constant.
    src = (ROOT / "runner" / "folder.py").read_text()
    assert "self.model != BOOTH_MODEL" in src
    assert "weights.fetch(BOOTH_MODEL," in src
    assert "protenix-v1" not in src, "a literal model id is back in runner/folder.py"


def test_the_load_guard_rejects_any_other_model():
    import pytest
    from runner.folder import Folder
    with pytest.raises(ValueError, match=preflight.MODEL):
        Folder(model="protenix-v2").load()


def test_the_daemon_greeting_advertises_the_booth_model():
    src = (ROOT / "runner" / "daemon.py").read_text()
    assert '"models": [BOOTH_MODEL]' in src


def test_the_job_default_is_the_shared_constant():
    from runner.model import BOOTH_MODEL
    assert Job.__dataclass_fields__["model"].default == BOOTH_MODEL == preflight.MODEL
