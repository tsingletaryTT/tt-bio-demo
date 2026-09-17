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
    # Alongside argv, snapshot the two weights-cache variables as this stub
    # process actually sees them -- not what run-demo.sh's own argv claims,
    # but what the daemon/UI process it launches actually INHERITS. This is
    # what lets a test check the folding WORKERS' side of the invariant
    # (they read these via runner/env.py's runner_environ, not via argv at
    # all), which is exactly the blind spot that let the packaged-mode
    # regression through test_the_weights_flag_still_overrides_everything --
    # that test only ever looked at argv.
    env_log = argv_log.with_suffix(".env")
    capture_env = (
        f'{{ printf "TT_BIO_CACHE=%s\\n" "${{TT_BIO_CACHE:-}}"; '
        f'printf "BOLTZ_CACHE=%s\\n" "${{BOLTZ_CACHE:-}}"; '
        # Also captured here (not just for the UI stub) so
        # test_the_restart_loop_exports_the_run_demo_sh_env_var_to_the_ui can
        # read back what the UI process actually INHERITED, the same "argv
        # claims vs. real environment" distinction the two lines above exist
        # for -- see ui/app.py's RUN_DEMO_SH_ENV_VAR.
        f'printf "TT_BIO_DEMO_RUN_DEMO_SH=%s\\n" "${{TT_BIO_DEMO_RUN_DEMO_SH:-}}"; '
        f'}} >> {env_log!s}\n'
    )
    path.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "-m" && "${2:-}" == "ui.playlist" ]]; then\n'
        f'  exec {real_python!s} "$@"\n'
        "fi\n"
        f'printf "%s\\n" "$@" >> {argv_log!s}\n'
        + capture_env
        + mark + wait +
        "exit 0\n"
    )
    path.chmod(0o755)


