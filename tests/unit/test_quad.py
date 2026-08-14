"""The quad view: four cells, four chips, one notice.

Most of these come from Task 14's brief verbatim. The ones after each marked
divider are this file's own, and they exist for two reasons the brief's set
does not cover:

1. **Four cells are a symmetric fixture.** Nearly every assertion below could
   be written against "some cell" and stay green while the quad rendered
   slot 2's fold into cell 1. So every test here that touches more than one
   cell gives each cell a DIFFERENT value and asserts on all of them, never
   on one -- see `_DISTINCT_CAPTIONS`. This project lost four bugs to
   degenerate fixtures (an on-axis camera that made a matrix transposition
   invisible; a mesh suite that never checked winding), and a 2x2 grid of
   identical cells is the same trap with a different shape.

2. **The machinery that must survive.** Camera ownership and
   hold-until-superseded are per-instance fields on `StructureViewer`, so
   four viewers get four copies for free -- but "for free by construction"
   is a claim, and an implementation that shared one viewer across cells, or
   reused one camera, would pass every test in the brief. Two tests below
   drive those directly.

No test here needs GL to be realized: `set_points`/`set_ribbon` are both
explicitly safe to call before realize, and the camera fields they move are
plain numpy state.
"""

import numpy as np
import pytest

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from _legibility import (assert_every_label_is_legible, color_rules_from_css,
                         iter_labels, label_has_an_explicit_color_rule)
from ui.panels import MIN_CONTRAST_RATIO, contrast_ratio
from ui.quad import QuadView, grid_position
import ui.quad as quadmod


# Four visibly different strings, one per cell. Used everywhere a test could
# otherwise confuse two cells: if a caption is routed to the wrong cell, the
# assertion names which one and which string it found.
_DISTINCT_CAPTIONS = ["TRUNK 12%", "DIFFUSION 62%", "SAVING", "FAILED"]


# ---------------------------------------------------------------------------
# The brief's tests.
# ---------------------------------------------------------------------------

def test_the_four_cells_are_a_two_by_two_grid():
    assert [grid_position(s) for s in range(4)] == [(0, 0), (1, 0), (0, 1), (1, 1)]


def test_reading_order_is_left_to_right_then_down():
    """Cell order must match the telemetry panel's own left-to-right chip
    order, or 'chip 2' on screen means two different chips in two panels."""
    assert grid_position(1)[1] == grid_position(0)[1]
    assert grid_position(2)[1] > grid_position(0)[1]


def test_one_viewer_per_card():
    quad = QuadView(cards=[0, 1, 2, 3])
    assert quad.slot_count == 4
    assert len({id(v) for v in quad.viewers}) == 4


def test_a_two_card_booth_builds_two_cells():
    quad = QuadView(cards=[0, 1])
    assert quad.slot_count == 2


def test_a_six_card_booth_builds_only_four():
    quad = QuadView(cards=[0, 1, 2, 3, 4, 5])
    assert quad.slot_count == 4


def test_each_cell_names_its_own_chip():
    """The claim the Tensix panel had to be walked back for. Now it is true,
    and each cell says which chip it is."""
    quad = QuadView(cards=[0, 1, 2, 3])
    labels = [quad.chip_label_text(s) for s in range(4)]
    assert labels == ["CHIP 0", "CHIP 1", "CHIP 2", "CHIP 3"]


def test_a_sparse_card_list_labels_the_real_chip_numbers():
    quad = QuadView(cards=[1, 3])
    assert [quad.chip_label_text(s) for s in range(2)] == ["CHIP 1", "CHIP 3"]


def test_the_focus_cell_is_marked_and_only_one_is():
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_focus(2)
    focused = [s for s in range(4) if quad.has_focus_marking(s)]
    assert focused == [2]
    quad.set_focus(0)
    assert [s for s in range(4) if quad.has_focus_marking(s)] == [0]


