"""One worker process: one chip, one resident model, one fold at a time.

This is the piece that actually folds. Today's daemon does all of this
in-process on a single card; multi-chip folding turns that loop into four of
these, one child process per chip, because tt-bio's device model is one
process per chip: ``tt_bio.tenstorrent.get_device()`` always opens *logical*
device 0, and ``TT_VISIBLE_DEVICES`` -- which has to be set before ttnn is
imported -- is what decides which physical chip that is. The parent hands
each child a complete environment via ``Popen(env=...)`` (see
``runner/workers.py``'s ``worker_environ``), so those variables are in place
before the child interpreter even starts.

The split in this module is deliberate and is the whole reason it can be
tested without a device:

- ``WorkerSession`` is pure plumbing -- a Folder, two emit callables, and an
  iterable of command lines. No fds, no argv, no subprocess. Everything this
  module has to get *right* lives here, where a fake Folder and a list of
  strings can drive it.
- ``main`` is deliberately thin: parse ``--card``/``--event-fd``, build a
  real ``Folder``, wrap the inherited fd, and drive a ``WorkerSession`` from
  stdin.

Two things this module pointedly does NOT do:

- It does not touch fd 1 or 2. tt-bio has a ``_silence_subprocess_output``
  helper that redirects a child's stdout/stderr to /dev/null; calling it here
  would throw away the only diagnostic an operator has when a chip fails to
  come up. Where fd 1 and 2 go is the *parent's* decision (it owns the
  per-worker log files and their size budget), not the worker's.
- It does not rewrite protocol events. Whatever ``Folder.fold`` emits goes
  onto the wire byte-for-byte, with no worker-added fields -- ``card`` is
  already carried by ``job_start`` because the daemon passes ``card=`` into
  ``fold()``, and this worker passes its own. A worker that decorated its
  events would be a worker whose events the UI has to learn about.

Wire shape. Commands arrive on stdin, one JSON object per line::

    {"cmd": "fold", "job_id": ..., "target_id": ..., "input_path": ...,
     "n_residues": ...}
    {"cmd": "stop"}

Events leave on ``EVENT_FD`` (never stdout), one JSON object per line,
flushed per line, interleaved with the parent<->worker control lines from
``runner/workers.py`` (``worker.ready`` / ``worker.idle`` / ``worker.fatal``).
The parent separates the two with ``is_control`` -- a control line's ``type``
is deliberately outside ``protocol.events.EVENT_TYPES``.
"""

import argparse
import json
import logging
import os
import sys

from runner.folder import Folder, FoldError
from runner.workers import (CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY,
                            EVENT_FD, control)

log = logging.getLogger("tt-bio-worker")

# Exit status for "this worker cannot serve and is saying so on the way out".
# Distinct from 0 (clean stop) and from 1 (an unhandled traceback) so an
# operator reading the parent's log can tell a worker that reported its own
# death from one that just fell over.
FATAL_EXIT_CODE = 3


