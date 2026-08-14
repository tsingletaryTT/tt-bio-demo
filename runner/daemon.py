"""tt-bio-demod: the compute daemon.

**This process holds no device.** Every fold happens in a worker subprocess --
one per chip, spawned and read by `runner/pool.py` -- and this module keeps
the four things that must have exactly one owner for the whole booth:

- the **queue** (`runner/queue.py`), which decides what folds next;
- the **card pool** (`runner/cards.py`), which decides what may fold at all
  (the 85 C thermal guard, and the busy/idle state the UI dims cells from);
- the **socket** (`runner/server.py`), which is the only thing that talks to a
  UI -- the pool deliberately fabricates no protocol events of its own;
- the **failure/quarantine policy** for *targets*.

It also owns **everything a UI can ask it for** (`on_client_message`), which
is two requests with deliberately different claims on the hardware:

- a **visitor's pick** (`_accept_pick`) becomes a real `Job` at
  `VISITOR_PRIORITY`, so the next chip to come free takes it ahead of the
  whole attract backlog. It goes to the head of the **queue**; it never
  pre-empts a fold in flight -- see `runner/queue.py`'s module docstring for
  why that is a ruling and not an omission. At most `MAX_PENDING_PICKS` picks
  wait at a time and a new one **replaces** the last, which mirrors the single
  `selected_target` the UI tracks and is what bounds a child tapping forty
  targets in ten seconds;
- the **easter egg** (`_dispatch_egg`), whose claim is the weakest one
  available: the next chip that is *already* free, never a fold in flight,
  never a place in the queue at all, and a refusal after `EGG_WAIT_S` if the
  booth is busy. See `dispatch_once`.

Both arrive on a client's reader thread, so `on_client_message` records and
returns, and never raises: an exception there kills that client's reader and
the visitor's UI goes deaf with nothing on screen saying so. Neither request
is special-cased downstream -- once a pick is a `Job`, `dispatch_once` cannot
tell it from an attract job, which is the entire point of the priority queue.

A pick also **wakes the dispatch loop** (`_wake`) rather than waiting out
`run()`'s idle backoff. The failure that guards against is concrete: a pick
landing one millisecond into the empty-playlist backoff is a pick the visitor
waits `EMPTY_PLAYLIST_IDLE_S` for, on top of the fold it must already wait
out.

Failure policy (spec section 6): a failed fold is logged in full by the worker
that ran it, reported to the UI as a `job_error`, and the loop advances to the
next target. A target that fails three times is quarantined for the session
(`QUARANTINE_AFTER`). The daemon does not exit on a fold failure -- an
unattended booth needs it to keep trying.

**Two failure counters, deliberately independent** (the multi-chip plan's
ruling for this task, "because it is easy to get backwards"). A worker that
dies mid-fold counts:

- **one failure for the target** it was folding, here, against
  `QUARANTINE_AFTER` -- a target that reliably kills a worker must eventually
  stop being handed to one; and
- **one death for the card**, in `runner/pool.py`, against
  `WORKER_RETIRE_AFTER` -- a chip that reliably kills workers must eventually
  stop being respawned onto.

Neither is derived from the other, and neither lives in both places. A poison
target must not retire four healthy chips, and a wedged chip must not
quarantine four healthy targets.

**And heat is neither of them.** A card over `max_temp_c` is quarantined by
`CardPool`, which means: it is handed no *new* work (it drops out of
`schedulable()`), the fold already running on it is *not* cancelled, and it
comes back into rotation by itself the moment telemetry says it has cooled.
Heat costs the card no retirement budget and costs no target a failure --
nothing about a hot chip says anything is wrong with the target it happened to
be folding, and retiring a chip for a condition that clears on its own would
take a quarter of the booth off the board for the rest of the day. See
`dispatch_once` and `on_worker_lost`, which are the two places that could get
this backwards.
"""

import argparse
import collections
import logging
import os
import signal
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from protocol.events import PROTOCOL_VERSION
from runner.cards import CardPool, sample_tt_smi
from runner.env import log_root_size, prune_log_root, runner_environ
# NOT `Folder`. This process opens no device and holds no model, and a Folder
# constructed here would be a fifth process's worth of weights and a lease on
# a chip nobody is folding on -- `_structures_dir_for` is a pure path helper
# (tempdir + device id), imported so the janitor below and the worker that
# writes into that directory agree on where it is without copying the path
# into two modules.
from runner.folder import _structures_dir_for
from runner.pool import WorkerPool
from runner.preflight import not_ready_event, run_preflight
from runner.queue import VISITOR_PRIORITY, Job, JobQueue
from runner.server import EventServer
from runner.workers import worker_specs

log = logging.getLogger("tt-bio-demod")

QUARANTINE_AFTER = 3          # consecutive failures before a target is dropped
TELEMETRY_PERIOD_S = 2.0

# How many of a visitor's picks may be waiting in the queue at once. A new
# pick removes the pending one and takes its place.
#
# One, because the UI is one: `ui/gallery.py` tracks a single `selected_target`
# and the booth has a single screen in front of a single person at the rail.
# Queueing every tap instead would let a child tapping forty targets in ten
# seconds put forty folds in front of the attract playlist, and the booth would
# stop being a playlist for the next ten minutes -- for a visitor who has long
# since walked away and whose fortieth tap is the only one they were waiting
# for anyway. Replacing means the newest tap is the one that folds, which is
# what "a visitor's pick starts folding" actually means to the person tapping.
#
# A constant rather than a literal `1` so the replacement rule below reads as
# a policy with a name, and so raising it to 2 (a rail with two people at it)
# is a one-line change rather than a re-reading of `_accept_pick`.
MAX_PENDING_PICKS = 1

# How long run()'s loop waits between dispatch passes. The loop body no longer
# blocks for the length of a fold (that happens in a worker now), so it needs
# a poll interval of its own. Short enough that a chip freeing up is put back
# to work well inside the gap between two frames on screen, long enough that
# an idle booth is not spinning: at 0.25s this is four cheap passes a second
# over a handful of dicts, against folds measured at 4.4-22s.
DISPATCH_POLL_S = 0.25

