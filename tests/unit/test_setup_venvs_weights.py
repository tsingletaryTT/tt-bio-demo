"""scripts/setup-venvs.sh's weights step.

The gap this covers was reported by a user who got the booth working and then
said: "The docs were not quite right - I had to discover a model downloading
command". The source install path built both venvs and stopped. Nothing in it
fetched the 3.7 GB the booth cannot fold without, and nothing in it said so --
so the first fold either pulled them silently or, at a venue, failed.

The .deb had covered this since Phase 3b (debian/tt-bio-demo-weights.postinst,
behind a debconf question). A git checkout had nothing.

Extended for the affinity-questions feature (2026-09-15/16): nesso1's weights
(the affinity head + its own CCD dict) and the ESM-2 encoder its featurizer
needs had the identical gap one layer up -- see docs/followups.md's "From the
affinity-questions feature" entry. `fetch_weights` now fetches all three,
under the SAME `--skip-weights`/`--skip-runner` gates as protenix-v2 -- a
booth that opts out of weights should not end up with folding but not Q&A (or
vice versa) because a second flag was needed and nobody added one.

Tested by SOURCING the script with SETUP_VENVS_LIB_ONLY=1, the same way
tests/unit/test_doctor.py sources doctor.sh: a shell function nothing calls
directly is one whose behaviour is only ever observed at a venue.
"""
import ast
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SETUP = REPO / "scripts" / "setup-venvs.sh"


def _stub_prefix(tmp_path, *, tt_bio_exit=0, python3_exit=0):
    """A prefix holding a venv-runner whose `tt-bio` and `python3` are stubs
    that record their invocations. Real interpreters, real exit codes -- what
    is under test is that the script RUNS the right command and honours what
    it returns.

    `python3` consumes its stdin (the ESM-2 pre-warm step feeds it a heredoc
    script) rather than leaving it unread, and logs one line per invocation
    to `python3.log` -- separate from `tt-bio`'s `argv.log` -- so a test can
    tell whether the ESM-2 pre-warm ran at all without caring what Python
    code it would have executed for real.
    """
    prefix = tmp_path / "prefix"
    bin_ = prefix / "venv-runner" / "bin"
    bin_.mkdir(parents=True)
    py = bin_ / "python3"
    py.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        f'echo called >> "{tmp_path}/python3.log"\n'
        f"exit {python3_exit}\n")
    py.chmod(0o755)
    tt_bio = bin_ / "tt-bio"
    tt_bio.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{tmp_path}/argv.log"\n'
        # One line per invocation, positionally matching argv.log -- lets a
        # test check what the SUBPROCESS actually saw in its own
        # environment, not just what was typed on its command line. This is
        # what matters for nesso1/nesso1-ccd: --cache is a no-op for them
        # (see test_the_nesso1_fetch_is_told_which_cache_to_use below), and
        # what actually redirects them is whether $TT_BIO_CACHE is present
        # in the environment tt-bio itself runs in.
        f'printf "TT_BIO_CACHE=%s\\n" "${{TT_BIO_CACHE:-}}" >> "{tmp_path}/env.log"\n'
        f"exit {tt_bio_exit}\n")
    tt_bio.chmod(0o755)
    return prefix


def _env_log(tmp_path):
    log = tmp_path / "env.log"
    return log.read_text() if log.exists() else ""


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


def _python3_call_count(tmp_path):
    log = tmp_path / "python3.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


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


# ---------------------------------------------------------------------------
# The affinity-questions provisioning gap (docs/followups.md, "From the
# affinity-questions feature"): nesso1's weights and the ESM-2 encoder its
# featurizer needs had no fetch step at all. These mirror the protenix-v2
# tests above, one artifact set over.
# ---------------------------------------------------------------------------

