"""Task 16: four chips, honestly -- the Tensix panel, per chip.

The panel was deliberately walked back once (whole-branch review, Critical
3): it fanned one fold's mode out to all four canvases while the daemon
folded on card 0 alone, so the booth claimed four chips were working when one
was. The fix made it say LESS -- one chip named in the header, three canvases
drawn idle -- and it was truthful precisely because it said less.

Four chips now genuinely fold at once (Tasks 8-15; a live quad run measured
65.4-73.7 degC at 1337-1350 MHz drawing 72-91 W across all four, against
12-17 W idle). This file is what earns the fuller claim back, and what stops
it being earned back further than it is true:

- each canvas animates ITS OWN chip's stage, so two chips in different stages
  animate differently;
- the header COUNTS what is actually running rather than asserting four;
- one chip working still NAMES that chip -- the Critical-3 fix has to survive
  the change that supersedes it;
- nothing running claims nothing;
- a daemon that goes quiet still stands each canvas down, independently
  (`tick_staleness`);
- wire-shaped junk costs the attribution, never an exception on the event
  path.

The pure half of the panel (`viz_mode`, `flow_params`, the page, the frame
governor) is unchanged and stays tested in tests/unit/test_chipviz.py; this
file is only about the per-chip attribution that replaced
`set_folding_chip`.
"""

import pytest

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402  (gi.require_version must come first)

from ui.chipviz import (  # noqa: E402
    STAGE_STALE_AFTER_S,
    ChipVizPanel,
    flow_params,
    viz_mode,
)


def _panel(monkeypatch, chips=4):
    """A panel that believes it has `chips` chips, and is available.

    `available` is forced rather than depended on: this box has WebKit, but a
    build container may not, and every assertion below is about the panel's
    per-chip POLICY -- which is decided in Python and is exactly as real with
    no WebView behind it. `_eval` is a no-op while `_webview` is None, so the
    policy runs and the JS simply goes nowhere.
    """
    monkeypatch.setattr("ui.chipviz.chip_count", lambda: chips)
    panel = ChipVizPanel()
    panel.available = True
    return panel


def _clocked_panel(monkeypatch, now, chips=4):
    """`_panel`, with a hand-driven clock -- staleness by arithmetic rather
    than by sleeping through fifteen real seconds. `now` is a one-element
    list the test advances."""
    monkeypatch.setattr("ui.chipviz.chip_count", lambda: chips)
    panel = ChipVizPanel(clock=lambda: now[0])
    panel.available = True
    return panel


# ---------------------------------------------------------------------------
# Each canvas animates its own chip.
# ---------------------------------------------------------------------------

def test_two_chips_folding_animate_and_two_do_not(monkeypatch):
    panel = _panel(monkeypatch)
    panel.set_chip_stages({0: "diffusion", 2: "trunk", 1: None, 3: None})
    modes = [panel._mode_for_chip(i) for i in range(4)]
    assert modes[0] != "idle" and modes[2] != "idle"
    assert modes[1] == "idle" and modes[3] == "idle"


def test_each_chip_animates_its_own_stage(monkeypatch):
    """A shared mode across four chips is the same untruth as before, just
    four times over."""
    panel = _panel(monkeypatch)
    panel.set_chip_stages({0: "diffusion", 1: "trunk"})
    assert panel._mode_for_chip(0) == viz_mode("folding", "diffusion")
    assert panel._mode_for_chip(1) == viz_mode("folding", "trunk")
    assert panel._mode_for_chip(0) != panel._mode_for_chip(1)


def test_each_chip_gets_its_own_activation_on_the_page(monkeypatch):
    """The policy above only reaches the glass through `activateChip`. This
    pins that the two chips' DIFFERENT modes are what is sent -- not one mode
    sent four times, which is what the old fan-out did.

    Mutation this catches: `_push_modes` sending `_mode_for_chip(0)` to every
    canvas.
    """
    panel = _panel(monkeypatch)
    calls = []
    monkeypatch.setattr(panel, "_eval", calls.append)
    panel.set_chip_stages({0: "diffusion", 1: "trunk"})
    joined = " ".join(calls)
    assert 'activateChip(0,"diffusion")' in joined
    assert 'activateChip(1,"thinking")' in joined
    assert 'activateChip(2,"diffusion")' not in joined
    assert 'activateChip(3,"diffusion")' not in joined


