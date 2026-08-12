import sys
import threading
import types
from pathlib import Path

import pytest

from runner.daemon import Daemon, DaemonConfig, main
from runner.folder import FoldError
from runner.queue import Job


class _FakeFolder:
    """Stands in for the real Folder: records calls, emits or raises on demand."""

    def __init__(self, outcomes=None):
        # outcomes maps target_id -> Exception to raise; anything else succeeds.
        self.outcomes = outcomes or {}
        self.folded = []
        self.loads = 0
        self.closes = 0
        # The real Folder's structures_dir is namespaced by device_id (Task
        # 10 review); tests that don't care about structures pruning just
        # need this to exist so _prune_structures has something to call
        # prune_log_root against (a nonexistent path is a harmless no-op --
        # see prune_log_root's own "missing root" handling).
        self.structures_dir = Path("/tmp/tt-bio-demo-test-fake/structures")

    def load(self):
        self.loads += 1

    def close(self):
        self.closes += 1

    def fold(self, job_id, input_path, emit, *, target_id, n_residues,
             card=0, n_step=200):
        self.folded.append((job_id, target_id, card))
        problem = self.outcomes.get(target_id)
        if problem is not None:
            raise problem
        emit({"type": "job_done", "job_id": job_id, "cif_path": "/tmp/x.cif",
              "wall_s": 1.0, "mean_plddt": 95.0})


class _FakeCards:
    """A card pool whose behavior each test controls explicitly."""

    def __init__(self, available=(0,), quarantine_on_claim=False):
        self._available = list(available)
        # A separate, fixed snapshot of the pool's full inventory -- mirrors
        # the real CardPool, where schedulable() (busy/hot-filtered) and the
        # underlying set of indices it tracks are two different things.
        # _available above mutates in some scenarios (e.g. _RaceCards below
        # sets it to [] on quarantine); the inventory does not.
        self._all = list(available)
        self.quarantine_on_claim = quarantine_on_claim
        self.busy_calls = []
        self.idle_calls = []

    def schedulable(self):
        return list(self._available)

    def all_indices(self):
        return list(self._all)

    def mark_busy(self, index):
        self.busy_calls.append(index)
        if self.quarantine_on_claim:
            # Simulates the telemetry thread quarantining this card between
            # schedulable() and mark_busy() — the real TOCTOU window.
            self.quarantine_on_claim = False
            self._available = []
            raise ValueError(f"card {index} is quarantined")
        return {"type": "card_state", "card": index, "state": "busy"}

    def mark_idle(self, index):
        self.idle_calls.append(index)
        return {"type": "card_state", "card": index, "state": "idle"}

    def update(self, cards):
        return []


def _daemon(tmp_path, folder, cards, **over):
    config = DaemonConfig(
        socket_path=str(tmp_path / "r.sock"),
        weights_dir=str(tmp_path / "weights"),
        playlist_dir=str(tmp_path / "playlist"),
        log_root=str(tmp_path / "logs"),
        **over,
    )
    daemon = Daemon(config)
    daemon.folder = folder
    daemon.cards = cards
    daemon.events = []
    daemon._emit = daemon.events.append      # capture instead of broadcasting
    return daemon


def test_a_successful_job_marks_the_card_busy_then_idle(tmp_path):
    folder, cards = _FakeFolder(), _FakeCards()
    daemon = _daemon(tmp_path, folder, cards)
    daemon._run_one(Job("j1", "trpcage", "/tmp/t.yaml"), card=0)

    states = [(e["state"]) for e in daemon.events if e["type"] == "card_state"]
    assert states == ["idle"], "the claim is made by the loop, the release by _run_one"
    assert cards.idle_calls == [0]
    assert folder.folded == [("j1", "trpcage", 0)]


def test_a_failed_fold_reports_job_error_and_releases_the_card(tmp_path):
    folder = _FakeFolder({"bad": FoldError("device fell over")})
    cards = _FakeCards()
    daemon = _daemon(tmp_path, folder, cards)
    daemon._run_one(Job("j1", "bad", "/tmp/b.yaml"), card=0)

    errors = [e for e in daemon.events if e["type"] == "job_error"]
    assert len(errors) == 1
    assert errors[0]["target_id"] == "bad"
    assert cards.idle_calls == [0], "a failed fold must still release its card"


