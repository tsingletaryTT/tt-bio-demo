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

What this module deliberately does NOT automate: "confirm the structure
renders (ribbon, no clipping, sane pLDDT)" per this task's brief still
needs a human looking at the actual booth UI (or the gallery/showcase
screen) on real glass -- pLDDT sanity is checked here, but "does the
ribbon look right, un-clipped, at the booth's actual window size" is a
visual judgment this headless pytest run has no display to make. Run the
real booth (`./scripts/run-demo.sh`) against these targets once this
module is green, look at each one, and only THEN paste the printed numbers
into playlist/manifest.yaml's `expected_s:` fields.

Slow (three real folds -- expect a few times test_real_fold.py's own
runtime) and requires a card, same as test_real_fold.py; see that module's
own docstring for the general pattern this one follows.
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
    ("fkbp12", "affinity_fkg.yaml", 107),
    ("dhfr", "affinity_dhfr.yaml", 187),
    ("trypsin", "affinity_tryp.yaml", 223),
]

for _target_id, _filename, _n in NEW_TARGETS:
    _path = _EXAMPLES_DIR / _filename
    assert _path.is_file(), (
        f"vendored input for {_target_id!r} is missing: {_path} -- this "
        "should be tracked in git; see examples/affinity_*.yaml's own "
        "header comments")


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

    Returns {target_id: {"events": [...], "elapsed_s": float}}.
    """
    folder = Folder(device_id=tt_device)
    folder.load()
    results = {}
    try:
        for target_id, filename, n_residues in NEW_TARGETS:
            input_path = _EXAMPLES_DIR / filename
            events = []
            wall0 = time.monotonic()
            folder.fold(f"j-{target_id}", str(input_path), events.append,
                        target_id=target_id, n_residues=n_residues,
                        card=tt_device)
            elapsed_s = time.monotonic() - wall0
            results[target_id] = {"events": events, "elapsed_s": elapsed_s}
            print(f"\n[new-target-timing] {target_id}: {elapsed_s:.1f}s wall "
                  f"({n_residues} residues, cold on first target in this "
                  "run, warm thereafter -- see Trp-cage's own cold/warm "
                  "split in playlist/manifest.yaml for what that gap "
                  "typically looks like)")
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

    return results


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_the_event_sequence_is_well_formed(folded_new_targets, target_id, _filename, _n):
    kinds = [e["type"] for e in folded_new_targets[target_id]["events"]]
    assert kinds[0] == "job_start"
    assert kinds[-1] == "job_done"
    assert all(k in EVENT_TYPES for k in kinds)


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_the_fold_finishes_at_exactly_one(folded_new_targets, target_id, _filename, _n):
    """A bar that stalls short of 100% is a bar the visitor reads as
    broken -- same property test_real_fold.py pins for Trp-cage."""
    fracs = [e["frac"] for e in folded_new_targets[target_id]["events"]
             if e["type"] == "stage"]
    assert fracs[-1] == pytest.approx(1.0)


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_confidence_is_reported_in_percent(folded_new_targets, target_id, _filename, _n):
    done = folded_new_targets[target_id]["events"][-1]
    assert 0.0 <= done["mean_plddt"] <= 100.0
    assert done["mean_plddt"] > 1.0, "looks like an unscaled fraction"


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_a_structure_file_was_written(folded_new_targets, target_id, _filename, _n):
    done = folded_new_targets[target_id]["events"][-1]
    assert pathlib.Path(done["cif_path"]).is_file()


@pytest.mark.parametrize("target_id,_filename,_n", NEW_TARGETS)
def test_the_fold_actually_took_measurable_time(folded_new_targets, target_id, _filename, _n):
    """Sanity floor on the timing this whole module exists to produce: a
    reported elapsed_s of ~0 would mean the timer was placed wrong (e.g.
    around a cached/no-op call) rather than around a real fold."""
    assert folded_new_targets[target_id]["elapsed_s"] > 0.5