def test_a_chip_that_stops_being_named_stands_down(monkeypatch):
    """The mapping is the WHOLE picture, not a patch on the last one. A fold
    that ends on chip 0 leaves it out of the next mapping, and a panel that
    merged instead of replacing would animate a finished fold for the rest of
    the day.

    Mutation this catches: `self._chip_stages.update(mapping)`.
    """
    panel = _panel(monkeypatch)
    panel.set_chip_stages({0: "diffusion", 1: "trunk"})
    calls = []
    monkeypatch.setattr(panel, "_eval", calls.append)
    panel.set_chip_stages({1: "trunk"})
    assert panel._mode_for_chip(0) == "idle"
    assert panel._mode_for_chip(1) == "thinking"
    assert 'activateChip(0,"idle")' in " ".join(calls)


# ---------------------------------------------------------------------------
# The header says what is true of the SET.
# ---------------------------------------------------------------------------

def test_all_four_folding_says_so(monkeypatch):
    panel = _panel(monkeypatch)
    panel.set_chip_stages({c: "diffusion" for c in range(4)})
    assert "4 CHIPS" in panel._title_text().upper()


def test_three_chips_folding_says_three_not_four(monkeypatch):
    """The count is counted, not asserted. Four chips are present and three
    are working, which is the ordinary state a few seconds into any quad
    cycle -- a header taking its number from the chip COUNT would claim the
    fourth.

    Mutation this catches: `f"{self._chip_shown} CHIPS FOLDING"`.
    """
    panel = _panel(monkeypatch)
    panel.set_chip_stages({0: "diffusion", 1: "trunk", 2: "confidence",
                           3: None})
    title = panel._title_text().upper()
    assert "3 CHIPS" in title
    assert "4 CHIPS" not in title


def test_a_host_side_stage_is_not_counted_as_a_chip_folding(monkeypatch):
    """`msa`, `prep` and `saving` are host-side work: the chip genuinely is
    not folding (ui/chipviz.py's `_MODE_BY_STAGE`). Counting them would put
    the panel straight back to claiming four.

    Mutation this catches: counting the entries in the mapping instead of the
    canvases whose MODE is not idle.
    """
    panel = _panel(monkeypatch)
    panel.set_chip_stages({0: "diffusion", 1: "msa", 2: "prep", 3: "saving"})
    title = panel._title_text().upper()
    assert "CHIP 0" in title
    assert "CHIPS FOLDING" not in title


def test_one_chip_folding_still_names_that_chip(monkeypatch):
    """The Critical-3 fix must survive: with one chip working, the header
    says which."""
    panel = _panel(monkeypatch)
    panel.set_chip_stages({2: "diffusion"})
    title = panel._title_text().upper()
    assert "CHIP 2" in title
    assert "4 CHIPS" not in title


def test_the_named_chips_own_stage_is_the_word_in_the_header(monkeypatch):
    """"CHIP 2 · REFINING" has to be chip 2's own stage. With one chip
    working and three idle it would be easy to caption the header from a
    booth-wide mode that no longer exists.

    Mutation this catches: captioning the header from the first non-idle
    entry in the mapping rather than from the chip actually named.
    """
    panel = _panel(monkeypatch)
    panel.set_chip_stages({2: "trunk"})
    assert "REFINING" in panel._title_text().upper()


def test_no_chip_folding_claims_nothing(monkeypatch):
    panel = _panel(monkeypatch)
    panel.set_chip_stages({})
    assert "IDLE" in panel._title_text().upper()
    assert all(panel._mode_for_chip(i) == "idle" for i in range(4))


