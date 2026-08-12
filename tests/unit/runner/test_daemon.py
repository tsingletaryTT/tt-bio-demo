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
    monkeypatch.setattr(mod, "prune_log_root",
                        lambda root, budget: (pruned.append(root), (0, []))[1])
    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    daemon._run_one(Job("j1", "t", "/tmp/t.yaml"), card=0)
    assert pruned, "the log budget is never enforced if pruning is not called"


def test_a_pruning_failure_does_not_stop_the_daemon(tmp_path, monkeypatch):
    from runner import daemon as mod

    def explode(root, budget):
        raise OSError("disk gone strange")

    monkeypatch.setattr(mod, "prune_log_root", explode)
    daemon = _daemon(tmp_path, _FakeFolder(), _FakeCards())
    daemon._run_one(Job("j1", "t", "/tmp/t.yaml"), card=0)   # must not raise


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
