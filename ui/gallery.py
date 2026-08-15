"""The booth's gallery: what this booth folds, and what tapping one does.

WHAT A TAP ACTUALLY DOES -- read this before editing any copy in this file.
A tap calls `on_pick(target_id)`, which ui/app.py hands to the state machine
(closing this screen and putting the live folds back up), to the slot router
(which is what moves the focus to the pick and what the booth says about it),
and to the daemon as a `pick` message. The daemon takes it at the head of its
priority queue and dispatches it to the next chip to come free.

What a tap does NOT do, and what the copy here must therefore not promise:
it does not interrupt anything. The folds already running are left to finish
-- tearing one down mid-device-operation is a documented instability source,
and pre-empting would blank a cell somebody is watching -- so a pick starts
in seconds rather than instantly, bounded by the earliest-finishing of four
folds. "Next", never "now". That wait is also exactly why the other three
cells keep moving, which is worth saying rather than apologising for.

This copy has been wrong in both directions and the history is the reason it
is written out here. It first over-promised ("TAP TO FOLD", a `Fold {name}`
tooltip) back when the socket carried nothing but the daemon's own
broadcasts, which the whole-branch review called out as the booth promising a
capability it did not have; it was then walked back to a catalogue with a
plainly-stated note that choosing one was not wired up. Task 3 gave the
protocol a client->server message, Task 9 gave the daemon the dispatch, and
Task 17 connected the tap to both -- so the walked-back copy became the
opposite lie and changed back in that same commit. The rule that survives
both mistakes: this copy and `ui/app.py`'s `_on_pick` move together, in one
commit, in whichever direction.

Two things, kept deliberately separate per the task brief ("keep the pure
part small and real: grid_shape decides layout and is tested; the widget
assembles"):

- `grid_shape(n_targets, width_px) -> (columns, rows)` -- a pure function
  over two integers, tested directly in tests/unit/test_gallery.py.
- `Gallery` -- a thin GTK widget built from a `list[ui.playlist.Target]`
  that lays cards out on a `Gtk.Grid` sized by `grid_shape`, and calls an
  `on_pick(target_id)` callback when a visitor taps one.

This module is UI-side: it must never import torch or tt_bio (see
docs/venv-bootstrap-notes.md and ui/playlist.py's own docstring for why --
ui/ and runner/ are different venvs, and tests/unit/'s split by directory,
not marker, depends on every module here staying importable under venv-ui
alone).

Base class note (mirrors ui/panels.py's own): the brief's produces line
types this as `Gallery(Gtk.Widget)`. Implemented here as a
`Gtk.ScrolledWindow` subclass instead -- `Gtk.ScrolledWindow` IS a
`Gtk.Widget` (isinstance(gallery, Gtk.Widget) holds), but a *direct*
Gtk.Widget subclass has no layout manager of its own and must implement
`do_measure`/`do_size_allocate` before it can arrange a single child --
real work with no behavioral payoff over a concrete GTK type that already
does what this widget needs. `Gtk.ScrolledWindow` specifically (rather than
`Gtk.Box`, panels.py's choice) because a gallery has no natural upper bound
on target count -- `grid_shape`'s own "no target is ever dropped" guarantee
(tests/unit/test_gallery.py) is about the grid never truncating a target
out of existence, not about every row fitting on screen unscrolled -- so
the grid needs to be able to grow taller than the window and still let a
visitor reach the bottom row.

Missing thumbnails: every target shipped so far (playlist/manifest.yaml)
has none -- Phase 4 owns the art -- so a missing/unreadable thumbnail is
the ORDINARY case here, not a rare edge, and must render a deliberate
placeholder, never GTK's own "broken image" icon and never an uncaught
exception. See `_load_thumbnail_texture` and `_build_thumbnail`.

Design note (redesign after the first render was reviewed on real glass):
the first cut of the placeholder was a large pale `_BG_ALT` block with a
giant dark initial -- since it is what every visitor sees on every card
today, this read as the same "kids app" problem ui/panels.py's own first
draft had (see that module's design-redirect note), and fought the dark,
restrained panel language sitting right next to it. The placeholder is
now dark-ground with only a hairline border and a small, muted initial --
quiet enough that the target's NAME and BLURB (what a visitor actually
reads to choose) are the loudest things on the card, not the empty tile.

Fold-time hint: each card also shows a short pacing note built by the pure
`_format_fold_time(target.expected_s)` -- a real, hardware-measured time
("~4.4s to fold") when `ui.playlist.Target.expected_s` is set, or an
explicit "not yet timed" sentence when it is `None` (a target nobody has
folded on this booth's real hardware yet -- see ui/playlist.py's own
docstring for why that is `None` rather than a guessed number). The
unmeasured case gets its own wording rather than blank space specifically
so it reads as a deliberate, known state, not a missing field.

Legibility guard: per this task's brief, ui/panels.py's generalized
"every label must carry an explicitly-coloured CSS class, checked both
statically against the real stylesheet text and dynamically via runtime
contrast" guard is extended to cover this module too, not left as a
one-off for panels alone -- see tests/unit/_legibility.py (the shared,
CSS-agnostic implementation factored out of tests/unit/test_panels.py) and
tests/unit/test_gallery.py's own use of it against `_GALLERY_CSS` /
`_BACKGROUND_BY_CLASS` below.
"""

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

