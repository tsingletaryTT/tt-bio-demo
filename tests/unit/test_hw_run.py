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


def test_it_refuses_the_whole_suite_flag_without_every_chip(box):
    _fake_gozer(box)
    r = _run(box, "--chips", "1", "--reason", "x", "--", "scripts/test.sh", "--hw", "-q")
    assert r.returncode == 12, r.stderr
    assert "opens EVERY card" in r.stderr
    assert not box["marker"].exists()


def test_it_allows_the_whole_suite_flag_with_every_chip(box):
    _fake_gozer(box)
    _exe(box["tmp"] / "scripts_test.sh", f'echo ran >> {box["marker"]}\n')
    r = _run(box, "--chips", "4", "--reason", "x", "--", str(box["tmp"] / "scripts_test.sh"), "--hw")
    assert r.returncode == 0, r.stderr      # a path that does not look like test.sh passes the guard anyway
    # and the real shape, named test.sh, is allowed at 4 chips:
    _exe(box["tmp"] / "test.sh", f'echo ran >> {box["marker"]}\n')
    r = _run(box, "--chips", "4", "--reason", "x", "--", str(box["tmp"] / "test.sh"), "--hw")
    assert r.returncode == 0, r.stderr


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
