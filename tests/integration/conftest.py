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


def _tt_bdfs(root="/sys/bus/pci/devices"):
    """Every Tenstorrent PCI BDF on this box, in stable sysfs order."""
    out = []
    try:
        entries = sorted(pathlib.Path(root).iterdir())
    except OSError:
        return out
    for entry in entries:
        try:
            if (entry / "vendor").read_text().strip().lower() == TT_VENDOR_ID:
                out.append(entry.name)
        except OSError:
            continue
    return out


@pytest.fixture(scope="session")
def tt_cards_present():
    """Skip unless this box has Tenstorrent cards. Says nothing about how many
    are visible -- use `tt_device` for anything opening one in this process."""
    count = _physical_card_count()
    if count == 0:
        pytest.skip("no Tenstorrent cards present on this machine")
    return count


@pytest.fixture(scope="session")
def tt_device(tt_cards_present):
    """The logical device these tests open. Always 0.

    This fixture used to narrow TT_VISIBLE_DEVICES to a single BDF, because
    tt-bio 0.6.3 forced a 1x1 P300 mesh-graph descriptor and refused to open a
    device at all while a whole p300c board pair was visible:

        TT_FATAL @ tt_metal/fabric/control_plane.cpp:1262
        Physical chip id 0 not found in control plane chip mapping.

    A gozer lease is exactly that case -- asking for one chip on a p300c grants
    the pair, because visibility cannot fence a board -- so every in-process
    test needed narrowing to run under a lease at all.

    tt-bio 0.6.4 applies the 1x1 descriptor only when exactly one chip IS
    visible, so a pair opens as a mesh again (upstream #11) and the narrowing
    is retired. Nothing replaced it: with the pair visible, logical device 0
    already resolves to the first visible chip, which is the first chip the
    lease granted -- so the narrowing was buying determinism it did not
    actually add. `./scripts/test.sh --hw` on 0.6.4 is what proves it.
    """
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
