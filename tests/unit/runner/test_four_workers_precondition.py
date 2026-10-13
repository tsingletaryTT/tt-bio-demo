"""tests/integration/conftest.pool_visibility_problem: the pure decision behind
the four-worker test's "needs a lease on every chip" failure. No device access."""
import importlib.util
from pathlib import Path

import pytest

_CONFTEST = Path(__file__).resolve().parents[3] / "tests" / "integration" / "conftest.py"


@pytest.fixture(scope="module")
def decide():
    spec = importlib.util.spec_from_file_location("_integration_conftest_under_test", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.pool_visibility_problem


def test_unset_visibility_proceeds(decide):
    assert decide(None, 4) is None
    assert decide("", 4) is None


def test_every_card_named_proceeds(decide):
    assert decide("a,b,c,d", 4) is None


def test_fewer_cards_named_than_the_box_has_is_a_problem(decide):
    msg = decide("0000:01:00.0,0000:02:00.0", 4)
    assert msg and "needs a lease on every chip" in msg and "2 of this box's 4" in msg


def test_a_single_card_is_a_problem(decide):
    assert decide("0000:01:00.0", 4)


def test_the_same_card_named_four_times_is_one_card(decide):
    # Four entries, one device: counting entries would let a one-chip lease
    # through on a four-card host.
    msg = decide("0000:01:00.0,0000:01:00.0,0000:01:00.0,0000:01:00.0", 4)
    assert msg and "1 of this box's 4" in msg


def test_repeats_differing_only_in_case_or_spacing_count_once(decide):
    msg = decide("0000:0A:00.0, 0000:0a:00.0 ,0000:0A:00.0,", 4)
    assert msg and "1 of this box's 4" in msg


def test_four_distinct_cards_with_a_repeat_still_proceed(decide):
    assert decide("a,b,c,d,a", 4) is None
