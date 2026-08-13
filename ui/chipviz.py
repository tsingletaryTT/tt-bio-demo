"""The Tensix activity panel: one animated core grid per REAL chip.

What it is
----------
A small, optional instrument in the booth's right-hand rail that draws one
`tensix-viz` Tensix core-grid animation per Tenstorrent chip actually present
under `/sys/class/tenstorrent/`, with an animation mode chosen to match the
stage of the fold the booth is running. It sits directly beneath the
telemetry panel and uses the same left-to-right chip order, so the third
animation is the third chip's readout's animation.

What is live, and what is not (measured, not assumed)
------------------------------------------------------
Say this accurately, because the whole booth's claim is "this is real":

- **The chip COUNT is live.** One canvas per real device under
  `/sys/class/tenstorrent/`. No chips, no panel.
- **The CLOCK READOUT is live and per-chip.** `_tick` reads every chip's
  `tt_aiclk` every second and shows the peak. On this booth's QB2 that reads
  1350 MHz, which is what the driver really reports.
- **The MODE is live**, driven by the fold's stage off the socket.
- **WHICH CHIP is folding is live, and only that chip animates the fold.**
  `job_start` carries the card index the daemon claimed (runner/folder.py),
  and `set_folding_chip` aims the fold's mode at that canvas alone; the
  others animate `idle`, which is what they are. This phase's daemon is
  card-0 only (runner/daemon.py builds `CardPool([config.device_id])` with
  `device_id = 0` and says so), so on this booth that is one chip working
  and three resting -- and the panel now shows exactly that. Until this fix
  the mode was fanned to all four canvases, which said four chips were
  folding when one was.
- **The per-chip ANIMATION is not visibly clock-driven.** Each chip's own
  activity IS fed to its own canvas (`setChipStats`, exactly as
  tt-local-generator's `activity_viz.py` does), and tensix-viz really does
  consume it -- its DRAM layer alpha is `dram_bw * env * 0.55`. But at the
  86px canvas this rail can afford, the difference between feeding a chip
  0.0 and feeding it 1.0 is not distinguishable on screen: rendered both
  ways and compared, the means differed by less than the animation's own
  frame-to-frame noise. The feed is kept because it is correct, costs four
  short JS calls a second, and starts working the moment the canvas or the
  library's memory layer grows -- but nothing in the UI claims it is doing
  something visible, and the help card was edited to remove a sentence that
  did. See this task's report.

Why there is a WebView in a project that chose "no browser"
------------------------------------------------------------
This is a deliberate, scoped exception to a real project decision, and it is
recorded here so the next reader knows it was a decision rather than a drift.

The original objection (see the spec and CLAUDE.md) was to a **browser
serving as the main 3D view** — the protein had to be native GTK4 + OpenGL,
not WebGL in a Chromium, because that is the part of the demo whose frame
timing, colour and provenance the booth is claiming to be real. None of that
applies to a decorative hardware animation in a 430px rail:

- it is **optional** — `available` is False and the widget hides itself if
  WebKit is missing, if there are no chips, or if the bundled assets cannot
  be read;
- it is **scoped** — one `WebView`, one `load_html` of an `about:blank` page,
  no navigation, no network (the assets are vendored, see
  `assets/tensix-viz/PROVENANCE.md`), and no path by which it can affect the
  viewer, the state machine or the socket;
- it is **fail-soft** — every JS evaluation, every sysfs read and every file
  read is guarded, and a failure costs at most one frame of animation;
- it is **proven in a sibling project** — `~/code/tt-local-generator`'s
  `app/activity_viz.py` has shipped this exact shape (WebKit + vendored
  tensix-viz + per-chip sysfs telemetry) and this module deliberately follows
  it rather than inventing a second one.

The cost, stated plainly: a WebKit WebProcess (tens of MB) in a booth process
that previously had none. That is the trade, and it buys the one thing the
booth could not otherwise show — that the silicon in this machine is real,
how much of it there is, and which of it this fold is running on. Note what
that is NOT a claim of: this phase folds on card 0 only, so what the panel
shows is one chip working next to three that are idle, and it is drawn that
way (see "What is live" above).

Differences from the reference implementation, and why
-------------------------------------------------------
1. **No background thread.** `activity_viz.py` polls per-chip POWER, which
   needs a `tt-smi` subprocess (~0.3s) and therefore a thread. This module
   polls `tt_aiclk` from sysfs, which is four small file reads and is
   effectively instantaneous, so the poll runs on the GTK main loop as a
   plain `GLib.timeout`. That removes a thread, removes an
   `idle_add` hand-back, and removes any possibility of this module touching
   a widget off the main loop.

   The cost is a coarser signal: on Blackhole, AICLK is close to binary
   (~800 MHz idle / 1350 MHz boosted), so `clock_activity` normalises
   idle-relative to make what movement there is visible. The MODE carries
   most of the meaning here anyway, and the honest per-chip number is on the
   telemetry panel a few pixels above.

2. **The mode is driven by the fold's STAGE, not by the booth's screen.**
   See `viz_mode`.

Bounds (the booth runs unattended all day)
-------------------------------------------
- One poll source, `POLL_INTERVAL_MS` (1000 ms), added by `set_running(True)`
  and *removed* by `set_running(False)` and on `unrealize`. While stopped,
  this module reads nothing and evaluates no JS at all.
- Widgets are created once, in `__init__`, and only ever re-labelled. Nothing
  is appended to the widget tree after construction.
- The unrealized-JS backlog is capped at `_MAX_PENDING_JS` entries, dropping
  stale telemetry and keeping the first (the mode activation) — the same
  bound the reference uses.
- The page is loaded once and never navigates, so the WebView's own memory is
  whatever one static page costs, not a function of uptime.
"""

