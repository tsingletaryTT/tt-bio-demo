"""The diagnostics panel: the booth's own protocol tap, made readable.

What this is, in the user's words: "a way with a press of a button or a
click [to] get shown more diagnostic, log-like data about what's happening
... something more code-like ... Of course some of our teaching what's
happening might go there too."

So this is two things braided together in one `tail -f`-shaped stream:

1. **Real traffic.** Every line marked with a timestamp is an actual
   protocol event as it lands off the socket -- stage transitions with
   their fractions, frame indices, the radius of gyration collapsing as
   the structure condenses, which chip the fold was placed on, the pLDDT
   the model gave itself, wall time. Nothing here is simulated, padded, or
   replayed for effect; if the daemon goes quiet, so does this panel, and
   that is information too.
2. **Teaching, interleaved.** The first time a fold enters a stage, one to
   three `#`-prefixed comment lines say what that stage actually does.
   They are written to be true rather than impressive: this is a diffusion
   model denoising atom coordinates over ~200 steps, of which the daemon
   subsamples ~30 to the wire (see runner/shaping.py's `select_frame_steps`
   and protocol/events.py's `STAGE_BANDS`), and the copy below says exactly
   that.

Two hard rules this module exists inside of
--------------------------------------------
**Never a stack trace, never raw error text.** A diagnostics panel is the
single most tempting place in the whole booth to violate the project's
"nothing in the UI may ever display a stack trace or raw error text" rule
-- a log-shaped widget practically asks for `str(exc)` to be piped into it.
It is not. `job_error`'s `message` field is never read here at all (grep
this file: the string "message" appears only in this sentence and in the
test that pins it), and every wire-sourced string that IS rendered goes
through `_safe`, which flattens newlines and truncates -- so even a daemon
that smuggled a traceback into `target_id` could not paint one on the
screen. The event is reported; the error text stays in the operator's log.

**Bounded, because the booth runs unattended all day.** Lines live in a
fixed-size `collections.deque(maxlen=MAX_LINES)` -- 200 entries, each at
most `MAX_LINE_CHARS` (120) characters, so the panel's total memory is
capped at roughly 24 KB of text no matter how long the booth runs. That
bound is not decoration: this project has already been bitten once by
unbounded growth (a tt-metal log writing 13-14 MB/s into an *unlinked*
file, which would have OOM'd the booth in about 31 minutes -- see
docs/followups.md and Phase 3a Task 1). The ring buffer is the ONLY thing
in this module that accumulates anything; the widget itself owns a fixed
number of row labels (`VISIBLE_LINES`), created once at construction and
re-labelled in place, so nothing grows there either.

Visual language: the same one as ui/panels.py -- the dark base `#092221`
as the only ground, `#F1F8F8`/`#C7D9D8` for text, `#1B8EB1` (as its
legible-on-dark tint `_ACCENT_TEXT`) as the single accent, small
letterspaced labels for headers. Monospace throughout, because a log that
jitters column-to-column is not a log.
"""

import collections
import logging
import math
import time

import gi
import numpy as np

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk, Pango

from protocol.events import within_stage_frac

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounds. See the module docstring: these are the whole of this module's
# memory story, and they are constants so a test can assert against the same
# numbers the widget uses.
# ---------------------------------------------------------------------------

# How many lines the ring buffer keeps. Deliberately larger than
# VISIBLE_LINES: an operator who opens the panel mid-fold should see the
# minute or so of history that led here, not only what arrives next. At the
# recorded fold's traffic (~35 events per ~4s cycle) 200 lines is roughly
# the last four folds.
MAX_LINES = 200

# Hard cap on one line's length. Nothing this module composes comes close;
# the cap exists for the wire-sourced fragments (`_safe`), so a pathological
# daemon string can neither blow up the buffer's memory bound nor push the
# rail wider than its fixed width.
MAX_LINE_CHARS = 120

# How many of those lines the panel actually shows. Fixed, because the row
# labels are created once and re-labelled in place -- the widget tree never
# grows or shrinks while the booth runs.
VISIBLE_LINES = 20

# ---------------------------------------------------------------------------
# Line kinds -- one CSS class each, all three legible on the dark ground
# (measured ratios are in this module's stylesheet comment below).
# ---------------------------------------------------------------------------
KIND_EVENT = "event"   # ordinary traffic
KIND_MARK = "mark"     # fold boundaries: job_start / job_done / connection
KIND_TEACH = "teach"   # the `#` commentary