def test_a_failed_fold_does_not_propagate_out_of_run_one(tmp_path):
    """An unattended booth keeps folding after one target misbehaves."""
    folder = _FakeFolder({"bad": FoldError("boom")})
    daemon = _daemon(tmp_path, folder, _FakeCards())
    daemon._run_one(Job("j1", "bad", "/tmp/b.yaml"), card=0)   # must not raise


def test_a_non_fold_error_exception_is_still_reported_and_does_not_crash_the_loop(tmp_path):
    """Folder.fold() is documented to raise only FoldError, but the daemon's
    loop must not bet the whole booth on every collaborator keeping that
    promise -- see runner/folder.py's own fix, where TapUnavailable used to
    escape fold() directly instead of being wrapped. This pins the daemon
    side's backstop independently of whether Folder.fold() itself is correct
    today: _FakeFolder here raises a plain RuntimeError, something FoldError
    handling alone would not catch.
    """
    folder = _FakeFolder({"bad": RuntimeError("tap fell over unexpectedly")})
    cards = _FakeCards()
    daemon = _daemon(tmp_path, folder, cards)
    daemon._run_one(Job("j1", "bad", "/tmp/b.yaml"), card=0)   # must not raise

    errors = [e for e in daemon.events if e["type"] == "job_error"]
    assert len(errors) == 1
    assert errors[0]["target_id"] == "bad"
    assert cards.idle_calls == [0], "the card must still be released"


class _ExplodingIdleCards(_FakeCards):
    """mark_idle raises unconditionally -- stands in for a bug in
    CardPool.mark_idle, which used to sit unguarded in _run_one's finally
    right next to _prune_logs/_prune_structures, both of which already were
    guarded (Task 9 review flagged the gap and deferred it)."""

    def mark_idle(self, index):
        raise RuntimeError("mark_idle blew up")


def test_a_mark_idle_failure_does_not_escape_run_one(tmp_path):
    """Same 'nothing may raise out of the fold loop' constraint _run_one's
    two janitor calls already respect. A successful fold still must not let
    a CardPool bug take the daemon down with it.
    """
    daemon = _daemon(tmp_path, _FakeFolder(), _ExplodingIdleCards())
    daemon._run_one(Job("j1", "t", "/tmp/t.yaml"), card=0)   # must not raise


def test_a_mark_idle_failure_does_not_prevent_the_janitors_from_running(tmp_path, monkeypatch):
    """The guard around mark_idle must not accidentally swallow the rest of
    the finally block too -- pruning must still run even if mark_idle blew
    up just before it.
    """
    from runner import daemon as mod

    pruned = []
    monkeypatch.setattr(
        mod, "prune_log_root",
        lambda root, budget, protect=None: (pruned.append(root), (0, []))[1])
    daemon = _daemon(tmp_path, _FakeFolder(), _ExplodingIdleCards())
    daemon._run_one(Job("j1", "t", "/tmp/t.yaml"), card=0)
    assert daemon.config.log_root in pruned


def test_a_target_is_quarantined_after_three_consecutive_failures(tmp_path):
    folder = _FakeFolder({"bad": FoldError("boom")})
    daemon = _daemon(tmp_path, folder, _FakeCards())
    for n in range(3):
        daemon._run_one(Job(f"j{n}", "bad", "/tmp/b.yaml"), card=0)
    assert "bad" in daemon._quarantined


def test_a_target_that_recovers_is_not_quarantined(tmp_path):
    """Two failures then a success must reset the count, not creep toward three."""
    folder = _FakeFolder({"flaky": FoldError("boom")})
    daemon = _daemon(tmp_path, folder, _FakeCards())
    daemon._run_one(Job("j1", "flaky", "/tmp/f.yaml"), card=0)
    daemon._run_one(Job("j2", "flaky", "/tmp/f.yaml"), card=0)
    folder.outcomes = {}                       # it works this time
    daemon._run_one(Job("j3", "flaky", "/tmp/f.yaml"), card=0)
    folder.outcomes = {"flaky": FoldError("boom")}
    daemon._run_one(Job("j4", "flaky", "/tmp/f.yaml"), card=0)
    assert "flaky" not in daemon._quarantined


def test_a_quarantined_target_is_not_re_enqueued(tmp_path):
    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "good.yaml").write_text("version: 1\n")
    (playlist / "bad.yaml").write_text("version: 1\n")

    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    daemon._quarantined.add("bad")
    daemon._enqueue_playlist()
    assert [j.target_id for j in daemon.queue.pending] == ["good"]


