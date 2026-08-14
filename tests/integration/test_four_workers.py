"""The whole booth's compute half, on real silicon: four workers, four chips.

**WHAT THIS COSTS.** This module opens EVERY Tenstorrent card on the box --
not one, all of them -- and holds all of them for the length of a model load
plus one fold each (measured on this QB2: ~35 s wall for four p300c chips).
Nothing else on the machine can use any card while it runs. That is why it is
opt-in and why it must stay opt-in:

    ./scripts/test.sh --hw
    .venvs/venv-runner/bin/python3 -m pytest tests/integration -v -k four_workers

`./scripts/test.sh` with no flag does NOT run it (see that script's
"hardware opt-in" section), and `pytest.ini`'s `testpaths` deliberately
excludes this directory from a bare `pytest`.

**What it proves that no software test can.** Everything above `WorkerPool`
is already covered against fakes -- `tests/unit/runner/test_worker_pool.py`
drives the multiplexing, the death path and the respawn with no subprocess at
all. What that cannot reach is the one fact the entire multi-chip design rests
on: that four `python3 -m runner.worker` children, each handed its own
`TT_VISIBLE_DEVICES` by `runner.workers.worker_environ`, really do open four
DIFFERENT physical chips at the same time -- rather than all silently landing
on chip 0, which is the failure mode `WorkerSpec`'s docstring exists to warn
about and which every event on the wire would misreport identically.

The evidence for that here is `tt_bio.device_lease`, not a power reading.
Every device open in tt-bio goes through an exclusive `flock` on a per-card
lock file, so two processes CANNOT hold the same physical chip at once: the
second blocks for `TT_BIO_LEASE_TIMEOUT` (120 s) and then raises
`DeviceInUseError`. Four workers all reaching `worker.ready` inside this
test's timeout is therefore four distinct chips held concurrently, proven by
the kernel rather than inferred from telemetry.

That distinction is not academic. Task 18 measured this box's IDLE power over
80 chip-samples: 12-33 W, with 4 of the 80 reading over 30 W on a chip doing
nothing at all. The Phase 5 spike's headline evidence for correct pinning --
"33.0 W on chip 1 against 13-17 W idle on 0/2/3" -- is one sample sitting at
the top of that noise band, and proves nothing. A test built on the same signal
would have been unfailable for the same reason. (`aiclk` WOULD have worked:
800 MHz idle against 1281-1350 MHz folding, with no overlap at all across 488
chip-samples. It is not used here because the flock is stronger still -- it is
enforcement, not correlation.)

**The named mutation this test is built to fail against**, and which was run:
make every `WorkerSpec` pin the same chip --

    # runner/workers.py, worker_specs()
    visible_devices="0",          # instead of str(assignment["visible_devices"])

which is exactly "the booth silently folds everything on chip 0". Verified on
hardware, not reasoned about: one worker took the lease, the other three failed
with `physical card 0 ... is in use by pid:NNNN ... Refusing to open it
concurrently`, went `worker.fatal`, and this test failed on the readiness wait.

The second assertion cluster -- that `stop()` leaves nothing running -- was
verified the same way, against `WorkerPool.stop` mutated to skip both
`terminate()` and `kill()`: the four children outlived the pool, `_pids_alive`
named all four pids, and the test failed. (That mutation is why this module records real
pids and kills them itself in `four_workers`' teardown, rather than trusting
the object under test to have done it. A test that can only clean up when the
code it is testing already worked is a test that leaves strays behind on
exactly the run where it mattered -- and this project has already paid for six
of those once.)
"""

import os
import pathlib
import signal
import sys
import time

import pytest

from runner.pool import WorkerPool, _spawn_subprocess
from runner.workers import worker_specs

# The same vendored input tests/integration/test_real_fold.py folds, and for
# the same reason: an absolute path into a sibling checkout is a silent-skip
# hazard. 20 residues, ~4.4 s warm -- this test is about four chips being
# four chips, not about how long a protein takes.
INPUT = (pathlib.Path(__file__).resolve().parents[2]
         / "examples" / "trpcage_no_msa.yaml")
assert INPUT.is_file(), (
    f"vendored integration-test input is missing: {INPUT} -- this should be "
    "tracked in git; see tests/integration/test_real_fold.py's own note")