def test_it_also_fetches_nesso1(tmp_path):
    """THE GAP THIS SECTION EXISTS FOR. `tt-bio weights --download nesso1` is
    a MODEL name, not an artifact key -- confirmed against the pinned
    tt-bio's own CLI (weights_cmd resolves MODEL names via
    weights.artifacts_for(*models); "nesso1-ccd" is an artifact key and
    raises KeyError if passed to --download). One call fetches BOTH of
    nesso1's registry rows (nesso1 itself and nesso1-ccd), since
    tt_bio.weights.MODEL_ARTIFACTS["nesso1"] lists both."""
    prefix = _stub_prefix(tmp_path)
    r = _run(tmp_path, "fetch_weights", "--prefix", str(prefix))
    log = _argv_log(tmp_path)
    assert "weights --download nesso1" in log, (
        f"nesso1 was not fetched:\n{r.stdout}{r.stderr}\n{log}")
    # Never the artifact key by itself: that is not a valid --download MODEL.
    assert "download nesso1-ccd" not in log, (
        f"nesso1-ccd is an artifact key, not a model -- `--download "
        f"nesso1-ccd` raises KeyError on the real CLI:\n{log}")


def test_it_also_pre_warms_the_esm2_cache(tmp_path):
    """The ESM-2 encoder (nesso1's featurizer) is not in tt_bio.weights'
    registry at all (its own module comment says so directly), so there is
    no `tt-bio weights --download` row to ask for it -- this is fetched
    through venv-runner's own python3 instead, which is why it shows up as a
    python3 invocation rather than another tt-bio argv line."""
    prefix = _stub_prefix(tmp_path)
    r = _run(tmp_path, "fetch_weights", "--prefix", str(prefix))
    assert _python3_call_count(tmp_path) >= 1, (
        f"the ESM-2 pre-warm never ran python3:\n{r.stdout}{r.stderr}")


def _stub_prefix_capturing_stdin(tmp_path):
    """Like _stub_prefix, but python3 SAVES the heredoc it is fed to a file
    instead of discarding it, so a test can inspect the exact code that
    would run for real -- not just that python3 ran. Only the python3 half
    is needed for the test below; tt-bio is stubbed out identically to
    _stub_prefix for functions that call both."""
    prefix = tmp_path / "prefix"
    bin_ = prefix / "venv-runner" / "bin"
    bin_.mkdir(parents=True)
    py = bin_ / "python3"
    py.write_text(
        "#!/bin/sh\n"
        f'cat > "{tmp_path}/stdin.py"\n'
        "exit 0\n")
    py.chmod(0o755)
    tt_bio = bin_ / "tt-bio"
    tt_bio.write_text("#!/bin/sh\nexit 0\n")
    tt_bio.chmod(0o755)
    return prefix


def test_the_esm2_prewarm_restricts_the_download_to_the_files_it_needs(tmp_path):
    """THE BUG. Without allow_patterns/ignore_patterns, snapshot_download
    fetches EVERY file in the HF repo -- measured on the dev box:
    model.safetensors (2.6 GB) AND pytorch_model.bin (2.6 GB) AND
    tf_model.h5 (2.6 GB), ~7.3 GB total, when setup_esm_model's
    AutoModelForMaskedLM.from_pretrained() prefers safetensors and never
    touches the other two once it is present. Every "~2.6 GB" figure in
    this script's own comments and the docs it is quoted into assumed the
    restriction was already there; it was not.

    This parses the ACTUAL heredoc fed to python3 -- not just that python3
    ran -- via a stub that captures stdin instead of discarding it, so a
    future edit that drops the restriction is caught even though the stub
    never executes real Python."""
    prefix = _stub_prefix_capturing_stdin(tmp_path)
    _run(tmp_path, "fetch_esm2_cache", "--prefix", str(prefix))
    src = (tmp_path / "stdin.py").read_text()
    tree = ast.parse(src)
    call = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "snapshot_download"):
            call = node
            break
    assert call is not None, f"no snapshot_download call found:\n{src}"
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    assert "ignore_patterns" in kwargs or "allow_patterns" in kwargs, (
        f"snapshot_download has no allow_patterns/ignore_patterns -- it "
        f"will fetch every weight format in the repo (~7.3 GB instead of "
        f"~2.6 GB):\n{src}")
    if "ignore_patterns" in kwargs:
        assert isinstance(kwargs["ignore_patterns"], (ast.List, ast.Tuple)), (
            f"ignore_patterns is not a literal list/tuple this test can "
            f"check the contents of:\n{src}")
        patterns = [elt.value for elt in kwargs["ignore_patterns"].elts]
        for unwanted in ("*.bin", "*.h5"):
            assert unwanted in patterns, (
                f"ignore_patterns does not exclude {unwanted}, so it "
                f"downloads anyway: {patterns}\n{src}")
        for needed in ("*.safetensors", "model.safetensors", "*.json", "*.txt"):
            assert needed not in patterns, (
                f"ignore_patterns excludes {needed!r}, which the real "
                f"featurizer needs: {patterns}\n{src}")
    else:
        assert isinstance(kwargs["allow_patterns"], (ast.List, ast.Tuple)), (
            f"allow_patterns is not a literal list/tuple this test can "
            f"check the contents of:\n{src}")
        patterns = [elt.value for elt in kwargs["allow_patterns"].elts]
        assert any("safetensors" in p for p in patterns), (
            f"allow_patterns does not admit the safetensors weights at "
            f"all: {patterns}\n{src}")


