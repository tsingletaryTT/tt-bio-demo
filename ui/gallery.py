"""The booth's gallery: a visitor picks which target to fold.

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
# Duplicated here for the identical reason: this module's own "tap to fold"
# hint sits on the same dark ground and needs the same fix.
_ACCENT_TEXT = "#3299B9"
_HAIRLINE = "rgba(199, 217, 216, 0.18)"  # _BG_ALT at 18% opacity, matches ui/panels.py


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
    otherwise a placeholder that looks deliberate -- a large initial on a
    light, calm fill plus a small "preview coming soon" caption -- rather
    than a broken-image icon or blank space, since every shipped target
    lacks a thumbnail today (Phase 4 owns the art) and this is the normal
    case, not a failure mode a visitor should read as one.
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

    placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    placeholder.add_css_class("gallery-thumbnail-placeholder")
    placeholder.set_size_request(_THUMBNAIL_WIDTH_PX, _THUMBNAIL_HEIGHT_PX)
    placeholder.set_halign(Gtk.Align.FILL)
    placeholder.set_valign(Gtk.Align.FILL)

    glyph = Gtk.Label(label=_placeholder_glyph(target.name))
    glyph.add_css_class("gallery-thumbnail-placeholder-glyph")
    glyph.set_halign(Gtk.Align.CENTER)
    glyph.set_valign(Gtk.Align.CENTER)
    glyph.set_vexpand(True)

    caption = Gtk.Label(label="PREVIEW COMING SOON")
    caption.add_css_class("gallery-thumbnail-placeholder-caption")
    caption.set_halign(Gtk.Align.CENTER)
    caption.set_margin_bottom(8)

    placeholder.append(glyph)
    placeholder.append(caption)
    return placeholder


# ---------------------------------------------------------------------------
# CSS, installed once against the default display -- same pattern as
# ui/panels.py (guarded on a live display existing at all, so constructing
# a Gallery never hard-requires one; see that module's comment).
#
# `_BACKGROUND_BY_CLASS` is the single source of truth for "which CSS class
# carries an explicitly-set background," read by tests/unit/test_gallery.py
# via the shared tests/unit/_legibility.py walker -- exactly the role
# ui/panels.py's own dict plays for its tests. Two tiers here (unlike
# panels.py's one): the gallery root's dark ground, and the placeholder
# thumbnail's own light fill -- a label inside the placeholder must be
# checked against ITS true nearest background (light), not the gallery
# root's (dark), which is precisely the "nearest, not merely registered"
# property tests/unit/_legibility.py's walker exists to get right.
# ---------------------------------------------------------------------------
_CSS_INSTALLED = False

_BACKGROUND_BY_CLASS = {
    "gallery": _DARK_BASE,
    "gallery-thumbnail-placeholder": _BG_ALT,
}

_GALLERY_CSS = f"""
.gallery {{
    background-color: {_BACKGROUND_BY_CLASS["gallery"]};
}}
.gallery-card {{
    padding: 14px;
    border-radius: 8px;
    border: 1px solid {_HAIRLINE};
    min-height: 260px;
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
.gallery-card-tap-hint {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: {_ACCENT_TEXT};
}}
.gallery-thumbnail-placeholder {{
    background-color: {_BACKGROUND_BY_CLASS["gallery-thumbnail-placeholder"]};
    border-radius: 6px;
}}
.gallery-thumbnail-placeholder-glyph {{
    font-family: "Berkeley Mono", monospace;
    font-size: 42px;
    font-weight: 700;
    color: {_DARK_BASE};
}}
.gallery-thumbnail-placeholder-caption {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.06em;
    color: {_DARK_BASE};
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
        self.set_child(self._grid)

        # target.id -> the Gtk.Button card built for it. Not read by GTK
        # itself, but it lets a test tap a specific card by id, and lets
        # diagnostics ask "how many cards actually got built" without
        # walking the grid's own child list -- same rationale as
        # TelemetryPanel.last_status / PipelinePanel.last_rows in
        # ui/panels.py.
        self.cards = {}

        self._build_cards()

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
        button.set_tooltip_text(f"Fold {target.name}")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
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
        blurb.set_vexpand(True)
        blurb.set_valign(Gtk.Align.START)
        content.append(blurb)

        hint = Gtk.Label(label="TAP TO FOLD", xalign=0.0)
        hint.add_css_class("gallery-card-tap-hint")
        content.append(hint)

        button.set_child(content)
        button.connect("clicked", self._on_card_clicked, target.id)
        return button

    def _on_card_clicked(self, _button, target_id):
        if self.on_pick is not None:
            self.on_pick(target_id)
