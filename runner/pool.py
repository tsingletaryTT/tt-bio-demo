"""The pool: one worker process per chip, one reader thread per worker.

This is the parent's whole job. One module owns every subprocess and every
reader thread, so "who is holding a device" has exactly one answer -- the
daemon above it (Task 8) keeps the queue, the thermal guard and the socket,
and never touches a chip.

Shape:

- ``start()`` spawns one child per ``WorkerSpec``, each with the environment
  ``runner.workers.worker_environ`` builds for it (its own
  ``TT_VISIBLE_DEVICES``, its own ``TT_METAL_LOGS_PATH``, a host-thread cap
  sized for however many workers this booth runs). The parent hands that
  environment to ``Popen(env=...)``, so it is in place before the child
  interpreter starts -- strictly stronger than "set it before importing
  ttnn".
- One reader thread per worker sits in a blocking ``readline()`` on that
  worker's event fd. ``on_event`` is called **from that thread**, and
  outside this pool's lock, which is what keeps a slow chip -- or a slow
  screen, since ``on_event`` ends in ``EventServer.broadcast`` -- from
  blocking the other three.
- ``dispatch(job, card)`` writes one fold command to one worker. Cards are
  dispatchable only after their worker has announced ``worker.ready`` and
  only while no job is in flight on them.

**Why calling ``on_event`` from four threads at once is safe, and why this
task had to come after Task 4.** ``on_event`` is the daemon's
``EventServer.broadcast``. It never raises into its caller (a UI that
disappears is normal and must not disturb compute), and **as of Task 4** it
holds a dedicated ``_send_lock`` across the whole ``sendall`` loop. Before
Task 4 it copied the client list under ``_lock`` and then sent *outside* it:
``sendall`` is not atomic -- a payload larger than the socket's send buffer
becomes several partial writes -- so two concurrent sends to one client
interleave and split a JSON line in half, and the UI decodes garbage. A
single-fold daemon could never produce that; four workers produce it
routinely. Do not reorder those two tasks, and do not "simplify"
``broadcast`` back to sending outside its lock.

**The multiplexing contract**, which is the whole reason this module exists:

- a line whose ``type`` is in ``protocol.events.EVENT_TYPES`` is passed to
  ``on_event(card, event)`` **unchanged** -- nothing here tags, rewrites or
  re-derives a field. The ``card`` the UI sees is the one the worker put in
  its own ``job_start``; the ``card`` this pool passes alongside is which
  pipe the line came off, which is authoritative in a way a payload field
  can never be.
- a line whose ``type`` starts with ``CONTROL_PREFIX`` is consumed here and
  **never** reaches ``on_event``. ``EventServer.encode`` would raise
  ``ProtocolError`` on one and drop it, so the only symptom of getting this
  wrong is a UI that never hears about something.
- anything else -- undecodable JSON, a JSON scalar, an unknown ``type`` --
  is dropped with a rate-limited log and does **not** kill the reader. A
  dead reader is a chip that silently stops reporting.

**Death (Task 7), which is the other five percent of the time.** A worker's
event stream reaching EOF means that process is gone, however it went. The
pool then, on that worker's own reader thread and touching no other card:

1. marks the card neither ready nor busy, and forgets its handle;
2. if a job was in flight, reports it once through
   ``on_worker_lost(card, job_id, target_id)`` -- the pool holds the whole
   ``Job`` from ``dispatch``, so the daemon never has to look the target up.
   **The pool never fabricates a protocol event**: what the wire sees is
   Task 8's decision, which keeps "who talks to the socket" in one module;
3. respawns after ``restart_delay_s``, unless this card has now died
   ``WORKER_RETIRE_AFTER`` times *consecutively* with no completed job in
   between -- or has already said ``worker.fatal`` -- in which case it is
   retired for the session with a loud log.

Two orderings in there are deliberate and are the opposite of the obvious
one:

- **The card is marked undispatchable BEFORE the loss is reported**, not
  after. Task 8's ``on_worker_lost`` requeues the orphan and then looks for a
  free chip; a card that still looked ready at that moment would be handed
  its own orphaned job straight back into a pipe whose far end is a corpse.
- **``worker.ready`` does not reset the death counter; ``worker.idle``
  does.** A chip in a bad state comes up, announces ready and dies, over and
  over -- resetting on ready would mean it never retires. Only a *completed
  job* (success or failure -- Task 2 emits ``worker.idle`` after the
  try/except either way) is evidence that this chip can still do the work.

What is true throughout, and load-bearing: a worker that dies mid-fold does
not take the booth down, no dead worker is ever handed work, and the other
three chips never notice.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from protocol.events import EVENT_TYPES
# `Job` is used here only as the shape of a card RESERVATION -- `dispatch_egg`
# needs somewhere to record "this card is occupied and here is the id" and a
# second dataclass meaning the same thing would be a second thing
# `_worker_exited` has to know about.
from runner.queue import Job
# The control vocabulary is imported by name rather than re-spelled here, so
# `worker.ready`/`worker.idle`/`worker.fatal` exist in exactly one module.
from runner.workers import (CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY,
                            is_control, worker_environ)

log = logging.getLogger(__name__)

# How long `stop()` waits for a terminated worker to actually go before
# resorting to SIGKILL. Generous: a worker killed mid-fold is mid-device-op,
# and tt-metal's teardown is not instant. One deadline covers all four (see
# `stop`), so booth shutdown does not scale with the number of chips.
WORKER_STOP_GRACE_S = 5.0

# How long the pool waits before putting a new worker on a chip whose last
# one died. Not zero: a chip that fails during device bring-up fails FAST, so
# a zero delay turns one bad card into a spawn loop that takes the tt-metal
# device-init lock several times a second -- which the other three workers
# need every time one of them opens or reopens a device. A few seconds of one
# dark chip is invisible at a booth; a lock-storm across all four is not.
WORKER_RESTART_DELAY_S = 5.0

# How many CONSECUTIVE deaths -- with no completed job in between -- retire a
# card for the rest of the session. Three, because the failure this guards
# against is a chip that is genuinely wedged (tt-bio's device-init notes
# describe a raced "remote-only" bring-up that presents exactly this way), and
# two respawns is enough to tell that from one unlucky fold. Retiring is
# strictly better than respawning forever: a dark chip costs the booth a
# quarter of its throughput, while a chip respawning every few seconds all day
# costs the OTHER three their device-init lock.
WORKER_RETIRE_AFTER = 3

# The largest a single worker's `worker.log` may get before the daemon's
# janitor bounds it (runner/daemon.py's `_prune_logs`).
#
# This file is the one thing under the log root that the PARENT holds open for
# the whole life of a worker (`_SubprocessWorker.__init__` opens it in append
# mode and hands it to `Popen` as stdout/stderr), which makes it the exact
# shape of the failure docs/followups.md already paid for once: tt-metal's
# Inspector held `mesh_workloads_log.yaml` open, the janitor's oldest-first
# sweep unlinked it, and 13-14 MB/s kept flowing into a now-nameless inode
# that no directory walk could see. Unlinking frees a NAME, not blocks. So
# `prune_log_root` is told never to touch these paths, and this cap plus
# `os.truncate` is what bounds them instead -- truncation is the one operation
# that actually returns the blocks while a process holds the fd.
#
# 64 MB per card, so four co-resident workers cost at most 256 MB -- an eighth
# of DEFAULT_LOG_BUDGET_BYTES, which leaves the byte budget still mostly about
# the tt-metal tree it was written for. It is also enormous next to what a
# healthy worker actually writes: the whole point of this file is tt-metal's
# C++ bring-up chatter on fd 1/2, which is measured in kilobytes per worker
# per session (`generated/` was 16-36 KB for a bare open/close). A worker that
# reaches 64 MB is a worker in a repeating-error loop, and the last thing a
# booth needs is that loop filling a tmpfs log root overnight.
WORKER_LOG_CAP_BYTES = 64 * 1024**2

# Minimum seconds between "that line was garbage" log lines, per worker. The
# same reasoning as `EventServer`'s own limiter (runner/server.py's
# `_BadLineLog`): a worker spraying its event fd would otherwise write one
# warning per line into the log root the janitor is trying to hold under a
# budget -- and daemon.log is not what that budget covers. Kept as a separate
# small class rather than imported from runner/server.py: that one is private
# to the server, phrased in terms of a *client* message, and the pool has no
# business importing the socket layer to get a rate limiter.
_BAD_LINE_LOG_INTERVAL_S = 5.0

# The longest fragment of a rejected line that may appear in a log message.
# The bytes on this fd come from tt-metal as much as from our own worker, and
# copying an unbounded line into a log turns loud output into a log-flooding
# vector (the same argument that produced `protocol.events`' own truncation).
_LOG_EXCERPT_CHARS = 120


class _BadLineLog:
    """Rate limiter for one worker's "that line was garbage" messages."""

    def __init__(self, card, interval_s=_BAD_LINE_LOG_INTERVAL_S,
                 clock=time.monotonic):
        self._card = card
        self._interval_s = interval_s
        self._clock = clock
        self._last = None
        self._suppressed = 0

    def record(self, why, line):
        excerpt = line[:_LOG_EXCERPT_CHARS]
        now = self._clock()
        if self._last is not None and now - self._last < self._interval_s:
            self._suppressed += 1
            return
        if self._suppressed:
            log.warning("card %s: ignored a worker line (%s): %r (and %d more "
                        "like it in the last %.0fs)", self._card, why, excerpt,
                        self._suppressed, now - self._last)
        else:
            log.warning("card %s: ignored a worker line (%s): %r",
                        self._card, why, excerpt)
        self._last = now
        self._suppressed = 0


