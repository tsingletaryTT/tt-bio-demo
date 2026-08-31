"""One place decides where the model weights live.

There used to be four, and they disagreed:

    runner/folder.py                     Path.home() / ".boltz"
    scripts/run-demo.sh                  ${TT_BIO_DEMO_WEIGHTS:-$HOME/.boltz}
    scripts/doctor.sh                    ${BOLTZ_CACHE:-$HOME/.boltz}
    debian/...weights.postinst           ${BOLTZ_CACHE:-$HOME/.boltz}

None of them read $TT_BIO_CACHE, which is the variable tt-bio itself prefers
and documents as relocating the whole cache. They all resolve to ~/.boltz when
nothing is set, which is exactly why this survived so long: the disagreement
only appears once an operator moves the cache, and then it appears as the
doctor pronouncing a booth healthy while a fold loads from an empty directory,
or the postinst filling a directory nothing reads.

This test is the guard against a fifth one appearing. It is textual on
purpose: the failure it prevents is somebody writing `~/.boltz` in a new file,
which no runtime test can see until that file runs on a machine with the
variable set.
"""
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# The files ALLOWED to spell the default out, because they are the resolver --
# one per language. Each is pinned to tt-bio's own cache_root() by a test:
# runner/env.py by tests/unit/runner/test_runner_env.py, the two shell copies
# by tests/unit/test_doctor.py and tests/unit/test_setup_venvs_weights.py.
_RESOLVERS = {
    "runner/env.py",             # python
    "scripts/weights-cache.sh",  # shell
}

# Documentation, changelogs and this project's own log are describing the
# path, not deriving it. tests/ may write fixtures wherever they like.
_PROSE_SUFFIXES = {".md", ".yaml", ".yml", ".txt", ".rst"}


def _source_files():
    """TRACKED files only, via git.

    An rglob walk also finds debhelper's staging trees (debian/tt-bio-demo/,
    debian/tt-bio-demo-weights/DEBIAN/), which are build OUTPUT -- copies of
    the very files this test checks. Flagging those reports one real problem
    as three and, worse, would keep reporting it after the source was fixed
    until somebody ran a clean build.
    """
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"],
                         capture_output=True, text=True, check=True)
    for rel in out.stdout.split("\0"):
        if not rel:
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        if rel.startswith(("tests/", "docs/", "recordings/", "dist/")):
            continue
        if path.suffix in _PROSE_SUFFIXES:
            continue
        if path.suffix not in (".py", ".sh", "") and "postinst" not in rel:
            continue
        yield rel, path


def test_nothing_outside_the_resolvers_hardcodes_the_default_cache():
    """`~/.boltz` / `.boltz` as a literal default belongs to the resolvers."""
    offenders = []
    for rel, path in _source_files():
        if rel in _RESOLVERS:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue          # a comment describing it is fine
            if re.search(r'["\']?~?/?\.boltz', line):
                offenders.append(f"{rel}:{n}: {stripped}")
    assert not offenders, (
        "these derive the weights cache themselves instead of asking the one "
        "resolver (runner.env.weights_cache, or the shell twin):\n  "
        + "\n  ".join(offenders))


def test_nothing_outside_the_resolvers_reads_the_cache_variables():
    """$TT_BIO_CACHE / $BOLTZ_CACHE likewise. Reading one of them directly is
    how a caller ends up honouring one variable and not the other -- the exact
    shape of the original bug, where the doctor read BOLTZ_CACHE and tt-bio
    preferred TT_BIO_CACHE."""
    offenders = []
    for rel, path in _source_files():
        if rel in _RESOLVERS:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "TT_BIO_CACHE" in line or "BOLTZ_CACHE" in line:
                offenders.append(f"{rel}:{n}: {stripped}")
    assert not offenders, (
        "these read the cache environment variables directly; ask the "
        "resolver so both variables are always honoured together:\n  "
        + "\n  ".join(offenders))


def test_the_resolver_files_still_exist_so_this_test_cannot_pass_vacuously():
    """If the resolvers were renamed away, both tests above would go green by
    finding nothing to check. Pin their existence."""
    for rel in _RESOLVERS:
        assert (REPO / rel).is_file(), f"{rel} is gone; the tests above are now vacuous"
