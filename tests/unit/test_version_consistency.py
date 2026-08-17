"""One version, four places to write it down.

    VERSION                          the repo's own answer
    debian/changelog (top stanza)    what a .deb will call itself
    docs/tt-bio-demo-onepager.pdf    what the printed handout claims
    the git tag                      what a release is named

The first three are checked here, so they fail on a developer's machine and
not only in CI. The fourth is checked in .github/workflows/packages.yml,
because only CI has a tag.

The PDF is the non-obvious one and the reason this module exists.
docs/onepager/build.sh stamps VERSION into the sheet at render time, so
bumping VERSION without re-running that script ships a handout claiming the
previous version -- on the one artefact most likely to be printed and handed
to a stranger, where nobody would think to check.

Needs no venv, no device and no package build -- which is why it is here
rather than in test_packaging.py, where every test waits on a module-scoped
dpkg-buildpackage that three string comparisons do not need.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
VERSION_FILE = REPO / "VERSION"
CHANGELOG = REPO / "debian" / "changelog"
HANDOUT = REPO / "docs" / "tt-bio-demo-onepager.pdf"

# `tt-bio-demo (0.1.0) noble; urgency=medium`
_CHANGELOG_TOP = re.compile(r"^tt-bio-demo \(([^)]+)\)")


def _version_file():
    return VERSION_FILE.read_text().strip()


def _changelog_version():
    first = CHANGELOG.read_text().splitlines()[0]
    m = _CHANGELOG_TOP.match(first)
    assert m, f"could not parse the top changelog stanza: {first!r}"
    return m.group(1)


def test_the_changelog_agrees_with_the_version_file():
    assert _changelog_version() == _version_file(), (
        f"debian/changelog says {_changelog_version()!r} but VERSION says "
        f"{_version_file()!r}. Both are hand-written; pick the right one and "
        "make the other match."
    )


def test_the_handout_pdf_was_rebuilt_for_this_version():
    version = _version_file()
    assert HANDOUT.is_file(), f"{HANDOUT} is missing"
    if shutil.which("pdftotext") is None:
        pytest.fail(
            "pdftotext is not installed, so the handout's version could not be "
            "checked. Install poppler-utils. This is a failure rather than a "
            "skip on purpose: a silently skipped check here is indistinguishable "
            "from a passing one."
        )
    text = subprocess.run(
        ["pdftotext", str(HANDOUT), "-"],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    assert f"v{version}" in text, (
        f"the handout does not mention v{version}. VERSION was probably bumped "
        "without re-rendering it -- run docs/onepager/build.sh and commit the "
        "result."
    )


def test_the_version_is_a_plain_three_part_number():
    """Guards the tag comparison in CI, which strips exactly one leading 'v'."""
    version = _version_file()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"VERSION is {version!r}; the release workflow compares it against a "
        "tag with one leading 'v' stripped, so anything else needs that "
        "comparison revisited first."
    )
