"""The daemon's parts that are not about how many chips there are.

Everything about dispatching to four chips, the greeting, and worker deaths
lives in `test_daemon_multichip.py`. What is left here is what survived the
move to one worker process per chip, rewritten against the new shape:

- the playlist enqueue (unchanged behaviour, unchanged tests);
- the two janitors, which no longer run in `_run_one`'s `finally` (there is no
  `_run_one`, and with four chips folding independently there is no longer a
  moment "between folds") and instead run on a period from `run()`'s loop, and
  which now sweep one structures directory per chip rather than one Folder's;
- `main()`'s CLI contract.

Every daemon here is built by `_daemonfakes._daemon`, so none of them holds a
device, a model or a subprocess.
"""

import sys
import types
from pathlib import Path

from runner.daemon import PROTECTED_STRUCTURE_COUNT, main

from _daemonfakes import _FakePool, _daemon, _run


# --- Daemon._hello() -------------------------------------------------------
#
# The multichip file covers not_ready-vs-hello and the card inventory. This
# one field has nothing to do with multiplicity and everything to do with a UI
# that goes permanently dark on a mismatch (PROTOCOL_VERSION 1 -> 2, no
# retry), so it is pinned on its own.

def test_hello_reports_the_protocol_version(tmp_path):
    from protocol.events import PROTOCOL_VERSION
    daemon = _daemon(tmp_path, _FakePool())
    hello = daemon._hello()
    assert hello["type"] == "hello", "guard: the fake pool is ready by default"
    assert hello["version"] == PROTOCOL_VERSION


# --- the playlist ---------------------------------------------------------

def test_a_quarantined_target_is_not_re_enqueued(tmp_path):
    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "good.yaml").write_text("version: 1\n")
    (playlist / "bad.yaml").write_text("version: 1\n")

    daemon = _daemon(tmp_path, _FakePool())
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
    """job_start carries n_residues purely for the UI's display label; before
    this fix it was always 0, because _enqueue_playlist never set it on the
    Job it submitted. The real count comes from tt_bio's own chain reader,
    summing every non-ligand chain's sequence length, matching
    tests/fixtures/streams/capture_real_fold.py's own formula.
    """
    _fake_tt_bio_main_read_bio_chains(monkeypatch, [
        ("A", "NLYIQWLKDGGPSSGRPPPS", None, "protein"),   # 20 residues
        ("B", "CCD_ATP", None, "ligand"),                  # excluded
    ])
    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "trpcage.yaml").write_text("version: 1\n")

    daemon = _daemon(tmp_path, _FakePool())
    daemon._enqueue_playlist()

    assert [j.n_residues for j in daemon.queue.pending] == [20], (
        "n_residues must count protein/RNA/DNA residues, excluding ligands")


def test_enqueue_playlist_defaults_n_residues_to_zero_on_a_read_failure(tmp_path, monkeypatch):
    """A malformed or unreadable playlist target must not crash the enqueue
    loop: n_residues is cosmetic, and a target this daemon truly cannot parse
    will still fail loudly and safely later, in the worker that tries to fold
    it, the same way a bad target always has.
    """
    _fake_tt_bio_main_read_bio_chains(monkeypatch, ValueError("malformed yaml"))
    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "bad.yaml").write_text("not: [valid\n")

    daemon = _daemon(tmp_path, _FakePool())
    daemon._enqueue_playlist()   # must not raise

    assert [j.n_residues for j in daemon.queue.pending] == [0]


def _fake_tt_bio_main_missing_read_bio_chains(monkeypatch):
    """Install a stand-in tt_bio.main with no _read_bio_chains attribute at
    all -- simulates a tt-bio upgrade that renames or removes the private
    helper _enqueue_playlist imports, which makes the `from tt_bio.main
    import _read_bio_chains` statement itself raise ImportError.
    """
    main_mod = types.ModuleType("tt_bio.main")   # deliberately no _read_bio_chains
    pkg = types.ModuleType("tt_bio")
    pkg.main = main_mod
    monkeypatch.setitem(sys.modules, "tt_bio", pkg)
    monkeypatch.setitem(sys.modules, "tt_bio.main", main_mod)


