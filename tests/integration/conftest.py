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

from runner.env import runner_environ, single_visible_device

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
    """One chip, visible, addressable as logical device 0.

    Narrowing is load-bearing since tt-bio 0.6.3, not tidiness. `get_device()`
    now forces a 1x1 P300 mesh-graph descriptor (see
    runner.env.single_visible_device for the full mechanism), so opening a
    device in THIS process while a whole p300c board pair is visible dies with

        TT_FATAL @ tt_metal/fabric/control_plane.cpp:1262
        Physical chip id 0 not found in control plane chip mapping.

    A gozer lease is exactly that case: asking for one chip on a p300c grants
    the pair, because visibility cannot fence a board. The booth itself is
    unaffected -- runner/workers.py pins each worker to one chip and the daemon
    opens nothing -- so this fixture is the in-process equivalent of the rule
    production already follows, not a workaround for a booth defect.

    An unset TT_VISIBLE_DEVICES means EVERY chip is visible, which is the
    broken case and not the safe one, so it is narrowed to the first card on
    the box rather than left alone.

    Session-scoped and set before any test opens a device: TT_VISIBLE_DEVICES
    is only read when ttnn is imported, so a later assignment is a no-op. Any
    test that must see several chips (tests/integration/test_four_workers.py)
    depends on `tt_cards_present` instead, and scripts/test.sh already runs it
    in its own pytest process.
    """
    chosen = single_visible_device(os.environ.get("TT_VISIBLE_DEVICES"))
    if chosen is None:
        bdfs = _tt_bdfs()
        if not bdfs:
            pytest.skip(
                "Tenstorrent cards are present but none could be enumerated "
                "from sysfs, so this process cannot narrow TT_VISIBLE_DEVICES "
                "to one chip -- which tt-bio 0.6.3 requires to open a device")
        chosen = bdfs[0]
    os.environ["TT_VISIBLE_DEVICES"] = chosen
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
