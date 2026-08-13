"""Shared fakes for the multi-chip daemon's tests.

Extracted here rather than copied into each test module because three copies
of a fake pool is three places for the fake to drift away from the real
`WorkerPool` -- and a fake that has drifted is a suite that passes against
behaviour production does not have. Imported by
`test_daemon_multichip.py`, `test_thermal_four_up.py` (Task 10) and
`test_janitors_four_up.py` (Task 11).

`_FakePool` mirrors exactly the surface `Daemon` uses -- `cards`, `start`,
`stop`, `any_ready`, `ready_cards`, `busy_job`, `dispatch` -- and mirrors the
two properties of the real one that the daemon's correctness rests on:

- `any_ready()` is **busy-independent**: true while every chip is folding.
  `ready_cards()` is not. The daemon's `_hello` and its dispatch decision use
  the two for different questions and must not be able to confuse them.
- `dispatch()` raises `ValueError` for a card that is not dispatchable, which
  is the exception the daemon's requeue path catches. A fake that returned
  False instead would let a silently-dropped job pass every test here.
"""

import threading

from runner.daemon import Daemon, DaemonConfig

# How long `_run` lets `Daemon.run()` go before stopping it from outside.
# Every test here ends the loop deterministically from inside a fake; this is
# only the backstop for when the thing under test is broken. Measured against
# passing runs of well under 100 ms, so it costs nothing when nothing is wrong.
RUN_WATCHDOG_S = 3.0


class _FakePool:
    """A `WorkerPool` with no processes, no threads and no chips."""

    def __init__(self, cards=(0, 1, 2, 3), ready=None):
        self.cards = list(cards)
        self._ready = list(cards if ready is None else ready)
        self._busy = {}
        self.dispatched = []
        # Every card `dispatch` was CALLED for, refused ones included.
        # Deliberately separate from `dispatched`: the real pool defends
        # itself, so a daemon that ignored `ready_cards()` entirely and sent
        # to every card would produce an identical `dispatched` list (the
        # not-ready sends raise, the jobs are requeued, and nothing visible
        # differs) -- which made the plan's own "a card the pool is not ready
        # on is not dispatched to" test unable to fail against the mutation it
        # names. This is the only place that difference is observable.
        self.attempts = []
        self.started = self.stopped = 0
        self.on_worker_lost = None

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def any_ready(self):
        return bool(self._ready)

    def ready_cards(self):
        return sorted(c for c in self._ready if c not in self._busy)

    def busy_job(self, card):
        return self._busy.get(card)

    def dispatch(self, job, card):
        self.attempts.append(card)
        if card not in self.ready_cards():
            raise ValueError(f"card {card} is not ready")
        self._busy[card] = job.job_id
        self.dispatched.append((card, job.job_id, job.target_id))

    # -- test helpers --
    def finish(self, card):
        self._busy.pop(card, None)


class _CollectingServer:
    """An `EventServer` that keeps what it was asked to broadcast."""

    def __init__(self):
        self.events = []
        self.started = self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def broadcast(self, event):
        self.events.append(event)
        return 1


def _daemon(tmp_path, pool, **over):
    """A `Daemon` wired to `pool` and a collecting server, folding nothing.

    Note what this deliberately does NOT do: it never assigns `daemon.cards`.
    `Daemon.cards` is derived from the pool's own inventory the first time it
    is read, so a test that substitutes a pool with a different card list gets
    a `CardPool` that agrees with it -- which is the property the real daemon
    depends on and a hand-assigned `CardPool([0, 1, 2, 3])` here would hide.
    """
    config = DaemonConfig(
        socket_path=str(tmp_path / "sock"), weights_dir=str(tmp_path),
        playlist_dir=str(tmp_path / "playlist"), log_root=str(tmp_path / "logs"),
        **over)
    daemon = Daemon(config)
    daemon.pool = pool
    daemon.server = _CollectingServer()
    return daemon


def _run(daemon, seconds=RUN_WATCHDOG_S):
    """`daemon.run()`, with an external stop after `seconds`.

    Use this rather than calling `run()` directly. `run()` is an unbounded
    poll loop, and a mutation (or a real bug) that stops whichever fake was
    going to end it from inside turns a test failure into a hung test run --
    which during Task 8's mutation sweep is exactly what happened, twice, and
    what took the sweep's per-test results with it. A watchdog makes that
    failure finite and legible instead.
    """
    watchdog = threading.Timer(seconds, daemon.stop)
    watchdog.start()
    try:
        daemon.run()
    finally:
        watchdog.cancel()
