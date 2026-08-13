"""The visitor-facing playlist: which fold targets exist, and where.

`playlist/manifest.yaml` is a YAML list of targets. Each entry names an
`input` fold spec (a tt-bio job YAML, e.g. `examples/trpcage_no_msa.yaml`),
plus the copy the gallery card shows a visitor (`name`, `blurb`), and
optionally `model` (defaults to `protenix-v2`, this project's default model
everywhere else), `thumbnail` (a picture of a real fold of this target,
built by `scripts/make-thumbnails.py`; absent entries render a placeholder
-- see `ui/gallery.py`), `tagline` (one short sentence for the caption under
the live render -- see `Target.tagline`), and `expected_s` (a fold time for
pacing, measured once on this booth's own hardware -- see below).

`expected_s` is OPTIONAL, on purpose. It exists to record a real,
hardware-measured fold time for pacing (Trp-cage's `4.4` is a 30-fold soak
average -- see playlist/manifest.yaml's own comment); nothing about this
module can produce that number for a target nobody has folded on this
booth's hardware yet, and inventing one would read to an operator as
measured when it is a guess. So a target is allowed to omit `expected_s`
(or set it explicitly to YAML `null`) to mean "not yet measured" --
`Target.expected_s` is `None` in that case, never a fabricated float. A
target that DOES supply `expected_s` still has it validated as a number,
exactly as before. `ui/gallery.py` is responsible for showing `None`
sensibly (never as a bogus time); see that module's `_format_fold_time`.

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

This module also has a tiny COMMAND-LINE face (`python3 -m ui.playlist
MANIFEST [id ...]`, see `main` at the bottom), which exists for exactly
one caller: `scripts/run-demo.sh`. The launcher has to hand the daemon a
directory of fold inputs and the UI a manifest, and those two used to be
chosen independently -- the gallery advertised four targets the daemon had
no input file for, so tapping "Trypsin - ~74.9s" got you a 20-residue
Trp-cage in four seconds. Now the launcher builds the daemon's directory
FROM this manifest, through this CLI, so the two cannot disagree: one
parser, one validation pass, one answer to "what can this booth fold".
"""

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# Matches runner/preflight.py's REQUIRED_WEIGHTS and this project's other
# default-model references; kept as a module-level constant rather than a
# literal so both stay in sync if it's ever repointed to protenix-v3.
DEFAULT_MODEL = "protenix-v2"