class WorkerSession:
    """Turns a stream of command lines into protocol events for one card.

    `emit` takes protocol events (`protocol.events.EVENT_TYPES`); `control_emit`
    takes parent<->worker control lines. They are two callables rather than one
    with a flag because the parent does genuinely different things with them:
    protocol events are forwarded to every connected UI, control lines never
    leave the daemon. Both are read off `self` on every call, not captured at
    construction, so a caller (or a test) can swap either afterwards.

    `card` is keyword-only and has no default on purpose. A default of 0 is
    exactly the bug this whole design exists to prevent -- a worker that
    reports card 3 on the wire while folding on chip 0 -- and the failure
    mode is silent, so the constructor refuses to guess.
    """

    def __init__(self, folder, emit, control_emit, *, card):
        self.folder = folder
        self.emit = emit
        self.control_emit = control_emit
        self.card = card

    def run(self, command_lines):
        """Load the model, then serve `command_lines` until stop or EOF.

        Returns normally on a clean stop (a `{"cmd": "stop"}` line) or on end
        of input -- the parent dying closes our stdin, and a worker that kept
        running then would sit on a chip nobody can reach (a stray tt-bio
        worker once pinned /dev/tenstorrent/3 for two hours this way).

        Raises SystemExit if the model cannot be loaded at all: there is no
        useful work this process can ever do afterwards, and a worker that
        exited *silently* would leave the parent waiting on a `worker.ready`
        that is never coming. So the death is announced (CONTROL_FATAL) and
        only then does the process go.
        """
        try:
            try:
                self.folder.load()
            except BaseException as exc:
                # Deliberately BaseException, not Exception: a load() killed
                # by a Ctrl-C or a SystemExit raised from inside tt-bio is
                # still a worker that will never become ready, and the parent
                # has to hear about it either way. The original exception is
                # chained, and the `finally` below still releases the device.
                log.exception("Folder.load() failed on card %s; worker cannot "
                              "serve", self.card)
                self.control_emit(control(CONTROL_FATAL, card=self.card,
                                          reason=str(exc)))
                raise SystemExit(FATAL_EXIT_CODE) from exc

            # Only now. The parent treats `worker.ready` as "the device is
            # open, the model is resident, send me a job" -- announcing it any
            # earlier means the first dispatched job races model loading.
            self.control_emit(control(CONTROL_READY, card=self.card))

            for line in command_lines:
                if not self._handle_line(line):
                    break
        finally:
            # The one thing that must happen no matter how this method ends,
            # including on the KeyboardInterrupt/SIGTERM path that is the
            # ORDINARY case at booth shutdown: a worker killed mid-fold still
            # has to let go of its chip. Guarded so a raising close() cannot
            # replace the exception that is already on its way out (which is
            # the one an operator actually needs to see).
            try:
                self.folder.close()
            except Exception:
                log.exception("Folder.close() raised on card %s; the device "
                              "may still be held", self.card)

    def _handle_line(self, line):
        """Handle one command line. Returns False if the worker should stop.

        Nothing a parent can put on this pipe may kill the worker: a truncated
        line from a parent that died mid-write, a command from a newer daemon
        this build does not know, a JSON scalar where an object was expected.
        All of it is logged and skipped. A worker that exited on a malformed
        line would turn one bad byte into a chip the booth loses for the rest
        of the session.
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
        if cmd == "fold":
            self._fold(command)
            return True
        log.warning("ignoring unknown command %r", cmd)
        return True

    def _fold(self, command):
        """Run one fold and report it, then free this worker.

        Mirrors runner/daemon.py's `_run_one` failure policy exactly, because
        it IS that policy, moved: a failed fold is logged in full, reported as
        a neutral `job_error`, and the loop advances. What is new is only that
        the failure is confined to one of four chips.
        """
        job_id = command.get("job_id")
        target_id = command.get("target_id")
        try:
            self.folder.fold(job_id, command.get("input_path"), self.emit,
                             target_id=target_id,
                             n_residues=command.get("n_residues", 0),
                             card=self.card)
        except FoldError as exc:
            # The documented failure path. `message` is for the daemon's log
            # only -- the UI's own contract is that it never reaches a screen
            # (ui/diagnostics.py) -- but it still has to be SENT, or the log
            # says a fold failed and nothing at all about why.
            log.exception("fold failed for %s on card %s", target_id, self.card)
            self.emit({"type": "job_error", "job_id": job_id,
                       "target_id": target_id, "message": str(exc)})
        except Exception as exc:
            # Backstop, deliberately a separate branch from the one above and
            # deliberately not narrowed to FoldError: fold() documents "raises
            # FoldError on failure", but the booth must not bet a chip on
            # every collaborator keeping its promise (runner/folder.py has a
            # history of one that didn't -- TapUnavailable used to escape
            # fold() directly). Logged distinctly so an operator can tell a
            # normal fold failure from something that violated its own
            # contract and needs a real fix.
            log.exception("unexpected (non-FoldError) exception folding %s on "
                          "card %s; treating it as a fold failure so this "
                          "worker keeps serving", target_id, self.card)
            self.emit({"type": "job_error", "job_id": job_id,
                       "target_id": target_id, "message": str(exc)})

        # After the try/except rather than in a `finally`, and that placement
        # is the whole point: KeyboardInterrupt and SystemExit do not subclass
        # Exception, so they pass straight through the handlers above -- and a
        # worker on its way out must NOT tell the parent it is idle and ready
        # for another job. `worker.idle` is the authoritative dispatch signal;
        # sending one from a dying process hands it a job that will never be
        # folded. Every fold that ENDS, succeeded or failed, frees the worker.
        self.control_emit(control(CONTROL_IDLE, card=self.card, job_id=job_id))


def main(argv=None):
    """Run one worker: `python3 -m runner.worker --card N --event-fd 3`.

    Thin on purpose -- everything worth testing is in WorkerSession. Note what
    is absent: nothing here redirects fd 1 or 2 (see the module docstring),
    and nothing here sets TT_VISIBLE_DEVICES. By the time this function runs,
    the parent has already put it in the environment this process was started
    with, which is strictly stronger than setting it before importing ttnn.
    """
    parser = argparse.ArgumentParser(
        prog="runner.worker",
        description="Fold on one Tenstorrent chip; commands on stdin, "
                    "events on the event fd.")
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

    folder = Folder(device_id=args.card)
    # `with`, so the fd is closed on every exit path including the SystemExit
    # WorkerSession.run raises on a fatal: the parent's reader sees a clean
    # EOF instead of hanging on a pipe nobody will ever write to again.
    with os.fdopen(args.event_fd, "w") as stream:
        def write(event):
            # Flushed per line, not per buffer: the parent's dispatch decisions
            # ride on these lines (`worker.idle` frees the card), so a line
            # sitting in a userspace buffer is a card the booth thinks is busy.
            stream.write(json.dumps(event) + "\n")
            stream.flush()

        session = WorkerSession(folder, write, write, card=args.card)
        # `iter(readline, "")` rather than `for line in sys.stdin`: this is a
        # pipe, and the explicit readline loop is unambiguously line-at-a-time
        # regardless of how the io layer buffers, which is what makes a
        # command dispatched by the parent get acted on now rather than when
        # some buffer happens to fill.
        session.run(iter(sys.stdin.readline, ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