import json
import logging
import math
import os
import pathlib
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk

log = logging.getLogger(__name__)

# ── the one environment variable this module must set, and why ──────────────
#
# WebKitGTK runs its web process inside a bubblewrap sandbox, which needs an
# unprivileged user namespace. Ubuntu 24.04 restricts exactly that by default
# (`kernel.apparmor_restrict_unprivileged_userns = 1`, confirmed on this box),
# so `bwrap` fails with "setting up uid map: Permission denied", xdg-dbus-proxy
# never starts, and WebKit responds with a `g_error` -- which is an ABORT.
#
# That matters far more than it looks: a `g_error` is not a Python exception.
# It is SIGTRAP, it kills the whole process, and NO try/except anywhere in
# this file can catch it. Every other failure mode here degrades to a hidden
# panel; this one would take the entire booth down at startup, in front of
# visitors, with nothing on screen. Fail-soft is not achievable by guarding
# -- only by never provoking it.
#
# Disabling that sandbox is defensible here specifically because of WHAT this
# WebView loads: one static, vendored, local page (see
# assets/tensix-viz/PROVENANCE.md), inlined into `about:blank`, that never
# navigates, never fetches anything, and never renders a single byte of
# remote or user-supplied content. The sandbox exists to contain hostile web
# content, and there is none for it to contain. (This is the same conclusion,
# for the same reason, that tt-local-generator's `app/main.py` reached.)
#
# MUST be set before `from gi.repository import WebKit` -- WebKit reads it
# when the web process is spawned, and importing it first would be too late.
# `setdefault`, not assignment, so an operator on a machine where the sandbox
# DOES work can leave it on by exporting the variable as "0".
os.environ.setdefault("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "1")

# WebKit is an OPTIONAL dependency, checked once at import. `venv-ui` on this
# development box has WebKit 6.0, but a booth image built elsewhere may not,
# and this module must degrade to an inert, hidden stub rather than taking the
# whole UI process down at import time.
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
    WEBKIT_AVAILABLE = True
except Exception:  # pragma: no cover - environment-dependent
    WebKit = None
    WEBKIT_AVAILABLE = False
    log.info("WebKit 6.0 is not available; the Tensix activity panel will be "
             "hidden (the booth is otherwise unaffected)")


# ── where things are ────────────────────────────────────────────────────────

# Resolved off THIS FILE, never off the process's working directory -- the
# same rule ui/playlist.py's own docstring explains has already cost this
# project once.
ASSETS_DIR = pathlib.Path(__file__).resolve().parent / "assets" / "tensix-viz"

# The driver's sysfs class directory. One `tenstorrent!N` entry per CHIP (this
# booth's two p300c boards present four of them). Reading these attributes is
# PASSIVE: it does not open the device, does not take a lock, and does not
# disturb another user's workload on the same silicon -- which is exactly why
# the telemetry here comes from sysfs and `tt-smi -s` rather than from
# anything that would need a device handle.
SYSFS_ROOT = pathlib.Path("/sys/class/tenstorrent")

# Above this many chips the panel draws only the first `MAX_CHIPS` and says
# so in its readout ("N/total"), so a bigger machine than this booth's QB2
# stays a legible corner instrument instead of a wall of postage stamps.
MAX_CHIPS = 4


# ── polling and staleness ───────────────────────────────────────────────────

# How often the panel re-reads every chip's AICLK, in milliseconds. Four
# sysfs reads per tick; at 1 Hz that is negligible next to the ONE-EVERY-TWO-
# SECONDS `tt-smi` subprocess the telemetry panel's sampler already runs
# (`ui/telemetry.py`'s `TelemetrySampler(period_s=2.0)` -- this comment said
# "2 Hz", which is the panel's REPAINT cadence, not the sample rate, and the
# help card had inherited the same 4x error). Deliberately slower
# than the animation itself -- tensix-viz animates continuously from its own
# rAF loop, and this only re-aims it.
POLL_INTERVAL_MS = 1000

# How long the panel will keep animating a fold's stage after the last stage
# update before falling back to `idle`.
#
# This exists so the panel cannot lie the way a frozen readout lies. A fold is
# ~4.4s warm (docs/followups.md) and emits several stage events inside that,
# so 15s is several missed folds -- but if the daemon dies, or the socket
# drops, or the booth is simply started with no `--socket` at all, the last
# thing this panel was told would otherwise animate "denoising" on screen
# forever, in front of a visitor, with nothing computing at all. The AICLK
# readout beside it stays honest either way (it is read from the driver, not
# from the wire); this makes the ANIMATION honest too.
STAGE_STALE_AFTER_S = 15.0

# Cap on JS queued while the WebView is not yet realized. `load_html` and
# `evaluate_javascript` are silent no-ops before realize, so calls are queued
# -- but a panel left running while never realized must not accumulate them
# forever. The first entry (the mode activation) is always kept; only stale
# telemetry is dropped.
_MAX_PENDING_JS = 32


# ── AICLK, normalised ───────────────────────────────────────────────────────

# Blackhole's AICLK is close to a two-state signal: ~800 MHz at rest, 1350 MHz
# boosted (measured on this box under load: all four chips at 1350). Scaling
# it from ZERO would leave every chip sitting at a constant ~0.6 and never
# visibly moving, so activity is normalised IDLE-RELATIVE -- 800 reads as 0.0,
# 1350 as 1.0 -- which is what makes the difference between a resting box and
# a working one visible at all. Same reasoning, and the same constants, as
# tt-local-generator's activity_viz.py.
AICLK_IDLE_MHZ = 800.0
AICLK_BOOST_MHZ = 1350.0


def chip_dirs():
    """Every Tenstorrent chip's sysfs directory, sorted.

    Sorted for a stable, meaningful order: `tenstorrent!0` .. `tenstorrent!3`
    is the same order `tt-smi -s` lists its devices in, which is the order the
    telemetry panel's cells appear in, which is the order these animations
    appear in. That correspondence is the whole reason this panel sits
    directly under that one.

    Returns `[]` on any OSError (no driver, no permission, no such path) --
    "there are no chips I can see" is a first-class answer here, not an
    error.
    """
    try:
        return sorted(SYSFS_ROOT.glob("tenstorrent!*"))
    except OSError:
        log.debug("could not enumerate %s; assuming no chips", SYSFS_ROOT,
                  exc_info=True)
        return []


def chip_count():
    """How many Tenstorrent chips this machine has. 0 when none/unreadable."""
    return len(chip_dirs())


def read_chip_clocks():
    """Every chip's AICLK in MHz, POSITION-ALIGNED with `chip_dirs()`.

    A chip whose clock cannot be read contributes `None` rather than being
    omitted, so index *i* always means chip *i*. Dropping it instead would
    silently shift every later chip's animation onto the wrong chip's clock
    -- the kind of wrong answer that still looks perfectly plausible on
    screen, which is the kind this project's own CLAUDE.md warns about.
    """
    clocks = []
    for chip_dir in chip_dirs():
        try:
            clocks.append(int((chip_dir / "tt_aiclk").read_text().strip()))
        except (OSError, ValueError):
            clocks.append(None)
    return clocks


def clock_activity(mhz):
    """One chip's AICLK -> an activity scalar in 0..1, idle-relative.

    See `AICLK_IDLE_MHZ`. Clamped at both ends, and a non-finite or
    non-numeric input reads as 0.0 rather than raising: this feeds an
    animation, and no possible sensor value may cost the booth an exception.
    """
    try:
        value = float(mhz)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    span = AICLK_BOOST_MHZ - AICLK_IDLE_MHZ
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, (value - AICLK_IDLE_MHZ) / span))