# How long run() waits before re-globbing a playlist directory that produced
# no schedulable targets (empty, or every target quarantined). Deliberately
# much longer than DISPATCH_POLL_S: this is the "nothing to do and nothing
# will change without a human" path, and it used to be a 10s wait in the
# pre-multi-chip loop for exactly the same reason.
EMPTY_PLAYLIST_IDLE_S = 5.0

# How long run() waits before retrying `worker_specs()` when it cannot work
# out which chips exist. Matches the retry cadence the pre-multi-chip daemon
# used for `Folder.load()`, and exists for the same reason, which was verified
# live rather than imagined: a device-enumeration failure that propagated out
# of run() killed the daemon with a traceback, and between that crash and
# systemd's restart the booth was a dead socket with no UI to even show a
# "preparing" screen on. Retrying keeps `_hello()` answering `not_ready`
# instead.
DEVICE_SCAN_RETRY_S = 5.0

# How often the janitors below sweep. Under the pre-multi-chip daemon they ran
# in `_run_one`'s `finally` -- "between folds, not during, because the gap
# between jobs is when nothing is competing for the disk". With four chips
# folding independently there is no such gap: some chip is always mid-fold, so
# "between folds" has stopped being a moment that exists. A period is what
# replaces it. 30s is short against the budgets involved (2 GB of tt-metal
# logs, 200 MB of structures) and long against a directory walk.
JANITOR_PERIOD_S = 30.0

# tt-metal wrote 121 MB of Inspector logs for two folds during the spike, and
# caps nothing itself. At a fold every ~45s for a conference day that is tens of
# gigabytes, so the daemon enforces its own budget between folds. 2 GB keeps
# enough recent history to diagnose a failure without threatening the disk.
DEFAULT_LOG_BUDGET_BYTES = 2 * 1024**3

# Task 5b flagged runner/folder.py's STRUCTURES_DIR (now a per-device
# `_structures_dir_for`) as the same class of problem: every fold writes one
# .cif, forever, with nothing pruning old ones. Measured at ~16 KB for a
# 20-residue target (tests/fixtures/streams/real_fold_trpcage.cif); the
# curated playlist's real targets will run larger, so this budgets by bytes
# rather than by a fold count that would mean something different for every
# target. 200 MB is small next to the 2 GB log budget on purpose -- once the
# UI has read a job_done's cif_path and built its ribbon mesh, nothing in
# this codebase reads that file again (no gallery exists yet; see
# docs/followups.md), so there is little reason to keep more than a handful
# of recent structures around for a human to inspect after the fact.
#
# The budget is PER CARD, because the directory is: `_structures_dir_for`
# namespaces by device id, so four folding chips are four separate roots and
# an oldest-first sweep of one can never reach into another's.
DEFAULT_STRUCTURES_BUDGET_BYTES = 200 * 1024**2

# How many of each card's most-recently-emitted .cif paths _prune_structures
# refuses to delete, no matter how old they look to prune_log_root. Review
# finding (Phase 3a Task 10): the file a fold *just* wrote is never actually
# at risk from oldest-first pruning -- it is the newest thing on disk -- but
# the one from a fold or two before it can be, and job_done is dispatched to
# the UI over a socket and a GLib.idle_add queued behind whatever else is on
# the GTK main loop; ribbon_from_cif alone measured up to ~1.22s on a
# 3000-residue structure (docs/followups.md). Without this floor, once the
# curated playlist's larger targets make the byte budget bind on every fold
# instead of every few hundred, a structure the UI has not gotten around to
# reading yet could be deleted out from under it. 3 protects the current
# fold's own output plus the two before it -- multiple whole fold-durations
# of margin over the ~1.2s worst case above.
#
# Counted PER CARD (self._recent_structures is a dict of deques, one per
# chip). A single shared deque of 3 across four chips folding concurrently
# would protect less than one fold each, which is the protection this
# constant exists to provide evaporating exactly when the booth got busy.
PROTECTED_STRUCTURE_COUNT = 3

# How long an easter-egg request waits for a chip before the daemon gives up
# and says so (`egg_refused`, reason "busy"). See `_dispatch_egg`.
#
# The number is set by what the booth is usually doing, which is folding on
# every chip it has: the attract loop re-enqueues the playlist whenever the
# queue drains, so `dispatch_once` hands work to every free card four times a
# second and "a chip is idle right now" is the rare state, not the normal one.
# What IS common is a chip freeing up soon -- with four cards and folds
# measured at 4.4-22 s, one finishes every 1-5 s on average. So the egg waits,
# briefly, for the next card to come free rather than refusing on the spot.
#
# 4 s is a bound on how long a visitor stares at a card that has not started
# yet, and it is deliberately SHORTER than the UI's own device-wait timeout
# (`_EGG_DEVICE_WAIT_MS` in ui/app.py) so that the ordinary busy-booth case
# ends with the daemon SAYING it is busy rather than with the UI timing out on
# silence. Those two constants must stay in that order.
#
# 2.5 s was tried first and measured on the running booth: the four chips do
# not free up independently, they free up in LOCKSTEP -- `dispatch_once` hands
# all four the same target within 750 ms of each other, so they finish within
# 750 ms of each other too, and the gap between a press and the next free chip
# is roughly uniform over one fold's length. At 4.3 s per trpcage that made
# 2.5 s a coin flip; the very first live press waited the full 2.5 s and was
# refused with the next chip 750 ms away. 4 s catches most of that window, and
# the fallback covers the rest honestly.
EGG_WAIT_S = 4.0


