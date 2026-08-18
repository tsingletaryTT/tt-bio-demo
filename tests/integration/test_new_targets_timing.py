"""Hardware validation pass for the three targets added to the playlist in
this task (see playlist/manifest.yaml's own header comment): FKBP12, human
DHFR, and bovine trypsin. All three ship with `expected_s` OMITTED --
nobody had access to this booth's Tenstorrent cards while this task was
done (they were running someone else's workload), so no real fold time
exists for any of them, and ui/playlist.py now treats that honestly as
"not yet measured" rather than requiring a guessed number.

This module is the single command that closes that gap once the cards are
free again:

    ./scripts/test.sh --hw -k new_targets_timing -s

(`-s` matters -- see FINAL SUMMARY below; pytest swallows stdout by
default and the whole point of this module is the printed numbers, not
just green checkmarks.) It does three things a later hardware pass needs,
per this task's brief:

  1. Folds each new target once on real silicon and times the wall clock.
  2. Runs the same sanity checks tests/integration/test_real_fold.py
     already trusts for Trp-cage (well-formed event sequence, progress
     that reaches exactly 1.0, a plausible pLDDT, a structure file that
     actually exists on disk) -- so a target that is somehow broken (a
     ligand chain type tt-bio's featurizer chokes on, a fold that hangs,
     an all-NaN structure) fails LOUDLY here instead of silently reaching
     the booth floor for the first time in front of a visitor.
  3. Prints a ready-to-paste `expected_s:` line for each target at the end
     of the run.
  4. Prints a second, separate PACING table (added 2026-08-17 with the HSA
     candidate): total time, when the first coordinates arrived, what share
     of the fold ran with nothing new to draw, and the longest the booth
     went with no event at all. Those numbers do not go into the manifest.
     They decide whether an entry belongs in it -- see
     `test_the_booth_is_never_silent_for_longer_than_the_budget`.

What this module deliberately does NOT automate: "confirm the structure
renders (ribbon, no clipping, sane pLDDT)" per this task's brief still
needs a human looking at the actual booth UI (or the gallery/showcase
screen) on real glass -- pLDDT sanity is checked here, but "does the
ribbon look right, un-clipped, at the booth's actual window size" is a
visual judgment this headless pytest run has no display to make. Run the
real booth (`./scripts/run-demo.sh`) against these targets once this
module is green, look at each one, and only THEN paste the printed numbers
into playlist/manifest.yaml's `expected_s:` fields.

Slow (one real fold per entry in NEW_TARGETS -- expect several times
test_real_fold.py's own runtime, and note that HSA at 585 residues is by
some margin the largest thing this repo has ever folded) and requires a
card, same as test_real_fold.py; see that module's own docstring for the
general pattern this one follows.

2026-08-17: this module was written for the three Phase-3b targets named
above, all of which now carry measured `expected_s` values in
playlist/manifest.yaml. It is kept -- rather than retired as done -- because
it is the repo's one harness for "fold a target on real silicon and find out
what it costs before writing any copy about it", and the tt-bio 0.6.3 upgrade
needed exactly that twice over.

NEW_TARGETS therefore now holds EVERY playlist target plus the candidate, not
just the original three. Two reasons, and the first is the load-bearing one:

  - A version bump invalidates measured copy. Every `expected_s` on the
    manifest, and the pLDDT figures the DNA and tRNA blurbs make claims about,
    were measured against tt-bio 0.6.2. 0.6.3 states its changes are bit-exact
    and its perf work is elsewhere, but "states" is not this project's standard
    for a number printed on a gallery card. Folding the whole set in one run
    is what turns that claim into evidence -- or catches it.
  - A pacing number for a 585-residue candidate means little without the
    20/24/76/107/187/223-residue rows beside it, folded in the same run, on
    the same card, at the same clock.
"""

import pathlib
import time

import pytest

from protocol.events import EVENT_TYPES
from runner.folder import Folder

_EXAMPLES_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "examples"