# ── which animation matches what the silicon is doing ───────────────────────
#
# tensix-viz ships nine modes (idle / inference / prefill / thinking / agents /
# diffusion / video / batch / explore / kernel_dispatch). Its own README is
# careful that these are VISUAL METAPHORS, not per-core traces, and this
# module inherits that honesty: the help card says "a picture of the work, not
# a per-core trace."
#
# The mapping is keyed on the fold's STAGE, not on the booth's screen, because
# the stage is the thing that describes what the chips are doing and the
# screen is not. The daemon folds continuously -- it starts fold N+1 the
# instant fold N finishes (see ui/app.py's module docstring, the whole reason
# the showcase dwell exists) -- so "the booth is showing a finished structure"
# and "the booth is showing a gallery" both routinely mean "the chips are
# mid-fold". Animating `idle` for those would be a straightforward lie, and
# the one thing this panel exists to say is that the hardware is really
# working.
#
# Per stage, against what that stage actually does (see
# ui/diagnostics.py's STAGE_TEACHING, which is written from runner/folder.py):
#
#   msa    -> idle       lining up evolutionary relatives: host-side search.
#   prep   -> idle       sequence/features become tensors: host-side layout.
#   trunk  -> thinking   10 refinement cycles of sustained, whole-grid matmul.
#                        tensix-viz marks `thinking` as its MOST accurate mode
#                        for exactly that ("sustained high matmul utilisation
#                        across all cores"), so this is the honest choice, not
#                        merely an evocative one.
#   diffusion -> diffusion   the booth's headline: ~200 denoising steps pulling
#                        a cloud of noise into atom coordinates. tensix-viz's
#                        `diffusion` mode is an expanding ring per denoising
#                        timestep -- the same shape as the collapse a visitor
#                        is watching on the left half of the screen, which is
#                        the one place mode and content genuinely rhyme.
#   confidence -> inference  a forward pass scoring the model's own answer.
#   saving -> idle       writing mmCIF: host-side file I/O.
#
# An unknown stage (a future protocol addition this build has never heard of)
# reads as `inference` -- something is running and we do not know what -- and
# never as `idle`, which would claim knowledge we do not have.
_MODE_BY_STAGE = {
    "msa": "idle",
    "prep": "idle",
    "trunk": "thinking",
    "diffusion": "diffusion",
    "confidence": "inference",
    "saving": "idle",
}

