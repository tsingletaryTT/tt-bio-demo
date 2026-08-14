"""The janitors at four-up: four log writers, four structure trees.

This is the Phase 3a trap with four writers instead of one, and
docs/followups.md records what that trap cost the first time: tt-metal's
Inspector held `mesh_workloads_log.yaml` open and kept writing **13-14 MB/s
into a file the janitor had already unlinked**. Unlinking removes a NAME; the
blocks stay allocated until the last fd closes. The daemon's own
`log_root_size()` walks the directory tree, so it saw a log root of ~0 bytes
while `lsof` showed the nameless inode growing from 938 MB to 1.79 GB in sixty
seconds -- on a tmpfs root, i.e. RAM, which it would have exhausted in about
thirty-one minutes. Two separate tasks had "verified log containment" over
two-fold sessions before a twenty-eight-fold run found it.

**The same trap is reintroduced by a different route in this phase.** The
parent now holds four `worker.log` files open -- `_SubprocessWorker` opens each
in append mode and hands it to `Popen` as that child's stdout and stderr -- and
`prune_log_root`'s oldest-first `unlink` would take their names and free
nothing. A short test run cannot see unbounded growth, so these tests do not
try to: they check the *mechanism* instead. In particular
`test_truncation_frees_the_blocks_a_held_open_writer_is_using` keeps a real fd
open across the sweep and compares inodes and block counts through it, which is
the unit-test equivalent of the `lsof` check that found the original bug and is
the only assertion here that could tell `unlink` from `truncate` at all.
"""

import os
from pathlib import Path

import pytest

import runner.daemon as mod
from runner.env import log_root_size
from runner.pool import WORKER_LOG_CAP_BYTES

from _daemonfakes import _FakePool, _daemon


def _write(path, size):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


class _PoolWithLogs(_FakePool):
    """A `_FakePool` that reports worker log paths, as the real pool does.

    Deliberately rooted somewhere other than the daemon's own `log_root` in
    the tests that use it, so that a daemon which rebuilt these paths from
    `config.log_root` instead of asking the pool would be visibly wrong rather
    than accidentally right.
    """

    def __init__(self, root, **kw):
        super().__init__(**kw)
        self.worker_log_paths = [
            str(Path(root) / f"card-{card}" / "worker.log")
            for card in self.cards]


# --- one tree per chip ----------------------------------------------------

def test_each_worker_writes_into_its_own_log_directory(tmp_path):
    """One tree per chip: a crash's evidence is attributable, and an
    oldest-first sweep cannot delete another worker's."""
    from runner.workers import WorkerSpec, worker_environ
    spec = WorkerSpec(card=3, label="q:tt3", visible_devices="3",
                      logical_device_id=0, mesh_graph_descriptor=None)
    env = worker_environ(spec, log_root=str(tmp_path), n_workers=4, base={})
    assert env["TT_METAL_LOGS_PATH"].endswith("card-3")


def test_the_worker_log_lives_inside_that_same_card_tree(tmp_path):
    """The two halves of "card 3's output" are computed in two modules --
    `workers.worker_environ` names the tt-metal tree, `pool._worker_log_path`
    names the file the parent holds open -- and the janitor's correctness
    depends on the second being INSIDE the first: `_prune_logs` sweeps the
    whole log root and spares exactly one path per card. If the two ever
    disagreed on the `card-N` spelling, the sweep would protect a path nobody
    writes to and unlink the one every worker does.
    """
    from runner.pool import _worker_log_path
    from runner.workers import WorkerSpec, worker_environ
    spec = WorkerSpec(card=3, label="q:tt3", visible_devices="3",
                      logical_device_id=0, mesh_graph_descriptor=None)
    env = worker_environ(spec, log_root=str(tmp_path), n_workers=4, base={})

    assert (_worker_log_path(tmp_path, 3).parent
            == Path(env["TT_METAL_LOGS_PATH"]))


# --- the held-open worker logs --------------------------------------------