# Reused, not reimplemented: the WCAG contrast math is delicate (see
# ui/panels.py's own docstring on relative_luminance/contrast_ratio) and
# this module needs the identical formula and identical AA floor, not a
# second copy that could silently drift from it. This is pure logic with no
# panel-specific meaning, unlike the brand hex literals below, which this
# codebase's convention (see ui/panels.py, ui/app.py) is to duplicate
# per-module as presentation data rather than import.
from ui.panels import MIN_CONTRAST_RATIO, contrast_ratio, relative_luminance  # noqa: F401  (re-exported for tests/unit/test_gallery.py)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand palette -- duplicated locally per this codebase's convention (each
# visual module owns its own copy of the hex literals; see ui/panels.py and
# ui/app.py). Values must match ui/panels.py's exactly -- the task brief's
# instruction is to "match Task 5's visual language," not invent a second
# one -- so the gallery reads as part of the same product, not a different
# app bolted on next to it.
# ---------------------------------------------------------------------------
_DARK_BASE = "#092221"
_BG = "#F1F8F8"
_BG_ALT = "#C7D9D8"
# ui/panels.py's own lightened accent tint (that module's comment explains
# the derivation: pure #1B8EB1 measures 4.40:1 on _DARK_BASE, just under the
# 4.5:1 AA floor, so a +10%-toward-white tint is used for TEXT instead).
# Duplicated here for the identical reason: this module's own per-card hint
# line ("IN THE ROTATION" -- see `_CARD_HINT`) sits on the same dark ground
# and needs the same fix.
_ACCENT_TEXT = "#3299B9"
_HAIRLINE = "rgba(199, 217, 216, 0.18)"  # _BG_ALT at 18% opacity, matches ui/panels.py


# ---------------------------------------------------------------------------
# Pure decision: how to show a target's fold time (or the lack of one).
# ---------------------------------------------------------------------------

