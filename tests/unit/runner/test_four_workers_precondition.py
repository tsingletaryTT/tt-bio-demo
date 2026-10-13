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
