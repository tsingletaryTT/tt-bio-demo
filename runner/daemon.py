"""tt-bio-demod: the compute daemon.

Opens the device once, holds the model resident, serves the protocol on a Unix
socket, and folds whatever the queue hands it. The UI is a separate process that
may come and go; nothing here depends on one being connected.

Failure policy (spec §6): a failed fold is logged in full, reported as a
`job_error`, and the loop advances to the next target. A target that fails three
times is quarantined for the session. The daemon does not exit on a fold
failure — an unattended booth needs it to keep trying.
"""

import argparse
import collections
import logging
import os
import signal
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from protocol.events import PROTOCOL_VERSION
from runner.cards import CardPool, sample_tt_smi
from runner.env import log_root_size, prune_log_root, runner_environ
from runner.folder import Folder, FoldError
from runner.preflight import not_ready_event, run_preflight
from runner.queue import Job, JobQueue
from runner.server import EventServer

log = logging.getLogger("tt-bio-demod")

QUARANTINE_AFTER = 3          # consecutive failures before a target is dropped
TELEMETRY_PERIOD_S = 2.0

# How long run() waits between retrying a failed Folder.load() (e.g. the
# device already leased by another process). Matches the "no schedulable
# cards" backoff already used elsewhere in this loop -- long enough not to
# spin a tight retry loop against a condition that needs a human or another
# process to clear, short enough that a transient conflict clearing quickly
# doesn't leave the booth dark for long.
LOAD_RETRY_PERIOD_S = 5.0

# tt-metal wrote 121 MB of Inspector logs for two folds during the spike, and
# caps nothing itself. At a fold every ~45s for a conference day that is tens of
# gigabytes, so the daemon enforces its own budget between folds. 2 GB keeps
# enough recent history to diagnose a failure without threatening the disk.
DEFAULT_LOG_BUDGET_BYTES = 2 * 1024**3

# Task 5b flagged runner/folder.py's STRUCTURES_DIR (now a per-device
# `Folder.structures_dir`) as the same class of problem: every fold writes
# one .cif, forever, with nothing pruning old ones. Measured at ~16 KB for a
# 20-residue target (tests/fixtures/streams/real_fold_trpcage.cif); the
# curated playlist's real targets will run larger, so this budgets by bytes
# rather than by a fold count that would mean something different for every
# target. 200 MB is small next to the 2 GB log budget on purpose -- once the
# UI has read a job_done's cif_path and built its ribbon mesh, nothing in
# this codebase reads that file again (no gallery exists yet; see
# docs/followups.md), so there is little reason to keep more than a handful
# of recent structures around for a human to inspect after the fact.
DEFAULT_STRUCTURES_BUDGET_BYTES = 200 * 1024**2

# How many of the daemon's own most-recently-emitted .cif paths
# _prune_structures refuses to delete, no matter how old they look to
# prune_log_root. Review finding (Task 10): the file a fold *just* wrote is
# never actually at risk from oldest-first pruning -- it is the newest thing
# on disk -- but the one from a fold or two before it can be, and job_done
# is dispatched to the UI over a socket and a GLib.idle_add queued behind
# whatever else is on the GTK main loop; ribbon_from_cif alone measured up
# to ~1.22s on a 3000-residue structure (docs/followups.md). Without this
# floor, once the curated playlist's larger targets make the byte budget
# bind on every fold instead of every few hundred, a structure the UI has
# not gotten around to reading yet could be deleted out from under it. 3
# protects the current fold's own output plus the two before it -- multiple
# whole fold-durations of margin over the ~1.2s worst case above.
PROTECTED_STRUCTURE_COUNT = 3


@dataclass
class DaemonConfig:
    socket_path: str
    weights_dir: str
    playlist_dir: str
    log_root: str
    # Not exposed as a CLI flag. This phase's main() used to accept
    # `--device` and thread it into both CardPool([device_id]) and
    # Folder(device_id=...), but tt_bio.tenstorrent.get_device() takes no
    # device-selecting argument at all -- its own docstring says "Open (or
    # return cached) TT device 0" and the physical chip that maps to is
    # decided by TT_VISIBLE_DEVICES *before ttnn is ever imported*, which
    # for this process happens well before argv is even parsed into a
    # device index (runner/preflight.py's own tap check already imports
    # tt_bio.protenix -- and therefore ttnn -- before a Daemon is
    # constructed at all). The flag was therefore accepted but inert:
    # CardPool would track whichever index an operator passed while the
    # fold always ran on whatever get_device() actually opened, silently
    # decoupling the thermal guard from the hardware doing the work.
    # Deleted rather than "fixed" by threading TT_VISIBLE_DEVICES through,
    # since that needs verifying on real multi-card hardware that ttnn's
    # logical-device-0 mapping lines up with tt-smi's own physical
    # indexing -- not something to get wrong on a shared machine. This
    # phase is card-0 only; the field stays (rather than being deleted too)
    # because Folder and CardPool are already exercised against other
    # values in their own unit tests and take a plain constructor
    # parameter either way -- it is the CLI surface that is card-0 only,
    # not these two classes.
    device_id: int = 0
    max_temp_c: float = 85.0
    log_budget_bytes: int = DEFAULT_LOG_BUDGET_BYTES
    structures_budget_bytes: int = DEFAULT_STRUCTURES_BUDGET_BYTES