def test_enqueue_playlist_survives_a_renamed_tt_bio_helper(tmp_path, monkeypatch):
    """A tt-bio upgrade that renames or removes _read_bio_chains must degrade
    _enqueue_playlist to n_residues=0, not take the whole daemon down. Before
    this fix the import sat above _enqueue_playlist's try block, so this exact
    scenario raised ImportError out of _enqueue_playlist and, since run()'s
    loop calls it unguarded, out of run() itself: an unattended booth killed
    by a routine tt-bio version bump.
    """
    _fake_tt_bio_main_missing_read_bio_chains(monkeypatch)
    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "trpcage.yaml").write_text("version: 1\n")

    daemon = _daemon(tmp_path, _FakePool())
    daemon._enqueue_playlist()   # must not raise ImportError

    assert [j.target_id for j in daemon.queue.pending] == ["trpcage"]
    assert [j.n_residues for j in daemon.queue.pending] == [0]


# --- the janitors ---------------------------------------------------------
#
# `structures_dirs` is derived from `runner.folder._structures_dir_for`, which
# names a real path under the system temp directory. Every test below that
# writes files redirects it into tmp_path first -- a unit test that pruned
# /tmp/tt-bio-demo/structures for real could delete a running daemon's output.

def _redirect_structures(monkeypatch, root):
    from runner import daemon as mod
    monkeypatch.setattr(mod, "_structures_dir_for",
                        lambda card: Path(root) / f"device-{card}")


def test_logs_are_pruned_against_the_log_root(tmp_path, monkeypatch):
    """Asserting the log root specifically, not just that pruning happened:
    both janitors call this same prune_log_root, so a `pruned` truthy check
    stayed green with the _prune_logs() call deleted entirely.
    """
    pruned = []
    from runner import daemon as mod
    monkeypatch.setattr(
        mod, "prune_log_root",
        lambda root, budget, protect=None: (pruned.append(root), (0, []))[1])
    daemon = _daemon(tmp_path, _FakePool())
    daemon._prune_logs()
    assert daemon.config.log_root in pruned


def test_a_log_pruning_failure_does_not_stop_the_daemon(tmp_path, monkeypatch):
    """Only _prune_logs's own try/except is on the hook here: the explosion is
    specific to the log root, so this cannot pass on the strength of
    _prune_structures's guard instead.
    """
    from runner import daemon as mod

    def explode_only_for_the_log_root(root, budget, protect=None):
        if root == log_root:
            raise OSError("disk gone strange")
        return (0, [])

    daemon = _daemon(tmp_path, _FakePool())
    log_root = daemon.config.log_root
    monkeypatch.setattr(mod, "prune_log_root", explode_only_for_the_log_root)
    daemon._prune_logs()   # must not raise


def test_every_cards_structures_directory_is_swept(tmp_path, monkeypatch):
    """One directory per chip (they are namespaced by device id), so a sweep
    that only visited the first card's would leave three quarters of the
    booth's .cif output growing forever.
    """
    from runner import daemon as mod

    _redirect_structures(monkeypatch, tmp_path / "structures")
    roots = []
    monkeypatch.setattr(
        mod, "prune_log_root",
        lambda root, budget, protect=None: (roots.append(root), (0, []))[1])
    daemon = _daemon(tmp_path, _FakePool())
    daemon._prune_structures()
    assert sorted(roots) == sorted(
        str(tmp_path / "structures" / f"device-{c}") for c in range(4))


def test_the_structures_prune_protects_each_cards_recently_emitted_paths(
        tmp_path, monkeypatch):
    """job_done's cif_path may not have been read by the UI yet -- it is
    dispatched via GLib.idle_add behind whatever else is queued on the GTK
    main loop, and ribbon_from_cif alone measured up to ~1.22s on a large
    structure -- so prune_log_root must never be told it is free to delete a
    path this daemon has recently emitted.

    And the protection is PER CARD: card 2's recent structures must not be
    offered as protection for card 0's directory, where they do not live.
    """
    from runner import daemon as mod

    _redirect_structures(monkeypatch, tmp_path / "structures")
    calls = {}
    monkeypatch.setattr(
        mod, "prune_log_root",
        lambda root, budget, protect=None: (calls.__setitem__(root, protect),
                                            (0, []))[1])
    daemon = _daemon(tmp_path, _FakePool())
    daemon.on_event(2, {"type": "job_done", "job_id": "j1",
                        "cif_path": "/s/device-2/a.cif", "wall_s": 1.0,
                        "mean_plddt": 95.0})
    daemon._prune_structures()
    assert calls[str(tmp_path / "structures" / "device-2")] == {"/s/device-2/a.cif"}
    assert calls[str(tmp_path / "structures" / "device-0")] == set()