def _fake_tt_bio_main_read_bio_chains(monkeypatch, chains_or_exc):
    """Install a stand-in tt_bio.main with only _read_bio_chains faked --
    same style as tests/unit/runner/test_folder_events.py's tt_bio fakes,
    so this pins daemon.py's *use* of the return value (or of a raised
    exception) without depending on tt_bio's real YAML schema, and without
    paying for a real torch import in every test that doesn't care about
    residue counting specifically.
    """
    main_mod = types.ModuleType("tt_bio.main")

    def _read_bio_chains(path):
        if isinstance(chains_or_exc, BaseException):
            raise chains_or_exc
        return chains_or_exc

    main_mod._read_bio_chains = _read_bio_chains
    pkg = types.ModuleType("tt_bio")
    pkg.main = main_mod
    monkeypatch.setitem(sys.modules, "tt_bio", pkg)
    monkeypatch.setitem(sys.modules, "tt_bio.main", main_mod)


def test_enqueue_playlist_populates_n_residues_from_the_target(tmp_path, monkeypatch):
    """job_start carries n_residues purely for the UI's display label (see
    runner/folder.py's fold()); before this fix it was always 0, because
    _enqueue_playlist never set it on the Job it submitted. The real count
    comes from tt_bio's own chain reader -- the same one
    Folder._run_fold() already calls to build features -- summing every
    non-ligand chain's sequence length, matching
    tests/fixtures/streams/capture_real_fold.py's own formula.
    """
    _fake_tt_bio_main_read_bio_chains(monkeypatch, [
        ("A", "NLYIQWLKDGGPSSGRPPPS", None, "protein"),   # 20 residues
        ("B", "CCD_ATP", None, "ligand"),                  # excluded
    ])
    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "trpcage.yaml").write_text("version: 1\n")

    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    daemon._enqueue_playlist()

    assert [j.n_residues for j in daemon.queue.pending] == [20], (
        "n_residues must count protein/RNA/DNA residues, excluding ligands")


def test_enqueue_playlist_defaults_n_residues_to_zero_on_a_read_failure(tmp_path, monkeypatch):
    """A malformed or unreadable playlist target must not crash the enqueue
    loop: n_residues is cosmetic, and a target this daemon truly cannot
    parse will still fail loudly and safely later, inside _run_one's own
    FoldError handling, the same way a bad target always has.
    """
    _fake_tt_bio_main_read_bio_chains(monkeypatch, ValueError("malformed yaml"))
    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "bad.yaml").write_text("not: [valid\n")

    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    daemon._enqueue_playlist()   # must not raise

    assert [j.n_residues for j in daemon.queue.pending] == [0]


def _fake_tt_bio_main_missing_read_bio_chains(monkeypatch):
    """Install a stand-in tt_bio.main with no _read_bio_chains attribute at
    all -- simulates a tt-bio upgrade that renames or removes the private
    helper _enqueue_playlist imports. Distinct from
    _fake_tt_bio_main_read_bio_chains above, which always defines the
    attribute (and only varies what calling it does); this one makes the
    `from tt_bio.main import _read_bio_chains` statement itself raise
    ImportError, which is the failure this regression test targets.
    """
    main_mod = types.ModuleType("tt_bio.main")   # deliberately no _read_bio_chains
    pkg = types.ModuleType("tt_bio")
    pkg.main = main_mod
    monkeypatch.setitem(sys.modules, "tt_bio", pkg)
    monkeypatch.setitem(sys.modules, "tt_bio.main", main_mod)


def test_enqueue_playlist_survives_a_renamed_tt_bio_helper(tmp_path, monkeypatch):
    """A tt-bio upgrade that renames or removes _read_bio_chains must degrade
    _enqueue_playlist to n_residues=0, the same as any other unreadable
    target (see test_enqueue_playlist_defaults_n_residues_to_zero_on_a_read_failure
    just above) -- not take the whole daemon down. Before this fix, the
    import sat above _enqueue_playlist's try block, so this exact scenario
    raised ImportError out of _enqueue_playlist and, since run()'s loop
    calls it unguarded, out of run() itself: an unattended booth killed by
    a routine tt-bio version bump.
    """
    _fake_tt_bio_main_missing_read_bio_chains(monkeypatch)
    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "trpcage.yaml").write_text("version: 1\n")

    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    daemon._enqueue_playlist()   # must not raise ImportError

    assert [j.target_id for j in daemon.queue.pending] == ["trpcage"]
    assert [j.n_residues for j in daemon.queue.pending] == [0]


