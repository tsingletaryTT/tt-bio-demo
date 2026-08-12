"""Shared, stylesheet-agnostic legibility-guard machinery.

Factored out of tests/unit/test_panels.py's own generalized checks (see
that file's "Critical 1 / Important 4" section for the original defect
this exists to catch: a `Gtk.Label` with no explicit `color:` rule behind
its CSS classes silently inherits the desktop theme instead, which on a
dark-themed machine measured ~1.01:1 contrast -- effectively invisible,
found only by rendering the panel and sampling the actual pixels).

NOT a test file itself (no `test_*` names here, so pytest's default
`python_files` pattern never collects it -- see pytest.ini). Both
tests/unit/test_panels.py and tests/unit/test_gallery.py import from here,
per this task's brief ("apply the same standard to the gallery, and
extend that guard to cover it rather than leaving ui/gallery.py
unprotected") -- so ui/panels.py's guard and ui/gallery.py's guard are
provably ONE mechanism applied to two stylesheets, not two independently
maintained copies of the same regex-and-CSS-cascade logic that could
silently drift apart from each other (exactly the kind of duplication this
project's own CLAUDE.md warns costs correctness: "ask what wrong answer a
test would still accept").

Every function below takes the CSS text / background-class map as a plain
argument, or — for the two the shipped tests exercise via monkeypatching a
module's own module-level constants (see test_panels.py's
"nearest_background_walker" tests) — as a zero-argument callable that reads
the CURRENT value fresh on every call, never a value captured once at
import time. That parametrization is the entire point: it is what lets
this one implementation run against `ui.panels._PANEL_CSS` /
`ui.panels._BACKGROUND_BY_CLASS` in one test file and
`ui.gallery._GALLERY_CSS` / `ui.gallery._BACKGROUND_BY_CLASS` in the other,
with neither file's stylesheet hardcoded into this module at all.
"""

import re

import gi
import pytest

# Pinned here as well as in the modules under test: this file is imported
# directly by several test modules, and if it is the FIRST thing to pull in
# gi.repository (import order is alphabetical, and `_legibility` sorts before
# `ui.*`), an unversioned import emits a PyGIWarning and -- worse -- would
# accept whatever version happened to be installed.
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk

# ---------------------------------------------------------------------------
# Widget-tree walking.
# ---------------------------------------------------------------------------

def iter_labels(widget):
    """Every `Gtk.Label` in `widget`'s subtree (including `widget` itself),
    depth-first. Walks the REAL, live widget tree via `get_first_child`/
    `get_next_sibling` -- not a re-derivation from CSS classes or from
    whatever a widget's own bookkeeping fields happen to record -- so a
    label added anywhere in a panel's or gallery's tree is found, not just
    the ones some other piece of code remembered to register."""
    if isinstance(widget, Gtk.Label):
        yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from iter_labels(child)
        child = child.get_next_sibling()


def rgba_to_hex(rgba):
    """A `Gdk.RGBA` (0.0-1.0 float channels, as `Gtk.Widget.get_color()`
    returns) as an uppercase `#RRGGBB` string, clamped to the valid range
    first -- GTK's own resolved colors are always in range in practice, but
    clamping keeps this conversion itself total rather than trusting that."""
    def channel(c):
        return round(max(0.0, min(1.0, c)) * 255)
    return f"#{channel(rgba.red):02X}{channel(rgba.green):02X}{channel(rgba.blue):02X}"


# ---------------------------------------------------------------------------
# Static CSS-text analysis -- theme-independent, never calls get_color().
# ---------------------------------------------------------------------------

_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_CSS_CLASS_TOKEN_RE = re.compile(r"\.([A-Za-z0-9_-]+)")
_CSS_BARE_CLASS_RE = re.compile(r"^\.[A-Za-z0-9_-]+$")
# Negative lookbehind on "-" excludes `background-color:`/`border-color:` --
# only a bare `color:` property counts here.
_CSS_EXPLICIT_COLOR_PROP_RE = re.compile(r"(?<!-)color\s*:")
_CSS_BACKGROUND_PROP_RE = re.compile(r"background(-color)?\s*:")


