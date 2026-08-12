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

import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

import pytest
from gi.repository import Gdk, Gtk

import ui.panels as ui_panels
from protocol.events import STAGE_ORDER, within_stage_frac
from ui.panels import (
    MIN_CONTRAST_RATIO,
    PipelinePanel,
    STALE_AFTER_S,
    TelemetryPanel,
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


_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_CSS_CLASS_TOKEN_RE = re.compile(r"\.([A-Za-z0-9_-]+)")
_CSS_BARE_CLASS_RE = re.compile(r"^\.[A-Za-z0-9_-]+$")
# Negative lookbehind on "-" excludes `background-color:`/`border-color:` --
# only a bare `color:` property counts here.
_CSS_EXPLICIT_COLOR_PROP_RE = re.compile(r"(?<!-)color\s*:")
_CSS_BACKGROUND_PROP_RE = re.compile(r"background(-color)?\s*:")


def _strip_css_comments(css_text):
    return _CSS_COMMENT_RE.sub("", css_text)


def _color_rules_from_css(css_text):
    """Every CSS rule in the REAL, currently-installed stylesheet
    (`ui.panels._PANEL_CSS`, read fresh each call -- not a cached copy --
    so a test that monkeypatches it, like the ones below, is actually
    honored) that sets a plain `color:` (never `background-color:`)
    property, as a list of frozensets of the class(es) a compound selector
    REQUIRES ALL of (e.g. `.telemetry-hero-number.telemetry-hero-hot`
    requires both, not either alone -- a naive "any token in this
    selector" reading would wrongly keep crediting a class after its OWN
    base rule's `color:` was deleted, as long as some unrelated compound
    rule happened to still mention that class name).

    This is what makes `_label_has_an_explicit_color_rule` below a STATIC,
    theme-independent check: it is pure text analysis of this module's own
    stylesheet source, never a runtime GTK CSS cascade resolved against
    whatever desktop theme happens to be loaded (see this task's report,
    "Fix round 2", for why a resolved-color check alone cannot catch this
    defect class on every machine).
    """
    css_text = _strip_css_comments(css_text)
    rules = []
    for selector_part, props in _CSS_RULE_RE.findall(css_text):
        if not _CSS_EXPLICIT_COLOR_PROP_RE.search(props):
            continue
        for compound in selector_part.split(","):
            required = frozenset(_CSS_CLASS_TOKEN_RE.findall(compound))
            if required:
                rules.append(required)
    return rules


def _label_has_an_explicit_color_rule(label, color_rules):
    """True if `label`'s currently-applied CSS classes are a superset of
    at least one real color-setting rule's required class set -- i.e. some
    rule in the actual stylesheet text genuinely applies a `color:` to
    exactly the classes this widget carries, so its foreground cannot be
    silently inherited from an ambient theme regardless of what that theme
    resolves to on any given machine."""
    classes = set(label.get_css_classes())
    return any(required <= classes for required in color_rules)


def _background_affecting_classes_from_css(css_text):
    """Every class named by a BARE single-class selector (`.foo { ... }`,
    no compound, no descendant combinator) whose rule sets a background,
    parsed from the real, currently-installed stylesheet text -- read
    fresh each call, not cached, so a test that monkeypatches
    `ui.panels._PANEL_CSS` (see the tests below) is honored.

    Deliberately restricted to BARE class selectors: this module's
    ProgressBar fill rules (e.g. `.stage-done.pipeline-progress trough
    progress { background-color: ...; }`) are compound/descendant
    selectors targeting a decorative nested paint node, not a container a
    Gtk.Label could sit inside -- and those rules' classes (`stage-done`,
    `pipeline-progress`, ...) are also applied directly to LABELS
    themselves (see PipelinePanel.set_stage), so treating every token in
    those selectors as "background-affecting" would make a label's OWN
    class look like a background it sits ON, an immediate false failure
    on every real pipeline row. A bare `.foo { background-color: ...; }`
    rule, by contrast, really does mean "an element with exactly this
    class has this background" -- which is exactly what a container that
    a label sits inside looks like in this stylesheet today
    (`.telemetry-panel`, `.pipeline-panel`), and exactly what a new nested
    background tier (the reviewer's scenario) would look like too.
    """
    css_text = _strip_css_comments(css_text)
    classes = set()
    for selector_part, props in _CSS_RULE_RE.findall(css_text):
        if not _CSS_BACKGROUND_PROP_RE.search(props):
            continue
        for piece in selector_part.split(","):
            piece = piece.strip()
            if _CSS_BARE_CLASS_RE.match(piece):
                classes.add(piece[1:])
    return classes


def _nearest_explicit_background_hex(widget):
    """Walk from `widget` up through its ancestors and return the hex of
    the FIRST (nearest) one carrying any class this module's real
    stylesheet actually paints a background with.

    This is the reviewer's fix for problem (b): the previous version
    climbed the ancestor chain looking only for classes already present in
    `_BACKGROUND_BY_CLASS`, so a nearer ancestor with a genuine background
    class that dict had never been taught about was silently skipped in
    favor of a REGISTERED ancestor further out -- validating a label at
    its true 1.08:1 contrast against a certified-but-wrong 16.6:1. Here,
    "does this ancestor have a background" is answered from the real CSS
    text itself (`_background_affecting_classes_from_css`, not a second,
    hand-maintained copy of that knowledge), so the walk stops at the
    TRUE nearest background-painted ancestor every time, registered or
    not. If it is not registered, that is a real gap in this test file's
    own bookkeeping and must fail loudly right here -- never silently
    fall through to a different, more distant ancestor's background.
    """
    background_classes = _background_affecting_classes_from_css(ui_panels._PANEL_CSS)
    node = widget
    while node is not None:
        hit = set(node.get_css_classes()) & background_classes
        if hit:
            registered = hit & set(ui_panels._BACKGROUND_BY_CLASS)
            if not registered:
                raise AssertionError(
                    f"{widget!r}'s NEAREST background-affecting ancestor "
                    f"{node!r} carries class(es) {sorted(hit)!r}, which set "
                    "a background in the real ui.panels._PANEL_CSS "
                    "stylesheet but are NOT registered in "
                    "ui.panels._BACKGROUND_BY_CLASS -- teach the walker "
                    "about this background tier; do not let it fall "
                    "through to a different, more distant ancestor")
            return ui_panels._BACKGROUND_BY_CLASS[next(iter(registered))]
        node = node.get_parent()
    raise AssertionError(
        f"no ancestor of {widget!r} carries any class that paints a "
        "background in the real ui.panels._PANEL_CSS stylesheet -- a label "
        "with no backgrounded ancestor at all cannot be contrast-checked")


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


# ---------------------------------------------------------------------------
# Fix round 2 (this review): the generalized legibility guard above cannot
# catch the original Critical defect it exists for. Root cause (see this
# task's report): on this box an UNSTYLED label resolves to white, which
# happens to have ~15:1 contrast against this module's dark ground --
# `_assert_every_label_is_legible` reads the REAL, RESOLVED colour via
# `get_color()`, so it is only as good as whatever theme happens to be
# loaded on the machine running the test. On a light-themed machine the same
# missing rule is invisible text instead, and this same suite would still be
# green. `test_every_label_carries_a_class_with_an_explicit_color_rule`
# below is the theme-INDEPENDENT complement: it never calls `get_color()` or
# depends on any ambient theme at all -- it is pure text analysis of this
# module's own `_PANEL_CSS` source (`_color_rules_from_css`) plus a
# structural check of which CSS classes a widget carries
# (`get_css_classes()`), so it gives the same verdict on every machine.
# ---------------------------------------------------------------------------

def _all_panel_states():
    """One (context, panel) pair per telemetry state and per pipeline
    stage/reset -- the same states `_assert_every_label_is_legible`'s two
    callers above already cover, reused here so both guards inspect
    identical widget trees."""
    telemetry_unknown = TelemetryPanel()
    telemetry_unknown.update(None, None)
    yield "telemetry: unknown", telemetry_unknown

    telemetry_empty = TelemetryPanel()
    telemetry_empty.update([], 0.1)
    yield "telemetry: empty", telemetry_empty

    telemetry_ok = TelemetryPanel()
    telemetry_ok.update([_reading(0, 45.0), _reading(1, 90.0)], 0.1)
    yield "telemetry: ok (normal + hot card)", telemetry_ok

    telemetry_stale = TelemetryPanel()
    telemetry_stale.update([_reading()], STALE_AFTER_S + 5.0)
    yield "telemetry: stale", telemetry_stale

    pipeline = PipelinePanel()
    for stage in STAGE_ORDER:
        pipeline.set_stage(stage, 0.5)
        yield f"pipeline: {stage}", pipeline
    pipeline.reset()
    yield "pipeline: reset", pipeline


def test_every_label_carries_a_class_with_an_explicit_color_rule():
    """Static, theme-INDEPENDENT guard for Critical 1's actual failure
    mode ("someone added/edited a label so it no longer has a real
    `color:` rule behind its classes") -- reproduced two ways in this
    task's report, both of which leave `_assert_every_label_is_legible`
    green on this box because an unstyled label happens to resolve to a
    high-contrast white here:

    - removing the `color:` line from `.telemetry-hero-number`'s rule
      (the hero label keeps its class, so the OLD walker's "is this class
      registered" question was never even the right question -- the
      class was always registered; its RULE stopped setting color);
    - same for `.telemetry-field-value`.

    This check asks a different, theme-proof question: for the classes
    this label ACTUALLY carries right now, does at least one rule in the
    REAL stylesheet text set `color:` for exactly that combination? If
    the answer is no, the label's foreground is at the mercy of whatever
    theme happens to be loaded -- true regardless of what any particular
    machine's theme resolves that inherited value to.
    """
    color_rules = _color_rules_from_css(ui_panels._PANEL_CSS)
    failures = []
    for context, panel in _all_panel_states():
        for label in _iter_labels(panel):
            if not _label_has_an_explicit_color_rule(label, color_rules):
                failures.append(
                    f"[{context}] label {label.get_label()!r} carries "
                    f"classes {sorted(label.get_css_classes())!r}, none of "
                    "which has a matching `color:` rule in the real "
                    "ui.panels._PANEL_CSS stylesheet")
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Fix round 2, problem (b): `_nearest_explicit_background_hex` must actually
# implement "nearest" -- stop at the first ancestor whose class the real
# stylesheet paints a background with, and fail loudly if THAT class is
# unregistered, rather than skipping past it to a registered ancestor
# further up. Both tests below build a synthetic nested background (via
# `monkeypatch` on `ui.panels._PANEL_CSS`, read fresh by the walker on every
# call -- see its own docstring) rather than editing the shipped stylesheet,
# so they can run unconditionally, without a revert step, alongside the rest
# of the suite.
# ---------------------------------------------------------------------------

def test_nearest_background_walker_fails_loudly_on_an_unregistered_nested_background():
    """Reproduces the reviewer's scenario directly: a nested container
    carries a REAL background-color rule (visible to the walker only
    because it genuinely parses `_PANEL_CSS`, not a hand-maintained
    duplicate of "known" backgrounds) that `_BACKGROUND_BY_CLASS` was
    never taught about. The walker must fail loudly right there -- never
    silently validate the label against the panel root's dark ground
    further out, which is exactly the bug that let a real 1.08:1 label
    get certified at 16.6:1."""
    extra_css = (
        ui_panels._PANEL_CSS
        + "\n.__test_unregistered_bg { background-color: #F1F8F8; }\n"
    )
    original_css = ui_panels._PANEL_CSS
    ui_panels._PANEL_CSS = extra_css
    try:
        panel = TelemetryPanel()
        panel.update([_reading()], 0.1)

        inner = Gtk.Box()
        inner.add_css_class("__test_unregistered_bg")
        stray_label = Gtk.Label(label="stray")
        stray_label.add_css_class("telemetry-field-value")
        inner.append(stray_label)
        panel.append(inner)

        with pytest.raises(AssertionError, match="NOT registered"):
            _nearest_explicit_background_hex(stray_label)
    finally:
        ui_panels._PANEL_CSS = original_css


def test_nearest_background_walker_uses_the_true_nearest_registered_background():
    """Positive companion: once the nested background IS taught to
    `_BACKGROUND_BY_CLASS`, the walker must validate against THAT nearer
    background, not the panel root further out -- proving "nearest" is
    fixed, not merely "loud when unregistered." The nested label below
    keeps its real `.telemetry-field-value` class (styled for the DARK
    ground, `_BG_ALT` on `_DARK_BASE`), so once correctly checked against
    its TRUE near background (a light one), it measures a genuine
    legibility failure -- exactly the class of bug (true low contrast
    certified as fine) the reviewer proved was reachable."""
    extra_css = (
        ui_panels._PANEL_CSS
        + "\n.__test_light_bg { background-color: #F1F8F8; }\n"
    )
    original_css = ui_panels._PANEL_CSS
    original_bg_map = ui_panels._BACKGROUND_BY_CLASS
    ui_panels._PANEL_CSS = extra_css
    ui_panels._BACKGROUND_BY_CLASS = dict(original_bg_map, __test_light_bg="#F1F8F8")
    try:
        panel = TelemetryPanel()
        panel.update([_reading()], 0.1)

        inner = Gtk.Box()
        inner.add_css_class("__test_light_bg")
        stray_label = Gtk.Label(label="stray")
        stray_label.add_css_class("telemetry-field-value")
        inner.append(stray_label)
        panel.append(inner)

        bg_hex = _nearest_explicit_background_hex(stray_label)
        assert bg_hex == "#F1F8F8", (
            "must validate against the label's TRUE nearest background, "
            "not fall through to the panel root's dark ground")

        fg_hex = _rgba_to_hex(stray_label.get_color())
        ratio = contrast_ratio(fg_hex, bg_hex)
        assert ratio < MIN_CONTRAST_RATIO, (
            f"expected a real legibility failure against the true nearby "
            f"background, got ratio={ratio:.2f} (fg={fg_hex} bg={bg_hex})")
    finally:
        ui_panels._PANEL_CSS = original_css
        ui_panels._BACKGROUND_BY_CLASS = original_bg_map
