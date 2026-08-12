"""End-to-end: a real fold on real silicon, producing real protocol events.

Slow (tens of seconds) and requires a card. Run with:
    .venvs/venv-runner/bin/python3 -m pytest tests/integration -v
"""

import pathlib

import numpy as np
import pytest

from protocol.events import EVENT_TYPES, unpack_coords
from runner.folder import Folder

# Vendored in this repo (examples/trpcage_no_msa.yaml), not read from a
# sibling tt-boltz checkout. This module used to point at
# ~/code/tt-boltz/examples/trpcage_no_msa.yaml -- an absolute path into a
# different repository this one does not control -- and skipped the whole
# module (all 7 tests) via pytest.mark.skipif if that path was ever
# missing. That is a silent-pass hazard: the runner half of scripts/test.sh
# still collects its other ~120 unit tests either way, so the "zero tests
# matched" exit-5 guard never fires, and the suite prints OVERALL: PASS
# with zero coverage of the demo's headline mechanism (a real fold actually
# condensing out of noise) if that sibling path ever moved. Vendoring the
# input removes the cross-repo dependency outright, so the file existing is
# now a fact about this repo, not about the environment it happens to run
# in -- if it is ever missing, that is a real problem with this checkout,
# not a legitimate reason to skip quietly, hence the loud assert (not
# skipif) below. The legitimate skip reason that remains -- no card present
# -- is still handled, by the `tt_device` fixture in conftest.py.
INPUT = pathlib.Path(__file__).resolve().parent.parent.parent / "examples" / "trpcage_no_msa.yaml"
assert INPUT.is_file(), (
    f"vendored integration-test input is missing: {INPUT} -- this should "
    "be tracked in git; see this module's own comment above")


@pytest.fixture(scope="module")
def folded(tt_device):
    folder = Folder(device_id=tt_device)
    folder.load()
    events = []
    try:
        folder.fold("j1", str(INPUT), events.append,
                    target_id="trpcage", n_residues=20, card=tt_device)
    finally:
        folder.close()
    return events


def test_the_event_sequence_is_well_formed(folded):
    kinds = [e["type"] for e in folded]
    assert kinds[0] == "job_start"
    assert kinds[-1] == "job_done"
    assert all(k in EVENT_TYPES for k in kinds)


def test_about_thirty_frames_are_emitted_not_two_hundred(folded):
    frames = [e for e in folded if e["type"] == "frame"]
    assert 25 <= len(frames) <= 32, f"got {len(frames)} frames"


def test_frames_carry_all_atom_coordinates(folded):
    frames = [e for e in folded if e["type"] == "frame"]
    coords = unpack_coords(frames[0]["coords_b64"])
    assert coords.ndim == 2 and coords.shape[1] == 3
    # 20 residues folded all-atom; the spike measured 154 for this input.
    assert coords.shape[0] > 100


def test_the_structure_actually_condenses(folded):
    """The demo's whole visual premise: noise becomes structure."""
    frames = [e for e in folded if e["type"] == "frame"]

    def radius_of_gyration(event):
        c = unpack_coords(event["coords_b64"])
        return float(np.sqrt(((c - c.mean(0)) ** 2).sum(1).mean()))

    first, last = radius_of_gyration(frames[0]), radius_of_gyration(frames[-1])
    assert first > last * 50, f"expected a large collapse, got {first:.1f} -> {last:.1f}"


def test_frame_step_indices_are_pinned_against_real_data(folded):
    """Regression: on_frame maps dump_fn's raw step to a wire index via
    `index = step + 1` (dump_fn's step -1, the initial noise draw, is the
    first frame the sampler ever emits, and must appear on the wire as
    index 0 -- not -1, which the protocol's `frame.step` field, an
    unsigned-feeling count out of `total`, was never meant to carry).

    A test that only counts frames (test_about_thirty_frames_are_emitted...)
    cannot catch a dropped `+1`: an off-by-one shifts every step value but
    changes the *count* of emitted frames not at all, since
    select_frame_steps() picks indices out of a fixed range regardless of
    what those indices are then labeled. This test runs against a real
    fold's real dump_fn call sequence (steps -1..199 -> wire indices
    0..200, exactly the mapping the spike measured) and pins the two values
    an off-by-one would actually move: the first frame's step (must be the
    initial noise draw, wire index 0) and the last frame's step (must be
    the final denoising step, wire index 200 = n_step).
    """
    frames = [e for e in folded if e["type"] == "frame"]
    assert frames[0]["step"] == 0, (
        "the initial noise draw (dump_fn step -1) must be wire index 0")
    assert frames[-1]["step"] == 200, (
        "the final denoising step (dump_fn step 199, n_step=200) must be "
        "wire index 200")
    # And monotonically increasing in between -- subsampling picks indices
    # in ascending order out of a fixed step count; a mapping bug that
    # scrambled order rather than merely shifting it would still be wrong.
    steps = [f["step"] for f in frames]
    assert steps == sorted(steps)


def test_confidence_is_reported_in_percent(folded):
    done = folded[-1]
    assert 0.0 <= done["mean_plddt"] <= 100.0
    assert done["mean_plddt"] > 1.0, "looks like an unscaled fraction"


def test_a_structure_file_was_written(folded):
    assert pathlib.Path(folded[-1]["cif_path"]).is_file()