def test_no_focus_marks_nothing():
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_focus(2)
    quad.set_focus(None)
    assert not any(quad.has_focus_marking(s) for s in range(4))


def test_an_out_of_range_focus_does_not_raise():
    """Wire-shaped data reaches this via a card index."""
    quad = QuadView(cards=[0, 1])
    quad.set_focus(9)
    assert not any(quad.has_focus_marking(s) for s in range(2))


def test_a_caption_reaches_only_its_own_cell():
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_caption(1, "DIFFUSION 62%")
    assert quad.caption_text(1) == "DIFFUSION 62%"
    assert quad.caption_text(0) != "DIFFUSION 62%"


def test_an_out_of_range_caption_does_not_raise():
    quad = QuadView(cards=[0, 1])
    quad.set_caption(7, "nonsense")


def test_the_notice_belongs_to_the_quad_and_not_to_a_cell():
    """It names a protein no cell is folding yet. Rendering it into cell 0's
    caption would label whatever cell 0 IS folding with the wrong name."""
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_notice("HEMOGLOBIN — NEXT UP")
    assert quad.notice_text() == "HEMOGLOBIN — NEXT UP"
    assert all(quad.caption_text(s) != "HEMOGLOBIN — NEXT UP" for s in range(4))


def test_a_cleared_notice_leaves_nothing_behind():
    """It is cleared the moment the picked fold starts. A banner still
    saying NEXT UP over the fold it announced is the booth talking over
    itself."""
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_notice("HEMOGLOBIN — NEXT UP")
    quad.set_notice(None)
    assert not quad.notice_text()


def test_the_connection_state_reaches_every_viewer():
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_connection_state("connected")
    assert all(v.connection_state == "connected" for v in quad.viewers)


def test_an_unknown_connection_state_does_not_raise_out_of_the_quad():
    """StructureViewer's setter deliberately raises on an unknown state.
    That validator must not be able to brick the channel for four cells at
    once -- the guard ui/app.py already carries, applied here."""
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_connection_state("teleporting")


def test_every_label_in_the_quad_is_legible():
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_caption(0, "DIFFUSION 62%")
    quad.set_notice("HEMOGLOBIN — NEXT UP")
    assert_every_label_is_legible(
        quad, context="ui.quad", min_contrast=MIN_CONTRAST_RATIO,
        contrast_ratio_fn=contrast_ratio,
        css_text_fn=lambda: quadmod._QUAD_CSS,
        background_by_class_fn=lambda: quadmod._BACKGROUND_BY_CLASS)


# ---------------------------------------------------------------------------
# Beyond the brief 1: the four cells must never be confusable.
#
# Every test above that touches more than one cell touches at most two. These
# drive all four at once, with four different values.
# ---------------------------------------------------------------------------

def test_four_different_captions_land_in_four_different_cells():
    """The brief's caption test sets ONE caption and checks ONE other cell
    did not get it -- which a quad that wrote every caption into cell 1
    would pass, and so would one that wrote them all into the LAST cell
    written to. Set all four, read all four back."""
    quad = QuadView(cards=[0, 1, 2, 3])
    for slot, text in enumerate(_DISTINCT_CAPTIONS):
        quad.set_caption(slot, text)
    assert [quad.caption_text(s) for s in range(4)] == _DISTINCT_CAPTIONS


def test_captions_set_out_of_order_still_land_in_their_own_cells():
    """Slots do not arrive in order off a socket: four chips finish in
    whatever order they finish. A quad that used "the next cell" rather than
    "cell N" would pass the in-order test above."""
    quad = QuadView(cards=[0, 1, 2, 3])
    for slot in (3, 0, 2, 1):
        quad.set_caption(slot, _DISTINCT_CAPTIONS[slot])
    assert [quad.caption_text(s) for s in range(4)] == _DISTINCT_CAPTIONS