class Daemon:
    def __init__(self, config):
        self.config = config
        self.queue = JobQueue()
        self.cards = CardPool([config.device_id], max_temp_c=config.max_temp_c)
        self.server = EventServer(config.socket_path, self._hello)
        self.folder = None
        self._stop = threading.Event()
        self._failures = {}
        self._quarantined = set()
        self._telemetry_thread = None
        # Flips True only after Folder.load() has actually succeeded once.
        # _hello() checks this so a client connecting during the retry
        # window run() now has (see run()'s docstring) gets a `not_ready`
        # greeting instead of a `hello` claiming readiness it doesn't have.
        self._folder_ready = False
        # The last PROTECTED_STRUCTURE_COUNT .cif paths this daemon has
        # actually emitted via job_done, oldest dropped automatically as new
        # ones arrive (deque(maxlen=...)) -- see that constant's comment for
        # why _prune_structures must never delete anything in here.
        self._recent_structures = collections.deque(maxlen=PROTECTED_STRUCTURE_COUNT)
        # run() opens the device; guards against a second call reopening
        # one on an already-closed Folder (see run()'s docstring).
        self._started = False

    def _hello(self):
        if not self._folder_ready:
            # A UI connecting before Folder.load() has ever succeeded (either
            # the very first attempt, or a retry after a transient failure --
            # see run()'s docstring) must not be told the daemon is ready.
            # Shaped like preflight's own not_ready_event() (same "type" and
            # "missing" keys) so the UI's eventual handling of one covers
            # both without a second code path.
            return {"type": "not_ready",
                    "missing": ["device: Folder.load() has not succeeded yet"]}
        return {"type": "hello", "version": PROTOCOL_VERSION,
                # Full inventory, not schedulable() -- a card legitimately
                # busy mid-fold has not stopped existing, and must not
                # vanish from the greeting just because it isn't free at
                # this exact instant. See CardPool.all_indices()'s docstring.
                "cards": self.cards.all_indices(),
                "models": ["protenix-v2"], "preflight": "ok"}

    def _emit(self, event):
        self.server.broadcast(event)

    def _telemetry_loop(self):
        while not self._stop.wait(TELEMETRY_PERIOD_S):
            for event in self.cards.update(sample_tt_smi()):
                self._emit(event)

    def _enqueue_playlist(self):
        # Imported here, not at module scope: tt_bio pulls in torch/ttnn,
        # which this module's own unit tests must not need (same discipline
        # runner/folder.py's load()/_run_fold() already follow).
        from tt_bio.main import _read_bio_chains

        for target in sorted(Path(self.config.playlist_dir).glob("*.yaml")):
            if target.stem in self._quarantined:
                continue
            # n_residues is cosmetic -- job_start carries it purely for the
            # UI's display label -- so a target this daemon cannot even
            # parse must not crash the enqueue loop over it. It will still
            # fail loudly and safely later, inside _run_one's own FoldError
            # handling, the same way a bad target always has; this is
            # best-effort only, and 0 is the same "unknown" default the
            # field already had before this fix.
            n_residues = 0
            try:
                chains = _read_bio_chains(target)
                n_residues = sum(len(seq) for _cid, seq, _msa, mol_type in chains
                                 if mol_type != "ligand")
            except Exception:
                log.warning("could not determine residue count for %s; "
                            "defaulting n_residues to 0", target, exc_info=True)
            self.queue.submit(Job(job_id=uuid.uuid4().hex[:8],
                                  target_id=target.stem,
                                  input_path=str(target), n_residues=n_residues))

    def run(self):
        """Run the fold loop until stop() is called.

        May be called at most once per Daemon instance. The device is opened
        exactly once per daemon lifetime (a global constraint on this whole
        module), and this method is what opens it; a second call would find
        self.folder already closed from the first call's teardown and, since
        Folder.load() is written to reopen after a close(), would silently
        open a second real device on it. Not reachable from main() today
        (it constructs one Daemon and calls run() once) -- this guard exists
        so that stays true if this is ever wired up differently later.

        Folder.load() failing is retried here rather than allowed to
        propagate. Verified live (this fix wave): with card 0 already
        leased by another process, load() raised straight out of this
        method and killed the daemon with a traceback -- systemd would
        restart the unit, but between the crash and that restart the booth
        was a dead socket with no UI to even show a "preparing" screen on.
        A transient lease conflict, a checkpoint download hiccup, anything
        load() can fail on and later succeed at deserves a daemon that
        stays up and keeps trying, the same as a fold failure already gets
        (see the module docstring's failure policy) -- so this loop retries
        with a backoff instead of exiting, and `_hello()` reports
        `not_ready` for as long as no load() attempt has yet succeeded.
        """
        if self._started:
            raise RuntimeError(
                "Daemon.run() may only be called once per instance; "
                "construct a new Daemon to run again")
        self._started = True
        self.server.start()
        try:
            # NOTE (deviation from the brief, safety-critical): the brief
            # unconditionally did `self.folder = Folder(...)` here, which
            # discards whatever Folder a caller already assigned to
            # self.folder (e.g. a test's fake, injected before run()) and
            # replaces it with a fresh real Folder every time run() starts.
            # .load() on *that* then opens an actual Tenstorrent device.
            # Verified the hard way: driving this test's original form of
            # run() opened and closed a real device (nanobind ttnn leak dump
            # and a UMD "Closing user mode device drivers" log appeared in
            # a test run that only ever touched a _FakeFolder). Only
            # construct a real Folder when none has been provided; either
            # way, still call .load() on whichever Folder this is — Folder's
            # own .load() is the thing that's idempotent per-instance, and
            # the fakes track their own load() calls too.
            if self.folder is None:
                self.folder = Folder(device_id=self.config.device_id)
            while not self._stop.is_set():
                try:
                    self.folder.load()
                    break
                except Exception:
                    log.exception(
                        "Folder.load() failed; serving not_ready and "
                        "retrying in %.0fs", LOAD_RETRY_PERIOD_S)
                    self._stop.wait(LOAD_RETRY_PERIOD_S)
            else:
                # The while/else above: this only runs if the loop ended via
                # _stop being set (stop() called during a retry wait) rather
                # than via the `break` on a successful load() -- nothing was
                # ever loaded, so there is nothing to fold. Fall straight
                # through to the shared teardown in `finally` below.
                return
            self._folder_ready = True
            # Stored (rather than fire-and-forget) so the finally below can
            # join it before closing the folder and server: without a
            # handle, shutdown order between "telemetry thread still
            # running" and "device/socket being torn out from under it" was
            # whatever the OS scheduler happened to do, not something this
            # code chose.
            self._telemetry_thread = threading.Thread(
                target=self._telemetry_loop, daemon=True)
            self._telemetry_thread.start()

            while not self._stop.is_set():
                # Spec §6: when no card may take work (all quarantined, or the
                # only card is hot), idle calmly and log loudly rather than
                # folding onto a card we have just decided is unsafe.
                available = self.cards.schedulable()
                if not available:
                    log.error("no schedulable cards; holding off")
                    self._stop.wait(5.0)
                    continue

                job = self.queue.take()
                if job is None:
                    self._enqueue_playlist()
                    if len(self.queue) == 0:
                        log.error("no playlist targets available; idling")
                        self._stop.wait(10.0)
                    continue

                # Claiming the card is a race: the telemetry thread runs
                # update() on a timer, so a card can be quarantined between
                # schedulable() above and mark_busy() here. CardPool raises
                # rather than handing out hot hardware, so catch it, put the
                # job back, and pick again with fresh state.
                card = available[0]
                try:
                    self._emit(self.cards.mark_busy(card))
                except ValueError:
                    log.warning("card %d was quarantined while being claimed; "
                                "requeueing %s", card, job.target_id)
                    self.queue.submit(job)
                    continue

                self._run_one(job, card=card)
        finally:
            # Set unconditionally (not just when stop() was already called
            # externally): if something above raised past the while loop
            # without _stop ever being set, the telemetry thread's own
            # `while not self._stop.wait(TELEMETRY_PERIOD_S)` would otherwise
            # never see a reason to end, and the join below would just sit
            # out its timeout instead of returning promptly.
            self._stop.set()
            if self._telemetry_thread is not None:
                self._telemetry_thread.join(timeout=TELEMETRY_PERIOD_S + 1.0)
            if self.folder is not None:
                self.folder.close()
            self.server.stop()

    def _record_failure(self, target_id):
        """Count one more failure for `target_id`; quarantine at the threshold.

        Shared by both except branches in _run_one: a target that fails is a
        target that fails, whether the exception was FoldError (fold()'s own
        documented contract) or something else entirely (the backstop below).
        """
        count = self._failures.get(target_id, 0) + 1
        self._failures[target_id] = count
        if count >= QUARANTINE_AFTER:
            self._quarantined.add(target_id)
            log.error("target %s failed %d times; quarantined for this session",
                      target_id, count)

    def _emit_and_track(self, event):
        """Forward `event` and, for job_done, remember its cif_path.

        The only reason this exists rather than passing self._emit straight
        into Folder.fold(): _prune_structures needs to know which .cif paths
        the daemon has actually told a UI about recently, so it can refuse
        to delete them (see PROTECTED_STRUCTURE_COUNT's comment). Watching
        job_done here -- the one event Folder.fold() emits with a cif_path
        -- is cheaper and more direct than having Folder.fold() return
        something new or having the daemon re-derive it from
        Folder.structures_dir after the fact.
        """
        if event.get("type") == "job_done":
            cif_path = event.get("cif_path")
            if cif_path:
                self._recent_structures.append(cif_path)
        self._emit(event)

    def _run_one(self, job, card):
        # The card is already claimed by the caller — claiming here would
        # duplicate the busy event and re-open the race the caller guards.
        try:
            self.folder.fold(job.job_id, job.input_path, self._emit_and_track,
                             target_id=job.target_id,
                             n_residues=job.n_residues, card=card)
            self._failures.pop(job.target_id, None)
        except FoldError as exc:
            # Logged in full; the UI gets a neutral notice and moves on.
            log.exception("fold failed for %s", job.target_id)
            self._emit({"type": "job_error", "job_id": job.job_id,
                        "target_id": job.target_id, "message": str(exc)})
            self._record_failure(job.target_id)
        except Exception as exc:
            # Backstop, deliberately separate from the branch above: fold()
            # documents "raises FoldError on failure", but this loop must not
            # bet the whole booth on every collaborator always keeping that
            # promise (see runner/folder.py's fix for one place it didn't:
            # TapUnavailable used to escape fold() directly). Anything that
            # is not BaseException-level control flow (KeyboardInterrupt,
            # SystemExit -- neither of which subclasses Exception, so bare
            # `except Exception` already leaves them alone) gets treated
            # exactly like a fold failure for counting and quarantine
            # purposes, but logged distinctly so an operator can tell "a
            # normal fold failure" apart from "something violated its own
            # contract and needs a real fix".
            log.exception("unexpected (non-FoldError) exception folding %s; "
                          "treating as a fold failure so the daemon keeps going",
                          job.target_id)
            self._emit({"type": "job_error", "job_id": job.job_id,
                        "target_id": job.target_id, "message": str(exc)})
            self._record_failure(job.target_id)
        finally:
            # Guarded like _prune_logs/_prune_structures just below: nothing
            # in this finally may raise out of the fold loop (this method's
            # own contract, same as Folder.fold()'s). Left unguarded until
            # this fix wave -- a bug in CardPool.mark_idle would have
            # escaped here even though the two janitor calls two lines down
            # were already protected.
            try:
                event = self.cards.mark_idle(card)
                if event is not None:
                    self._emit(event)
            except Exception:
                log.exception("cards.mark_idle(%d) raised; the card's state "
                              "on the wire may now be stale", card)
            # Between folds, not during: pruning walks the tree, and the gap
            # between jobs is when nothing is competing for the disk.
            self._prune_logs()
            self._prune_structures()

    def _prune_logs(self):
        """Keep tt-metal's log output inside its budget. Never fatal."""
        try:
            freed, removed = prune_log_root(self.config.log_root,
                                            self.config.log_budget_bytes)
            if removed:
                log.info("log root pruned: %d file(s), %.1f MB freed, now %.1f MB",
                         len(removed), freed / 1e6,
                         log_root_size(self.config.log_root) / 1e6)
        except Exception:
            # A janitor failure must never stop the demo folding.
            log.exception("log pruning failed; continuing")

    def _prune_structures(self):
        """Keep accumulated .cif output inside its budget. Never fatal.

        Same unbounded-growth shape as _prune_logs above, just a different
        root (this Folder's own `structures_dir`, namespaced by device_id --
        see runner/folder.py) and a different (smaller) budget -- see
        DEFAULT_STRUCTURES_BUDGET_BYTES's comment for why the numbers
        differ. Still reuses prune_log_root rather than forking a second
        copy of its deletion logic: "delete oldest regular files under a
        root until it fits a byte budget, without touching a protected set"
        is exactly the same operation here as it is for tt-metal's log root
        -- the `protect` argument (added for this call site) is a property
        of the *files*, not of *this root*, so the underlying mechanism
        still belongs in one place. What's structures-specific is only
        which paths go into `protect`: the daemon's own record of what it
        has recently told a UI about (self._recent_structures, populated by
        _emit_and_track), so a fold or two of GTK-main-loop lag can never
        turn into a deleted-out-from-under-it .cif once real targets make
        the budget bind on every fold instead of every few hundred.
        """
        try:
            freed, removed = prune_log_root(
                self.folder.structures_dir, self.config.structures_budget_bytes,
                protect=set(self._recent_structures))
            if removed:
                log.info("structures pruned: %d file(s), %.1f MB freed",
                         len(removed), freed / 1e6)
        except Exception:
            # A janitor failure must never stop the demo folding.
            log.exception("structure pruning failed; continuing")

    def stop(self):
        self._stop.set()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tt-bio-demod")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--weights", required=True)
    parser.add_argument("--playlist", required=True)
    parser.add_argument("--log-root", required=True)
    # No --device flag: this phase is card-0 only. See DaemonConfig.device_id's
    # comment for why -- get_device() (tt_bio.tenstorrent) has no way to select
    # a specific card, so a flag here would have looked like it worked while
    # silently decoupling CardPool's thermal guard from whatever hardware
    # actually ran the fold. Delete this comment along with adding the flag
    # back if that ever gets fixed at the tt_bio layer, not before.
    parser.add_argument("--max-temp", type=float, default=85.0)
    parser.add_argument("--log-budget-gb", type=float, default=2.0,
                        help="cap on tt-metal's log root; oldest files pruned first")
    parser.add_argument("--structures-budget-gb", type=float, default=0.2,
                        help="cap on accumulated .cif output; oldest pruned first")
    parser.add_argument("--preflight-only", action="store_true",
                        help="check readiness and exit; opens no device")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # NOTE (deviation from the task brief's reference code): the brief called
    # this as `runner_environ(args.log_root, base={})`. That bypasses
    # runner_environ's own "an operator who has already set LOG_ROOT_VAR keeps
    # their choice" guarantee (see runner/env.py) — the guard is a
    # `setdefault` against `base`, and an empty dict never has the operator's
    # real env var in it, so the daemon's own resolved path would always win
    # and silently clobber a deliberately-set TT_METAL_LOGS_PATH once written
    # back with os.environ.update(). Passing no `base` makes runner_environ
    # copy the *real* os.environ, so setdefault actually sees what the
    # operator set, and only the gap gets filled in.
    os.environ.update(runner_environ(args.log_root))

    # The tap check is the most valuable one preflight does — a broken tap means
    # folds succeed while nothing condenses on screen — and it opens no device,
    # so it runs in preflight-only mode too.
    result = run_preflight(args.weights, args.playlist, check_tap=True)
    if args.preflight_only:
        for item in result.missing:
            print(f"missing: {item}")
        print("preflight: ok" if result.ok else "preflight: not ready")
        return 0 if result.ok else 2
    if not result.ok:
        log.error("preflight failed; not starting: %s", result.missing)
        # Still serve, so the UI can show a 'preparing' screen rather than a
        # dead socket and an endless reconnect loop.
        server = EventServer(args.socket, lambda: not_ready_event(result))
        server.start()
        # NOTE (deviation from the brief): the brief waited here with a bare
        # `time.sleep(3600)` loop guarded only by `except KeyboardInterrupt`,
        # which catches SIGINT but not SIGTERM — the signal systemd sends on
        # `stop`/restart. Under that version, SIGTERM kills the process via
        # Python's default handler before the `finally` ever runs, so
        # `server.stop()` (which unlinks the socket file) is skipped. Using
        # the same stop-Event pattern as the ready path below, with both
        # signals wired to it, makes shutdown behave identically regardless
        # of which signal asks for it.
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        try:
            stop.wait()
        finally:
            server.stop()
        return 2

    daemon = Daemon(DaemonConfig(
        socket_path=args.socket, weights_dir=args.weights,
        playlist_dir=args.playlist, log_root=args.log_root,
        max_temp_c=args.max_temp,
        log_budget_bytes=int(args.log_budget_gb * 1024**3),
        structures_budget_bytes=int(args.structures_budget_gb * 1024**3)))
    signal.signal(signal.SIGTERM, lambda *_: daemon.stop())
    signal.signal(signal.SIGINT, lambda *_: daemon.stop())
    daemon.run()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
