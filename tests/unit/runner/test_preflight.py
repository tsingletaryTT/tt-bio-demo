import os

import pytest

from runner.preflight import not_ready_event, run_preflight


def _ready(tmp_path):
    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "protenix-v2.pt").write_bytes(b"x")
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
