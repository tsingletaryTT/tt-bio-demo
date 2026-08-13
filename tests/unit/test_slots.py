"""Per-fold slot state: the decision layer for the quad view.

Given verbatim by Task 12 of docs/superpowers/plans/2026-08-13-multi-chip-folding.md.
Pure: no GTK, no display, no hardware.
"""

import pytest

from ui.slots import MAX_SLOTS, PICK_PENDING_WARN_S, SlotRouter, SlotState
from ui.states import showcase_ended


def _start(job_id="j1", card=0, target_id="trpcage"):
    return {"type": "job_start", "job_id": job_id, "target_id": target_id,
            "model": "protenix-v2", "card": card, "n_residues": 20}


def _done(job_id="j1"):
    return {"type": "job_done", "job_id": job_id, "cif_path": "/a.cif",
            "wall_s": 4.4, "mean_plddt": 95.3}


def _error(job_id="j1"):
    return {"type": "job_error", "job_id": job_id, "target_id": "t",
            "message": "boom"}


# ---- SlotState: one cell's dwell -----------------------------------------

def test_a_slot_starts_idle():
    assert SlotState().state == "idle"


def test_a_job_start_puts_the_slot_into_folding():
    slot = SlotState()
    assert slot.on_job_start(_start()) == "folding"
    assert slot.job_id == "j1"


def test_a_job_done_starts_this_slots_own_showcase():
    slot = SlotState()
    slot.on_job_start(_start())
    assert slot.on_job_done(_done()) == "showcase"


def test_points_are_suppressed_only_while_this_slot_showcases():
    """The whole reason the dwell is per slot: cell 1 is mid-diffusion while
    cell 0 holds a finished structure."""
    slot = SlotState()
    slot.on_job_start(_start())
    assert slot.points_are_visible
    slot.on_job_done(_done())
    assert not slot.points_are_visible


def test_a_ribbon_may_only_be_revealed_while_this_slot_showcases():
    slot = SlotState()
    slot.on_job_start(_start())
    assert not slot.ribbon_may_be_revealed
    slot.on_job_done(_done())
    assert slot.ribbon_may_be_revealed


def test_the_dwell_is_measured_from_the_reveal_not_from_job_done():
    """Unchanged rule, moved: job_done says the daemon finished, not that
    the visitor can see anything. Between them sit the ribbon build (up to
    ~1.2s) and the 0.8s cross-fade."""
    slot = SlotState(showcase_dwell_s=2.0)
    slot.on_job_start(_start())
    slot.on_job_done(_done())
    slot.tick(now=0.0)
    slot.tick(now=1.5)
    slot.on_structure_revealed()
    slot.tick(now=1.5)
    assert slot.tick(now=3.0) == "showcase", "the dwell restarted at the reveal"
    assert slot.tick(now=3.6) == "idle"


def test_a_job_start_for_a_new_fold_does_not_cut_a_dwell_short():
    """The daemon starts the next fold on this chip the instant the last one
    finishes. That ordering is exactly what the dwell exists to survive."""
    slot = SlotState(showcase_dwell_s=2.0)
    slot.on_job_start(_start("j1"))
    slot.on_job_done(_done("j1"))
    slot.tick(now=0.0)
    assert slot.on_job_start(_start("j2")) == "showcase"


def test_the_deferred_job_start_is_applied_when_the_dwell_expires():
    """The clear belongs to job_start; the dwell only delays it."""
    slot = SlotState(showcase_dwell_s=2.0)
    slot.on_job_start(_start("j1"))
    slot.on_job_done(_done("j1"))
    slot.tick(now=0.0)
    slot.on_job_start(_start("j2"))
    assert slot.tick(now=3.0) == "folding"
    assert slot.job_id == "j2"


def test_a_job_error_ends_a_fold_without_a_showcase():
    slot = SlotState()
    slot.on_job_start(_start())
    assert slot.on_job_error(_error()) == "idle"