def test_logs_are_pruned_after_a_job(tmp_path, monkeypatch):
    """Mirrors test_structures_are_pruned_after_a_job's specificity on
    purpose: the previous form of this test asserted only `pruned` truthy,
    which _prune_structures's call alone already satisfies (both janitors
    call this same monkeypatched prune_log_root and append to the same
    list) -- deleting the _prune_logs() call entirely left this test green.
    Asserting the log root specifically is what closes that gap.
    """
    pruned = []
    from runner import daemon as mod
    monkeypatch.setattr(
        mod, "prune_log_root",
        lambda root, budget, protect=None: (pruned.append(root), (0, []))[1])
    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    daemon._run_one(Job("j1", "t", "/tmp/t.yaml"), card=0)
    assert daemon.config.log_root in pruned, (
        "the log budget is never enforced if pruning is not called against "
        "the log root specifically")


def test_a_pruning_failure_does_not_stop_the_daemon(tmp_path, monkeypatch):
    """Deliberately distinguished from
    test_a_structures_pruning_failure_does_not_stop_the_daemon just below:
    the previous versions of these two tests both monkeypatched
    prune_log_root to explode unconditionally and both just asserted
    _run_one doesn't raise -- same symbol patched, same call driven,
    nothing to tell them apart, so neither could fail without the other
    (if _prune_logs's own guard broke but _prune_structures's did not, both
    tests were equally green or equally red; there was no way for one to
    catch a regression the other didn't). Making the explosion specific to
    *this* janitor's root (the log root, not the structures dir) means only
    _prune_logs's own try/except is on the hook here.
    """
    from runner import daemon as mod

    def explode_only_for_the_log_root(root, budget, protect=None):
        if root == log_root:
            raise OSError("disk gone strange")
        return (0, [])

    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    log_root = daemon.config.log_root
    monkeypatch.setattr(mod, "prune_log_root", explode_only_for_the_log_root)
    daemon._run_one(Job("j1", "t", "/tmp/t.yaml"), card=0)   # must not raise


def test_structures_are_pruned_after_a_job(tmp_path, monkeypatch):
    """The .cif accumulation flagged in Task 5b (runner/folder.py's
    Folder.structures_dir, per-device since the Task 10 review) must
    actually be swept, the same way the tt-metal log root is -- both calls
    happen in _run_one's finally, so a fold that never calls prune_log_root
    against structures_dir would leave that directory growing forever even
    though the log root is bounded.
    """
    from runner import daemon as mod

    calls = []
    monkeypatch.setattr(
        mod, "prune_log_root",
        lambda root, budget, protect=None: (calls.append((root, protect)), (0, []))[1])
    folder = _FakeFolder()
    daemon = _daemon(tmp_path, folder, _FakeCards())
    daemon._run_one(Job("j1", "t", "/tmp/t.yaml"), card=0)
    roots = [root for root, _protect in calls]
    assert folder.structures_dir in roots, (
        "the structures budget is never enforced if pruning is not called "
        "against Folder.structures_dir")


def test_the_structures_prune_protects_recently_emitted_paths(tmp_path, monkeypatch):
    """The review's central finding for this task: job_done's cif_path may
    not have been read by the UI yet -- it's dispatched via GLib.idle_add
    behind whatever else is queued on the GTK main loop, and
    ribbon_from_cif alone measured up to ~1.22s on a large structure
    (docs/followups.md) -- so prune_log_root must never be told it is free
    to delete a path this daemon has recently emitted.
    """
    from runner import daemon as mod

    calls = []
    monkeypatch.setattr(
        mod, "prune_log_root",
        lambda root, budget, protect=None: (calls.append(protect), (0, []))[1])
    folder = _FakeFolder()
    daemon = _daemon(tmp_path, folder, _FakeCards())
    daemon._run_one(Job("j1", "t", "/tmp/t.yaml"), card=0)
    # _FakeFolder.fold() always reports cif_path "/tmp/x.cif" in job_done.
    assert calls[-1] == {"/tmp/x.cif"}, (
        "the structures prune call must protect the cif_path this fold "
        "just told a UI about")


