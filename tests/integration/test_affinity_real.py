"""End-to-end: a real affinity question answered on real silicon.

Slow (~10-80s depending on target -- see docs/spike-nesso1-affinity.md
section 3.1/4) and requires a card. Run with:
    gozer run --chips 1 --who "claude:affinity-qa" --reason "..." -- \
      .venvs/venv-runner/bin/python3 -m pytest tests/integration/test_affinity_real.py -v

Confirmed in the spike (docs/spike-nesso1-affinity.md section 3.4) that this
needs no prior fold and never imports tt_bio.protenix/tt_bio.opendde -- so
unlike test_real_fold.py's `folded` fixture, this one never touches Folder
at all.
"""

import pathlib

import pytest

from runner.affinity import AffinityScorer

# Vendored in this repo (examples/affinity_dhfr.yaml), same rationale as
# test_real_fold.py's INPUT: a manifest entry pointing outside this repo is a
# path this repo does not control, and the loud assert below (not skipif)
# makes a missing vendored file a real problem with this checkout, not a
# legitimate reason to silently skip. The one legitimate skip reason -- no
# card present -- is handled by the `tt_device` fixture in conftest.py.
INPUT = pathlib.Path(__file__).resolve().parent.parent.parent / "examples" / "affinity_dhfr.yaml"
assert INPUT.is_file(), (
    f"vendored integration-test input is missing: {INPUT} -- this should "
    "be tracked in git; see this module's own comment above")


@pytest.fixture(scope="module")
def scorer(tt_device):
    """One resident AffinityScorer, shared by every test in this module --
    AffinityScorer has no close() (deliberately out of this task's scope;
    see runner/affinity.py's module docstring and the plan's Task 6, which
    wires device lifecycle through a subprocess worker instead), so opening
    a second one in the same process without releasing this one first would
    contend for the same device. One instance, loaded once, reused."""
    scorer = AffinityScorer(device_id=tt_device)
    scorer.load()
    return scorer


@pytest.fixture(scope="module")
def answered(scorer):
    events = []
    scorer.score("q1", "dhfr", str(INPUT), events.append)
    return events


def test_the_event_sequence_is_well_formed(answered):
    kinds = [e["type"] for e in answered]
    assert kinds[0] == "answer_start"
    assert kinds[-1] in ("answer_done", "answer_error")


def test_scores_a_real_question_on_real_hardware(answered):
    assert answered[-1]["type"] == "answer_done", (
        f"expected a successful score, got: {answered[-1]}")
    assert isinstance(answered[-1]["score"], (int, float))
    assert 0.0 <= answered[-1]["score"] <= 1.0, (
        "affinity_probability_binary is documented as a literal [0, 1] "
        "probability -- see docs/spike-nesso1-affinity.md section 3.3")


def test_the_pred_value_is_also_reported(answered):
    """Spec section 7's content-honesty rule: the continuous score is
    secondary/supporting detail, but it must still be on the wire -- not
    collapsed away in favor of the single probability."""
    assert isinstance(answered[-1]["affinity_pred_value"], (int, float))


def test_dhfr_methotrexate_scores_as_a_plausible_binder(answered):
    """Sanity check against known chemistry, same reasoning as the spike's
    own reading of this exact complex (section 3.3): methotrexate is a
    famously nanomolar DHFR inhibitor, so nesso1's own binder probability
    for this pair should land clearly on the "binder" side, not near 0.5."""
    assert answered[-1]["score"] > 0.5


def test_a_second_question_on_the_same_target_also_succeeds(scorer, answered):
    """The model stays resident across questions (this project's whole
    residency pattern, mirrored from Folder) -- confirm a second score()
    call on the SAME AffinityScorer instance still works, rather than only
    ever being exercised once per process. Depends on `answered` so this
    runs after the first score(), making this the "repeat target" call the
    spike's section 4 measured (ESM-2 embedding cache hits, host prep does
    not get materially cheaper)."""
    events = []
    scorer.score("q2", "dhfr", str(INPUT), events.append)
    assert events[-1]["type"] == "answer_done"