def test_every_cell_can_be_focused_in_turn_and_leaves_the_others_dark():
    """The brief's focus test visits cells 2 and 0. An implementation that
    marked `slot % 2` or `slot // 2` would pass it."""
    quad = QuadView(cards=[0, 1, 2, 3])
    for focused in range(4):
        quad.set_focus(focused)
        marked = [s for s in range(4) if quad.has_focus_marking(s)]
        assert marked == [focused], (focused, marked)
        assert quad.focus_slot == focused


def _chip_label_at(quad, column, row):
    """The chip name of whichever cell is REALLY at this grid position.

    Read out of the live widget tree, never from `quad.chip_label_text(slot)`
    -- that answers from the quad's own cell list and would happily agree
    with itself while the widgets sat somewhere else entirely. (First draft
    of this file did exactly that, and the transposed-attach mutation
    survived it.)
    """
    child = quad.get_child_at(column, row)
    if child is None:
        return None
    for label in iter_labels(child):
        if "quad-chip-label" in label.get_css_classes():
            return label.get_label()
    raise AssertionError(f"no chip label inside the cell at {(column, row)}")


def test_a_cell_is_attached_at_its_own_grid_position():
    """`grid_position` being right is worth nothing if the cells are not
    attached where it says. Read the CHIP NAME out of the widget really
    sitting at each position, so a transposed attach -- which for a
    symmetric 2x2 leaves the SET of occupied positions identical, and so is
    invisible to any test that only asks "is something here" -- is caught."""
    quad = QuadView(cards=[0, 1, 2, 3])
    seen = {(column, row): _chip_label_at(quad, column, row)
            for column in range(2) for row in range(2)}
    assert seen == {(0, 0): "CHIP 0", (1, 0): "CHIP 1",
                    (0, 1): "CHIP 2", (1, 1): "CHIP 3"}
    # And every cell is a distinct widget at a distinct position.
    assert len({id(quad.get_child_at(*grid_position(s))) for s in range(4)}) == 4


def test_a_sparse_card_list_still_puts_slot_n_in_slot_ns_position():
    """Cards [1, 3] fill the TOP ROW, not the diagonal: slot index drives
    position, card number drives only the label. Getting this backwards puts
    CHIP 3 in the bottom-right of a two-chip booth with two empty cells."""
    quad = QuadView(cards=[1, 3])
    assert quad.get_child_at(0, 0) is not None
    assert quad.get_child_at(1, 0) is not None
    assert quad.get_child_at(0, 1) is None
    assert quad.chip_label_text(0) == "CHIP 1"
    assert quad.chip_label_text(1) == "CHIP 3"


def test_viewer_for_slot_returns_that_slots_own_viewer():
    quad = QuadView(cards=[0, 1, 2, 3])
    for slot in range(4):
        assert quad.viewer_for_slot(slot) is quad.viewers[slot]
    assert quad.viewer_for_slot(9) is None
    assert quad.viewer_for_slot(-1) is None


# ---------------------------------------------------------------------------
# Beyond the brief 2: the machinery that had to survive the rewrite.
# ---------------------------------------------------------------------------

def _cloud(n, scale):
    """A deterministic point cloud of a given size, scaled so a camera
    framed against it has a distinctly different extent from one framed
    against a different scale."""
    rng = np.random.default_rng(7)
    return (rng.normal(size=(n, 3)) * scale).astype(np.float32)


def _ribbon(n, scale):
    verts = _cloud(n, scale)
    return (verts, np.zeros_like(verts), np.ones((n, 3), dtype=np.float32),
            np.arange(n, dtype=np.uint32))


