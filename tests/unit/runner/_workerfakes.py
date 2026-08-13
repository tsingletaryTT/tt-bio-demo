"""Fakes for driving `runner.pool.WorkerPool` with no subprocess at all.

NOT a test file itself (no `test_*` names here, so pytest's default
`python_files` pattern never collects it -- the same convention
`tests/unit/_legibility.py` already uses). Imported by
`tests/unit/runner/test_worker_pool.py` (Task 6) and
`tests/unit/runner/test_worker_death.py` (Task 7): one fake worker, shared,
so the two files cannot drift into two subtly different ideas of what a
worker handle does.

`WorkerPool`'s `spawn` seam is a callable `(spec, env) -> handle`, and the
handle protocol is exactly five members: `send(command)`, `readline()`,
`terminate()`, `kill()`, and the `alive` property. `_FakeWorker` implements
that protocol over a list of lines the TEST writes by hand, which is what
lets a test drive the pool's schedule deliberately -- emit a control line,
emit junk, reach EOF mid-fold -- rather than hoping a real worker happens to
do the interesting thing while the test is watching.
"""

import json
import threading
import time

from runner.queue import Job
from runner.workers import WorkerSpec


def _spec(card):
    """One WorkerSpec, shaped like what `worker_specs` builds on this box.

    `mesh_graph_descriptor` is set (not None) on purpose: every chip on the
    quietbox is detected as a P300 and therefore gets the 1x1 MGD attached
    (Task 1's implementer note), so the fake specs match the real ones.
    """
    return WorkerSpec(card=card, label=f"quietbox:tt{card}",
                      visible_devices=str(card), logical_device_id=0,
                      mesh_graph_descriptor="/mgd/p150.textproto")


class _FakeWorker:
    """A worker handle whose event stream the test writes by hand."""

    def __init__(self, spec, env):
        self.spec = spec
        self.env = env
        self.commands = []
        self.terminated = False
        self.killed = False
        self._lines = []
        self._cv = threading.Condition()
        self._eof = False

    # -- what the pool calls --
    def send(self, command):
        self.commands.append(command)

    def readline(self):
        with self._cv:
            while not self._lines and not self._eof:
                self._cv.wait(timeout=2.0)
            return self._lines.pop(0) if self._lines else ""

    def terminate(self):
        self.terminated = True
        self.die()

    def kill(self):
        self.killed = True
        self.die()

    @property
    def alive(self):
        return not self._eof

    # -- what the test calls --
    def emit(self, obj):
        with self._cv:
            self._lines.append(json.dumps(obj) + "\n")
            self._cv.notify_all()

    def emit_raw(self, text):
        with self._cv:
            self._lines.append(text)
            self._cv.notify_all()

    def die(self):
        with self._cv:
            self._eof = True
            self._cv.notify_all()

    @property
    def drained(self):
        """True once the reader has taken every line this fake was given.

        Used as a GUARD by tests that assert something did NOT happen (a junk
        line that must not have been forwarded, say): without it, "the
        callback saw nothing" is equally true of a pool that dropped the line
        correctly and of a reader thread that had not gotten to it yet.
        """
        with self._cv:
            return not self._lines


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _job(job_id="j1", target_id="trpcage"):
    return Job(job_id=job_id, target_id=target_id,
               input_path=f"/p/{target_id}.yaml", n_residues=20)
