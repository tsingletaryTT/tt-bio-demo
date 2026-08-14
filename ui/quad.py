"""The quad: four folds on screen at once, one cell per chip.

`ui/viewer.py` renders ONE structure and keeps doing so. Everything it
learned the hard way -- camera ownership (a finished ribbon keeps the camera
so the next fold's noise cannot rescale it), hold-until-superseded (the last
real structure stays up, dimmed, while a long fold spends ~15s in `trunk`
with nothing to draw), per-job reset -- is PER-CELL machinery already, held
in per-instance fields on `StructureViewer`. Reworking that file into a
multi-viewport renderer would put four folds' worth of that state back into
one object, which is the exact defect this phase exists to remove.

So the quad is four `StructureViewer`s in a `Gtk.Grid`, and `ui/viewer.py` is
not touched. Each cell gets its own GL context, its own camera, its own
hold flag, its own blend -- for free, by construction, rather than by a
dispatch table keyed on slot index that someone has to keep right.

FOUR GL CONTEXTS ON THIS STACK: verified, not assumed. A spike built exactly
this widget tree on the booth's own KWin/Wayland/radeonsi stack and fed each
cell a DIFFERENT point cloud (bar / helix / ring / zigzag): all four
`Gtk.GLArea`s realized, `get_error()` was None on all four, and all four
rendered their own geometry. One shared context with four viewports was the
fallback and was not needed.

Layout, and why the notice is not a fifth caption
-------------------------------------------------
Rows 0-1 are the 2x2 of cells (see `grid_position`). Row 2 is the NOTICE: one
line spanning the whole quad. It is what the booth says between a visitor's
tap and the fold that answers it, and at that moment NO CELL is folding the
thing it names -- so rendering it into cell 0's caption would label whatever
cell 0 actually IS folding with the wrong protein's name. It belongs to the
quad, so it lives on the quad.

The toggle key
--------------
`QUAD_KEYS` below is this view's decided key, and the copy for the `?` card
travels with it (`QUAD_HELP_LINE`) so the two cannot drift apart. Task 15
wired them into `ui/app.py`'s `_handle_key` and `_HELP_PANELS`; nothing in
this module reads a keyboard.

The quad is the OPTIONAL view, not the default one: the booth still comes up
showing one big protein. That is `set_solo_mode` below -- one widget tree,
with the cells that are not the focus hidden -- and `Q` is what turns the
other three on. See the comment above `set_solo_mode` for why the hero is a
cell of this grid rather than a fifth viewer of its own.
"""

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, Gtk, Pango

from ui.slots import MAX_SLOTS
from ui.viewer import StructureViewer

log = logging.getLogger(__name__)

# The grid is 2 wide. Not a bare `2` in `grid_position`: `MAX_SLOTS` (4) and
# this are the two numbers that decide the shape, and a quad that ever became
# a six-up would change this one, not four scattered literals.
_COLUMNS = 2

# The row the notice spans, immediately under the cells.
_NOTICE_ROW = MAX_SLOTS // _COLUMNS


# ── the toggle key ──────────────────────────────────────────────────────────
#
# `Q`, for quad. Taken already, and deliberately not reused: `?`/F1/Help (the
# help card), `D` (diagnostics), `T` (the Tensix panel), `Esc` (close
# whatever is open), and the three operator chords `Ctrl+F` / `Ctrl+G` /
# `Ctrl+Q`.
#
# A PLAIN LETTER, not a chord, and that is the deliberate part. ui/app.py's
# keyboard policy is that every unbound plain key is a visitor touch, so any
# letter bound here is a letter carved out of the visitor surface -- which is
# exactly the trade `D` and `T` already make for the two rail panels, for the
# same reason: this is booth chrome an operator or a curious visitor asks
# for, on the same footing as those two, and it should be reachable the same
# way. `Q` is the first letter of the thing it shows, matching `D` for
# diagnostics and `T` for Tensix.
#
# It cannot collide with `Ctrl+Q` (quit). ui/app.py's `_handle_key` tests
# every Ctrl chord FIRST and then swallows any remaining `ctrl` press
# outright, so a plain-letter branch is never reached with Ctrl held. The
# reverse near-miss -- an operator reaching for Ctrl+Q and missing the
# modifier -- toggles a view and is undone by pressing it again, rather than
# quitting the booth in front of a visitor.
#
# UNLIKE the easter egg, this IS on the `?` card. The egg is undocumented on
# purpose (an easter egg that is documented is a feature); the quad is the
# visible payoff of the whole multi-chip phase and a booth operator should
# not have to be told about it in person.
QUAD_KEYS = frozenset({"q"})