# ---------------------------------------------------------------------------
# Teaching copy. Real copy, not placeholders -- and true.
#
# Line lengths are held to ~62 characters on purpose: the rail is a fixed
# 430px column (ui/app.py's `_SIDE_RAIL_WIDTH_PX`), and a monospace line
# longer than that would ellipsize mid-sentence. Where a stage needs more
# than one line to explain, it gets more than one line, each wrapped by
# hand rather than by Pango -- which is also what keeps every row in the
# panel exactly one line tall.
# ---------------------------------------------------------------------------
STAGE_TEACHING = {
    "msa": (
        "# msa: line up evolutionary relatives of the sequence --",
        "#   residues that co-evolve tend to touch in 3D.",
    ),
    "prep": (
        "# prep: sequence and features become tensors, laid out",
        "#   for the Tenstorrent chip that will run the fold.",
    ),
    "trunk": (
        "# trunk: 10 refinement cycles build the model's map of",
        "#   which residue sits next to which.",
    ),
    "diffusion": (
        "# diffusion: ~200 denoising steps pull a cloud of noise",
        "#   into real atom coordinates. ~30 of those steps are",
        "#   sampled to the wire -- the collapse you can see.",
    ),
    # Per RESIDUE, not per atom: the score this booth actually renders is
    # read from the CA atom's B-factor, one value per residue
    # (ui/geometry.py's load_ca_trace), and the ribbon is coloured by that.
    # Both this line and the help card's pLDDT legend said "per atom".
    "confidence": (
        "# confidence: the model scores its own answer, per",
        "#   residue. That score is pLDDT (0-100) and it",
        "#   colours the ribbon.",
    ),
    "saving": (
        "# saving: coordinates written out as mmCIF -- the file",
        "#   the ribbon on screen is built from.",
    ),
}

# Shown once, when the log is created: what a visitor is looking at.
BANNER = (
    "# tail -f on this booth's own protocol socket.",
    "# every timestamped line below is a real event,",
    "# off a real fold, as it lands.",
)


def _safe(value, limit=48):
    """Render one wire-sourced value as a single short, single-line token.

    Every string this module takes from an event goes through here. It is
    the structural reason a traceback cannot reach the screen even if a
    daemon put one in a field this panel does render: newlines (and tabs,
    and stray control characters) become spaces, and the result is
    truncated. `None` reads as `?` rather than as the word "None", which
    would look like a value the daemon actually sent.
    """
    if value is None:
        return "?"
    text = str(value)
    text = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text or "?"