def test_a_live_worker_log_is_never_unlinked_by_the_pruner(tmp_path):
    """Unlinking a file a process holds open removes its NAME and frees
    nothing -- the exact failure docs/followups.md measured at 13-14 MB/s
    into a tmpfs root. The sweep must refuse to touch these."""
    daemon = _daemon(tmp_path, _FakePool(), log_budget_bytes=1)
    live = [_write(tmp_path / "logs" / f"card-{c}" / "worker.log", 4096)
            for c in range(4)]
    daemon.worker_log_paths = [str(p) for p in live]
    _write(tmp_path / "logs" / "card-0" / "kernels.yaml", 4096)

    daemon._prune_logs()

    assert all(p.exists() for p in live)


def test_an_oversized_worker_log_is_truncated_in_place_not_deleted(tmp_path):
    """Truncation is what actually frees the blocks under a held fd.

    Note the budget: the default two gigabytes, nowhere near binding. The cap
    on a single worker log is a separate bound from the log root's total, and
    it has to apply on its own -- a root comfortably under budget is exactly
    the situation in which one worker in an error loop runs away with the
    disk.
    """
    daemon = _daemon(tmp_path, _FakePool())
    log = _write(tmp_path / "logs" / "card-0" / "worker.log",
                 WORKER_LOG_CAP_BYTES + 4096)
    daemon.worker_log_paths = [str(log)]

    daemon._prune_logs()

    assert log.exists()
    assert log.stat().st_size <= WORKER_LOG_CAP_BYTES


def test_a_worker_log_under_the_cap_is_left_alone(tmp_path):
    daemon = _daemon(tmp_path, _FakePool())
    log = _write(tmp_path / "logs" / "card-0" / "worker.log", 1024)
    daemon.worker_log_paths = [str(log)]

    daemon._prune_logs()

    assert log.stat().st_size == 1024


def test_truncation_frees_the_blocks_a_held_open_writer_is_using(
        tmp_path, monkeypatch):
    """The one assertion here that can tell `unlink` from `truncate`.

    Everything else in this file inspects the file by NAME, and a name is
    exactly what the original bug still had -- `prune_log_root` unlinks, the
    walk stops seeing the file, and the writer keeps filling the disk through
    an fd nothing in the directory tree points at any more. `lsof` is how that
    was eventually found on the running booth; `os.fstat` on a fd this test
    holds across the sweep is the same check, in a unit test:

    - **same inode, still linked** -- so the surviving name is the file the
      writer is actually writing into, not a fresh one created next to an
      orphaned inode. `st_nlink == 0` through the fd is the signature of the
      original failure and is asserted against directly.
    - **blocks actually returned** -- `st_size` AND `st_blocks` seen through
      the writer's own fd, which is the number a directory walk cannot lie
      about.
    - **the writer keeps working afterwards**, appending from the new end
      rather than re-inflating the file to its old size, which is the property
      `O_APPEND` gives and the reason truncation is safe here at all.

    The cap is monkeypatched down so this writes kilobytes rather than 64 MB;
    the mechanism under test is indifferent to the number, and patching it
    also proves the daemon reads the constant rather than a literal.
    """
    monkeypatch.setattr(mod, "WORKER_LOG_CAP_BYTES", 4096)
    daemon = _daemon(tmp_path, _FakePool(), log_budget_bytes=1)
    path = tmp_path / "logs" / "card-0" / "worker.log"
    path.parent.mkdir(parents=True)
    daemon.worker_log_paths = [str(path)]

    # Exactly how runner/pool.py opens it: append mode, held for the life of
    # the worker.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        os.write(fd, b"x" * (4096 * 4))
        before = os.fstat(fd)
        assert before.st_blocks > 0, "precondition: the file occupies blocks"

        daemon._prune_logs()

        after = os.fstat(fd)
        assert after.st_nlink >= 1, (
            "the writer's file still has a name -- an unlinked-but-open log "
            "is the 13-14 MB/s invisible-growth bug all over again")
        assert path.exists() and path.stat().st_ino == before.st_ino, (
            "and that name still resolves to the same inode, so nothing was "
            "unlinked and recreated beside a still-growing orphan")
        assert after.st_size == 0 and after.st_blocks == 0, (
            "the blocks were actually returned, as seen through the fd rather "
            "than through a directory walk that cannot see the truth")

        os.write(fd, b"y" * 10)
        assert os.fstat(fd).st_size == 10, (
            "O_APPEND puts the next write at the new end; a file re-inflated "
            "to its old size would mean the truncation freed nothing lasting")
    finally:
        os.close(fd)


