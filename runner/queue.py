"""Priority job queue for the runner daemon.

**Two producers, two bands.** `Daemon._enqueue_playlist` submits the whole
curated playlist at priority 0 -- the attract loop -- and
`Daemon.on_client_message` submits a visitor's pick at `VISITOR_PRIORITY`, so
the next chip to come free takes that pick ahead of the entire attract
backlog. Both are live: the multi-chip phase gave the socket a client->server
direction (`PROTOCOL_VERSION` 3, `protocol/events.pick_message`) and the
daemon turns a `pick` into a `Job` here.

**In-flight jobs are never cancelled.** A pick waits at the head of the queue
and takes the next chip to finish; it does not pre-empt. Two reasons, and they
are the whole design decision this queue encodes: tearing down a fold
mid-device-op is a needless source of instability on a booth that already has
four worker processes that can die, and pre-emption is visible destruction --
it blanks a cell some other visitor (possibly the *previous* visitor, whose
pick it was) is watching, which would make the booth less trustworthy exactly
as it got busier. With four chips the wait is bounded by the earliest-finishing
of four folds, not the longest.

Ordering is **higher priority first, submission order within a priority**. The
second half is not decoration: two picks in the same band must be served in
the order they were tapped, so a visitor path that reverses itself under load
is a visitor path that serves the wrong person first.

Historical note, kept because the previous version of this docstring stated
the opposite in the present tense and a docstring that survives the change it
describes is the next reader's wrong mental model: from Phase 3a until the
multi-chip phase the socket ran in one direction only, no producer submitted
above the default band, and this queue behaved as a plain FIFO in production.
That is over -- everything above is present tense on purpose.
"""

import itertools
import threading
from dataclasses import dataclass, field

# The band a visitor's tap is submitted in, against the attract loop's 0.
# Any value above 0 orders correctly; 10 is chosen so that a third band can
# later be slotted *between* the playlist and a visitor (or above a visitor)
# without renumbering either of the two that already exist.
VISITOR_PRIORITY = 10


@dataclass
class Job:
    job_id: str
    target_id: str
    input_path: str
    priority: int = 0
    n_residues: int = 0
    model: str = "protenix-v2"
    meta: dict = field(default_factory=dict)


class JobQueue:
    """Thread-safe: higher priority first, submission order within a priority."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []            # (-priority, seq, job)
        self._seq = itertools.count()

    def submit(self, job):
        with self._lock:
            self._items.append((-job.priority, next(self._seq), job))
            self._items.sort(key=lambda item: (item[0], item[1]))

    def take(self):
        """Remove and return the next job, or None if nothing is waiting."""
        with self._lock:
            if not self._items:
                return None
            return self._items.pop(0)[2]

    def remove(self, job_id):
        """Drop the waiting job with this id. Returns whether one was removed.

        Returns False rather than raising when nothing matches, because the
        only caller races the dispatch loop for the job it is removing:
        `Daemon._accept_pick` decides to replace the pending pick from a
        `pending` snapshot, and `dispatch_once` -- on another thread -- can
        take that very job in the gap before the removal lands. That is not an
        error; it is the previous pick starting to fold, which is a better
        outcome than the one being replaced. It must not raise, because the
        caller runs on a client's reader thread where an exception costs that
        client its socket.

        Only ever removes something still WAITING. A job already handed to a
        chip is out of this list and stays out: nothing here can cancel a fold
        in flight, which is the module docstring's ruling expressed as an
        absence of API.
        """
        with self._lock:
            for position, item in enumerate(self._items):
                if item[2].job_id == job_id:
                    del self._items[position]
                    return True
            return False

    @property
    def pending(self):
        """A snapshot of waiting jobs, in the order they will be taken."""
        with self._lock:
            return [item[2] for item in self._items]

    def __len__(self):
        with self._lock:
            return len(self._items)
