"""Tests for ui/playlist.py: the booth's playlist manifest.

Test 1 (`test_loads_the_shipped_manifest`) and test 2
(`test_every_shipped_target_points_at_a_file_that_exists`) run against the
REAL shipped manifest (`playlist/manifest.yaml`) rather than a synthetic
fixture -- deliberately, per the task brief: it means a bad entry in the
real playlist fails CI instead of failing quietly at the booth, and it
exercises the `model` default-fallback path for real (the shipped entry
omits `model`; see manifest.yaml's own comment).

Tests 5 and 6 (`test_a_missing_thumbnail_is_tolerated`,
`test_duplicate_ids_are_rejected`) were left as `...` in the brief for this
task to write. Both are built to reject at least one plausible cheat
implementation, not just to assert the literal wording of the brief's
docstring:

- test 5 loads TWO entries, one with a thumbnail and one without, and
  checks BOTH: an implementation that always sets `thumbnail = None`
  regardless of the manifest (satisfying only the "missing" half) would
  fail the "has one" assertion.
- test 6 loads a control manifest with two DIFFERENT ids first and asserts
  it succeeds, before checking that two entries sharing one id raises --
  otherwise an implementation that raises PlaylistError unconditionally
  (never actually checking for duplicates) would still make the naive
  version of this test pass.

`test_expected_s_*` (added when the playlist grew past its first target):
`expected_s` became optional, meaning "not yet measured on real hardware"
when absent or explicit YAML `null`, rather than a required field every
entry must fabricate a number for -- see ui/playlist.py's own docstring.
Three tests, not one, because there are three genuinely different
behaviors to pin down: absent stays None, explicit null ALSO stays None
(the two spellings of "not yet measured" must not diverge), and a PRESENT
but non-numeric value is still a loud PlaylistError -- the leniency is for
"nothing was said," not for "something wrong was said."
"""

import pytest

from ui.playlist import PlaylistError, Target, load_playlist, select_targets
from ui.playlist import main as playlist_main


def test_loads_the_shipped_manifest():
    targets = load_playlist("playlist/manifest.yaml")
    assert len(targets) >= 1
    assert all(isinstance(t, Target) for t in targets)


def test_every_shipped_target_points_at_a_file_that_exists():
    """A manifest entry naming a missing input is a booth that fails mid-loop."""
    for t in load_playlist("playlist/manifest.yaml"):
        assert t.input_path.is_file(), f"{t.id} names a missing input: {t.input_path}"


def test_a_missing_required_field_names_the_offending_entry(tmp_path):
    m = tmp_path / "m.yaml"
    m.write_text("- id: x\n  name: X\n")          # no input
    with pytest.raises(PlaylistError, match="x"):
        load_playlist(m)


def test_a_missing_manifest_raises_a_clear_error(tmp_path):
    with pytest.raises(PlaylistError, match="not found"):
        load_playlist(tmp_path / "nope.yaml")


def test_a_missing_thumbnail_is_tolerated(tmp_path):
    """Thumbnails are Phase 4 content; the gallery must work without them."""
    (tmp_path / "a.yaml").write_text("version: 1\n")
    thumb = tmp_path / "b.png"
    thumb.write_bytes(b"not a real png, existence is all that matters here")

    m = tmp_path / "m.yaml"
    m.write_text(
        "- id: no-thumb\n"
        "  input: a.yaml\n"
        "  name: No Thumb\n"
        "  blurb: this one has no thumbnail\n"
        "  expected_s: 5.0\n"
        "- id: has-thumb\n"
        "  input: a.yaml\n"
        "  name: Has Thumb\n"
        "  blurb: this one has a thumbnail\n"
        "  expected_s: 5.0\n"
        "  thumbnail: b.png\n"
    )

    by_id = {t.id: t for t in load_playlist(m)}
    assert by_id["no-thumb"].thumbnail is None
    # Not just "doesn't crash" -- an entry that DOES name a thumbnail must
    # still get it, resolved against the manifest's own directory like any
    # other path. Catches an implementation that tolerates a missing
    # thumbnail by ignoring the field unconditionally.
    assert by_id["has-thumb"].thumbnail == thumb.resolve()