_UNKNOWN_STAGE_MODE = "inference"

# Human copy for the header, keyed by mode.
_MODE_CAPTION = {
    "idle": "idle",
    "inference": "scoring",
    "thinking": "refining",
    "diffusion": "denoising",
}


def viz_mode(state, stage):
    """The tensix-viz mode to animate, from the booth state and fold stage.

    Pure — no GTK, no sysfs — so the whole policy is testable directly.

    `state` gates only the two cases where the booth genuinely KNOWS nothing
    is folding:

    - `"preparing"`: the daemon has said `not_ready`; there is no fold.
    - `stage is None`: no fold has been seen at all — the booth was just
      started, or it is running with no socket, or the stage is stale (see
      `STAGE_STALE_AFTER_S`, applied by the caller).

    Every other state — attract, gallery, folding, showcase — passes straight
    through to the stage, because none of them tells you anything about the
    silicon. See the table above `_MODE_BY_STAGE`.
    """
    if state == "preparing":
        return "idle"
    if stage is None:
        return "idle"
    return _MODE_BY_STAGE.get(stage, _UNKNOWN_STAGE_MODE)


def mode_caption(mode):
    """The word the header shows for `mode` — the booth's own vocabulary
    ("denoising"), not tensix-viz's internal mode names, because the header
    is read by visitors who have just been told on the help card that the
    model works by denoising."""
    return _MODE_CAPTION.get(mode, mode)


def flow_params(activity, active):
    """Map an activity scalar (0..1) to tensix-viz's `setMemoryStats`
    parameters `(dram_bw, l1_fill, writeback)` — the density of read
    particles, the L1 fill level, and the density of writeback particles.

    Two regimes, and the floor in the first is the point: when a real mode is
    animating (`active`), guaranteeing a visible baseline of bidirectional
    flow is what stops a chip whose clock happens to be resting from looking
    switched off mid-fold. Load then intensifies it from there. When idle,
    flow tracks activity but stays quiet.

    Pure; rounded to 3dp so the generated JS is short and stable (a test can
    compare exact strings).
    """
    activity = max(0.0, min(1.0, float(activity)))
    if active:
        dram = 0.35 + 0.65 * activity
        l1 = 0.30 + 0.60 * activity
        writeback = 0.15 + 0.35 * activity
    else:
        dram = 0.05 + 0.45 * activity
        l1 = 0.10 + 0.30 * activity
        writeback = 0.10 * activity
    return (round(dram, 3), round(l1, 3), round(writeback, 3))


# ── layout ──────────────────────────────────────────────────────────────────

# Gap between canvases, px.
_GAP = 6

# How much horizontal room this panel actually has: the rail's fixed width
# (ui/app.py's `_SIDE_RAIL_WIDTH_PX`, 430) minus the rail's own 18px margins
# and this panel's 16px padding, both sides. Written out rather than imported
# from ui/app.py because importing the app module from a widget it builds
# would be a cycle; `tests/unit/test_chipviz.py` pins the two together so
# this cannot drift from the real rail width.
RAIL_INNER_WIDTH_PX = 430 - 2 * 18 - 2 * 16

# ONE ROW, one canvas per chip, sized to line each one up under its own cell
# in the telemetry panel directly above.
#
# The size is set by the rail, not by taste: the rail is a fixed 430px column
# (ui/app.py's `_SIDE_RAIL_WIDTH_PX`), which leaves 394px inside its margins
# and 362px inside this panel's own padding. Four canvases plus three gaps in
# 362px is 86px each, and that is the whole budget.
#
# A 2x2 grid at 178x140 -- big enough to watch individual Tensix cores, and
# what tt-local-generator's corner instrument uses -- was built and looked at
# first. It was rejected on the glass for two reasons: it is ~330px tall,
# which pushes the diagnostics panel off the bottom of a 1080p screen when a
# visitor opens it, and it destroys the left-to-right correspondence with the
# four chip readouts above, which is the single thing that makes this panel
# read as "these four chips, right here" rather than as generic decoration.
#
# At 86px a core is ~4px and you cannot follow one core. That is the accepted
# trade and it is the right one for a booth: this is read from a metre or two
# away, where what registers is "four separate things, all alive, moving
# differently" -- and the per-chip NUMBERS are already directly above it.
_CANVAS_W = 86
_CANVAS_H = 104


def grid_layout(chip_count_shown):
    """`(cols, canvas_w, canvas_h)` for `chip_count_shown` canvases.

    One row, one column per chip, up to `MAX_CHIPS`. Kept as a function (not
    three constants) so the caller reads the same way it would if this ever
    needed to wrap, and so the arithmetic that turns it into a pixel width is
    testable.
    """
    cols = max(1, min(int(chip_count_shown), MAX_CHIPS))
    return (cols, _CANVAS_W, _CANVAS_H)


