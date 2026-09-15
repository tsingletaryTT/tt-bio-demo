"""One Q&A worker process: one reserved chip, one resident nesso1 model,
one affinity question at a time.

Mirrors `runner/worker.py`'s shape exactly -- a thin `main()` around a pure
session class -- for the same reason: `WorkerSession` there proved you can
test the whole command-handling/emit contract with a fake collaborator and a
list of strings, no fds, no subprocess, no device. `QaSession` does the
identical job here for `AffinityScorer` instead of `Folder`.

**Why this is a separate worker process rather than `AffinityScorer.load()`
called in-process inside the daemon.** `docs/followups.md`'s Phase 5 Task 18
entry documents a real, previously-hit deadlock: a process that has opened a
Tenstorrent device cannot safely spawn a child that opens one afterward.
`runner/daemon.py` spawns one fold-worker subprocess per chip; if the
daemon's own process also called `AffinityScorer.load()` in-process, it would
be a process that has opened a device (nesso1's) going on to spawn fold-
worker children -- exactly that shape. Keeping every device open inside a
worker subprocess -- fold or Q&A -- and the daemon itself holding none,
sidesteps the trap entirely rather than getting close to it and hoping the
ordering never matters. This is why `runner/daemon.py` never imports
`AffinityScorer` at all; it only spawns this module, exactly as it spawns
`runner.worker` for a fold, over `runner/pool.py`'s same `WorkerPool`/
`_SubprocessWorker` machinery (see that module's `module=` parameter).

Wire shape. Commands arrive on stdin, one JSON object per line::

    {"cmd": "question", "question_id": ..., "target_id": ..., "input_path": ...}
    {"cmd": "stop"}

Events leave on `EVENT_FD` (never stdout), interleaved with the parent<->
worker control lines exactly as `runner/worker.py`'s do: `CONTROL_READY` once
nesso1 is resident, `CONTROL_IDLE` after every question (answered or failed),
`CONTROL_FATAL` if `load()` itself fails.

**Unlike `runner/worker.py`, there is no SIGTERM handler here.**
`AffinityScorer` has no `close()` method -- confirmed deliberate in
`runner/affinity.py`'s own module docstring: device lifecycle for this
worker is owned by process exit, the same way tt-bio's own
`device_lease` releases its flock when the holding process ends, rather than
through an explicit teardown call this module would need to reach on every
exit path. `runner/worker.py`'s SIGTERM handler exists purely to guarantee a
path to `Folder.close()` even when the process is killed mid-fold; with no
such call to guarantee a path to here, installing one would add a second
signal-handling surface with nothing for it to protect. `runner/pool.py`'s
ordinary shutdown sequence (close stdin, SIGTERM, then SIGKILL after a grace
period if still alive) tears this worker down exactly as cleanly under the
OS's own default disposition as `runner/worker.py` needs a custom one to
reach its `finally`.
"""

import argparse
import json
import logging
import os
import sys

from runner.affinity import AffinityScorer
from runner.workers import (CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY,
                            EVENT_FD, control)

log = logging.getLogger("tt-bio-qa-worker")

# Same meaning and same value as runner/worker.py's FATAL_EXIT_CODE: distinct
# from 0 (clean stop) and 1 (an unhandled traceback), so an operator reading
# the parent's log can tell a worker that announced its own death from one
# that just fell over.
FATAL_EXIT_CODE = 3