def test_the_protected_structures_set_is_bounded_to_the_most_recent_few(tmp_path):
    """self._recent_structures must not grow forever -- it is meant to
    cover a handful of folds' worth of GTK-main-loop lag, not become an
    unbounded record of every .cif this daemon has ever written (which
    would make _prune_structures's protection swallow the whole budget
    after a long enough run).
    """
    from runner.daemon import PROTECTED_STRUCTURE_COUNT

    class _NamedFolder(_FakeFolder):
        def fold(self, job_id, input_path, emit, **kwargs):
            emit({"type": "job_done", "job_id": job_id,
                  "cif_path": f"/tmp/{job_id}.cif", "wall_s": 1.0,
                  "mean_plddt": 95.0})

    daemon = _daemon(tmp_path, _NamedFolder(), _FakeCards())
    for n in range(PROTECTED_STRUCTURE_COUNT + 5):
        daemon._run_one(Job(f"j{n}", "t", "/tmp/t.yaml"), card=0)
    assert len(daemon._recent_structures) == PROTECTED_STRUCTURE_COUNT
    expected = [f"/tmp/j{n}.cif" for n in range(5, PROTECTED_STRUCTURE_COUNT + 5)]
    assert list(daemon._recent_structures) == expected


def test_a_structures_pruning_failure_does_not_stop_the_daemon(tmp_path, monkeypatch):
    """The companion half of the pair described in
    test_a_pruning_failure_does_not_stop_the_daemon above: this one only
    explodes for the structures root, so only _prune_structures's own
    try/except is on the hook, independent of whatever _prune_logs does.
    """
    from runner import daemon as mod

    folder = _FakeFolder()

    def explode_only_for_the_structures_dir(root, budget, protect=None):
        if root == folder.structures_dir:
            raise OSError("disk gone strange")
        return (0, [])

    monkeypatch.setattr(mod, "prune_log_root", explode_only_for_the_structures_dir)
    daemon = _daemon(tmp_path, folder, _FakeCards())
    daemon._run_one(Job("j1", "t", "/tmp/t.yaml"), card=0)   # must not raise


def test_structures_pruning_bounds_growth_but_never_deletes_a_recent_one(tmp_path):
    """Not mocked: drives _run_one through several real folds that write
    real files under a real (tmp_path) structures_dir, with a budget tight
    enough to bind on every fold -- the concrete "does the new policy
    actually work" check the coordinator asked for, since the shipped
    default (200 MB against ~16 KB trpcage structures) never exercises this
    path in practice.

    Budget (500 bytes) is deliberately smaller than even one file (1000
    bytes): without protection, prune_log_root's oldest-first sweep would
    delete every file including the newest one, because a single file
    already exceeds the budget. With protection, the PROTECTED_STRUCTURE_COUNT
    most recently emitted paths survive regardless -- the root stays above
    budget (the documented correctness-floor-over-budget-guarantee
    tradeoff), but nothing the daemon has recently told a UI about is ever
    deleted out from under it.
    """
    from runner.daemon import PROTECTED_STRUCTURE_COUNT

    structures = tmp_path / "structures"
    structures.mkdir()

    class _WritingFolder(_FakeFolder):
        """Writes a real 1 KB file per fold and reports its real path in
        job_done, the way the real Folder does (just without tt-bio)."""

        def __init__(self):
            super().__init__()
            self.structures_dir = structures
            self._n = 0

        def fold(self, job_id, input_path, emit, **kwargs):
            self._n += 1
            path = self.structures_dir / f"s{self._n}.cif"
            path.write_bytes(b"x" * 1000)
            self.folded.append((job_id, kwargs.get("target_id"), kwargs.get("card")))
            emit({"type": "job_done", "job_id": job_id, "cif_path": str(path),
                  "wall_s": 1.0, "mean_plddt": 95.0})

    folder = _WritingFolder()
    daemon = _daemon(tmp_path, folder, _FakeCards(), structures_budget_bytes=500)
    for n in range(10):
        daemon._run_one(Job(f"j{n}", "t", "/tmp/t.yaml"), card=0)

    # Compared as sets, not sorted lists: "s10.cif" < "s2.cif" as strings,
    # which has nothing to do with which structures are actually recent.
    remaining = {p.name for p in structures.iterdir()}
    expected = {f"s{n}.cif" for n in range(10 - PROTECTED_STRUCTURE_COUNT + 1, 11)}
    assert remaining == expected, (
        "exactly the most recently emitted structures must survive an "
        "impossibly tight budget, and nothing older should")
    total = sum((structures / name).stat().st_size for name in remaining)
    assert total == PROTECTED_STRUCTURE_COUNT * 1000, (
        "the protected floor, not the (unreachable) 500-byte budget, "
        "should be what's left standing"
    )