def test_a_stale_job_error_does_not_disturb_the_current_fold():
    """Events for a job this slot has moved on from must be ignored, not
    applied -- a late job_error for j1 while j2 folds would blank the cell."""
    slot = SlotState()
    slot.on_job_start(_start("j1"))
    slot.on_job_start(_start("j2"))
    assert slot.on_job_error(_error("j1")) == "folding"
    assert slot.job_id == "j2"


def test_showcase_ended_is_the_shared_helper_and_works_on_a_slot():
    slot = SlotState(showcase_dwell_s=1.0)
    slot.on_job_start(_start())
    slot.on_job_done(_done())
    slot.tick(now=0.0)
    previous = slot.state
    assert showcase_ended(previous, slot.tick(now=5.0))


# ---- SlotRouter: which cell does this event belong to ---------------------

def test_one_slot_per_card_in_card_order():
    router = SlotRouter(cards=[0, 1, 2, 3])
    assert len(router.slots) == 4
    assert [router.slot_for_card(c) for c in (0, 1, 2, 3)] == [0, 1, 2, 3]


def test_a_booth_with_fewer_chips_gets_fewer_slots():
    router = SlotRouter(cards=[0, 2])
    assert len(router.slots) == 2
    assert router.slot_for_card(2) == 1
    assert router.slot_for_card(1) is None


def test_more_chips_than_cells_does_not_overflow_the_quad():
    """A five-card machine folds on five and shows four. Better than
    crashing, and better than silently drawing the fifth over the first."""
    router = SlotRouter(cards=[0, 1, 2, 3, 4, 5])
    assert len(router.slots) == MAX_SLOTS
    assert router.slot_for_card(5) is None


def test_a_job_start_binds_its_job_id_to_its_cards_slot():
    router = SlotRouter(cards=[0, 1, 2, 3])
    assert router.on_event(_start("j9", card=2)) == 2
    assert router.slot_for_job("j9") == 2


def test_every_later_event_of_a_job_routes_by_job_id_alone():
    """Only job_start carries `card`. Everything after it carries job_id and
    nothing else -- which is exactly why the UI keys by job_id."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.on_event(_start("j9", card=3))
    assert router.on_event({"type": "stage", "job_id": "j9",
                            "stage": "diffusion", "frac": 0.5}) == 3
    assert router.on_event(_done("j9")) == 3


def test_an_event_for_an_unknown_job_belongs_to_no_slot():
    """A frame that beats its own job_start through the UI's idle queue must
    not be drawn into whichever cell happens to be first."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    assert router.on_event({"type": "frame", "job_id": "ghost", "step": 1,
                            "total": 200, "n_atoms": 20,
                            "coords_b64": "AAAA"}) is None


def test_four_concurrent_folds_stay_in_their_own_cells():
    router = SlotRouter(cards=[0, 1, 2, 3])
    for card in (0, 1, 2, 3):
        router.on_event(_start(f"j{card}", card=card))
    router.on_event(_done("j2"))
    assert [s.state for s in router.slots] == ["folding", "folding",
                                               "showcase", "folding"]


def test_a_cards_second_fold_replaces_the_first_in_the_same_cell():
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.on_event(_start("j1", card=1))
    router.on_event(_start("j2", card=1))
    assert router.slot_for_job("j2") == 1
    assert router.slots[1].job_id == "j2"


def test_the_job_id_map_does_not_grow_without_bound():
    """An all-day booth folds thousands of jobs. A dict that remembers every
    one is a leak with a screen attached."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    for n in range(500):
        router.on_event(_start(f"j{n}", card=n % 4))
    assert len(router.tracked_jobs) <= 4 * MAX_SLOTS


def test_tick_reports_only_the_slots_that_changed():
    router = SlotRouter(cards=[0, 1, 2, 3], showcase_dwell_s=1.0)
    router.on_event(_start("j0", card=0))
    router.on_event(_done("j0"))
    router.slots[0].on_structure_revealed()
    router.tick(now=0.0)
    assert router.tick(now=0.1) == []
    assert router.tick(now=5.0) == [0]


# ---- the focus slot ------------------------------------------------------

def test_with_no_pick_the_focus_follows_the_newest_finished_structure():
    router = SlotRouter(cards=[0, 1, 2, 3])
    for card in (0, 1, 2, 3):
        router.on_event(_start(f"j{card}", card=card))
    router.on_event(_done("j2"))
    assert router.focus_slot == 2


def test_a_visitors_pick_takes_the_focus_when_its_fold_starts():
    """Spec: 'a visitor's pick becomes the hero of the quad while the other
    three chips continue the attract playlist.'"""
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.on_event(_start("j0", card=0, target_id="attract-a"))
    router.select_target("hemoglobin")
    router.on_event(_start("j3", card=3, target_id="hemoglobin"))
    assert router.focus_slot == 3