def test_every_shipped_thumbnail_points_at_a_file_that_exists():
    """The gallery falls back to a placeholder for a target with NO
    thumbnail, which is by design -- but a target that names one and whose
    file is missing is a typo shipping as a silent placeholder. That path
    must stay loud here, where it fails CI rather than at the booth.
    """
    named = [t for t in load_playlist("playlist/manifest.yaml") if t.thumbnail]
    # Guards this test against becoming vacuous the day someone drops the
    # thumbnails: "all zero of them exist" must not read as a pass.
    assert named, "no shipped target names a thumbnail at all"
    for t in named:
        assert t.thumbnail.is_file(), (
            f"{t.id} names a missing thumbnail: {t.thumbnail}")


def test_shipped_thumbnails_are_small_enough_to_ship():
    """Thumbnails go into the repo AND into the .deb, so a stray multi-MB
    render is a packaging regression. 400 KB each is the packaging plan's
    assertion; a ribbon on a flat ground compresses far below it."""
    named = [t for t in load_playlist("playlist/manifest.yaml") if t.thumbnail]
    assert named, "no shipped target names a thumbnail at all"
    for t in named:
        size = t.thumbnail.stat().st_size
        assert size < 400_000, f"{t.id}: {t.thumbnail.name} is {size} bytes"


def test_a_missing_tagline_is_tolerated(tmp_path):
    """`tagline` is optional the same way `thumbnail` is: a target added
    before anyone has written one must still load, and the caption under
    the render then shows its name alone.

    Loads BOTH shapes and checks both, so an implementation that always
    sets `tagline = None` (satisfying only the "missing" half) fails here.
    """
    (tmp_path / "a.yaml").write_text("version: 1\n")
    m = tmp_path / "m.yaml"
    m.write_text(
        "- id: no-tagline\n"
        "  input: a.yaml\n"
        "  name: No Tagline\n"
        "  blurb: this one has no tagline\n"
        "- id: has-tagline\n"
        "  input: a.yaml\n"
        "  name: Has Tagline\n"
        "  blurb: this one has a tagline\n"
        "  tagline: one short sentence\n"
        # Whitespace-only is the third spelling of "nothing was said" and
        # must land on None too -- otherwise the caption renders a blank
        # second line under the name.
        "- id: blank-tagline\n"
        "  input: a.yaml\n"
        "  name: Blank Tagline\n"
        "  blurb: this one has a whitespace tagline\n"
        "  tagline: '   '\n"
    )

    by_id = {t.id: t for t in load_playlist(m)}
    assert by_id["no-tagline"].tagline is None
    assert by_id["blank-tagline"].tagline is None
    assert by_id["has-tagline"].tagline == "one short sentence"


def test_every_shipped_target_has_a_tagline():
    """Not a schema requirement (the loader tolerates its absence, and must
    -- see above) but a content one: every target currently in the booth's
    rotation should say what it is under the render. A new entry failing
    this is a reminder to write one, not a crash."""
    for t in load_playlist("playlist/manifest.yaml"):
        assert t.tagline, f"{t.id} has no tagline for the caption"


def test_expected_s_absent_means_not_yet_measured(tmp_path):
    """Omitting `expected_s` entirely is not an error -- it means this
    target has not been folded on real hardware yet (this task's brief).
    `Target.expected_s` must come back `None`, never a fabricated float."""
    (tmp_path / "a.yaml").write_text("version: 1\n")
    m = tmp_path / "m.yaml"
    m.write_text(
        "- id: unmeasured\n"
        "  input: a.yaml\n"
        "  name: Unmeasured\n"
        "  blurb: no expected_s at all\n"
    )
    targets = load_playlist(m)
    assert len(targets) == 1
    assert targets[0].expected_s is None


