"""The four-fold fixture: what `quad_fold.jsonl` has to be true of.

Task 13's brief supplies most of these verbatim; the ones after the marked
divider are this file's own additions, guarding the property the brief's set
does not: that the four folds are DISTINGUISHABLE from one another. Four
cells are a symmetric fixture by nature, and every test below that talks
about "the four folds" would still pass against a stream where all four are
byte-identical copies -- at which point a quad view that renders slot 2's
coordinates into cell 1 also passes, forever. See make_quad_fold.py's
docstring for the three axes it varies deliberately.

Lives in tests/unit/ (the venv-ui half) rather than tests/unit/runner/,
matching tests/unit/test_mock_runner.py's own precedent: `runner.mock` is
stdlib + `protocol.events` only, so it imports cleanly under venv-ui, and the
thing under test here is a UI-side fixture.
"""

import collections
import pathlib

import numpy as np

from protocol.events import EVENT_TYPES, unpack_coords
from runner.mock import load_stream          # stdlib+numpy only; safe in venv-ui

FIXTURE = pathlib.Path("tests/fixtures/streams/quad_fold.jsonl")


def _events():
    return load_stream(FIXTURE)


def test_every_line_decodes_as_a_protocol_event():
    for event in _events():
        assert event["type"] in EVENT_TYPES


def test_all_four_cards_fold():
    cards = {e["card"] for e in _events() if e["type"] == "job_start"}
    assert cards == {0, 1, 2, 3}


def test_more_than_one_fold_is_in_flight_at_once():
    """A fixture that serialises the four folds proves nothing about the
    thing this phase changes."""
    open_jobs, concurrent = set(), 0
    for event in _events():
        if event["type"] == "job_start":
            open_jobs.add(event["job_id"])
        elif event["type"] in ("job_done", "job_error"):
            open_jobs.discard(event["job_id"])
        concurrent = max(concurrent, len(open_jobs))
    assert concurrent >= 3


def test_frames_from_different_jobs_interleave():
    """Consecutive frames belonging to different jobs -- the exact case a
    single global LatestFrame buffer gets wrong."""
    frames = [e["job_id"] for e in _events() if e["type"] == "frame"]
    assert any(a != b for a, b in zip(frames, frames[1:]))


def test_every_job_id_is_unique_across_cards():
    starts = [e["job_id"] for e in _events() if e["type"] == "job_start"]
    assert len(starts) == len(set(starts))


def test_every_started_job_also_ends():
    """A job with no ending strands its cell in `folding` forever, which is
    a UI bug the fixture must be able to expose rather than cause."""
    started = {e["job_id"] for e in _events() if e["type"] == "job_start"}
    ended = {e["job_id"] for e in _events()
             if e["type"] in ("job_done", "job_error")}
    assert started == ended


def test_a_cards_next_job_starts_before_the_previous_ones_ribbon_lands():
    """The measured ordering this whole UI is arranged around:
    job_done(N) ... job_start(N+1) on the SAME card. Reproduced here so the
    per-slot deferred clear is exercised, not just described."""
    by_card = collections.defaultdict(list)
    order = {}
    for index, event in enumerate(_events()):
        if event["type"] == "job_start":
            by_card[event["card"]].append(event["job_id"])
            order[("start", event["job_id"])] = index
        elif event["type"] == "job_done":
            order[("done", event["job_id"])] = index
    overlaps = [(jobs[0], jobs[1]) for jobs in by_card.values() if len(jobs) > 1
                and order.get(("done", jobs[0]), 0) > order[("start", jobs[1])]]
    assert overlaps, "no card starts its second fold before the first finishes"


def test_at_least_one_fold_fails():
    """The booth's failure path is not exotic; it must be in the fixture or
    it is only ever tested by hand-built dicts."""
    assert any(e["type"] == "job_error" for e in _events())


def test_coordinates_are_decodable():
    for event in _events():
        if event["type"] == "frame":
            assert unpack_coords(event["coords_b64"]).shape[1] == 3


def test_the_fixture_matches_what_the_generator_produces(tmp_path):
    """A fixture that has drifted from its generator is a fixture nobody can
    regenerate. make_short_fold.py has the same relationship and no test
    holding it."""
    import subprocess
    import sys
    out = tmp_path / "quad_fold.jsonl"
    subprocess.run([sys.executable, "tests/fixtures/streams/make_quad_fold.py",
                    "--out", str(out)], check=True)
    assert out.read_text() == FIXTURE.read_text()


# ---------------------------------------------------------------------------
# Beyond the brief: the four folds must be TELLABLE APART.
#
# Everything above is satisfied by a stream of four identical folds. These
# are not. Each one names the axis a confused slot index would collapse.
# ---------------------------------------------------------------------------

def _frames_by_job():
    by_job = collections.defaultdict(list)
    for event in _events():
        if event["type"] == "frame":
            by_job[event["job_id"]].append(unpack_coords(event["coords_b64"]))
    return by_job


