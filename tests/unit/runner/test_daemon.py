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
        self.quarantine_on_claim = quarantine_on_claim
        self.busy_calls = []
        self.idle_calls = []

    def schedulable(self):
        return list(self._available)

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


def test_logs_are_pruned_after_a_job(tmp_path, monkeypatch):
    pruned = []
    from runner import daemon as mod
    monkeypatch.setattr(
        mod, "prune_log_root",
        lambda root, budget, protect=None: (pruned.append(root), (0, []))[1])
    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    daemon._run_one(Job("j1", "t", "/tmp/t.yaml"), card=0)
    assert pruned, "the log budget is never enforced if pruning is not called"


def test_a_pruning_failure_does_not_stop_the_daemon(tmp_path, monkeypatch):
    from runner import daemon as mod

    def explode(root, budget, protect=None):
        raise OSError("disk gone strange")

    monkeypatch.setattr(mod, "prune_log_root", explode)
    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
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
    from runner import daemon as mod

    def explode(root, budget, protect=None):
        raise OSError("disk gone strange")

    monkeypatch.setattr(mod, "prune_log_root", explode)
    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
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
