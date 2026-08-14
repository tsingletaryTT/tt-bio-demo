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
