"""Tests for ui/gallery.py: the pure `grid_shape` layout decision and the
`Gallery` widget built on top of it.

Constructing a `Gallery` needs a live display (it builds real
`Gtk.Grid`/`Gtk.Button`/`Gtk.Label`/`Gtk.Picture` children in `__init__`,
same as ui/panels.py's `TelemetryPanel`/`PipelinePanel` -- see that test
file's own module docstring) -- this box has one (DISPLAY=:0 per the
task's environment notes), so these run against the real thing.

Section map:
  1. The brief's own `grid_shape` tests, verbatim.
  2. Thumbnail loading (`_load_thumbnail_texture`): missing file, corrupt
     file, and a real image, each verified directly and each verified NOT
     to raise -- the "beyond the brief" requirement that a missing/broken
     thumbnail renders a deliberate placeholder, never GTK's own
     broken-image icon and never an uncaught exception.
  3. Widget assembly: one card per target, correct grid placement, and
     `on_pick` firing with the TAPPED card's own id (not always the first
     or the last).
  4. The legibility guard, extended to ui/gallery.py per this task's
     brief, via the shared tests/unit/_legibility.py module ui/panels.py's
     own guard was factored into -- both the theme-independent static
     check (every label carries a class with a real `color:` rule) and
     the theme-dependent runtime contrast check (>=4.5:1 against the
     nearest explicit background), exercised across every visual state
     this module can render: a card with a real thumbnail, a card with a
     missing one (the placeholder), and a card with a long blurb.
"""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

import pytest
from gi.repository import GdkPixbuf, Gtk

import _legibility
import ui.gallery as ui_gallery
from ui.gallery import Gallery, MIN_CONTRAST_RATIO, _format_fold_time, contrast_ratio, grid_shape
from ui.playlist import Target

# The thumbnail-loading/placeholder helpers (_load_thumbnail_texture,
# _build_thumbnail, _placeholder_glyph) are module-private (leading
# underscore) -- accessed here via `ui_gallery.<name>`, the same qualified-
# access pattern tests/unit/test_panels.py already uses for private
# INSTANCE state (`panel._cards_box`, `panel._rows`), rather than a bare
# `from ui.gallery import _build_thumbnail` that would be the only
# private-name import anywhere in this test suite.


def _target(id="t", name="Trp-cage", blurb="A small fast-folding protein.",
            expected_s=4.4, thumbnail=None, input_path=None):
    return Target(
        id=id,
        input_path=input_path or Path("/nonexistent/input.yaml"),
        model="protenix-v2",
        name=name,
        blurb=blurb,
        expected_s=expected_s,
        thumbnail=thumbnail,
    )


def _make_real_png(path):
    """A tiny but genuinely valid PNG, built with GdkPixbuf itself (the
    same library _load_thumbnail_texture uses to read one back) rather
    than depending on an external image library this venv may not have --
    see docs/venv-bootstrap-notes.md: venv-ui has gi/gemmi/PyOpenGL/numpy,
    nothing more."""
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 4, 4)
    pixbuf.fill(0x336699FF)
    pixbuf.savev(str(path), "png", [], [])


# ---------------------------------------------------------------------------
# 1. The brief's own tests, verbatim.
# ---------------------------------------------------------------------------

def test_a_single_target_gets_one_cell():
    assert grid_shape(1, 1280) == (1, 1)


