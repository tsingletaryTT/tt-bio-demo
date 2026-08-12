"""Tests for ui/telemetry.py -- the UI's independent tt-smi sampler.

Why this exists (see ui/telemetry.py's module docstring): the panel must
keep showing card temperatures even if the runner daemon is wedged or dead,
so it samples `tt-smi` on its own thread, never through the socket.

PLAN GAP found and fixed here (see task-4-report.md for the full writeup):
the brief's test_a_timeout_does_not_kill_the_sampler placed its
`assert s.thread_alive is True` *after* the try/finally that calls
`s.stop()`. Since stop() is required to join the thread (that's the whole
point of the sibling stop()-not-joining mutation later in this file), the
thread is *supposed* to be dead by the time that assertion runs under a
correct implementation -- so the assertion as literally written could never
pass for a correct implementation once test_stop_ends_the_thread_promptly's
requirement is also honoured. It only makes sense checked *before* stop() is
called, while the sampler is still mid-loop surviving repeated timeouts --
which is also the only placement where it actually catches "an exception
kills the thread" (this test's named mutation). Moved inside the `try`,
before the `finally` runs.
"""

import json
import subprocess
import time

import pytest

from ui.telemetry import CardReading, TelemetrySampler, parse_snapshot

SNAPSHOT = {
    "device_info": [
        {"board_info": {"board_type": "p300c", "bus_id": "0000:01:00.0"},
         "telemetry": {"asic_temperature": "43.7", "power": " 18.0", "aiclk": " 800"}},
        {"board_info": {"board_type": "p300c", "bus_id": "0000:02:00.0"},
         "telemetry": {"asic_temperature": "46.3", "power": " 13.0", "aiclk": " 800"}},
    ]
}


def _wait(predicate, timeout):
    """Poll `predicate` until it's true or `timeout` seconds have passed.

    Polls on a short interval rather than sleeping the full timeout, so a
    test that succeeds quickly (the common case) doesn't pay for the whole
    budget -- and a test that's genuinely stuck still fails loudly instead
    of hanging forever.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(f"condition not met within {timeout}s")


def test_parses_padded_string_values():
    cards = parse_snapshot(SNAPSHOT)
    assert [c.index for c in cards] == [0, 1]
    assert cards[0].temperature_c == pytest.approx(43.7)
    assert cards[0].power_w == pytest.approx(18.0)


def test_one_unreadable_card_does_not_blind_the_panel_to_the_others():
    snapshot = {"device_info": [
        {"board_info": {}, "telemetry": {"asic_temperature": "n/a"}},
        SNAPSHOT["device_info"][0],
    ]}
    assert len(parse_snapshot(snapshot)) == 1


def test_latest_is_none_before_the_first_sample():
    assert TelemetrySampler().latest() is None


def test_a_missing_tt_smi_binary_is_survivable(monkeypatch):
    monkeypatch.setattr("ui.telemetry._run_tt_smi",
                        lambda timeout: (_ for _ in ()).throw(FileNotFoundError()))
    s = TelemetrySampler(period_s=0.01)
    s.start()
    try:
        _wait(lambda: s.samples_attempted >= 2, 3.0)
    finally:
        s.stop()
    assert s.latest() is None, "no telemetry is not fake telemetry"


def test_a_timeout_does_not_kill_the_sampler(monkeypatch):
    def slow(timeout):
        raise subprocess.TimeoutExpired(cmd="tt-smi", timeout=timeout)

    monkeypatch.setattr("ui.telemetry._run_tt_smi", slow)
    s = TelemetrySampler(period_s=0.01)
    s.start()
    try:
        _wait(lambda: s.samples_attempted >= 3, 3.0)
        # Checked here, before stop() -- see the module docstring's PLAN GAP
        # note. stop() is required to join the thread, so checking this
        # *after* stop() would demand thread_alive be True on a thread that
        # correct behaviour has already terminated, which no implementation
        # honouring test_stop_ends_the_thread_promptly could ever satisfy.
        assert s.thread_alive is True, "the sampler must keep trying"
    finally:
        s.stop()


def test_malformed_json_is_survivable(monkeypatch):
    monkeypatch.setattr("ui.telemetry._run_tt_smi", lambda timeout: "not json{")
    s = TelemetrySampler(period_s=0.01)
    s.start()
    try:
        _wait(lambda: s.samples_attempted >= 2, 3.0)
    finally:
        s.stop()
    assert s.latest() is None


def test_age_reports_how_stale_the_reading_is(monkeypatch):
    monkeypatch.setattr("ui.telemetry._run_tt_smi",
                        lambda timeout: json.dumps(SNAPSHOT))
    s = TelemetrySampler(period_s=0.01)
    s.start()
    try:
        _wait(lambda: s.latest() is not None, 3.0)
    finally:
        s.stop()
    assert s.age_s() is not None and s.age_s() < 2.0


def test_stop_ends_the_thread_promptly(monkeypatch):
    monkeypatch.setattr("ui.telemetry._run_tt_smi",
                        lambda timeout: json.dumps(SNAPSHOT))
    s = TelemetrySampler(period_s=0.01)
    s.start()
    _wait(lambda: s.latest() is not None, 3.0)
    s.stop()
    assert s.thread_alive is False
