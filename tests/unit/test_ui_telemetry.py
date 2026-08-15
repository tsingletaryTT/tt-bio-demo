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

from ui.telemetry import (
    ChipReading, TelemetrySampler, distinct_board_count, parse_snapshot,
)

SNAPSHOT = {
    "device_info": [
        {"board_info": {"board_type": "p300c", "bus_id": "0000:01:00.0"},
         "telemetry": {"asic_temperature": "43.7", "power": " 18.0", "aiclk": " 800"}},
        {"board_info": {"board_type": "p300c", "bus_id": "0000:02:00.0"},
         "telemetry": {"asic_temperature": "46.3", "power": " 13.0", "aiclk": " 800"}},
    ]
}

# Verbatim shape of this booth's own `tt-smi -s`: four device entries (four
# CHIPS), pairwise sharing a `board_id` (two p300c BOARDS). Copied from the
# real snapshot taken on the machine, telemetry values trimmed.
QB2_SNAPSHOT = {
    "device_info": [
        {"board_info": {"board_type": "p300c", "bus_id": "0000:01:00.0",
                        "board_id": "0000046131924062"},
         "telemetry": {"asic_temperature": "43.7", "power": " 18.0", "aiclk": "1350"}},
        {"board_info": {"board_type": "p300c", "bus_id": "0000:02:00.0",
                        "board_id": "0000046131924062"},
         "telemetry": {"asic_temperature": "44.1", "power": " 17.0", "aiclk": "1350"}},
        {"board_info": {"board_type": "p300c", "bus_id": "0000:03:00.0",
                        "board_id": "0000046131924055"},
         "telemetry": {"asic_temperature": "45.9", "power": " 19.0", "aiclk": "1350"}},
        {"board_info": {"board_type": "p300c", "bus_id": "0000:04:00.0",
                        "board_id": "0000046131924055"},
         "telemetry": {"asic_temperature": "46.3", "power": " 13.0", "aiclk": "1350"}},
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


def test_board_id_survives_the_parse_so_chips_can_be_grouped_by_board():
    """Without this, the panel cannot tell four chips on two boards from
    four boards -- which is the exact mistake the old "4 cards" heading
    made."""
    readings = parse_snapshot(QB2_SNAPSHOT)
    assert len(readings) == 4, "four device entries are four chips"
    assert [r.board_id for r in readings] == [
        "0000046131924062", "0000046131924062",
        "0000046131924055", "0000046131924055"]
    assert distinct_board_count(readings) == 2


def test_a_snapshot_without_board_ids_yields_no_board_count():
    """An older tt-smi (or a device with no board_info) must cost the board
    grouping and NOTHING else: every chip still parses, with all of its
    telemetry."""
    readings = parse_snapshot(SNAPSHOT)
    assert len(readings) == 2
    assert all(r.board_id is None for r in readings)
    assert distinct_board_count(readings) is None
    assert readings[0].temperature_c == pytest.approx(43.7), (
        "a missing board_id must not cost the reading itself")


def test_distinct_board_count_of_nothing_is_none():
    assert distinct_board_count([]) is None
    assert distinct_board_count(None) is None


def test_one_unreadable_card_does_not_blind_the_panel_to_the_others():
    snapshot = {"device_info": [
        {"board_info": {}, "telemetry": {"asic_temperature": "n/a"}},
        SNAPSHOT["device_info"][0],
    ]}
    assert len(parse_snapshot(snapshot)) == 1


def test_a_non_finite_temperature_is_treated_as_unparseable():
    """Regression (fix round 1 review): float() happily parses "nan"/"inf",
    so without a finiteness check a NaN telemetry value would sail through
    as a genuine-looking ChipReading -- rendering as a plausible card
    showing "nan °C" instead of being skipped like every other unreadable
    value (tt-smi's own "n/a", a missing key, ...) already is."""
    snapshot = {"device_info": [
        {"board_info": {"board_type": "p300c"},
         "telemetry": {"asic_temperature": "nan", "power": "18.0", "aiclk": "800"}},
        SNAPSHOT["device_info"][0],
    ]}
    cards = parse_snapshot(snapshot)
    assert len(cards) == 1, "the non-finite card must be skipped, not kept as a fake reading"
    assert cards[0].temperature_c == pytest.approx(43.7), "the OTHER card must still come through"


@pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf", "Infinity"])
def test_non_finite_variants_are_all_treated_as_unparseable(bad_value):
    snapshot = {"device_info": [
        {"board_info": {"board_type": "p300c"},
         "telemetry": {"asic_temperature": bad_value, "power": "18.0", "aiclk": "800"}},
    ]}
    assert parse_snapshot(snapshot) == []


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
    """`stop()` must JOIN, not merely signal.

    Asserting on `s.thread_alive` alone could not fail: the property is
    `self._thread is not None and self._thread.is_alive()`, and `stop()`
    sets `self._thread = None` on the line after the join -- so deleting
    the join outright left this test green (whole-branch review). The
    thread object is therefore captured BEFORE stop(), so the assertion is
    about the OS thread rather than about the field having been cleared.

    `samples_attempted` is checked too, because it is the other thing a
    caller relies on after `stop()` returns: the sampler is not still
    running `tt-smi` against a process that is tearing down. At
    period_s=0.01 a thread left alive would tick ~10 times in the 0.1s
    below, so a stale count is a real signal, not a race.
    """
    monkeypatch.setattr("ui.telemetry._run_tt_smi",
                        lambda timeout: json.dumps(SNAPSHOT))
    s = TelemetrySampler(period_s=0.01)
    s.start()
    thread = s._thread
    assert thread is not None, "start() did not create a thread"
    _wait(lambda: s.latest() is not None, 3.0)
    s.stop()
    assert not thread.is_alive(), (
        "stop() returned while its sampler thread was still running -- it "
        "signalled the stop event but never joined")
    settled = s.samples_attempted
    time.sleep(0.1)
    assert s.samples_attempted == settled, (
        "the sampler kept polling after stop() returned")
    assert s.thread_alive is False


# --- Fix round 1: tri-state latest() (see review Critical finding) --------
#
# A well-formed, exit-0 snapshot where EVERY device's telemetry is
# unparseable must not be treated as a successful "zero cards" reading: it
# is tt-smi telling us hardware exists and none of it could be read, which
# is a failed sample. It must leave a prior good reading in place (and let
# it age), exactly like a missing binary or a timeout would. Only a
# genuinely empty device_info is a truthful zero-cards reading.

_ALL_UNREADABLE_NONEMPTY_SNAPSHOT = json.dumps({
    "device_info": [{"board_info": {}, "telemetry": {"asic_temperature": "n/a"}}],
})


def test_an_all_unparseable_nonempty_snapshot_preserves_the_prior_reading(monkeypatch):
    calls = {"n": 0}

    def stub(timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps(SNAPSHOT)  # one good sample to seed a real reading
        return _ALL_UNREADABLE_NONEMPTY_SNAPSHOT  # every call after: unreadable

    monkeypatch.setattr("ui.telemetry._run_tt_smi", stub)
    s = TelemetrySampler(period_s=0.01)
    s.start()
    try:
        _wait(lambda: s.latest() is not None, 3.0)
        first_reading = s.latest()
        first_age = s.age_s()
        # Let several more poll cycles run, all hitting the "seen but
        # unreadable" branch, then let real time pass so age_s() has room
        # to visibly separate from first_age -- a bare `>` comparison right
        # after the first read could pass on clock-resolution noise alone.
        _wait(lambda: s.samples_attempted >= 8, 3.0)
        time.sleep(0.1)
    finally:
        s.stop()

    # Identity, not truthiness: a bug that clobbered the reading with some
    # *other* non-empty list (e.g. zeros, or a stale placeholder) would
    # still pass `latest() is not None` or `len(latest()) > 0`.
    assert s.latest() == first_reading
    assert s.latest()[0].temperature_c == pytest.approx(43.7)
    assert s.age_s() > first_age + 0.05, (
        "age_s() must keep growing while every subsequent sample fails, "
        "not reset because a fresh (empty) reading was recorded"
    )


def test_a_well_formed_empty_snapshot_is_a_truthful_zero_card_reading(monkeypatch):
    monkeypatch.setattr("ui.telemetry._run_tt_smi",
                        lambda timeout: json.dumps({"device_info": []}))
    s = TelemetrySampler(period_s=0.01)
    s.start()
    try:
        _wait(lambda: s.samples_attempted >= 2, 3.0)
    finally:
        s.stop()
    assert s.latest() == [], "an empty device_info is a real reading, not a failure"
    assert s.age_s() is not None and s.age_s() < 2.0


def test_a_tt_smi_killed_by_a_signal_is_not_a_warning(monkeypatch, caplog):
    """The UI polls tt-smi itself, so there are TWO samplers with this
    problem and `runner/cards.py`'s fix was half of it.

    Stopping the booth by process group signals whatever tt-smi child this
    sampler has in flight, so every clean shutdown ended with a warning and a
    traceback. Found by stopping the real booth and reading the log -- the
    unit test for the daemon-side fix could not have, and the follow-up
    naming it mentioned only that file.

    Mutation: removing the `returncode < 0` branch. Red -- a WARNING with
    exc_info attached.
    """
    import logging
    import subprocess

    from ui import telemetry as telemetry_module

    def killed_by_sigint(*a, **kw):
        raise subprocess.CalledProcessError(-2, "tt-smi")

    monkeypatch.setattr(telemetry_module, "_run_tt_smi", killed_by_sigint)
    sampler = telemetry_module.TelemetrySampler()

    with caplog.at_level(logging.INFO, logger="ui.telemetry"):
        sampler._sample_once()

    assert caplog.records, "the sample failed and said nothing at all"
    for record in caplog.records:
        assert record.levelno < logging.WARNING, (
            f"a signalled tt-smi logged at {record.levelname}")
        assert record.exc_info is None, "a clean shutdown printed a traceback"


def test_a_tt_smi_that_really_failed_still_warns_with_its_traceback(
        monkeypatch, caplog):
    """The other half: a sampler that fails while the booth is RUNNING is
    losing the panel its telemetry, and that must stay loud."""
    import logging
    import subprocess

    from ui import telemetry as telemetry_module

    def exited_nonzero(*a, **kw):
        raise subprocess.CalledProcessError(1, "tt-smi", stderr="no such device")

    monkeypatch.setattr(telemetry_module, "_run_tt_smi", exited_nonzero)
    sampler = telemetry_module.TelemetrySampler()

    with caplog.at_level(logging.INFO, logger="ui.telemetry"):
        sampler._sample_once()

    assert any(r.levelno >= logging.WARNING and r.exc_info
               for r in caplog.records), \
        "a real tt-smi failure lost its warning or its traceback"
    assert any("no such device" in r.getMessage() for r in caplog.records), \
        "tt-smi's own explanation was dropped"