def test_structures_pruning_still_bounds_a_reachable_budget(tmp_path):
    """Companion to the impossible-budget case above: when the budget
    comfortably exceeds the protected floor, pruning still does real work
    -- older, unprotected files actually get deleted down toward the
    budget, not just down toward the protection floor. Confirms the new
    `protect` argument is inert (no behavior change) on the ordinary path
    where the byte budget, not the recency floor, is what's binding.
    """
    structures = tmp_path / "structures"
    structures.mkdir()

    class _WritingFolder(_FakeFolder):
        def __init__(self):
            super().__init__()
            self.structures_dir = structures
            self._n = 0

        def fold(self, job_id, input_path, emit, **kwargs):
            self._n += 1
            path = self.structures_dir / f"s{self._n}.cif"
            path.write_bytes(b"x" * 1000)
            emit({"type": "job_done", "job_id": job_id, "cif_path": str(path),
                  "wall_s": 1.0, "mean_plddt": 95.0})

    folder = _WritingFolder()
    # Room for 5 of the 1 KB files -- comfortably more than
    # PROTECTED_STRUCTURE_COUNT (3), so the byte budget binds first.
    daemon = _daemon(tmp_path, folder, _FakeCards(), structures_budget_bytes=5000)
    for n in range(10):
        daemon._run_one(Job(f"j{n}", "t", "/tmp/t.yaml"), card=0)

    remaining = {p.name for p in structures.iterdir()}
    assert remaining == {f"s{n}.cif" for n in range(6, 11)}
    total = sum((structures / name).stat().st_size for name in remaining)
    assert total <= 5000


def test_main_reports_preflight_failure_and_exits_non_zero(tmp_path, capsys, monkeypatch):
    # Deviation from the brief: as given, this test calls the real
    # run_preflight() unmocked. Its tap check imports tt_bio.protenix, which
    # pulls in ttnn, and its default card-count probe shells out to the real
    # tt-smi binary -- both real-hardware-adjacent, and both things Task 8's
    # own preflight tests (tests/unit/runner/test_preflight.py) never do
    # unmocked in a single one of their cases. Matching that convention here:
    # neither mock changes what this test is actually checking (main()'s CLI
    # wiring and exit-code contract), since weights_dir/playlist_dir don't
    # exist either way, so preflight fails regardless of what these two
    # report.
    from runner import cards as cards_mod
    from runner import preflight as preflight_mod
    monkeypatch.setattr(preflight_mod, "check_tap_supported", lambda: None)
    monkeypatch.setattr(cards_mod, "sample_tt_smi", lambda timeout=5.0: [])

    code = main([
        "--socket", str(tmp_path / "r.sock"),
        "--weights", str(tmp_path / "weights"),
        "--playlist", str(tmp_path / "playlist"),
        "--log-root", str(tmp_path / "logs"),
        "--preflight-only",
    ])
    assert code == 2
    out = capsys.readouterr().out
    assert "missing:" in out, "an operator needs the list, not just an exit code"


def test_run_may_not_be_called_twice_on_one_instance(tmp_path):
    """The device is opened exactly once per daemon lifetime, and run() is
    what opens it. A second call would find self.folder already closed by
    the first call's teardown and (Folder.load() being written to reopen
    after a close()) would silently open a second real device on it.
    """
    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    daemon.stop()          # pre-stop so the first run() returns immediately
    daemon.run()
    with pytest.raises(RuntimeError, match="once"):
        daemon.run()


class _RaceCards(_FakeCards):
    """Quarantines on the very first mark_busy() call -- the real TOCTOU
    between schedulable() and mark_busy() that the telemetry thread can hit --
    and then stops the daemon itself.

    Driving Daemon.run() (rather than calling _run_one directly, as every
    other test above does) is the only way to exercise the loop's own
    try/except around mark_busy: that guard lives in run(), not in
    _run_one(), by the brief's own design note. But run() is an unbounded
    loop, so something has to end it deterministically -- sleeping and hoping
    is exactly what the task brief says not to do. Stopping the daemon from
    inside the fake, at the instant the race would occur in production, is
    both deterministic and a faithful stand-in for "the telemetry thread
    quarantined this card right then."
    """

    def __init__(self, daemon_holder, **kwargs):
        super().__init__(quarantine_on_claim=True, **kwargs)
        self._daemon_holder = daemon_holder

    def mark_busy(self, index):
        try:
            return super().mark_busy(index)
        finally:
            self._daemon_holder["daemon"].stop()