def test_skip_weights_skips_nesso1_and_esm2_too(tmp_path):
    """ONE FLAG FOR ALL OF IT. A booth that opts out of downloading weights
    must not end up with protenix-v2 skipped and nesso1/ESM-2 fetched anyway
    (or the reverse) because a second flag was needed and nobody added one."""
    prefix = _stub_prefix(tmp_path)
    r = _run(tmp_path, "fetch_weights", "--prefix", str(prefix), "--skip-weights")
    out = r.stdout + r.stderr
    assert _argv_log(tmp_path) == "", f"--skip-weights fetched nesso1 anyway:\n{out}"
    assert _python3_call_count(tmp_path) == 0, (
        f"--skip-weights pre-warmed ESM-2 anyway:\n{out}")
    assert "nesso1" in out, f"nothing said Q&A weights were skipped too:\n{out}"


def test_skip_runner_skips_nesso1_and_esm2_too(tmp_path):
    """Same reasoning as --skip-weights: there is no venv-runner to fetch
    WITH, for any of the three artifact sets."""
    prefix = _stub_prefix(tmp_path)
    r = _run(tmp_path, "fetch_weights", "--prefix", str(prefix), "--skip-runner")
    out = r.stdout + r.stderr
    assert _argv_log(tmp_path) == "", f"--skip-runner fetched nesso1 anyway:\n{out}"
    assert _python3_call_count(tmp_path) == 0, (
        f"--skip-runner pre-warmed ESM-2 anyway:\n{out}")


def test_a_failed_nesso1_download_is_reported_but_not_fatal(tmp_path):
    """Same non-fatal-download reasoning as protenix-v2: a resumable download
    dropping must not turn a good venv bootstrap into exit 1."""
    prefix = _stub_prefix(tmp_path, tt_bio_exit=1)
    r = _run(tmp_path, "fetch_weights || echo CAUGHT_NONZERO", "--prefix", str(prefix))
    out = r.stdout + r.stderr
    assert "CAUGHT_NONZERO" not in out, f"a failed nesso1 fetch was made fatal:\n{out}"
    assert "weights --download nesso1" in out, (
        f"a failed nesso1 fetch must leave the operator the command:\n{out}")


def test_a_failed_esm2_prewarm_is_reported_but_not_fatal(tmp_path):
    """Same reasoning again, one artifact set over: the ESM-2 pre-warm is a
    resumable network operation too, and a dropped connection must not cost
    the operator a working venv bootstrap."""
    prefix = _stub_prefix(tmp_path, python3_exit=1)
    r = _run(tmp_path, "fetch_weights || echo CAUGHT_NONZERO", "--prefix", str(prefix))
    out = r.stdout + r.stderr
    assert "CAUGHT_NONZERO" not in out, f"a failed ESM-2 pre-warm was made fatal:\n{out}"
    assert "esm2" in out.lower() or "ESM-2" in out, (
        f"a failed ESM-2 pre-warm must say so:\n{out}")