def _format_fold_time(expected_s, expected_slow_s=None):
    """The short fold-time hint shown on a gallery card.

    `expected_s` is `None` for a target nobody has folded on this booth's
    real hardware yet (`ui.playlist.Target.expected_s`, and see that
    module's docstring for why it is `None` rather than a guessed number).
    That case gets its OWN explicit sentence here -- "not yet timed" -- so
    it reads as a deliberate, known state to a visitor or operator glancing
    at the card, not as a blank space that looks like a missing field, and
    absolutely not as some fabricated number formatted to look measured.
    A real, measured value is rounded to one decimal place: sub-second
    precision on a multi-second fold reads as more precise than this
    project's own measurement methodology (docs/followups.md's 30-fold
    soak) actually supports.

    A RANGE when `expected_slow_s` is set, because on this box a fold's
    duration depends on which chip it lands on and a visitor cannot choose.
    Two of the four chips throttle to 906 MHz about fifteen minutes into a
    session (chassis position, not workload -- Task 19's soak), which costs
    the long targets 4-26%. A single number was measured honestly and was
    still optimistic by up to a quarter for half the box; a range is true
    whichever chip takes the pick, and is a promise a visitor can check
    against the clock rather than one they watch the booth miss.

    The range drops the decimal where whole seconds still tell the two ends
    apart: "20-25s" is what the measurement supports, and "19.8-25.0s"
    claims a precision that survives neither chip-to-chip variation nor the
    throttle ramp. Where they do NOT -- FKBP12's 11.7 and 12.3 both round to
    12 -- the decimal stays, because "12-12s" is not a range at all. That
    was the first version of this rule, and it read as a rendering fault.
    """
    if expected_s is None:
        return "fold time: not yet timed"
    if expected_slow_s is None or expected_slow_s <= expected_s:
        return f"~{expected_s:.1f}s to fold"
    if expected_s >= 10.0 and round(expected_slow_s) != round(expected_s):
        return f"{expected_s:.0f}-{expected_slow_s:.0f}s to fold"
    return f"{expected_s:.1f}-{expected_slow_s:.1f}s to fold"


# ---------------------------------------------------------------------------
# Pure decision: grid layout.
# ---------------------------------------------------------------------------

# Minimum comfortable width, in px, for one touch-target card at this booth.
# Large enough to read a thumbnail + name + a couple lines of blurb from
# arm's length and to tap reliably on a public kiosk touchscreen -- well
# above the ~44px "minimum tap target" guidance aimed at finger-precise
# phone UIs; a booth invites a less careful, less precise tap than a phone
# does. Not a magic number scattered through the widget code below: both
# `grid_shape` and `Gallery`'s default `width_px` (matching ui/app.py's
# window default size, 1280x800) are built around this one constant.
_MIN_CELL_WIDTH_PX = 400


