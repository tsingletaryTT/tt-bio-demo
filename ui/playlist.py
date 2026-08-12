"""The visitor-facing playlist: which fold targets exist, and where.

`playlist/manifest.yaml` is a YAML list of targets. Each entry names an
`input` fold spec (a tt-bio job YAML, e.g. `examples/trpcage_no_msa.yaml`),
plus the copy the gallery card shows a visitor (`name`, `blurb`), an
`expected_s` fold time for pacing, and optionally `model` (defaults to
`protenix-v2`, this project's default model everywhere else) and
`thumbnail` (Phase 4 art; absent entries render a placeholder -- see
`ui/gallery.py`).

This module is UI-side: it must never import torch or tt_bio (see
docs/venv-bootstrap-notes.md -- ui/ and runner/ are different venvs, and
tests/unit/'s split by directory, not marker, depends on every module here
staying importable under venv-ui alone). Parsing a YAML file with PyYAML
is the only external dependency, and PyYAML is already present in venv-ui
(`--system-site-packages` off apt's python3-yaml).

`input_path` resolution -- the load-bearing decision in this module: paths
in the manifest are resolved relative to the MANIFEST FILE'S OWN DIRECTORY,
never the process's current working directory and never a hardcoded
"repo root" guess. This project's daemon and UI are launched from
different working directories (scripts/run-demo.sh cd's nowhere in
particular before either process starts; a future systemd --user unit for
the daemon has its own WorkingDirectory, likely `/`), so any resolution
rule that reads the CWD would work by accident in a dev shell and break
silently at the venue. Resolving off the manifest's own path needs nothing
from the environment: wherever `playlist/manifest.yaml` ends up installed
(this repo's checkout today, /opt/tt-bio-demo/playlist/ once packaged),
its own directory is always known the instant its path is, and entries can
address sibling files with an ordinary relative path (e.g.
`../examples/trpcage_no_msa.yaml` from `playlist/manifest.yaml`) that keeps
working across both. This is the same lesson `examples/trpcage_no_msa.yaml`
and `tests/integration/test_real_fold.py` already paid for with a stale
absolute path into a sibling tt-boltz checkout -- see those files' own
comments -- generalized to every path this module hands out.

Error messages are for logs and operators only. Per CLAUDE.md and the
spec's §6, nothing in the UI ever shows a stack trace or raw exception
text to a visitor; a `PlaylistError` always names the offending entry (its
`id`, or its 1-based position in the file if `id` itself is what's
missing) so an operator staring at fifteen targets and one bad line does
not have to bisect the file by hand.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

# Matches runner/preflight.py's REQUIRED_WEIGHTS and this project's other
# default-model references; kept as a module-level constant rather than a
# literal so both stay in sync if it's ever repointed to protenix-v3.
DEFAULT_MODEL = "protenix-v2"

# Every field an entry must supply explicitly -- no sane cross-target
# default exists for any of these (a made-up name, blurb, or fold time
# would be worse than a loud failure at load time). Checked in this order
# so a manifest entry missing more than one field always reports the same
# field first, deterministically, rather than whichever dict-iteration
# order happens to land. "model" and "thumbnail" are NOT here: both have
# well-defined optional behavior (DEFAULT_MODEL / no thumbnail) so their
# absence is not an error at all -- see load_playlist.
_REQUIRED_FIELDS = ("input", "name", "blurb", "expected_s")


class PlaylistError(Exception):
    """The playlist manifest is missing, malformed, or internally inconsistent.

    Always names the offending entry (by `id`, or by position if `id`
    itself is absent) and, for a missing file, always contains the literal
    substring "not found" -- both are relied on by tests/unit/test_playlist.py
    and are part of this exception's contract, not incidental phrasing.
    """


@dataclass(frozen=True)
class Target(object):
    """One fold target a visitor can pick from the gallery.

    Frozen: a Target is loaded once at startup and handed around read-only
    (ui/gallery.py's cards, ui/app.py's dispatch to the daemon) -- nothing
    downstream should ever be mutating one in place.
    """

    id: str
    input_path: Path
    model: str
    name: str
    blurb: str
    expected_s: float
    thumbnail: Path | None = None


def _entry_label(entry, index):
    """A human-identifiable name for a manifest entry in an error message.

    Prefers the entry's own `id` (what an operator will actually recognize
    against the manifest file), falling back to its 1-based position for
    the one case an id can't be used: the id field itself is what's
    missing or the entry isn't even a mapping.
    """
    if isinstance(entry, dict):
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id.strip():
            return entry_id
    return f"entry #{index + 1}"


def load_playlist(path):
    """Load and validate a playlist manifest, returning a list of Target.

    `path` may be a `str` or `pathlib.Path`; the manifest itself must be a
    YAML list of mappings. Raises `PlaylistError` -- never yaml.YAMLError,
    KeyError, TypeError, or anything else raw -- for every failure mode:
    a missing file, a file that isn't a YAML list, an entry missing a
    required field, an entry with a non-numeric `expected_s`, or two
    entries sharing one `id`.
    """
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise PlaylistError(f"playlist manifest not found: {manifest_path}")

    try:
        raw_text = manifest_path.read_text()
    except OSError as exc:
        raise PlaylistError(f"playlist manifest {manifest_path} could not be read: {exc}") from exc

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise PlaylistError(f"playlist manifest {manifest_path} is not valid YAML: {exc}") from exc

    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise PlaylistError(
            f"playlist manifest {manifest_path} must be a YAML list of "
            f"targets, got {type(raw).__name__}"
        )

    # Every relative path in the manifest resolves against THIS directory --
    # see the module docstring for why (never the process CWD, never a
    # guessed repo root).
    manifest_dir = manifest_path.resolve().parent

    targets = []
    seen_ids = {}
    for index, entry in enumerate(raw):
        label = _entry_label(entry, index)

        if not isinstance(entry, dict):
            raise PlaylistError(
                f"{label}: manifest entries must be mappings, got {type(entry).__name__}"
            )

        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise PlaylistError(f"{label}: missing required field 'id'")

        for field in _REQUIRED_FIELDS:
            value = entry.get(field)
            # Reject missing, None, and "" alike -- a blurb of "" would
            # silently ship a blank gallery card, exactly the failure mode
            # this validation exists to catch before it reaches a visitor.
            if value is None or (isinstance(value, str) and not value.strip()):
                raise PlaylistError(f"{entry_id}: missing required field '{field}'")

        if entry_id in seen_ids:
            raise PlaylistError(
                f"duplicate target id '{entry_id}' in {manifest_path} "
                f"(entries #{seen_ids[entry_id] + 1} and #{index + 1}) -- "
                "which one a visitor picked would be ambiguous"
            )
        seen_ids[entry_id] = index

        try:
            expected_s = float(entry["expected_s"])
        except (TypeError, ValueError) as exc:
            raise PlaylistError(
                f"{entry_id}: 'expected_s' must be a number, got {entry['expected_s']!r}"
            ) from exc

        thumbnail = entry.get("thumbnail")
        thumbnail_path = (manifest_dir / thumbnail).resolve() if thumbnail else None

        targets.append(Target(
            id=entry_id,
            input_path=(manifest_dir / entry["input"]).resolve(),
            model=entry.get("model") or DEFAULT_MODEL,
            name=entry["name"],
            blurb=entry["blurb"],
            expected_s=expected_s,
            thumbnail=thumbnail_path,
        ))

    return targets