def test_a_claim_race_with_the_telemetry_thread_requeues_the_job_and_continues(
    tmp_path, monkeypatch
):
    # Keep this test from ever touching real hardware even if timing let the
    # telemetry thread's loop body run before the race-stop takes effect.
    from runner import daemon as mod
    monkeypatch.setattr(mod, "sample_tt_smi", lambda timeout=5.0: [])

    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "trpcage.yaml").write_text("version: 1\n")

    folder = _FakeFolder()
    daemon_holder = {}
    cards = _RaceCards(daemon_holder)
    daemon = _daemon(tmp_path, folder, cards)
    daemon_holder["daemon"] = daemon

    daemon.run()   # must return: the fake stops the daemon from inside the race

    assert cards.busy_calls == [0], "the loop must have attempted the claim"
    assert [j.target_id for j in daemon.queue.pending] == ["trpcage"], (
        "a job that loses the claim race must be requeued, not dropped"
    )
    assert folder.folded == [], "a quarantined claim must never reach fold()"


# --- Daemon.run()'s loop body, driven end to end -----------------------
#
# Every test above either pre-stops the daemon before the loop body ever runs
# (test_run_may_not_be_called_twice_on_one_instance) or deliberately loses the
# card-claim race before fold() is ever reached
# (test_a_claim_race_with_the_telemetry_thread_requeues_the_job_and_continues),
# or calls _run_one() directly rather than through run(). None of them would
# notice self.server.start(), self.folder.load(), or
# self._run_one(job, card=card) being deleted from run()'s body -- verified
# by mutating each in turn (see final-fix-report.md).

class _StoppingFolder(_FakeFolder):
    """Like _FakeFolder, but stops the daemon from inside fold() -- the same
    technique _RaceCards above uses to end run()'s otherwise-unbounded loop
    deterministically -- and, before folding, asserts the UI socket file
    already exists. Checking for the file's existence (rather than
    connecting a client and reading `hello`) keeps this test decoupled from
    the daemon's greeting machinery, which is exercised directly by the
    _hello() tests further below; it only needs to confirm server.start()
    actually ran before any fold does.
    """

    def __init__(self, daemon_holder):
        super().__init__()
        self._daemon_holder = daemon_holder

    def fold(self, job_id, input_path, emit, **kwargs):
        daemon = self._daemon_holder["daemon"]
        assert Path(daemon.config.socket_path).exists(), (
            "the UI socket must already be serving by the time a fold runs")
        try:
            super().fold(job_id, input_path, emit, **kwargs)
        finally:
            daemon.stop()


def test_run_drives_the_real_loop_body_end_to_end(tmp_path, monkeypatch):
    """Drives Daemon.run() itself (not _run_one) through one full, ordinary
    iteration: server.start(), folder.load(), claim a card, fold the
    playlist's one target, then stop. Verified (final-fix-report.md) to go
    red against each of three mutations: deleting self.folder.load(),
    deleting self.server.start(), and replacing
    self._run_one(job, card=card) with `pass`.
    """
    from runner import daemon as mod
    monkeypatch.setattr(mod, "sample_tt_smi", lambda timeout=5.0: [])

    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "trpcage.yaml").write_text("version: 1\n")

    daemon_holder = {}
    folder = _StoppingFolder(daemon_holder)
    daemon = _daemon(tmp_path, folder, _FakeCards())
    daemon_holder["daemon"] = daemon

    # Safety net, not the mechanism under test: if the mutation being
    # checked for is `_run_one(...)` replaced with `pass`, fold() -- and
    # therefore this test's own daemon.stop() call inside it -- never runs.
    # Without an independent bound the loop would then spin forever (the
    # fake CardPool's schedulable() never reflects a claimed-but-never-
    # released card the way the real CardPool would), pegging the CPU
    # instead of failing. A external watchdog, not the fold-triggered stop,
    # is what makes that failure mode finite. Cancelled well before it
    # would ever fire on the passing path (fold() stops the daemon almost
    # immediately), so it costs nothing when nothing is wrong.
    watchdog = threading.Timer(2.0, daemon.stop)
    watchdog.start()
    try:
        daemon.run()
    finally:
        watchdog.cancel()

    assert folder.loads == 1, "the model must actually be loaded before folding"
    assert [t for _j, t, _c in folder.folded] == ["trpcage"], (
        "the daemon must actually have folded the playlist's one target")