def test_expected_s_explicit_null_also_means_not_yet_measured(tmp_path):
    """The other spelling of "not yet measured": `expected_s: null` (or,
    equivalently, a bare `expected_s:` with nothing after the colon) must be
    tolerated identically to the field being absent outright -- not treated
    as a "missing required field" and not coerced into some other number."""
    (tmp_path / "a.yaml").write_text("version: 1\n")
    m = tmp_path / "m.yaml"
    m.write_text(
        "- id: unmeasured\n"
        "  input: a.yaml\n"
        "  name: Unmeasured\n"
        "  blurb: expected_s is explicit null\n"
        "  expected_s: null\n"
    )
    targets = load_playlist(m)
    assert targets[0].expected_s is None


def test_expected_s_still_validated_as_a_number_when_present(tmp_path):
    """A target that DOES supply expected_s keeps the original contract: a
    non-numeric value is still a loud PlaylistError naming the entry, not
    silently treated as 'not yet measured' -- that leniency is reserved for
    absent/null, not for "someone typed garbage here"."""
    (tmp_path / "a.yaml").write_text("version: 1\n")
    m = tmp_path / "m.yaml"
    m.write_text(
        "- id: bogus\n"
        "  input: a.yaml\n"
        "  name: Bogus\n"
        "  blurb: expected_s is not a number\n"
        "  expected_s: soon\n"
    )
    with pytest.raises(PlaylistError, match="bogus"):
        load_playlist(m)


def test_duplicate_ids_are_rejected(tmp_path):
    """Two entries with one id makes 'which did the visitor pick' ambiguous."""
    (tmp_path / "a.yaml").write_text("version: 1\n")

    def manifest_text(second_id):
        return (
            "- id: dup\n"
            "  input: a.yaml\n"
            "  name: First\n"
            "  blurb: the first entry\n"
            "  expected_s: 5.0\n"
            f"- id: {second_id}\n"
            "  input: a.yaml\n"
            "  name: Second\n"
            "  blurb: the second entry\n"
            "  expected_s: 5.0\n"
        )

    # Control: two DIFFERENT ids must load cleanly. Without this, an
    # implementation that raises PlaylistError unconditionally (never
    # actually comparing ids) would still pass the assertion below.
    ok = tmp_path / "ok.yaml"
    ok.write_text(manifest_text("unique"))
    assert len(load_playlist(ok)) == 2

    bad = tmp_path / "bad.yaml"
    bad.write_text(manifest_text("dup"))
    with pytest.raises(PlaylistError, match="dup"):
        load_playlist(bad)


# ---------------------------------------------------------------------------
# select_targets + the module's CLI face.
#
# Both exist for scripts/run-demo.sh, which builds the daemon's fold inputs
# from the same manifest (and the same selection) the UI's gallery is built
# from -- see this module's docstring and tests/unit/test_run_demo_sh.py for
# the defect that motivated it.
# ---------------------------------------------------------------------------

def _two_target_manifest(tmp_path):
    (tmp_path / "a.yaml").write_text("stub\n")
    (tmp_path / "b.yaml").write_text("stub\n")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "- id: alpha\n"
        "  input: a.yaml\n"
        "  name: Alpha\n"
        "  blurb: the first target\n"
        "- id: beta\n"
        "  input: b.yaml\n"
        "  name: Beta\n"
        "  blurb: the second target\n"
    )
    return manifest


def test_select_targets_with_no_ids_keeps_everything(tmp_path):
    targets = load_playlist(_two_target_manifest(tmp_path))
    assert [t.id for t in select_targets(targets, None)] == ["alpha", "beta"]
    assert [t.id for t in select_targets(targets, [])] == ["alpha", "beta"]


def test_select_targets_keeps_manifest_order_not_argument_order(tmp_path):
    """The gallery reads top-to-bottom off the file an operator edits;
    `--targets beta,alpha` must not silently reorder the grid."""
    targets = load_playlist(_two_target_manifest(tmp_path))
    assert [t.id for t in select_targets(targets, ["beta", "alpha"])] == \
        ["alpha", "beta"]


def test_select_targets_drops_the_ones_not_asked_for(tmp_path):
    targets = load_playlist(_two_target_manifest(tmp_path))
    assert [t.id for t in select_targets(targets, ["beta"])] == ["beta"]