# (target_id, input filename, residue count) -- target_id and residue count
# match playlist/manifest.yaml's own entries exactly, so the printed
# summary at the end of this module's fixture can be pasted straight in
# without translating names. Residue counts are the protein chain's own
# length (examples/affinity_*.yaml's header comments); the ligand chain
# each of these inputs also carries adds a handful of atoms but is not
# itself a "residue" in the sense n_residues means elsewhere in this
# protocol (job_start's n_residues, matching test_real_fold.py's own usage
# for Trp-cage, is informational only -- nothing downstream keys behavior
# off of it).
NEW_TARGETS = [
    # Every target playlist/manifest.yaml ships, in manifest order, followed by
    # any candidate under evaluation. The whole list is folded in ONE run on
    # ONE resident Folder, which is what makes the numbers comparable to each
    # other -- and, after a tt-bio version bump, comparable to the previous
    # release's numbers as a set rather than one target at a time.
    #
    # Trp-cage leads because it is the manifest's first entry and the cheapest
    # fold here, so it pays the cold-model cost (~60s) that would otherwise
    # distort a more interesting target's number. See the fixture.
    ("trpcage", "trpcage_no_msa.yaml", 20),
    ("fkbp12", "affinity_fkg.yaml", 107),
    ("dhfr", "affinity_dhfr.yaml", 187),
    ("trypsin", "affinity_tryp.yaml", 223),
    ("dna", "dna_dickerson.yaml", 24),
    ("trna", "trna_phe.yaml", 76),
    # HSA is the 2026-08-17 addition and is NOT on the playlist -- it is a
    # candidate, folded here to find out what it costs. See
    # examples/hsa_no_msa.yaml's own header for why it was parked until
    # tt-bio 0.6.3 and what to expect from its pLDDT. It sits LAST on purpose:
    # the first target in this list pays the cold-fold cost (the model is
    # resident thereafter), and the number a visitor actually meets is the
    # warm one, so the candidate under evaluation gets measured in the state
    # the booth's attract loop actually keeps it in.
    ("hsa", "hsa_no_msa.yaml", 585),
]

# The longest a fold may go without emitting ANY event before this harness
# calls it a stall.
#
# This is the number that decides whether HSA can ship, and it is deliberately
# not a fold-duration limit. The booth's standing rule (CLAUDE.md, 2026-08-13,
# "the empty viewer") is that the visitor is never shown nothing: during the
# pre-diffusion stages the hero slot holds the PREVIOUS fold's structure,
# dimmed and captioned "Now folding X", and the rail shows a progress bar. All
# of that stays honest and legible for as long as the events keep coming --
# so an arbitrarily long fold is fine, and a SILENT one is not, because a
# progress bar that has not moved in half a minute reads as a hung booth no
# matter what is on the rest of the screen.
#
# 30s is a judgement, not a measurement, and it is the first thing to revisit
# if this trips: a failure here is a FINDING about pacing (report it, decide
# what the booth should do), never a flaky test to be silenced by raising the
# number without looking at what produced the gap.
_SILENCE_BUDGET_S = 30.0

for _target_id, _filename, _n in NEW_TARGETS:
    _path = _EXAMPLES_DIR / _filename
    assert _path.is_file(), (
        f"vendored input for {_target_id!r} is missing: {_path} -- this "
        "should be tracked in git; see each examples/*.yaml's own header "
        "comment for why it is vendored rather than read from a sibling "
        "tt-boltz checkout")


