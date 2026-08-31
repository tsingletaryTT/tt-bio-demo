import os

import pytest

from runner.preflight import not_ready_event, run_preflight


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _ready(tmp_path):
    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "protenix-v2.pt").write_bytes(b"x")
    # The EXTRACTED molecule library, which is what a fold loads. A healthy
    # cache has this directory; the mols.tar it came from may or may not still
    # be there (tt-bio discards it, and `tt-bio weights --prune` removes it).
    (weights / "mols").mkdir()
    (weights / "mols" / "ALA.pkl").write_bytes(b"x")
    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "trpcage.yaml").write_text("version: 1\n")
    return weights, playlist


def test_a_complete_installation_passes(tmp_path):
    weights, playlist = _ready(tmp_path)
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert result.ok
    assert result.missing == []


def test_missing_weights_are_reported_specifically(tmp_path):
    weights, playlist = _ready(tmp_path)
    (weights / "protenix-v2.pt").unlink()
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert not result.ok
    assert any("protenix-v2.pt" in m for m in result.missing)


def test_an_empty_playlist_is_reported(tmp_path):
    weights, playlist = _ready(tmp_path)
    (playlist / "trpcage.yaml").unlink()
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert not result.ok
    assert any("playlist" in m.lower() for m in result.missing)


def test_no_cards_is_reported(tmp_path):
    weights, playlist = _ready(tmp_path)
    result = run_preflight(weights, playlist, check_tap=False, card_count=0)
    assert not result.ok
    assert any("card" in m.lower() for m in result.missing)


def test_every_problem_is_reported_at_once_not_just_the_first(tmp_path):
    weights, playlist = _ready(tmp_path)
    (weights / "protenix-v2.pt").unlink()
    (playlist / "trpcage.yaml").unlink()
    result = run_preflight(weights, playlist, check_tap=False, card_count=0)
    assert len(result.missing) >= 3, "an operator should see the whole list in one run"


def test_a_broken_trajectory_tap_is_a_preflight_failure(tmp_path, monkeypatch):
    # If the tap cannot work, folds still succeed but nothing condenses on
    # screen. That must be caught before the demo opens, not discovered at it.
    from runner import preflight as mod

    def broken():
        from runner.dump_tap import TapUnavailable
        raise TapUnavailable("edm_sample moved")

    monkeypatch.setattr(mod, "check_tap_supported", broken)
    weights, playlist = _ready(tmp_path)
    result = run_preflight(weights, playlist, check_tap=True, card_count=4)
    assert not result.ok
    assert any("trajectory" in m.lower() or "edm_sample" in m for m in result.missing)


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permission bits")
def test_an_unreadable_weights_directory_is_reported_not_raised(tmp_path):
    # A wrong-ownership copy or a not-yet-mounted NFS share makes the weights
    # directory unreadable. Path.is_file() raises PermissionError in that
    # case (EACCES is not one of the errnos pathlib treats as "missing").
    # That must become a missing-entry, not a crash — and the checks that run
    # after it (playlist, cards) must still get a chance to report too.
    weights, playlist = _ready(tmp_path)
    (playlist / "trpcage.yaml").unlink()
    os.chmod(weights, 0o000)
    try:
        result = run_preflight(weights, playlist, check_tap=False, card_count=0)
    finally:
        os.chmod(weights, 0o755)  # restore so tmp_path cleanup can remove it
    assert not result.ok
    assert any("weight" in m.lower() for m in result.missing)
    assert any("playlist" in m.lower() for m in result.missing)
    assert any("card" in m.lower() for m in result.missing)


def test_a_tap_check_error_other_than_tapunavailable_is_reported_not_raised(tmp_path, monkeypatch):
    # check_tap_supported imports tt_bio.protenix; a partial/broken install
    # can fail with something other than the module's own TapUnavailable
    # (e.g. ImportError). That must still become a missing-entry rather than
    # propagate — and it must not discard problems already found by the
    # checks that ran before it.
    from runner import preflight as mod

    def broken():
        raise ImportError("tt_bio.protenix could not be imported")

    monkeypatch.setattr(mod, "check_tap_supported", broken)
    weights, playlist = _ready(tmp_path)
    (weights / "protenix-v2.pt").unlink()
    result = run_preflight(weights, playlist, check_tap=True, card_count=0)
    assert not result.ok
    assert any("trajectory" in m.lower() for m in result.missing)
    assert any("weight" in m.lower() for m in result.missing)
    assert any("card" in m.lower() for m in result.missing)


def test_default_card_count_samples_tt_smi(tmp_path, monkeypatch):
    # card_count=None is the path Task 9's daemon actually uses; every other
    # test here passes an explicit count. Confirm the default branch really
    # calls sample_tt_smi rather than being skipped.
    from runner import cards as cards_mod

    monkeypatch.setattr(cards_mod, "sample_tt_smi", lambda timeout=5.0: [object()] * 4)
    weights, playlist = _ready(tmp_path)
    result = run_preflight(weights, playlist, check_tap=False)
    assert result.ok
    assert result.missing == []


def test_not_ready_event_carries_the_full_missing_list(tmp_path):
    weights, playlist = _ready(tmp_path)
    (weights / "protenix-v2.pt").unlink()
    result = run_preflight(weights, playlist, check_tap=False, card_count=0)
    event = not_ready_event(result)
    assert event["type"] == "not_ready"
    assert event["missing"] == result.missing


# ---------------------------------------------------------------------------
# The molecule library.
#
# REQUIRED_WEIGHTS was ("protenix-v2.pt",) -- the checkpoint only. But
# protenix-v2 loads the CCD molecule library too (tt-bio's registry lists
# `mols` under that model, and runner/folder.py calls download_mols
# alongside the checkpoint fetch), so a cache holding the checkpoint and no
# molecules passed preflight, printed "preflight: ok", and then died on the
# first fold.
#
# This is the same defect the doctor had from the other direction, and it is
# the one a user actually hit: nothing in any install path made the molecule
# library a checked box.
# ---------------------------------------------------------------------------