def test_targets_fill_columns_before_adding_rows():
    cols, rows = grid_shape(6, 1280)
    assert cols * rows >= 6
    assert rows == pytest.approx(-(-6 // cols))


def test_a_narrow_screen_uses_fewer_columns():
    wide_cols, _ = grid_shape(12, 1920)
    narrow_cols, _ = grid_shape(12, 800)
    assert narrow_cols < wide_cols


def test_no_target_is_ever_dropped():
    """A layout that cannot fit everything must add rows, not truncate."""
    for n in (1, 3, 7, 12, 30):
        cols, rows = grid_shape(n, 1280)
        assert cols * rows >= n


def test_zero_targets_does_not_divide_by_zero():
    assert grid_shape(0, 1280) == (0, 0)


# ---------------------------------------------------------------------------
# Additional grid_shape coverage.
# ---------------------------------------------------------------------------

def test_grid_shape_never_gives_more_columns_than_targets():
    """A single target on a very wide screen must not get spread across
    empty columns it doesn't need -- cols is capped at n_targets."""
    cols, rows = grid_shape(1, 1920 * 4)
    assert cols == 1
    assert rows == 1


def test_grid_shape_is_deterministic():
    assert grid_shape(7, 1280) == grid_shape(7, 1280)


# ---------------------------------------------------------------------------
# `_format_fold_time`: a measured time formats as a time; an unmeasured
# target (expected_s is None) gets its own explicit, deliberate-looking
# sentence -- never blank, never a fabricated number.
# ---------------------------------------------------------------------------

def test_a_measured_fold_time_is_shown_as_a_time():
    text = _format_fold_time(4.4)
    assert "4.4" in text
    assert "s" in text


def test_an_unmeasured_fold_time_says_so_explicitly_not_blank():
    text = _format_fold_time(None)
    assert text.strip() != ""
    # Must not read as a number a visitor could mistake for a real
    # measurement -- no digits at all in the unmeasured case.
    assert not any(ch.isdigit() for ch in text)


def test_format_fold_time_rounds_rather_than_shows_false_precision():
    """0.1s-precision measurement methodology (docs/followups.md's 30-fold
    soak) must not be dressed up with more decimal places than that: a
    value like 4.44444 must format with exactly one decimal digit, not be
    echoed back verbatim."""
    assert _format_fold_time(4.44444) == "~4.4s to fold"


# ---------------------------------------------------------------------------
# 2. Thumbnail loading: missing / corrupt / real, each must not raise.
# ---------------------------------------------------------------------------

def test_a_missing_thumbnail_file_returns_none_not_an_exception(tmp_path):
    texture = ui_gallery._load_thumbnail_texture(tmp_path / "does-not-exist.png")
    assert texture is None


def test_a_corrupt_thumbnail_file_returns_none_not_an_exception(tmp_path):
    """GdkPixbuf raises GLib.Error (verified directly, see this task's
    report) for a file that exists but isn't decodable as an image --
    _load_thumbnail_texture must catch that too, identically to a missing
    file, not just the "file not found" case."""
    bad = tmp_path / "not-really-a-png.png"
    bad.write_text("this is not image data")
    texture = ui_gallery._load_thumbnail_texture(bad)
    assert texture is None


def test_a_real_thumbnail_file_loads_successfully(tmp_path):
    good = tmp_path / "real.png"
    _make_real_png(good)
    texture = ui_gallery._load_thumbnail_texture(good)
    assert texture is not None


def test_placeholder_glyph_is_the_uppercase_first_letter():
    assert ui_gallery._placeholder_glyph("Trp-cage") == "T"
    assert ui_gallery._placeholder_glyph("hemoglobin") == "H"


def test_placeholder_glyph_of_a_blank_name_does_not_raise():
    assert ui_gallery._placeholder_glyph("   ") == "?"


def test_build_thumbnail_of_a_target_with_no_thumbnail_is_a_placeholder():
    widget = ui_gallery._build_thumbnail(_target(thumbnail=None))
    assert not isinstance(widget, Gtk.Picture)
    assert widget.has_css_class("gallery-thumbnail-placeholder")


def test_build_thumbnail_of_a_missing_file_is_a_placeholder_not_an_exception(tmp_path):
    """The defensive case: a manifest that NAMES a thumbnail whose file
    isn't actually there. Must render exactly like "no thumbnail at all",
    never raise and never fall through to some other broken state."""
    widget = ui_gallery._build_thumbnail(_target(thumbnail=tmp_path / "missing.png"))
    assert widget.has_css_class("gallery-thumbnail-placeholder")


def test_build_thumbnail_of_a_corrupt_file_is_a_placeholder_not_an_exception(tmp_path):
    bad = tmp_path / "corrupt.png"
    bad.write_text("not a png")
    widget = ui_gallery._build_thumbnail(_target(thumbnail=bad))
    assert widget.has_css_class("gallery-thumbnail-placeholder")


def test_build_thumbnail_of_a_real_file_is_a_picture_not_a_placeholder(tmp_path):
    good = tmp_path / "real.png"
    _make_real_png(good)
    widget = ui_gallery._build_thumbnail(_target(thumbnail=good))
    assert isinstance(widget, Gtk.Picture)


# ---------------------------------------------------------------------------
# 3. Widget assembly.
# ---------------------------------------------------------------------------

def test_gallery_builds_one_card_per_target():
    targets = [_target(id="a"), _target(id="b"), _target(id="c")]
    gallery = Gallery(targets, width_px=1280)
    assert set(gallery.cards) == {"a", "b", "c"}
    for card in gallery.cards.values():
        assert isinstance(card, Gtk.Button)


def test_gallery_with_zero_targets_does_not_raise():
    gallery = Gallery([], width_px=1280)
    assert gallery.cards == {}


def test_gallery_places_cards_row_major_per_grid_shape():
    """3 targets at a width that only fits 1 column must stack straight
    down (0,0), (0,1), (0,2) -- proving the widget's placement actually
    follows grid_shape's column count, not some independent layout."""
    targets = [_target(id="a"), _target(id="b"), _target(id="c")]
    gallery = Gallery(targets, width_px=400)  # 400 // 400 == 1 column
    cols, _rows = grid_shape(3, 400)
    assert cols == 1
    assert gallery._grid.get_child_at(0, 0) is gallery.cards["a"]
    assert gallery._grid.get_child_at(0, 1) is gallery.cards["b"]
    assert gallery._grid.get_child_at(0, 2) is gallery.cards["c"]


def test_gallery_places_cards_filling_columns_before_rows():
    """6 targets at a width fitting 3 columns must fill row 0 fully
    (0,0),(1,0),(2,0) before starting row 1 -- the widget-level mirror of
    grid_shape's own "fill columns before adding rows" contract."""
    targets = [_target(id=str(i)) for i in range(6)]
    gallery = Gallery(targets, width_px=1280)
    cols, _rows = grid_shape(6, 1280)
    assert cols == 3
    for index, target in enumerate(targets):
        row, col = divmod(index, cols)
        assert gallery._grid.get_child_at(col, row) is gallery.cards[target.id]


def test_tapping_a_card_invokes_on_pick_with_that_cards_own_target_id():
    """Negative-control-shaped: builds THREE distinct targets and taps the
    MIDDLE one specifically, so an implementation that always reports the
    first (or the last) target's id -- which a naive `lambda *_: on_pick(
    targets[0].id)` wiring mistake would do -- fails this test, not just
    "some" id-matching test."""
    targets = [_target(id="a"), _target(id="b"), _target(id="c")]
    picked = []
    gallery = Gallery(targets, on_pick=picked.append, width_px=1280)
    gallery.cards["b"].emit("clicked")
    assert picked == ["b"]


def test_tapping_each_card_in_turn_reports_its_own_id():
    targets = [_target(id="a"), _target(id="b"), _target(id="c")]
    picked = []
    gallery = Gallery(targets, on_pick=picked.append, width_px=1280)
    for target in targets:
        gallery.cards[target.id].emit("clicked")
    assert picked == ["a", "b", "c"]


def test_tapping_a_card_with_no_on_pick_callback_does_not_raise():
    gallery = Gallery([_target(id="a")], width_px=1280)
    gallery.cards["a"].emit("clicked")  # must not raise


def test_gallery_card_shows_the_targets_name_and_blurb():
    target = _target(id="a", name="Trp-cage", blurb="Folds in microseconds.")
    gallery = Gallery([target], width_px=1280)
    labels = {label.get_label() for label in _legibility.iter_labels(gallery)}
    assert "Trp-cage" in labels
    assert "Folds in microseconds." in labels


def test_gallery_card_shows_a_measured_fold_time():
    target = _target(id="a", expected_s=4.4)
    gallery = Gallery([target], width_px=1280)
    labels = {label.get_label() for label in _legibility.iter_labels(gallery)}
    assert _format_fold_time(4.4) in labels


def test_gallery_card_of_an_unmeasured_target_says_so_not_a_bogus_time():
    """A target with `expected_s=None` (not yet folded on real hardware --
    ui.playlist's own contract) must render the SAME deliberate sentence
    _format_fold_time produces for None, not a blank card slot and not the
    default 4.4 some other target happens to carry."""
    target = _target(id="a", expected_s=None)
    gallery = Gallery([target], width_px=1280)
    labels = {label.get_label() for label in _legibility.iter_labels(gallery)}
    assert _format_fold_time(None) in labels


# ---------------------------------------------------------------------------
# 4. Legibility guard, extended from ui/panels.py's (see this task's brief:
# "extend that guard to cover it rather than leaving ui/gallery.py
# unprotected"). Both halves reused from tests/unit/_legibility.py, the
# module ui/panels.py's own guard was factored into for exactly this reuse
# -- not a second, hand-copied implementation of the same regex/CSS-cascade
# logic.
# ---------------------------------------------------------------------------

def _gallery_css_text_fn():
    return ui_gallery._GALLERY_CSS


def _gallery_background_by_class_fn():
    return ui_gallery._BACKGROUND_BY_CLASS


def _all_gallery_states(tmp_path):
    """One Gallery per visual state this module can render: a card with a
    real thumbnail, a card whose thumbnail is missing (the placeholder),
    and a card with an unusually long blurb (wrapped text is still just a
    Gtk.Label with the same CSS classes, but exercised for real rather
    than assumed)."""
    real_png = tmp_path / "real.png"
    _make_real_png(real_png)

    with_thumbnail = Gallery(
        [_target(id="a", thumbnail=real_png)], width_px=1280)
    yield "card with real thumbnail", with_thumbnail

    without_thumbnail = Gallery([_target(id="b", thumbnail=None)], width_px=1280)
    yield "card with placeholder thumbnail", without_thumbnail

    long_blurb = Gallery([_target(
        id="c", thumbnail=None,
        blurb="A very long blurb " * 20,
    )], width_px=1280)
    yield "card with long blurb", long_blurb

    multi = Gallery(
        [_target(id="d1", thumbnail=None), _target(id="d2", thumbnail=real_png)],
        width_px=1280)
    yield "multi-card grid", multi

    # The new gallery-card-time label added when the playlist grew past its
    # first target: an unmeasured target (expected_s=None) is the state
    # this task actually ships (three of the four real targets), so the
    # legibility guard must see the "not yet timed" wording rendered for
    # real, not just the measured 4.4 default every other fixture above
    # uses.
    unmeasured = Gallery([_target(id="e", thumbnail=None, expected_s=None)], width_px=1280)
    yield "card with unmeasured fold time", unmeasured


def test_every_gallery_label_is_legible_in_every_state(tmp_path):
    for context, gallery in _all_gallery_states(tmp_path):
        _legibility.assert_every_label_is_legible(
            gallery, context=context, min_contrast=MIN_CONTRAST_RATIO,
            contrast_ratio_fn=contrast_ratio,
            css_text_fn=_gallery_css_text_fn,
            background_by_class_fn=_gallery_background_by_class_fn,
        )


def test_every_gallery_label_carries_a_class_with_an_explicit_color_rule(tmp_path):
    """Theme-INDEPENDENT companion to the runtime contrast check above --
    see ui/panels.py's own "Fix round 2" for why both are needed: an
    unstyled label can happen to resolve to a high-contrast colour on THIS
    particular machine's theme, silently passing the runtime check while
    still being one CSS edit away from invisible on a different machine.
    This asks a different question: for the classes a label ACTUALLY
    carries, does the real _GALLERY_CSS text set a `color:` for exactly
    that combination?"""
    color_rules = _legibility.color_rules_from_css(ui_gallery._GALLERY_CSS)
    failures = []
    for context, gallery in _all_gallery_states(tmp_path):
        for label in _legibility.iter_labels(gallery):
            if not _legibility.label_has_an_explicit_color_rule(label, color_rules):
                failures.append(
                    f"[{context}] label {label.get_label()!r} carries "
                    f"classes {sorted(label.get_css_classes())!r}, none of "
                    "which has a matching `color:` rule in the real "
                    "ui.gallery._GALLERY_CSS stylesheet")
    assert not failures, "\n".join(failures)


def test_gallery_background_map_registers_every_background_class_in_its_own_css():
    """Sanity companion: every bare-class background rule in
    _GALLERY_CSS must be registered in _BACKGROUND_BY_CLASS -- otherwise
    the walker in the tests above would (correctly) start raising
    "NOT registered" the moment a real card's own stylesheet grows a new
    background tier that this test file's fixtures don't happen to visit."""
    css_classes = _legibility.background_affecting_classes_from_css(
        ui_gallery._GALLERY_CSS)
    assert css_classes <= set(ui_gallery._BACKGROUND_BY_CLASS)


# ---------------------------------------------------------------------------
# The gallery must promise exactly what a tap delivers -- no more, no less.
#
# This copy has been wrong in both directions, so these tests are written
# against both. Whole-branch review, Critical 2: every string here once said
# a tap would fold that protein while a visitor's pick reached nothing at
# all, and the copy was walked back to a catalogue with a plainly-stated note
# that choosing one was not wired up. Task 17 connected the tap to the
# daemon, which made the walked-back copy the opposite lie -- so it changed
# back in that same commit, and these tests changed with it.
#
# What must NEVER come back is the specific over-promise: a tap does not
# interrupt anything, so the pick starts on the next chip to free rather than
# now. "Next", never "instantly".
# ---------------------------------------------------------------------------

def _all_text(widget):
    """Every label and tooltip a visitor can read on this widget."""
    texts = [label.get_label() or "" for label in _legibility.iter_labels(widget)]

    def walk(node):
        tooltip = node.get_tooltip_text()
        if tooltip:
            texts.append(tooltip)
        child = node.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(widget)
    return texts


def test_no_card_promises_a_fold_that_starts_at_once():
    """The over-promise that has to stay dead. A tap puts that protein NEXT;
    it starts when a chip frees, because nothing running is interrupted. A
    card that said otherwise would break its promise in front of the one
    visitor standing there timing it.

    Mutation this catches: `_CARD_HINT = "TAP TO FOLD NOW"`.
    """
    gallery = Gallery([_target(id="a", name="Trp-cage")], width_px=1280)
    for text in _all_text(gallery):
        lowered = text.lower()
        for forbidden in ("instantly", "straight away", "immediately",
                          "tap to fold now"):
            assert forbidden not in lowered, f"over-promising copy: {text!r}"


def test_a_card_tells_a_visitor_that_tapping_folds_it_next():
    """The inverse of the test this replaced, and it replaced it in the same
    commit as the behaviour. A booth that disclaims a capability it has
    teaches visitors not to try it -- which for this feature means nobody
    ever sees the thing the whole amendment exists for."""
    gallery = Gallery([_target(id="a", name="Trp-cage")], width_px=1280)
    joined = " ".join(_all_text(gallery)).lower()
    assert "next" in joined
    assert "isn't wired up" not in joined
    assert "is not wired up" not in joined


def test_the_screen_still_says_what_it_is():
    """Both halves matter: an implementation that merely swapped the strings
    would leave a visitor with an unexplained grid of proteins."""
    gallery = Gallery([_target(id="a", name="Trp-cage")], width_px=1280)
    joined = " ".join(_all_text(gallery)).lower()
    assert "what this booth folds" in joined


def test_the_screen_says_the_running_folds_are_left_to_finish():
    """The wait, stated as the feature it is, in the one place a visitor
    reads BEFORE tapping rather than after. Without it, a few seconds of
    nothing happening reads as a broken booth."""
    gallery = Gallery([_target(id="a", name="Trp-cage")], width_px=1280)
    joined = " ".join(_all_text(gallery)).lower()
    assert "finish" in joined or "interrupt" in joined


# ── the fold-time range, for a box whose chips do not all fold alike ────────

def test_a_ranged_target_shows_both_ends():
    """Two of this box's four chips throttle about fifteen minutes into a
    session and the long targets lose 4-26% on them (Task 19's soak). A
    visitor's pick lands on whichever chip frees up first and they cannot
    choose, so a card showing only the fast number is a promise the booth
    misses half the time.

    Mutation: ignoring `expected_slow_s`. Red.
    """
    from ui.gallery import _format_fold_time

    assert _format_fold_time(19.7, 25.0) == "20-25s to fold"
    assert _format_fold_time(22.3, 27.1) == "22-27s to fold"


def test_a_range_too_narrow_for_whole_seconds_keeps_its_decimal():
    """FKBP12's 11.7 and 12.3 both round to 12, and "12-12s" is not a range
    -- it reads as a rendering fault. This was the first version of the rule.

    Mutation: dropping the `round(...) != round(...)` test. Red.
    """
    from ui.gallery import _format_fold_time

    assert _format_fold_time(11.7, 12.3) == "11.7-12.3s to fold"


def test_a_target_without_a_slow_end_is_unchanged():
    """Most targets need no range: the short ones finish before a chip has
    time to warm up. Their cards must read exactly as they did."""
    from ui.gallery import _format_fold_time

    assert _format_fold_time(4.4) == "~4.4s to fold"
    assert _format_fold_time(8.6, None) == "~8.6s to fold"
    assert _format_fold_time(None) == "fold time: not yet timed"


def test_a_degenerate_range_does_not_render_as_one():
    """A slow end equal to (or below) the fast end is not a range. Rendering
    "4.4-4.4s" would be a worse claim than the single number it replaced."""
    from ui.gallery import _format_fold_time

    assert _format_fold_time(4.4, 4.4) == "~4.4s to fold"


def test_the_shipped_playlist_ranges_are_ordered_and_measured():
    """Every ranged entry in the real manifest, checked against the loader's
    own validation: a slow end below the fast end is rejected at load, so
    this is what notices if one is ever edited into the manifest backwards
    AND the validation is weakened.
    """
    from ui.playlist import load_playlist

    ranged = [t for t in load_playlist("playlist/manifest.yaml")
              if t.expected_slow_s is not None]
    assert ranged, "no shipped target carries a range at all"
    for t in ranged:
        assert t.expected_s is not None, f"{t.id}: a range needs both ends"
        assert t.expected_slow_s > t.expected_s, (
            f"{t.id}: slow end {t.expected_slow_s} is not slower than "
            f"{t.expected_s}")
