"""Guards for the runner-side unit tests.

**No unit test in this directory may enumerate a real Tenstorrent device or
spawn a real worker process.** That is not a style rule; it is written here
because it was violated for real. While mutation-testing Task 8, a mutant that
made `Daemon.run()` ignore an already-assigned pool sent every `run()`-driving
test down the production path instead: `worker_specs()` enumerated
`/dev/tenstorrent`, a real `WorkerPool` was built, and four
`python3 -m runner.worker` children were spawned on a SHARED machine, outliving
the pytest process that started them. They had to be found with `pgrep` and
terminated by hand.

The fixture below closes that off at the source. `runner.daemon`'s two doors to
real hardware are replaced, for every test in this directory, with something
that raises loudly. A test that genuinely needs to observe what `run()` asks
for substitutes its own fakes over the top (see
`test_daemon_multichip._run_with_a_built_pool`), which is the same
`monkeypatch.setattr` and restores the same way.

This is deliberately a directory-level `conftest.py` rather than a fixture in
one module: the plan adds two more daemon test files (Tasks 10 and 11) that
will drive the same loop, and a guard they have to remember to import is a
guard that will be missed.
"""

import contextlib
import os
import sys

import pytest


@contextlib.contextmanager
def environ_snapshot():
    """Context manager half of `_restore_os_environ` below, pulled out as its
    own name so `test_daemon.py` can drive it directly and observe both
    sides of the restore (leak present while the context is open, gone once
    it closes) -- a test that only ran code wrapped in the real `autouse`
    fixture could never see the "before restore" half at all, since the
    fixture's own teardown would already have run by the time the test body
    could look. See `_restore_os_environ`'s docstring for why this exists.
    """
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _restore_os_environ():
    """`runner.daemon.main()` intentionally mutates the REAL process
    `os.environ` -- `os.environ.update(runner_environ(args.log_root,
    weights_dir=args.weights))`, unconditionally, before the preflight check
    even runs (see that line's own comment: "weights_dir pins --weights for
    the WORKERS too, not just preflight"). That is correct for an actual
    daemon process, which owns its own environment for its own lifetime.

    It is wrong for a test that calls the real `main()` in-process (several
    tests in `test_daemon.py` do, to exercise its CLI/exit-code contract) --
    `runner_environ`'s own `setdefault` discipline means the FIRST such test
    to run in a pytest session pins `TT_BIO_CACHE`/`BOLTZ_CACHE`/
    `TT_METAL_LOGS_PATH`/`TT_METAL_INSPECTOR` to ITS OWN now-deleted
    `tmp_path`, and every later test in the same process -- including, on a
    full `--hw` run, the real hardware integration tests -- inherits that
    stale cache path and fails with a confusing "weights missing, could not
    download" error instead of whatever it was actually testing. Found for
    real running the full `--hw` suite after the tt-bio 0.12.0 upgrade:
    `test_new_targets_timing.py`/`test_real_fold.py` failed entirely in that
    combined run and passed cleanly (60/60) run in isolation, which is what
    pointed at a leak rather than a real regression.

    Snapshotting and restoring the WHOLE environment, not just these four
    names, on purpose: a named-variable list is the same "a check that knows
    less than the thing it's protecting" shape this project's own history
    keeps finding -- a future `runner_environ` change adding a fifth variable
    would silently reopen this exact leak if the guard only knew about four.
    """
    with environ_snapshot():
        yield


@pytest.fixture(autouse=True)
def _no_real_devices_or_workers(monkeypatch):
    # Deliberately does NOT import runner.daemon itself: this fixture runs for
    # every test in the directory, most of which have nothing to do with the
    # daemon, and a conftest that drags a module into every one of them is a
    # conftest that decides their import graph for them. If some test module
    # here imported runner.daemon, pytest has already done so at collection
    # time and it is in sys.modules; if none did, there is nothing to guard.
    daemon = sys.modules.get("runner.daemon")
    if daemon is None:
        return

    class _TouchedRealHardware(BaseException):
        """Deliberately NOT an Exception subclass.

        `Daemon._build_pool` catches `Exception` broadly and retries -- which
        is right for a booth (a driver mid-reload must not kill the daemon)
        and exactly wrong for this guard, which would be swallowed and
        silently retried until the test's watchdog fired. A BaseException
        cannot be caught by that handler, so reaching real hardware fails the
        test at the line that did it, loudly, the way KeyboardInterrupt and
        SystemExit already pass through every `except Exception` here.
        """

    def _forbidden(*args, **kwargs):
        raise _TouchedRealHardware(
            "a unit test reached runner.daemon's real device/worker path. "
            "These tests must never enumerate /dev/tenstorrent or spawn a "
            "worker process -- substitute a fake pool (tests/unit/runner/"
            "_daemonfakes.py) or monkeypatch these two names yourself.")

    monkeypatch.setattr(daemon, "worker_specs", _forbidden)
    monkeypatch.setattr(daemon, "WorkerPool", _forbidden)