def test_an_unknown_target_id_is_loud(tmp_path):
    """Not a smaller playlist: a typo that silently shipped a subset is how
    the two processes drift apart in the first place. The message names both
    the bad id and what the manifest actually holds, because the reader is
    an operator at a venue."""
    targets = load_playlist(_two_target_manifest(tmp_path))
    with pytest.raises(PlaylistError) as exc:
        select_targets(targets, ["alpha", "gamma"])
    assert "gamma" in str(exc.value)
    assert "alpha" in str(exc.value)


def test_the_cli_prints_id_and_resolved_input_path(tmp_path, capsys):
    manifest = _two_target_manifest(tmp_path)
    assert playlist_main([str(manifest), "beta"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [f"beta\t{tmp_path / 'b.yaml'}"]


def test_the_cli_fails_with_one_line_and_no_traceback(tmp_path, capsys):
    """scripts/run-demo.sh prints this straight to an operator. A traceback
    would be exactly the raw-error-text this project bans everywhere else."""
    assert playlist_main([str(tmp_path / "nope.yaml")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1
    assert "not found" in captured.err


# ── copy that has to stay true as the playlist grows ────────────────────────
#
# The failure these guard against is not a crash: it is a gallery card
# confidently telling a visitor something that stopped being true when a
# later target shipped. That has now happened once (the DNA duplex called
# itself "the only thing this booth folds that is not a protein", and stayed
# that way through the whole of the tRNA entry landing), which is what these
# are written from.

def _entity_types(target):
    """Which entity kinds a target's own input file actually contains.

    Read out of the YAML rather than out of the blurb, which is the point:
    it is the input that decides what the chips fold.
    """
    import yaml

    spec = yaml.safe_load(target.input_path.read_text())
    kinds = set()
    for entry in spec.get("sequences", []):
        kinds.update(entry.keys())
    return kinds


def test_no_target_claims_to_be_the_only_one_of_its_kind_once_it_is_not():
    """Mutation: restoring the DNA tagline's "the only thing this booth
    folds that is not a protein" while the tRNA entry ships. Red.

    Deliberately keyed on "the only", not on the old sentence: the next
    version of this mistake will be worded differently, and a test that only
    knows the exact sentence it was written for cannot catch it.
    """
    targets = load_playlist("playlist/manifest.yaml")
    non_protein = [t for t in targets
                   if not _entity_types(t) & {"protein"}]
    if len(non_protein) < 2:
        pytest.skip("only one non-protein target ships; the claim is true")
    for t in non_protein:
        for field in ("tagline", "blurb"):
            text = (getattr(t, field) or "").lower()
            assert "the only thing" not in text, (
                f"{t.id}'s {field} calls itself the only one of its kind, "
                f"but {len(non_protein)} non-protein targets ship: "
                f"{', '.join(sorted(x.id for x in non_protein))}")


def test_the_playlist_folds_more_than_one_kind_of_molecule():
    """The booth's whole pitch is that this model does nucleic acids and
    ligands, not proteins alone (docs/index.html says so). A playlist that
    quietly became all-protein would make that page wrong.

    Mutation: dropping the DNA and tRNA entries. Red.
    """
    kinds = set()
    for t in load_playlist("playlist/manifest.yaml"):
        kinds |= _entity_types(t)
    assert {"protein", "dna", "rna"} <= kinds, (
        f"the shipped playlist folds only {sorted(kinds)}")


def test_every_shipped_target_has_a_measured_time():
    """`expected_s` is optional by design -- a target added before anyone has
    folded it must load, and the gallery says "not yet timed" rather than
    inventing a number (see the tests above). But nothing SHIPPED should be
    in that state: the number is measured on this booth's own hardware and
    the manifest records how.

    Mutation: shipping a target with no expected_s. Red -- which is the
    reminder to go and fold it.
    """
    for t in load_playlist("playlist/manifest.yaml"):
        assert t.expected_s is not None, (
            f"{t.id} ships with no measured fold time; fold it on the card "
            f"and record the warm number (see the manifest's own comments)")
        assert 0.0 < t.expected_s < 120.0, f"{t.id}: {t.expected_s}s"