def _launch(tmp_path, *args, expect_ok=True, env_overrides=None, script=None):
    """Run the real launcher with stub interpreters. Returns (proc, runtime).

    `expect_ok` asserts the launch succeeded right here, with the script's
    own stderr in the message -- so a launcher that fell over is reported as
    that, rather than as a confusing missing-argv-file error several
    assertions later.

    `env_overrides` is applied AFTER the scrubbing below, which is the only
    order that works: this harness deliberately deletes the launcher's env
    knobs so nothing leaks in from the developer's shell, and a test that is
    specifically about one of those knobs has to be able to put it back.

    `script` overrides which copy of run-demo.sh is actually invoked --
    defaults to RUN_DEMO (this repo's own). test_run_demo_sh_packaged_
    weights.py-style tests pass a path under a fake, .git/tests-less
    directory so run-demo.sh's own $REPO_ROOT (computed from its own
    location, not from TT_BIO_DEMO_PREFIX -- see run-demo.sh's own comment
    on why those are different questions) resolves to something
    tt_bio_demo_install_mode calls "package".
    """
    prefix = tmp_path / "prefix"
    runtime = tmp_path / "xdg"
    runtime.mkdir(parents=True, exist_ok=True)
    sentinel = tmp_path / "daemon.started"
    # Each launch starts from a clean slate, so `_argv` always describes THIS
    # launch -- test_a_target_dropped_between_runs launches twice on purpose.
    for stale in (sentinel, tmp_path / "venv-runner.argv", tmp_path / "venv-ui.argv",
                  tmp_path / "venv-runner.env", tmp_path / "venv-ui.env"):
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
    # TT_BIO_DEMO_DEVICES is scrubbed for the same reason and, unlike the two
    # above, must not merely be emptied: an empty value is itself one of the
    # cases under test (see test_no_chip_selection_means_no_flag_at_all).
    env.pop("TT_BIO_DEMO_DEVICES", None)
    # Likewise the weights-cache variables: a developer's own shell easily
    # has $TT_BIO_CACHE or $BOLTZ_CACHE set (this project's own docs tell you
    # to), and that would silently defeat exactly what the packaged-vs-source
    # weights tests below are checking.
    env.pop("TT_BIO_CACHE", None)
    env.pop("BOLTZ_CACHE", None)
    env.pop("TT_BIO_DEMO_WEIGHTS", None)
    env.update(env_overrides or {})

    proc = subprocess.run(["bash", str(script or RUN_DEMO), *args], env=env,
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


def _stub_env(tmp_path, which, name):
    """The named variable as the `which` ('venv-ui'/'venv-runner') stub
    process actually saw it -- i.e. what the real daemon/UI process would
    have INHERITED, as opposed to what run-demo.sh's own argv claims it
    resolved. See `_write_stub`'s capture_env for why this exists."""
    log = tmp_path / f"{which}.env"
    if not log.exists():
        return None
    for line in log.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line[len(name) + 1:]
    return None


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


# --- --devices (Task 18) ----------------------------------------------------
# The daemon has had a `--devices` flag since Task 8 and the launcher had no
# way to reach it, so the only way to run the booth on a subset of a shared
# machine's chips was to bypass run-demo.sh and start both processes by hand.


def test_the_chip_selection_reaches_the_daemon(tmp_path):
    _launch(tmp_path, "--devices", "0,2", "--targets", "trpcage")
    assert _flag(_argv(tmp_path, "venv-runner"), "--devices") == "0,2"


def test_the_chip_selection_can_come_from_the_environment(tmp_path):
    """Every other knob in this launcher has an env-var twin (see its header);
    a flag that only works as a flag is one systemd's own unit file cannot
    set."""
    _launch(tmp_path, "--targets", "trpcage",
            env_overrides={"TT_BIO_DEMO_DEVICES": "1,3"})
    assert _flag(_argv(tmp_path, "venv-runner"), "--devices") == "1,3"


def test_no_chip_selection_means_no_flag_at_all_not_an_empty_one(tmp_path):
    """The mutation this exists to fail against: appending `--devices
    "$DEVICES"` unconditionally.

    That is not a cosmetic difference. `--devices ""` reaches the daemon as an
    explicit selection and goes straight through to
    `tt_bio.runtime.detect_tenstorrent_devices` (runner/daemon.py passes it
    UNVALIDATED, on purpose), where an empty selection is a different request
    from the default `None` -- "every detected chip". A booth that quietly
    asked for no chips would come up serving `not_ready` forever, which looks
    exactly like a driver problem.
    """
    _launch(tmp_path, "--targets", "trpcage")
    assert "--devices" not in _argv(tmp_path, "venv-runner")


def test_the_ui_is_not_told_which_chips_to_expect(tmp_path):
    """The UI learns the booth's inventory from `hello`, never from argv.

    A `--devices 0,1` echoed into `ui.app` would be a second source of truth
    for "which chips exist" -- and the wrong one: a chip that fails to come up,
    or that the pool retires mid-session (runner/pool.py's
    WORKER_RETIRE_AFTER), still appears on a command line and no longer
    appears in `hello`. The screen must show what the daemon has, not what the
    operator asked for.
    """
    _launch(tmp_path, "--devices", "0,2", "--targets", "trpcage")
    assert "--devices" not in _argv(tmp_path, "venv-ui")


# --- --questions -------------------------------------------------------------
# Opting a booth IN to the affinity-Q&A feature's permanent chip reservation
# (runner/workers.py's split_for_qa), forwarded straight to the daemon. The
# UI needs no flag at all here -- it already hides the whole feature on
# `qa_capable: false`, which is what a daemon started WITHOUT --questions
# (the default) reports.


def test_the_questions_flag_reaches_the_daemon(tmp_path):
    _launch(tmp_path, "--questions", "--targets", "trpcage")
    assert "--questions" in _argv(tmp_path, "venv-runner")


def test_without_the_flag_the_daemon_gets_no_questions_argument(tmp_path):
    """The daemon's own default (never reserve a chip for Q&A) is what a
    plain run-demo.sh invocation must still get -- appending the flag
    unconditionally would silently opt every booth in and cost it 25% of its
    fold throughput on a 4-chip box."""
    _launch(tmp_path, "--targets", "trpcage")
    assert "--questions" not in _argv(tmp_path, "venv-runner")


def test_the_questions_flag_is_not_echoed_to_the_ui(tmp_path):
    """Not a UI concern at all: ui/app.py's gate is `qa_capable` from
    `hello`, never a command-line flag of its own."""
    _launch(tmp_path, "--questions", "--targets", "trpcage")
    assert "--questions" not in _argv(tmp_path, "venv-ui")


# --- the Ctrl+A restart loop -------------------------------------------------
#
# ui/app.py's `_request_qa_restart` (Ctrl+A) exits the UI with
# `QUESTIONS_RESTART_EXIT_CODE` when the operator asks, live, to enable Q&A.
# This script is what turns that sentinel into an actual restart: tear down
# the OLD daemon, then re-exec itself with --questions added. The two stub
# interpreters below are bespoke (not `_write_stub`/`_launch`) because this
# is the one behavior in the file that needs the UI to exit NONZERO and the
# whole script to run to completion TWICE in one process tree.


def _write_restart_daemon_stub(path, argv_log):
    """Records one line per launch (`$*`, space-joined -- simpler to split
    back into two runs than `_write_stub`'s one-argument-per-line log) and
    exits immediately. There is nothing to synchronize with: `cleanup` only
    needs the argv already flushed to disk, which a plain `printf` guarantees
    before this script's next line runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {argv_log!s}\n'
    )
    path.chmod(0o755)


def _write_restart_ui_stub(path, argv_log, real_python, restart_marker):
    """`-m ui.playlist` is delegated to the real interpreter, same as
    `_write_stub` -- run-demo.sh's own manifest-expansion step has to
    actually run or nothing downstream gets a playlist directory. `-m ui.app`
    exits with `QUESTIONS_RESTART_EXIT_CODE` exactly ONCE (`restart_marker`)
    and 0 on every launch after that.

    The "exactly once" shape mirrors the real UI, not an arbitrary
    simplification: `_request_qa_restart` refuses to produce the sentinel
    once `qa_capable` is true, and a real restart's daemon reports
    `qa_capable=True` from that point on -- so the real mechanism this test
    is modeling can also only ever fire once per booth-up. A stub that
    always returned the sentinel would spin run-demo.sh's restart loop
    forever and this test would time out instead of failing cleanly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "-m" && "${2:-}" == "ui.playlist" ]]; then\n'
        f'  exec {real_python!s} "$@"\n'
        "fi\n"
        f'printf "%s\\n" "$@" >> {argv_log!s}\n'
        f'if [[ -e {restart_marker!s} ]]; then\n'
        "  exit 0\n"
        "fi\n"
        f': > {restart_marker!s}\n'
        "exit 42\n"
    )
    path.chmod(0o755)


