"""scripts/setup-venvs.sh's weights step.

The gap this covers was reported by a user who got the booth working and then
said: "The docs were not quite right - I had to discover a model downloading
command". The source install path built both venvs and stopped. Nothing in it
fetched the 3.7 GB the booth cannot fold without, and nothing in it said so --
so the first fold either pulled them silently or, at a venue, failed.

The .deb had covered this since Phase 3b (debian/tt-bio-demo-weights.postinst,
behind a debconf question). A git checkout had nothing.

Tested by SOURCING the script with SETUP_VENVS_LIB_ONLY=1, the same way
tests/unit/test_doctor.py sources doctor.sh: a shell function nothing calls
directly is one whose behaviour is only ever observed at a venue.
"""
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SETUP = REPO / "scripts" / "setup-venvs.sh"


def _stub_prefix(tmp_path, *, tt_bio_exit=0):
    """A prefix holding a venv-runner whose `tt-bio` is a stub that records its
    argv. Real interpreter, real exit code -- what is under test is that the
    script RUNS the right command and honours what it returns."""
    prefix = tmp_path / "prefix"
    bin_ = prefix / "venv-runner" / "bin"
    bin_.mkdir(parents=True)
    (bin_ / "python3").write_text("#!/bin/sh\nexit 0\n")
    (bin_ / "python3").chmod(0o755)
    tt_bio = bin_ / "tt-bio"
    tt_bio.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{tmp_path}/argv.log"\n'
        f"exit {tt_bio_exit}\n")
    tt_bio.chmod(0o755)
    return prefix


def _run(tmp_path, script, *args, **env):
    """Source setup-venvs.sh as a library, then run `script`."""
    argv = " ".join(f"'{a}'" for a in args)
    return subprocess.run(
        ["bash", "-c",
         f"SETUP_VENVS_LIB_ONLY=1 . {SETUP} {argv}\n{script}"],
        capture_output=True, text=True, env={**os.environ, **env}, cwd=REPO)


def _argv_log(tmp_path):
    log = tmp_path / "argv.log"
    return log.read_text() if log.exists() else ""


def test_it_still_parses():
    r = subprocess.run(["bash", "-n", str(SETUP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_by_default_it_fetches_the_weights(tmp_path):
    """THE REPORTED GAP. A plain source install must end with a box that can
    actually fold, not one that needs a command the operator has to discover."""
    prefix = _stub_prefix(tmp_path)
    r = _run(tmp_path, "fetch_weights", "--prefix", str(prefix))
    assert "weights --download protenix-v2" in _argv_log(tmp_path), (
        f"nothing was fetched:\n{r.stdout}{r.stderr}")


def test_skip_weights_is_a_recognised_flag(tmp_path):
    """Asserted separately from the skipping, and this is not pedantry.

    The first version of the test below checked only that nothing was
    downloaded. Deleting the `--skip-weights` case from the argument parser
    left it GREEN: the flag then fell through to the `*)` catch-all, which
    prints usage and exits 1, so the sourced shell died before fetch_weights
    was even defined -- an empty download log for a reason that has nothing to
    do with skipping. Verified as a real surviving mutation, not a worry.
    """
    prefix = _stub_prefix(tmp_path)
    r = _run(tmp_path, "echo PARSED_OK", "--prefix", str(prefix), "--skip-weights")
    assert "PARSED_OK" in r.stdout, (
        f"--skip-weights was not accepted as a flag:\n{r.stdout}{r.stderr}")
    assert "unknown argument" not in r.stdout + r.stderr


def test_skip_weights_fetches_nothing(tmp_path):
    """The opt-out. 3.7 GB is not something to make unavoidable."""
    prefix = _stub_prefix(tmp_path)
    r = _run(tmp_path, "fetch_weights", "--prefix", str(prefix), "--skip-weights")
    out = r.stdout + r.stderr
    assert _argv_log(tmp_path) == "", f"--skip-weights downloaded anyway:\n{out}"
    # It must have SKIPPED, not merely failed to get that far.
    assert "--skip-weights" in out, f"nothing said it was skipping:\n{out}"


def test_skip_runner_implies_skip_weights(tmp_path):
    """--skip-runner is for iterating on the UI. There is no venv-runner to
    fetch WITH, so asking would be a confusing failure rather than a download."""
    prefix = _stub_prefix(tmp_path)
    r = _run(tmp_path, "fetch_weights", "--prefix", str(prefix), "--skip-runner")
    out = r.stdout + r.stderr
    assert _argv_log(tmp_path) == "", f"--skip-runner downloaded anyway:\n{out}"
    assert "--skip-runner" in out, f"nothing said it was skipping:\n{out}"


def test_a_failed_download_is_reported_but_not_fatal(tmp_path):
    """A conference-hotel connection dropping must not turn a bootstrap that
    built both venvs correctly into exit 1. The venvs are still good; what is
    missing is a resumable download, and the script says how to resume it."""
    prefix = _stub_prefix(tmp_path, tt_bio_exit=1)
    r = _run(tmp_path, "fetch_weights || echo CAUGHT_NONZERO",
             "--prefix", str(prefix))
    out = r.stdout + r.stderr
    assert "CAUGHT_NONZERO" not in out, f"a failed fetch was made fatal:\n{out}"
    assert "tt-bio weights --download protenix-v2" in out, (
        f"a failed fetch must leave the operator the command:\n{out}")


def test_a_missing_runner_venv_prints_the_command_instead_of_failing(tmp_path):
    """setup-venvs.sh's own degraded path (exit 2: venv-runner built but its
    stack will not import) leaves no usable tt-bio. Print, do not die."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    r = _run(tmp_path, "fetch_weights || echo CAUGHT_NONZERO", "--prefix", str(prefix))
    out = r.stdout + r.stderr
    assert "CAUGHT_NONZERO" not in out, out
    assert "tt-bio weights --download protenix-v2" in out, out


def test_the_flag_is_documented_in_the_usage_text():
    """--help is the first place someone looks for the opt-out, and this
    script's usage() is hand-maintained."""
    r = subprocess.run(["bash", str(SETUP), "--help"], capture_output=True, text=True)
    assert "--skip-weights" in r.stdout + r.stderr


def test_the_header_comment_documents_it_too():
    """The header block is what `--help` and the README both quote from, and
    this project has had it go stale before (run-demo.sh shipped a flag
    invisibly for exactly this reason)."""
    assert "--skip-weights" in SETUP.read_text().split("set -euo pipefail")[0]


def test_the_fetch_is_told_which_cache_to_use(tmp_path):
    """FOUND IN REVIEW. The summary prints `weights: $(weights_cache_dir)` but
    the fetch ran with no --cache, letting tt-bio re-derive the root from its
    own environment. Two independent derivations again -- the exact defect
    this branch exists to end.

    They differ whenever HOME differs between the two evaluations, and the
    documented way to run this is `sudo scripts/setup-venvs.sh` (doctor.sh
    prints that; so does INSTALL.md). Under sudo the 3.7 GB can land in root's
    home while the user-service booth loads from the operator's -- a download
    that reports success and leaves the booth unable to fold.
    """
    prefix = _stub_prefix(tmp_path)
    cache = tmp_path / "chosen-cache"
    r = _run(tmp_path, "fetch_weights", "--prefix", str(prefix),
             TT_BIO_CACHE=str(cache))
    log = _argv_log(tmp_path)
    assert "--cache" in log, f"the fetch did not pin the cache:\n{log}\n{r.stdout}"
    assert str(cache) in log, f"the fetch used a different cache:\n{log}"