# How long every worker gets to open its chip and load protenix-v2 before this
# test gives up. The spike measured 3.1 s solo and 6.4-9.2 s under four-way
# contention; this test measures ~4.8 s and Task 18's live booth reached its
# first `hello` 6.0 s after the daemon started. 150 s is ~30x the worst of
# those -- enough headroom that a busy host is never the reason this goes red,
# and short enough that the one failure mode that CANNOT resolve itself (the
# UMD deadlock `_forbid_a_poisoned_process` describes) is a two-minute red
# instead of a five-minute one. A chip already leased by somebody else's job
# fails faster still: `tt_bio.device_lease`'s own 120 s DeviceInUseError
# reports itself as `worker.fatal`, inside this.
READY_TIMEOUT_S = 150.0

# Ditto for the folds, which run concurrently: all four must finish, and the
# slowest of four contending 20-residue folds measured ~9 s.
FOLD_TIMEOUT_S = 300.0

# How long `stop()` gets before this test starts naming pids. `WorkerPool`'s
# own WORKER_STOP_GRACE_S is 5 s and it escalates to SIGKILL after that, so
# anything past this is a pool that did not do its job.
STOP_TIMEOUT_S = 30.0


def _pids_alive(pids):
    """Which of `pids` still exist. The stray-worker check, spelled honestly.

    `os.kill(pid, 0)` rather than `handle.alive`: `alive` asks the pool's own
    `Popen` object, which is precisely the thing under test here, and a
    `Popen.poll()` that returns an exit status the kernel never produced is a
    bug this assertion would then agree with. `/proc` is an oracle the code
    under test does not own.
    """
    still_here = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:      # exists, owned by someone else -- exists
            pass
        still_here.append(pid)
    return still_here


class _Events:
    """Every protocol event the pool forwarded, with the card it came off.

    `on_event` is called from four reader threads at once (see
    `runner/pool.py`), so appends go under a lock -- `list.append` is atomic
    under CPython's GIL today, but this test would be a poor place to find out
    that stopped being true.
    """

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self.items = []

    def __call__(self, card, event):
        with self._lock:
            self.items.append((card, event))

    def of_type(self, kind):
        with self._lock:
            return [(card, e) for card, e in self.items if e["type"] == kind]


@pytest.fixture(scope="module", autouse=True)
def _forbid_a_poisoned_process():
    """Refuse to run in a process that has already opened a device itself.

    **Measured, not theorised** (Task 18): once a process has opened a
    Tenstorrent device through `tt_bio.tenstorrent.get_device()`, a child
    process it spawns afterwards cannot open one at all. The child parks in
    `futex_do_wait` inside `ttnn.open_device` -- UMD's cross-process bring-up
    path, the exact deadlock `tt_bio.tenstorrent._device_init_lock`'s docstring
    describes -- and never returns, while holding that host-wide init flock, so
    the other three workers queue behind it too. It happens even though the
    parent's `cleanup()` returned and the parent holds no `/dev/tenstorrent`
    fd. Reproduced deterministically: a worker child spawned from a clean
    process is ready in 3.5 s; from a process that had opened and closed a
    device, it was still not ready after 120 s.

    `tests/integration/test_egg_on_device.py` and `test_real_fold.py` both open
    a device in the pytest process, so `pytest tests/integration` used to wedge
    four workers on all four cards for the length of this test's timeout.
    `scripts/test.sh` now runs this file as its own pytest invocation. This
    guard is what makes that arrangement self-enforcing rather than a fact
    somebody has to remember: run it the wrong way and you get this message in
    two seconds instead of four wedged workers in two minutes.

    The signal is "has `tt_bio.tenstorrent` been imported into this process".
    That is a PROXY, and a deliberate one: merely importing it opens nothing,
    so in principle it can be imported innocently. In this repo it is not --
    nothing in the parent process imports it except code that is about to open
    a device (`runner/folder.py` imports it lazily inside `load()`, which the
    parent never calls; `runner/egg.py` likewise). If that ever stops being
    true, the fix is to make tt-bio expose "this process has opened a device"
    and check that instead -- not to delete this guard.
    """
    if "tt_bio.tenstorrent" in sys.modules:
        pytest.fail(
            "tt_bio.tenstorrent is already imported in this pytest process, "
            "which means something here has probably opened a device. Worker "
            "children spawned from such a process deadlock in UMD bring-up "
            "and never come ready (see this fixture's docstring). Run this "
            "file in its own pytest process -- `scripts/test.sh --hw` already "
            "does, and `pytest tests/integration/test_four_workers.py` does "
            "too.")