def test_the_restart_loop_relaunches_once_with_questions_added(tmp_path):
    """End to end: the UI's sentinel exit code (42, matching
    `QUESTIONS_RESTART_EXIT_CODE` in ui/app.py) makes this script tear down
    the first daemon and re-exec itself with --questions -- never a second
    daemon launched alongside a still-live first one, and never a loop that
    keeps re-adding --questions.

    Also the regression test for the Critical bug the review round found:
    the argument-parsing `while` loop `shift`s every positional argument
    away, so by the time execution reached the restart branch, `"$@"` was
    ALWAYS empty and `exec "$0" "$@" --questions` silently discarded every
    flag the operator originally passed -- `--devices` being the sharpest
    edge, since a restarted daemon that forgot it would claim EVERY
    detected chip on a shared machine. This launches with `--devices 0,1`
    (a flag with no env-var fallback, so it has no other way to survive a
    restart) and asserts it is still there on the SECOND daemon launch.
    """
    prefix = tmp_path / "prefix"
    runtime = tmp_path / "xdg"
    runtime.mkdir(parents=True, exist_ok=True)
    daemon_argv_log = tmp_path / "daemon.argv"
    ui_argv_log = tmp_path / "ui.argv"
    restart_marker = tmp_path / "ui.restarted-once"

    _write_restart_daemon_stub(prefix / "venv-runner" / "bin" / "python3",
                               daemon_argv_log)
    _write_restart_ui_stub(prefix / "venv-ui" / "bin" / "python3", ui_argv_log,
                           Path(sys.executable), restart_marker)

    env = dict(os.environ)
    env.update({
        "TT_BIO_DEMO_PREFIX": str(prefix),
        "XDG_RUNTIME_DIR": str(runtime),
        "TT_BIO_DEMO_TARGETS": "trpcage",
    })
    for var in ("TT_BIO_DEMO_PLAYLIST", "TT_BIO_DEMO_DEVICES", "TT_BIO_CACHE",
                "BOLTZ_CACHE", "TT_BIO_DEMO_WEIGHTS", "TT_BIO_DEMO_ALL_TARGETS"):
        env.pop(var, None)

    proc = subprocess.run(["bash", str(RUN_DEMO), "--devices", "0,1"], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"restart loop did not end cleanly: {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

    daemon_runs = [line.split() for line in
                   daemon_argv_log.read_text().splitlines()]
    assert len(daemon_runs) == 2, (
        "expected exactly two daemon launches (before and after the "
        f"restart), got {len(daemon_runs)}: {daemon_runs!r}")
    assert "--questions" not in daemon_runs[0], (
        "the FIRST daemon launch must not already carry --questions -- "
        "that would mean the restart fired before Ctrl+A")
    assert "--questions" in daemon_runs[1], (
        "the SECOND daemon launch (after the restart) must carry --questions")

    for which, run in (("first", daemon_runs[0]), ("second", daemon_runs[1])):
        assert "--devices" in run, (
            f"the {which} daemon launch lost --devices entirely: {run!r}")
        assert run[run.index("--devices") + 1] == "0,1", (
            f"the {which} daemon launch's --devices value drifted: {run!r}")


def test_the_restart_loop_exports_the_run_demo_sh_env_var_to_the_ui(tmp_path):
    """ui/app.py's `_request_qa_restart` only trusts the sentinel exit path
    when `RUN_DEMO_SH_ENV_VAR` is present in its own environment -- this is
    what lets it tell "run-demo.sh launched me" apart from a bare
    `python3 -m ui.app` or the packaged deployment, neither of which has a
    restart mechanism watching for the exit code at all."""
    _launch(tmp_path, "--targets", "trpcage")
    assert _stub_env(tmp_path, "venv-ui", "TT_BIO_DEMO_RUN_DEMO_SH") == "1"


# --- weights-cache resolution: source vs. packaged (Critical 1) ------------
#
# scripts/run-demo.sh is not a dev convenience: debian/com.tenstorrent.ttbio.
# demo.desktop's Exec= runs it directly, and INSTALL.md calls it "the normal
# path" for a packaged install. It used to always resolve the plain,
# $HOME-relative weights default (scripts/weights-cache.sh's
# tt_bio_demo_weights_cache), even when this checkout WAS a real
# /opt/tt-bio-demo tree -- so a packaged booth's postinst fetched weights to
# the fixed /opt/tt-bio-demo/weights while launching the booth via the
# desktop entry resolved the desktop user's own ~/.boltz instead. See
# docs/followups.md's "run-demo.sh resolved home-relative even from a
# packaged install" entry (FIXED).
#
# tt_bio_demo_install_mode ("source"/"package") is a .git/tests sniff test on
# the directory holding the script (scripts/weights-cache.sh's shared
# function; see also tests/unit/test_doctor.py's identical technique for
# scripts/doctor.sh). So a "packaged install" is simulated here by symlinking
# the real ui/protocol/playlist/examples trees (needed for `-m ui.playlist`
# to actually import something) and the real run-demo.sh/weights-cache.sh
# into a directory with neither -- never a directory this repo's own .git
# happens to be an ancestor of.


def _fake_packaged_tree(tmp_path):
    """A directory tt_bio_demo_install_mode reports "package" for, with just
    enough of the real tree (via symlinks, not copies -- so this always runs
    the ACTUAL current run-demo.sh/weights-cache.sh, not a frozen copy) for
    run-demo.sh to actually work end to end."""
    fake_root = tmp_path / "fake-opt-tt-bio-demo"
    (fake_root / "scripts").mkdir(parents=True)
    for name in ("run-demo.sh", "weights-cache.sh", "materialize-playlist.sh"):
        (fake_root / "scripts" / name).symlink_to(REPO_ROOT / "scripts" / name)
    for name in ("ui", "protocol", "playlist", "examples"):
        (fake_root / name).symlink_to(REPO_ROOT / name)
    return fake_root


def test_a_packaged_install_resolves_weights_to_the_fixed_path(tmp_path):
    """CRITICAL-1 FIX, the headline assertion: from a directory
    tt_bio_demo_install_mode calls "package", with neither $TT_BIO_CACHE nor
    $BOLTZ_CACHE set, run-demo.sh must hand the daemon the SAME fixed
    /opt/tt-bio-demo/weights the postinst populates and scripts/doctor.sh
    already diagnoses against -- not a $HOME-relative guess belonging to
    whoever's desktop session happens to launch the booth."""
    fake_root = _fake_packaged_tree(tmp_path)
    _launch(tmp_path, "--targets", "trpcage",
            script=fake_root / "scripts" / "run-demo.sh")
    weights = _flag(_argv(tmp_path, "venv-runner"), "--weights")
    assert weights == "/opt/tt-bio-demo/weights", (
        f"expected the packaged fixed cache, got {weights!r}")


def test_a_packaged_installs_default_still_pins_tt_bio_cache_for_the_workers(tmp_path):
    """Guards the OTHER branch of the deferred-resolution fix: with no
    explicit override, a packaged install must still export $TT_BIO_CACHE to
    the fixed path for the workers to inherit -- exactly as it did before
    the fix moved this decision to after argument parsing. Only the explicit
    branch changed; this pins the default branch didn't regress along with
    it."""
    fake_root = _fake_packaged_tree(tmp_path)
    _launch(tmp_path, "--targets", "trpcage",
            script=fake_root / "scripts" / "run-demo.sh")
    assert _stub_env(tmp_path, "venv-runner", "TT_BIO_CACHE") == "/opt/tt-bio-demo/weights"


def test_a_source_checkout_keeps_resolving_the_home_relative_default(tmp_path):
    """The fix is scoped to a packaged tree -- this repo (or ANY prefix
    tt_bio_demo_install_mode calls "source") must keep resolving today's
    $HOME-relative default unconditionally."""
    home = tmp_path / "somebody"
    home.mkdir()
    _launch(tmp_path, "--targets", "trpcage", env_overrides={"HOME": str(home)})
    weights = _flag(_argv(tmp_path, "venv-runner"), "--weights")
    assert weights == str(home / ".boltz"), weights


def test_a_packaged_installs_own_cache_override_still_wins(tmp_path):
    """This project's standing rule, restated for run-demo.sh: an operator
    who has already set $TT_BIO_CACHE (or $BOLTZ_CACHE) keeps that choice --
    the packaged pin only fills the gap when NEITHER is set."""
    fake_root = _fake_packaged_tree(tmp_path)
    moved = tmp_path / "operators-own-disk"
    _launch(tmp_path, "--targets", "trpcage",
            script=fake_root / "scripts" / "run-demo.sh",
            env_overrides={"TT_BIO_CACHE": str(moved)})
    weights = _flag(_argv(tmp_path, "venv-runner"), "--weights")
    assert weights == str(moved), weights


def test_the_weights_flag_still_overrides_everything(tmp_path):
    """--weights (and its env twin) is a separate, explicit escape hatch and
    must still win regardless of source-vs-package detection."""
    fake_root = _fake_packaged_tree(tmp_path)
    custom = tmp_path / "a-custom-cache"
    _launch(tmp_path, "--targets", "trpcage", "--weights", str(custom),
            script=fake_root / "scripts" / "run-demo.sh")
    weights = _flag(_argv(tmp_path, "venv-runner"), "--weights")
    assert weights == str(custom), weights


def test_an_explicit_weights_override_is_what_the_workers_actually_load_from(tmp_path):
    """CRITICAL-1's OWN fix (the packaged-mode pin, above) reintroduced this
    project's exact "a check that knows less than the thing it is checking"
    bug, one layer down: it exported $TT_BIO_CACHE to the fixed packaged path
    UNCONDITIONALLY, before argument parsing had even run -- so it did not
    yet know whether the operator was about to pass their own --weights.

    So on a packaged install, `run-demo.sh --weights /mnt/usb/weights` had
    preflight (runner/daemon.py's `run_preflight(args.weights, ...)`, which
    reads the daemon's own --weights argv directly) correctly approve
    /mnt/usb/weights, while the environment this script had ALREADY exported
    into carried $TT_BIO_CACHE=/opt/tt-bio-demo/weights -- and
    runner/env.py's runner_environ() only fills in $BOLTZ_CACHE from
    --weights when NEITHER $TT_BIO_CACHE NOR $BOLTZ_CACHE is already present.
    So the folding WORKERS resolved the packaged default instead, silently
    disagreeing with what preflight had just approved.

    The previous test above (test_the_weights_flag_still_overrides_
    everything) could not see this: it only ever asserts on the daemon's
    OWN argv, which was never wrong (args.weights always got /mnt/usb). The
    bug was entirely in what the WORKERS resolve, downstream of the
    environment -- so this asserts on that side directly, the same way the
    regression was actually verified by hand: capture what this stub
    process really inherited for $TT_BIO_CACHE/$BOLTZ_CACHE, then run the
    REAL runner_environ()/weights_cache() over it, exactly as the daemon's
    own worker processes would.
    """
    from runner.env import runner_environ, weights_cache

    fake_root = _fake_packaged_tree(tmp_path)
    custom = tmp_path / "a-custom-cache"
    _launch(tmp_path, "--targets", "trpcage", "--weights", str(custom),
            script=fake_root / "scripts" / "run-demo.sh")

    daemon_argv = _argv(tmp_path, "venv-runner")
    preflight_weights = _flag(daemon_argv, "--weights")
    assert preflight_weights == str(custom), preflight_weights

    # What the daemon process (and, via its own os.environ.update, every
    # worker it spawns) actually inherited for these two variables.
    captured = {
        "TT_BIO_CACHE": _stub_env(tmp_path, "venv-runner", "TT_BIO_CACHE"),
        "BOLTZ_CACHE": _stub_env(tmp_path, "venv-runner", "BOLTZ_CACHE"),
    }
    base = {k: v for k, v in captured.items() if v}

    # The exact call runner/daemon.py's main() makes: `os.environ.update(
    # runner_environ(args.log_root, weights_dir=args.weights))`. Feeding it
    # the captured environment (rather than the real os.environ) is what
    # makes this deterministic regardless of the test runner's own shell.
    worker_env = runner_environ(str(tmp_path / "logs"), base=base,
                                 weights_dir=preflight_weights)
    worker_weights = str(weights_cache(worker_env))

    assert worker_weights == str(custom), (
        "preflight approved a weights directory the folding workers do not "
        f"actually load from: preflight approved {preflight_weights!r}, "
        f"but the workers would resolve {worker_weights!r} "
        f"(captured environment: {captured!r})")
