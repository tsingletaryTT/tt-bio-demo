"""Skip hardware tests when no usable Tenstorrent device is present.

A packaging or CI machine with no cards is not a failure; a machine with cards
whose driver is not loaded is a different situation and should not be silently
treated as 'no cards' (see scripts/setup-venvs.sh, which makes the same
distinction for the installer).
"""

import os
import pathlib
import shutil
import tempfile

import pytest

from runner.env import runner_environ

TT_VENDOR_ID = "0x1e52"


def _physical_card_count():
    root = pathlib.Path("/sys/bus/pci/devices")
    if not root.is_dir():
        return 0
    count = 0
    for entry in root.iterdir():
        vendor = entry / "vendor"
        try:
            if vendor.read_text().strip().lower() == TT_VENDOR_ID:
                count += 1
        except OSError:
            continue
    return count


@pytest.fixture(scope="session")
def tt_device():
    if _physical_card_count() == 0:
        pytest.skip("no Tenstorrent cards present on this machine")
    return 0


@pytest.fixture(scope="session", autouse=True)
def _contained_tt_metal_logs():
    """Pin tt-metal's Inspector/Watcher output to a scratch dir outside this
    repo's working tree.

    Without TT_METAL_LOGS_PATH set, a fold's `generated/` tree lands relative
    to the process CWD -- the Phase 3a spike measured 121 MB for two 200-step
    folds (docs/spike-real-fold.md). runner/env.py's runner_environ() is the
    one place that knows the actual (surprising -- see that module's
    docstring) variable name; reused here rather than hand-rolled so this
    test session is contained the same way the daemon contains itself.
    Session-scoped and autouse so it is in effect before any test in this
    directory gets a chance to open a device, regardless of which test runs
    first.

    Removed on teardown: a scratch dir this fixture creates fresh every
    session and nothing else references once the session ends, so leaving
    it behind would just be the exact unbounded-growth-in-/tmp problem this
    fixture exists to keep tt-metal's own output out of (see runner/env.py's
    module docstring: 121 MB for two folds, with nothing upstream capping
    it) -- recreated one directory lower.
    """
    log_root = tempfile.mkdtemp(prefix="tt-bio-demo-test-logs-")
    os.environ.update(runner_environ(log_root))
    try:
        yield log_root
    finally:
        shutil.rmtree(log_root, ignore_errors=True)
