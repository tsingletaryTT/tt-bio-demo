"""The turnkey launcher: one playlist, both processes.

The defect this file exists to stop coming back (whole-branch review,
Critical 1): `scripts/run-demo.sh` gave the daemon a directory holding one
fold input and gave the UI nothing at all, so the UI fell back to the full
four-target `playlist/manifest.yaml`. The booth then advertised FKBP12,
DHFR and Trypsin -- each stamped with a real, measured fold time -- over a
daemon with no input file for any of them. A visitor tapping "Trypsin ·
~74.9s to fold" got a 20-residue Trp-cage four seconds later.

Nothing here needs GTK, a daemon, a socket, or a card. The launcher is run
for real, against a PREFIX of two stub interpreters (`TT_BIO_DEMO_PREFIX`,
the same override scripts/test.sh already has) that record their argv and
exit -- except for `-m ui.playlist`, which is delegated to the real
interpreter because that is the launcher's own manifest parser and stubbing
it out would test nothing.

The load-bearing assertion is `_ui_target_ids(...) == _daemon_target_ids(...)`:
it resolves what the gallery would actually show (by running the UI's own
manifest loader over the UI's own arguments) and compares it to what the
daemon could actually fold (the .yaml stems in the directory it was given).
That is the invariant, not "both command lines mention a playlist" -- which
the broken version would have satisfied.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ui.playlist import load_playlist, select_targets

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUN_DEMO = REPO_ROOT / "scripts" / "run-demo.sh"
MANIFEST = REPO_ROOT / "playlist" / "manifest.yaml"


def _write_stub(path, argv_log, real_python, *, sentinel, wait_for_sentinel):
    """One fake venv interpreter: record argv, then exit 0.

    `-m ui.playlist` is the exception and is delegated to `real_python`:
    that invocation is how the launcher expands the manifest into the
    daemon's fold inputs, so stubbing it would leave the very thing under
    test unexercised. Everything else (`-m runner.daemon`, `-m ui.app`)
    must NOT run -- one opens a Tenstorrent device, the other a window.

    The sentinel is what makes this test deterministic rather than racy.
    run-demo.sh backgrounds the daemon and then runs the UI in the
    foreground; when the UI exits, its EXIT trap SIGTERMs the daemon. A stub
    daemon that has not finished writing its argv by then gets killed
    mid-write -- observed as a truncated argv log. So the daemon stub drops
    a sentinel once its record is safely on disk, and the UI stub waits
    (bounded) for that sentinel before returning, which is also the moment
    the launcher is genuinely finished doing everything under test.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    wait = ""
    if wait_for_sentinel:
        wait = (
            'if [[ "${1:-}" == "-m" && "${2:-}" == "ui.app" ]]; then\n'
            "  for _ in $(seq 1 200); do\n"
            f'    [[ -e {sentinel!s} ]] && break\n'
            "    sleep 0.025\n"
            "  done\n"
            "fi\n"
        )
    mark = ""
    if not wait_for_sentinel:
        mark = ('if [[ "${1:-}" == "-m" && "${2:-}" == "runner.daemon" ]]; then\n'
                f"  : > {sentinel!s}\n"
                "fi\n")
    path.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "-m" && "${2:-}" == "ui.playlist" ]]; then\n'
        f'  exec {real_python!s} "$@"\n'
        "fi\n"
        f'printf "%s\\n" "$@" >> {argv_log!s}\n'
        + mark + wait +
        "exit 0\n"
    )
    path.chmod(0o755)