def grid_width_px(cols, canvas_w):
    """Total pixel width of `cols` canvases at `canvas_w` with `_GAP`
    between them."""
    return cols * canvas_w + max(0, cols - 1) * _GAP


def readout_text(clocks, shown, actual):
    """The header's right-hand readout: the peak AICLK across the chips, plus
    a "shown/total" note when the panel is displaying fewer chips than the
    machine has.

    Returns an em dash when no clock could be read at all — the same "we do
    not know" register the telemetry panel uses, never a plausible-looking
    zero.
    """
    present = [c for c in clocks if c is not None]
    if not present:
        return "—"
    head = f"{int(max(present))} MHz"
    if shown < actual:
        return f"{head} · {shown}/{actual}"
    return head


# ── stylesheet ──────────────────────────────────────────────────────────────
#
# Same shape and the same brand constants as ui/panels.py and ui/app.py. The
# rule this project holds every label to -- an explicitly-set background
# implies an explicitly-set foreground, >= 4.5:1 -- applies here too, and
# `_BACKGROUND_BY_CLASS` is what lets the shared guard in
# tests/unit/_legibility.py find this panel's ground while walking a rail
# assembled from four different stylesheets.
_DARK_BASE = "#092221"
_BG = "#F1F8F8"
_BG_ALT = "#C7D9D8"

_BACKGROUND_BY_CLASS = {
    "chipviz-panel": _DARK_BASE,
}

_CHIPVIZ_CSS = f"""
.chipviz-panel {{
    background-color: {_BACKGROUND_BY_CLASS["chipviz-panel"]};
    padding: 10px 16px 12px 16px;
    border-radius: 6px;
}}
/* Same 10px letterspaced register as the telemetry panel's field labels, so
   the two read as one instrument stacked in two halves rather than as two
   unrelated widgets. `_BG_ALT` on `_DARK_BASE` measures 11.36:1. */
.chipviz-title {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: {_BG_ALT};
}}
/* The live clock. Monospace and brighter (`_BG`, 15.46:1) because it is a
   NUMBER that ticks -- same treatment, for the same reason, as the telemetry
   panel's hero readings: a proportional face makes a changing value visibly
   jitter column to column. */
.chipviz-readout {{
    font-family: "Berkeley Mono", monospace;
    font-size: 10px;
    font-weight: 600;
    color: {_BG};
}}
"""

_CSS_INSTALLED = False


def _ensure_css_installed():
    """Install `_CHIPVIZ_CSS` once, against the default display.

    Guarded on a display existing at all, exactly like ui/panels.py's and
    ui/app.py's installers, so constructing this panel never hard-requires
    one -- which is what lets the legibility tests build it directly with no
    main loop.
    """
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        log.debug("no default display; skipping chipviz CSS install")
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CHIPVIZ_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


def read_assets():
    """The vendored tensix-viz `(js, css)` source as text, or `(None, None)`.

    A missing or unreadable asset is a reason to hide this panel, never a
    reason to raise: `available` goes False and the booth carries on without
    it. See `assets/tensix-viz/PROVENANCE.md`.
    """
    try:
        return ((ASSETS_DIR / "tensix-viz.js").read_text(),
                (ASSETS_DIR / "tensix-viz.css").read_text())
    except OSError:
        log.warning("bundled tensix-viz assets could not be read from %s; the "
                    "Tensix activity panel will be hidden", ASSETS_DIR,
                    exc_info=True)
        return (None, None)