def grid_shape(n_targets, width_px):
    """How many `(columns, rows)` a gallery of `n_targets` cards should use
    at `width_px` wide.

    Columns are decided FIRST, from the available width alone (via
    `_MIN_CELL_WIDTH_PX`) -- a narrower screen gets fewer columns, a wider
    one gets more -- then capped at `n_targets` itself so a single target
    never gets spread across empty columns it doesn't need. Rows are
    whatever is needed to fit every target at that column count, computed
    with CEILING division (`-(-a // b)`, the classic ceiling-via-negation
    idiom for two positive ints): ordinary floor division would silently
    truncate -- e.g. 7 targets at 3 columns needs 3 rows (9 cells, 2
    empty), but floor division gives `7 // 3 == 2` rows (6 cells), dropping
    the 7th target off the bottom of the grid with no error anywhere.
    "Fill columns before adding rows" is exactly this ordering: decide the
    widest layout the screen can support first, and only grow downward
    (never sideways past that) once every column is full.

    `n_targets <= 0` is a special case, not a fall-through left to the
    arithmetic above: with zero targets, `min(cols_by_width, 0)` is `0`,
    and `-(-0 // 0)` is a ZeroDivisionError -- there is no "safe" column
    count to divide by when there is nothing to lay out, so this returns
    `(0, 0)` directly rather than trusting the general-case formula to
    degrade gracefully on its own.
    """
    if n_targets <= 0:
        return (0, 0)
    cols_by_width = max(1, width_px // _MIN_CELL_WIDTH_PX)
    cols = min(cols_by_width, n_targets)
    rows = -(-n_targets // cols)
    return (cols, rows)


# ---------------------------------------------------------------------------
# Thumbnails: real image if it loads, a deliberate placeholder if not.
# ---------------------------------------------------------------------------

_THUMBNAIL_WIDTH_PX = 320
_THUMBNAIL_HEIGHT_PX = 200


def _load_thumbnail_texture(path):
    """Load `path` as a `Gdk.Texture`, scaled to the card's thumbnail area,
    or `None` on ANY failure -- a missing file, a corrupt file, or a path
    that simply isn't a valid image. GdkPixbuf raises `GLib.Error` (not a
    built-in Python exception type) for both "file does not exist" and
    "file exists but is not decodable as an image" -- verified directly
    against both cases before writing this guard (see this task's report),
    so both are caught here identically and neither one is allowed to
    surface as a raised exception or as GTK's own default "broken image"
    icon.

    Catches only `GLib.Error`, deliberately, not a bare `Exception`:
    anything else escaping this call is a bug worth seeing fail loudly,
    not something a thumbnail-loading helper should also be silencing.
    """
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(path), _THUMBNAIL_WIDTH_PX, _THUMBNAIL_HEIGHT_PX, False)
    except GLib.Error as exc:
        log.warning("gallery: could not load thumbnail %s: %s", path, exc)
        return None
    return Gdk.Texture.new_for_pixbuf(pixbuf)


def _placeholder_glyph(name):
    """The single uppercase initial shown on a placeholder thumbnail --
    e.g. "T" for "Trp-cage". Falls back to "?" for a blank name: it should
    never actually be blank (`ui.playlist.load_playlist` rejects an empty
    `name` at load time), but a display helper still must not crash on a
    hypothetically-blank string handed to it directly (as a unit test
    might)."""
    stripped = name.strip()
    return stripped[0].upper() if stripped else "?"


def _build_thumbnail(target):
    """One thumbnail widget for `target`'s card.

    Its real image if `target.thumbnail` names a file that actually loads;
    otherwise a placeholder that looks deliberate -- a hairline-bordered
    tile on the SAME dark ground as the rest of the card, holding only a
    small, muted initial -- rather than a broken-image icon, blank space,
    or (the first-cut design this replaced) a large pale block that
    outshone the target's own name and blurb. Every shipped target lacks a
    thumbnail today (Phase 4 owns the art), so this is the normal case a
    visitor sees on every card, not a rare failure mode that can afford to
    look loud.
    """
    texture = None
    if target.thumbnail is not None:
        texture = _load_thumbnail_texture(target.thumbnail)

    if texture is not None:
        picture = Gtk.Picture.new_for_paintable(texture)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(_THUMBNAIL_WIDTH_PX, _THUMBNAIL_HEIGHT_PX)
        picture.add_css_class("gallery-thumbnail")
        picture.set_can_shrink(True)
        return picture

    placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    placeholder.add_css_class("gallery-thumbnail-placeholder")
    placeholder.set_size_request(_THUMBNAIL_WIDTH_PX, _THUMBNAIL_HEIGHT_PX)
    placeholder.set_halign(Gtk.Align.FILL)
    placeholder.set_valign(Gtk.Align.FILL)
    # Layout bug found by rendering and measuring actual pixel rows (see
    # this task's report): GtkWidget's default "compute my own expand from
    # my children" behavior means the vexpand=True set on `glyph` below
    # (there only to CENTER it within this box's own fixed height) would
    # otherwise propagate upward and make THIS BOX report vexpand=True too
    # -- which let the tile stretch to help absorb a taller sibling card's
    # extra row height, instead of staying pinned at exactly
    # _THUMBNAIL_HEIGHT_PX. A shorter blurb (less content elsewhere in the
    # card) meant MORE spare row height landed here, so tiles measured
    # visibly different heights across a row of differently-sized blurbs
    # (confirmed directly: 278px / 303px / 317px for three real cards
    # sharing one grid row). Setting vexpand explicitly here overrides that
    # propagation -- see Gtk.Widget.set_vexpand -- so the tile's own size
    # never depends on what else is in its row.
    placeholder.set_vexpand(False)

    glyph = Gtk.Label(label=_placeholder_glyph(target.name))
    glyph.add_css_class("gallery-thumbnail-placeholder-glyph")
    glyph.set_halign(Gtk.Align.CENTER)
    glyph.set_valign(Gtk.Align.CENTER)
    glyph.set_vexpand(True)  # centers the glyph WITHIN the placeholder's fixed height; see placeholder.set_vexpand(False) above for why this doesn't leak outward

    placeholder.append(glyph)
    return placeholder


# ---------------------------------------------------------------------------
# CSS, installed once against the default display -- same pattern as
# ui/panels.py (guarded on a live display existing at all, so constructing
# a Gallery never hard-requires one; see that module's comment).
#
# `_BACKGROUND_BY_CLASS` is the single source of truth for "which CSS class
# carries an explicitly-set background," read by tests/unit/test_gallery.py
# via the shared tests/unit/_legibility.py walker -- exactly the role
# ui/panels.py's own dict plays for its tests. ONE tier here, like
# panels.py's own redesign ("everything in this module now sits on one
# background tier -- no nested card/chip backgrounds"): the placeholder
# thumbnail no longer has a background of its own (see _GALLERY_CSS below
# -- it is a hairline-bordered cutout on the SAME dark ground, not a
# second, lighter surface), so every label in this module is checked
# against the identical `_DARK_BASE` ground, the gallery root's.
# ---------------------------------------------------------------------------
_CSS_INSTALLED = False

_BACKGROUND_BY_CLASS = {
    "gallery": _DARK_BASE,
}

_GALLERY_CSS = f"""
.gallery {{
    background-color: {_BACKGROUND_BY_CLASS["gallery"]};
}}
.gallery-card {{
    padding: 14px;
    border-radius: 8px;
    border: 1px solid {_HAIRLINE};
}}
.gallery-card:hover {{
    background-color: rgba(199, 217, 216, 0.08);
}}
.gallery-card-name {{
    font-size: 19px;
    font-weight: 700;
    color: {_BG};
}}
.gallery-card-blurb {{
    font-size: 13px;
    color: {_BG_ALT};
}}
/* Same muted secondary-text colour as the blurb, smaller and set apart --
   pacing info a visitor may glance at, not the reason they picked the
   card. See _format_fold_time for what this shows when a target has no
   measured time yet. */
.gallery-card-time {{
    font-size: 11px;
    color: {_BG_ALT};
}}
.gallery-card-tap-hint {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: {_ACCENT_TEXT};
}}
/* The one line that says what this screen IS. Same register as the side
   rail's own title/subtitle pair (ui/app.py's `.booth-title`/`.booth-sub`)
   so the gallery reads as the same product, not a different screen. */
.gallery-caption-title {{
    font-size: 19px;
    font-weight: 700;
    color: {_BG};
}}
.gallery-caption {{
    font-size: 13px;
    color: {_BG_ALT};
}}
/* The placeholder tile: NO fill of its own -- just a hairline border on
   the card's own dark ground, so an empty tile reads as a deliberate,
   quiet cutout rather than a competing light surface. See the design note
   above _build_thumbnail's docstring for what this replaced. */
.gallery-thumbnail-placeholder {{
    border: 1px solid {_HAIRLINE};
    border-radius: 6px;
}}
/* A quiet mark, not a billboard: small, muted secondary-text colour --
   the SAME `_BG_ALT` the blurb uses, not a saturated or "loud" hue --
   so the target's name (above, in `_BG`) stays the visually loudest
   thing on the card, exactly like ui/panels.py reserves its brightest
   treatment for the hero number, not a decorative icon. */
.gallery-thumbnail-placeholder-glyph {{
    font-family: "Berkeley Mono", monospace;
    font-size: 22px;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: {_BG_ALT};
}}
"""


def _ensure_css_installed():
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        log.debug("no default display; skipping gallery CSS install")
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_GALLERY_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


# ---------------------------------------------------------------------------
# Visitor-facing copy for the screen as a whole.
#
# True as written, and checked against what the code actually does -- see
# this module's docstring. Three claims, each one the booth can back up:
# four chips fold at once (Phase 5, measured); a tap puts that protein next
# (Task 17, `ui/app.py`'s `_on_pick` -> `EventClient.send_pick`); and the
# folds already running are left to finish, which is why the wait exists and
# why the other cells keep moving.
#
# What is deliberately absent: "instantly", "now", "straight away". The pick
# starts on the next chip to come free, usually within seconds, and the one
# visitor who times it is the one who would catch the booth in the claim.
# ---------------------------------------------------------------------------
_CAPTION_TITLE = "What this booth folds"
_CAPTION_BODY = (
    "Four of these are folding at once, one on each Tenstorrent chip a few "
    "feet away. Tap any of them to put it next: it starts on the chip that "
    "finishes first, because the folds already running are left to finish "
    "rather than interrupted."
)

# The per-card line, in the same place the old "TAP TO FOLD" sat.
#
# It has been all three things this feature has been. "TAP TO FOLD" promised
# a capability the booth did not have; "IN THE ROTATION" was the walk-back to
# a plain statement about the target; this is the instruction again, now that
# a tap really does put that protein next -- and worded as "next" rather than
# "now" for the reason above.
_CARD_HINT = "TAP TO FOLD NEXT"


# ---------------------------------------------------------------------------
# The widget: thin assembly over grid_shape and _build_thumbnail.
# ---------------------------------------------------------------------------

class Gallery(Gtk.ScrolledWindow):
    """A grid of touch-tappable cards, one per `ui.playlist.Target`.

    Construction takes the target list, an optional `on_pick(target_id)`
    callback (called with the tapped card's `Target.id`, matching
    `ui.states.StateMachine.on_pick`'s own parameter -- Task 9's wiring
    layer is expected to pass `state_machine.on_pick` straight through,
    though this module never imports or touches ui.states itself), and
    `width_px` (defaults to ui/app.py's window default width, 1280) used
    once, at construction, to decide the column count via `grid_shape`.

    A plain callback, not a custom `GObject.Signal` -- matching this
    codebase's existing convention for cross-component notification
    (`ui.client.EventClient`'s `on_event`/`on_state_change` constructor
    callbacks, both driven the same way) rather than introducing a second,
    GTK-signal-based idiom this project has not otherwise used. "The
    gallery's job is to produce that target_id; it does not drive the
    machine itself" (this task's brief) -- calling `on_pick` is the whole
    of that job; what happens after is entirely Task 9's concern.
    """

    def __init__(self, targets, on_pick=None, width_px=1280):
        super().__init__()
        _ensure_css_installed()
        self.add_css_class("gallery")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.targets = list(targets)
        self.on_pick = on_pick
        self.width_px = width_px

        self._grid = Gtk.Grid()
        self._grid.set_column_homogeneous(True)
        self._grid.set_row_spacing(16)
        self._grid.set_column_spacing(16)
        self._grid.set_margin_top(16)
        self._grid.set_margin_bottom(16)
        self._grid.set_margin_start(16)
        self._grid.set_margin_end(16)

        # Caption above the grid, inside the same scrolled viewport, so a
        # visitor who has just walked up learns what this screen is before
        # reading any card -- and so the disclosure about picking cannot be
        # scrolled away from the cards it qualifies. `self._grid` stays the
        # cards' parent (tests address it directly, and the grid is what
        # `grid_shape` sizes); this box only stacks the two.
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        column.append(self._build_caption())
        column.append(self._grid)
        self.set_child(column)

        # target.id -> the Gtk.Button card built for it. Not read by GTK
        # itself, but it lets a test tap a specific card by id, and lets
        # diagnostics ask "how many cards actually got built" without
        # walking the grid's own child list -- same rationale as
        # TelemetryPanel.last_status / PipelinePanel.last_rows in
        # ui/panels.py.
        self.cards = {}

        self._build_cards()

    def _build_caption(self):
        """The screen's own two lines: what it is, and what tapping does.

        Wrapped and width-capped rather than allowed to run the full width
        of a 1490px hero slot: a single line of prose that long is not read
        at a booth. See `_CAPTION_BODY` for why the copy says what it says.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        title = Gtk.Label(label=_CAPTION_TITLE, xalign=0.0)
        title.add_css_class("gallery-caption-title")
        title.set_wrap(True)

        body = Gtk.Label(label=_CAPTION_BODY, xalign=0.0)
        body.add_css_class("gallery-caption")
        body.set_wrap(True)
        body.set_max_width_chars(84)

        box.append(title)
        box.append(body)
        return box

    def _build_cards(self):
        cols, _rows = grid_shape(len(self.targets), self.width_px)
        for index, target in enumerate(self.targets):
            card = self._build_card(target)
            self.cards[target.id] = card
            row, col = divmod(index, cols)
            self._grid.attach(card, col, row, 1, 1)

    def _build_card(self, target):
        button = Gtk.Button()
        # "flat" is a built-in GTK style class (removes the theme's default
        # button chrome/border) -- not something this module's own CSS
        # defines, so it plays no part in the legibility guard's background
        # bookkeeping (tests/unit/_legibility.py only ever parses THIS
        # module's own _GALLERY_CSS text, never the ambient desktop theme's).
        button.add_css_class("flat")
        button.add_css_class("gallery-card")
        # Not "Fold {name}": a tap does not fold this target (see the module
        # docstring). The tooltip is for an operator with a mouse anyway --
        # it still must not claim something the booth cannot do.
        button.set_tooltip_text(f"{target.name} — one of the proteins this booth folds")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        # A grid ROW is necessarily one height shared by every cell in it
        # (see grid_shape/Gallery docs), so a card sitting next to a
        # longer-blurbed sibling can be allocated more height than its own
        # content needs. Top-aligning the whole content block means the
        # thumbnail/name/blurb/hint always start at the SAME y position
        # across a row regardless of that -- any leftover height becomes
        # ordinary bottom padding under the card, not a gap wedged between
        # two lines of text (the previous design's `blurb.set_vexpand
        # (True)` pushed "TAP TO FOLD" to the card's true bottom instead,
        # which read as a dead gap whenever a sibling card had a much
        # longer blurb -- see this task's report).
        content.set_valign(Gtk.Align.START)
        content.append(_build_thumbnail(target))

        name = Gtk.Label(label=target.name, xalign=0.0)
        name.add_css_class("gallery-card-name")
        name.set_wrap(True)
        content.append(name)

        # The blurb is visitor-facing copy read while deciding what to
        # pick (this task's brief) -- given room to wrap fully rather than
        # ellipsized/line-capped, so nothing a visitor might want to read
        # is ever silently cut off.
        blurb = Gtk.Label(label=target.blurb, xalign=0.0)
        blurb.add_css_class("gallery-card-blurb")
        blurb.set_wrap(True)
        blurb.set_justify(Gtk.Justification.LEFT)
        content.append(blurb)

        # Pacing info, not a promise: see _format_fold_time's own docstring
        # for why an unmeasured target gets its own explicit sentence here
        # rather than silence or a fabricated number.
        time_label = Gtk.Label(label=_format_fold_time(target.expected_s,
                                               target.expected_slow_s),
                               xalign=0.0)
        time_label.add_css_class("gallery-card-time")
        content.append(time_label)

        hint = Gtk.Label(label=_CARD_HINT, xalign=0.0)
        hint.add_css_class("gallery-card-tap-hint")
        content.append(hint)

        button.set_child(content)
        button.connect("clicked", self._on_card_clicked, target.id)
        return button

    def _on_card_clicked(self, _button, target_id):
        if self.on_pick is not None:
            self.on_pick(target_id)