# Every field an entry must supply explicitly -- no sane cross-target
# default exists for any of these (a made-up name or blurb would be worse
# than a loud failure at load time). Checked in this order so a manifest
# entry missing more than one field always reports the same field first,
# deterministically, rather than whichever dict-iteration order happens to
# land. "model", "thumbnail", "tagline" and "expected_s" are NOT here: all
# four have well-defined optional behavior (DEFAULT_MODEL / no thumbnail /
# no caption line / None meaning "not yet measured") so their absence is not
# an error at all -- see load_playlist.
_REQUIRED_FIELDS = ("input", "name", "blurb")


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
    (ui/gallery.py's cards; scripts/run-demo.sh's expansion of this manifest
    into the daemon's fold inputs, via this module's CLI) -- nothing
    downstream should ever be mutating one in place.

    Note what is NOT among those readers: there is no dispatch from the UI
    to the daemon. This docstring used to name one. The socket protocol is
    one-way (runner/server.py broadcasts; ui/client.py never sends), so a
    visitor's pick reaches ui/app.py's state machine and stops there -- see
    ui/gallery.py's module docstring, which carries the same warning for the
    copy on that screen.
    """

    id: str
    input_path: Path
    model: str
    name: str
    blurb: str
    # None means "not yet measured on real hardware" -- see the module
    # docstring. Never a fabricated number; ui/gallery.py renders this case
    # explicitly rather than formatting None into something that looks like
    # a real time.
    expected_s: float | None = None
    thumbnail: Path | None = None
    # One short sentence about the molecule, for the caption under the live
    # render (ui/app.py's `_build_target_info`). Deliberately a SECOND field
    # rather than a reuse of `blurb`: `blurb` is gallery-card copy, several
    # sentences long, and is read by someone who has stopped and is choosing.
    # This one is read at two metres by someone walking past, so it has to be
    # one line at a size that carries -- and the two are written to different
    # lengths on purpose, not derived from each other.
    #
    # Optional, like `thumbnail`: a target added before anyone has written
    # one still loads, and the caption simply shows its name alone.
    tagline: str | None = None


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
    required field, an entry whose `expected_s` is PRESENT but not a
    number, or two entries sharing one `id`. `expected_s` itself may be
    absent or explicit YAML `null` -- both mean "not yet measured" and
    produce `Target.expected_s is None`, not an error; see the module
    docstring.
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

        # Absent key or explicit YAML `null` both mean "not yet measured on
        # real hardware" -- None, not a fabricated number (see the module
        # docstring and Target.expected_s's own comment). Only a PRESENT,
        # non-null value is validated as a number; this is why expected_s
        # is not in _REQUIRED_FIELDS above.
        raw_expected_s = entry.get("expected_s")
        if raw_expected_s is None:
            expected_s = None
        else:
            try:
                expected_s = float(raw_expected_s)
            except (TypeError, ValueError) as exc:
                raise PlaylistError(
                    f"{entry_id}: 'expected_s' must be a number (or absent/null "
                    f"for 'not yet measured'), got {raw_expected_s!r}"
                ) from exc

        thumbnail = entry.get("thumbnail")
        thumbnail_path = (manifest_dir / thumbnail).resolve() if thumbnail else None

        # Absent, null and "" all collapse to None -- the caption under the
        # render then shows the target's name alone rather than an empty
        # second line. Same rule `blurb` gets above, minus the hard failure:
        # a tagline is optional (see Target.tagline).
        raw_tagline = entry.get("tagline")
        tagline = raw_tagline.strip() if isinstance(raw_tagline, str) else None
        tagline = tagline or None

        targets.append(Target(
            id=entry_id,
            input_path=(manifest_dir / entry["input"]).resolve(),
            model=entry.get("model") or DEFAULT_MODEL,
            name=entry["name"],
            blurb=entry["blurb"],
            expected_s=expected_s,
            thumbnail=thumbnail_path,
            tagline=tagline,
        ))

    return targets


def select_targets(targets, ids):
    """The subset of `targets` named by `ids`, in the manifest's own order.

    `ids` may be `None` or empty, which means "all of them" -- the booth's
    ordinary case. An id that is not in the manifest is a `PlaylistError`,
    loudly, rather than a silently smaller playlist: this is what
    scripts/run-demo.sh uses to decide which targets BOTH processes get, so
    a typo in an operator's `--targets` must not quietly ship a booth that
    advertises one thing and folds another (see the module docstring).

    Manifest order, not the caller's: the gallery reads top to bottom off
    the file an operator edits, and `--targets trypsin,trpcage` reordering
    the grid would be a surprise nobody asked for.
    """
    if not ids:
        return list(targets)
    wanted = list(ids)
    known = {target.id for target in targets}
    unknown = [target_id for target_id in wanted if target_id not in known]
    if unknown:
        raise PlaylistError(
            f"no such target(s) in the playlist: {', '.join(unknown)} "
            f"(this manifest has: {', '.join(sorted(known))})")
    return [target for target in targets if target.id in set(wanted)]


def main(argv=None):
    """`python3 -m ui.playlist MANIFEST [id ...]` -> one `id<TAB>input_path`
    line per selected target.

    For scripts/run-demo.sh, which turns those lines into the symlink
    directory the daemon folds from -- see the module docstring. Tab
    separated (never spaces): a path may contain spaces, an id may not
    (nothing here forbids it, but the manifest is ours and does not).

    A bad manifest exits 2 with ONE line on stderr -- no traceback. The
    launcher prints that line to an operator, and this is the same rule the
    UI itself follows: a raw exception is never the user-facing artifact.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python3 -m ui.playlist MANIFEST [id ...]", file=sys.stderr)
        return 2
    try:
        targets = select_targets(load_playlist(argv[0]), argv[1:])
    except PlaylistError as exc:
        print(f"playlist: {exc}", file=sys.stderr)
        return 2
    for target in targets:
        print(f"{target.id}\t{target.input_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
