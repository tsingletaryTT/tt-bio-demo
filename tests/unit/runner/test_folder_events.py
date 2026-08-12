import pytest

from protocol.events import EVENT_TYPES
from runner.folder import fold_event_sequence


def _result(**over):
    base = {"cif_path": "/tmp/out.cif", "wall_s": 5.73, "mean_plddt": 95.2}
    base.update(over)
    return base


def test_the_sequence_starts_with_job_start_and_ends_with_job_done():
    events = fold_event_sequence(
        stages=[("prep", 0.15), ("trunk", 0.4), ("diffusion", 0.9)],
        frames=[{"type": "frame", "job_id": "j1", "step": 0, "total": 200,
                 "n_atoms": 154, "coords_b64": ""}],
        result=_result(),
        job_id="j1", target_id="trpcage", model="protenix-v2",
        card=0, n_residues=20,
    )
    assert events[0]["type"] == "job_start"
    assert events[-1]["type"] == "job_done"


def test_every_emitted_event_is_a_known_protocol_type():
    events = fold_event_sequence(
        stages=[("prep", 0.15)], frames=[], result=_result(),
        job_id="j1", target_id="t", model="protenix-v2", card=0, n_residues=20,
    )
    for event in events:
        assert event["type"] in EVENT_TYPES


def test_job_start_carries_what_the_ui_needs_to_label_the_screen():
    events = fold_event_sequence(
        stages=[], frames=[], result=_result(),
        job_id="j1", target_id="trpcage", model="protenix-v2", card=2, n_residues=20,
    )
    start = events[0]
    assert start["target_id"] == "trpcage"
    assert start["model"] == "protenix-v2"
    assert start["card"] == 2
    assert start["n_residues"] == 20


def test_job_done_reports_plddt_in_percent_not_as_a_fraction():
    events = fold_event_sequence(
        stages=[], frames=[], result=_result(mean_plddt=0.952),
        job_id="j1", target_id="t", model="protenix-v2", card=0, n_residues=20,
    )
    assert events[-1]["mean_plddt"] == pytest.approx(95.2)


def test_frames_appear_between_the_stages_and_the_completion():
    frames = [{"type": "frame", "job_id": "j1", "step": s, "total": 200,
               "n_atoms": 154, "coords_b64": ""} for s in (0, 100, 200)]
    events = fold_event_sequence(
        stages=[("prep", 0.15), ("diffusion", 0.9)], frames=frames, result=_result(),
        job_id="j1", target_id="t", model="protenix-v2", card=0, n_residues=20,
    )
    kinds = [e["type"] for e in events]
    assert kinds.index("stage") < kinds.index("frame")
    assert kinds.index("frame") < kinds.index("job_done")


def test_all_six_protocol_stages_can_be_expressed():
    stages = [("msa", 0.05), ("prep", 0.15), ("trunk", 0.4),
              ("diffusion", 0.9), ("confidence", 0.95), ("saving", 0.99)]
    events = fold_event_sequence(
        stages=stages, frames=[], result=_result(),
        job_id="j1", target_id="t", model="protenix-v2", card=0, n_residues=20,
    )
    emitted = [e["stage"] for e in events if e["type"] == "stage"]
    assert emitted == ["msa", "prep", "trunk", "diffusion", "confidence", "saving"]