def test_preparing_stands_every_chip_down(monkeypatch):
    """The one booth state that overrides the stage (ui/chipviz.py's
    `viz_mode`): the daemon has said `not_ready`, so nothing is folding on
    any chip whatever the last stage said.

    Mutation this catches: `_mode_for_chip` reading the stage table directly
    instead of going through `viz_mode`.
    """
    panel = _panel(monkeypatch)
    panel.set_chip_stages({c: "diffusion" for c in range(4)})
    panel.set_state("preparing")
    assert all(panel._mode_for_chip(i) == "idle" for i in range(4))
    assert "IDLE" in panel._title_text().upper()


# ---------------------------------------------------------------------------
# Staleness, per chip.
# ---------------------------------------------------------------------------

def test_a_stale_stage_stops_claiming_work(monkeypatch):
    """Unchanged rule: a dead daemon must not leave 'denoising' animating in
    front of a visitor. Now it must stop for each chip independently."""
    now = [0.0]
    panel = _clocked_panel(monkeypatch, now)
    panel.set_chip_stages({0: "diffusion"})
    now[0] = 1000.0
    panel.tick_staleness()
    assert panel._mode_for_chip(0) == "idle"


def test_a_stale_stage_stops_claiming_work_in_the_header_too(monkeypatch):
    """The animation and the words are two claims, and standing only the
    animation down would leave "CHIP 0 · DENOISING" over an idle grid."""
    now = [0.0]
    panel = _clocked_panel(monkeypatch, now)
    panel.set_chip_stages({0: "diffusion"})
    now[0] = 1000.0
    panel.tick_staleness()
    assert "IDLE" in panel._title_text().upper()


def test_a_live_fold_is_not_called_stale(monkeypatch):
    """The other half of the same rule: a fold is ~4.4s warm
    (docs/followups.md's 30-fold soak) and holds `diffusion` for most of it,
    so an ordinary fold must never trip the fallback.

    The 5.0s here is a REAL DURATION, deliberately not written as
    `STAGE_STALE_AFTER_S - 1.0`: a test measured against the constant it is
    checking moves with it and cannot notice the constant shrinking under a
    real fold, which is the only way this ever fires wrongly.

    Mutation this catches: `STAGE_STALE_AFTER_S = 1.0`.
    """
    now = [0.0]
    panel = _clocked_panel(monkeypatch, now)
    panel.set_chip_stages({0: "diffusion"})
    now[0] = 5.0
    panel.tick_staleness()
    assert panel._mode_for_chip(0) == "diffusion"
    assert STAGE_STALE_AFTER_S > 5.0, (
        "the window has to outlast a whole fold, or a healthy booth stands "
        "its own chips down mid-diffusion")


def test_one_chip_going_stale_leaves_the_others_animating(monkeypatch):
    """"Independently" is the load-bearing word, and this is the case it is
    load-bearing FOR: ui/app.py re-asserts every cell's cached stage on every
    event, so a daemon that keeps folding on three chips while one wedges
    would otherwise keep the wedged chip's canvas alive forever on the back
    of its neighbours' events.

    Mutation this catches: one panel-wide stamp refreshed by any chip's
    stage, or a stamp refreshed on every re-assertion rather than on a
    genuine change.
    """
    now = [0.0]
    panel = _clocked_panel(monkeypatch, now)
    panel.set_chip_stages({0: "diffusion", 1: "trunk"})
    now[0] = STAGE_STALE_AFTER_S + 1.0
    # Chip 1's fold has moved on; chip 0 has said nothing new since t=0.
    panel.set_chip_stages({0: "diffusion", 1: "confidence"})
    panel.tick_staleness()
    assert panel._mode_for_chip(0) == "idle"
    assert panel._mode_for_chip(1) == "inference"


def test_a_chip_that_starts_a_new_fold_is_fresh_again(monkeypatch):
    """The flip side: a chip that went stale and is then genuinely told
    something new animates again. A panel that only ever expired would go
    permanently dark on the first hiccup of a conference day."""
    now = [0.0]
    panel = _clocked_panel(monkeypatch, now)
    panel.set_chip_stages({0: "diffusion"})
    now[0] = 1000.0
    panel.tick_staleness()
    panel.set_chip_stages({0: "trunk"})
    assert panel._mode_for_chip(0) == "thinking"