class QaSession:
    """Turns a stream of command lines into protocol events for the one chip
    reserved for Q&A. See `runner.worker.WorkerSession` for the identical
    shape and the reasoning behind splitting it out of `main()`.

    `emit` takes protocol events (`answer_start`/`answer_done`/
    `answer_error`); `control_emit` takes parent<->worker control lines. Both
    are read off `self` on every call, not captured at construction, for the
    same reason `WorkerSession` does this: a caller (or a test) can swap
    either afterwards.

    `card` is keyword-only and has no default, for the identical reason
    `WorkerSession.card` has none: a default of 0 is exactly the bug this
    design exists to prevent -- a worker that reports card 3 on the wire
    while answering on chip 0.
    """

    def __init__(self, scorer, emit, control_emit, *, card):
        self.scorer = scorer
        self.emit = emit
        self.control_emit = control_emit
        self.card = card

    def run(self, command_lines):
        """Load nesso1, then serve `command_lines` until stop or EOF.

        Raises SystemExit if the model cannot be loaded at all -- mirrors
        `WorkerSession.run()` exactly, including announcing `CONTROL_FATAL`
        before dying, so the parent's `worker.ready` wait does not hang
        forever on a chip that will never answer anything.
        """
        try:
            self.scorer.load()
        except BaseException as exc:
            # Deliberately BaseException, not Exception -- same reasoning as
            # WorkerSession.run(): a load() killed by a Ctrl-C or a
            # SystemExit raised from inside tt-bio is still a worker that
            # will never become ready, and the parent has to hear about it.
            log.exception("AffinityScorer.load() failed on card %s; this "
                          "worker cannot answer questions", self.card)
            self.control_emit(control(CONTROL_FATAL, card=self.card,
                                      reason=str(exc)))
            raise SystemExit(FATAL_EXIT_CODE) from exc

        # Only now -- the parent treats `worker.ready` as "the device is
        # open, the model is resident, send me a question".
        self.control_emit(control(CONTROL_READY, card=self.card))

        for line in command_lines:
            if not self._handle_line(line):
                break

    def _handle_line(self, line):
        """Handle one command line. Returns False if the worker should stop.

        Same "nothing on this pipe may kill the worker" rule as
        `runner.worker.WorkerSession._handle_line`, for the identical reason:
        a truncated line from a dying parent, or a command from a daemon
        build this worker predates, must cost the line and nothing else.
        """
        line = line.strip() if isinstance(line, str) else line
        if not line:
            return True
        try:
            command = json.loads(line)
        except (TypeError, ValueError):
            log.warning("ignoring malformed command line: %r", line)
            return True
        if not isinstance(command, dict):
            log.warning("ignoring non-object command line: %r", line)
            return True

        cmd = command.get("cmd")
        if cmd == "stop":
            return False
        if cmd == "question":
            self._question(command)
            return True
        log.warning("ignoring unknown command %r", cmd)
        return True

    def _question(self, command):
        """Score one question, then free this worker.

        `AffinityScorer.score()` is documented to never raise -- it catches
        its own failures and emits `answer_error` itself -- but this still
        wraps the call, for the same reason `WorkerSession._fold` keeps a
        backstop beyond `FoldError`: a promise a collaborator makes today is
        not a promise this process should bet the booth's Q&A feature on
        forever.
        """
        question_id = command.get("question_id")
        target_id = command.get("target_id")
        input_path = command.get("input_path")
        try:
            self.scorer.score(question_id, target_id, input_path, self.emit)
        except Exception as exc:
            log.exception("question %s (target %s) raised past score() on "
                          "card %s", question_id, target_id, self.card)
            self.emit({"type": "answer_error", "question_id": question_id,
                       "target_id": target_id, "message": str(exc)})
        # After the try/except, never in a `finally` -- same placement and
        # reasoning as WorkerSession._fold: KeyboardInterrupt/SystemExit pass
        # straight through the handler above, and a worker unwinding through
        # either must not announce itself idle and ready for another
        # question.
        self.control_emit(control(CONTROL_IDLE, card=self.card,
                                  job_id=question_id))


def main(argv=None):
    """Run one Q&A worker: `python3 -m runner.affinity_worker --card N
    --event-fd 3`. Thin on purpose -- everything worth testing is in
    `QaSession`, same split as `runner.worker.main`.
    """
    parser = argparse.ArgumentParser(
        prog="runner.affinity_worker",
        description="Answer affinity questions on one reserved Tenstorrent "
                    "chip; commands on stdin, events on the event fd.")
    parser.add_argument("--card", type=int, required=True,
                        help="physical device index this worker owns")
    parser.add_argument("--event-fd", type=int, default=EVENT_FD,
                        help="inherited fd to write JSON events to "
                             f"(default {EVENT_FD}; never stdout)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format=f"%(asctime)s %(levelname)s %(name)s[card {args.card}]: "
               "%(message)s")

    scorer = AffinityScorer(device_id=args.card)
    # `with`, so the fd is closed on every exit path -- the parent's reader
    # sees a clean EOF instead of hanging on a pipe nobody will ever write to
    # again.
    with os.fdopen(args.event_fd, "w") as stream:
        def write(event):
            # Flushed per line, not per buffer -- same reasoning as
            # runner/worker.py: the parent's dispatch decisions ride on these
            # lines (`worker.idle` frees the card), so a line sitting in a
            # userspace buffer is a chip the booth thinks is still busy.
            stream.write(json.dumps(event) + "\n")
            stream.flush()

        session = QaSession(scorer, write, write, card=args.card)
        # `iter(readline, "")`, not `for line in sys.stdin` -- same reasoning
        # as runner/worker.py: unambiguously line-at-a-time regardless of how
        # the io layer buffers a pipe.
        session.run(iter(sys.stdin.readline, ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
