"""scripts/update-tensix-viz.sh: re-vendoring tensix-viz in one step.

Runs the real script against a synthetic project tree and a synthetic
upstream git checkout -- never against this repo's own ui/assets/tensix-viz
(that would vendor whatever happens to be checked out at ~/code/tensix-viz
onto a developer's machine mid test run, which is exactly the kind of
side effect a test must not have). `_project_tree()` builds a throwaway
copy of just the two paths the script actually touches (itself, and
ui/assets/tensix-viz/PROVENANCE.md), so `REPO_ROOT` -- which the script
derives from its own location, not a parameter -- resolves inside the
tmp tree instead of the real repo.
"""

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_REAL_PATH = REPO_ROOT / "scripts" / "update-tensix-viz.sh"

_PROVENANCE_TEMPLATE = textwrap.dedent("""\
    # Vendored: tensix-viz

    - **Project:** tensix-viz
    - **Upstream:** https://github.com/tsingletaryTT/tensix-viz
    - **Version:** 1.2.0
    - **Commit:** `deadbeefdeadbeefdeadbeefdeadbeefdeadbeef` (`some old commit`)
    - **Licence:** Apache-2.0
    - **Copied on:** 2020-01-01

    Everything below this line is untouched by the update script.
    """)


def _project_tree(tmp_path):
    """A throwaway copy of just the two paths update-tensix-viz.sh touches,
    so REPO_ROOT (derived from the script's own location) resolves here."""
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    (root / "ui" / "assets" / "tensix-viz").mkdir(parents=True)
    script = root / "scripts" / "update-tensix-viz.sh"
    script.write_text(SCRIPT_REAL_PATH.read_text())
    script.chmod(0o755)
    (root / "ui" / "assets" / "tensix-viz" / "PROVENANCE.md").write_text(
        _PROVENANCE_TEMPLATE)
    return root


def _fake_upstream(tmp_path, *, version="1.4.0", dirty=False):
    """A synthetic tensix-viz checkout: a real git repo (a real commit is
    the whole point of what this script attributes), a package.json naming
    `version`, and non-empty built JS/CSS."""
    upstream = tmp_path / "tensix-viz"
    upstream.mkdir()
    (upstream / "package.json").write_text(f'{{"name": "tensix-viz", "version": "{version}"}}\n')
    (upstream / "tensix-viz.js").write_text("/* fake built tensix-viz.js */\n")
    (upstream / "tensix-viz.css").write_text("/* fake built tensix-viz.css */\n")
    subprocess.run(["git", "init", "-q"], cwd=upstream, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=upstream, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=upstream, check=True)
    subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: a new mode"], cwd=upstream, check=True)
    if dirty:
        (upstream / "tensix-viz.js").write_text("/* uncommitted edit */\n")
    return upstream


def _run(root, upstream):
    return subprocess.run(
        [str(root / "scripts" / "update-tensix-viz.sh"), str(upstream)],
        capture_output=True, text=True)


def test_the_script_is_executable():
    assert SCRIPT_REAL_PATH.is_file()
    assert SCRIPT_REAL_PATH.stat().st_mode & 0o111, "update-tensix-viz.sh is not executable"


def test_a_clean_upstream_vendors_the_files_and_rewrites_provenance(tmp_path):
    root = _project_tree(tmp_path)
    upstream = _fake_upstream(tmp_path, version="1.4.0")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream, capture_output=True, text=True, check=True
    ).stdout.strip()

    result = _run(root, upstream)
    assert result.returncode == 0, result.stderr

    dest = root / "ui" / "assets" / "tensix-viz"
    assert (dest / "tensix-viz.js").read_text() == "/* fake built tensix-viz.js */\n"
    assert (dest / "tensix-viz.css").read_text() == "/* fake built tensix-viz.css */\n"

    provenance = (dest / "PROVENANCE.md").read_text()
    assert "- **Version:** 1.4.0" in provenance
    assert f"- **Commit:** `{commit}` (`feat: a new mode`)" in provenance
    assert re.search(r"- \*\*Copied on:\*\* \d{4}-\d{2}-\d{2}", provenance)
    # The old values are gone, not merely appended past.
    assert "1.2.0" not in provenance
    assert "deadbeef" not in provenance
    assert "2020-01-01" not in provenance
    # Everything else in the file survives untouched.
    assert "Everything below this line is untouched by the update script." in provenance


def test_a_dirty_upstream_checkout_is_refused(tmp_path):
    root = _project_tree(tmp_path)
    upstream = _fake_upstream(tmp_path, dirty=True)

    result = _run(root, upstream)
    assert result.returncode != 0
    assert "uncommitted changes" in result.stderr
    # Nothing was vendored.
    dest = root / "ui" / "assets" / "tensix-viz"
    assert not (dest / "tensix-viz.js").exists()


def test_a_non_git_upstream_is_refused(tmp_path):
    root = _project_tree(tmp_path)
    upstream = tmp_path / "not-a-checkout"
    upstream.mkdir()
    (upstream / "package.json").write_text('{"version": "1.0.0"}\n')
    (upstream / "tensix-viz.js").write_text("x")
    (upstream / "tensix-viz.css").write_text("x")

    result = _run(root, upstream)
    assert result.returncode != 0
    assert "not a git checkout" in result.stderr


def test_an_unbuilt_upstream_is_refused(tmp_path):
    root = _project_tree(tmp_path)
    upstream = tmp_path / "unbuilt"
    upstream.mkdir()
    (upstream / "package.json").write_text('{"version": "1.0.0"}\n')
    subprocess.run(["git", "init", "-q"], cwd=upstream, check=True)

    result = _run(root, upstream)
    assert result.returncode != 0
    assert "no built tensix-viz.js" in result.stderr


def test_a_missing_upstream_directory_is_refused(tmp_path):
    root = _project_tree(tmp_path)
    result = _run(root, tmp_path / "does-not-exist")
    assert result.returncode != 0
    assert "no such directory" in result.stderr


def test_a_reworded_provenance_fails_loudly_rather_than_silently_skipping(tmp_path):
    root = _project_tree(tmp_path)
    # Simulate PROVENANCE.md having been reworded since this script's regexes
    # were written -- the failure mode the script's own error message names.
    provenance_path = root / "ui" / "assets" / "tensix-viz" / "PROVENANCE.md"
    provenance_path.write_text("# Vendored: tensix-viz\n\nNo bullet fields here at all.\n")
    upstream = _fake_upstream(tmp_path)

    result = _run(root, upstream)
    assert result.returncode != 0
    assert "were not found in the expected shape" in result.stdout + result.stderr
