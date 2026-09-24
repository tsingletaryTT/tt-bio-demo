"""TelemetryBroadcaster: samples ui.telemetry on the bridge's own thread and
publishes a `telemetry` event on change.

This is bridge-side telemetry, deliberately independent of the daemon --
the same reason ui/telemetry.py's own module docstring gives for why the
native UI samples on its own thread rather than reading it off the wire: a
wedged or dead daemon must still leave the silicon visibly breathing on
screen. `ui.telemetry.TelemetrySampler` already does the hard part (a
background thread, tt-smi, never-raises contract); this class only adds
"publish when the reading actually changed" on top of it.
"""
import dataclasses
import logging
import threading

from ui.telemetry import TelemetrySampler

log = logging.getLogger(__name__)

DEFAULT_PERIOD_S = 2.0


def _reading_key(reading):
    """A hashable snapshot of one ChipReading's fields, for change
    detection -- comparing dataclass INSTANCES would always differ (a
    fresh object every sample), so this compares VALUES instead."""
    return (reading.index, reading.board_type, reading.temperature_c,
            reading.power_w, reading.aiclk_mhz, reading.board_id)


def _reading_to_dict(reading):
    return dataclasses.asdict(reading)


class TelemetryBroadcaster:
    """Publishes a `telemetry` event to `daemon_link` whenever the sampled
    reading list changes. `sampler` is injectable (default: a real
    `TelemetrySampler`) so tests never spawn a real `tt-smi`."""

    def __init__(self, daemon_link, sampler=None, period_s=DEFAULT_PERIOD_S):
        self._daemon_link = daemon_link
        self._sampler = sampler if sampler is not None else TelemetrySampler(
            period_s=period_s)
        self._owns_sampler = sampler is None
        self._period_s = period_s
        self._stop = threading.Event()
        self._thread = None
        self._last_key = None

    def start(self):
        if self._owns_sampler:
            self._sampler.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._period_s + 2.0)
        if self._owns_sampler:
            self._sampler.stop()

    def _run(self):
        while not self._stop.is_set():
            readings = self._sampler.latest()
            if readings is not None:
                key = tuple(_reading_key(r) for r in readings)
                if key != self._last_key:
                    self._last_key = key
                    self._daemon_link.publish({
                        "type": "telemetry",
                        "chips": [_reading_to_dict(r) for r in readings],
                    })
            self._stop.wait(self._period_s)