@pytest.fixture(scope="module")
def four_workers(tt_device, tmp_path_factory, monkeypatch_module):
    """A real `WorkerPool` over every chip on this box, torn down for real.

    Yields `(pool, specs, events, pids)`. `pids` are the CHILDREN's real pids,
    captured through the `spawn` seam -- so the teardown can verify (and, if
    the pool failed to, enforce) that no worker outlives this module.
    """
    # Production-like per-card log namespacing. tests/integration/conftest.py's
    # session-autouse fixture puts TT_METAL_LOGS_PATH in os.environ for
    # containment, and `worker_environ` sets that variable with `setdefault` --
    # so leaving it there would collapse all four workers' tt-metal output into
    # ONE tree, which is the exact thing worker_environ's per-card root exists
    # to prevent, silently, in the one test that folds on four chips at once.
    # Dropped here so each worker derives its own `<log_root>/card-N`;
    # containment is preserved because `log_root` below is under tmp_path.
    monkeypatch_module.delenv("TT_METAL_LOGS_PATH", raising=False)

    specs = worker_specs()
    assert len(specs) >= 2, (
        f"this test is about several chips being several chips; this box "
        f"reported {len(specs)}")

    log_root = tmp_path_factory.mktemp("four-workers-logs")
    events = _Events()
    pids = []

    def spawn(spec, env):
        handle = _spawn_subprocess(spec, env, log_root=str(log_root))
        pids.append(handle._proc.pid)
        return handle

    pool = WorkerPool(specs, events, log_root=str(log_root), spawn=spawn,
                      # No respawns during this test. A worker that dies here
                      # is a result, not something to paper over -- and a
                      # respawn would put a process on a chip AFTER the pids
                      # list was read, which is how a "no strays" check ends
                      # up not covering the stray.
                      restart_delay_s=10_000.0)
    try:
        pool.start()
        yield pool, specs, events, pids
    finally:
        pool.stop()
        # Belt and braces, and deliberately not conditional on the assertions
        # above having run: if `stop()` is broken (the second named mutation),
        # this is the only thing standing between a failed test and four
        # orphaned processes holding every card on a shared machine.
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while _pids_alive(pids) and time.monotonic() < deadline:
            time.sleep(0.2)
        strays = _pids_alive(pids)
        for pid in strays:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.fixture(scope="module")
def monkeypatch_module():
    """`monkeypatch` is function-scoped; this module's fixture is not."""
    with pytest.MonkeyPatch.context() as mp:
        yield mp


