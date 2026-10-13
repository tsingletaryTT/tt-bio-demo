"""The booth names its model in several places that run in different venvs.

This pins them to one answer, and to an answer tt-bio's registry can actually
download. It lives under tests/unit/runner/ because it imports runner.*; the
UI's `ui.playlist` is plain Python and imports fine in that venv.
"""
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


def _explains_retirement(path):
    """True when every line naming the retired model says it is not shipped."""
    return all("not shipped" in line or "retired" in line
               for line in path.read_text(errors="ignore").splitlines()
               if _RETIRED in line)


def test_no_shipped_code_still_names_the_retired_model():
    offenders = []
    for sub in _SCANNED:
        for p in (ROOT / sub).rglob("*"):
            if (p.is_file() and p.suffix in _SUFFIXES
                    and _RETIRED in p.read_text(errors="ignore")
                    and not _explains_retirement(p)):
                offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, offenders