@dataclass
class DaemonConfig:
    socket_path: str
    weights_dir: str
    playlist_dir: str
    log_root: str
    # Which physical chips this booth folds on, as tt-bio spells it: a comma
    # separated list of device ids ("0,1,2,3"), or None for "every chip
    # detected under /dev/tenstorrent", capped at runner.workers.MAX_WORKERS.
    # Exposed as `--devices` (see main()).
    #
    # This REPLACES the `device_id: int` field, and the long comment that used
    # to sit here explaining why `--device` had been deleted rather than
    # plumbed. That comment was right about its own phase and is now wrong in
    # every particular, so it is rewritten rather than removed -- a reader who
    # remembers the old rule needs to be told what replaced it. What was true:
    # `tt_bio.tenstorrent.get_device()` takes no device-selecting argument, so
    # a `--device` flag in a daemon that folded in-process was inert -- it
    # moved CardPool's thermal bookkeeping onto a card while the fold still
    # ran on whatever chip TT_VISIBLE_DEVICES had already decided, silently
    # decoupling the guard from the hardware doing the work. What is true now:
    # this process does not fold at all. Each chip gets its own worker
    # subprocess, and the parent hands that child a complete environment --
    # including its own TT_VISIBLE_DEVICES -- via Popen(env=...), which is in
    # place before the child interpreter starts, and therefore strictly
    # stronger than "set it before importing ttnn". get_device() opening
    # "device 0" in that child opens the one physical chip made visible to it.
    # So CardPool, the pool, tt-smi's indices and the hardware finally all
    # refer to the same chips, and selecting them from the CLI is meaningful
    # for the first time. Verified on the spike: a worker pinned to chip 1
    # drew 33.0 W mid-fold against 13-17 W idle on chips 0/2/3.
    device_ids: str | None = None
    max_temp_c: float = 85.0
    log_budget_bytes: int = DEFAULT_LOG_BUDGET_BYTES
    structures_budget_bytes: int = DEFAULT_STRUCTURES_BUDGET_BYTES