def build_page_html(js, css, chip_count_shown, canvas_w, canvas_h, arch="blackhole"):
    """The single self-contained page the WebView loads.

    Pure (a string in, a string out) so what actually gets rendered is
    testable with no WebKit and no display at all.

    A small facade on `window.__viz` carries three calls: `activate(mode)`
    fans a mode out to every chip (used once, at page load, to start
    everything idle), `activateChip(i, mode)` aims one at a single canvas --
    which is what makes "chip 0 is folding and the other three are not"
    drawable -- and `setChipStats(i, stats)` feeds PER-CHIP telemetry. Built by hand
    rather than via tensix-viz's own `CardViz`/`SystemViz` because those
    wrap and hide their inner `TensixViz` instances, and per-chip access is
    the entire point here: feeding all four canvases one averaged number
    would make this panel decoration, not an instrument.

    Every JS statement that touches the library is individually try/caught: a
    library that fails to construct one canvas must not take the other three
    down, and nothing here may ever put an error on the booth's screen.
    """
    # `activate` is staggered by 100ms per chip so the four grids do not pulse
    # in lockstep -- in lockstep they read as one big animation rather than as
    # four independent chips, which is the opposite of this panel's point.
    init = (
        "(function(){"
        "var host=document.getElementById('chips');"
        "if(!host){return;}"
        "window.__vizChips=[];"
        "for(var i=0;i<" + str(int(chip_count_shown)) + ";i++){"
        "var c=document.createElement('canvas');"
        "c.width=" + str(int(canvas_w)) + ";c.height=" + str(int(canvas_h)) + ";"
        "c.className='tv-chip-canvas';host.appendChild(c);"
        "try{window.__vizChips.push(new window.TensixViz(c,{arch:"
        + json.dumps(arch) + ",showMemory:true}));}catch(e){}}"
        "window.__viz={"
        "activate:function(m){window.__vizChips.forEach(function(v,i){"
        "setTimeout(function(){try{v.activate(m);}catch(e){}},i*100);});},"
        "activateChip:function(i,m){var v=window.__vizChips[i];"
        "if(v){try{v.activate(m);}catch(e){}}},"
        "setChipStats:function(i,s){var v=window.__vizChips[i];"
        "if(v){try{v.setMemoryStats(s);}catch(e){}}}};"
        "try{window.__viz.activate('idle');}catch(e){}"
        "})();"
    )
    cols = max(1, int(chip_count_shown))
    # `space-between`, not `center`: the WebView is given the panel's full
    # inner width, so spreading the canvases across it puts each one under
    # its own telemetry cell instead of huddling all four in the middle with
    # dead margins either side.
    grid_css = (
        "#chips{display:grid;grid-template-columns:repeat(" + str(cols) + ","
        + str(int(canvas_w)) + "px);gap:" + str(_GAP) + "px;"
        "justify-content:space-between;}"
    )
    # Content-Security-Policy: deny everything, then re-allow ONLY the two
    # things this page is actually made of.
    #
    # Why this is here at all: WebKit's own bubblewrap sandbox is disabled in
    # this process (see WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS at the top
    # of this module), and the argument for that being safe is "there is no
    # hostile content here to contain -- one vendored local page, no
    # navigation, no network". This header turns that from a property of what
    # we happen to load into one the engine enforces: every fetch/XHR,
    # WebSocket, image, font, frame and form submission is refused outright.
    #
    # Why NOT a bare `default-src 'none'`: this page's script and style are
    # INLINE (`<script>`/`<style>` below, because the assets are vendored and
    # inlined rather than fetched -- see assets/tensix-viz/PROVENANCE.md).
    # Under CSP, inline script and inline style each need their own explicit
    # 'unsafe-inline', and default-src 'none' alone would block both -- i.e.
    # it would silently blank this panel at the venue, which is the failure
    # this module spends most of its length avoiding. 'unsafe-inline' permits
    # exactly the bytes we put in the page ourselves; it does not permit a
    # single remote source, which is the property being bought here.
    csp = ("default-src 'none'; script-src 'unsafe-inline'; "
           "style-src 'unsafe-inline'")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"" + csp + "\">"
        "<style>html,body{margin:0;padding:0;background:" + _DARK_BASE
        + ";overflow:hidden}canvas{display:block}" + grid_css
        + (css or "") + "</style></head><body>"
        "<div id='chips'></div>"
        "<script>" + (js or "") + "</script>"
        "<script>" + init + "</script>"
        "</body></html>"
    )


