"""scripts/publish-release.sh -- the release job's own logic, made testable.

WHY THIS EXISTS. The v0.5.5 release was cut by creating it in the GitHub UI,
which makes the tag AND the release atomically. The tag push then triggered
the workflow, whose guard refused because a release with that name already
existed -- and the run died BEFORE the upload step. The result was a published
release with zero assets, while INSTALL.md tells operators to
`gh release download --pattern '*.deb'`. Silent: the release page looks fine.

The guard's invariant was right and its TEST was a proxy:

    A published .deb is never replaced. Someone may already have installed it.

That is a statement about ASSETS. It was implemented as a statement about the
release EXISTING, and the two come apart for a release created seconds earlier
with nothing in it -- there is no .deb to protect.

The logic lived inside packages.yml, where nothing could reach it. It is a
script now, driven here with a stubbed `gh` on PATH: the same stub-binary
approach tests/unit/test_setup_venvs_weights.py uses for tt-bio.
"""
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "publish-release.sh"


def _gh_stub(tmp_path, *, exists, assets=0, body=""):
    """A `gh` that answers the three questions the script asks, and records
    every invocation so the test can assert on what was actually run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >> "{tmp_path}/gh.log"
case "$1 $2" in
  "release view")
      [ "{int(exists)}" = "1" ] || exit 1
      case "$*" in
        *--json\\ assets*)      printf '%s\\n' '{assets}' ;;
        *--json\\ body*)        printf '%s\\n' '{body}' ;;
        *) printf 'a release\\n' ;;
      esac
      ;;
esac
exit 0
""")
    gh.chmod(0o755)
    return bin_dir


def _run(tmp_path, bin_dir, tag="v9.9.9"):
    notes = tmp_path / "notes.md"
    if not notes.exists():
        notes.write_text("* something changed\n")
    pkg = tmp_path / "tt-bio-demo_9.9.9_all.deb"
    pkg.write_bytes(b"deb")
    return subprocess.run(
        ["bash", str(SCRIPT), tag, str(notes), str(pkg)],
        capture_output=True, text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})


def _log(tmp_path):
    f = tmp_path / "gh.log"
    return f.read_text() if f.exists() else ""


def test_it_parses():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_existing_release_is_created_with_its_assets(tmp_path):
    """The normal path, unchanged: push a tag, get a release with packages."""
    r = _run(tmp_path, _gh_stub(tmp_path, exists=False))
    assert r.returncode == 0, r.stdout + r.stderr
    log = _log(tmp_path)
    assert "release create" in log, log
    assert ".deb" in log, log


def test_an_existing_release_WITH_assets_is_refused(tmp_path):
    """THE INVARIANT, unchanged. Someone may have installed those packages;
    a fixed version number must mean fixed contents."""
    r = _run(tmp_path, _gh_stub(tmp_path, exists=True, assets=4))
    assert r.returncode != 0, "a release with assets was not protected"
    log = _log(tmp_path)
    assert "release create" not in log, log
    assert "release upload" not in log, f"it replaced published assets:\n{log}"


def test_an_existing_EMPTY_release_is_filled_rather_than_refused(tmp_path):
    """THE BUG. A release created in the GitHub UI exists but holds nothing,
    so there is no published .deb to protect -- and refusing leaves the
    project with a release nobody can install from."""
    r = _run(tmp_path, _gh_stub(tmp_path, exists=True, assets=0))
    assert r.returncode == 0, f"an empty release was refused:\n{r.stdout}{r.stderr}"
    log = _log(tmp_path)
    assert "release upload" in log, f"nothing was uploaded:\n{log}"
    assert ".deb" in log, log


def test_filling_an_empty_release_does_not_clobber_a_human_written_body(tmp_path):
    """Somebody wrote that description on purpose. The changelog notes are a
    default for an empty body, not a correction of someone's prose."""
    r = _run(tmp_path, _gh_stub(tmp_path, exists=True, assets=0,
                                body="my own release notes"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "release edit" not in _log(tmp_path), \
        "it overwrote a body the human wrote"


def test_an_empty_release_with_an_empty_body_gets_the_changelog_notes(tmp_path):
    """The converse: a bare release created with no description is improved,
    not left blank."""
    r = _run(tmp_path, _gh_stub(tmp_path, exists=True, assets=0, body=""))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "release edit" in _log(tmp_path), \
        "an empty body was left empty"


def test_an_unreadable_asset_list_refuses_rather_than_assuming_empty(tmp_path):
    """The fail-safe branch, which survived its first mutation because nothing
    exercised it.

    If `gh release view --json assets` fails -- a rate limit, a token without
    the scope, an API blip -- we do not know whether this release holds
    published .debs. The two possible assumptions are not symmetric: assuming
    EMPTY overwrites packages people may have installed, assuming OCCUPIED
    costs a re-run. So it refuses, and says why.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >> "{tmp_path}/gh.log"
case "$*" in
  *"--json assets"*) exit 1 ;;          # the API call that fails
  "release view"*)   printf 'a release\\n'; exit 0 ;;
esac
exit 0
""")
    gh.chmod(0o755)

    r = _run(tmp_path, bin_dir)
    assert r.returncode != 0, \
        f"an unreadable asset list was treated as safe to overwrite:\n{r.stdout}{r.stderr}"
    log = _log(tmp_path)
    assert "release upload" not in log, f"it uploaded anyway:\n{log}"
    assert "release create" not in log, log


# --- the wiring ---------------------------------------------------------------
#
# The script can be correct while the workflow still does its own thing. These
# pin that the release job actually goes through it -- which is the only
# reason any of the tests above mean anything in CI.

WORKFLOW = REPO / ".github" / "workflows" / "packages.yml"


def _release_job():
    import yaml
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["release"]


def test_the_release_job_publishes_through_this_script():
    steps = " ".join(str(s.get("run", "")) for s in _release_job()["steps"])
    assert "scripts/publish-release.sh" in steps, \
        "the release job no longer calls publish-release.sh; the tests above " \
        "are testing a script CI does not run"


def test_the_release_job_does_not_call_gh_release_create_itself():
    """The bug was a guard and a create that could get out of step with each
    other. One decision, one place -- so the workflow must not grow its own
    copy back."""
    steps = " ".join(str(s.get("run", "")) for s in _release_job()["steps"])
    for forbidden in ("gh release create", "gh release upload", "gh release edit"):
        assert forbidden not in steps, \
            f"the workflow calls `{forbidden}` directly again; that logic " \
            "belongs in publish-release.sh where it can be tested"


def test_the_release_job_still_only_runs_for_a_pushed_tag():
    """Untouched by this change, and worth pinning while editing the job: a
    workflow_dispatch on an existing tag must never publish."""
    cond = _release_job()["if"]
    assert "github.event_name == 'push'" in cond
    assert "startsWith(github.ref, 'refs/tags/')" in cond
