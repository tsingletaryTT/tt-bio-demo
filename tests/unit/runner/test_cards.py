import pytest

from runner.cards import CardPool, CardState, parse_tt_smi

SNAPSHOT = {
    "device_info": [
        {"board_info": {"board_type": "p300c", "bus_id": "0000:01:00.0"},
         "telemetry": {"asic_temperature": "43.7", "power": " 18.0", "aiclk": " 800"}},
        {"board_info": {"board_type": "p300c", "bus_id": "0000:02:00.0"},
         "telemetry": {"asic_temperature": "46.3", "power": " 13.0", "aiclk": " 800"}},
    ]
}


def test_parses_every_card_in_the_snapshot():
    cards = parse_tt_smi(SNAPSHOT)
    assert len(cards) == 2
    assert [c.index for c in cards] == [0, 1]


def test_parses_padded_string_values_into_numbers():
    first = parse_tt_smi(SNAPSHOT)[0]
    assert first.temperature_c == pytest.approx(43.7)
    assert first.power_w == pytest.approx(18.0)
    assert first.aiclk_mhz == pytest.approx(800)
    assert first.board_type == "p300c"


def test_an_empty_snapshot_yields_no_cards():
    assert parse_tt_smi({"device_info": []}) == []
    assert parse_tt_smi({}) == []


def test_a_card_with_unreadable_telemetry_is_skipped_not_fatal():
    snapshot = {"device_info": [
        {"board_info": {"board_type": "p300c"}, "telemetry": {"asic_temperature": "n/a"}},
        SNAPSHOT["device_info"][0],
    ]}
    cards = parse_tt_smi(snapshot)
    assert [c.temperature_c for c in cards] == [pytest.approx(43.7)]


def _card(index, temp):
    return CardState(index=index, board_type="p300c", temperature_c=temp,
                     power_w=15.0, aiclk_mhz=800)


def test_cool_cards_are_schedulable():
    pool = CardPool([0, 1])
    pool.update([_card(0, 45.0), _card(1, 46.0)])
    assert pool.schedulable() == [0, 1]


def test_an_overheating_card_stops_being_scheduled():
    pool = CardPool([0, 1], max_temp_c=85.0)
    pool.update([_card(0, 91.0), _card(1, 46.0)])
    assert pool.schedulable() == [1]


def test_overheating_emits_a_quarantined_card_state_event():
    pool = CardPool([0, 1], max_temp_c=85.0)
    pool.update([_card(0, 45.0), _card(1, 46.0)])
    events = pool.update([_card(0, 91.0), _card(1, 46.0)])
    assert {"type": "card_state", "card": 0, "state": "quarantined"} in events


def test_no_events_are_emitted_when_nothing_changed():
    pool = CardPool([0, 1])
    pool.update([_card(0, 45.0), _card(1, 46.0)])
    assert pool.update([_card(0, 45.5), _card(1, 46.5)]) == []


def test_a_card_that_cools_down_becomes_schedulable_again():
    pool = CardPool([0], max_temp_c=85.0)
    pool.update([_card(0, 91.0)])
    events = pool.update([_card(0, 60.0)])
    assert pool.schedulable() == [0]
    assert {"type": "card_state", "card": 0, "state": "idle"} in events


def test_a_busy_card_is_not_handed_out_again():
    pool = CardPool([0, 1])
    pool.update([_card(0, 45.0), _card(1, 46.0)])
    pool.mark_busy(0)
    assert pool.schedulable() == [1]
    pool.mark_idle(0)
    assert pool.schedulable() == [0, 1]


def test_marking_busy_emits_a_busy_event():
    pool = CardPool([0])
    assert pool.mark_busy(0) == {"type": "card_state", "card": 0, "state": "busy"}


# --- busy x hot combination (regression coverage for the single-state-string bug) ---
#
# The original CardPool represented "idle"/"busy"/"quarantined" as one status
# string per card. That made a busy card's overheating clobber the fact that a
# job was in flight, and let the card cooling back down silently un-quarantine
# it — schedulable() would then hand out a card whose original job was still
# running. These tests cover exactly the combination the single-string bug
# hid: busy AND hot on the same card, in both orders.

def test_a_busy_card_that_overheats_stays_unschedulable_while_cooling():
    pool = CardPool([0], max_temp_c=85.0)
    pool.mark_busy(0)
    pool.update([_card(0, 91.0)])          # overheats mid-fold
    assert pool.schedulable() == []
    pool.update([_card(0, 60.0)])          # cools, but the fold is still running
    assert pool.schedulable() == []        # must NOT become schedulable via cooling alone
    pool.mark_idle(0)                      # the fold actually finishes
    assert pool.schedulable() == [0]


def test_a_busy_card_that_overheats_reports_quarantined_not_busy():
    pool = CardPool([0], max_temp_c=85.0)
    pool.mark_busy(0)
    events = pool.update([_card(0, 91.0)])
    assert {"type": "card_state", "card": 0, "state": "quarantined"} in events


def test_a_busy_card_that_cools_while_still_running_reports_busy_not_idle():
    pool = CardPool([0], max_temp_c=85.0)
    pool.mark_busy(0)
    pool.update([_card(0, 91.0)])
    events = pool.update([_card(0, 60.0)])
    assert {"type": "card_state", "card": 0, "state": "busy"} in events
    assert {"type": "card_state", "card": 0, "state": "idle"} not in events


def test_mark_idle_on_a_still_hot_card_returns_none_and_stays_quarantined():
    pool = CardPool([0], max_temp_c=85.0)
    pool.mark_busy(0)
    pool.update([_card(0, 91.0)])
    assert pool.mark_idle(0) is None
    assert pool.schedulable() == []


def test_mark_busy_on_a_quarantined_card_raises():
    pool = CardPool([0], max_temp_c=85.0)
    pool.update([_card(0, 91.0)])
    with pytest.raises(ValueError):
        pool.mark_busy(0)


def test_all_indices_reports_every_card_regardless_of_busy_or_hot_state():
    """Distinct from schedulable(): a busy or quarantined card must still
    appear here -- see runner/daemon.py's Daemon._hello, the call site this
    exists for.
    """
    pool = CardPool([0, 1])
    pool.mark_busy(0)
    pool.update([_card(1, 91.0)])
    assert pool.schedulable() == []            # neither card is free
    assert pool.all_indices() == [0, 1]        # both still exist
