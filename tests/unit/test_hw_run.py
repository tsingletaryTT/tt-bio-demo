"""scripts/hw-run.sh: the one way to run anything that opens a chip.

Every test stubs the PCI tree, the device nodes, gozer and fuser, so no chip is touched."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hw-run.sh"
BDFS = ["0000:01:00.0", "0000:02:00.0", "0000:03:00.0", "0000:04:00.0"]


def _exe(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def box(tmp_path):
    pci = tmp_path / "pci"
    for b in BDFS:
        (pci / b).mkdir(parents=True)
        (pci / b / "vendor").write_text("0x1e52\n")
    (pci / "0000:00:1f.0").mkdir()
    (pci / "0000:00:1f.0" / "vendor").write_text("0x8086\n")      # not a Tenstorrent card
    dev = tmp_path / "dev"
    dev.mkdir()
    for i in range(4):
        (dev / str(i)).write_text("")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _exe(bin_ / "fuser", "exit 1\n")                 # fuser -s fails: nobody holds the node (idle)
    marker = tmp_path / "ran.txt"
    # The command under test: records what it saw instead of opening anything.
    cmd = tmp_path / "cmd.sh"
    _exe(cmd, f'echo "ran TV=${{TT_VISIBLE_DEVICES:-unset}} args=$*" >> {marker}\n')
    # A PATH holding only the basic tools the script needs, so a missing `fuser` or
    # `gozer` on the real machine cannot leak into a test.
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("bash", "tr", "sort", "basename", "sed", "cat", "env"):
        (tools / name).symlink_to(next(Path(d, name) for d in ("/usr/bin", "/bin") if Path(d, name).exists()))
    env = {"PATH": f"{bin_}:{tools}", "TT_HW_PCI_ROOT": str(pci),
           "TT_HW_DEV_ROOT": str(dev), "HOME": str(tmp_path)}
    return dict(bin=bin_, marker=marker, cmd=cmd, env=env, tmp=tmp_path)


def _run(box, *args, extra_env=None):
    env = dict(box["env"], **(extra_env or {}))
    return subprocess.run(["/bin/bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)


def _fake_gozer(box):
    """A gozer that logs its arguments, grants a two-chip board, and runs the command."""
    log = box["tmp"] / "gozer.txt"
    _exe(box["bin"] / "gozer", f'echo "$@" >> {log}\n'
         'shift; while [ "$1" != "--" ]; do shift; done; shift\n'
         'export TT_VISIBLE_DEVICES="0000:03:00.0,0000:04:00.0"\n'
         'exec "$@"\n')
    return log


def _test_sh(box):
    """A real executable named test.sh that records that it ran. If the guard lets it
    through, the marker file appears; if the guard refuses, it never does."""
    path = box["tmp"] / "test.sh"
    _exe(path, f'echo ran >> {box["marker"]}\n')
    return str(path)


def test_it_refuses_the_whole_suite_flag_without_every_chip(box):
    _fake_gozer(box)
    r = _run(box, "--chips", "1", "--reason", "x", "--", _test_sh(box), "--hw", "-q")
    assert r.returncode == 12, r.stderr
    assert "opens EVERY card" in r.stderr
    assert not box["marker"].exists()


def test_it_allows_the_whole_suite_flag_with_every_chip(box):
    _fake_gozer(box)
    _exe(box["tmp"] / "scripts_test.sh", f'echo ran >> {box["marker"]}\n')
    r = _run(box, "--chips", "4", "--reason", "x", "--", str(box["tmp"] / "scripts_test.sh"), "--hw")
    assert r.returncode == 0, r.stderr      # a path that does not look like test.sh passes the guard anyway
    assert box["marker"].read_text() == "ran\n"
    box["marker"].unlink()
    # and the real shape, named test.sh, is allowed at 4 chips and really runs:
    r = _run(box, "--chips", "4", "--reason", "x", "--", _test_sh(box), "--hw")
    assert r.returncode == 0, r.stderr
    assert box["marker"].read_text() == "ran\n"


def test_the_env_switch_refuses_test_sh_without_a_hw_argument(box):
    _fake_gozer(box)
    r = _run(box, "--chips", "1", "--reason", "x", "--", _test_sh(box),
             extra_env={"TT_BIO_DEMO_HW_TESTS": "1"})
    assert r.returncode == 12, r.stderr
    assert not box["marker"].exists()


@pytest.mark.parametrize("tail", [["--hw", "-q"], ["-q", "--hw", "-x"]])
def test_hw_in_a_non_final_position_is_still_refused(box, tail):
    _fake_gozer(box)
    r = _run(box, "--chips", "1", "--reason", "x", "--", _test_sh(box), *tail)
    assert r.returncode == 12, r.stderr
    assert not box["marker"].exists()


def test_arguments_with_spaces_arrive_as_separate_arguments(box):
    _exe(box["tmp"] / "count.sh", f'echo "n=$# first=$1 second=$2" >> {box["marker"]}\n')
    r = _run(box, "--chips", "1", "--reason", "x", "--", str(box["tmp"] / "count.sh"), "a", "b c")
    assert r.returncode == 0, r.stderr
    assert box["marker"].read_text() == "n=2 first=a second=b c\n"


def test_a_busy_chip_other_than_the_pinned_one_still_stops_the_run(box):
    # Only node 3 is held. The run pins chip 0 alone, but chip 0 may share a board
    # with a sibling, so any busy card on the box must refuse it without gozer.
    _exe(box["bin"] / "fuser", 'case "$*" in *"/3") exit 0 ;; esac\nexit 1\n')
    r = _run(box, "--chips", "1", "--one-chip", "--reason", "x", "--", str(box["cmd"]))
    assert r.returncode == 11, r.stderr
    assert not box["marker"].exists()


def test_assume_idle_lets_a_run_proceed_with_no_fuser(box):
    (box["bin"] / "fuser").unlink()
    r = _run(box, "--chips", "1", "--reason", "x", "--", str(box["cmd"]),
             extra_env={"HW_RUN_ASSUME_IDLE": "1"})
    assert r.returncode == 0, r.stderr
    assert "TV=0000:01:00.0 " in box["marker"].read_text()


def test_help_prints_only_the_usage_lines(box):
    r = _run(box, "--help")
    assert r.returncode == 0, r.stderr
    assert "scripts/hw-run.sh --chips N" in r.stdout
    assert "set -euo" not in r.stdout


def test_with_gozer_it_takes_a_lease_and_narrows_to_one_chip_on_request(box):
    log = _fake_gozer(box)
    r = _run(box, "--chips", "1", "--one-chip", "--reason", "why not", "--", str(box["cmd"]), "a")
    assert r.returncode == 0, r.stderr
    assert "--chips 1" in log.read_text() and "why not" in log.read_text()
    assert "TV=0000:03:00.0 " in box["marker"].read_text()          # first BDF of the granted board only


def test_with_gozer_and_no_narrowing_the_command_sees_the_whole_grant(box):
    _fake_gozer(box)
    _run(box, "--chips", "1", "--reason", "x", "--", str(box["cmd"]))
    assert "TV=0000:03:00.0,0000:04:00.0 " in box["marker"].read_text()


def test_without_gozer_it_pins_the_first_n_chips_and_says_nothing_is_fenced(box):
    r = _run(box, "--chips", "2", "--reason", "x", "--", str(box["cmd"]))
    assert r.returncode == 0, r.stderr
    assert "TV=0000:01:00.0,0000:02:00.0 " in box["marker"].read_text()
    assert "NOT leased" in r.stderr


def test_without_gozer_one_chip_pins_exactly_one(box):
    _run(box, "--chips", "2", "--one-chip", "--reason", "x", "--", str(box["cmd"]))
    assert "TV=0000:01:00.0 " in box["marker"].read_text()


def test_without_gozer_a_busy_chip_stops_the_run(box):
    _exe(box["bin"] / "fuser", "exit 0\n")           # fuser -s succeeds: somebody holds the node
    r = _run(box, "--chips", "1", "--reason", "x", "--", str(box["cmd"]))
    assert r.returncode == 11, r.stderr
    assert not box["marker"].exists()


def test_without_gozer_or_fuser_it_will_not_guess(box):
    (box["bin"] / "fuser").unlink()                   # leave a PATH with neither gozer nor fuser
    r = _run(box, "--chips", "1", "--reason", "x", "--", "/bin/true")
    assert r.returncode == 11, r.stderr


@pytest.mark.parametrize("args", [
    ["--reason", "x", "--", "true"],                  # no --chips
    ["--chips", "1", "--", "true"],                   # no --reason
    ["--chips", "1", "--reason", "x"],                # no command
    ["--chips", "9", "--reason", "x", "--", "true"],  # more chips than the box has
])
def test_usage_errors_exit_2(box, args):
    assert _run(box, *args).returncode == 2