def test_the_protected_structures_set_is_bounded_per_card(tmp_path):
    """self._recent_structures must not grow forever -- it covers a handful of
    folds' worth of GTK-main-loop lag, not every .cif this daemon has ever
    written (which would make the protection swallow the whole budget after a
    long enough run). Bounded per card, not across the booth: a single shared
    bound of three would protect less than one fold each once four chips are
    folding, which is the protection evaporating exactly when it is needed.
    """
    daemon = _daemon(tmp_path, _FakePool())
    for card in (0, 3):
        for n in range(PROTECTED_STRUCTURE_COUNT + 5):
            daemon.on_event(card, {"type": "job_done", "job_id": f"j{n}",
                                   "cif_path": f"/s/{card}/j{n}.cif",
                                   "wall_s": 1.0, "mean_plddt": 95.0})
        assert len(daemon._recent_structures[card]) == PROTECTED_STRUCTURE_COUNT
        assert list(daemon._recent_structures[card]) == [
            f"/s/{card}/j{n}.cif"
            for n in range(5, PROTECTED_STRUCTURE_COUNT + 5)]


def test_a_structures_pruning_failure_on_one_card_does_not_stop_the_others(
        tmp_path, monkeypatch):
    """Per-card guard, not one guard around the whole loop: one chip whose
    structures directory has gone strange must not stop the other three being
    swept for the rest of the day.
    """
    from runner import daemon as mod

    _redirect_structures(monkeypatch, tmp_path / "structures")
    swept = []

    def explode_for_card_zero(root, budget, protect=None):
        if root.endswith("device-0"):
            raise OSError("disk gone strange")
        swept.append(root)
        return (0, [])

    monkeypatch.setattr(mod, "prune_log_root", explode_for_card_zero)
    daemon = _daemon(tmp_path, _FakePool())
    daemon._prune_structures()   # must not raise
    assert sorted(swept) == sorted(
        str(tmp_path / "structures" / f"device-{c}") for c in (1, 2, 3))


def test_structures_pruning_bounds_growth_but_never_deletes_a_recent_one(
        tmp_path, monkeypatch):
    """Not mocked: real files under a real (tmp_path) structures directory,
    with a budget tight enough to bind on every fold -- the concrete "does the
    policy actually work" check, since the shipped default (200 MB against
    ~16 KB trpcage structures) never exercises this path in practice.

    Budget (500 bytes) is deliberately smaller than even one file (1000
    bytes): without protection, prune_log_root's oldest-first sweep would
    delete every file including the newest, because a single file already
    exceeds the budget. With protection, the PROTECTED_STRUCTURE_COUNT most
    recently emitted paths survive regardless -- the root stays above budget
    (the documented correctness-floor-over-budget tradeoff), but nothing the
    daemon has recently told a UI about is ever deleted out from under it.
    """
    _redirect_structures(monkeypatch, tmp_path / "structures")
    device1 = tmp_path / "structures" / "device-1"
    device1.mkdir(parents=True)

    daemon = _daemon(tmp_path, _FakePool(), structures_budget_bytes=500)
    for n in range(1, 11):
        path = device1 / f"s{n}.cif"
        path.write_bytes(b"x" * 1000)
        daemon.on_event(1, {"type": "job_done", "job_id": f"j{n}",
                            "cif_path": str(path), "wall_s": 1.0,
                            "mean_plddt": 95.0})
        daemon._prune_structures()

    # Compared as sets, not sorted lists: "s10.cif" < "s2.cif" as strings,
    # which has nothing to do with which structures are actually recent.
    remaining = {p.name for p in device1.iterdir()}
    expected = {f"s{n}.cif"
                for n in range(10 - PROTECTED_STRUCTURE_COUNT + 1, 11)}
    assert remaining == expected, (
        "exactly the most recently emitted structures must survive an "
        "impossibly tight budget, and nothing older should")
    total = sum((device1 / name).stat().st_size for name in remaining)
    assert total == PROTECTED_STRUCTURE_COUNT * 1000, (
        "the protected floor, not the (unreachable) 500-byte budget, should "
        "be what's left standing")


def test_structures_pruning_still_bounds_a_reachable_budget(tmp_path, monkeypatch):
    """Companion to the impossible-budget case above: when the budget
    comfortably exceeds the protected floor, pruning still does real work --
    older, unprotected files are actually deleted down toward the budget, not
    just down toward the protection floor.
    """
    _redirect_structures(monkeypatch, tmp_path / "structures")
    device1 = tmp_path / "structures" / "device-1"
    device1.mkdir(parents=True)

    # Room for 5 of the 1 KB files -- comfortably more than
    # PROTECTED_STRUCTURE_COUNT (3), so the byte budget binds first.
    daemon = _daemon(tmp_path, _FakePool(), structures_budget_bytes=5000)
    for n in range(1, 11):
        path = device1 / f"s{n}.cif"
        path.write_bytes(b"x" * 1000)
        daemon.on_event(1, {"type": "job_done", "job_id": f"j{n}",
                            "cif_path": str(path), "wall_s": 1.0,
                            "mean_plddt": 95.0})
        daemon._prune_structures()

    remaining = {p.name for p in device1.iterdir()}
    assert remaining == {f"s{n}.cif" for n in range(6, 11)}
    total = sum((device1 / name).stat().st_size for name in remaining)
    assert total <= 5000