# ---------------------------------------------------------------------------
# Wire-shaped input, and the panel that is not there at all.
# ---------------------------------------------------------------------------

def test_a_card_index_outside_the_drawn_canvases_claims_no_canvas(monkeypatch):
    panel = _panel(monkeypatch, chips=2)
    panel.set_chip_stages({7: "diffusion"})
    assert all(panel._mode_for_chip(i) == "idle" for i in range(2))


def test_a_card_index_outside_the_drawn_canvases_is_not_in_the_header(monkeypatch):
    """The other half of not clamping: an unclaimed canvas must not become an
    unclaimed HEADER either. "CHIP 7" over two canvases, neither of them
    animating, is worse than saying nothing."""
    panel = _panel(monkeypatch, chips=2)
    panel.set_chip_stages({7: "diffusion"})
    title = panel._title_text().upper()
    assert "IDLE" in title
    assert "CHIP 7" not in title


def test_wire_shaped_junk_costs_the_attribution_not_an_exception(monkeypatch):
    panel = _panel(monkeypatch)
    panel.set_chip_stages({"two": "diffusion", 1: 17})
    panel._title_text()


def test_an_unhashable_stage_still_says_something_is_running(monkeypatch):
    """A stage this build has never heard of is never `idle` -- that would
    claim knowledge the booth does not have. An UNHASHABLE one would raise
    straight out of a dict lookup on the event path, which is worse than
    either.

    Mutation this catches: `_MODE_BY_STAGE[stage]` reached without a guard.
    """
    panel = _panel(monkeypatch)
    panel.set_chip_stages({0: ["diffusion"]})
    assert panel._mode_for_chip(0) != "idle"
    panel._title_text()


def test_a_mapping_that_is_not_a_mapping_at_all_is_survived(monkeypatch):
    """`set_chip_stages` is reached from the event path, and every other
    entry point on that path treats its input as wire-shaped."""
    panel = _panel(monkeypatch)
    panel.set_chip_stages(None)                 # must not raise
    panel.set_chip_stages([(0, "diffusion")])   # nor this
    panel._title_text()


def test_an_unavailable_panel_ignores_everything(monkeypatch):
    panel = _panel(monkeypatch)
    panel.available = False
    panel.set_chip_stages({0: "diffusion"})     # must not raise
    panel.set_state("folding")
    panel.tick_staleness()


# ---------------------------------------------------------------------------
# The flow floor, per chip -- the other half of "looks like it is working".
# ---------------------------------------------------------------------------

def test_the_flow_floor_follows_each_chips_own_stage(tmp_path, monkeypatch):
    """`flow_params`' active floor exists so a working chip never looks
    switched off. It has to follow the same per-chip answer the animation
    mode does, or a resting chip is given the busy chip's flow at the same
    boosted clock and looks busy by another route.

    Mutation this catches: `active=True` for every canvas once any chip is
    folding.
    """
    root = tmp_path / "tenstorrent"
    root.mkdir()
    for index in range(4):
        chip = root / f"tenstorrent!{index}"
        chip.mkdir()
        (chip / "tt_aiclk").write_text("1350\n")
    monkeypatch.setattr("ui.chipviz.SYSFS_ROOT", root)
    panel = ChipVizPanel()
    if not panel.available:
        pytest.skip("WebKit unavailable in this environment")
    panel.set_chip_stages({0: "diffusion"})
    calls = []
    monkeypatch.setattr(panel, "_eval", calls.append)
    panel._tick()
    stats = [call for call in calls if "setChipStats" in call]
    working = [c for c in stats
               if c.startswith("window.__viz&&window.__viz.setChipStats(0,")]
    resting = [c for c in stats
               if c.startswith("window.__viz&&window.__viz.setChipStats(1,")]
    active_dram = flow_params(1.0, True)[0]
    idle_dram = flow_params(1.0, False)[0]
    assert working and f"dram_bw:{active_dram:.3f}" in working[-1]
    assert resting and f"dram_bw:{idle_dram:.3f}" in resting[-1]
    assert active_dram != idle_dram, "this test proves nothing if they match"
