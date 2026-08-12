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

# tt-metal wrote 121 MB of Inspector logs for two folds during the spike, and
# caps nothing itself. At a fold every ~45s for a conference day that is tens of
# gigabytes, so the daemon enforces its own budget between folds. 2 GB keeps
# enough recent history to diagnose a failure without threatening the disk.
DEFAULT_LOG_BUDGET_BYTES = 2 * 1024**3


@dataclass
class DaemonConfig:
    socket_path: str
    weights_dir: str
    playlist_dir: str
    log_root: str
    device_id: int = 0
    max_temp_c: float = 85.0
    log_budget_bytes: int = DEFAULT_LOG_BUDGET_BYTES


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

    def _hello(self):
        return {"type": "hello", "version": PROTOCOL_VERSION,
                "cards": self.cards.schedulable(),
                "models": ["protenix-v2"], "preflight": "ok"}

    def _emit(self, event):
        self.server.broadcast(event)

    def _telemetry_loop(self):
        while not self._stop.wait(TELEMETRY_PERIOD_S):
            for event in self.cards.update(sample_tt_smi()):
                self._emit(event)

    def _enqueue_playlist(self):
        for target in sorted(Path(self.config.playlist_dir).glob("*.yaml")):
            if target.stem in self._quarantined:
                continue
            self.queue.submit(Job(job_id=uuid.uuid4().hex[:8],
                                  target_id=target.stem,
                                  input_path=str(target)))

    def run(self):
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
            self.folder.load()
            threading.Thread(target=self._telemetry_loop, daemon=True).start()

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
            if self.folder is not None:
                self.folder.close()
            self.server.stop()

    def _run_one(self, job, card):
        # The card is already claimed by the caller — claiming here would
        # duplicate the busy event and re-open the race the caller guards.
        try:
            self.folder.fold(job.job_id, job.input_path, self._emit,
                             target_id=job.target_id,
                             n_residues=job.n_residues, card=card)
            self._failures.pop(job.target_id, None)
        except FoldError as exc:
            # Logged in full; the UI gets a neutral notice and moves on.
            log.exception("fold failed for %s", job.target_id)
            self._emit({"type": "job_error", "job_id": job.job_id,
                        "target_id": job.target_id, "message": str(exc)})
            count = self._failures.get(job.target_id, 0) + 1
            self._failures[job.target_id] = count
            if count >= QUARANTINE_AFTER:
                self._quarantined.add(job.target_id)
                log.error("target %s failed %d times; quarantined for this session",
                          job.target_id, count)
        finally:
            event = self.cards.mark_idle(card)
            if event is not None:
                self._emit(event)
            # Between folds, not during: pruning walks the tree, and the gap
            # between jobs is when nothing is competing for the disk.
            self._prune_logs()

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

    def stop(self):
        self._stop.set()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tt-bio-demod")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--weights", required=True)
    parser.add_argument("--playlist", required=True)
    parser.add_argument("--log-root", required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--max-temp", type=float, default=85.0)
    parser.add_argument("--log-budget-gb", type=float, default=2.0,
                        help="cap on tt-metal's log root; oldest files pruned first")
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
        device_id=args.device, max_temp_c=args.max_temp,
        log_budget_bytes=int(args.log_budget_gb * 1024**3)))
    signal.signal(signal.SIGTERM, lambda *_: daemon.stop())
    signal.signal(signal.SIGINT, lambda *_: daemon.stop())
    daemon.run()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