def test_each_cell_owns_its_camera_separately():
    """CAMERA OWNERSHIP, per cell. A finished ribbon keeps the camera so an
    incoming fold's noise cannot rescale it -- the bug it prevents rendered
    the protein at 14% of frame height instead of 68%.

    Four cells must each get their OWN copy of that, not share one. So:
    give cell 1 a ribbon (it takes the camera) and give cell 2 nothing (its
    point cloud still owns the camera), then hit BOTH with an enormous noise
    cloud. Cell 1's extent must not move; cell 2's must.
    """
    quad = QuadView(cards=[0, 1, 2, 3])
    held, free = quad.viewer_for_slot(1), quad.viewer_for_slot(2)
    held.set_ribbon(*_ribbon(30, 1.0))
    before_held, before_free = held._extent, free._extent

    noise = _cloud(200, 400.0)
    held.set_points(noise)
    free.set_points(noise)

    assert held._extent == before_held, (
        "cell 1's finished ribbon lost the camera to another fold's noise")
    assert free._extent != before_free, (
        "cell 2 never had a ribbon, so its point cloud should still frame it "
        "-- if this is unchanged the test proved nothing about ownership")
    # The two cells must not have converged on one shared camera.
    assert held._extent != free._extent


def test_a_cells_camera_is_released_by_its_own_clear_and_no_one_elses():
    """Ownership releases on `clear_structure` -- for THAT cell. A shared
    reset would hand every cell's camera back at once, so the next noise
    frame anywhere rescaled every finished ribbon in the quad."""
    quad = QuadView(cards=[0, 1, 2, 3])
    a, b = quad.viewer_for_slot(0), quad.viewer_for_slot(3)
    for viewer in (a, b):
        viewer.set_ribbon(*_ribbon(30, 1.0))
    before_b = b._extent

    a.clear_structure()
    noise = _cloud(200, 400.0)
    a.set_points(noise)
    b.set_points(noise)

    assert a._extent != before_b, "cell 0's own clear did not release it"
    assert b._extent == before_b, (
        "clearing cell 0 released cell 3's camera too")


def test_set_blend_zero_releases_only_its_own_cells_camera():
    """The second documented release path, checked per cell for the same
    reason as `clear_structure` above."""
    quad = QuadView(cards=[0, 1, 2, 3])
    a, b = quad.viewer_for_slot(2), quad.viewer_for_slot(1)
    for viewer in (a, b):
        viewer.set_ribbon(*_ribbon(30, 1.0))
    before_b = b._extent

    a.set_blend(0)
    noise = _cloud(200, 400.0)
    a.set_points(noise)
    b.set_points(noise)

    assert a._extent != before_b
    assert b._extent == before_b


def test_holding_one_cell_does_not_dim_the_others():
    """HOLD-UNTIL-SUPERSEDED, per cell. The viewer keeps the last real
    structure, dimmed and captioned, until a new fold produces coordinates
    -- because only `diffusion` emits frames and a big protein spends ~15s
    in `trunk` with nothing to draw. With four folds in flight, one cell
    holding must not dim the three that are live."""
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.viewer_for_slot(2).set_held(True)
    assert [v.held for v in quad.viewers] == [False, False, True, False]
    quad.viewer_for_slot(2).set_held(False)
    assert not any(v.held for v in quad.viewers)


def test_every_cells_viewer_is_a_separate_object_with_separate_state():
    """The blunt version of both tests above: no field a fold writes may be
    shared between two cells."""
    quad = QuadView(cards=[0, 1, 2, 3])
    # Scales chosen well clear of `ui.viewer._MIN_EXTENT` (5.0): two cells
    # given small clouds BOTH clamp to the floor and look "shared" when they
    # are not, which is a false red this test hit on its first run.
    for slot, viewer in enumerate(quad.viewers):
        viewer.set_points(_cloud(10 + slot, 3.0 + 4.0 * slot))
    extents = [v._extent for v in quad.viewers]
    from ui.viewer import _MIN_EXTENT
    assert all(e > _MIN_EXTENT for e in extents), (extents, _MIN_EXTENT)
    assert len(set(extents)) == 4, extents