class Daemon:
    def __init__(self, config):
        self.config = config
        self.queue = JobQueue()
        self.server = EventServer(config.socket_path, self._hello,
                                  on_client_message=self.on_client_message)
        # Built by run() from worker_specs(), or injected by a test before
        # run() is called. None until then -- constructing it here would make
        # merely constructing a Daemon enumerate /dev/tenstorrent and import
        # tt_bio.main (and therefore ttnn and torch), which unit tests must
        # not need and which `--preflight-only` has no business doing either.
        self.pool = None
        self._cards = None
        self._stop = threading.Event()
        # Set whenever something has happened that `run()`'s next pass should
        # act on immediately -- today, a visitor's pick -- and by `stop()`, so
        # shutdown stays as prompt as it was when the loop waited on `_stop`.
        #
        # This exists because of arithmetic rather than tidiness. `run()`'s
        # idle paths wait DISPATCH_POLL_S (0.25s) or EMPTY_PLAYLIST_IDLE_S
        # (5s); a pick landing one millisecond into the latter is five seconds
        # the visitor spends watching a booth do nothing, on top of the fold
        # it must already wait out, and "tapped it and nothing happened for
        # twenty seconds" is a booth that reads as broken however correct the
        # queue is. Waiting on this instead makes both backoffs interruptible
        # without shortening either -- an idle booth still polls at the same
        # cadence, it simply stops sleeping through the one event that matters.
        #
        # Cleared at the TOP of each pass, before the work: a pick that arrives
        # mid-pass then leaves this set, the wait at the bottom returns at
        # once, and the wakeup is not lost.
        self._wake = threading.Event()
        self._failures = {}
        self._quarantined = set()
        self._telemetry_thread = None
        # card -> the last PROTECTED_STRUCTURE_COUNT .cif paths this daemon
        # has actually emitted a job_done for, oldest dropped automatically as
        # new ones arrive. See that constant's comment for why
        # _prune_structures must never delete anything in here, and why the
        # count is per card rather than shared.
        self._recent_structures = collections.defaultdict(
            lambda: collections.deque(maxlen=PROTECTED_STRUCTURE_COUNT))
        # card -> the target_id currently dispatched to it. Kept from the
        # dispatch side rather than read back off the wire: `job_done` carries
        # no target_id (see protocol/events.py), and the daemon needs one to
        # clear that target's failure count on success.
        self._in_flight = {}
        # The easter egg (runner/egg.py), and nothing else about the
        # client->server direction. `_egg_request` is (egg_id, deadline) or
        # None -- at most one waiting at a time, newest wins -- and
        # `_egg_on_card` records which card is running one so a worker death
        # mid-egg is reported as a refused egg rather than as a failed fold.
        # Both are touched from a reader thread (`on_client_message`) and from
        # run()'s own thread (`_dispatch_egg`), hence the lock.
        self._egg_lock = threading.Lock()
        self._egg_request = None
        self._egg_on_card = {}
        # run() spawns worker processes; guards against a second call spawning
        # a second set onto chips the first call's teardown has just released.
        self._started = False

    # -- inventory ---------------------------------------------------------

    @property
    def cards(self):
        """The thermal/busy bookkeeping for every chip this booth folds on.

        Derived, once, from the pool's own inventory (`WorkerPool.cards`),
        because there must be exactly one answer to "which chips are we
        talking about" and the pool is where that decision was made -- a
        CardPool built from a second, independently-computed list is a
        CardPool that can silently disagree with the processes actually
        holding the hardware.

        Lazy rather than built in `__init__` for one concrete reason: the pool
        does not exist until `run()` builds it (or a test injects one), and
        materialising this early would mean either enumerating devices at
        construction time or tracking a card set nothing is folding on.
        Assignable, so a test can substitute its own.
        """
        if self._cards is None:
            if self.pool is None:
                raise RuntimeError(
                    "Daemon.cards is derived from the worker pool's inventory; "
                    "assign Daemon.pool (or call Daemon.run()) first")
            self._cards = CardPool(self.pool.cards,
                                   max_temp_c=self.config.max_temp_c)
        return self._cards

    @cards.setter
    def cards(self, value):
        self._cards = value

    @property
    def structures_dirs(self):
        """Where each card's worker writes its .cif output, one per chip.

        One directory per device (`runner.folder._structures_dir_for`), which
        is what lets `_prune_structures` sweep each independently: an
        oldest-first sweep over a single shared root would delete one chip's
        structures to make room for another's.
        """
        return [str(_structures_dir_for(card))
                for card in self.cards.all_indices()]

    # -- the socket --------------------------------------------------------

    def _hello(self):
        """The greeting every connecting UI receives. Called by EventServer.

        `not_ready` until at least one worker has announced it can fold. This
        is the whole of the spec's "Feasibility" preflight rule at four chips:
        model load stretched from 3.1s solo to 6.4-9.2s under four-way
        contention on the hardware spike, so there is a real, multi-second
        window at startup in which the daemon is up, the socket is accepting
        and nothing can fold yet. A UI connecting in that window must see
        "preparing", not a `hello` promising a booth that cannot yet work.

        `pool.any_ready()` is deliberately busy-INDEPENDENT (Task 6): it is
        true as soon as one worker has announced ready, whether or not every
        chip is currently mid-fold. A booth with all four chips folding is the
        most working a booth ever is, and reporting `not_ready` there would
        blank the screen for the length of every fold.
        """
        if self.pool is None or not self.pool.any_ready():
            # Shaped like preflight's own not_ready_event() (same "type" and
            # "missing" keys) so the UI's handling of one covers both without
            # a second code path.
            return {"type": "not_ready",
                    "missing": ["workers: no chip has finished loading its "
                                "model yet"]}
        return {"type": "hello", "version": PROTOCOL_VERSION,
                # Full inventory, not schedulable() -- a card legitimately
                # busy mid-fold, quarantined for heat, or retired after
                # repeated worker deaths has not stopped existing, and must
                # not vanish from the greeting just because it isn't free at
                # this exact instant. See CardPool.all_indices()'s docstring.
                "cards": self.cards.all_indices(),
                "models": ["protenix-v2"], "preflight": "ok"}

    def _emit(self, event):
        self.server.broadcast(event)

    # -- what a UI asks for -------------------------------------------------

    def on_client_message(self, message, now=None):
        """One decoded client->server message. Runs on that client's reader
        thread (runner/server.py `_reader_loop`).

        **Enqueue and return.** Nothing here folds, dispatches or blocks: a
        `pick` becomes a queued `Job` and an `egg` becomes a recorded request,
        and `run()`'s own thread acts on both. This thread's job is to keep
        reading that client's socket.

        **It never raises**, whatever arrives. The guard wraps the whole body,
        including the `.get` that reads the message type, because the argument
        is not guaranteed to be a dict by anything this method can see -- and
        an exception escaping here kills that client's reader thread, at which
        point the visitor's UI goes deaf with nothing on screen saying so. Same
        shape, and the same reason, as the UI's own GLib callbacks.

        Anything that is not a `pick` or an `egg` is logged and dropped.
        `decode_client_message` has already refused everything malformed and
        everything of an unknown type; this is the second line of that, not the
        first.
        """
        try:
            kind = message.get("type")
            if kind == "pick":
                self._accept_pick(message)
            elif kind == "egg":
                self._accept_egg(message, now)
            else:
                log.info("ignoring client message %r; this daemon acts on "
                         "'pick' and 'egg' only", kind)
        except Exception:
            # Deliberately broad, and deliberately terminal: there is nothing
            # to report back (the client->server direction has no replies) and
            # nothing to retry. What matters is that the reader thread above
            # this frame lives to read the next message.
            log.exception("dropping client message %r; this client's reader "
                          "survives it", message)

    def _accept_pick(self, message):
        """A visitor tapped a target: queue it at `VISITOR_PRIORITY`.

        Three ways a pick is refused, then the one way it is accepted. All of
        the refusals are silent as far as the wire is concerned: the UI
        acknowledged the tap at tap time and does not wait on an answer from
        here.

        1. **It does not name a playlist entry.** Resolution goes through the
           same `sorted(glob("*.yaml"))` enumeration `_enqueue_playlist` walks,
           matching `target_id` against a file *stem* -- never a path join.
           That is a security boundary, not a style preference: `target_id`
           arrives from another process over a socket, and
           `Path(playlist_dir) / f"{target_id}.yaml"` with `../../../etc/passwd`
           in it is a file read somewhere else on the box and, worse, a path
           handed to a folder subprocess. A stem cannot contain a separator, so
           nothing outside the playlist directory can ever match.
        2. **It is quarantined.** `QUARANTINE_AFTER` means this target has
           already failed three times, and a tap does not overrule the guard
           that stopped the booth failing the same fold all afternoon.
        3. **It is already folding.** Queueing nothing is the right answer, not
           a lost request: the UI focuses the cell that is already folding it,
           which is what the visitor asked to see and is faster than a second
           fold of the same thing occupying a second chip to show it twice.
        4. Otherwise it is queued -- and then the *previous* pending pick is
           the thing that goes, not this one. See `MAX_PENDING_PICKS`.
        """
        target_id = message.get("target_id")
        target = self._playlist_target(target_id)
        if target is None:
            log.info("ignoring pick %r: no such target in the playlist",
                     target_id)
            return
        if target_id in self._quarantined:
            log.info("ignoring pick %r: that target is quarantined for this "
                     "session", target_id)
            return
        if target_id in set(self._in_flight.values()):
            log.info("pick %r is already folding; queueing nothing", target_id)
            return
        job = Job(job_id=uuid.uuid4().hex[:8], target_id=target_id,
                  input_path=str(target), priority=VISITOR_PRIORITY,
                  n_residues=self._residue_count(target))
        # Take a snapshot, then remove by id. `pending` is a copy, so the
        # dispatch loop may take one of these between the two -- which is why
        # `JobQueue.remove` answers False instead of raising. Nothing is lost
        # either way: a pick that has already been dispatched is a pick that
        # is folding.
        waiting = [j for j in self.queue.pending if j.priority == VISITOR_PRIORITY]
        for stale in waiting[:max(0, len(waiting) - MAX_PENDING_PICKS + 1)]:
            if self.queue.remove(stale.job_id):
                log.info("pick %s replaces %s, which never got a chip",
                         target_id, stale.target_id)
        self.queue.submit(job)
        # AFTER the submit, never before: a loop woken by a pick that is not
        # in the queue yet does a pass' worth of nothing and goes back to
        # sleep for the length of the backoff this exists to interrupt.
        self._wake.set()
        log.info("pick %s queued as job %s at priority %d",
                 target_id, job.job_id, VISITOR_PRIORITY)

    def _accept_egg(self, message, now=None):
        """A visitor pressed the chord: record a request for the next free chip.

        An egg never enters the job queue and never becomes a `Job`. It is a
        request that expires on its own (`EGG_WAIT_S`); see `_dispatch_egg` for
        what happens either way.
        """
        egg_id = message.get("egg_id")
        deadline = (time.monotonic() if now is None else now) + EGG_WAIT_S
        with self._egg_lock:
            # Newest wins, and the one it replaces is simply forgotten. A
            # visitor pressing the chord twice means "do it again", and the UI
            # keys its frames on `egg_id`, so an older request that later got
            # a chip would draw into a viewer that is no longer showing it.
            if self._egg_request is not None:
                log.info("egg %s replaces %s, which never got a chip",
                         egg_id, self._egg_request[0])
            self._egg_request = (egg_id, deadline)
        log.info("egg %s requested; waiting up to %.1fs for a free chip",
                 egg_id, EGG_WAIT_S)

    def _playlist_target(self, target_id):
        """The playlist file whose stem is exactly `target_id`, or None.

        The ONLY way a client-supplied id becomes a path in this daemon. See
        `_accept_pick` for why a path join is not an acceptable alternative
        here. Deliberately the same enumeration `_enqueue_playlist` uses, so a
        target a visitor can pick and a target the attract loop folds are the
        same set by construction rather than by two lists agreeing.
        """
        if not isinstance(target_id, str) or not target_id:
            return None
        for path in sorted(Path(self.config.playlist_dir).glob("*.yaml")):
            if path.stem == target_id:
                return path
        return None

    # -- what the pool reports ---------------------------------------------

    def on_event(self, card, event):
        """One protocol event from one worker. Runs on that worker's reader
        thread (runner/pool.py `_forward`), outside the pool's lock.

        Every event is forwarded to the UI unchanged -- this daemon adds no
        fields and rewrites none; `card` here is which pipe the line came off,
        which the pool guarantees and a payload field never could.

        The bookkeeping around the forward is the daemon's alone: `job_start`
        claims the card so the UI can light that cell, `job_done`/`job_error`
        release it and settle the target's failure count. Both halves are
        guarded so that a bug in the bookkeeping costs the bookkeeping and not
        the event, which is the thing the screen actually needs.
        """
        kind = event.get("type")
        # NOTHING HERE CLEARS `_egg_on_card`, and that is deliberate rather
        # than an omission. An earlier revision cleared it on the last
        # `egg_frame`, on `egg_refused` and on `job_start`, reasoning that a
        # stale entry would make a later fold's death read as a refused egg.
        # Mutation testing showed the clear could be deleted outright with
        # nothing going red, and the reason is solid: the only reader compares
        # the entry against the `job_id` that just died, egg ids are uuids,
        # and no fold can ever carry one. The dict is one entry per card,
        # overwritten by the next egg on that card, so it cannot grow either.
        # Untestable defensive code on the path every fold's every frame takes
        # is worse than no code, so it is gone.
        if kind == "job_start":
            # The worker's own statement of what it is folding. `dispatch_once`
            # already recorded this, and the two agree -- but a `job_done`
            # carries no target_id (protocol/events.py), so this is the value
            # that later clears that target's failure count, and taking it from
            # the wire as well means the record is right even for a card whose
            # job this daemon did not itself dispatch.
            if event.get("target_id"):
                self._in_flight[card] = event["target_id"]
            try:
                # Emitted BEFORE the job_start it explains, so a UI never sees
                # a fold begin on a cell it still believes is idle.
                self._emit(self.cards.mark_busy(card))
            except ValueError:
                # CardPool refuses to mark a quarantined card busy. Reachable
                # for real: the telemetry thread can quarantine a chip in the
                # gap between dispatch and the worker announcing it started.
                # The fold is NOT cancelled for that (see the module
                # docstring) -- the card simply stays reported as quarantined,
                # which is the precedence CardPool already documents.
                log.warning("card %s went hot between dispatch and job_start; "
                            "reporting it quarantined rather than busy", card)
            except Exception:
                log.exception("card %s: claiming the card for %r failed",
                              card, event.get("job_id"))

        self._emit(event)

        if kind in ("job_done", "job_error"):
            try:
                self._job_finished(card, event, failed=(kind == "job_error"))
            except Exception:
                log.exception("card %s: settling %r left this card's state "
                              "stale on the wire", card, event.get("job_id"))

    def _job_finished(self, card, event, *, failed):
        """A fold on `card` ended: settle the target's count, free the card."""
        target_id = event.get("target_id") or self._in_flight.get(card)
        self._in_flight.pop(card, None)
        if not failed:
            cif_path = event.get("cif_path")
            if cif_path:
                self._recent_structures[card].append(cif_path)
            if target_id is not None:
                # A target that works is a target with no history: two
                # failures then a success must reset the count, not creep
                # toward QUARANTINE_AFTER over a whole conference day.
                self._failures.pop(target_id, None)
        else:
            self._record_failure(target_id)
        idle = self.cards.mark_idle(card)
        if idle is not None:
            # None means "still hot": the card stays reported as quarantined
            # rather than idle, and stays out of schedulable() until a later
            # telemetry sample sees it cool.
            self._emit(idle)

    def on_worker_lost(self, card, job_id, target_id=None):
        """A worker died with `job_id` in flight. Runs on a pool reader thread.

        Three things, and it must survive all three failing, because an
        exception here kills that worker's reader thread and the chip goes
        silent for the rest of the day. The pool wraps this call too, but a
        guard on the caller's side is not a licence to raise: the pool would
        also skip the respawn it schedules after this returns.

        1. **A `job_error` on the wire.** Without it the UI sits in `folding`
           forever: it was told a job started and is never told it ended. The
           pool deliberately fabricates no protocol events, so this is the
           only place that report can come from.
        2. **The target's failure count.** A worker death is one failure for
           the target -- a target that reliably kills a worker must eventually
           be quarantined. It is counted BEFORE the card is released, because
           `mark_idle` is the one call here that can realistically raise
           (that is exactly what this method's own test drives), and losing
           the accounting that decides quarantine to a CardPool bug would mean
           a poison target never stops being handed out.
        3. **The card, marked idle.** Its retirement is the pool's business
           and not counted here (module docstring): the two counters are
           independent and neither may be derived from the other.

        The orphaned job is deliberately NOT requeued. The attract loop
        re-enqueues the playlist whenever the queue drains, so an ordinary
        target comes back around on its own within a pass -- and immediately
        resubmitting the one target that has just killed a worker is how a
        crash loop gets built out of a policy that was meant to survive one.
        """
        try:
            with self._egg_lock:
                was_egg = self._egg_on_card.get(card) == job_id
                if was_egg:
                    self._egg_on_card.pop(card, None)
            if was_egg:
                # A `job_error` here would be a lie in the one direction that
                # matters: the UI's `job_error` branch takes down the "now
                # folding X" caption, and no fold was running. The egg says it
                # was refused, the UI falls back to its own CPU descent, and
                # the label it puts up says CPU -- which is then true.
                log.warning("card %s: worker died running egg %s", card, job_id)
                self._emit({"type": "egg_refused", "egg_id": job_id,
                            "reason": "device"})
                # No `mark_idle`: an egg never marked the card busy in
                # CardPool in the first place (only `job_start` does that, and
                # an egg emits none), so there is nothing here to undo. The
                # pool's own reservation is cleared by `_worker_exited`, which
                # is what actually decides whether this chip gets more work.
                return
            log.warning("card %s: worker died with job %s (%s) in flight",
                        card, job_id, target_id)
            self._emit({"type": "job_error", "job_id": job_id,
                        "target_id": target_id,
                        "message": "the worker holding this chip exited "
                                   "mid-fold"})
            self._in_flight.pop(card, None)
            self._record_failure(target_id)
            idle = self.cards.mark_idle(card)
            if idle is not None:
                self._emit(idle)
        except Exception:
            log.exception("card %s: reporting the loss of job %s raised; the "
                          "card's state on the wire may now be stale",
                          card, job_id)

    def _record_failure(self, target_id):
        """Count one more failure for `target_id`; quarantine at the threshold."""
        if target_id is None:
            # A worker that died before it ever told us what it was folding.
            # Nothing to count it against; the card's own death budget in
            # runner/pool.py still applies.
            return
        count = self._failures.get(target_id, 0) + 1
        self._failures[target_id] = count
        if count >= QUARANTINE_AFTER:
            self._quarantined.add(target_id)
            log.error("target %s failed %d times; quarantined for this session",
                      target_id, count)

    # -- the dispatch decision ---------------------------------------------

    def dispatch_once(self):
        """One pass: give a job to every chip that may take one, right now.

        Extracted from `run()`'s loop on purpose, exactly as `_run_one` was
        before it, so the whole scheduling decision is testable without an
        unbounded loop, worker processes or a clock.

        **A card must clear two independent gates.** `CardPool.schedulable()`
        knows about heat and about which cards are already reported busy;
        `WorkerPool.ready_cards()` knows whether a process is alive on that
        chip, has finished loading its model, and has no job in flight. Both
        have to agree: a chip at 91 C with a perfectly healthy worker must get
        nothing, and so must a cool chip whose worker died thirty seconds ago.

        Note what this does NOT do: it does not mark the card busy in
        `CardPool`. Dispatch reserves the card in the *pool* (which is what
        stops a second job going to the same chip in this same pass), and the
        card is reported busy on the wire only once its worker announces the
        fold has actually started -- see `on_event`. Two facts, two owners.

        **The easter egg goes first, and `ready_cards()` is read afterwards.**
        That order is the whole of the egg's scheduling policy and it is
        deliberately the weakest possible claim on the hardware: the egg is
        offered the next card that is ALREADY free, it never pre-empts a fold
        in flight, it never jumps the job queue (the queue is untouched -- the
        job that would have gone to this card simply goes to the next one, or
        on the next pass), and if no card is free it waits `EGG_WAIT_S` and
        then gets nothing. The playlist is an infinite attract loop, so the
        cost of an egg is at most one chip busy for ~1.5 s -- not a visitor's
        protein, which is the line this must not cross.
        """
        self._dispatch_egg()
        # AFTER the egg, never before: `dispatch_egg` reserves a card in the
        # pool, and a `ready` set snapshotted first would still list it.
        ready = set(self.pool.ready_cards())
        # A snapshot, taken once: schedulable() is recomputed from mutable
        # state and iterating it live while dispatching into it is how a pass
        # ends up skipping a chip.
        for card in self.cards.schedulable():
            if card not in ready:
                continue
            job = self.queue.take()
            if job is None:
                return                      # nothing left to hand out
            try:
                self.pool.dispatch(job, card)
            except ValueError:
                # The card stopped being dispatchable between `ready_cards()`
                # above and the send -- its worker died, or was retired, in
                # that window. The pool has already cleared its own
                # reservation and reported the loss exactly once (by this
                # exception, never also as an orphan), so the only thing left
                # to do is not lose the job. Continue rather than break: one
                # dead chip must not cost the other three this pass.
                log.warning("card %s refused job %s (%s); requeueing it",
                            card, job.job_id, job.target_id)
                self.queue.submit(job)
                continue
            self._in_flight[card] = job.target_id

    # -- the easter egg's share of the hardware ----------------------------

    def _take_egg_request(self, egg_id):
        """Clear the pending request, but only if it is still `egg_id`.

        Guarded on identity because a second press can arrive between this
        pass reading the request and acting on it, and clearing blindly would
        drop the NEWER request -- which is the one the visitor is waiting for.
        """
        with self._egg_lock:
            if self._egg_request is not None and self._egg_request[0] == egg_id:
                self._egg_request = None

    def _free_card_for_egg(self):
        """The first chip that could take an egg right now, or None.

        The same two independent gates `dispatch_once` uses for a fold, in the
        same order and for the same reasons: `CardPool.schedulable()` knows
        about heat, `WorkerPool.ready_cards()` knows whether a live worker is
        idle on that chip. An egg is not special enough to skip either -- in
        particular it must not be handed a chip that is out of rotation at
        91 C, which is exactly the sort of exception a "it's only a toy"
        argument would make and which would put load on the one chip the
        thermal guard is trying to protect.
        """
        if self.pool is None:
            return None
        ready = set(self.pool.ready_cards())
        for card in self.cards.schedulable():
            if card in ready:
                return card
        return None

    def _dispatch_egg(self, now=None):
        """Give a waiting easter egg the next free chip, or refuse it.

        Returns the card it went to, or None. Never raises into `run()`'s
        loop: an egg is a toy, and a bug in it must not be able to stop the
        booth folding.
        """
        with self._egg_lock:
            request = self._egg_request
        if request is None:
            return None
        egg_id, deadline = request
        now = time.monotonic() if now is None else now
        card = self._free_card_for_egg()
        if card is None:
            if now < deadline:
                return None                 # still waiting for a chip
            self._take_egg_request(egg_id)
            log.info("egg %s: every chip is folding; refusing it", egg_id)
            # "busy", not "device": the UI puts a different sentence on screen
            # for each, and the difference between "the booth is working" and
            # "something went wrong" is the whole reason there are two.
            self._emit({"type": "egg_refused", "egg_id": egg_id,
                        "reason": "busy"})
            return None
        self._take_egg_request(egg_id)
        try:
            self.pool.dispatch_egg(egg_id, card)
        except Exception:
            # Same window `dispatch_once` guards for a fold: the card stopped
            # being dispatchable between `ready_cards()` and the write. There
            # is nothing to requeue -- an egg is not a job -- so the visitor
            # is told, and the UI falls back to the CPU with the CPU label.
            log.warning("card %s refused egg %s; telling the UI", card, egg_id,
                        exc_info=True)
            self._emit({"type": "egg_refused", "egg_id": egg_id,
                        "reason": "busy"})
            return None
        with self._egg_lock:
            self._egg_on_card[card] = egg_id
        log.info("egg %s running on card %s", egg_id, card)
        return card

    # -- the loop ----------------------------------------------------------

    def _telemetry_once(self):
        """One tt-smi sample, folded into CardPool, with every state change
        put on the wire.

        Extracted from the loop below for the same reason `dispatch_once` was
        extracted from `run()`: it is the whole of the thermal decision, and a
        test of it should not have to sleep out a `TELEMETRY_PERIOD_S`. The UI
        dims a quarantined chip from these events -- if one never leaves, a
        chip at 91 C looks healthy on screen for the rest of the day.
        """
        for event in self.cards.update(sample_tt_smi()):
            self._emit(event)

    def _telemetry_loop(self):
        while not self._stop.wait(TELEMETRY_PERIOD_S):
            try:
                self._telemetry_once()
            except Exception:
                # Telemetry is a guard, not the demo. A tt-smi that starts
                # answering strangely must not end the thread that watches
                # temperature for the rest of the day.
                log.exception("telemetry sample failed; continuing")

    def _residue_count(self, target):
        """Best-effort residue count for a playlist file. 0 if unknowable.

        n_residues is cosmetic -- job_start carries it purely for the UI's
        display label -- so a target this daemon cannot even parse must not
        crash the caller over it. It will still fail loudly and safely later,
        in the worker, the same way a bad target always has; this is
        best-effort only, and 0 is the same "unknown" default the field
        already had.

        The import lives inside the try, not at module or method scope: tt_bio
        pulls in torch/ttnn, which this module's own unit tests must not need
        -- but the try/except is what makes a *renamed* private helper degrade
        to 0 instead of an ImportError killing run()'s whole loop
        (`_enqueue_playlist` is called unguarded from there) or a client's
        reader thread (`_accept_pick` runs on one).
        """
        try:
            from tt_bio.main import _read_bio_chains
            chains = _read_bio_chains(target)
            return sum(len(seq) for _cid, seq, _msa, mol_type in chains
                       if mol_type != "ligand")
        except Exception:
            log.warning("could not determine residue count for %s; "
                        "defaulting n_residues to 0", target, exc_info=True)
            return 0

    def _enqueue_playlist(self):
        for target in sorted(Path(self.config.playlist_dir).glob("*.yaml")):
            if target.stem in self._quarantined:
                continue
            self.queue.submit(
                Job(job_id=uuid.uuid4().hex[:8], target_id=target.stem,
                    input_path=str(target),
                    n_residues=self._residue_count(target)))

    def _build_pool(self):
        """Work out which chips exist and build the pool that holds them.

        Retries rather than propagating, for the reason
        DEVICE_SCAN_RETRY_S documents: an unattended booth that dies of a
        device-enumeration failure is a dead socket, and `_hello()` reports
        `not_ready` for as long as this has not succeeded. Returns False if
        `stop()` was called while retrying.
        """
        while not self._stop.is_set():
            try:
                specs = worker_specs(self.config.device_ids)
            # Broad on purpose, exactly as the Folder.load() retry this
            # replaced was -- NOT just `WorkerSpecError`. That type covers what
            # worker_specs chooses to wrap (no chips detected, a device id that
            # is not there), but the ways this can fail on a booth machine are
            # not limited to it: a driver mid-reload, a tt-bio import that has
            # gone wrong. Every one of them is better served by a daemon that
            # stays up answering not_ready than by a traceback and a dead
            # socket with a UI reconnecting to nothing.
            except Exception:
                log.exception("could not determine which chips to fold on; "
                              "serving not_ready and retrying in %.0fs",
                              DEVICE_SCAN_RETRY_S)
                self._stop.wait(DEVICE_SCAN_RETRY_S)
                continue
            self.pool = WorkerPool(specs, self.on_event,
                                   log_root=self.config.log_root,
                                   on_worker_lost=self.on_worker_lost)
            log.info("folding on %d chip(s): %s", len(specs),
                     ", ".join(f"{s.card} ({s.label})" for s in specs))
            return True
        return False

    def run(self):
        """Run the booth until stop() is called.

        May be called at most once per Daemon instance: this is what spawns
        the worker processes, and a second call would put a second set onto
        chips whose devices the first call's teardown has only just released.

        A pool already assigned to `self.pool` is used as-is and never
        replaced -- that is how a test drives this loop without spawning a
        single process, and the same discipline the pre-multi-chip run()
        needed for `Folder` after it was found opening a real device in a test
        that only ever touched a fake.
        """
        if self._started:
            raise RuntimeError(
                "Daemon.run() may only be called once per instance; "
                "construct a new Daemon to run again")
        self._started = True
        self.server.start()
        try:
            if self.pool is None and not self._build_pool():
                return          # stopped while still trying to find the chips
            if self._stop.is_set():
                # Asked to shut down before a single worker was spawned.
                # Spawning them now would put four processes onto four chips
                # with nothing left to close their devices -- the same rule
                # runner/pool.py follows when a respawn is scheduled during
                # shutdown. `finally` still stops the (empty) pool below.
                return
            # Materialise the card pool on THIS thread, before the telemetry
            # thread below can be the first to touch the lazy property.
            self.cards
            self.pool.start()
            # Stored (rather than fire-and-forget) so the finally below can
            # join it before tearing the workers and the socket out from under
            # it: without a handle, shutdown order was whatever the OS
            # scheduler happened to do, not something this code chose.
            self._telemetry_thread = threading.Thread(
                target=self._telemetry_loop, daemon=True)
            self._telemetry_thread.start()

            next_prune = time.monotonic() + JANITOR_PERIOD_S
            while not self._stop.is_set():
                # Before the work, not after it: anything that arrives while
                # this pass runs re-sets `_wake`, the wait at the bottom
                # returns immediately, and the next pass sees it. Clearing
                # after the work would swallow exactly those wakeups.
                self._wake.clear()
                idle = False
                if len(self.queue) == 0:
                    self._enqueue_playlist()
                    if len(self.queue) == 0:
                        # Every target quarantined, or an empty playlist
                        # directory. Nothing here will change without a human.
                        log.error("no playlist targets available; idling")
                        idle = True
                self.dispatch_once()
                if time.monotonic() >= next_prune:
                    next_prune = time.monotonic() + JANITOR_PERIOD_S
                    self._prune_logs()
                    self._prune_structures()
                # `_wake`, not `_stop`: same two numbers, both now
                # interruptible. `stop()` sets `_wake` as well, so shutdown is
                # exactly as prompt as it was when this waited on `_stop`, and
                # a pick no longer has to sit out a backoff it landed one
                # millisecond into. The loop condition above is still what
                # decides whether to go round again.
                self._wake.wait(EMPTY_PLAYLIST_IDLE_S if idle
                                else DISPATCH_POLL_S)
        finally:
            # Set unconditionally (not just when stop() was already called
            # externally): if something above raised past the while loop
            # without _stop ever being set, the telemetry thread's own
            # `while not self._stop.wait(...)` would otherwise never see a
            # reason to end, and the join below would just sit out its timeout.
            self._stop.set()
            if self._telemetry_thread is not None:
                self._telemetry_thread.join(timeout=TELEMETRY_PERIOD_S + 1.0)
            if self.pool is not None:
                # Every worker, every chip. A daemon that exits leaving a
                # worker holding a device is a chip nobody can fold on until
                # someone finds the process.
                self.pool.stop()
            self.server.stop()

    def stop(self):
        self._stop.set()
        # BOTH, and in this order. run()'s idle waits are on `_wake` now, so a
        # stop that set only `_stop` would be noticed no sooner than the end
        # of the current backoff -- up to EMPTY_PLAYLIST_IDLE_S of a systemd
        # stop/restart spent doing nothing, with four worker processes still
        # holding four chips.
        self._wake.set()

    # -- the janitors ------------------------------------------------------

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
        """Keep each card's accumulated .cif output inside its budget.

        Same unbounded-growth shape as _prune_logs above, just a different set
        of roots (one per chip -- see `structures_dirs`) and a different,
        smaller budget applied to each. Still reuses prune_log_root rather
        than forking a second copy of its deletion logic: "delete oldest
        regular files under a root until it fits a byte budget, without
        touching a protected set" is exactly the same operation here. What is
        structures-specific is only which paths go into `protect`: this
        daemon's own record of what it has recently told a UI about
        (`self._recent_structures[card]`), so a fold or two of GTK-main-loop
        lag can never turn into a deleted-out-from-under-it .cif once real
        targets make the budget bind on every fold instead of every few
        hundred.

        Never fatal, per card: one chip whose structures directory has gone
        strange must not stop the other three being swept.
        """
        for card, root in zip(self.cards.all_indices(), self.structures_dirs):
            try:
                freed, removed = prune_log_root(
                    root, self.config.structures_budget_bytes,
                    protect=set(self._recent_structures[card]))
                if removed:
                    log.info("card %s: structures pruned: %d file(s), "
                             "%.1f MB freed", card, len(removed), freed / 1e6)
            except Exception:
                log.exception("card %s: structure pruning failed; continuing",
                              card)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tt-bio-demod")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--weights", required=True)
    parser.add_argument("--playlist", required=True)
    parser.add_argument("--log-root", required=True)
    # Spelled the way tt-bio's own CLI spells it, and passed through to
    # tt_bio.runtime.detect_tenstorrent_devices UNVALIDATED by us, so a typo
    # ("--devices 7" on a four-card box) becomes that function's own clear
    # error naming the ids actually present, rather than a silently smaller
    # booth. Default: every chip detected, capped at runner.workers.MAX_WORKERS.
    parser.add_argument("--devices", default=None, metavar="IDS",
                        help="comma-separated chip ids to fold on "
                             "(default: every detected chip)")
    parser.add_argument("--max-temp", type=float, default=85.0)
    parser.add_argument("--log-budget-gb", type=float, default=2.0,
                        help="cap on tt-metal's log root; oldest files pruned first")
    parser.add_argument("--structures-budget-gb", type=float, default=0.2,
                        help="cap on each chip's .cif output; oldest pruned first")
    parser.add_argument("--preflight-only", action="store_true",
                        help="check readiness and exit; opens no device")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # NOTE (deviation from the original task brief's reference code): the brief
    # called this as `runner_environ(args.log_root, base={})`. That bypasses
    # runner_environ's own "an operator who has already set LOG_ROOT_VAR keeps
    # their choice" guarantee (see runner/env.py) -- the guard is a
    # `setdefault` against `base`, and an empty dict never has the operator's
    # real env var in it. Passing no `base` makes runner_environ copy the
    # *real* os.environ, so setdefault actually sees what the operator set.
    os.environ.update(runner_environ(args.log_root))

    # The tap check is the most valuable one preflight does -- a broken tap means
    # folds succeed while nothing condenses on screen -- and it opens no device,
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
        # Both signals wired to one stop Event: a bare `except KeyboardInterrupt`
        # catches SIGINT but not the SIGTERM systemd sends on stop/restart, and
        # under that version the `finally` (which unlinks the socket file) never
        # ran at all.
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
        device_ids=args.devices,
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