@pytest.fixture(scope="module")
def folded_new_targets(tt_device):
    """Fold every target in NEW_TARGETS once, on ONE resident Folder.

    Module-scoped and folds all three up front (not per-test) for the same
    reason test_real_fold.py's own `folded` fixture folds once per module:
    Folder.load() is expensive (a real checkpoint load onto the card) and
    is meant to be paid once per process lifetime, not once per test --
    exactly how runner/daemon.py itself uses a Folder in production. Folding
    three DIFFERENT targets back-to-back on the same resident Folder is
    also a more realistic rehearsal of the actual attract loop (which never
    reloads the model between targets) than folding each in isolation would
    be.

    Returns {target_id: {"events": [...], "offsets_s": [...],
                         "elapsed_s": float, "first_frame_s": float | None,
                         "max_silence_s": float}}.

    `offsets_s` is parallel to `events`: seconds from the start of the fold to
    the moment each event was emitted. The events themselves carry no
    timestamp (see protocol/events.py), and without one the two numbers that
    decide whether a long target can ship -- how long the viewer waits for the
    first coordinates, and the longest the booth goes with no news at all --
    cannot be recovered afterwards. So the emit callback stamps them as they
    arrive rather than reconstructing anything later.
    """
    folder = Folder(device_id=tt_device)
    folder.load()
    results = {}
    try:
        for target_id, filename, n_residues in NEW_TARGETS:
            input_path = _EXAMPLES_DIR / filename
            events, offsets_s = [], []
            wall0 = time.monotonic()

            def _emit(event, _events=events, _offsets=offsets_s, _t0=wall0):
                _offsets.append(time.monotonic() - _t0)
                _events.append(event)

            error = None
            try:
                folder.fold(f"j-{target_id}", str(input_path), _emit,
                            target_id=target_id, n_residues=n_residues,
                            card=tt_device)
            except Exception as exc:                      # noqa: BLE001
                # One target failing must not destroy the whole run. This
                # module exists to MEASURE candidates, and a candidate that
                # cannot fold is a result -- the most important one it can
                # produce. Aborting here would also take out the numbers for
                # every target after it, which is exactly what happened on
                # the tt-bio 0.6.3 upgrade: FKBP12 stopped folding and the
                # abort hid that all five other targets were fine and had in
                # fact got faster. Recorded, and asserted on per target below.
                error = f"{type(exc).__name__}: {exc}"
            elapsed_s = time.monotonic() - wall0

            # Time to the first coordinates. Only the `diffusion` stage emits
            # `frame`; msa/prep/trunk emit progress and no coordinates, so
            # this is exactly the window in which the hero slot has nothing
            # NEW to draw and is holding the previous fold's structure.
            first_frame_s = next(
                (off for off, e in zip(offsets_s, events)
                 if e["type"] == "frame"), None)

            # The longest the booth went with no event of any kind -- counting
            # the run-up to the first event, which is the gap the visitor
            # meets immediately after pressing a target.
            gaps = [b - a for a, b in zip([0.0] + offsets_s, offsets_s)]
            max_silence_s = max(gaps) if gaps else elapsed_s

            results[target_id] = {
                "events": events, "offsets_s": offsets_s,
                "elapsed_s": elapsed_s, "first_frame_s": first_frame_s,
                "max_silence_s": max_silence_s, "error": error,
            }
            if error is not None:
                print(f"\n[new-target-timing] {target_id}: FAILED after "
                      f"{elapsed_s:.1f}s -- {error}", flush=True)
                continue
            _ff = "never" if first_frame_s is None else f"{first_frame_s:.1f}s"
            print(f"\n[new-target-timing] {target_id}: {elapsed_s:.1f}s wall "
                  f"({n_residues} residues, cold on first target in this "
                  "run, warm thereafter -- see Trp-cage's own cold/warm "
                  "split in playlist/manifest.yaml for what that gap "
                  "typically looks like)")
            print(f"[new-target-timing] {target_id}: first coordinates at "
                  f"{_ff}, longest silence {max_silence_s:.1f}s "
                  f"(budget {_SILENCE_BUDGET_S:.0f}s), "
                  f"{len(events)} events")
    finally:
        folder.close()

    # FINAL SUMMARY: the whole reason this module exists. Printed once,
    # after every target has folded, so an operator scrolling back through
    # `-s` output sees one clean block to paste from rather than hunting
    # through interleaved per-test output for each number.
    print("\n" + "=" * 72)
    print("expected_s values measured this run -- paste into "
          "playlist/manifest.yaml (replacing the 'expected_s intentionally "
          "omitted' comment on each entry):")
    for target_id, filename, n_residues in NEW_TARGETS:
        elapsed_s = results[target_id]["elapsed_s"]
        print(f"  {target_id + ':':10s} expected_s: {elapsed_s:.1f}"
              f"   # measured {n_residues} residues, this run, see "
              "this module's own docstring for cold/warm caveats")
    print("=" * 72)

    # The second summary: the numbers that decide whether a LONG target can
    # ship at all. Separate from the paste block above because these do not go
    # into the manifest -- they go into the decision about whether an entry
    # belongs there, and into the copy that entry needs if it does.
    print("\nPacing -- what the visitor is looking at while it works:")
    print(f"  {'target':10s} {'total':>8s} {'1st coords':>11s} "
          f"{'held for':>9s} {'max gap':>8s}")
    for target_id, filename, n_residues in NEW_TARGETS:
        r = results[target_id]
        ff = r["first_frame_s"]
        ff_s = "never" if ff is None else f"{ff:.1f}s"
        held = "n/a" if ff is None else f"{100.0 * ff / r['elapsed_s']:.0f}%"
        print(f"  {target_id:10s} {r['elapsed_s']:7.1f}s {ff_s:>11s} "
              f"{held:>9s} {r['max_silence_s']:7.1f}s")
    print("  'held for' = share of the fold with no new coordinates, i.e. how "
          "long the\n  hero slot shows the PREVIOUS fold dimmed and captioned "
          "rather than this one.\n  On a chip's FIRST fold after launch there "
          "is no previous structure to hold,\n  so that same window is "
          "genuinely empty -- which is the case to look at.")
    print("=" * 72)

    return results