# ---------------------------------------------------------------------------
# Beyond the brief 3: the quad must not grow the layout.
#
# The rail grew last time from a WebView's NATURAL width -- the hero slot went
# 1332 -> 1300px -- and the "never expands the rail" test missed it because it
# only checked the MINIMUM. These measure the natural width.
# ---------------------------------------------------------------------------

def _natural_width(widget):
    _minimum, natural, _min_base, _nat_base = widget.measure(
        Gtk.Orientation.HORIZONTAL, -1)
    return natural


def test_the_notices_width_saturates_instead_of_following_its_text():
    """The notice is a real grid child spanning both columns, so unlike the
    captions (which are overlay children, and GTK excludes those from an
    overlay's own measurement) its text really does move the quad's natural
    width. It must therefore SATURATE: a 4000-character notice must measure
    exactly the same as one at the label's own character limit.

    Measured first draft, before `max-width-chars`: a hidden notice measured
    16px and a 4000-character one measured 444px -- and without ellipsizing
    at all it follows the text without bound. "Never grows at all" is the
    wrong claim (a notice that is showing legitimately needs room); "grows
    by its own declared limit and not one pixel further, whatever it is
    handed" is the right one, and it is the one that goes red if either the
    ellipsize mode or the character cap is deleted.
    """
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_notice("AA")
    short = _natural_width(quad)
    quad.set_notice("A" * quadmod._NOTICE_MAX_CHARS)
    at_limit = _natural_width(quad)
    quad.set_notice("A" * 4000)
    absurd = _natural_width(quad)

    assert at_limit > short, (
        short, at_limit,
        "the limit is not even reached by a string at the limit -- this test "
        "would pass against a label that never grows at all")
    assert absurd == at_limit, (at_limit, absurd)


def test_even_a_saturated_notice_fits_the_hero_slot():
    """The absolute bound, against the real screen. `ui/app.py` gives the
    rail a fixed width and the hero slot everything else; a quad whose
    natural width exceeded that would be asking `GtkBoxLayout` for space
    the rail is holding -- the exact mechanism that moved the hero slot
    1332 -> 1300px last time."""
    from ui.app import _SIDE_RAIL_WIDTH_PX
    hero_slot = 1920 - _SIDE_RAIL_WIDTH_PX
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_notice("A" * 4000)
    for slot in range(4):
        quad.set_caption(slot, "HAEMOGLOBIN ALPHA CHAIN " * 200)
    assert _natural_width(quad) < hero_slot, (
        _natural_width(quad), hero_slot)


def test_a_long_caption_does_not_widen_the_quad():
    """Captions are overlay children and GTK excludes those from an
    overlay's measurement by default, so this holds structurally rather
    than because of the ellipsize -- but it holds only as long as nobody
    calls `set_measure_overlay(True)` or moves the labels into the cell's
    own box, which is exactly the change this catches."""
    quad = QuadView(cards=[0, 1, 2, 3])
    baseline = _natural_width(quad)
    for slot in range(4):
        quad.set_caption(slot, "HAEMOGLOBIN ALPHA CHAIN " * 200)
    assert _natural_width(quad) <= baseline, (
        _natural_width(quad), baseline)


def test_a_long_chip_label_cannot_widen_the_quad():
    """A card number is small today. It is still the one label built from
    data the UI did not choose."""
    narrow = QuadView(cards=[0, 1, 2, 3])
    wide = QuadView(cards=[100000, 200000, 300000, 400000])
    assert _natural_width(wide) <= _natural_width(narrow) + 1