def test_the_run_loop_actually_runs_the_janitors(tmp_path, monkeypatch):
    """The one thing that had no coverage after the move. Both janitors used
    to be called from `_run_one`'s finally; `_run_one` is gone, and deleting
    their new call site in run()'s loop leaves every test above green -- they
    all call `_prune_logs` / `_prune_structures` directly -- while the booth
    quietly grows a tt-metal log root without bound for a conference day.
    """
    from runner import daemon as mod

    monkeypatch.setattr(mod, "JANITOR_PERIOD_S", 0.0)   # sweep on the first pass
    _redirect_structures(monkeypatch, tmp_path / "structures")
    roots = []
    monkeypatch.setattr(
        mod, "prune_log_root",
        lambda root, budget, protect=None: (roots.append(root), (0, []))[1])

    holder = {}

    class _OnePass(_FakePool):
        """Ends run()'s loop from inside the pass it is already running --
        the same technique the pre-multi-chip tests used, and the only way to
        drive an otherwise-unbounded loop deterministically. `ready_cards` is
        called by `dispatch_once`, which run() calls before the janitors, so
        exactly one full pass happens.
        """

        def ready_cards(self):
            holder["daemon"].stop()
            return super().ready_cards()

    daemon = _daemon(tmp_path, _OnePass())
    holder["daemon"] = daemon
    _run(daemon)

    assert daemon.config.log_root in roots, "the log budget is never enforced"
    assert str(tmp_path / "structures" / "device-0") in roots, (
        "nor is the structures budget")


# --- main() ---------------------------------------------------------------

def test_main_reports_preflight_failure_and_exits_non_zero(tmp_path, capsys, monkeypatch):
    # run_preflight's tap check imports tt_bio.protenix (and therefore ttnn)
    # and its card-count probe shells out to the real tt-smi binary. Neither
    # is what this test is about -- main()'s CLI wiring and exit-code contract
    # -- and weights_dir/playlist_dir do not exist either way, so preflight
    # fails regardless of what these two report.
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


def test_the_devices_flag_reaches_the_daemon_config(tmp_path, monkeypatch):
    """`--devices` is the CLI half of the field that replaced `device_id`. A
    flag that parses but never reaches DaemonConfig is the exact shape of the
    inert `--device` this phase deleted, so it is pinned end to end.
    """
    from runner import cards as cards_mod
    from runner import daemon as mod
    from runner import preflight as preflight_mod

    monkeypatch.setattr(preflight_mod, "check_tap_supported", lambda: None)
    monkeypatch.setattr(cards_mod, "sample_tt_smi", lambda timeout=5.0: [])
    monkeypatch.setattr(mod, "run_preflight",
                        lambda *a, **k: types.SimpleNamespace(ok=True, missing=[]))
    # main() installs SIGTERM/SIGINT handlers for the daemon it is about to
    # run. Left in place they would outlive this test and take Ctrl-C away
    # from the rest of the session, so the installation itself is stubbed --
    # what is under test here is the config, not the signal wiring.
    monkeypatch.setattr(mod.signal, "signal", lambda *a, **k: None)

    built = {}

    class _CapturingDaemon:
        def __init__(self, config):
            built["config"] = config

        def run(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(mod, "Daemon", _CapturingDaemon)
    code = main([
        "--socket", str(tmp_path / "r.sock"),
        "--weights", str(tmp_path / "weights"),
        "--playlist", str(tmp_path / "playlist"),
        "--log-root", str(tmp_path / "logs"),
        "--devices", "0,2",
    ])
    assert code == 0
    assert built["config"].device_ids == "0,2"


def test_the_devices_flag_defaults_to_every_chip(tmp_path):
    """None, not "0" and not "0,1,2,3": worker_specs passes None straight to
    tt-bio's detection, which is what "use every card present" means there.
    """
    from runner.daemon import DaemonConfig
    assert DaemonConfig(socket_path="s", weights_dir="w", playlist_dir="p",
                        log_root="l").device_ids is None