class ChipVizPanel(Gtk.Box):
    """The rail widget: a header line above one tensix-viz canvas per chip.

    Construction never fails and never raises. If WebKit is missing, if there
    are no chips, or if the bundled assets cannot be read, `available` is
    False, the widget hides itself, and every method below becomes a no-op --
    the rail simply has one fewer thing in it. That is the whole fail-soft
    contract, and it is why the caller can append this unconditionally.

    Two independent controls, deliberately separate:

    - `set_mode(state, stage)` — which animation plays. Driven from booth
      events (see ui/app.py); pure policy lives in `viz_mode`.
    - `set_running(bool)` — whether the AICLK poll runs at all. This is the
      resource switch: stopped means zero timers, zero sysfs reads, zero JS.

    `clock` is injectable for the same reason ui/app.py's and
    ui/diagnostics.py's are: a test that asserts on staleness should not have
    to sleep through it.
    """

    def __init__(self, clock=time.monotonic):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        _ensure_css_installed()
        self.add_css_class("chipviz-panel")

        self._clock = clock
        self._webview = None
        self._pending_js = []
        self._poll_source_id = None
        self._running = False
        self._mode = "idle"
        self._stage = None
        self._stage_at = None
        self._state = None
        # Which chip index the daemon said it is folding on (`job_start`'s
        # `card`), or None for "nobody has told us yet" -- see
        # `set_folding_chip` for what each means on screen.
        self._folding_chip = None
        # What each canvas was last told to animate, so `_push_modes` can
        # skip the JS for canvases that have not changed. One entry per
        # canvas, all starting at the page's own initial `activate('idle')`.
        self._chip_modes = []

        # Honest chip count, capped for legibility. `_chip_actual` is what the
        # machine has; `_chip_shown` is how many we draw. The readout says
        # "N/total" whenever they differ, so the cap can never quietly
        # under-report the hardware.
        self._chip_actual = chip_count()
        self._chip_shown = min(self._chip_actual, MAX_CHIPS)
        self._chip_modes = ["idle"] * self._chip_shown

        cols, canvas_w, canvas_h = grid_layout(self._chip_shown)
        self._canvas_w, self._canvas_h = canvas_w, canvas_h

        # ── header: what it is (left) + the live peak clock (right) ────────
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._title_label = Gtk.Label(xalign=0.0, label="TENSIX ACTIVITY")
        self._title_label.add_css_class("chipviz-title")
        self._title_label.set_hexpand(True)
        header.append(self._title_label)
        self._readout_label = Gtk.Label(xalign=1.0, label="—")
        self._readout_label.add_css_class("chipviz-readout")
        header.append(self._readout_label)
        self.append(header)

        js, css = read_assets()

        # `available` is the single answer to "is this panel a real thing on
        # this machine". Three independent ways to be False, all of them
        # ordinary and none of them an error on screen.
        self.available = bool(
            WEBKIT_AVAILABLE and self._chip_shown > 0 and js is not None)
        if not self.available:
            log.info("Tensix activity panel unavailable (webkit=%s chips=%d "
                     "assets=%s); hiding it",
                     WEBKIT_AVAILABLE, self._chip_actual, js is not None)
            self.set_visible(False)
            return

        self._webview = WebKit.WebView()
        try:
            self._webview.get_settings().set_enable_javascript(True)
        except Exception:
            # An older/odd WebKit that will not hand over its settings object
            # still gets a page loaded; JS is on by default.
            log.debug("could not set WebView settings", exc_info=True)
        # A FIXED footprint that does not expand. The rail is a fixed column
        # (ui/app.py's `_SIDE_RAIL_WIDTH_PX`) and the protein is the hero: a
        # WebView allowed to expand would negotiate its way into the rail's
        # whole remaining height and push the diagnostics panel off the
        # bottom of the screen.
        self._webview.set_hexpand(False)
        self._webview.set_vexpand(False)
        self._webview.set_halign(Gtk.Align.FILL)
        # The page's own grid spreads the canvases across whatever width it
        # is given (`space-between`), so ask for the panel's full inner width
        # -- but never more than the rail can give, which is what
        # `grid_width_px` bounds and what test_chipviz.py asserts.
        self._webview.set_size_request(
            max(grid_width_px(cols, canvas_w), RAIL_INNER_WIDTH_PX), canvas_h)
        # load_html/evaluate_javascript before realize are silent no-ops, so
        # JS is queued until then (`_eval`) and flushed here.
        self._webview.connect("realize", self._on_realize)
        # Stop the poll when this widget goes away, unconditionally -- a
        # source left registered against a destroyed widget is exactly the
        # kind of thing that survives a booth's whole day.
        self.connect("unrealize", lambda *_a: self.set_running(False))
        try:
            self._webview.load_html(
                build_page_html(js, css, self._chip_shown, canvas_w, canvas_h),
                "about:blank")
        except Exception:
            log.warning("tensix-viz page failed to load; the panel will stay "
                        "blank", exc_info=True)
        self.append(self._webview)

    # ── the animation ───────────────────────────────────────────────────────

    def set_mode(self, state, stage):
        """Record what the booth is doing and re-aim the animation.

        Idempotent and cheap: if nothing about the resulting picture has
        changed, no JS is evaluated at all — which matters because
        ui/app.py calls this from every `stage` event.
        """
        if not self.available:
            return
        self._state = state
        if stage is not None:
            self._stage = stage
            try:
                self._stage_at = self._clock()
            except Exception:
                # A clock that raises must not cost the booth an event; the
                # worst case is that this stage never goes stale.
                log.exception("clock failed while stamping a stage")
        self._apply_mode(viz_mode(state, self._stage))

    def set_folding_chip(self, index):
        """Which chip the current fold is running on — `job_start`'s `card`.

        This is what stops the panel claiming four-way work when one chip is
        folding (whole-branch review, Critical 3). `index` is a chip index
        as the daemon reports it; `None` means "we have not been told", and
        is not the same thing as "no chip is folding":

        - a KNOWN index animates the fold's mode on that canvas alone and
          `idle` on the rest, which is what the hardware is actually doing
          (this phase's daemon is card-0 only — runner/daemon.py);
        - `None` falls back to animating every canvas in the mode, and the
          header omits the chip number so nothing on screen attributes the
          work to a particular chip. That window is transient in practice:
          the daemon has sent `card` on every `job_start` since Phase 3a, so
          this only covers "no fold has started yet" (where the mode is
          `idle` anyway) and a hypothetical daemon that stops saying.

        An index outside the canvases actually drawn (a fifth chip on a
        bigger machine than this booth's QB2 — see `MAX_CHIPS`) is stored,
        so no canvas claims the work, rather than being clamped onto chip 0.
        """
        if not self.available:
            return
        if index is not None:
            try:
                index = int(index)
            except (TypeError, ValueError):
                # Wire-shaped data: an unusable card index costs the
                # attribution, never an exception on the event path.
                log.debug("ignoring unusable folding chip index %r", index)
                index = None
        if index == self._folding_chip:
            return
        self._folding_chip = index
        self._push_modes()

    def _apply_mode(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        self._push_modes()

    def _push_modes(self):
        """Send each canvas the mode IT should be animating, and re-label.

        Only canvases whose mode actually changed get JS, so the common case
        (a stage event that changes nothing) costs nothing at all.
        """
        for index in range(self._chip_shown):
            wanted = self._mode_for_chip(index)
            if index < len(self._chip_modes) and self._chip_modes[index] == wanted:
                continue
            if index < len(self._chip_modes):
                self._chip_modes[index] = wanted
            self._eval("window.__viz&&window.__viz.activateChip(%d,%s)"
                       % (index, json.dumps(wanted)))
        self._title_label.set_label(self._title_text().upper())

    def _mode_for_chip(self, index):
        """`self._mode` for the chip doing the work, `idle` for the rest.

        With no known folding chip the mode goes to every canvas — see
        `set_folding_chip` for why that fallback is the honest one there.
        """
        if self._mode == "idle" or self._folding_chip is None:
            return self._mode
        return self._mode if index == self._folding_chip else "idle"

    def _title_text(self):
        """The header: what is running, and — when we know it — where.

        "TENSIX ACTIVITY · CHIP 0 · DENOISING" is the whole point: it says
        one chip is denoising, which is what the animation below it now
        draws and what the daemon is actually doing.
        """
        caption = mode_caption(self._mode)
        if self._mode == "idle" or self._folding_chip is None:
            return f"TENSIX ACTIVITY · {caption}"
        return f"TENSIX ACTIVITY · CHIP {self._folding_chip} · {caption}"

    def _stage_is_stale(self):
        """True once nothing has updated the stage for `STAGE_STALE_AFTER_S`.
        See that constant: this is what stops a dead daemon leaving a
        "denoising" animation running in front of a visitor."""
        if self._stage is None or self._stage_at is None:
            return False
        try:
            return (self._clock() - self._stage_at) >= STAGE_STALE_AFTER_S
        except Exception:
            log.exception("clock failed while checking stage staleness")
            return False

    # ── the poll (main loop only; no threads anywhere in this module) ───────

    def set_running(self, running):
        """Start or stop the AICLK poll. Idempotent both ways.

        Stopping REMOVES the GLib source rather than leaving it registered
        and returning early: a timer that fires every second all day to do
        nothing is exactly the sort of thing this project has already been
        bitten by once.
        """
        if running and not self.available:
            return
        if running:
            if self._poll_source_id is not None:
                return
            self._running = True
            self._poll_source_id = GLib.timeout_add(POLL_INTERVAL_MS, self._tick)
            # Paint one sample immediately rather than showing an em dash for
            # the first whole second after the booth opens.
            self._tick()
            return
        self._running = False
        if self._poll_source_id is not None:
            GLib.source_remove(self._poll_source_id)
            self._poll_source_id = None

    def _tick(self):
        """One poll: read every chip's clock, update the readout, feed each
        canvas its own flow. (The readout is the visible half; see the module
        docstring on what the per-chip flow does and does not do at this
        size.)

        Runs on the GTK main loop. The whole body is guarded with the
        `return True` OUTSIDE the try, for the reason ui/app.py's
        `_tick_state` spells out: this is a REPEATING source, and an
        exception escaping it removes the source permanently — a panel frozen
        for the rest of the day with nothing on screen saying so.
        """
        try:
            if self._stage_is_stale():
                # Let go of the stale stage entirely, so a later `set_mode`
                # with stage=None (or a reconnect) starts from a clean slate.
                self._stage = None
                self._stage_at = None
                self._apply_mode(viz_mode(self._state, None))

            clocks = read_chip_clocks()
            self._readout_label.set_label(
                readout_text(clocks[:self._chip_shown], self._chip_shown,
                             self._chip_actual))
            for index in range(self._chip_shown):
                mhz = clocks[index] if index < len(clocks) else None
                if mhz is None:
                    continue
                # Per chip, not per panel: the flow floor in `flow_params`
                # exists so a chip that is genuinely working never looks
                # switched off, and applying it to the three chips that are
                # NOT folding would be the same claim this panel just
                # stopped making with the animation mode.
                active = self._mode_for_chip(index) != "idle"
                dram, l1, writeback = flow_params(clock_activity(mhz), active)
                self._eval(
                    "window.__viz&&window.__viz.setChipStats(%d,"
                    "{dram_bw:%.3f,l1_fill:%.3f,writeback:%.3f})"
                    % (index, dram, l1, writeback))
        except Exception:
            log.exception("Tensix activity poll failed")
        return True

    # ── JS plumbing ─────────────────────────────────────────────────────────

    def _on_realize(self, _widget):
        pending, self._pending_js = self._pending_js, []
        for js in pending:
            self._eval_now(js)

    def _eval_now(self, js):
        try:
            # WebKit 6.0's signature: (script, length, world_name, source_uri,
            # cancellable, callback, user_data). Fire and forget.
            self._webview.evaluate_javascript(js, -1, None, None, None, None, None)
        except Exception:
            # Fail-soft by design: the animation just does not update this
            # tick. Debug, not warning -- at 1 Hz for a whole conference day a
            # warning-level log of a persistent failure is its own unbounded
            # resource.
            log.debug("tensix-viz JS evaluation failed", exc_info=True)

    def _eval(self, js):
        if self._webview is None:
            return
        if self._webview.get_realized():
            self._eval_now(js)
            return
        self._pending_js.append(js)
        if len(self._pending_js) > _MAX_PENDING_JS:
            # Keep the FIRST entry (the mode activation, which the animation
            # needs) and the most recent telemetry; drop the stale middle.
            self._pending_js = (self._pending_js[:1]
                                + self._pending_js[-(_MAX_PENDING_JS // 2):])