def test_at_a_real_booth_size_no_label_is_ellipsized_away():
    """FOUND BY LOOKING AT IT, and by nothing else in this file.

    The first version of the quad set every label `halign START`. An
    ellipsizing label with `halign START` is allocated its NATURAL width,
    which `max-width-chars` caps -- and that cap is an average-character
    estimate, so a line of capitals is cut well before the count suggests.
    On the real booth the cells said "CHIP …" and captions were cut
    mid-word ("TRYPSIN — 2…") with two thirds of the cell empty beside
    them. Every test in this file stayed green: they read `get_label()`,
    which returns the full string whatever Pango draws.

    So this one allocates the quad at a real booth size and asks PANGO what
    it actually rendered. `halign FILL` + `xalign` is the fix, and reverting
    either turns this red.
    """
    quad = QuadView(cards=[0, 1, 2, 3])
    for slot, text in enumerate(_DISTINCT_CAPTIONS):
        quad.set_caption(slot, text)
    quad.set_notice("HEMOGLOBIN — NEXT UP")
    # 1368x860 is the hero slot on the booth's own 1920x1080 fullscreen
    # window (1920 minus ui/app.py's `_SIDE_RAIL_WIDTH_PX`), i.e. the size
    # this actually has to be right at.
    quad.allocate(1368, 860, -1, None)

    ellipsized = [label.get_label() for label in iter_labels(quad)
                  if label.get_label() and label.get_layout().is_ellipsized()]
    assert not ellipsized, (
        f"cut off at a full booth width: {ellipsized}")


def test_a_cleared_notice_takes_no_vertical_space():
    """Not merely blank: HIDDEN. A notice row that keeps its height when
    empty steals a slice of the hero image from all four cells for the whole
    time the booth has nothing to say, which is most of the time."""
    quad = QuadView(cards=[0, 1, 2, 3])
    empty = quad.measure(Gtk.Orientation.VERTICAL, -1)[1]
    quad.set_notice("HEMOGLOBIN — NEXT UP")
    shown = quad.measure(Gtk.Orientation.VERTICAL, -1)[1]
    quad.set_notice(None)
    cleared = quad.measure(Gtk.Orientation.VERTICAL, -1)[1]
    assert shown > empty, (empty, shown)
    assert cleared == empty, (empty, cleared)


# ---------------------------------------------------------------------------
# Beyond the brief 4: the toggle key.
#
# The user asked for the quad to be OPTIONAL -- a toggle, not a replacement
# for the hero view -- and for the key to be on the `?` card. The decision and
# the copy live in ui/quad.py so they cannot drift apart; ui/app.py's wiring
# is Task 15's, and these tests are what that task inherits.
# ---------------------------------------------------------------------------

def test_the_toggle_key_is_not_one_already_taken():
    """`?`/F1/Help, D, T, Esc, and the Ctrl chords F/G/Q are spoken for.
    Every other plain key is a visitor touch, so binding one is a real
    trade -- but binding a taken one is a collision."""
    assert quadmod.QUAD_KEYS
    assert not (quadmod.QUAD_KEYS & quadmod.KEYS_ALREADY_TAKEN)


def test_the_toggle_key_names_are_lowercase_keyval_names():
    """`ui/app.py`'s `_handle_key` lowercases `Gdk.keyval_name` before
    comparing. A key spelled "Q" here would never match anything."""
    assert all(key == key.lower() for key in quadmod.QUAD_KEYS)
    assert all(key.strip() == key and key for key in quadmod.QUAD_KEYS)


def test_the_help_card_line_names_the_key_it_documents():
    """The `?` card and the binding must say the same thing. A card that
    documents a key nobody bound is worse than no card."""
    line = quadmod.QUAD_HELP_LINE.lower()
    assert all(f" {key} " in f" {line} " or f"press {key}" in line
               for key in quadmod.QUAD_KEYS), quadmod.QUAD_HELP_LINE


def test_the_help_card_line_says_it_is_a_toggle():
    """Optional was the whole request: it must be possible to get the hero
    view back, and the card has to say so.

    The `or "one" in line` this originally had made it unfailable -- the
    line already says "one protein per chip", so the disjunction was true
    however the second half of the sentence was rewritten. Both halves are
    required now: the word that says press it again, and the name of what
    pressing it again gets you back."""
    line = quadmod.QUAD_HELP_LINE.lower()
    assert "again" in line, line
    assert "single" in line, line


