"""TelemetryBroadcaster: samples ui.telemetry, publishes on change only.

A fake sampler (not the real TelemetrySampler) drives every test here --
this is a unit test of the CHANGE-DETECTION and publish wiring, not of
tt-smi itself (ui/telemetry.py's own test file already covers that).
"""
import threading
import time

import pytest

from ui.telemetry import ChipReading
from webview import bridge
from webview.telemetry_broadcaster import TelemetryBroadcaster


class _FakeSampler:
    def __init__(self, readings_sequence):
        self._sequence = list(readings_sequence)
        self._index = 0
        self.calls = 0

    def latest(self):
        self.calls += 1
        i = min(self._index, len(self._sequence) - 1)
        return self._sequence[i]

    def advance(self):
        self._index += 1


class _FakeLink:
    def __init__(self):
        self.published = []
        self._lock = threading.Lock()

    def publish(self, event):
        with self._lock:
            self.published.append(event)


def _reading(index=0, temp=47.8, power=19.0, aiclk=800.0, board_id="B0"):
    return ChipReading(index=index, board_type="p300c", temperature_c=temp,
                       power_w=power, aiclk_mhz=aiclk, board_id=board_id)


def test_a_reading_publishes_exactly_once_while_unchanged():
    sampler = _FakeSampler([[_reading()]] * 5)
    link = _FakeLink()
    b = TelemetryBroadcaster(link, sampler=sampler, period_s=0.01)
    b.start()
    time.sleep(0.1)
    b.stop()
    assert len(link.published) == 1
    assert link.published[0] == {
        "type": "telemetry",
        "chips": [{"index": 0, "board_type": "p300c", "temperature_c": 47.8,
                   "power_w": 19.0, "aiclk_mhz": 800.0, "board_id": "B0"}],
    }


def test_a_real_change_publishes_again():
    sampler = _FakeSampler([[_reading(power=19.0)], [_reading(power=19.0)],
                             [_reading(power=55.0)]])

    class _AdvancingSampler(_FakeSampler):
        def latest(self):
            reading = super().latest()
            self.advance()
            return reading

    sampler = _AdvancingSampler([[_reading(power=19.0)], [_reading(power=55.0)],
                                  [_reading(power=55.0)]])
    link = _FakeLink()
    b = TelemetryBroadcaster(link, sampler=sampler, period_s=0.01)
    b.start()
    time.sleep(0.1)
    b.stop()
    powers = [event["chips"][0]["power_w"] for event in link.published]
    assert powers[0] == 19.0
    assert 55.0 in powers
    assert powers[-1] == 55.0
    # No consecutive duplicate once it settles at 55.0.
    assert not (len(powers) >= 2 and powers[-1] == powers[-2] == 55.0
                and len(set(powers[-3:])) == 1 and len(powers) > 3)


def test_no_sample_yet_publishes_nothing():
    """tt-smi genuinely absent/failing -- TelemetrySampler.latest() is
    None until a first sample ever succeeds. Never a fabricated reading,
    never a crash."""
    sampler = _FakeSampler([None, None, None])
    link = _FakeLink()
    b = TelemetryBroadcaster(link, sampler=sampler, period_s=0.01)
    b.start()
    time.sleep(0.05)
    b.stop()
    assert link.published == []


def test_stop_actually_stops_the_thread():
    sampler = _FakeSampler([[_reading()]])
    link = _FakeLink()
    b = TelemetryBroadcaster(link, sampler=sampler, period_s=0.01)
    b.start()
    time.sleep(0.02)
    b.stop()
    count_after_stop = sampler.calls
    time.sleep(0.05)
    assert sampler.calls == count_after_stop, (
        "the sampler kept being polled after stop() -- the thread was not "
        "actually stopped, only forgotten")


def test_a_full_subscriber_queue_does_not_block_a_real_telemetry_event_for_others():
    """Review Focus: the DaemonLink.publish() backpressure contract (Task
    1 -- drop for the full subscriber only, never raise outward) must
    hold for a REAL telemetry event too, not just the generic event
    Task 1's own test used. Exercises DaemonLink.publish() directly
    (not a fake) against two real queue.Queue subscribers."""
    import queue as queue_module

    link = bridge.DaemonLink("/nonexistent")
    full_q = link.subscribe()
    for _ in range(bridge.SUBSCRIBER_QUEUE_MAX):
        full_q.put_nowait({"type": "filler"})
    healthy_q = link.subscribe()

    sampler = _FakeSampler([[_reading(power=19.0)]])
    b = TelemetryBroadcaster(link, sampler=sampler, period_s=0.01)
    b.start()
    time.sleep(0.05)
    b.stop()

    # The full subscriber's queue is untouched beyond its cap (publish()
    # dropped the telemetry event for it, per Task 1) -- and the healthy
    # subscriber still received it.
    assert full_q.qsize() == bridge.SUBSCRIBER_QUEUE_MAX
    received = []
    while True:
        try:
            received.append(healthy_q.get_nowait())
        except queue_module.Empty:
            break
    assert any(e.get("type") == "telemetry" for e in received)