class WorkerPool:
    """Every worker subprocess, and every thread reading one.

    `specs` is one `WorkerSpec` per chip this booth folds on;
    `on_event(card, event)` receives every protocol event, on the reader
    thread that read it. `spawn` is the seam -- a callable `(spec, env) ->
    handle` -- so tests drive fake workers with no subprocess at all; the
    default is a real `python3 -m runner.worker` child. A handle is anything
    with `send(command)`, `readline()`, `terminate()`, `kill()` and `alive`.

    `on_worker_lost(card, job_id, target_id)` is called, on that worker's
    reader thread, for a job that was in flight when its worker died -- once,
    and never for a card that was idle. It is the daemon's business what a UI
    then sees; this module puts nothing on the wire. `restart_delay_s` is how
    long a chip waits for its replacement worker.

    `clock` is injected for the same reason it is on `EventServer`'s bad-line
    limiter: rate limiting and shutdown deadlines are otherwise untestable
    without sleeping. `restart_delay_s` is deliberately NOT measured against
    it: it is waited out on `threading.Event.wait`, which takes real seconds,
    and a test that collapsed the clock would silently turn the delay into a
    busy loop.

    All bookkeeping is private (`_workers`, `_ready`, `_busy`, ...), matching
    `EventServer._clients` / `JobQueue._items` / `CardPool._busy`. Tests
    attach their own handles at `pool.workers` / `pool.spawns` / `pool.lost`,
    and a public attribute of the same name would be silently clobbered by
    the fixture -- leaving a test asserting against state it had overwritten.
    """

    def __init__(self, specs, on_event, *, log_root, spawn=None,
                 on_worker_lost=None, restart_delay_s=WORKER_RESTART_DELAY_S,
                 clock=time.monotonic):
        # Insertion-ordered, so `cards`, `start()` and every log line agree on
        # an order without re-sorting a dict view at each call site.
        self._specs = {spec.card: spec for spec in specs}
        self._on_event = on_event
        # Optional so a caller that only reads events (and every Task 6 test)
        # keeps working. A pool with no `on_worker_lost` still frees, respawns
        # and retires -- it just has nobody to tell about the orphan, which is
        # a reporting gap and not a stuck chip.
        self._on_worker_lost = on_worker_lost
        self._restart_delay_s = restart_delay_s
        self._log_root = log_root
        # The seam is `(spec, env) -> handle`. The production spawn needs one
        # more thing the seam does not carry -- where this booth's logs live --
        # so it is bound here rather than re-derived from the child's own
        # environment: `TT_METAL_LOGS_PATH` is a setdefault an operator may
        # have overridden, and deriving the worker.log path from it would put
        # all four workers' output in one file exactly when someone had done
        # so, silently disagreeing with `worker_log_paths` (which the janitor
        # protects).
        self._spawn = (spawn if spawn is not None
                       else functools.partial(_spawn_subprocess,
                                              log_root=log_root))
        self._clock = clock

        # Re-entrant: `dispatch` and the control handlers both call helpers
        # that take it, and a plain Lock would turn a future nested call into
        # a deadlock rather than a mistake anyone notices.
        self._lock = threading.RLock()
        self._workers = {}        # card -> handle
        self._threads = {}        # card -> reader Thread
        self._ready = {}          # card -> has announced worker.ready
        self._busy = {}           # card -> the Job in flight, or None
        # Consecutive deaths since this card last completed a job, and whether
        # it has been given up on for the session. Both survive a respawn on
        # purpose -- that is the whole point of counting.
        self._deaths = {card: 0 for card in self._specs}
        self._retired = {card: False for card in self._specs}
        self._stopping = threading.Event()

    # -- inventory ---------------------------------------------------------

    @property
    def cards(self):
        """Every card this pool manages, retired ones included.

        The booth's hardware inventory, which is what Task 8's `hello`
        reports: a card that is busy, dead or retired has not stopped
        existing. Deliberately NOT derived from `ready_cards()` -- see
        `CardPool.all_indices`, which exists for the identical reason and had
        the identical bug.
        """
        return sorted(self._specs)

    @property
    def worker_log_paths(self):
        """Where each worker's stdout/stderr goes, one path per card.

        Exposed for the daemon's janitor (Task 11): the parent holds these
        files open for the life of every worker, so the pruner must never
        *unlink* them -- unlinking a file a process holds open removes its
        name and frees nothing, which is exactly the 13-14 MB/s-into-a-
        nameless-inode failure docs/followups.md already measured once.
        """
        return [str(_worker_log_path(self._log_root, card))
                for card in self.cards]

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Spawn one worker per chip and start reading each one.

        A worker that cannot be spawned at all costs that one chip, not the
        booth: it is logged, left not-ready, and the other three start
        normally. Failing closed here would mean one missing device node
        takes the whole demo down.
        """
        with self._lock:
            for spec in self._specs.values():
                if spec.card in self._workers:
                    continue          # already running; never two per chip
                self._spawn_worker(spec)

    def _spawn_worker(self, spec):
        """Spawn one worker and its reader thread. Call with `_lock` held.

        Returns the handle, or None if the spawn itself failed or the card has
        been retired. The retirement check lives here rather than in the two
        callers so that "a retired card never gets another process" has one
        enforcement point: `start()` (a second call, or a restart of a booth
        that retired a chip earlier in the session) and the respawn path both
        pass through it.
        """
        if self._retired.get(spec.card):
            log.info("card %s: retired for this session; not spawning a worker",
                     spec.card)
            return None
        try:
            env = worker_environ(spec, log_root=self._log_root,
                                 n_workers=len(self._specs))
            handle = self._spawn(spec, env)
        except Exception:
            # Deliberately swallowed, deliberately loud. See `start`.
            log.exception("card %s: could not spawn a worker; this chip will "
                          "not fold", spec.card)
            self._ready[spec.card] = False
            self._busy[spec.card] = None
            return None
        self._workers[spec.card] = handle
        self._ready[spec.card] = False
        self._busy[spec.card] = None
        thread = threading.Thread(
            target=self._read_loop, args=(spec.card, handle), daemon=True,
            name=f"worker-reader-card-{spec.card}")
        self._threads[spec.card] = thread
        thread.start()
        log.info("card %s: worker spawned (%s)", spec.card, spec.label)
        return handle

    def stop(self):
        """Ask every worker to exit, then make sure it did.

        Politely first (`terminate()`), and only a worker still alive after
        one shared grace period is killed. Idempotent -- the daemon's own
        shutdown path and a test fixture's teardown both call it, and a
        second call must not escalate a worker that already exited to a
        SIGKILL that never happened.

        Returns only once every reader thread has been joined (bounded), so a
        "stopped" pool is not one still forwarding events to a daemon that
        has torn its socket down.
        """
        self._stopping.set()
        with self._lock:
            workers = dict(self._workers)
            threads = dict(self._threads)
            # No card is dispatchable once stop has begun. This is the half of
            # shutdown that is easy to forget: terminating the processes
            # without clearing readiness leaves `dispatch` willing to write a
            # fold command into a pipe whose far end is a corpse.
            self._ready = {card: False for card in self._specs}

        for card, handle in workers.items():
            try:
                handle.terminate()
            except Exception:
                log.exception("card %s: terminate() raised; will kill it", card)

        self._join_readers(threads.values(), WORKER_STOP_GRACE_S)

        for card, handle in workers.items():
            try:
                if handle.alive:
                    log.warning("card %s: worker still alive after terminate(); "
                                "killing it", card)
                    handle.kill()
            except Exception:
                log.exception("card %s: kill() raised; the chip may still be "
                              "held", card)
        self._join_readers(threads.values(), 1.0)
        still_running = [t.name for t in threads.values() if t.is_alive()]
        if still_running:
            log.warning("reader thread(s) still running after stop(): %s",
                        still_running)

    def _join_readers(self, threads, seconds):
        """Join every reader thread against ONE shared deadline.

        Not one timeout each: shutdown must not scale with the number of
        chips (the same rule `EventServer.stop` follows for its own reader
        threads). Measured against the injected clock, so a test can collapse
        the wait instead of sleeping through it.
        """
        deadline = self._clock() + seconds
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - self._clock()))

    # -- what the daemon asks --------------------------------------------

    def ready_cards(self):
        """Cards whose worker has announced ready and is not busy."""
        with self._lock:
            return sorted(card for card in self._specs
                          if self._ready.get(card) and self._busy.get(card) is None)

    def any_ready(self):
        """Can this booth fold at all?

        True as soon as ONE worker has announced ready, whether or not it is
        currently folding. Deliberately not `bool(self.ready_cards())`: Task
        8 turns this into `hello` vs `not_ready` for every UI that connects,
        and a booth whose chips are all mid-fold is the most working a booth
        ever is. Reporting `not_ready` there would blank the screen for the
        length of every fold.
        """
        with self._lock:
            return any(self._ready.get(card) for card in self._specs)

    def busy_job(self, card):
        """The `job_id` in flight on `card`, or None."""
        with self._lock:
            job = self._busy.get(card)
            return job.job_id if job is not None else None

    def dispatch(self, job, card):
        """Send one fold command to `card`'s worker.

        Raises ValueError if that card is not dispatchable -- not ready yet,
        already folding, stopped, or its worker gone. Raising rather than
        dropping is the point: a silently-swallowed dispatch is a target that
        never folds and never fails, and the daemon's caller requeues on
        exactly this exception (Task 8's dispatch-race test).

        The send happens under `_lock`, which is safe and deliberate. The
        worker being written to is by definition idle and blocked in
        `readline`, and one fold command is a few hundred bytes against a
        64 KiB pipe buffer, so this cannot block for any meaningful time.
        Releasing the lock first would open a window in which a reader thread
        processes this card's death between the readiness check and the
        write.
        """
        command = {"cmd": "fold", "job_id": job.job_id,
                   "target_id": job.target_id, "input_path": job.input_path,
                   "n_residues": job.n_residues}
        self._send(command, job, card)

    def dispatch_egg(self, egg_id, card, *, seed=None):
        """Send one easter-egg command to `card`'s worker (`runner/egg.py`).

        Reserves the card exactly as `dispatch` does -- same readiness gates,
        same `_busy` slot, freed by the same `worker.idle` -- because for the
        second or so this takes, the chip really is occupied and must not also
        be handed a fold. What it does NOT do is invent a Job or a target: the
        reservation carries `target_id=None`, so if this worker dies mid-egg
        the daemon's `on_worker_lost` counts no failure against any playlist
        entry. A toy must never be able to quarantine a protein.

        Raises ValueError on exactly the conditions `dispatch` does, so the
        caller has one exception to catch for "that chip would not take it".
        """
        reservation = Job(job_id=egg_id, target_id=None, input_path=None)
        command = {"cmd": "egg", "egg_id": egg_id, "seed": seed}
        self._send(command, reservation, card)

    def _send(self, command, job, card):
        """Reserve `card` for `job` and write `command` to its worker.

        Shared by `dispatch` and `dispatch_egg` so that the readiness gates,
        the reservation and the undo-on-failure exist once. What differs
        between the two callers is only the command dict and what goes in the
        reservation; every rule about when a card may be written to is the
        same, and a second copy of those rules is a second copy to get wrong.
        """
        with self._lock:
            if self._retired.get(card):
                # Its own message, ahead of the readiness check that would
                # also have caught this: a retired card is never coming back,
                # and the daemon requeueing this job onto another chip wants
                # to know that rather than reading "not ready yet".
                raise ValueError(f"card {card} has been retired for this "
                                 f"session and will not fold again")
            if not self._ready.get(card) or self._busy.get(card) is not None:
                raise ValueError(
                    f"card {card} is not ready for work "
                    f"(ready={bool(self._ready.get(card))}, "
                    f"busy={self.busy_job(card)!r})")
            handle = self._workers.get(card)
            if handle is None:
                raise ValueError(f"card {card} has no worker")
            self._busy[card] = job
            try:
                handle.send(command)
            except Exception as exc:
                # The worker went away between its last control line and this
                # write. Undo the reservation so the imminent EOF does not
                # ALSO report this job as orphaned (the caller is about to
                # hear about it by exception), and hand the caller the one
                # exception type dispatch is documented to raise.
                self._busy[card] = None
                self._ready[card] = False
                log.warning("card %s: sending job %s failed (%s); the worker "
                            "is gone", card, job.job_id, exc)
                raise ValueError(f"card {card}'s worker is gone") from exc
        log.info("card %s: dispatched %s %s (%s)", card, command.get("cmd"),
                 job.job_id, job.target_id)

    # -- the reader side ---------------------------------------------------

    def _read_loop(self, card, handle):
        """Read one worker's event stream until it ends.

        Nothing a worker can put on this fd may end this loop early. The one
        thing that ends it is EOF, which means the process is gone however it
        went -- including the case Task 2 flagged: a worker whose parent
        closed the read end dies of `BrokenPipeError` with no
        `worker.fatal`, because there is nowhere left to send one. "Exited
        without a fatal line" is therefore a death like any other, not a
        special case.
        """
        bad_lines = _BadLineLog(card, clock=self._clock)
        try:
            while True:
                try:
                    line = handle.readline()
                except Exception:
                    log.exception("card %s: reading the worker's event stream "
                                  "failed; treating it as a death", card)
                    break
                if not line:
                    break                     # EOF -- the worker is gone
                line = line.strip()
                if not line:
                    continue
                self._handle_line(card, line, bad_lines)
        except BaseException:
            # A reader that dies leaves a chip that silently stops reporting,
            # so this is logged as the serious thing it is -- and then treated
            # as a death, because a card whose reader is gone must never be
            # handed another job. NOT a `continue`: swallowing an unexpected
            # exception per line would make this loop unable to fail in the
            # one way its tests exist to catch.
            log.exception("card %s: reader thread died unexpectedly", card)
        finally:
            self._worker_exited(card, handle)

    def _handle_line(self, card, line, bad_lines):
        """Route one line: control lines in, protocol events out, junk gone."""
        try:
            event = json.loads(line)
        except ValueError as exc:
            # json.JSONDecodeError is a ValueError. tt-metal is loud and this
            # fd is only nominally ours: a stray C++ log line must cost the
            # line and nothing else.
            bad_lines.record(f"undecodable: {exc}", line)
            return
        if not isinstance(event, dict):
            bad_lines.record("not a JSON object", line)
            return
        if is_control(event):
            self._handle_control(card, event)
            return
        if event.get("type") not in EVENT_TYPES:
            bad_lines.record(f"unknown event type {event.get('type')!r}", line)
            return
        self._forward(card, event)

    def _forward(self, card, event):
        """Hand one protocol event to the daemon, unchanged.

        Called OUTSIDE `_lock`, and that is the property that makes four
        chips independent: `on_event` is `EventServer.broadcast`, which can
        block up to `client_send_timeout` per connected screen. Holding the
        pool's lock across it would let one wedged UI stop every other card's
        control lines -- and therefore every dispatch decision the booth
        makes.
        """
        try:
            self._on_event(card, event)
        except Exception:
            # The daemon's code, running on this thread. An exception escaping
            # it would kill this reader and take the chip silent for the rest
            # of the day, which is far worse than a dropped event.
            log.exception("card %s: on_event raised on a %r event; dropping it",
                          card, event.get("type"))

    def _handle_control(self, card, event):
        """Consume one parent<->worker control line.

        `card` is the pipe this line came off, never `event["card"]`: which
        chip a line came from is a fact about the fd, and a worker that
        mislabels itself must not be able to free another card.
        """
        kind = event.get("type")
        if kind == CONTROL_READY:
            with self._lock:
                self._ready[card] = True
                self._busy[card] = None
            log.info("card %s: worker ready", card)
        elif kind == CONTROL_IDLE:
            with self._lock:
                in_flight = self._busy.get(card)
                self._busy[card] = None
                # A completed job clears the consecutive-death count. "One bad
                # fold followed by a crash" is not a bad chip, and a booth
                # that loses a worker at 09:00 and another at 14:00 must not
                # retire a card that folded a hundred targets in between.
                # `worker.idle` deliberately counts even when the fold FAILED
                # (Task 2 emits it after the try/except either way): what it
                # proves is that this process survived a whole job, which is
                # exactly the thing a wedged chip cannot do.
                self._deaths[card] = 0
            reported = event.get("job_id")
            if in_flight is not None and reported not in (None, in_flight.job_id):
                # Cleared anyway. A card left marked busy forever is a quarter
                # of the booth gone silently, which is worse than acting on a
                # confusing line -- but it is worth a log, because the only
                # way this happens is a bug in the worker or a crossed pipe.
                log.warning("card %s: worker.idle names job %r while %r was in "
                            "flight; freeing the card anyway",
                            card, reported, in_flight.job_id)
        elif kind == CONTROL_FATAL:
            # The worker has said it cannot serve: not dispatchable from this
            # moment, and retired the instant it exits. Respawning it twice
            # more to confirm what it already told us is time the booth would
            # spend at three chips for no information.
            with self._lock:
                self._ready[card] = False
                self._retired[card] = True
            log.error("card %s: worker reported a fatal condition: %s; this "
                      "chip is retired for the session", card,
                      event.get("reason"))
        else:
            log.warning("card %s: ignoring unknown control line %r", card, kind)

    def _worker_exited(self, card, handle):
        """One worker's stream ended: it is gone, however it went.

        Runs on that worker's own reader thread, as the last thing it does.
        Touches this card and nothing else -- there is no loop over the other
        three anywhere in here, and there must never be one: a card-level
        sweep would free (and orphan the jobs of) three chips that are folding
        perfectly well.

        The order below is deliberately NOT the brief's numbering. Marking the
        card undispatchable comes FIRST, before the loss is reported, because
        Task 8's `on_worker_lost` requeues the orphan and then immediately
        looks for a free chip: a card that still looked ready at that instant
        would be handed its own orphaned job right back, into a pipe whose far
        end is a corpse.
        """
        with self._lock:
            if self._workers.get(card) is not handle:
                # A replacement worker is already running on this card. This
                # EOF belongs to the previous one and must not clear the new
                # one's state, report its job as lost, or spawn a third
                # process onto the chip.
                return
            self._ready[card] = False
            orphan = self._busy.get(card)
            self._busy[card] = None
            # Dropped, not kept: between now and the respawn this card has no
            # worker at all, and `dispatch` says so rather than writing into a
            # dead handle. It also makes the identity check above self-arming
            # for any second EOF from this same handle.
            self._workers.pop(card, None)
            self._deaths[card] = self._deaths.get(card, 0) + 1
            deaths = self._deaths[card]
            said_fatal = self._retired.get(card, False)
            retire = said_fatal or deaths >= WORKER_RETIRE_AFTER
            self._retired[card] = retire
        if self._stopping.is_set():
            # The booth is going down. No orphan report (there is nobody left
            # to fold it) and emphatically no respawn: `stop()` has already
            # snapshotted the workers it intends to reap, and a process
            # created after that is one nothing will ever close a device for.
            #
            # The death WAS counted above, which is only correct because a
            # pool is never started again after `stop()` -- the daemon builds
            # one and stops it once. If that ever changes, a restart would
            # find every card carrying four shutdown "deaths" and retire the
            # whole booth on its first real crash.
            log.info("card %s: worker exited during shutdown", card)
            return
        if orphan is None:
            log.warning("card %s: worker exited while idle (death %d); this "
                        "chip is not folding until it is respawned",
                        card, deaths)
        else:
            log.warning("card %s: worker exited with job %s (%s) in flight "
                        "(death %d)", card, orphan.job_id, orphan.target_id,
                        deaths)
            self._report_loss(card, orphan)
        if retire:
            if said_fatal:
                log.error("card %s: RETIRED for the rest of this session -- "
                          "its worker reported a fatal condition and then "
                          "exited. The booth continues on the other cards.",
                          card)
            else:
                log.error("card %s: RETIRED for the rest of this session -- "
                          "%d worker deaths in a row with no completed job in "
                          "between. Something is wrong with this chip; the "
                          "booth continues on the other cards.", card, deaths)
            return
        self._respawn_later(card)

    def _report_loss(self, card, job):
        """Tell the daemon about one job that died with its worker.

        Called OUTSIDE `_lock`, for the same reason `_forward` is: this is the
        daemon's code, and Task 8's handler requeues and may dispatch, which
        comes straight back into this pool.

        The pool does not invent a `job_failed` event to put on the wire. It
        hands the daemon the card, the job and the target it already had from
        `dispatch`, and what a UI sees is Task 8's decision -- so exactly one
        module talks to the socket.
        """
        if self._on_worker_lost is None:
            log.warning("card %s: job %s (%s) was lost with its worker and "
                        "there is no on_worker_lost to report it to",
                        card, job.job_id, job.target_id)
            return
        try:
            self._on_worker_lost(card, job.job_id, job.target_id)
        except Exception:
            # An exception escaping the daemon's handler must not cost this
            # card its respawn -- that would turn one lost job into one dark
            # chip for the rest of the day.
            log.exception("card %s: on_worker_lost raised for job %s; the "
                          "chip will still be respawned", card, job.job_id)

    def _respawn_later(self, card):
        """Wait out the restart delay, then put a new worker on this chip.

        Runs on the dead worker's own reader thread, which has nothing else
        left to do -- no timer thread, no polling loop elsewhere. The wait is
        on `_stopping` rather than `time.sleep` so that booth shutdown is not
        held up by however much of the delay happens to be left.

        Loops only for the case where the spawn ITSELF fails: a failed spawn
        leaves no process, so no EOF will ever bring us back here, and a chip
        that lost its worker to a transient EMFILE would otherwise stay dark
        until the daemon restarted. Each failed attempt counts as a death, so
        this is bounded by `WORKER_RETIRE_AFTER` exactly like a crash loop is.
        """
        while True:
            if self._stopping.wait(self._restart_delay_s):
                return
            with self._lock:
                if self._stopping.is_set():
                    return
                if card in self._workers:
                    # Somebody else already put a worker here. Never two
                    # processes on one chip -- that is the exact contention
                    # `TT_VISIBLE_DEVICES` pinning exists to prevent.
                    return
                if self._retired.get(card):
                    return
                spec = self._specs.get(card)
                if spec is None:                  # not a card we manage
                    return
                log.info("card %s: respawning its worker after %.2fs",
                         card, self._restart_delay_s)
                if self._spawn_worker(spec) is not None:
                    return
                # `_spawn_worker` has already logged the exception.
                self._deaths[card] = self._deaths.get(card, 0) + 1
                deaths = self._deaths[card]
                retire = deaths >= WORKER_RETIRE_AFTER
                self._retired[card] = retire
            if retire:
                log.error("card %s: RETIRED for the rest of this session -- "
                          "its worker could not even be spawned, %d attempts "
                          "in a row. The booth continues on the other cards.",
                          card, deaths)
                return


# ---------------------------------------------------------------------------
# The real worker handle. Everything above this line runs against a fake in
# the tests; everything below opens processes and pipes and is exercised for
# real by the hardware tasks (18/19).

def _worker_log_path(log_root, card):
    """Where card `card`'s worker sends stdout/stderr.

    Inside that card's own `TT_METAL_LOGS_PATH` tree (see
    `workers.worker_environ`), so one chip's crash evidence is in one place
    and an oldest-first prune of another card's tree cannot touch it.
    """
    return Path(log_root).resolve() / f"card-{card}" / "worker.log"


class _SubprocessWorker:
    """One `python3 -m runner.worker` child, holding one chip.

    Three fds matter here and each is deliberate:

    - **stdin** carries fold commands. Closing it is how a worker is asked to
      exit cleanly: `runner.worker.main` drives `iter(sys.stdin.readline, "")`,
      so EOF ends its loop and runs the `finally` that releases the device.
    - **the event fd** is a pipe created here, passed to the child via
      `pass_fds` and named on its command line with `--event-fd`. It is NOT
      renumbered to 3 by `Popen` -- `pass_fds` inherits fds at their own
      numbers -- which is precisely why `runner.worker` takes the number as an
      argument instead of hardcoding `EVENT_FD`.
    - **stdout and stderr** go to one append-mode file per card. Not
      /dev/null: tt-metal writes to fd 1/2 from C++ during device bring-up and
      kernel compilation, and that output is the only diagnostic an operator
      has when a chip fails to come up. The parent holds these files open for
      the life of the worker, which is why the janitor must truncate rather
      than unlink them (Task 11).
    """

    def __init__(self, spec, env, *, log_path, python=None):
        self.spec = spec
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # Append, so a respawn (Task 7) adds to the record instead of erasing
        # the evidence of why the last worker died. O_APPEND is also what
        # makes truncating this file in place correct.
        self._log = open(self._log_path, "a", buffering=1)
        read_fd, write_fd = os.pipe()
        argv = [python or sys.executable, "-m", "runner.worker",
                "--card", str(spec.card), "--event-fd", str(write_fd)]
        try:
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=self._log,
                stderr=subprocess.STDOUT, env=env, pass_fds=(write_fd,),
                close_fds=True, text=True)
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            self._log.close()
            raise
        # The parent must not keep the write end: while any process holds it,
        # the read below never sees EOF, so a dead worker would look like a
        # worker that has simply gone quiet.
        os.close(write_fd)
        self._events = os.fdopen(read_fd, "r", buffering=1)
        self._closed = False

    # -- what the pool calls --

    def send(self, command):
        self._proc.stdin.write(json.dumps(command) + "\n")
        self._proc.stdin.flush()

    def readline(self):
        line = self._events.readline()
        if not line:
            self._reap()
        return line

    def terminate(self):
        """Ask the worker to exit: EOF on stdin first, then SIGTERM.

        Closing stdin is the polite half and the one that lets an idle worker
        exit through its own `finally` and release the device cleanly. SIGTERM
        follows immediately because a worker mid-fold is not reading stdin at
        all and would otherwise sit there until its fold finished -- which at
        booth shutdown can be minutes.
        """
        try:
            if self._proc.stdin is not None and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.terminate()
        except OSError:
            pass

    def kill(self):
        """SIGKILL, and nothing else.

        Deliberately does NOT reap: `kill()` is called from `WorkerPool.stop`,
        on a different thread from the one sitting in `readline()`, and
        closing a buffered reader while another thread is blocked inside it
        waits for that thread's read lock -- so a worker whose stream never
        reaches EOF (its event pipe inherited by a grandchild, say) would
        turn shutdown into a deadlock instead of a warning. The reader thread
        owns the stream and reaps it when its read returns, which SIGKILL
        makes happen; `stop()` joins that thread immediately afterwards.
        """
        try:
            self._proc.kill()
        except OSError:
            pass

    @property
    def alive(self):
        return self._proc.poll() is None

    # -- housekeeping --

    def _reap(self):
        """Close our ends and collect the child, once it is really gone.

        Called when the event stream reaches EOF and after `kill()`. Without
        it, every respawn would leak a pipe, a log file handle and a zombie --
        and Task 7 respawns on every worker death for the length of a
        conference day.
        """
        if self._closed:
            return
        self._closed = True
        for closeable in (self._events, self._log, self._proc.stdin):
            try:
                if closeable is not None:
                    closeable.close()
            except OSError:
                pass
        try:
            self._proc.wait(timeout=WORKER_STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            # It closed its event fd but is still running. The pool's own
            # stop()/respawn path escalates to terminate() and kill(); this
            # is only about not blocking a reader thread forever.
            log.warning("card %s: worker %s still running after its event "
                        "stream ended", self.spec.card, self._proc.pid)


def _spawn_subprocess(spec, env, *, log_root):
    """The production `spawn`: one real worker process for one real chip."""
    return _SubprocessWorker(spec, env,
                             log_path=_worker_log_path(log_root, spec.card))
