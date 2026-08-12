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
"""

import pytest

from ui.playlist import PlaylistError, Target, load_playlist


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