def test_the_frame_stream_switches_jobs_over_and_over():
    """The brief's `test_frames_from_different_jobs_interleave` above asks
    only for ONE adjacent pair of frames from different jobs -- which a
    fully SERIALISED stream still satisfies, at each of the four boundaries
    between one job's block of frames and the next's. Measured: serialising
    this fixture leaves it green.

    So this is the version that can fail. Real interleaving means the frame
    stream changes hands most of the time, not four times in total.
    """
    frames = [e["job_id"] for e in _events() if e["type"] == "frame"]
    n_jobs = len({e["job_id"] for e in _events() if e["type"] == "job_start"})
    switches = sum(1 for a, b in zip(frames, frames[1:]) if a != b)
    # A serialised stream can only ever reach n_jobs - 1 switches. Require
    # comfortably more than double that, which the committed fixture clears
    # by a wide margin (22 switches across 24 frames).
    assert switches >= 2 * n_jobs, (switches, n_jobs, frames)


def test_no_two_folds_have_the_same_atom_count():
    """Atom count is the cheapest thing a test can key on to prove a cell is
    showing ITS OWN fold. If two folds shared one, half the assertions the
    quad's own tests want to make would be ambiguous."""
    counts = [e["n_residues"] for e in _events() if e["type"] == "job_start"]
    assert len(counts) == len(set(counts)), counts
    frame_counts = {job: {c.shape[0] for c in clouds}
                    for job, clouds in _frames_by_job().items()}
    # Every frame of a job carries that job's own atom count, and no two
    # jobs' counts collide.
    assert all(len(sizes) == 1 for sizes in frame_counts.values()), frame_counts
    flat = [next(iter(sizes)) for sizes in frame_counts.values()]
    assert len(flat) == len(set(flat)), frame_counts


def test_no_two_folds_converge_to_the_same_shape():
    """A bar, a helix, a ring, a zigzag and a duplex -- not five copies of
    one cloud. Compared on the LAST frame of each job (where the noise has
    decayed and the geometry is what is left), by per-axis extent: an
    index bug that swapped two cells would be invisible against five clouds
    with the same bounding box."""
    finals = {job: clouds[-1] for job, clouds in _frames_by_job().items()}
    assert len(finals) == 5, sorted(finals)
    extents = {}
    for job, cloud in finals.items():
        extents[job] = tuple(
            round(float(v), 1) for v in (cloud.max(axis=0) - cloud.min(axis=0)))
    assert len(set(extents.values())) == len(extents), extents


def test_each_card_folds_a_differently_named_target():
    """`job_start.target_id` is what the caption says. Four cells all
    captioned TRPCAGE would hide a caption routed to the wrong cell."""
    by_card = {e["card"]: e["target_id"] for e in _events()
               if e["type"] == "job_start" and e["card"] != 0}
    # Card 0 folds twice (that is the pathology), so it is excluded from the
    # one-target-per-card claim and checked separately below.
    assert len(set(by_card.values())) == len(by_card) == 3, by_card
    card0 = [e["target_id"] for e in _events()
             if e["type"] == "job_start" and e["card"] == 0]
    assert len(card0) == 2 and card0[0] != card0[1], card0
    assert not (set(card0) & set(by_card.values())), (card0, by_card)


def test_the_generator_is_deterministic(tmp_path):
    """Two runs, byte-identical. Without this, the fixture-matches-generator
    test above could be green by luck on the run that happened to write the
    committed file and red for everyone else."""
    import subprocess
    import sys
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    for out in (first, second):
        subprocess.run([sys.executable,
                        "tests/fixtures/streams/make_quad_fold.py",
                        "--out", str(out)], check=True)
    assert first.read_bytes() == second.read_bytes()


def test_every_frame_agrees_with_its_own_payload_and_its_jobs_start():
    """Three numbers describe the same fold's size and nothing in the wire
    format makes them agree: a `frame`'s `n_atoms` field, the actual length
    of its packed coordinates, and its `job_start`'s `n_residues`. A
    generator that drew a cloud at one size and labelled it another produces
    a fixture the UI would render at full confidence and wrongly, and every
    other test in this file would stay green.

    `step`/`total` are checked here too: `total` is what a progress readout
    divides by, so a job whose last frame is step 4 of a declared total of 6
    leaves the booth's bar stuck at 67% forever.
    """
    declared = {e["job_id"]: e["n_residues"] for e in _events()
                if e["type"] == "job_start"}
    seen = collections.defaultdict(list)
    for event in _events():
        if event["type"] != "frame":
            continue
        job = event["job_id"]
        coords = unpack_coords(event["coords_b64"])
        assert coords.shape[0] == event["n_atoms"], (job, event["step"])
        assert event["n_atoms"] == declared[job], (job, event["step"])
        seen[job].append((event["step"], event["total"]))
    assert set(seen) == set(declared), (sorted(seen), sorted(declared))
    for job, pairs in seen.items():
        steps = [step for step, _total in pairs]
        totals = {total for _step, total in pairs}
        assert steps == list(range(1, len(steps) + 1)), (job, steps)
        assert totals == {len(steps)}, (job, totals, len(steps))


def test_every_frames_coordinates_are_finite_and_not_all_identical():
    """A generator bug that emitted the same cloud for every frame of a job
    (or a NaN one) would still satisfy `test_coordinates_are_decodable`, and
    the viewer would show a structure that never converges."""
    for job, clouds in _frames_by_job().items():
        assert len(clouds) >= 3, (job, len(clouds))
        for cloud in clouds:
            assert np.isfinite(cloud).all(), job
        assert not np.allclose(clouds[0], clouds[-1]), job
