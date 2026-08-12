"""Independent tt-smi sampling for the UI's telemetry panel.

Why the UI samples independently: see runner/cards.py's module docstring --
that duplication is deliberate. The daemon that actually computes and drives
a fold must never be coupled to the thing most likely to fail (reading a
sensor over `tt-smi`). This module therefore never touches the socket and
never depends on the runner daemon being up, healthy, or even installed: it
polls `tt-smi` on its own background thread, gated only on whether `tt-smi`
itself answers. That is what lets a wedged or dead daemon still leave the
silicon visibly breathing on screen.

This module intentionally mirrors runner/cards.py's field handling
(`_number`'s strip-then-float, the `board_type` default, the per-card
try/except that skips one bad card rather than failing the whole snapshot)
rather than reinventing it -- per the brief, don't gratuitously diverge from
a shape that already works. It is a separate copy, not an import: ui/ and
runner/ live in different venvs that must never cross-import (see
docs/venv-bootstrap-notes.md), so importing runner.cards from here is not
available even in principle, only convenient by accident on a dev box where
both happen to be on the same PYTHONPATH. If more than the parse step itself
ever needs to be shared between the two sides, it belongs in protocol/,
which both venvs can already reach -- see the report for task 4 of the
2026-08-12-ui-panels plan for why this task's copy stayed this small (one
function, `_number`, plus the per-card try/except shape) and didn't trigger
that move.

Design note on `latest()` / `age_s()`: a failed sample never overwrites a
previous good reading with None, an empty list, or zeros -- it just leaves
the last known reading in place and lets `age_s()` say how old it is. That
is what makes `age_s()` meaningful at all: if a single flaky poll blanked
the reading, there would be nothing left to measure the age of, and the
panel could not tell "tt-smi answered once and has been quiet since" (a
sensor hiccup, wait it out) from "the cards are genuinely idle" (also
static, but for a different reason) -- exactly the distinction the brief
calls out as `age_s()`'s reason to exist. The panel is expected to combine
`latest()` with `age_s()` itself (e.g. treat anything older than a couple of
sample periods as unknown) rather than this module silently deciding that
threshold. What `latest()` guarantees on its own is narrower and absolute:
before the first sample ever succeeds, it is None, never a placeholder.
"""

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# --snapshot_no_tty matches runner/cards.py's invocation: it is what makes
# `tt-smi -s` safe to run from a background thread with no controlling
# terminal (a booth kiosk process may not have one) instead of trying to
# paint a TUI at it.
_TT_SMI_ARGS = ["tt-smi", "-s", "--snapshot_no_tty"]


@dataclass(frozen=True)
class CardReading:
    index: int
    board_type: str
    temperature_c: float
    power_w: float
    aiclk_mhz: float


def _number(value):
    """Parse one tt-smi telemetry field.

    Real values arrive as strings, sometimes left-padded (`' 18.0'`, per the
    verified real `tt-smi -s` sample quoted in this task's brief) -- hence
    strip-then-float rather than a bare `float()`. Matches
    runner/cards.py:_number on purpose; see the module docstring.
    """
    return float(str(value).strip())


def parse_snapshot(snapshot):
    """Parse a `tt-smi -s` snapshot dict into a list of CardReadings.

    A card whose telemetry can't be parsed (tt-smi's own "n/a" sentinel, a
    missing key, an empty board_info) is skipped, not fatal: one unreadable
    card must not blind the panel to the other three. Mirrors
    runner/cards.py:parse_tt_smi's shape -- see the module docstring for why
    this is a deliberate copy rather than a shared import.
    """
    readings = []
    for index, device in enumerate(snapshot.get("device_info", []) or []):
        board = device.get("board_info", {}) or {}
        telemetry = device.get("telemetry", {}) or {}
        try:
            readings.append(CardReading(
                index=index,
                board_type=board.get("board_type", "unknown"),
                temperature_c=_number(telemetry.get("asic_temperature")),
                power_w=_number(telemetry.get("power")),
                aiclk_mhz=_number(telemetry.get("aiclk")),
            ))
        except (TypeError, ValueError):
            log.warning("card %d has unreadable telemetry; skipping it", index)
    return readings


def _run_tt_smi(timeout):
    """Run `tt-smi -s` and return its stdout as text.

    Kept as its own function purely so tests have one seam to monkeypatch
    (`ui.telemetry._run_tt_smi`) instead of mocking subprocess -- see
    tests/unit/test_ui_telemetry.py. This function does not itself decide
    what's survivable: a missing binary raises FileNotFoundError, a hung
    process raises subprocess.TimeoutExpired, a nonzero exit raises
    CalledProcessError, and all of that propagates unchanged. TelemetrySampler
    is the layer that catches it.
    """
    result = subprocess.run(
        _TT_SMI_ARGS, capture_output=True, timeout=timeout, check=True, text=True,
    )
    return result.stdout