# The `?` card's line for it. Says what the key does and what the view shows,
# in the same register as the rest of the card: what it is, plainly, with no
# claim the booth cannot back up. Four cells means four chips REALLY folding
# four different proteins at the same time -- which is true only because
# Tasks 6-9 made it true, and is the whole point of saying it.
QUAD_HELP_LINE = (
    "Press Q for the quad view: all four Tenstorrent chips at once, one "
    "protein per chip, each folding on its own silicon. Press Q again for "
    "the single large view."
)

# The keys this module knows are already spoken for elsewhere in the booth,
# so `test_the_toggle_key_is_not_one_already_taken` is a real check against a
# real list rather than a restatement of `QUAD_KEYS`. Kept here rather than
# imported from ui/app.py on purpose: importing ui.app to learn this would
# make every test of this module drag in the whole application.
KEYS_ALREADY_TAKEN = frozenset({
    "question", "f1", "help",   # the ? card
    "d",                        # diagnostics panel
    "t",                        # Tensix activity panel
    "escape",                   # close whatever is open
    "f", "g",                   # only as Ctrl chords: fullscreen, the egg
})


# ---------------------------------------------------------------------------
# Palette and stylesheet.
#
# The same brand constants ui/panels.py and ui/gallery.py use, imported from
# panels rather than re-typed: a third hand-copied copy of `#092221` is a
# third place for the booth's ground to drift.
#
# `_BACKGROUND_BY_CLASS` is the single source of truth for "which CSS class
# carries an explicitly-set background", read by tests/unit/test_quad.py
# through the shared tests/unit/_legibility.py walker -- the identical role
# it plays for the other two modules. TWO tiers here, and both are real: a
# cell paints the same near-black teal the GL clear colour uses (so a cell
# with no structure yet is continuous with one that has), and the notice bar
# paints its own ground because it sits BELOW the cells, outside any of them,
# and a label with no backgrounded ancestor cannot be contrast-checked at
# all.
# ---------------------------------------------------------------------------

from ui.panels import (  # noqa: E402  (after gi.require_version, deliberately)
    _ACCENT, _ACCENT_TEXT, _BG, _BG_ALT, _DARK_BASE, _HAIRLINE)

_CSS_INSTALLED = False

_BACKGROUND_BY_CLASS = {
    "quad-cell": _DARK_BASE,
    "quad-notice": _DARK_BASE,
}

_QUAD_CSS = f"""
.quad {{
    background-color: {_DARK_BASE};
}}
.quad-cell {{
    background-color: {_BACKGROUND_BY_CLASS["quad-cell"]};
    /* A border on EVERY cell, always, in the hairline colour -- so marking
       one focused below changes only the border's COLOUR and never the
       cell's size. A focus ring that adds width would reflow all four
       cells every time the booth's attention moved. */
    border: 2px solid {_HAIRLINE};
    border-radius: 6px;
}}
.quad-cell.quad-cell-focus {{
    border-color: {_ACCENT};
}}
.quad-chip-label {{
    font-family: "Berkeley Mono", monospace;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: {_BG_ALT};
}}
/* The focused cell says which chip it is a little louder -- the one place
   colour is spent inside a cell, and it is spent on the same accent the
   focus ring uses so the two read as one statement. */
.quad-chip-label.quad-chip-label-focus {{
    color: {_ACCENT_TEXT};
}}
.quad-caption {{
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: {_BG};
}}
.quad-notice {{
    background-color: {_BACKGROUND_BY_CLASS["quad-notice"]};
    padding: 8px 14px;
    border-radius: 6px;
}}
.quad-notice-text {{
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: {_BG};
}}
"""


def _ensure_css_installed():
    """Install `_QUAD_CSS` against the default display, once.

    Guarded on a display existing at all, matching ui/panels.py and
    ui/gallery.py: constructing a `QuadView` must never hard-require one, so
    the widget tree can be built and walked in a test process that has no
    display without this raising.
    """
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        log.debug("no default display; skipping quad CSS install")
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_QUAD_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