def test_the_help_card_line_claims_nothing_the_booth_cannot_back_up():
    """The booth's standing rule about its own copy. The quad shows four
    chips really folding; it must not be described as anything more."""
    line = quadmod.QUAD_HELP_LINE.lower()
    for overclaim in ("fastest", "world", "instantly", "simulat", "render of"):
        assert overclaim not in line, (overclaim, quadmod.QUAD_HELP_LINE)


# ---------------------------------------------------------------------------
# Beyond the brief 5: legibility, stated in numbers.
# ---------------------------------------------------------------------------

def test_every_label_carries_a_class_this_stylesheet_actually_colours():
    """The STATIC half of the legibility guard, and it is not redundant with
    the live-colour walk below it -- measured: replacing `.quad-caption` on
    the caption label with a class the stylesheet does not colour left
    `test_every_label_in_the_quad_is_legible` GREEN, because the ambient
    desktop theme happened to resolve that label to a light colour that
    still cleared 4.5:1 against the dark ground.

    That is the precise defect `_legibility`'s module docstring exists for:
    a label with no explicit `color:` inherits the DESKTOP THEME, which on
    this machine is legible today and measured ~1.01:1 on the machine where
    this class of bug was first found. The live walk cannot see it; a static
    read of the stylesheet's own text can.
    """
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_caption(0, "DIFFUSION 62%")
    quad.set_notice("HEMOGLOBIN — NEXT UP")
    quad.set_focus(0)
    rules = color_rules_from_css(quadmod._QUAD_CSS)
    labels = list(iter_labels(quad))
    assert len(labels) >= 9, (
        len(labels),
        "expected four chip labels, four captions and the notice -- if this "
        "is short, the walk is not reaching the cells and proves nothing")
    uncoloured = [label.get_label() or "(empty)" for label in labels
                  if not label_has_an_explicit_color_rule(label, rules)]
    assert not uncoloured, (
        f"labels with no colour-bearing class in _QUAD_CSS: {uncoloured}")


def test_every_colour_this_stylesheet_sets_clears_the_contrast_floor():
    """`test_every_label_in_the_quad_is_legible` walks the LIVE widget tree
    and therefore only checks colours some label currently carries. This
    checks the stylesheet itself, so a class added here and not yet used --
    or used only in a state no test constructs, like the focused chip label
    -- cannot ship below the floor.

    Measured against the quad's ground (#092221):
      .quad-chip-label        #C7D9D8 -> 11.36:1
      .quad-chip-label-focus  #3299B9 ->  5.06:1
      .quad-caption           #F1F8F8 -> 15.46:1
      .quad-notice-text       #F1F8F8 -> 15.46:1
    """
    import re
    ground = quadmod._BACKGROUND_BY_CLASS["quad-cell"]
    colours = re.findall(r"(?<!-)color:\s*(#[0-9A-Fa-f]{6})", quadmod._QUAD_CSS)
    assert len(colours) >= 4, colours
    for colour in colours:
        ratio = contrast_ratio(colour, ground)
        assert ratio >= MIN_CONTRAST_RATIO, (colour, round(ratio, 2))


def test_both_background_tiers_are_registered():
    """`_legibility.nearest_explicit_background_hex` fails loudly if a
    label's nearest background-painting ancestor is not in the map -- but
    only for a label that EXISTS. This pins the map against the stylesheet
    directly, so a third tier added to the CSS is caught even before a label
    sits on it."""
    from _legibility import background_affecting_classes_from_css
    painted = background_affecting_classes_from_css(quadmod._QUAD_CSS)
    unregistered = painted - set(quadmod._BACKGROUND_BY_CLASS) - {"quad"}
    assert not unregistered, unregistered