class TelemetrySampler:
    """Polls `tt-smi` on a background thread and keeps the newest reading.

    Deliberately ignorant of the socket, the runner daemon, and GTK: see
    the module docstring for why. Safe to call `latest()` / `age_s()` from
    any thread (they're the two calls a later GTK main-loop task will read
    this from) -- both only ever take the internal lock, never block on
    `tt-smi` itself.

    `samples_attempted` and `thread_alive` are observable state, read
    directly by tests (and useful for a future "sampler looks stuck"
    diagnostic), not incidental implementation detail:

    - `samples_attempted` counts every poll *attempt*, success or failure,
      so a test (or a diagnostic) can tell "the thread is looping" from "the
      thread died/never started" without waiting out a full timeout window.
    - `thread_alive` reflects the actual OS thread's live state
      (`Thread.is_alive()`), not a latched flag -- so it is trustworthy both
      while the sampler is running (proof an exception hasn't silently
      killed it) and after `stop()` (proof `stop()` actually joined it,
      rather than firing the stop signal and returning immediately while the
      thread was still mid-iteration).
    """

    def __init__(self, period_s=2.0, timeout_s=5.0):
        self.period_s = period_s
        self.timeout_s = timeout_s
        self.samples_attempted = 0
        self._lock = threading.Lock()
        self._latest = None       # list[CardReading] | None
        self._latest_at = None    # time.monotonic() of the last successful sample
        self._stop_event = threading.Event()
        self._thread = None

    @property
    def thread_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """Start the background poll loop. Safe to call more than once;
        a second call while already running is a no-op rather than a second
        thread racing the first."""
        if self.thread_alive:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="tt-smi-telemetry-sampler", daemon=True,
        )
        self._thread.start()

    def stop(self):
        """Signal the loop to stop and wait for it to actually exit.

        Joining (rather than just setting the stop flag and returning) is
        what lets a caller rely on `thread_alive` being False the instant
        `stop()` returns -- e.g. before tearing down whatever `_run_tt_smi`
        depends on. Safe to call before `start()` (no-op: there is no
        thread) and safe to call twice (the second call finds the thread
        object already gone and is a no-op too).

        The join timeout is bounded but generous relative to `timeout_s`:
        the worst case the loop can be stuck in is one in-flight
        `_run_tt_smi` call, which itself cannot run longer than `timeout_s`
        (that's what its own `timeout` argument bounds), so `timeout_s`
        plus a margin comfortably covers "stop() landed right as a slow
        sample started."
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout_s + 1.0)
            self._thread = None

    def latest(self):
        """The most recent successfully-parsed reading, or None.

        None means exactly one thing: no sample has ever succeeded (either
        because none has been attempted yet, or because every attempt so
        far has failed). It is never a stand-in for "the last good reading,
        but old" -- that distinction is what `age_s()` is for.
        """
        with self._lock:
            return self._latest

    def age_s(self):
        """Seconds since the last successful sample, or None if there has
        never been one."""
        with self._lock:
            if self._latest_at is None:
                return None
            return time.monotonic() - self._latest_at

    def _run(self):
        # The loop's own exceptions are handled entirely inside
        # _sample_once: nothing here can raise, so nothing here can kill
        # this thread. That's the point -- this thread has to keep polling
        # for as long as the booth is open regardless of what tt-smi does.
        while not self._stop_event.is_set():
            self._sample_once()
            # Event.wait() (rather than time.sleep()) so a stop() during the
            # inter-sample gap wakes this up immediately instead of leaving
            # it asleep for up to period_s after the caller already asked
            # it to stop.
            self._stop_event.wait(self.period_s)

    def _sample_once(self):
        with self._lock:
            self.samples_attempted += 1
        try:
            raw = _run_tt_smi(self.timeout_s)
            readings = parse_snapshot(json.loads(raw))
        except Exception:
            # Anything at all -- missing binary, timeout, non-zero exit,
            # junk instead of JSON, a JSON value shaped nothing like a
            # tt-smi snapshot -- is "no telemetry this round," never a
            # reason to stop trying. Logged (not raised) so this survives
            # unattended for a whole conference day: see the module
            # docstring and the class docstring's `thread_alive` note.
            # Deliberately does NOT touch self._latest / self._latest_at:
            # a previous good reading (if any) stays exactly as it was,
            # and its growing age_s() is how the panel notices staleness.
            log.warning("tt-smi sample failed; treating as no telemetry this round",
                        exc_info=True)
            return
        with self._lock:
            self._latest = readings
            self._latest_at = time.monotonic()
