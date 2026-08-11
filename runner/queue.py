"""Priority job queue for the runner daemon.

A visitor's pick is submitted at a higher priority than the attract loop's own
jobs, so it is taken next by whichever card frees up first. In-flight jobs are
never cancelled: with four cards and sub-minute folds the wait is imperceptible,
and tearing down a fold mid-device-op is a needless source of instability.
"""

import itertools
import threading
from dataclasses import dataclass, field


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

    @property
    def pending(self):
        """A snapshot of waiting jobs, in the order they will be taken."""
        with self._lock:
            return [item[2] for item in self._items]

    def __len__(self):
        with self._lock:
            return len(self._items)