def test_the_focus_ring_changes_only_colour_and_never_a_size():
    """A focus ring that added width would reflow all four cells every time
    the booth's attention moved -- a visible twitch, once per fold, forever.
    Every cell carries its border always; focus changes only the colour.

    Checked against the STYLESHEET, not by measuring the widget, and that is
    a finding rather than a preference: GTK4 resolves an unrooted widget's
    style once and does not re-resolve it when a CSS class is added later,
    so `cell.measure(...)` returns the SAME number before and after
    `set_focus` even when the focus rule is mutated to `border: 8px`.
    Measured directly: a plain `Gtk.Box` with `.a{border:2px}` measures 4,
    and still measures 4 after gaining a class whose rule says
    `border:20px`. A measure-based version of this test could not fail, so
    it is not the version that shipped.
    """
    import re
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}",
                       quadmod._QUAD_CSS.replace("/*", "\n/*"))
    focus_props = [props for selector, props in rules
                   if "quad-cell-focus" in selector]
    assert focus_props, "no .quad-cell-focus rule in the stylesheet at all"
    for props in focus_props:
        for declaration in props.split(";"):
            name = declaration.split(":")[0].strip()
            if not name:
                continue
            assert name.endswith("color"), (
                f".quad-cell-focus sets {name!r}, which is not a colour -- a "
                "focus marking may only recolour, never resize")


# ---------------------------------------------------------------------------
# Beyond the brief 6: nothing here may raise out of a GLib callback.
#
# An unhandled exception in a GLib callback silently freezes that source
# forever. Every public method takes wire-shaped input.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slot", [-1, 4, 99, None, "0", 1.5, True])
def test_no_slot_shaped_junk_raises_out_of_any_method(slot):
    quad = QuadView(cards=[0, 1, 2, 3])
    quad.set_caption(slot, "x")
    quad.set_focus(slot)
    assert quad.caption_text(slot) == "" or slot is True
    assert quad.has_focus_marking(slot) in (True, False)
    assert quad.viewer_for_slot(slot) is None or slot is True
    assert quad.chip_label_text(slot) in ("", "CHIP 1")


def test_a_quad_with_no_cards_at_all_is_still_a_widget():
    """`ui/app.py` builds the quad before `hello` arrives. A booth whose
    daemon never answers must render an empty quad, not crash."""
    quad = QuadView(cards=[])
    assert quad.slot_count == 0
    assert quad.viewers == ()
    quad.set_caption(0, "x")
    quad.set_focus(0)
    quad.set_connection_state("connected")
    quad.set_notice("waiting")
    assert quad.notice_text() == "waiting"


def test_the_quad_renders_exactly_the_text_it_is_given_and_invents_none():
    """Nothing in the UI may ever display a stack trace or raw error text.

    This class cannot police what `ui/app.py` hands it, but it can promise
    it never INVENTS any: what goes in comes out verbatim -- no `repr()`
    quoting (which is how a bare exception object becomes
    `ValueError('...')` on a booth screen), no formatting, no dropping of a
    falsy-but-real value.

    The first version of this test asserted only that `None` and `""` both
    clear, which the sweep showed could not fail against ANY of 36
    mutations -- it restated `test_a_cleared_notice_leaves_nothing_behind`
    in different words. This version has its own mutations.
    """
    quad = QuadView(cards=[0, 1, 2, 3])
    for text in ("HEMOGLOBIN — NEXT UP", "DIFFUSION 62%", "n/a"):
        quad.set_notice(text)
        assert quad.notice_text() == text
        quad.set_caption(2, text)
        assert quad.caption_text(2) == text

    # A falsy value that is nonetheless real text. `text or ""` would eat
    # these; `"" if text is None else str(text)` does not.
    quad.set_caption(2, 0)
    assert quad.caption_text(2) == "0"

    # Only None (and the empty string) clear.
    quad.set_notice(None)
    assert quad.notice_text() == ""
    quad.set_notice("")
    assert quad.notice_text() == ""
