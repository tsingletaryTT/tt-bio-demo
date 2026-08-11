import numpy as np
import pytest

from protocol.events import EVENT_TYPES, unpack_coords
from runner.shaping import (
    STAGE_ORDER,
    frame_event,
    plddt_to_percent,
    select_frame_steps,
)


def test_selects_about_the_target_number_of_frames():
    steps = select_frame_steps(201, target=30)
    assert 28 <= len(steps) <= 30


def test_always_keeps_the_first_and_last_step():
    steps = select_frame_steps(201, target=30)
    assert steps[0] == 0
    assert steps[-1] == 200


def test_selected_steps_are_sorted_and_unique():
    steps = select_frame_steps(201, target=30)
    assert steps == sorted(steps)
    assert len(steps) == len(set(steps))


def test_a_short_run_keeps_every_step_rather_than_inventing_any():
    steps = select_frame_steps(5, target=30)
    assert steps == [0, 1, 2, 3, 4]


def test_single_step_run_is_handled():
    assert select_frame_steps(1, target=30) == [0]


def test_zero_steps_selects_nothing():
    assert select_frame_steps(0, target=30) == []


def test_frame_event_round_trips_the_coordinates():
    coords = np.array([[1.5, -2.0, 3.25], [0.0, 0.5, -1.0]], dtype=np.float32)
    event = frame_event("j1", step=7, total=200, coords=coords)
    np.testing.assert_allclose(unpack_coords(event["coords_b64"]), coords, atol=1e-6)


def test_frame_event_matches_the_wire_contract():
    coords = np.zeros((154, 3), dtype=np.float32)
    event = frame_event("j1", step=7, total=200, coords=coords)
    assert event["type"] == "frame"
    assert event["type"] in EVENT_TYPES
    assert event["job_id"] == "j1"
    assert event["step"] == 7
    assert event["total"] == 200
    assert event["n_atoms"] == 154


def test_plddt_is_scaled_from_fraction_to_percent():
    # tt-bio returns conf["plddt"] as a fraction; the wire format is 0-100.
    assert plddt_to_percent(0.95) == pytest.approx(95.0)
    assert plddt_to_percent(0.4837) == pytest.approx(48.37)


def test_plddt_already_in_percent_is_left_alone():
    # Guard against double-scaling if tt-bio ever changes units.
    assert plddt_to_percent(95.0) == pytest.approx(95.0)


def test_stage_order_matches_the_protocol_table():
    assert STAGE_ORDER == ("msa", "prep", "trunk", "diffusion", "confidence", "saving")