def _num(value, fmt, fallback="?"):
    """Format a wire-sourced number, or `fallback` if it is not one.

    The wire's numbers are trusted nowhere in this codebase (see
    ui/app.py's `_format_missing` for the same argument applied to logging):
    a `frac` that arrives as a string or `None` must cost this panel one
    unremarkable `?`, not an exception inside a rendering path.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return format(number, fmt)


def radius_of_gyration(coords):
    """Root-mean-square distance of `coords` from their own centroid, in
    the coordinate units the wire uses (angstroms).

    This is the one derived quantity the panel reports, and it is derived
    here rather than displayed raw because it is the number that makes the
    collapse legible as a NUMBER: a diffusion trajectory starts as a wide
    ball of noise (large Rg) and condenses onto a folded structure (small
    Rg), so a visitor watching the screen collapse can watch this figure
    fall in step with it. Standard definition -- sqrt(mean(|r - r_mean|^2))
    -- with no mass weighting, because the wire carries positions only.

    Returns NaN for an empty frame rather than raising or inventing 0.0:
    "no atoms" is not "a structure of size zero", and the caller renders
    NaN as `?`.
    """
    points = np.asarray(coords, dtype="float64")
    if points.size == 0:
        return float("nan")
    points = points.reshape(-1, 3)
    centre = points.mean(axis=0)
    return float(np.sqrt(((points - centre) ** 2).sum(axis=1).mean()))


class DiagnosticsLog:
    """The bounded ring buffer, and the pure event -> lines decisions.

    No GTK here at all (the widget below is thin assembly over this), so
    every line this panel can ever show is testable without a display --
    including the two rules that matter most: that a `job_error` never
    surfaces the daemon's error text, and that the buffer's length is
    capped no matter how long the booth runs.

    `clock` is injectable for the same reason ui/app.py's is: a test that
    asserts on a timestamp should not have to guess what time it is.
    Production gets `time.time` -- wall clock, deliberately, unlike the
    state machine's monotonic clock: these stamps are read by a human
    standing at a booth comparing them against their watch, not used to
    measure an interval.
    """

    def __init__(self, max_lines=MAX_LINES, clock=time.time, banner=True):
        self._lines = collections.deque(maxlen=max_lines)
        self._clock = clock
        # Which stages this fold has already been taught about. Cleared on
        # every job_start, so each fold teaches its stages once -- a visitor
        # who arrives mid-cycle still gets the explanation on the next fold,
        # and a visitor who stands there for ten folds is not shown the same
        # paragraph forty times.
        self._taught = set()
        # Bumped on every append. The widget repaints only when this moves,
        # which is what keeps a 30Hz frame stream from re-labelling twenty
        # rows for nothing.
        self.revision = 0
        if banner:
            for line in BANNER:
                self.add(line, KIND_TEACH, stamped=False)

    # -- the buffer ------------------------------------------------------

    def add(self, text, kind=KIND_EVENT, stamped=True):
        """Append one line. Truncated to `MAX_LINE_CHARS`; the deque's own
        `maxlen` drops the oldest line once the buffer is full, which is
        the entire bound (see the module docstring)."""
        text = text[:MAX_LINE_CHARS]
        stamp = self._stamp() if stamped else ""
        self._lines.append((stamp, kind, text))
        self.revision += 1

    def _stamp(self):
        seconds = self._clock()
        return time.strftime("%H:%M:%S", time.localtime(seconds)) + \
            f".{int((seconds % 1) * 1000):03d}"

    def tail(self, count=VISIBLE_LINES):
        """The last `count` entries as `(stamp, kind, text)`, oldest
        first. Fewer than `count` early on -- the widget pads."""
        if count >= len(self._lines):
            return list(self._lines)
        return list(self._lines)[-count:]

    def __len__(self):
        return len(self._lines)

    # -- events in, lines out --------------------------------------------

    def note_event(self, event):
        """Turn one decoded protocol event into zero or more lines.

        Deliberately total: an event type this build does not know about
        still produces one honest line rather than nothing, and no branch
        here can raise on wire-shaped data (`_safe`/`_num` see to that).
        """
        kind = event.get("type")

        if kind == "hello":
            # The wire field is `cards` and stays `cards` -- it is a
            # protocol contract (protocol/events.py, runner/server.py) and
            # renaming it would break every daemon that speaks v1. What it
            # CARRIES is one entry per chip, which is what this line now
            # says: the vocabulary fix is in the rendering, not the wire.
            chips = event.get("cards")
            count = len(chips) if isinstance(chips, list) else "?"
            # Pluralised: this daemon is card-0 only (runner/daemon.py), so
            # the ordinary reading is ONE, and "1 chips" was rendering
            # directly above a panel that reads "4 chips on 2 boards".
            noun = "chip" if count == 1 else "chips"
            self.add(f"connected · protocol v{_safe(event.get('version'), 4)}"
                     f" · {count} {noun} · preflight "
                     f"{_safe(event.get('preflight'), 12)}", KIND_MARK)
        elif kind == "job_start":
            self._taught = set()
            self.add(f"▶ fold {_safe(event.get('target_id'), 22)}"
                     f" · {_num(event.get('n_residues'), '.0f')} res"
                     f" · chip {_safe(event.get('card'), 4)}", KIND_MARK)
            self.add(f"  model {_safe(event.get('model'), 20)}"
                     f" · job {_safe(event.get('job_id'), 16)}")
        elif kind == "stage":
            stage = event.get("stage")
            wire = event.get("frac", 0.0)
            self.add(f"stage {_safe(stage, 12):<11}"
                     f" wire {_num(wire, '6.1%')}"
                     f" · in-stage {_num(self._within(stage, wire), '6.1%')}")
            self._teach(stage)
        elif kind == "job_done":
            self.add(f"■ done · {_num(event.get('wall_s'), '.2f')}s wall"
                     f" · pLDDT {_num(event.get('mean_plddt'), '.1f')}", KIND_MARK)
        elif kind == "job_error":
            # The one line in this module that a careless edit would turn
            # into a rule violation. The daemon's own error text is NOT
            # read here -- not truncated, not sanitised, not read. It is in
            # the operator's log (ui/app.py logs it in full); the visitor
            # gets the fact, not the traceback.
            self.add(f"✗ fold did not finish · job "
                     f"{_safe(event.get('job_id'), 16)} · see operator log",
                     KIND_MARK)
        elif kind == "not_ready":
            # Same rule, same reason: `missing` names real filesystem paths
            # (ui/app.py's `_PREPARING_MESSAGE` comment). The count is
            # information; the paths are not ours to show.
            missing = event.get("missing")
            count = len(missing) if isinstance(missing, list) else "?"
            self.add(f"! daemon not ready · {count} item(s) outstanding",
                     KIND_MARK)
        elif kind == "card_state":
            # Same rule as `hello` above: the EVENT TYPE and its `card`
            # field are wire vocabulary and untouched; the LINE says chip,
            # because that is what the daemon is describing.
            self.add(f"chip {_safe(event.get('card'), 4)}"
                     f" · {_safe(event.get('state'), 14)}"
                     f" · {_num(event.get('temperature_c'), '.1f')}°C")
        elif kind == "frame":
            # Frames arrive far too fast to log from the socket thread, and
            # the booth only ever draws the newest one anyway -- so they
            # are logged where they are DRAWN, by note_frame below, with
            # the geometry the viewer actually received.
            pass
        else:
            self.add(f"? unhandled event {_safe(kind, 20)}")

    def note_frame(self, event, coords):
        """One line for a diffusion frame that reached the screen.

        Called from the frame drain (ui/app.py), not from the socket
        thread, and therefore honest about what it says: these are the
        frames a visitor actually saw. The wire carries ~30 subsampled
        steps of a ~200-step trajectory, and the buffer between socket and
        screen is latest-wins, so a step number that jumps is the truth of
        the pipeline, not a gap in this log.
        """
        rg = radius_of_gyration(coords) if coords is not None else float("nan")
        n_atoms = len(coords) if coords is not None else None
        self.add(f"frame {_num(event.get('step'), '>3.0f')}"
                 f"/{_num(event.get('total'), '<3.0f')}"
                 f" · {_num(n_atoms, '>5.0f')} atoms"
                 f" · Rg {_num(rg, '6.2f')} Å")

    def note_connection(self, state):
        """The socket came up, went away, or turned out to speak a protocol
        version this build does not."""
        self.add(f"socket {_safe(state, 16)}", KIND_MARK)

    def note(self, text, kind=KIND_EVENT):
        """A line the app itself wants in the stream (a visitor's pick, the
        panel being opened). Kept distinct from `note_event` so it is
        obvious at the call site that this line is NOT wire traffic."""
        self.add(text, kind)

    # -- internals -------------------------------------------------------

    def _within(self, stage, wire_frac):
        """The wire's whole-fold fraction as a within-stage one, via the
        single shared converter (protocol.events.within_stage_frac) -- the
        same one the pipeline panel is driven through, so the two readouts
        can never disagree about what "40%" means."""
        try:
            return within_stage_frac(stage, wire_frac)
        except (TypeError, ValueError):
            return float("nan")

    def _teach(self, stage):
        if stage in self._taught:
            return
        self._taught.add(stage)
        for line in STAGE_TEACHING.get(stage, ()):
            self.add(line, KIND_TEACH, stamped=False)


# ---------------------------------------------------------------------------
# Stylesheet. Same conventions as ui/panels.py: one background tier (the
# dark ground), a `_BACKGROUND_BY_CLASS` map that is the single source of
# truth for the shared legibility guard (tests/unit/_legibility.py), and
# every label carrying a class that sets an explicit `color:`.
#
# Measured contrast against the `#092221` ground (ui.panels.contrast_ratio,
# WCAG 2.x, AA floor 4.5:1):
#     .diagnostics-header  #C7D9D8   11.36:1
#     .diagnostics-event   #C7D9D8   11.36:1
#     .diagnostics-mark    #F1F8F8   15.46:1
#     .diagnostics-teach   #3299B9    5.06:1
# `_ACCENT_TEXT` (#3299B9) rather than the raw brand accent #1B8EB1 for the
# teaching lines, for exactly the reason ui/panels.py pins that constant:
# the pure accent measures 4.40:1 and is therefore a fill colour, not a
# text colour.
# ---------------------------------------------------------------------------
_CSS_INSTALLED = False

_DARK_BASE = "#092221"
_BG = "#F1F8F8"
_BG_ALT = "#C7D9D8"
_ACCENT_TEXT = "#3299B9"
_HAIRLINE = "rgba(199, 217, 216, 0.18)"

_BACKGROUND_BY_CLASS = {
    "diagnostics-panel": _DARK_BASE,
}

_DIAGNOSTICS_CSS = f"""
.diagnostics-panel {{
    background-color: {_BACKGROUND_BY_CLASS["diagnostics-panel"]};
    padding: 10px 16px 12px 16px;
    border-radius: 6px;
    border-top: 1px solid {_HAIRLINE};
}}
.diagnostics-header {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: {_BG_ALT};
    padding-bottom: 6px;
}}
.diagnostics-line {{
    font-family: "Berkeley Mono", monospace;
    font-size: 10px;
}}
.diagnostics-event {{
    color: {_BG_ALT};
}}
.diagnostics-mark {{
    color: {_BG};
    font-weight: 700;
}}
.diagnostics-teach {{
    color: {_ACCENT_TEXT};
}}
"""

_KIND_CLASSES = {
    KIND_EVENT: "diagnostics-event",
    KIND_MARK: "diagnostics-mark",
    KIND_TEACH: "diagnostics-teach",
}


def _ensure_css_installed():
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        log.debug("no default display; skipping diagnostics CSS install")
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_DIAGNOSTICS_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


class DiagnosticsPanel(Gtk.Box):
    """`VISIBLE_LINES` monospace rows over a `DiagnosticsLog`.

    Thin, in the same sense ui/panels.py's widgets are thin: every decision
    about WHAT a line says lives in `DiagnosticsLog`; this class owns the
    row labels, their CSS classes, and nothing else.

    Fixed shape by construction -- the rows are created once, in
    `__init__`, and only ever re-labelled. Nothing is added to or removed
    from the widget tree while the booth runs, which is the widget-side
    half of the module's boundedness story (the ring buffer is the other
    half). Rows are ellipsized rather than wrapped so a long line can
    never change a row's height or push the fixed-width rail wider.
    """

    def __init__(self, visible_lines=VISIBLE_LINES):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        _ensure_css_installed()
        self.add_css_class("diagnostics-panel")
        self.set_hexpand(False)
        self.set_vexpand(False)

        header = Gtk.Label(label="DIAGNOSTICS · LIVE PROTOCOL TAP", xalign=0.0)
        header.add_css_class("diagnostics-header")
        self.append(header)

        self._rows = []
        for _ in range(visible_lines):
            row = Gtk.Label(label="", xalign=0.0)
            row.add_css_class("diagnostics-line")
            row.add_css_class(_KIND_CLASSES[KIND_EVENT])
            row.set_ellipsize(Pango.EllipsizeMode.END)
            row.set_max_width_chars(64)
            row.set_single_line_mode(True)
            self.append(row)
            self._rows.append(row)

        # What revision of the log these rows were painted from. `-1` (not
        # 0) so the very first refresh always paints, including the banner
        # a freshly-constructed log starts with.
        self._painted_revision = -1

    def refresh(self, diag_log, force=False):
        """Repaint from `diag_log` if it has moved on since the last paint.

        Called from the booth's existing 100ms state tick rather than from
        every `add`: a 30Hz frame stream would otherwise re-label twenty
        rows thirty times a second to show a list that a human reads at
        reading speed. Returns True if it actually painted -- which is what
        the test asserts against, rather than scraping label text.
        """
        if not force and diag_log.revision == self._painted_revision:
            return False
        self._painted_revision = diag_log.revision

        entries = diag_log.tail(len(self._rows))
        # Newest at the BOTTOM, like a terminal: pad at the top so the
        # stream grows downward and the most recent line is always on the
        # same row, instead of the whole block sliding as the log fills.
        padding = [None] * (len(self._rows) - len(entries))
        for row, entry in zip(self._rows, padding + entries):
            if entry is None:
                text, kind = "", KIND_EVENT
            else:
                stamp, kind, body = entry
                text = f"{stamp} {body}" if stamp else body
            for css_class in _KIND_CLASSES.values():
                row.remove_css_class(css_class)
            row.add_css_class(_KIND_CLASSES.get(kind, _KIND_CLASSES[KIND_EVENT]))
            row.set_label(text)
        return True