def test_the_nesso1_fetch_is_told_which_cache_to_use(tmp_path):
    """UNLIKE the protenix-v2 version of this test, `--cache` here is NOT
    what redirects nesso1/nesso1-ccd -- confirmed empirically against the
    pinned tt-bio (see task-15-report.md): `weights.fetch`/`.resolve`/
    `.status` IGNORE `root=` (the CLI's `--cache`) for "hf-repo" sourced
    artifacts, which both of nesso1's registry rows are. `--cache` is still
    passed here, for the same reason the postinst's Python block still
    passes `root=cache` to `weights.fetch("nesso1", ...)` even though it is
    a no-op there too: one invocation shape for every artifact, rather than
    a special case an editor could accidentally drop. This only checks that
    the flag is present and consistent with the protenix-v2 call, NOT that
    it has any effect on where nesso1 lands -- see the test below for what
    actually controls that."""
    prefix = _stub_prefix(tmp_path)
    cache = tmp_path / "chosen-cache"
    r = _run(tmp_path, "fetch_weights", "--prefix", str(prefix),
             TT_BIO_CACHE=str(cache))
    log = _argv_log(tmp_path)
    nesso1_lines = [l for l in log.splitlines() if "nesso1" in l]
    assert nesso1_lines, f"no nesso1 invocation found:\n{log}"
    assert all("--cache" in l and str(cache) in l for l in nesso1_lines), (
        f"the nesso1 fetch did not use the pinned cache:\n{nesso1_lines}")


def test_nesso1_actually_lands_under_the_operators_cache_via_the_environment(tmp_path):
    """THE REAL LEVER. `tt_bio.weights.configure_hf_cache()` redirects the
    Hugging Face hub cache (where nesso1/nesso1-ccd actually resolve) under
    `$TT_BIO_CACHE` when that variable is present in the PROCESS environment
    tt-bio itself runs in -- not from `--cache`, which it ignores for these
    two rows (see the test above). Bash does not strip environment
    variables from child processes, so as long as this script does not
    unset or otherwise hide `$TT_BIO_CACHE` before invoking `tt-bio`, an
    operator who sets it (the documented, preferred variable -- see
    scripts/weights-cache.sh's own header comment) gets the redirect for
    free, with no extra code needed here. This proves that property against
    the real subprocess environment, not just against its command line."""
    prefix = _stub_prefix(tmp_path)
    cache = tmp_path / "chosen-cache"
    _run(tmp_path, "fetch_weights", "--prefix", str(prefix),
         TT_BIO_CACHE=str(cache))
    env_log = _env_log(tmp_path)
    assert env_log, f"tt-bio never ran:\n{env_log}"
    assert all(line == f"TT_BIO_CACHE={cache}" for line in env_log.splitlines()), (
        f"$TT_BIO_CACHE did not reach every tt-bio invocation unchanged -- "
        f"nesso1 would silently land in a different cache than protenix-v2:"
        f"\n{env_log}")


def test_the_hardcoded_esm2_model_id_matches_the_real_constant():
    """ESM2_MODEL is hardcoded in setup-venvs.sh rather than imported from
    tt_bio.nesso1_input (which pulls torch, rdkit and safetensors at module
    scope just to read one string -- see the big comment above fetch_weights
    in setup-venvs.sh). Hardcoding without a guard is exactly the kind of
    drift this project's CLAUDE.md warns about, so this pins the literal
    against the pinned tt-bio's own source, parsed with `ast` rather than
    imported for the same reason the postinst's contract test does.
    """
    site = next((REPO / ".venvs" / "venv-runner").glob(
        "lib/python3.*/site-packages/tt_bio"), None)
    if site is None:
        pytest.skip("venv-runner is not built; cannot check the real constant")

    tree = ast.parse((site / "nesso1_input.py").read_text())
    real_value = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "ESM2_MODEL"
                and isinstance(node.value, ast.Constant)):
            real_value = node.value.value
            break
    assert real_value is not None, (
        "could not find ESM2_MODEL = \"...\" in tt_bio/nesso1_input.py; "
        "update this test if it was renamed")

    hardcoded = None
    for line in SETUP.read_text().splitlines():
        line = line.strip()
        if line.startswith("ESM2_MODEL="):
            hardcoded = line.split("=", 1)[1].strip('"')
            break
    assert hardcoded == real_value, (
        f"setup-venvs.sh hardcodes ESM2_MODEL={hardcoded!r}, but the pinned "
        f"tt-bio's tt_bio.nesso1_input.ESM2_MODEL is {real_value!r}")
