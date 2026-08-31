"""scripts/doctor.sh -- the check-and-repair tool that has to work whether
the booth was installed from a git checkout or from the .deb packages.

Its branches are tested by SOURCING the script and calling its functions,
the same approach debian/helpers.sh uses and for the same reason: a shell
script full of conditionals that nothing calls directly is a script whose
behaviour is only ever observed once, at a venue, by someone who cannot
debug it.

`DOCTOR_LIB_ONLY=1` makes it define its functions and return instead of
running the checks, which is what makes any of this possible.
"""
import os
import re
import subprocess
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCTOR = REPO / "scripts" / "doctor.sh"


def _sh(script, **env):
    return subprocess.run(
        ["bash", "-c", f"DOCTOR_LIB_ONLY=1 . {DOCTOR}\n{script}"],
        capture_output=True, text=True, env={**os.environ, **env})


def _uncommented(text):
    out = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line.split(" #", 1)[0])
    return "\n".join(out)


def test_it_parses():
    r = subprocess.run(["bash", "-n", str(DOCTOR)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_it_never_resets_a_card_or_installs_behind_your_back():
    """The standing rules of this project, in the one script most tempted to
    break them: it is a "fix things" tool that runs on a shared box."""
    s = _uncommented(DOCTOR.read_text())
    for forbidden in ("tt-smi -r", "tt-smi --reset", "dpkg -i",
                      "apt-get install", "apt install", "rmmod", "modprobe -r"):
        assert forbidden not in s, f"doctor must never run: {forbidden}"


def test_install_deps_is_only_ever_printed_never_executed():
    """`tt-bio install-deps` installs kernel modules. The doctor may TELL you
    to run it; it must not run it, not even under --fix."""
    s = _uncommented(DOCTOR.read_text())
    for line in s.splitlines():
        if "install-deps" not in line:
            continue
        stripped = line.strip()
        assert (stripped.startswith("echo") or stripped.startswith("say")
                or stripped.startswith("fail") or stripped.startswith("warn")
                or stripped.startswith("hint") or '"' in stripped), \
            f"install-deps appears outside a message: {line!r}"


def test_it_finds_the_repo_when_run_from_a_source_checkout():
    r = _sh("doctor_prefix")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(REPO), \
        f"source checkout not detected: {r.stdout.strip()!r}"


def test_it_finds_the_installed_tree_when_there_is_no_checkout(tmp_path):
    """The .deb case: /opt/tt-bio-demo, with no git repo anywhere near."""
    fake = tmp_path / "opt" / "tt-bio-demo"
    (fake / "ui").mkdir(parents=True)
    (fake / "scripts").mkdir()
    (fake / "playlist").mkdir()
    r = _sh("doctor_prefix", TT_BIO_DEMO_PREFIX=str(fake))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(fake)


def test_it_reports_the_install_mode_it_detected(tmp_path):
    """Which mode it is in changes the advice it gives, so it must say."""
    assert _sh("doctor_install_mode").stdout.strip() == "source"
    fake = tmp_path / "opt" / "tt-bio-demo"
    (fake / "ui").mkdir(parents=True)
    r = _sh("doctor_install_mode", TT_BIO_DEMO_PREFIX=str(fake))
    assert r.stdout.strip() == "package"


def test_a_missing_venv_is_a_failure_not_a_warning(tmp_path):
    """The booth cannot fold without venv-runner. Reporting that as a warning
    would let someone leave for a venue believing they were fine."""
    fake = tmp_path / "prefix"
    (fake / "ui").mkdir(parents=True)
    r = _sh("doctor_check_venvs", TT_BIO_DEMO_PREFIX=str(fake))
    assert r.returncode != 0
    assert "setup-venvs.sh" in (r.stdout + r.stderr), \
        "said the venv was missing without saying how to build it"


def test_every_playlist_input_is_checked_for_existence(tmp_path):
    """The failure this catches is a booth that dies mid-loop on target 3."""
    fake = tmp_path / "prefix"
    (fake / "playlist").mkdir(parents=True)
    (fake / "examples").mkdir()
    (fake / "playlist" / "manifest.yaml").write_text(
        "- id: ghost\n  name: Ghost\n  input: ../examples/nope.yaml\n"
        "  expected_s: 1.0\n  blurb: b\n  tagline: t\n")
    r = _sh("doctor_check_playlist", TT_BIO_DEMO_PREFIX=str(fake))
    assert r.returncode != 0, "a manifest naming a missing input passed"
    assert "nope.yaml" in (r.stdout + r.stderr)


def test_a_playlist_whose_inputs_all_exist_passes(tmp_path):
    """The matched pair: a checker that always fails is as useless as one
    that always passes."""
    fake = tmp_path / "prefix"
    (fake / "playlist").mkdir(parents=True)
    (fake / "examples").mkdir()
    (fake / "examples" / "real.yaml").write_text("sequences: []\n")
    (fake / "playlist" / "manifest.yaml").write_text(
        "- id: real\n  name: Real\n  input: ../examples/real.yaml\n"
        "  expected_s: 1.0\n  blurb: b\n  tagline: t\n")
    r = _sh("doctor_check_playlist", TT_BIO_DEMO_PREFIX=str(fake))
    assert r.returncode == 0, r.stdout + r.stderr


def test_weights_are_checked_by_size_not_merely_existence(tmp_path):
    """A truncated or zero-byte download is the realistic failure -- an
    existence check calls that healthy."""
    cache = tmp_path / ".boltz"
    cache.mkdir()
    (cache / "protenix-v2.pt").write_bytes(b"")
    (cache / "mols.tar").write_bytes(b"")
    r = _sh("doctor_check_weights", BOLTZ_CACHE=str(cache))
    assert r.returncode != 0, "zero-byte weights reported as present"


def test_the_exit_code_distinguishes_broken_from_merely_imperfect():
    """A booth with no hardware attached is a WARNING (you can still develop);
    a booth with no venv is a FAILURE. `--fix` scripts and CI both need to
    tell those apart, so they must not share an exit code."""
    s = DOCTOR.read_text()
    assert "EXIT_OK" in s and "EXIT_WARN" in s and "EXIT_FAIL" in s
    assert re.search(r"EXIT_WARN=\s*2", s), "warnings must not share 0 or 1"


# ---------------------------------------------------------------------------
# The weights check, after a user reported the gap this section exists for:
# "I had to discover a model downloading command, the doctor.sh didn't know
# about". Two separate defects were behind that, plus a third found while
# fixing them.
#
# The check has two layers, and these tests say which one they mean:
#
#   1. filesystem presence and a size floor -- always available, and the ONLY
#      thing that can run before venv-runner exists, which is exactly when an
#      operator most needs the doctor.
#   2. tt-bio's own verifier, when venv-runner is built -- authoritative, and
#      the only thing that can tell a complete artifact from a large corrupt
#      one.
#
# Layer 1 is tested against a prefix with no venv-runner in it, so the result
# is deterministic rather than depending on what happens to be installed on
# the machine running the suite. Layer 2 is tested with a stub interpreter,
# the same way tests/unit/test_run_demo_sh.py stubs its two venvs.
# ---------------------------------------------------------------------------

def _sized(path, size):
    """A file that REPORTS `size` bytes without occupying them.

    The check under test reads `stat -c %s`, so a sparse file exercises it
    exactly. The first version of these tests wrote the bytes for real -- 1.1
    GB and 1.9 GB per fixture -- and put 15 GB into /tmp across three runs on
    a box that was already at 100%. A test that needs a gigabyte of disk to
    assert on a number is measuring the wrong thing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)


def _prefix_without_a_runner_venv(tmp_path, mode="source"):
    """A prefix the doctor can find but which has no venv-runner, forcing the
    filesystem fallback. `mode` picks what doctor_install_mode will report:
    a `tests/` directory is what makes it a source checkout."""
    prefix = tmp_path / "prefix"
    (prefix / "ui").mkdir(parents=True)
    if mode == "source":
        (prefix / "tests").mkdir()
    return prefix


def test_an_unpacked_molecule_library_is_healthy_even_with_no_tar(tmp_path):
    """The FALSE ALARM. The check required mols.tar at >= 1 GB, but tt-bio
    discards that archive once it is unpacked -- its own status() calls the
    archive being gone "harmless for mols once the library is unpacked" -- and
    `tt-bio weights --prune` deletes it outright. A fold loads <cache>/mols.
    So a booth that can fold perfectly well was being told it was broken."""
    cache = tmp_path / ".boltz"
    (cache / "mols").mkdir(parents=True)
    (cache / "mols" / "ALA.pkl").write_bytes(b"x")
    _sized(cache / "protenix-v2.pt", 1_100_000_000)
    r = _sh("doctor_check_weights", BOLTZ_CACHE=str(cache), TT_BIO_CACHE="",
            TT_BIO_DEMO_PREFIX=str(_prefix_without_a_runner_venv(tmp_path)))
    assert r.returncode == 0, f"healthy booth reported broken:\n{r.stdout}{r.stderr}"


def test_a_tar_with_no_unpacked_library_is_not_ready(tmp_path):
    """The converse: an install interrupted between download and extraction.
    The tar is present, the directory is not, and a fold cannot run."""
    cache = tmp_path / ".boltz"
    cache.mkdir(parents=True)
    _sized(cache / "protenix-v2.pt", 1_100_000_000)
    _sized(cache / "mols.tar", 1_900_000_000)
    r = _sh("doctor_check_weights", BOLTZ_CACHE=str(cache), TT_BIO_CACHE="",
            TT_BIO_DEMO_PREFIX=str(_prefix_without_a_runner_venv(tmp_path)))
    assert r.returncode != 0, f"unextracted molecule library called ready:\n{r.stdout}"


def test_an_empty_molecule_directory_is_not_a_library(tmp_path):
    """`mkdir mols` is not an unpacked library, and it is what a failed
    extraction leaves behind. Presence of the directory alone must not be the
    test, or this fix would just move the false-healthy report."""
    cache = tmp_path / ".boltz"
    (cache / "mols").mkdir(parents=True)
    _sized(cache / "protenix-v2.pt", 1_100_000_000)
    r = _sh("doctor_check_weights", BOLTZ_CACHE=str(cache), TT_BIO_CACHE="",
            TT_BIO_DEMO_PREFIX=str(_prefix_without_a_runner_venv(tmp_path)))
    assert r.returncode != 0, f"empty mols/ called ready:\n{r.stdout}"


@pytest.mark.parametrize("mode", ["source", "package"])
def test_it_names_a_command_that_actually_downloads_the_weights(tmp_path, mode):
    """THE REPORTED BUG. In source mode the entire advice was "they download
    on the first fold, or fetch them ahead of time" -- no command. A user had
    to go and find `tt-bio weights --download` themselves.

    A hint that does not contain a command is not a hint. Both modes must name
    it: the packaged install has dpkg-reconfigure as its preferred route, but
    an operator whose debconf answer is stuck still needs the direct one.

    This asserts on the command, not on the prose around it, so rewording the
    message freely is fine and deleting the command is not."""
    cache = tmp_path / ".boltz"
    cache.mkdir(parents=True)
    r = _sh("doctor_check_weights", BOLTZ_CACHE=str(cache), TT_BIO_CACHE="",
            TT_BIO_DEMO_PREFIX=str(_prefix_without_a_runner_venv(tmp_path, mode)))
    out = r.stdout + r.stderr
    assert "tt-bio weights --download" in out, f"no download command offered:\n{out}"
    assert "protenix-v2" in out, f"command does not name the model:\n{out}"


def test_the_packaged_install_still_leads_with_dpkg_reconfigure(tmp_path):
    """Naming the tt-bio command everywhere must not cost the packaged install
    its own answer: debconf is where a .deb install's consent lives, and
    running the download outside it leaves the package's own state stale."""
    cache = tmp_path / ".boltz"
    cache.mkdir(parents=True)
    r = _sh("doctor_check_weights", BOLTZ_CACHE=str(cache), TT_BIO_CACHE="",
            TT_BIO_DEMO_PREFIX=str(_prefix_without_a_runner_venv(tmp_path, "package")))
    assert "dpkg-reconfigure tt-bio-demo-weights" in r.stdout + r.stderr


def test_tt_bios_own_verdict_is_preferred_when_venv_runner_exists(tmp_path):
    """LAYER 2. A file can be the right size and still be corrupt -- a
    truncated 1.8 GB download is the case the size floor cannot see, and it
    surfaces at a venue as `PytorchStreamReader ... failed finding central
    directory`. When venv-runner is built, tt-bio's own verifier is asked and
    its verdict wins.

    Stubbed rather than mocked: a real interpreter that prints the verdict
    this test wants, so what is being checked is that the doctor RUNS it and
    HONOURS it, not that some function was called."""
    prefix = tmp_path / "prefix"
    (prefix / "ui").mkdir(parents=True)
    (prefix / "tests").mkdir()
    stub = prefix / ".venvs" / "venv-runner" / "bin"
    stub.mkdir(parents=True)
    py = stub / "python3"
    py.write_text("#!/bin/sh\n"
                  "echo 'protenix-v2 corrupt'\n"
                  "echo 'mols present'\n")
    py.chmod(0o755)

    cache = tmp_path / ".boltz"
    (cache / "mols").mkdir(parents=True)
    (cache / "mols" / "ALA.pkl").write_bytes(b"x")
    # Big enough to sail through the size floor: only tt-bio can catch this.
    _sized(cache / "protenix-v2.pt", 1_100_000_000)

    r = _sh("doctor_check_weights", BOLTZ_CACHE=str(cache), TT_BIO_CACHE="",
            TT_BIO_DEMO_PREFIX=str(prefix))
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"corrupt checkpoint reported healthy:\n{out}"
    assert "corrupt" in out.lower(), out


def test_the_weights_cache_honours_tt_bio_cache(tmp_path):
    """$TT_BIO_CACHE is the variable tt-bio itself prefers, and the doctor
    read only the older $BOLTZ_CACHE. On a booth whose cache was relocated,
    the check that exists to find missing weights looked in the directory the
    operator had moved away from."""
    moved = tmp_path / "big-disk"
    moved.mkdir()
    r = _sh("doctor_weights_cache", TT_BIO_CACHE=str(moved), BOLTZ_CACHE=str(tmp_path / "old"))
    assert r.stdout.strip() == str(moved), r.stdout


def test_an_empty_cache_variable_falls_through_rather_than_meaning_cwd(tmp_path):
    """`TT_BIO_CACHE=` is what an exported-but-unset variable looks like in a
    systemd unit or a sourced env file. Honouring it as a path would point the
    check at whatever directory the doctor was run from."""
    r = _sh("doctor_weights_cache", TT_BIO_CACHE="", BOLTZ_CACHE="", HOME="/home/somebody")
    assert r.stdout.strip() == "/home/somebody/.boltz", r.stdout