def _await(predicate, timeout_s, what):
    """Poll `predicate` until it is truthy, or fail after `timeout_s`.

    `what` is a CALLABLE, not a string, and that is not a stylistic
    preference -- it was a real defect here. The first version took an
    f-string, which Python evaluates at the call site, five minutes before the
    timeout it describes: the mutation run that proved this module can fail
    reported "got []" because that was the state when `_await` was ENTERED,
    while the actual state at the deadline was one ready card out of four. A
    diagnostic that describes the moment before the wait is worse than none,
    because it reads like the moment after it.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    pytest.fail(f"timed out after {timeout_s:.0f}s waiting for {what()}")


def test_every_chip_on_this_box_folds_at_the_same_time(four_workers):
    """One test, because it is one expensive thing: four chips, four folds.

    Split into four `assert` clusters rather than four test functions on
    purpose -- each would otherwise need its own module-scoped fixture
    ordering to avoid re-folding, and the failure this exists to catch (chip 4
    never came up) makes every one of them fail identically anyway.
    """
    pool, specs, events, pids = four_workers
    cards = [spec.card for spec in specs]

    # 1. Every worker opened a chip and loaded the model.
    #
    # This is the load-bearing assertion of the whole module. `worker.ready`
    # is emitted only after `Folder.load()` returns (runner/worker.py), which
    # means the device is OPEN -- and tt_bio.device_lease holds an exclusive
    # flock on that physical card for as long as it stays open. All of them
    # ready at once is therefore all of them on different chips, enforced by
    # the kernel. If they had all been pinned to chip 0, three would still be
    # blocked in `flock` here (or already dead of DeviceInUseError).
    t0 = time.monotonic()
    _await(lambda: len(pool.ready_cards()) == len(specs), READY_TIMEOUT_S,
           lambda: (f"all {len(specs)} workers to announce worker.ready "
                    f"(ready at the deadline: {pool.ready_cards()})"))
    load_s = time.monotonic() - t0
    assert sorted(pool.ready_cards()) == sorted(cards)
    assert len(pids) == len(specs), (
        f"expected one child per chip, spawned {len(pids)}")
    assert _pids_alive(pids) == pids, "a worker died before it was given work"

    # 2. One fold each, all in flight together.
    from runner.queue import Job
    for i, card in enumerate(cards):
        pool.dispatch(Job(job_id=f"hw-{card}", target_id="trpcage",
                          input_path=str(INPUT), n_residues=20), card)
    assert pool.ready_cards() == [], (
        "every card should be busy the instant its job is dispatched")

    _await(lambda: len(events.of_type("job_done")) == len(specs),
           FOLD_TIMEOUT_S,
           lambda: (f"all {len(specs)} folds to finish (done at the deadline: "
                    f"{sorted(c for c, _ in events.of_type('job_done'))}; "
                    f"errors: {[e for _, e in events.of_type('job_error')]})"))

    # 3. Four real structures, one per chip, each plausible.
    done = events.of_type("job_done")
    by_card = {card: event for card, event in done}
    assert sorted(by_card) == sorted(cards), (
        f"expected one job_done per chip, got {sorted(by_card)}")

    # The pipe a line came off IS the card; `job_start` also carries the card
    # the WORKER believes it is. They must agree, or a chip is misreporting
    # itself to every UI on the socket -- which is the failure a four-chip
    # booth cannot see any other way, since the wire is otherwise identical.
    # (`job_done` carries no `card` field; `job_start` is the event that does.)
    for card, event in events.of_type("job_start"):
        assert event["card"] == card, (
            f"a line off card {card}'s pipe claims card {event['card']}")
        assert event["job_id"] == f"hw-{card}"
        assert event["target_id"] == "trpcage"

    cif_paths = set()
    for card, event in done:
        assert event["job_id"] == f"hw-{card}"
        path = pathlib.Path(event["cif_path"])
        assert path.is_file(), f"card {card} reported a .cif that is not there"
        # A real mmCIF, not an empty file the assertion above would accept.
        assert path.stat().st_size > 1000
        assert "ATOM" in path.read_text()
        cif_paths.add(str(path))
        # Trp-cage on protenix-v2 measures 93-96 on this box. The window is
        # wide because this is a confidence check, not a regression pin -- but
        # it is NOT `0 <= plddt <= 100`, which every possible number passes.
        assert 60.0 <= event["mean_plddt"] <= 100.0, (
            f"card {card} folded to an implausible pLDDT "
            f"{event['mean_plddt']}")
    assert len(cif_paths) == len(specs), (
        f"four folds must write four files, got {sorted(cif_paths)}")

    # And the trajectory really streamed -- a fold that emitted a job_done and
    # no frames is a fold nothing would have rendered.
    frames_per_card = {}
    for card, event in events.of_type("frame"):
        frames_per_card[card] = frames_per_card.get(card, 0) + 1
    assert sorted(frames_per_card) == sorted(cards)
    assert all(n >= 25 for n in frames_per_card.values()), frames_per_card

    # 4. Every chip is free again, and stop() leaves nothing behind.
    assert sorted(pool.ready_cards()) == sorted(cards), (
        "every worker should be idle and dispatchable again after its fold")
    pool.stop()
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while _pids_alive(pids) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _pids_alive(pids) == [], (
        f"WorkerPool.stop() left worker process(es) running: "
        f"{_pids_alive(pids)} -- on a shared machine those hold a card each")
    assert pool.ready_cards() == [], "a stopped pool must dispatch nothing"

    # Reported, not asserted: the numbers this test is in a position to
    # measure and the booth's operators care about. `-s` to see them.
    print(f"\nfour-worker load: {load_s:.1f}s to all {len(specs)} ready; "
          f"pLDDT " + ", ".join(f"card {c}={e['mean_plddt']:.1f}"
                                for c, e in sorted(by_card.items())))
