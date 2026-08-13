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

import sys

import pytest


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