def strip_css_comments(css_text):
    return _CSS_COMMENT_RE.sub("", css_text)


def color_rules_from_css(css_text):
    """Every rule in `css_text` that sets a plain `color:` (never
    `background-color:`) property, as a list of frozensets of the class(es)
    a compound selector REQUIRES ALL of (e.g.
    `.telemetry-hero-number.telemetry-hero-hot` requires both, not either
    alone -- a naive "any token in this selector" reading would wrongly
    keep crediting a class after its OWN base rule's `color:` was deleted,
    as long as some unrelated compound rule happened to still mention that
    class name).

    This is what makes `label_has_an_explicit_color_rule` below a STATIC,
    theme-independent check: pure text analysis of a stylesheet's own
    source, never a runtime GTK CSS cascade resolved against whatever
    desktop theme happens to be loaded (see the module docstring's "Fix
    round 2" motivation).
    """
    css_text = strip_css_comments(css_text)
    rules = []
    for selector_part, props in _CSS_RULE_RE.findall(css_text):
        if not _CSS_EXPLICIT_COLOR_PROP_RE.search(props):
            continue
        for compound in selector_part.split(","):
            required = frozenset(_CSS_CLASS_TOKEN_RE.findall(compound))
            if required:
                rules.append(required)
    return rules


def label_has_an_explicit_color_rule(label, color_rules):
    """True if `label`'s currently-applied CSS classes are a superset of at
    least one real color-setting rule's required class set -- i.e. some
    rule in the actual stylesheet text genuinely applies a `color:` to
    exactly the classes this widget carries, so its foreground cannot be
    silently inherited from an ambient theme regardless of what that theme
    resolves to on any given machine."""
    classes = set(label.get_css_classes())
    return any(required <= classes for required in color_rules)


def background_affecting_classes_from_css(css_text):
    """Every class named by a BARE single-class selector (`.foo { ... }`,
    no compound, no descendant combinator) whose rule sets a background,
    parsed from `css_text`.

    Deliberately restricted to BARE class selectors: a stylesheet's
    decorative nested-paint-node rules (e.g. a progress bar's `trough
    progress` fill) target compound/descendant selectors whose classes are
    also applied directly to LABELS themselves in some of these modules --
    treating every token in those selectors as "background-affecting"
    would make a label's OWN class look like a background it sits ON. A
    bare `.foo { background-color: ...; }` rule, by contrast, really does
    mean "an element with exactly this class has this background," which
    is what an actual container tier looks like in either
    ui.panels._PANEL_CSS or ui.gallery._GALLERY_CSS today.
    """
    css_text = strip_css_comments(css_text)
    classes = set()
    for selector_part, props in _CSS_RULE_RE.findall(css_text):
        if not _CSS_BACKGROUND_PROP_RE.search(props):
            continue
        for piece in selector_part.split(","):
            piece = piece.strip()
            if _CSS_BARE_CLASS_RE.match(piece):
                classes.add(piece[1:])
    return classes