def test_other_files_under_the_log_root_are_still_pruned(tmp_path):
    """Protecting the worker logs must not turn the whole sweep off."""
    daemon = _daemon(tmp_path, _FakePool(), log_budget_bytes=1024)
    _write(tmp_path / "logs" / "card-0" / "worker.log", 512)
    junk = _write(tmp_path / "logs" / "card-1" / "kernels.yaml", 8192)
    daemon.worker_log_paths = [str(tmp_path / "logs" / "card-0" / "worker.log")]

    daemon._prune_logs()

    assert not junk.exists()
    assert log_root_size(tmp_path / "logs") == 512


# --- the wiring: production assigns none of this by hand -------------------

def test_the_daemon_takes_the_worker_log_paths_from_the_pool(tmp_path):
    """The pool opened these files, so the pool is the one that knows where
    they are. A daemon that rebuilt the paths from `config.log_root` would be
    two independently-computed lists -- and `TT_METAL_LOGS_PATH` is a
    setdefault an operator may have overridden, which is exactly when the two
    would part company (see WorkerPool's own note on this).
    """
    pool = _PoolWithLogs(tmp_path / "elsewhere")
    daemon = _daemon(tmp_path, pool)

    assert daemon.worker_log_paths == pool.worker_log_paths


def test_the_pools_worker_logs_are_protected_without_a_test_assigning_them(
        tmp_path):
    """Everything above hands `daemon.worker_log_paths` a list. Production
    never does -- so this is the test that fails if the property exists, works
    perfectly, and is wired to nothing.
    """
    pool = _PoolWithLogs(tmp_path / "logs")
    daemon = _daemon(tmp_path, pool, log_budget_bytes=1)
    live = [_write(p, 4096) for p in pool.worker_log_paths]
    junk = _write(tmp_path / "logs" / "card-0" / "kernels.yaml", 4096)

    daemon._prune_logs()

    assert all(p.exists() for p in live)
    assert not junk.exists(), "and the rest of the root is still swept"


def test_a_daemon_with_no_pool_yet_protects_nothing_and_does_not_raise(tmp_path):
    """`_prune_logs` can run before a pool exists (run() builds one while the
    janitor period is already ticking). No pool means no held-open fds, so an
    empty protected set is the honest answer as well as the safe one.
    """
    daemon = _daemon(tmp_path, _FakePool())
    daemon.pool = None
    assert daemon.worker_log_paths == []
    daemon._prune_logs()          # must not raise


# --- four structure trees --------------------------------------------------

def test_structures_are_pruned_across_every_cards_directory(tmp_path):
    """Four folders, four structure trees. A sweep that only knows about
    card 0's grows the other three forever."""
    daemon = _daemon(tmp_path, _FakePool(), structures_budget_bytes=1024)
    dirs = []
    for card in range(4):
        d = tmp_path / "structures" / f"device-{card}"
        _write(d / "old.cif", 8192)
        dirs.append(d)
    daemon.structures_dirs = [str(d) for d in dirs]

    daemon._prune_structures()

    assert not any((d / "old.cif").exists() for d in dirs)


def test_a_recently_emitted_cif_is_protected_on_every_card(tmp_path):
    """The UI can be more than a second behind the socket in reading a .cif
    (ribbon_from_cif measured 1221ms at 3000 residues). Four cards means
    four such files can be in flight at once."""
    daemon = _daemon(tmp_path, _FakePool(), structures_budget_bytes=1)
    fresh = []
    for card in range(4):
        d = tmp_path / "structures" / f"device-{card}"
        path = _write(d / "fresh.cif", 8192)
        fresh.append(path)
        daemon._emit_and_track(card, {"type": "job_done", "job_id": f"j{card}",
                                      "cif_path": str(path), "wall_s": 4.4,
                                      "mean_plddt": 95.3})
    daemon.structures_dirs = [str(tmp_path / "structures" / f"device-{c}")
                              for c in range(4)]

    daemon._prune_structures()

    assert all(p.exists() for p in fresh)