# --- Daemon._hello() ------------------------------------------------------
#
# Nothing above ever calls _hello() -- every test constructs its daemon via
# _daemon(), which stubs Daemon._emit but leaves _hello untouched, and then
# either calls _run_one() directly or drives run() without any UI client
# ever connecting (so EventServer's accept loop never calls the
# hello_factory it was given). test_runner_server.py's own tests exercise
# EventServer's hello-calling *mechanism* with a locally defined _hello()
# fixture, never the daemon's real one -- so a bug in Daemon._hello itself
# (e.g. a wrong "version") had zero coverage anywhere in the suite.

def test_hello_reports_the_protocol_version(tmp_path):
    from protocol.events import PROTOCOL_VERSION
    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    daemon._folder_ready = True   # simulate a daemon that has already loaded
    hello = daemon._hello()
    assert hello["type"] == "hello"
    assert hello["version"] == PROTOCOL_VERSION


def test_hello_reports_the_full_card_inventory_not_only_schedulable_cards(tmp_path):
    """A card busy mid-fold must not vanish from a UI's greeting just
    because it is not currently schedulable -- `hello.cards` describes what
    hardware exists, not what happens to be free at this instant (that's
    what card_state events are for). Using a real CardPool (not
    _FakeCards) so this exercises the actual precedence/inventory logic,
    not a test double that could trivially get this right by accident.
    """
    from runner.cards import CardPool

    cards = CardPool([0, 1])
    cards.mark_busy(0)
    daemon = _daemon(tmp_path, _FakeFolder(), cards)
    daemon._folder_ready = True
    hello = daemon._hello()
    assert sorted(hello["cards"]) == [0, 1], (
        "a busy card must still appear in hello's card inventory")


def test_hello_reports_not_ready_before_the_first_successful_load(tmp_path):
    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    assert daemon._hello() == {
        "type": "not_ready",
        "missing": ["device: Folder.load() has not succeeded yet"],
    }


# --- A transient Folder.load() failure ------------------------------------

class _FlakyLoadFolder(_FakeFolder):
    """load() raises for the first `fail_times` calls, then succeeds --
    stands in for a transient condition clearing on retry (e.g. the device
    lease the reviewer verified live: card 0 already held by another
    process).
    """

    def __init__(self, fail_times):
        super().__init__()
        self._fail_times = fail_times

    def load(self):
        self.loads += 1
        if self.loads <= self._fail_times:
            raise RuntimeError("device 0 already leased by another process")


def test_a_transient_folder_load_failure_serves_not_ready_and_retries(
    tmp_path, monkeypatch
):
    """Verified live by the reviewer: with card 0 already leased by another
    process, Folder.load() raising used to propagate straight out of
    run() and kill the daemon with a traceback. An unattended booth cannot
    recover from a dead process on its own -- run() must retry instead.
    """
    from runner import daemon as mod
    monkeypatch.setattr(mod, "sample_tt_smi", lambda timeout=5.0: [])
    monkeypatch.setattr(mod, "LOAD_RETRY_PERIOD_S", 0.01)   # keep the test fast

    folder = _FlakyLoadFolder(fail_times=2)
    daemon = _daemon(tmp_path, folder, _FakeCards())

    assert daemon._hello()["type"] == "not_ready", (
        "before the first successful load, hello must not claim readiness")

    # Bounded externally: once load() succeeds there is an empty playlist,
    # so run() idles (self._stop.wait(10.0)) rather than returning on its
    # own. threading.Event.wait() returns as soon as the event is set, so
    # this ends the test promptly rather than actually waiting out 10s.
    watchdog = threading.Timer(1.0, daemon.stop)
    watchdog.start()
    try:
        daemon.run()   # must not raise
    finally:
        watchdog.cancel()

    assert folder.loads == 3, "two failures, then a successful third load"
    # Closes the gap the previous fix wave left open: this test asserted
    # not_ready *before* the first load and never checked again, so
    # deleting run()'s `self._folder_ready = True` line (which is what
    # actually flips _hello() over to claiming readiness) left the suite
    # green -- a daemon that loads successfully but never sets the flag
    # would serve not_ready to every UI for the rest of the conference,
    # and nothing here would have noticed. Checking _hello() again after
    # run() returns is what makes that deletion visible as a failure.
    assert daemon._hello()["type"] == "hello", (
        "after a successful load, hello must stop claiming not_ready")