def nearest_explicit_background_hex(widget, *, css_text_fn, background_by_class_fn):
    """Walk from `widget` up through its ancestors and return the hex of
    the FIRST (nearest) one carrying any class the real stylesheet (from
    `css_text_fn()`, called fresh -- never cached, so a test that
    monkeypatches the underlying module constant is honored) actually
    paints a background with.

    `css_text_fn`/`background_by_class_fn` are zero-argument callables
    (typically `lambda: some_module._SOME_CSS`), not plain values, for
    exactly that reason -- a value captured once at call time would miss a
    monkeypatch applied mid-test.

    If the nearest background-painted ancestor's class is not present in
    `background_by_class_fn()`'s keys, this fails LOUDLY right here rather
    than silently falling through to a different, more distant, registered
    ancestor -- the fix for a real defect (see test_panels.py's own
    "nearest_background_walker" tests): climbing straight to the first
    REGISTERED class let a genuinely-nearer, unregistered light background
    go unnoticed, certifying a label's true 1.08:1 contrast at 16.6:1.
    """
    background_classes = background_affecting_classes_from_css(css_text_fn())
    node = widget
    while node is not None:
        hit = set(node.get_css_classes()) & background_classes
        if hit:
            background_by_class = background_by_class_fn()
            registered = hit & set(background_by_class)
            if not registered:
                raise AssertionError(
                    f"{widget!r}'s NEAREST background-affecting ancestor "
                    f"{node!r} carries class(es) {sorted(hit)!r}, which set "
                    "a background in the real stylesheet but are NOT "
                    "registered in the background-by-class map -- teach "
                    "the walker about this background tier; do not let it "
                    "fall through to a different, more distant ancestor")
            return background_by_class[next(iter(registered))]
        node = node.get_parent()
    raise AssertionError(
        f"no ancestor of {widget!r} carries any class that paints a "
        "background in the real stylesheet -- a label with no backgrounded "
        "ancestor at all cannot be contrast-checked")


def merged_stylesheets(*sources):
    """Combine several modules' stylesheets into ONE (css_text_fn,
    background_by_class_fn) pair, for checking a widget tree that is
    assembled from more than one of them.

    ui/app.py's side rail is exactly that tree: its own labels sit on
    `_APP_CSS`'s `.booth-side`, but it also contains ui/panels.py's two
    panels and ui/diagnostics.py's log rows, each with its own ground and
    its own background-by-class map. Checking such a tree against a single
    module's stylesheet is not merely incomplete -- it is WRONG in the
    dangerous direction: `nearest_explicit_background_hex` would not even
    see the nearer panel background (its class sets no background in the
    stylesheet it was handed), so it would climb past it and certify every
    panel label against the rail's ground instead of its own. Today those
    two grounds happen to be the same colour, which is exactly why this
    could rot silently the day a panel gains a tier of its own.

    Each `source` is a `(css_text_fn, background_by_class_fn)` pair of
    zero-argument callables -- the same late-binding contract every other
    function here takes, so a test that monkeypatches a module constant is
    still honored through the merge.
    """
    def css_text_fn():
        return "\n".join(css_fn() for css_fn, _bg_fn in sources)

    def background_by_class_fn():
        merged = {}
        for _css_fn, bg_fn in sources:
            merged.update(bg_fn())
        return merged

    return css_text_fn, background_by_class_fn


def assert_every_label_is_legible(root, *, context, min_contrast, contrast_ratio_fn,
                                   css_text_fn, background_by_class_fn):
    """Walk every real `Gtk.Label` descendant of `root`, read its ACTUALLY-
    RESOLVED foreground colour via the real GTK CSS engine
    (`Gtk.Widget.get_color()`), and check it against the nearest ancestor
    carrying an explicitly-set background -- failing loudly (never
    skipping) if there is no live display to resolve real colors against,
    per this project's own rule against a silently-empty/no-op test half
    being reported as a pass.
    """
    if Gdk.Display.get_default() is None:
        pytest.fail(
            f"[{context}] no default display: cannot resolve real GTK "
            "colors, and a legibility test that silently no-ops on a "
            "headless run would be worse than not having one")
    failures = []
    for label in iter_labels(root):
        fg_hex = rgba_to_hex(label.get_color())
        bg_hex = nearest_explicit_background_hex(
            label, css_text_fn=css_text_fn, background_by_class_fn=background_by_class_fn)
        ratio = contrast_ratio_fn(fg_hex, bg_hex)
        if ratio < min_contrast:
            failures.append(
                f"[{context}] label {label.get_label()!r}: fg={fg_hex} "
                f"bg={bg_hex} ratio={ratio:.2f} < {min_contrast}")
    assert not failures, "\n".join(failures)