# ---------------------------------------------------------------------------
# How wide a label is allowed to make itself.
#
# THE RAIL MUST NOT GROW, and the way it grew last time was not a minimum
# size: a WebView's NATURAL width pushed the hero slot from 1332px to 1300px,
# and the "never expands the rail" test missed it because it only checked the
# minimum. A Gtk.Label's natural width is the full width of its text unless
# ellipsizing is on AND `max-width-chars` is set -- both, not either -- so a
# caption naming a long protein, or a notice naming a long one, would widen
# the quad and take those pixels from somewhere.
#
# Bounded here, and pinned by tests that measure the NATURAL width (not the
# minimum) before and after setting a very long string.
# ---------------------------------------------------------------------------
_CAPTION_MAX_CHARS = 24
_NOTICE_MAX_CHARS = 52
_CHIP_LABEL_MAX_CHARS = 12


def grid_position(slot):
    """Where cell `slot` sits, as `(column, row)`.

    `0 -> (0, 0)`, `1 -> (1, 0)`, `2 -> (0, 1)`, `3 -> (1, 1)`: reading
    order, left to right and then down. Pure, and it must MATCH the
    telemetry panel's own left-to-right chip order -- if the two disagree,
    "chip 2" on screen means two different chips in two panels of the same
    booth.
    """
    return (slot % _COLUMNS, slot // _COLUMNS)


class _Cell:
    """One chip's cell: a viewer, the chip's name, and a caption.

    A plain holder, not a widget subclass -- the widget IS `self.frame`. It
    exists so `QuadView` below reads as "ask cell N" rather than as four
    parallel lists indexed in lockstep, which is the shape an off-by-one
    hides in.
    """

    def __init__(self, card):
        self.card = card

        self.viewer = StructureViewer()
        self.viewer.set_hexpand(True)
        self.viewer.set_vexpand(True)

        # halign FILL + xalign 0, NOT halign START, and that is a fix rather
        # than a preference: an ellipsizing label given `halign START` is
        # allocated its NATURAL width, which `max-width-chars` caps -- and
        # `max-width-chars` is an average-character estimate, so a line of
        # capitals is cut well before the count suggests. Looking at the
        # real thing is what caught it: cell labels rendered "CHIP …" and
        # captions were cut mid-word ("TRYPSIN — 2…") with two thirds of the
        # cell empty beside them. FILL hands the label the cell's width and
        # `xalign 0` keeps the text left; ellipsizing then happens at the
        # cell's actual edge, which is the only place it should.
        self.chip_label = Gtk.Label(label=f"CHIP {card}")
        self.chip_label.add_css_class("quad-chip-label")
        self.chip_label.set_halign(Gtk.Align.FILL)
        self.chip_label.set_xalign(0.0)
        self.chip_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.chip_label.set_max_width_chars(_CHIP_LABEL_MAX_CHARS)

        self.caption = Gtk.Label(label="")
        self.caption.add_css_class("quad-caption")
        self.caption.set_halign(Gtk.Align.FILL)
        self.caption.set_xalign(0.0)
        self.caption.set_ellipsize(Pango.EllipsizeMode.END)
        self.caption.set_max_width_chars(_CAPTION_MAX_CHARS)

        # The two labels float OVER the GL area rather than stealing rows
        # from it: a cell is already a quarter of the hero image, and giving
        # up two text rows of it per cell would cost more of the protein than
        # the labels are worth.
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.append(self.chip_label)
        text.append(self.caption)
        # FILL horizontally so the two labels above really do get the cell's
        # width to ellipsize against; END vertically so they sit along the
        # bottom edge rather than over the middle of the protein.
        text.set_halign(Gtk.Align.FILL)
        text.set_valign(Gtk.Align.END)
        text.set_margin_start(10)
        text.set_margin_end(10)
        text.set_margin_bottom(8)
        # An overlay child does not participate in the overlay's size
        # request, but the BOX still reports one; keep it from being the
        # thing that decides how wide a cell is.
        text.set_can_target(False)

        self.frame = Gtk.Overlay()
        self.frame.add_css_class("quad-cell")
        self.frame.set_hexpand(True)
        self.frame.set_vexpand(True)
        self.frame.set_child(self.viewer)
        self.frame.add_overlay(text)

    def set_focus_marking(self, focused):
        """Mark (or unmark) this cell as the one the booth is following."""
        for widget, css_class in ((self.frame, "quad-cell-focus"),
                                  (self.chip_label, "quad-chip-label-focus")):
            if focused:
                widget.add_css_class(css_class)
            else:
                widget.remove_css_class(css_class)

    @property
    def has_focus_marking(self):
        return "quad-cell-focus" in self.frame.get_css_classes()


class QuadView(Gtk.Grid):
    """Four chips' folds, in a 2x2 grid, with one notice line under them.

    Built from a CARD LIST, never from a count: cells are labelled with the
    chip number they actually show, so a booth started on cards `[1, 3]`
    says CHIP 1 and CHIP 3 rather than CHIP 0 and CHIP 1. A booth with more
    chips than cells folds on all of them and shows the first four
    (`MAX_SLOTS`), which is the same rule `ui.slots.SlotRouter` applies to
    the state it keeps -- the two are built from the same list, in the same
    order, so slot N here and slot N there are the same chip.
    """

    def __init__(self, cards):
        super().__init__()
        _ensure_css_installed()
        self.add_css_class("quad")
        self.set_row_homogeneous(False)
        self.set_column_homogeneous(True)
        self.set_row_spacing(8)
        self.set_column_spacing(8)

        self.cards = list(cards)[:MAX_SLOTS]
        self._cells = []
        for slot, card in enumerate(self.cards):
            cell = _Cell(card)
            column, row = grid_position(slot)
            self.attach(cell.frame, column, row, 1, 1)
            self._cells.append(cell)

        self._focus_slot = None

        # Solo mode: the booth's DEFAULT, and what `Q` toggles off. See
        # `set_solo_mode`.
        self._solo_mode = True
        self._apply_solo()

        # The notice: one line, spanning the whole quad, under the cells.
        # Hidden rather than emptied when there is nothing to say, so it
        # takes no vertical space at all and the cells do not resize the
        # moment a visitor taps.
        self._notice_label = Gtk.Label(label="")
        self._notice_label.add_css_class("quad-notice-text")
        # Same FILL-and-align fix as the cell labels, centred rather than
        # left: with `halign CENTER` the notice was cut at roughly 39
        # capitals despite a 52-character cap, for the reason spelled out on
        # `_Cell.__init__`. `max-width-chars` still bounds the NATURAL width
        # -- which is the number that could widen the quad -- so the guard
        # this file is under is unaffected.
        self._notice_label.set_halign(Gtk.Align.FILL)
        self._notice_label.set_xalign(0.5)
        self._notice_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._notice_label.set_max_width_chars(_NOTICE_MAX_CHARS)
        self._notice_label.set_hexpand(True)
        self._notice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._notice.add_css_class("quad-notice")
        self._notice.set_halign(Gtk.Align.FILL)
        self._notice.append(self._notice_label)
        self._notice.set_visible(False)
        self.attach(self._notice, 0, _NOTICE_ROW, _COLUMNS, 1)

    # ── shape ────────────────────────────────────────────────────────────

    @property
    def slot_count(self):
        """How many cells this quad actually built."""
        return len(self._cells)

    @property
    def viewers(self):
        """Every cell's `StructureViewer`, in slot order.

        A fresh tuple each time rather than a stored list: nothing outside
        should be able to append a fifth viewer to the quad's own bookkeeping
        and have `slot_count` disagree with it.
        """
        return tuple(cell.viewer for cell in self._cells)

    def viewer_for_slot(self, slot):
        """The viewer for cell `slot`, or None if there is no such cell."""
        cell = self._cell(slot)
        return None if cell is None else cell.viewer

    def _cell(self, slot):
        """Cell `slot`, or None -- the ONE bounds check in this class.

        Every public method that takes a slot goes through here. Slot
        indices reach this class from wire-shaped data (a `card` field the
        router turned into an index), so an out-of-range one is ordinary
        input, not a programming error, and it must not raise out of a GLib
        callback: an unhandled exception in one silently freezes that source
        forever, which at a booth reads as "the screen stopped updating" with
        nothing on screen saying why.
        """
        if not isinstance(slot, int) or isinstance(slot, bool):
            return None
        if 0 <= slot < len(self._cells):
            return self._cells[slot]
        return None

    def chip_label_text(self, slot):
        """What cell `slot` says its chip is, e.g. `"CHIP 2"`."""
        cell = self._cell(slot)
        return "" if cell is None else cell.chip_label.get_label()

    # ── per-cell state ───────────────────────────────────────────────────

    def set_caption(self, slot, text):
        """Set cell `slot`'s caption. Out-of-range slots are ignored."""
        cell = self._cell(slot)
        if cell is None:
            log.debug("caption for slot %r ignored: no such cell", slot)
            return
        cell.caption.set_label("" if text is None else str(text))

    def caption_text(self, slot):
        cell = self._cell(slot)
        return "" if cell is None else cell.caption.get_label()

    def set_focus(self, slot):
        """Mark exactly one cell as the one the booth is following, or none.

        Clears the previous marking FIRST and unconditionally, so focus
        moving from cell 2 to cell 0 leaves cell 2 unmarked rather than
        lighting two cells at once. `None` (and any out-of-range slot) marks
        nothing.
        """
        for cell in self._cells:
            cell.set_focus_marking(False)
        cell = None if slot is None else self._cell(slot)
        if cell is not None:
            cell.set_focus_marking(True)
            self._focus_slot = slot
        else:
            self._focus_slot = None
        # In solo mode the focused cell is the ONE cell on screen, so moving
        # the focus moves the hero image. Re-applied from here rather than
        # left to the caller: two call sites that must both remember is one
        # call site that will not.
        self._apply_solo()

    # ── solo (hero) mode ─────────────────────────────────────────────────
    #
    # The quad is OPTIONAL, by request: the booth comes up showing one big
    # protein, exactly as it always has, and `Q` (ui/app.py's `QUAD_KEYS`)
    # opens all four. That is one widget tree either way -- solo mode simply
    # hides the cells that are not the focus, and the surviving cell expands
    # to fill the grid.
    #
    # Hiding rather than building a second, separate hero viewer is the whole
    # point. A fifth `StructureViewer` fed from "whichever cell has the
    # focus" would need its own camera, its own hold flag and its own copy of
    # each fold's last real structure -- four folds' worth of per-cell state
    # back in one object, which is the exact defect this module's docstring
    # opens by refusing. Here the hero IS cell N: same viewer, same camera,
    # same held structure, so moving the focus cuts to a cell that is already
    # showing its own fold rather than to an empty one.

    def set_solo_mode(self, solo):
        """Show only the focused cell (True) or all of them (False)."""
        self._solo_mode = bool(solo)
        self._apply_solo()

    @property
    def solo_mode(self):
        return self._solo_mode

    def _apply_solo(self):
        """Reconcile cell visibility with the mode and the focus.

        With no focus marked, solo mode falls back to cell 0 rather than
        hiding every cell: an unfocused booth must still show a protein.
        """
        if not self._solo_mode:
            for cell in self._cells:
                cell.frame.set_visible(True)
            return
        hero = self._focus_slot if self._cell(self._focus_slot) is not None else 0
        for slot, cell in enumerate(self._cells):
            cell.frame.set_visible(slot == hero)

    @property
    def visible_slots(self):
        """Which cells are actually on screen, in slot order.

        Read back off the widgets rather than derived from the mode, for the
        same reason `focus_slot` is: the two must not be able to disagree.
        """
        return tuple(slot for slot, cell in enumerate(self._cells)
                     if cell.frame.get_visible())

    def has_focus_marking(self, slot):
        cell = self._cell(slot)
        return False if cell is None else cell.has_focus_marking

    @property
    def focus_slot(self):
        """Which cell is marked, or None. Read back from the marking itself
        rather than from a remembered index, so the two cannot disagree."""
        for slot, cell in enumerate(self._cells):
            if cell.has_focus_marking:
                return slot
        return None

    # ── the notice ───────────────────────────────────────────────────────

    def set_notice(self, text):
        """Say one thing across the whole quad, or (with None) stop saying it.

        NOT a fifth caption. It is what the booth says between a visitor's
        tap and the fold that answers it, and at that moment no cell is
        folding the thing it names -- so it belongs to no cell. Clearing it
        hides the row entirely: a banner still saying NEXT UP over the fold
        it announced is the booth talking over itself.
        """
        text = "" if text is None else str(text)
        self._notice_label.set_label(text)
        self._notice.set_visible(bool(text))

    def notice_text(self):
        return self._notice_label.get_label()

    # ── booth-wide state ─────────────────────────────────────────────────

    def set_connection_state(self, state):
        """Tell every cell what the socket is doing.

        EVERY cell, not the focused one: connection state is a fact about
        the booth, and a quad where one cell knows the daemon is gone and
        three do not is worse than one where none of them do.

        `StructureViewer.connection_state`'s setter deliberately RAISES on an
        unknown state -- it is the guard that catches a typo where it is set
        rather than three modules later, and it should keep doing that. But
        this method is on the path from the socket to four widgets, and a
        validator that can take down the channel for four cells at once is a
        different thing from one that catches a typo. So the raise is
        contained here, per cell, exactly as ui/app.py already contains it
        for the single viewer: log it, and leave every cell's last known
        state alone rather than half-applying a bad one.
        """
        rejected = []
        for cell in self._cells:
            try:
                cell.viewer.connection_state = state
            except ValueError:
                rejected.append(cell.card)
        if rejected:
            log.warning(
                "ignoring unknown connection state %r for chip(s) %s; their "
                "last known state is unchanged", state, rejected)