def test_one_cards_output_does_not_evict_anothers_protected_file(tmp_path):
    """The bug a single shared protected deque of 3 would produce: four
    cards finishing at once evict each other's newest structures.

    This is the one that cannot be argued from the code. With
    PROTECTED_STRUCTURE_COUNT = 3 and four cards, a shared deque holds three
    *in total*, so nine of these twelve fresh files lose their protection the
    moment the budget binds -- and the budget here is one byte, so it binds.
    """
    daemon = _daemon(tmp_path, _FakePool(), structures_budget_bytes=1)
    fresh = []
    for card in range(4):
        d = tmp_path / "structures" / f"device-{card}"
        for n in range(3):
            path = _write(d / f"s{n}.cif", 4096)
            daemon._emit_and_track(card, {"type": "job_done",
                                          "job_id": f"j{card}-{n}",
                                          "cif_path": str(path), "wall_s": 4.4,
                                          "mean_plddt": 95.3})
            fresh.append(path)
    daemon.structures_dirs = [str(tmp_path / "structures" / f"device-{c}")
                              for c in range(4)]

    daemon._prune_structures()

    assert all(p.exists() for p in fresh), "each card protects its own three"


def test_emit_and_track_protects_the_file_before_telling_anyone_about_it(
        tmp_path):
    """Order matters, and it is free. The instant the event leaves this
    process a UI may be opening the path it names, so the file has to be in
    the protected set before the broadcast rather than after it -- a janitor
    pass landing in that window would otherwise see a live structure as
    unprotected.
    """
    daemon = _daemon(tmp_path, _FakePool())
    seen = []
    daemon.server.broadcast = lambda event: seen.append(
        list(daemon._recent_structures[2])) or 1

    daemon._emit_and_track(2, {"type": "job_done", "job_id": "j1",
                               "cif_path": "/s/device-2/a.cif",
                               "wall_s": 4.4, "mean_plddt": 95.3})

    assert seen == [["/s/device-2/a.cif"]], (
        "the path was already protected at the moment it went on the wire")


def test_an_event_that_names_no_structure_records_nothing(tmp_path):
    """Most events are not job_done. Recording a None (or every event's
    absent cif_path) would fill the per-card deque with junk and evict the
    real structures it exists to protect.
    """
    daemon = _daemon(tmp_path, _FakePool())
    for event in ({"type": "job_start", "job_id": "j1", "target_id": "t1",
                   "n_residues": 20},
                  {"type": "progress", "job_id": "j1", "step": 3, "total": 200},
                  {"type": "job_error", "job_id": "j1", "target_id": "t1",
                   "message": "boom"}):
        daemon._emit_and_track(0, event)

    assert list(daemon._recent_structures[0]) == []
    assert len(daemon.server.events) == 3, "and every one still reached the UI"


# --- and none of it may ever stop the booth --------------------------------

def test_a_janitor_failure_never_stops_the_booth(tmp_path, monkeypatch):
    def explode(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(mod, "prune_log_root", explode)
    daemon = _daemon(tmp_path, _FakePool())
    daemon._prune_logs()          # must not raise
    daemon._prune_structures()    # must not raise


def test_a_worker_log_that_cannot_be_truncated_still_gets_the_root_swept(
        tmp_path, monkeypatch):
    """The failure the two halves of `_prune_logs` were given separate guards
    for. A worker log on a filesystem that refuses `truncate` is a bad day;
    a booth that then stops pruning tt-metal's tree as well is a full disk.
    """
    monkeypatch.setattr(mod, "WORKER_LOG_CAP_BYTES", 4096)
    daemon = _daemon(tmp_path, _FakePool(), log_budget_bytes=1024)
    log = _write(tmp_path / "logs" / "card-0" / "worker.log", 4096 * 4)
    junk = _write(tmp_path / "logs" / "card-1" / "kernels.yaml", 8192)
    daemon.worker_log_paths = [str(log)]
    # A real EACCES from a real truncate(2), rather than a patched `os` --
    # the directory stays writable, so `prune_log_root` could still unlink
    # this file if it were not protected. Both halves are therefore under
    # test at once: the truncate fails, and the sweep must neither give up
    # nor fall back to deleting.
    os.chmod(log, 0o444)
    try:
        daemon._prune_logs()      # must not raise
    finally:
        os.chmod(log, 0o644)

    assert log.exists(), "and it is still protected from being unlinked"
    assert log.stat().st_size == 4096 * 4, "nothing was freed, honestly so"
    assert not junk.exists(), "the rest of the sweep happened anyway"