@pytest.fixture
def folded(folded_new_targets, target_id):
    """One target's result, or a clean failure naming what went wrong.

    Every test below goes through here so that a target which did not fold
    reports ITS OWN error once per test, instead of each test failing with
    whatever incidental KeyError/IndexError an empty event list produces."""
    r = folded_new_targets[target_id]
    if r["error"] is not None:
        pytest.fail(f"{target_id} did not fold: {r['error']}")
    return r


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_the_event_sequence_is_well_formed(folded, target_id, _filename, _n):
    kinds = [e["type"] for e in folded["events"]]
    assert kinds[0] == "job_start"
    assert kinds[-1] == "job_done"
    assert all(k in EVENT_TYPES for k in kinds)


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_the_fold_finishes_at_exactly_one(folded, target_id, _filename, _n):
    """A bar that stalls short of 100% is a bar the visitor reads as
    broken -- same property test_real_fold.py pins for Trp-cage."""
    fracs = [e["frac"] for e in folded["events"] if e["type"] == "stage"]
    assert fracs[-1] == pytest.approx(1.0)


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_confidence_is_reported_in_percent(folded, target_id, _filename, _n):
    done = folded["events"][-1]
    assert 0.0 <= done["mean_plddt"] <= 100.0
    assert done["mean_plddt"] > 1.0, "looks like an unscaled fraction"


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_a_structure_file_was_written(folded, target_id, _filename, _n):
    done = folded["events"][-1]
    assert pathlib.Path(done["cif_path"]).is_file()


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_the_fold_actually_took_measurable_time(folded, target_id, _filename, _n):
    """Sanity floor on the timing this whole module exists to produce: a
    reported elapsed_s of ~0 would mean the timer was placed wrong (e.g.
    around a cached/no-op call) rather than around a real fold."""
    assert folded["elapsed_s"] > 0.5


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_the_booth_is_never_silent_for_longer_than_the_budget(
        folded, target_id, _filename, _n):
    """The accept criterion for a long target: not "is it fast" but "is there
    always something to show".

    A fold of any length is fine at this booth. What is not fine is a stretch
    with no events at all, because every visitor-facing affordance during the
    pre-diffusion stages is driven by them -- the progress bar advances on
    `stage`, and the "Now folding X" caption over the held previous structure
    is an assertion this project only allows while it is being restated. A
    booth that goes quiet for half a minute is one a visitor walks away from,
    whatever is still rotating on screen.

    If this fails for HSA, the finding is about pacing and the fix is a booth
    decision (order it so it is never the first fold on a cold chip, give the
    long stages their own copy, or leave it off the playlist with a measured
    reason). It is not a reason to raise _SILENCE_BUDGET_S without looking.
    """
    r = folded
    assert r["max_silence_s"] <= _SILENCE_BUDGET_S, (
        f"{target_id} went {r['max_silence_s']:.1f}s with no event "
        f"(budget {_SILENCE_BUDGET_S:.0f}s). During that window the booth "
        "shows a frozen progress bar over a held structure. See this test's "
        "docstring before changing the budget.")


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_the_wait_for_the_first_coordinates_is_measured(
        folded, target_id, _filename, _n):
    """Every target must actually reach the diffusion stage and emit
    coordinates.

    A fold that completes without a single `frame` event is a fold this booth
    cannot show -- the hero slot would hold the previous structure from
    `job_start` right through to `job_done`, so a visitor who picked this
    target would never see it, only the one before it. That is a silent
    failure of the demo's entire premise (`ui/app.py` clears the viewer on the
    first real frame and nowhere else), and it is invisible to every other
    assertion in this module: the event sequence is well formed, progress
    reaches 1.0, pLDDT is plausible and a .cif is written, all without a
    single coordinate ever reaching the screen.
    """
    r = folded
    assert r["first_frame_s"] is not None, (
        f"{target_id} folded in {r['elapsed_s']:.1f}s and emitted no frame "
        "events at all -- nothing of this target would ever appear on the "
        "booth's hero slot")
    assert r["first_frame_s"] < r["elapsed_s"]