def test_the_molecule_library_is_required_not_just_the_checkpoint(tmp_path):
    """A cache with the checkpoint and no molecules must NOT report ok."""
    weights, playlist = _ready(tmp_path)
    _rmtree(weights / "mols")
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert not result.ok
    assert any("mols" in m for m in result.missing), result.missing


def test_the_extracted_directory_satisfies_it_not_the_tar(tmp_path):
    """`mols.tar` is DISCARDED once unpacked -- tt-bio's own status() calls the
    archive being gone "harmless for mols once the library is unpacked", and
    `tt-bio weights --prune` removes it. So the thing to require is the
    extracted directory. Requiring the tar instead would fail a booth that is
    perfectly able to fold, which is exactly the false alarm the doctor's
    1 GB size floor on mols.tar produced."""
    weights, playlist = _ready(tmp_path)
    (weights / "mols.tar").unlink(missing_ok=True)
    assert (weights / "mols").is_dir()
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert result.ok, result.missing


# This is where a WRONG assumption of mine used to live. It asserted that an
# unextracted mols.tar was not readiness, on the reasoning that a fold loads
# the directory. True as far as it goes -- and wrong about who extracts it.
# `Folder.load()` calls download_mols(cache), so the fold unpacks the archive
# itself, and preflight runs ONCE: blocking there turned a state that repairs
# itself in twenty seconds into a booth stuck on "preparing" forever. The
# corrected expectation is
# `test_a_downloaded_but_unextracted_molecule_library_is_not_a_blocker` below.
#
# The DOCTOR still reports this state (test_a_tar_with_no_unpacked_library_is_
# not_ready), and that split is the point: the doctor is a pre-venue question
# where "you have not finished installing" is worth saying, and preflight is a
# start-or-refuse gate where it is not.


def test_what_preflight_requires_is_what_the_pinned_tt_bio_says_it_needs():
    """Derived from tt-bio's registry, not hand-listed, so a release that adds
    a third artifact to protenix-v2 is picked up here instead of surfacing as
    a fold that dies on a machine preflight called ready.

    Hand-listing is what caused the bug this section exists for: `mols` was
    added to the model's requirements upstream and REQUIRED_WEIGHTS never
    moved with it."""
    from tt_bio import weights as tt_weights

    from runner.preflight import required_weights

    want = {a.key for a in tt_weights.artifacts_for("protenix-v2")}
    got = {label for label, _path in required_weights("/nonexistent")}
    assert got == want, f"preflight requires {got}, tt-bio says protenix-v2 needs {want}"


def test_a_missing_weight_reads_as_english_not_as_a_traceback(tmp_path):
    """preflight's strings go on a conference screen -- they are what the UI's
    `not_ready` "preparing" overlay shows a visitor (spec section 6: nothing
    in the UI may ever display a stack trace). So the molecule-library line
    has to be presentable, not a repr of an exception or a registry row."""
    weights, playlist = _ready(tmp_path)
    _rmtree(weights / "mols")
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    line = next(m for m in result.missing if "mols" in m)
    assert "Traceback" not in line and "Error" not in line
    assert "Artifact(" not in line and "object at 0x" not in line
    assert len(line) < 200, f"too long for the preparing screen: {line!r}"


# --- what the review found ---------------------------------------------------

def test_a_relocated_artifact_is_found_where_it_actually_lives(tmp_path, monkeypatch):
    """tt-bio lets an operator point ONE artifact somewhere else --
    $PROTENIX_CKPT / $TT_BIO_PROTENIX_V2 for the checkpoint, $TT_BIO_MOLS for
    the molecule library -- and `weights.resolve()` honours that while
    `Artifact.dest()` does not.

    Building the paths from dest() meant preflight reported the checkpoint
    missing on a host where every fold would have worked, and the daemon then
    served `not_ready` forever: the booth sits on the "preparing" screen
    while nothing is actually wrong. That is the false-alarm class this whole
    change set out to remove, reintroduced one layer down."""
    weights, playlist = _ready(tmp_path)
    elsewhere = tmp_path / "big-disk"
    elsewhere.mkdir()
    moved = elsewhere / "protenix-v2.pt"
    moved.write_bytes(b"x")
    (weights / "protenix-v2.pt").unlink()          # not in the cache any more
    monkeypatch.setenv("TT_BIO_PROTENIX_V2", str(moved))

    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert result.ok, result.missing


def test_a_downloaded_but_unextracted_molecule_library_is_not_a_blocker(tmp_path):
    """The fold extracts it. `Folder.load()` calls download_mols(cache), which
    unpacks mols.tar on the spot, so a cache holding the archive and no
    directory used to start fine and repair itself on the first fold.

    Requiring the extracted directory turned that self-healing state into a
    daemon that never starts -- and preflight runs ONCE, so the booth would
    sit on "preparing" forever rather than spending twenty seconds unpacking.
    An archive that is present is readiness, because the thing that needs it
    knows how to finish the job."""
    weights, playlist = _ready(tmp_path)
    _rmtree(weights / "mols")
    (weights / "mols.tar").write_bytes(b"x" * 1024)
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert result.ok, result.missing


def test_neither_the_archive_nor_the_directory_is_still_a_blocker(tmp_path):
    """The guard on the test above: accepting the archive must not soften the
    case where nothing is there at all."""
    weights, playlist = _ready(tmp_path)
    _rmtree(weights / "mols")
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert not result.ok
    assert any("mols" in m for m in result.missing), result.missing
