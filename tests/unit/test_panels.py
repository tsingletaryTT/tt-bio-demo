"""Tests for ui/panels.py: the pure decisions (`card_color`, `stage_rows`,
`contrast_ratio`) and the two widgets built on top of them (`TelemetryPanel`,
`PipelinePanel`).

Constructing either widget needs a live display (both build real
Gtk.Label/Gtk.Box/Gtk.ProgressBar children in __init__, unlike
tests/unit/test_app_handle_event.py's DemoApp() which defers all widget
construction to do_activate) -- this box has one (DISPLAY=:0,
WAYLAND_DISPLAY=wayland-0 per the task's environment notes), so these run
against the real thing rather than a fake.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

import pytest
from gi.repository import Gdk, Gtk

from protocol.events import STAGE_ORDER, within_stage_frac
from ui.panels import (
    MIN_CONTRAST_RATIO,
    PipelinePanel,
    STALE_AFTER_S,
    TelemetryPanel,
    _BACKGROUND_BY_CLASS,
    card_color,
    contrast_ratio,
    relative_luminance,
    stage_rows,
)
from ui.telemetry import CardReading

STAGES = ("msa", "prep", "trunk", "diffusion", "confidence", "saving")


# ---------------------------------------------------------------------------
# The brief's own tests, verbatim.
# ---------------------------------------------------------------------------

def test_a_cool_card_and_a_hot_card_do_not_look_the_same():
    assert card_color(45.0) != card_color(90.0)


def test_the_quarantine_threshold_is_where_the_color_changes():
    assert card_color(84.9) == card_color(45.0)
    assert card_color(85.1) == card_color(90.0)


def test_every_protocol_stage_has_a_row():
    rows = stage_rows("trunk", 0.4)
    assert [name for name, _, _ in rows] == list(STAGES)


def test_stages_before_the_current_one_read_as_complete():
    rows = {name: frac for name, frac, _ in stage_rows("diffusion", 0.5)}
    assert rows["msa"] == 1.0
    assert rows["prep"] == 1.0
    assert rows["trunk"] == 1.0


def test_stages_after_the_current_one_read_as_not_started():
    rows = {name: frac for name, frac, _ in stage_rows("trunk", 0.4)}
    assert rows["diffusion"] == 0.0
    assert rows["confidence"] == 0.0


def test_the_current_stage_shows_its_own_progress():
    rows = {name: frac for name, frac, _ in stage_rows("diffusion", 0.37)}
    assert rows["diffusion"] == pytest.approx(0.37)


def test_an_unknown_stage_does_not_raise():
    """A future protocol stage must not break the panel."""
    rows = stage_rows("something-new", 0.5)
    assert len(rows) == len(STAGES)


# ---------------------------------------------------------------------------
# Additional card_color coverage: STAGE_ORDER really is the protocol's own
# table (not a hardcoded local copy that could silently drift from it -- see
# protocol/events.py's comment on why this moved there), and card_color's
# threshold matches runner/cards.py's CardPool exactly, including at the
# boundary itself.
# ---------------------------------------------------------------------------

def test_stage_rows_uses_the_real_protocol_stage_order():
    assert [name for name, _, _ in stage_rows("msa", 0.0)] == list(STAGE_ORDER)


def test_card_color_at_exactly_the_threshold_is_hot():
    """CardPool.update uses `>=`, not `>` (a card AT max_temp_c is already
    quarantined) -- card_color must agree exactly, not merely bracket it."""
    assert card_color(85.0) == card_color(90.0)
    assert card_color(85.0) != card_color(84.9)


def test_card_color_respects_a_custom_threshold():
    assert card_color(50.0, max_temp_c=40.0) == card_color(90.0, max_temp_c=40.0)
    assert card_color(30.0, max_temp_c=40.0) != card_color(50.0, max_temp_c=40.0)


def test_card_color_of_a_non_finite_temperature_is_hot_not_normal():
    """Fix round 1 minor: a NaN temperature must not read as a healthy
    green/neutral card -- there are only two buckets, and "we don't have a
    real number" must land in the alarming one, never the reassuring one.
    ui/telemetry.py's parse_snapshot is the primary defense (a non-finite
    value is now treated as unparseable at the source), but card_color
    stays defensive on its own too."""
    assert card_color(float("nan")) == card_color(90.0)
    assert card_color(float("nan")) != card_color(45.0)
    assert card_color(float("inf")) == card_color(90.0)
    assert card_color(float("-inf")) == card_color(90.0)


# ---------------------------------------------------------------------------
# The third tuple element ("done"/"active"/"pending"): the brief's signature
# is `list[tuple[str, float, str]]` and never says what the third element
# means. Defined and tested here per the controller ruling: exactly one row
# is "active" during a normal fold, and an unknown stage's rows are all
# "pending" (never a false "done") -- the same reading `PipelinePanel.reset()`
# uses.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_exactly_one_row_is_active_during_a_normal_fold(stage):
    states = [state for _name, _frac, state in stage_rows(stage, 0.5)]
    assert states.count("active") == 1
    assert states[list(STAGE_ORDER).index(stage)] == "active"


def test_an_unknown_stage_marks_every_row_pending_not_done():
    rows = stage_rows("something-new", 0.5)
    assert all(state == "pending" for _name, _frac, state in rows)
    assert all(frac == 0.0 for _name, frac, _state in rows)


def test_done_rows_precede_active_which_precedes_pending():
    """The three states must partition STAGE_ORDER into a contiguous
    done-prefix, one active row, then a pending-suffix -- never an
    out-of-order mix (e.g. a later stage reading done while an earlier one
    is still pending)."""
    rows = stage_rows("diffusion", 0.5)
    states = [state for _name, _frac, state in rows]
    assert states == ["done", "done", "done", "active", "pending", "pending"]


def test_stage_rows_of_a_non_finite_frac_reads_as_empty_not_full():
    """Fix round 1 minor: Python's own `min(1.0, float('nan'))` silently
    keeps 1.0 -- so an unmeasured "how far in" used to render as a FULLY
    filled bar, claiming the active stage was done when nothing of the sort
    is known. A non-finite frac must read as empty (0.0) instead; the row
    stays "active" (this IS still the current stage -- NaN is a
    progress-measurement problem, not a stage-position problem)."""
    rows = {name: frac for name, frac, _ in stage_rows("diffusion", float("nan"))}
    assert rows["diffusion"] == 0.0
    states = {name: state for name, _frac, state in stage_rows("diffusion", float("nan"))}
    assert states["diffusion"] == "active"


# ---------------------------------------------------------------------------
# The within-stage/whole-fold composition test the controller ruling
# demands: this is what makes the mismatch the brief warns about ("a bar
# sits at 15% through the whole of diffusion") impossible rather than
# merely documented. within_stage_frac("diffusion", 0.55) must convert the
# WIRE's whole-fold fraction (diffusion's band is 0.15-0.95) into 0.5, and
# stage_rows must then show diffusion's OWN row at 0.5 -- not the raw wire
# value, and not some other stage's row.
# ---------------------------------------------------------------------------

def test_within_stage_frac_composes_correctly_with_stage_rows():
    converted = within_stage_frac("diffusion", 0.55)
    rows = {name: frac for name, frac, _ in stage_rows("diffusion", converted)}
    assert rows["diffusion"] == pytest.approx(0.5)
    # And nothing else moved: every other stage still reads its own
    # before/after value, regardless of the conversion.
    assert rows["trunk"] == 1.0
    assert rows["confidence"] == 0.0


def test_within_stage_frac_leaking_the_raw_wire_value_would_be_wrong():
    """Negative-control: passing the RAW wire fraction straight into
    stage_rows (skipping within_stage_frac) gives a materially different,
    wrong-looking number for the same wire event -- proving the two are not
    interchangeable and the conversion step is load-bearing."""
    wire_frac = 0.55
    raw_rows = {name: frac for name, frac, _ in stage_rows("diffusion", wire_frac)}
    converted_rows = {
        name: frac for name, frac, _ in
        stage_rows("diffusion", within_stage_frac("diffusion", wire_frac))
    }
    assert raw_rows["diffusion"] != converted_rows["diffusion"]
    assert raw_rows["diffusion"] == pytest.approx(0.55)
    assert converted_rows["diffusion"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# PipelinePanel: thin assembly over stage_rows. Verified via the panel's own
# `last_rows` (the exact structure `set_stage` just handed to the widgets),
# and independently via the ProgressBar/CSS state actually applied to the
# GTK objects -- so a bug that recorded the right `last_rows` but rendered
# the wrong widget state would still be caught.
# ---------------------------------------------------------------------------

def test_pipeline_panel_set_stage_matches_stage_rows():
    panel = PipelinePanel()
    panel.set_stage("diffusion", 0.37)
    assert panel.last_rows == stage_rows("diffusion", 0.37)


def test_pipeline_panel_progress_bars_reflect_the_rows():
    panel = PipelinePanel()
    panel.set_stage("trunk", 0.4)
    for name, frac, state in stage_rows("trunk", 0.4):
        label, bar = panel._rows[name]
        assert bar.get_fraction() == pytest.approx(frac)
        assert label.has_css_class(f"stage-{state}")
        assert bar.has_css_class(f"stage-{state}")
        # No stale state from a class that should have been removed.
        for other_state in ("done", "active", "pending"):
            if other_state != state:
                assert not label.has_css_class(f"stage-{other_state}")


def test_pipeline_panel_reset_clears_progress_and_state():
    panel = PipelinePanel()
    panel.set_stage("diffusion", 0.8)
    panel.reset()
    assert panel.last_rows == [(name, 0.0, "pending") for name in STAGE_ORDER]
    for name in STAGE_ORDER:
        _label, bar = panel._rows[name]
        assert bar.get_fraction() == 0.0


def test_pipeline_panel_starts_reset():
    """A freshly constructed panel reads the same as an explicitly reset
    one -- a visitor must never see a stale/undefined state before the
    first real stage event arrives."""
    panel = PipelinePanel()
    assert panel.last_rows == [(name, 0.0, "pending") for name in STAGE_ORDER]


# ---------------------------------------------------------------------------
# TelemetryPanel: the tri-state must be visually distinct (different CSS
# status classes -> different `last_status`, which mirrors what actually
# landed on the label/cards), never a plausible-looking card of zeros for
# either failure state, and the staleness threshold must be a real,
# testable boundary.
# ---------------------------------------------------------------------------

def _reading(index=0, temperature_c=45.0, board_type="p300c"):
    return CardReading(index=index, board_type=board_type,
                        temperature_c=temperature_c, power_w=18.0, aiclk_mhz=800.0)


def test_none_readings_shows_no_telemetry_never_zeros():
    panel = TelemetryPanel()
    panel.update(None, None)
    assert panel.last_status == "unknown"
    assert panel._cards_box.get_first_child() is None, (
        "None must never render as a card, even a zeroed one")
    assert panel._status_label.has_css_class("telemetry-unknown")


def test_empty_readings_is_distinct_from_none():
    panel = TelemetryPanel()
    panel.update(None, None)
    unknown_status = panel.last_status
    panel.update([], 0.2)
    assert panel.last_status != unknown_status
    assert panel.last_status == "empty"
    assert not panel._status_label.has_css_class("telemetry-unknown")
    assert panel._status_label.has_css_class("telemetry-empty")
    assert panel._cards_box.get_first_child() is None


def test_nonempty_readings_renders_one_card_per_reading():
    panel = TelemetryPanel()
    panel.update([_reading(0, 45.0), _reading(1, 90.0)], 0.1)
    assert panel.last_status == "ok"
    cards = []
    child = panel._cards_box.get_first_child()
    while child is not None:
        cards.append(child)
        child = child.get_next_sibling()
    assert len(cards) == 2
    assert cards[0].has_css_class("telemetry-card-normal")
    assert cards[1].has_css_class("telemetry-card-hot")


def test_the_three_states_are_all_mutually_distinct():
    panel = TelemetryPanel()
    panel.update(None, None)
    unknown = panel.last_status
    panel.update([], 0.1)
    empty = panel.last_status
    panel.update([_reading()], 0.1)
    ok = panel.last_status
    assert len({unknown, empty, ok}) == 3


def test_staleness_boundary_just_below_threshold_is_not_stale():
    panel = TelemetryPanel()
    panel.update([_reading()], STALE_AFTER_S - 0.01)
    assert panel.last_status == "ok"
    assert not panel._status_label.has_css_class("telemetry-stale")


def test_staleness_boundary_at_threshold_is_stale():
    panel = TelemetryPanel()
    panel.update([_reading()], STALE_AFTER_S)
    assert panel.last_status == "stale-ok"
    assert panel._status_label.has_css_class("telemetry-stale")


def test_stale_empty_reading_is_also_flagged():
    """Staleness applies to the `[]` state too: a sampler that established
    "no cards" and then wedged must not keep looking like a fresh reading
    forever."""
    panel = TelemetryPanel()
    panel.update([], STALE_AFTER_S + 1.0)
    assert panel.last_status == "stale-empty"
    assert panel._status_label.has_css_class("telemetry-stale")


def test_none_readings_is_never_flagged_stale():
    """age_s is always None alongside readings=None (ui/telemetry.py:
    latest_at is only ever set on a successful sample) -- there is nothing
    to call stale, only unknown from the very start."""
    panel = TelemetryPanel()
    panel.update(None, None)
    assert not panel._status_label.has_css_class("telemetry-stale")


def test_a_fresh_good_reading_after_a_stale_one_clears_the_stale_flag():
    panel = TelemetryPanel()
    panel.update([_reading()], STALE_AFTER_S + 5.0)
    assert panel._status_label.has_css_class("telemetry-stale")
    panel.update([_reading()], 0.05)
    assert not panel._status_label.has_css_class("telemetry-stale")


# ---------------------------------------------------------------------------
# Important 3 (fix round 1 review): cards must not accumulate. A previous
# implementation was CORRECT (it already called `_clear_cards()`), but
# nothing pinned that behavior -- replacing `_clear_cards()`'s body with
# `pass` left the whole suite green. These two tests exist specifically to
# make that mutation red; see this section's own mutation-verification note
# in the task report.
# ---------------------------------------------------------------------------

def _count_children(box):
    count = 0
    child = box.get_first_child()
    while child is not None:
        count += 1
        child = child.get_next_sibling()
    return count


def test_two_consecutive_nonempty_updates_yield_exactly_n_cards_not_more():
    panel = TelemetryPanel()
    panel.update([_reading(0), _reading(1)], 0.1)
    assert _count_children(panel._cards_box) == 2
    panel.update([_reading(0), _reading(1), _reading(2)], 0.1)
    assert _count_children(panel._cards_box) == 3, (
        "a second update must REPLACE the card set, not add to it")


def test_nonempty_to_empty_transition_leaves_zero_cards():
    """A `[readings] -> []` transition must not leave the old cards sitting
    on screen underneath the "no cards detected" banner -- that would look
    exactly like a real reading, contradicting the banner right above it."""
    panel = TelemetryPanel()
    panel.update([_reading(0), _reading(1)], 0.1)
    assert _count_children(panel._cards_box) == 2
    panel.update([], 0.1)
    assert panel._cards_box.get_first_child() is None
    assert _count_children(panel._cards_box) == 0


# ---------------------------------------------------------------------------
# Important 5 (fix round 1 review): PipelinePanel.set_stage_from_wire is the
# structurally-safe entry point for the daemon-driven wiring layer -- it
# does the within_stage_frac conversion internally, so the raw-wire-fraction
# path is never reachable from that caller at all.
# ---------------------------------------------------------------------------

def test_set_stage_from_wire_matches_manual_conversion_plus_set_stage():
    panel_a = PipelinePanel()
    panel_a.set_stage_from_wire("diffusion", 0.55)

    panel_b = PipelinePanel()
    panel_b.set_stage("diffusion", within_stage_frac("diffusion", 0.55))

    assert panel_a.last_rows == panel_b.last_rows
    diffusion_frac = {name: frac for name, frac, _ in panel_a.last_rows}["diffusion"]
    assert diffusion_frac == pytest.approx(0.5)


def test_set_stage_from_wire_never_leaks_the_raw_wire_value():
    """Negative control, mirroring test_within_stage_frac_leaking_the_raw_
    wire_value_would_be_wrong above but through the widget entry point a
    real caller would actually use: calling set_stage_from_wire with the
    wire's raw 0.55 must NOT produce the same row set as calling set_stage
    directly with 0.55 -- if it did, the conversion would not actually be
    happening internally."""
    panel = PipelinePanel()
    panel.set_stage_from_wire("diffusion", 0.55)
    via_wire = panel.last_rows

    panel.set_stage("diffusion", 0.55)  # the WRONG thing a caller could do
    via_raw = panel.last_rows

    assert via_wire != via_raw


def test_within_stage_frac_warns_when_the_wire_value_is_outside_its_band(caplog):
    """Important 5: within_stage_frac logs a warning when a stage event's
    wire fraction falls outside that stage's own band -- this should never
    happen from a correctly-behaving daemon, so it must be loud when it
    does, not silently clamped with nothing to diagnose it by."""
    import logging

    with caplog.at_level(logging.WARNING, logger="protocol.events"):
        within_stage_frac("trunk", 0.5)  # trunk's band is (0.10, 0.15)
    assert any("trunk" in r.message and "outside" in r.message for r in caplog.records)


def test_within_stage_frac_does_not_warn_for_a_value_inside_the_band(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="protocol.events"):
        within_stage_frac("trunk", 0.12)  # inside (0.10, 0.15)
    assert not any("outside" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Important 2 (fix round 1 review): a small, tested, pure WCAG contrast
# helper -- not a magic number buried in the legibility walker below.
# ---------------------------------------------------------------------------

def test_contrast_ratio_of_black_on_white_is_the_wcag_maximum():
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0, abs=0.01)


def test_contrast_ratio_of_identical_colors_is_one():
    assert contrast_ratio("#1B8EB1", "#1B8EB1") == pytest.approx(1.0, abs=1e-9)


def test_contrast_ratio_is_symmetric_in_its_arguments():
    a, b = "#092221", "#F1F8F8"
    assert contrast_ratio(a, b) == pytest.approx(contrast_ratio(b, a))


def test_contrast_ratio_matches_an_independently_computed_reference_value():
    # Cross-checked against a second, independently written implementation
    # of the same WCAG formula (see this task's report) -- not just "the
    # function agrees with itself."
    assert contrast_ratio("#1B8EB1", "#092221") == pytest.approx(4.397, abs=0.01)
    assert contrast_ratio("#F1F8F8", "#092221") == pytest.approx(15.465, abs=0.01)
    assert contrast_ratio("#FA512E", "#092221") == pytest.approx(4.976, abs=0.01)


def test_relative_luminance_of_black_is_zero_and_white_is_one():
    assert relative_luminance("#000000") == pytest.approx(0.0, abs=1e-9)
    assert relative_luminance("#FFFFFF") == pytest.approx(1.0, abs=1e-6)


def test_min_contrast_ratio_is_the_wcag_aa_normal_text_floor():
    assert MIN_CONTRAST_RATIO == 4.5


# ---------------------------------------------------------------------------
# Critical 1 / Important 4 (fix round 1 review): a GENERALIZED legibility
# guard, not a one-off assertion about the specific labels the review named.
# Walks every real Gtk.Label descendant of a constructed, real (not faked)
# panel, reads its ACTUALLY-RESOLVED foreground colour via the real GTK CSS
# engine (`Gtk.Widget.get_color()` -- not a re-implementation of CSS
# cascade rules), and checks it against the nearest ancestor carrying an
# explicitly-set background (from `ui.panels._BACKGROUND_BY_CLASS`, the
# SAME dict the panel's own stylesheet is generated from -- not a second,
# independently-maintained copy of that knowledge that could silently
# drift from the real CSS).
#
# This is deliberately NOT a skip-on-no-display test: per this project's
# own rule against a silently-empty/no-op test half being reported as a
# pass, a legibility check that quietly opts out on the one machine where
# legibility matters would be worse than not having it at all -- so a
# missing display is a hard pytest.fail, never a skip.
# ---------------------------------------------------------------------------

def _iter_labels(widget):
    if isinstance(widget, Gtk.Label):
        yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _iter_labels(child)
        child = child.get_next_sibling()


def _rgba_to_hex(rgba):
    def channel(c):
        return round(max(0.0, min(1.0, c)) * 255)
    return f"#{channel(rgba.red):02X}{channel(rgba.green):02X}{channel(rgba.blue):02X}"


def _nearest_explicit_background_hex(widget):
    node = widget
    while node is not None:
        for css_class, bg_hex in _BACKGROUND_BY_CLASS.items():
            if node.has_css_class(css_class):
                return bg_hex
        node = node.get_parent()
    raise AssertionError(
        f"no ancestor of {widget!r} carries a class in "
        "ui.panels._BACKGROUND_BY_CLASS -- a new background tier was added "
        "without teaching the legibility walker about it")


def _assert_every_label_is_legible(root, *, context):
    if Gdk.Display.get_default() is None:
        # Loud, not a skip -- see this section's own docstring above.
        pytest.fail(
            f"[{context}] no default display: cannot resolve real GTK "
            "colors, and a legibility test that silently no-ops on a "
            "headless run would be worse than not having one")
    failures = []
    for label in _iter_labels(root):
        fg_hex = _rgba_to_hex(label.get_color())
        bg_hex = _nearest_explicit_background_hex(label)
        ratio = contrast_ratio(fg_hex, bg_hex)
        if ratio < MIN_CONTRAST_RATIO:
            failures.append(
                f"[{context}] label {label.get_label()!r}: fg={fg_hex} "
                f"bg={bg_hex} ratio={ratio:.2f} < {MIN_CONTRAST_RATIO}")
    assert not failures, "\n".join(failures)


def test_telemetry_panel_labels_are_legible_in_every_state():
    panel = TelemetryPanel()
    panel.update(None, None)
    _assert_every_label_is_legible(panel, context="unknown")
    panel.update([], 0.1)
    _assert_every_label_is_legible(panel, context="empty")
    panel.update([_reading(0, 45.0), _reading(1, 90.0)], 0.1)
    _assert_every_label_is_legible(panel, context="ok (normal + hot card)")
    panel.update([_reading()], STALE_AFTER_S + 5.0)
    _assert_every_label_is_legible(panel, context="stale")


def test_pipeline_panel_labels_are_legible_at_every_stage():
    panel = PipelinePanel()
    for stage in STAGE_ORDER:
        panel.set_stage(stage, 0.5)
        _assert_every_label_is_legible(panel, context=stage)
    panel.reset()
    _assert_every_label_is_legible(panel, context="reset")