def test_a_pick_does_not_move_the_focus_before_its_fold_starts():
    """The daemon dispatches the pick to the next chip to free (Task 9), so
    this is usually a wait of seconds -- but it is never zero, and moving
    the focus to a cell folding something else in the meantime would point
    the hero cell at the wrong protein."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.on_event(_start("j0", card=0, target_id="attract-a"))
    router.on_event(_done("j0"))
    router.select_target("hemoglobin")
    assert router.focus_slot == 0


def test_the_picked_focus_survives_other_cells_finishing():
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.select_target("hemoglobin")
    router.on_event(_start("j3", card=3, target_id="hemoglobin"))
    router.on_event(_start("j1", card=1, target_id="attract-b"))
    router.on_event(_done("j1"))
    assert router.focus_slot == 3


def test_the_pick_is_released_when_its_fold_ends():
    """Otherwise the focus stays pinned to a finished cell for the rest of
    the day and the booth stops following the action."""
    router = SlotRouter(cards=[0, 1, 2, 3], showcase_dwell_s=1.0)
    router.select_target("hemoglobin")
    router.on_event(_start("j3", card=3, target_id="hemoglobin"))
    router.on_event(_done("j3"))
    router.slots[3].on_structure_revealed()
    router.tick(now=0.0)
    router.tick(now=5.0)
    assert router.selected_target is None


def test_an_empty_booth_focuses_the_first_cell():
    assert SlotRouter(cards=[0, 1, 2, 3]).focus_slot == 0


# ---- the pick, between the tap and the fold ------------------------------

def test_there_is_no_pick_status_without_a_pick():
    assert SlotRouter(cards=[0, 1, 2, 3]).pick_status(now=0.0) is None


def test_a_pick_is_acknowledgeable_the_instant_it_is_made():
    """Nothing about this may wait on the daemon answering: the socket may
    be down, the daemon may be mid-fold on all four chips, and the visitor
    is standing there either way."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.select_target("hemoglobin", now=0.0)
    assert router.pick_status(now=0.0) == "queued"


def test_a_long_wait_is_named_differently_so_the_booth_can_say_more():
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.select_target("hemoglobin", now=100.0)
    assert router.pick_status(now=100.0 + PICK_PENDING_WARN_S - 0.1) == "queued"
    assert router.pick_status(now=100.0 + PICK_PENDING_WARN_S + 0.1) == "waiting"


def test_the_status_becomes_folding_when_the_picked_target_starts():
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.select_target("hemoglobin", now=0.0)
    router.on_event(_start("j3", card=3, target_id="hemoglobin"))
    assert router.pick_status(now=999.0) == "folding"


def test_a_pick_for_a_target_already_folding_is_folding_at_once():
    """The daemon queues nothing in this case (Task 9). A router that waited
    for a job_start that will never come would leave the booth saying NEXT
    UP forever about something already on screen."""
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.on_event(_start("j2", card=2, target_id="hemoglobin"))
    router.select_target("hemoglobin", now=0.0)
    assert router.pick_status(now=0.0) == "folding"
    assert router.focus_slot == 2


def test_releasing_the_pick_clears_its_status():
    router = SlotRouter(cards=[0, 1, 2, 3])
    router.select_target("hemoglobin", now=0.0)
    router.release_target()
    assert router.pick_status(now=0.0) is None