def _launch(tmp_path, *args, expect_ok=True):
    """Run the real launcher with stub interpreters. Returns (proc, runtime).

    `expect_ok` asserts the launch succeeded right here, with the script's
    own stderr in the message -- so a launcher that fell over is reported as
    that, rather than as a confusing missing-argv-file error several
    assertions later.
    """
    prefix = tmp_path / "prefix"
    runtime = tmp_path / "xdg"
    runtime.mkdir(parents=True, exist_ok=True)
    sentinel = tmp_path / "daemon.started"
    # Each launch starts from a clean slate, so `_argv` always describes THIS
    # launch -- test_a_target_dropped_between_runs launches twice on purpose.
    for stale in (sentinel, tmp_path / "venv-runner.argv", tmp_path / "venv-ui.argv"):
        stale.unlink(missing_ok=True)
    for venv in ("venv-runner", "venv-ui"):
        _write_stub(prefix / venv / "bin" / "python3",
                    tmp_path / f"{venv}.argv", Path(sys.executable),
                    sentinel=sentinel, wait_for_sentinel=(venv == "venv-ui"))

    env = dict(os.environ)
    env.update({
        "TT_BIO_DEMO_PREFIX": str(prefix),
        "XDG_RUNTIME_DIR": str(runtime),
        # Nothing may leak in from the developer's own shell: these are
        # exactly the knobs under test.
        "TT_BIO_DEMO_PLAYLIST": "",
        "TT_BIO_DEMO_TARGETS": "",
        "TT_BIO_DEMO_ALL_TARGETS": "0",
    })
    # An empty TT_BIO_DEMO_PLAYLIST/TARGETS would defeat the script's own
    # `${VAR:-default}`, which is what we want for PLAYLIST (fall back to
    # the repo manifest) but not something to rely on accidentally -- drop
    # them entirely instead.
    del env["TT_BIO_DEMO_PLAYLIST"], env["TT_BIO_DEMO_TARGETS"]

    proc = subprocess.run(["bash", str(RUN_DEMO), *args], env=env,
                          capture_output=True, text=True, timeout=120)
    if expect_ok:
        assert proc.returncode == 0, (
            f"run-demo.sh {' '.join(args)} exited {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc, tmp_path


def _argv(tmp_path, which):
    """The recorded argv of the last `which` ('venv-ui'/'venv-runner') run
    that was NOT the manifest expansion."""
    lines = (tmp_path / f"{which}.argv").read_text().splitlines()
    # Each invocation starts at a "-m"; split the log back into runs.
    runs, current = [], []
    for line in lines:
        if line == "-m" and current:
            runs.append(current)
            current = []
        current.append(line)
    if current:
        runs.append(current)
    real = [run for run in runs if run[:2] != ["-m", "ui.playlist"]]
    assert real, f"{which} was never invoked for anything but ui.playlist"
    return real[-1]


def _flag(argv, name):
    assert name in argv, f"{name} missing from {argv!r}"
    return argv[argv.index(name) + 1]


def _ui_target_ids(tmp_path):
    """What the gallery would actually show, resolved the way ui/app.py
    resolves it: its own --playlist and --targets through its own loader."""
    argv = _argv(tmp_path, "venv-ui")
    assert argv[:2] == ["-m", "ui.app"]
    raw = _flag(argv, "--targets")
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    targets = select_targets(load_playlist(_flag(argv, "--playlist")), ids)
    return sorted(target.id for target in targets)


def _daemon_target_ids(tmp_path):
    """What the daemon could actually fold: runner/daemon.py globs `*.yaml`
    in the directory it is given and takes each file's stem as target_id."""
    argv = _argv(tmp_path, "venv-runner")
    assert argv[:2] == ["-m", "runner.daemon"]
    playlist_dir = Path(_flag(argv, "--playlist"))
    return sorted(path.stem for path in playlist_dir.glob("*.yaml"))


def test_the_ui_and_the_daemon_are_driven_from_the_same_playlist(tmp_path):
    """The headline invariant: every target the gallery offers is a target
    the daemon has an input file for, and vice versa."""
    proc, _ = _launch(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _ui_target_ids(tmp_path) == _daemon_target_ids(tmp_path)


def test_the_default_playlist_is_everything_the_booth_can_do(tmp_path):
    """The default shows the whole manifest, not one safe target.

    It was Trp-cage alone while the longer targets were unvalidated over the
    socket. That validation happened -- a 320s live run, 21 folds across all
    four, zero client drops -- so the conservative default was costing a
    visitor three of the four proteins for no remaining reason. A full cycle
    is ~58s. Use `--targets trpcage` to fold one target while iterating."""
    _launch(tmp_path)
    every_id = sorted(target.id for target in load_playlist(MANIFEST))
    assert _ui_target_ids(tmp_path) == every_id
    assert _daemon_target_ids(tmp_path) == every_id
    assert len(every_id) > 1, "the manifest should ship more than one target"


def test_all_targets_opens_the_whole_manifest_to_both_processes(tmp_path):
    proc, _ = _launch(tmp_path, "--all-targets")
    assert proc.returncode == 0, proc.stderr
    every_id = sorted(target.id for target in load_playlist(MANIFEST))
    assert _ui_target_ids(tmp_path) == every_id
    assert _daemon_target_ids(tmp_path) == every_id


def test_an_explicit_target_selection_reaches_both_processes(tmp_path):
    proc, _ = _launch(tmp_path, "--targets", "trpcage,dhfr")
    assert proc.returncode == 0, proc.stderr
    assert _ui_target_ids(tmp_path) == ["dhfr", "trpcage"]
    assert _daemon_target_ids(tmp_path) == ["dhfr", "trpcage"]


def test_a_target_dropped_between_runs_stops_being_folded(tmp_path):
    """The daemon's directory is rebuilt, not added to: a leftover symlink
    from a wider previous run would fold something the gallery no longer
    offers -- the same defect, one run later."""
    _launch(tmp_path, "--all-targets")
    assert len(_daemon_target_ids(tmp_path)) > 1
    _launch(tmp_path, "--targets", "trpcage")
    assert _daemon_target_ids(tmp_path) == ["trpcage"]
    assert _ui_target_ids(tmp_path) == ["trpcage"]


def test_a_typo_in_targets_stops_the_launch_rather_than_shipping_a_subset(tmp_path):
    """Loud, not lenient: silently dropping an unknown id is how a booth
    ends up advertising something nobody can fold."""
    proc, _ = _launch(tmp_path, "--targets", "trpcage,trpcaeg", expect_ok=False)
    assert proc.returncode != 0
    assert "trpcaeg" in proc.stderr
    assert not (tmp_path / "venv-runner.argv").exists(), (
        "the daemon must not be started at all when the playlist is bad")


def test_the_daemon_target_ids_are_the_manifest_ids(tmp_path):
    """Not the input FILE's stem: `examples/affinity_tryp.yaml` is folded as
    `trypsin`, so the daemon's own job_start (and the diagnostics line built
    from it) names the same thing the visitor was shown. The unfixed
    launcher printed "visitor picked trypsin" directly above "▶ fold
    affinity_tryp".
    """
    _launch(tmp_path, "--all-targets")
    argv = _argv(tmp_path, "venv-runner")
    playlist_dir = Path(_flag(argv, "--playlist"))
    for link in playlist_dir.glob("*.yaml"):
        assert link.is_symlink()
        assert link.resolve().is_file(), f"{link} points nowhere"
    assert "trypsin" in _daemon_target_ids(tmp_path)


@pytest.mark.parametrize("flag", ["--playlist", "--targets"])
def test_the_ui_is_never_launched_without_its_share_of_the_playlist(tmp_path, flag):
    """The mutation this file was written against: dropping either flag from
    the `ui.app` invocation puts the UI back on its own `_DEFAULT_PLAYLIST`
    fallback, which is the whole bug."""
    _launch(tmp_path)
    assert flag in _argv(tmp_path, "venv-ui")
