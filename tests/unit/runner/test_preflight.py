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


def test_not_ready_event_carries_the_full_missing_list(tmp_path):
    weights, playlist = _ready(tmp_path)
    (weights / "protenix-v2.pt").unlink()
    result = run_preflight(weights, playlist, check_tap=False, card_count=0)
    event = not_ready_event(result)
    assert event["type"] == "not_ready"
    assert event["missing"] == result.missing
