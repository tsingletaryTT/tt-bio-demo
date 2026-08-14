"""The Tensix activity panel: one animated core grid per REAL chip.

What it is
----------
A small, optional instrument in the booth's right-hand rail that draws one
`tensix-viz` Tensix core-grid animation per Tenstorrent chip actually present
under `/sys/class/tenstorrent/`, with an animation mode chosen to match the
stage of the fold the booth is running. It sits directly beneath the
telemetry panel and uses the same left-to-right chip order, so the third
animation is the third chip's readout's animation.

It is CHROME: closed on every start, opened with `T` (ui/app.py's
`_TENSIX_KEYS`), and closed again by `Esc` or by five minutes of nobody
touching the booth. The protein is the hero and this is a decoration
attached to the booth's smallest claim, so it is asked for rather than
assumed. While closed it polls nothing and evaluates no JS at all --
ui/app.py's `_set_chipviz_visible` stops `set_running` with the panel.

Which animation runs when
--------------------------
One table, `_MODE_BY_STAGE`, is the whole answer to "when do we use what
viz", and `viz_mode` is the whole policy around it:

    msa, prep, saving  -> idle        host-side work: MSA lookup, input
                                      featurisation, writing the mmCIF. The
                                      chip genuinely is not folding.
    trunk              -> thinking    the pair/single trunk, reasoning about
                                      which residues touch which.
    diffusion          -> diffusion   the denoising sampler -- an expanding
                                      ring per timestep, the same shape as
                                      the collapse on the left of the screen.
    confidence         -> inference   a forward pass scoring its own answer.
    (anything else)    -> inference   an unknown stage means SOMETHING is
                                      running and we do not know what; it is
                                      never `idle`, which would claim
                                      knowledge we do not have.

`viz_mode` overrides that to `idle` in exactly two cases where the booth
genuinely knows nothing is folding: the `preparing` state, and no stage seen
(or a stale one) at all. Every other booth state passes straight through to
the stage, because none of them says anything about the silicon. The comment
above `_MODE_BY_STAGE` carries the per-row reasoning; this is the summary.

What is live, and what is not (measured, not assumed)
------------------------------------------------------
Say this accurately, because the whole booth's claim is "this is real":

- **The chip COUNT is live.** One canvas per real device under
  `/sys/class/tenstorrent/`. No chips, no panel.
- **The CLOCK READOUT is live and per-chip.** `_tick` reads every chip's
  `tt_aiclk` every second and shows the peak. On this booth's QB2 that reads
  1350 MHz, which is what the driver really reports.
- **The MODE is live**, driven by the fold's stage off the socket.
- **WHICH CHIPS are folding is live, and each one animates ITS OWN stage.**
  `job_start` carries the card index the daemon claimed (runner/folder.py)
  and every later `stage` event says what that chip's fold is doing, so
  ui/app.py keeps one stage per cell and hands the whole picture down as
  `set_chip_stages({card: stage})`. Each canvas animates the mode for its
  own chip's stage and `idle` for a chip with no fold -- so two chips in
  different stages animate differently, and a chip between folds is drawn
  resting, which is what it is.

  Read the history here before widening any claim in this file. The panel
  originally fanned ONE mode out to all four canvases while the daemon
  folded on card 0 alone (runner/daemon.py's `CardPool([config.device_id])`),
  i.e. it said four chips were working when one was; the whole-branch review
  called that Critical 3, and the fix was to say LESS -- name the one chip,
  draw the other three idle. Four chips genuinely fold at once as of Phase
  5, measured on this booth at 65.4-73.7 degC / 1337-1350 MHz / 72-91 W
  across all four against 12-17 W idle, which is what earns the fuller claim
  back. It is earned back exactly as far as it is true and no further: the
  header COUNTS the canvases that are actually animating work rather than
  asserting the chip count, so three chips folding says three and one still
  says which one.
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
applies to a decorative hardware animation in a fixed-width rail, one that
is now off by default and opened with a key:

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
how much of it there is, and which of it these folds are running on. Note
what that is NOT a claim of: it is a claim about the chips that are folding
*right now*, which on this booth is usually but not always four (a chip
between folds, or one whose stage has gone stale, is drawn resting and is
not counted in the header).

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
- The animation's frame budget is the page's, not the display's. A RESTING
  chip is drawn at `RESTING_ANIMATION_FPS`; the chip that is folding keeps the
  display's own rate. That is the flicker fix, and the measurement it was set
  against is written out above `RESTING_ANIMATION_FPS` -- read it before
  changing the number.
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

# How long the panel will keep animating a chip's stage after the last time
# that chip was told something new, before falling back to `idle`.
#
# This exists so the panel cannot lie the way a frozen readout lies. A fold is
# ~4.4s warm (docs/followups.md) and emits several stage events inside that,
# so 15s is several missed folds -- but if the daemon dies, or the socket
# drops, or the booth is simply started with no `--socket` at all, the last
# thing this panel was told would otherwise animate "denoising" on screen
# forever, in front of a visitor, with nothing computing at all. The AICLK
# readout beside it stays honest either way (it is read from the driver, not
# from the wire); this makes the ANIMATION honest too.
#
# PER CHIP, and measured from a genuine CHANGE rather than from the last time
# ui/app.py re-asserted the same thing. That distinction is what makes it
# independent: the app rebuilds the whole `{card: stage}` picture from its own
# per-cell cache on every event, so a stamp refreshed on every re-assertion
# would let three healthy chips keep a fourth, wedged one animating for the
# rest of the day on the back of their events. The cost of measuring from the
# change instead is that a single stage genuinely lasting longer than this
# stands its canvas down -- which on a booth whose folds are ~4.4s means
# something really has gone wrong, and standing down is the honest answer to
# that.
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


# ── how fast each animation is allowed to advance ───────────────────────────
#
# THE FLICKER FIX, and the measurement behind it. Read this before changing a
# number here.
#
# Symptom, as reported from the booth: "when the tensix viz is on there's a lot
# of flicker in their rendering area."
#
# What it was NOT (measured, by rendering the live WebView to a texture on
# every GTK frame and comparing pixels -- see this task's report): not the 1 Hz
# `_tick`. With the poll running and with it stopped the panel's pixel
# statistics are the same; there is no re-layout, no resize, no blanking, no
# white flash, and the whole-panel mean luminance holds to +/-0.3% across a
# 20-frame screen capture of the real booth.
#
# What it IS: the `idle` animation. tensix-viz's `idle` mode is a per-cell
# random pop -- `min(1, prev*0.9 + (rand < 0.03 ? rand*0.35 : 0))` -- evaluated
# once per `requestAnimationFrame` callback, i.e. once per DISPLAY frame. Two
# things multiply it into flicker here:
#
#   1. the loop is frame-rate-coupled, so on a 60 Hz panel each canvas starts
#      ~4 new pops every 17 ms (140 compute cells x 0.03), and each decays to
#      nothing in ~0.36 s;
#   2. `_drawHeatmap` normalises the grid by the frame's own maximum, so the
#      brightest cell is painted at full contrast no matter how small the
#      underlying activity is -- every fresh pop lands as a hard, near-white
#      4x7 px dot on a near-black ground.
#
# Measured over the four canvases, `idle`, ungoverned: 5135 pixel-brightenings
# per second and 8779 pixels/second changing by more than 20/255. The three
# modes with smooth, deterministic fields (`thinking`, `diffusion`,
# `inference`) are 10-30x quieter on the same metric -- which is why the panel
# flickers WORSE when the booth is resting than when it is folding.
#
# The fix is to own the frame budget rather than the library. The page this
# module builds installs a `requestAnimationFrame` governor (see
# `_FRAME_GOVERNOR_JS`) and this table tells it how fast each mode may run:
# a RESTING chip is drawn at a resting cadence, and the chip that is actually
# folding keeps the display's own rate so the diffusion ring and the trunk's
# wave stay at the cadence they were designed for. Same picture -- the steady
# population of lit cells is set by the pop rate over the decay per frame, so
# it does not change -- with the turnover slowed by 3x.
#
# Deliberately NOT done by editing the vendored library: see
# assets/tensix-viz/PROVENANCE.md ("Do not hand-edit these files"), which is
# also why the policy lives here in Python and reaches the page as data.
#
# 20 fps was chosen against the measurement, not by taste: it takes the idle
# metric from 5135 to 1201 brightenings/second (4.3x) while leaving pops
# visible individually. Below ~12 fps the return flattens out and each pop
# starts to read as a discrete blink rather than a twinkle.
RESTING_ANIMATION_FPS = 20

# 0 means "do not govern this mode at all" -- the display's own rate.
WORKING_ANIMATION_FPS = 0


def animation_fps(mode):
    """Frames per second `mode` may animate at; 0 for the display's rate.

    Pure, so the whole policy is testable without a WebView. Only `idle` is
    slowed: it is the one mode whose field is random per frame, it is what
    three of four canvases show throughout a fold (and all four when nothing
    is folding), and it is the measured source of the flicker.
    """
    return RESTING_ANIMATION_FPS if mode == "idle" else WORKING_ANIMATION_FPS


def animation_fps_table():
    """`animation_fps` as the lookup the page gets, including its default.

    `"*"` is the fallback for any mode not named -- a future tensix-viz mode,
    or one a later `_MODE_BY_STAGE` row asks for -- so an unlisted mode is
    ungoverned rather than accidentally frozen at somebody's leftover number.
    """
    modes = set(_MODE_BY_STAGE.values()) | {_UNKNOWN_STAGE_MODE}
    table = {mode: animation_fps(mode) for mode in sorted(modes)}
    table["*"] = WORKING_ANIMATION_FPS
    return table


# The governor itself. It wraps `requestAnimationFrame`/`cancelAnimationFrame`
# for the whole page and hands each animation chain its own budget:
#
# - `window.__vizRun(mode, fn)` runs `fn` (an `activate` call) with the chain
#   budget set from `window.__vizFps`. Because tensix-viz re-arms its loop by
#   calling `requestAnimationFrame` from INSIDE its own callback, and the
#   wrapper restores the budget around that callback, the rate propagates
#   down the whole chain without the library knowing anything about it.
# - a budget of 0 passes straight through on the next native frame, so an
#   ungoverned mode costs one extra function call per frame and nothing else.
# - ids are ours, and `cancelAnimationFrame` looks them up in the same map,
#   so the library's `reset()` still stops a chain dead. Dropping that would
#   leave two loops racing on one canvas after every mode change.
_FRAME_GOVERNOR_JS = (
    "(function(){"
    "var raf=window.requestAnimationFrame.bind(window);"
    "var caf=window.cancelAnimationFrame.bind(window);"
    "var live={};var next=1;var chain=0;"
    "function budget(m){var f=window.__vizFps[m];"
    "return f===undefined?window.__vizFps['*']:f;}"
    "window.__vizRun=function(m,fn){var prev=chain;chain=budget(m);"
    "try{fn();}finally{chain=prev;}};"
    "window.requestAnimationFrame=function(cb){"
    "var fps=chain;var id=next++;"
    "var due=performance.now()+(fps>0?1000/fps:0);"
    "function step(ts){"
    "if(!(id in live)){return;}"
    "if(!(fps>0)||performance.now()>=due-1){"
    "delete live[id];var prev=chain;chain=fps;"
    "try{cb(ts);}finally{chain=prev;}return;}"
    "live[id]=raf(step);}"
    "live[id]=raf(step);return id;};"
    "window.cancelAnimationFrame=function(id){"
    "if(id in live){caf(live[id]);delete live[id];}};"
    "})();"
)


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
# (ui/app.py's `_SIDE_RAIL_WIDTH_PX`, 552) minus the rail's own 18px margins
# and this panel's 16px padding, both sides. Written out rather than imported
# from ui/app.py because importing the app module from a widget it builds
# would be a cycle; `tests/unit/test_chipviz.py` pins the two together so
# this cannot drift from the real rail width.
#
# Conservative by one term: the rail's margins sit OUTSIDE its allocation,
# so a panel actually gets the full `_SIDE_RAIL_WIDTH_PX` less its own
# padding. Subtracting them anyway leaves this panel 36px of slack it never
# claims, which is the right direction for the one widget in the rail that
# must never be the thing setting the column's width.
RAIL_INNER_WIDTH_PX = 552 - 2 * 18 - 2 * 16

# ONE ROW, one canvas per chip, sized to line each one up under its own cell
# in the telemetry panel directly above.
#
# The size was set by the rail, not by taste: when this was chosen the rail
# was a fixed 430px column, which left 362px inside this panel's padding, and
# four canvases plus three gaps in 362px is 86px each.
#
# The rail is 552px now (ui/app.py's `_SIDE_RAIL_WIDTH_PX` -- it grew to hold
# the telemetry panel's reserved chip cells, see that constant), so there is
# slack in the budget for the first time. The canvases are deliberately NOT
# grown into it: they are square-ish because the Tensix core grid they draw
# is, and stretching them to 116px wide against the same 104px height would
# distort every core to buy nothing at this size. The page's own
# `space-between` spreads the four across whatever width it is given, which
# keeps them under their own telemetry cells to within ~20px -- the same
# correspondence, to the same tolerance, as before the rail changed.
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
        # The frame budget, as data, before anything can start a loop. See
        # `animation_fps` and `_FRAME_GOVERNOR_JS` -- this is the flicker fix.
        "window.__vizFps=" + json.dumps(animation_fps_table()) + ";"
        + _FRAME_GOVERNOR_JS +
        "window.__vizChips=[];"
        "for(var i=0;i<" + str(int(chip_count_shown)) + ";i++){"
        "var c=document.createElement('canvas');"
        "c.width=" + str(int(canvas_w)) + ";c.height=" + str(int(canvas_h)) + ";"
        "c.className='tv-chip-canvas';host.appendChild(c);"
        "try{window.__vizChips.push(new window.TensixViz(c,{arch:"
        + json.dumps(arch) + ",showMemory:true}));}catch(e){}}"
        "window.__viz={"
        "activate:function(m){window.__vizChips.forEach(function(v,i){"
        "setTimeout(function(){window.__vizRun(m,function(){"
        "try{v.activate(m);}catch(e){}});},i*100);});},"
        "activateChip:function(i,m){var v=window.__vizChips[i];"
        "if(v){window.__vizRun(m,function(){"
        "try{v.activate(m);}catch(e){}});}},"
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

    Three independent controls, deliberately separate:

    - `set_chip_stages({card: stage_or_None})` — WHICH chips are folding and
      what each one's own fold is doing. The mapping is the whole picture,
      not a patch on the last one: a chip left out of it is idle.
    - `set_state(state)` — the booth's own state. Only `preparing` means
      anything to the animation (see `viz_mode`); every other state says
      nothing about the silicon and is passed through.
    - `set_running(bool)` — whether the AICLK poll runs at all. This is the
      resource switch: stopped means zero timers, zero sysfs reads, zero JS.

    Plus `tick_staleness()`, called from ui/app.py's own 100ms tick: the
    explicitly-callable form of the check that stops a dead daemon leaving
    "denoising" animating in front of a visitor. See `STAGE_STALE_AFTER_S`.

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
        self._state = None
        # `{chip index: (stage, stamped_at)}` for every chip the booth
        # currently believes is mid-fold -- one entry per WORKING chip, so an
        # empty dict is "nothing is folding" and is drawn that way. The stamp
        # is the staleness clock and is refreshed only when that chip's stage
        # genuinely CHANGES; see `STAGE_STALE_AFTER_S` for why that, and not
        # every re-assertion, is what makes the check independent per chip.
        self._chip_stages = {}
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

    def set_state(self, state):
        """Record what the BOOTH is doing (not what the chips are).

        Only one value changes the picture — `preparing`, which is the
        daemon saying `not_ready`, i.e. there is no fold anywhere. Every
        other booth state passes straight through to each chip's own stage,
        because none of them says anything about the silicon; see `viz_mode`.

        Idempotent and cheap: no JS at all when nothing about the resulting
        picture changed, which matters because ui/app.py calls this from
        every single event.
        """
        if not self.available:
            return
        if state == self._state:
            return
        self._state = state
        self._push_modes()

    def set_chip_stages(self, stages):
        """Which chips are folding, and what each one's own fold is doing.

        `stages` is `{card index: stage}` as ui/app.py builds it from its
        per-cell state — `job_start`'s `card` for the key, that cell's latest
        `stage` event for the value — with `None` (or simple absence) meaning
        "this chip has no fold". It is the WHOLE picture and REPLACES the
        last one: a chip left out is stood down, because a mapping that
        merged would keep animating a fold that finished.

        This is what replaced `set_folding_chip`. That method aimed ONE mode
        at ONE canvas because one chip folded; four chips fold now, and
        fanning one stage across them would be the original Critical-3 lie
        with a different shape (see the module docstring). Each canvas gets
        the mode for its own chip's stage and nothing else.

        Everything here is wire-shaped, so nothing here may raise: this is
        reached from `_handle_event`. An unusable card index, an unusable
        stage, or an argument that is not a mapping at all costs the
        attribution and is logged at debug — never an exception on the event
        path, and never anything on the booth's screen.

        A card index outside the canvases actually drawn (a fifth chip on a
        machine bigger than this booth's QB2 — see `MAX_CHIPS`) is kept
        rather than clamped onto chip 0, so no canvas and no header claims
        work that belongs to a chip nobody can see.
        """
        if not self.available:
            return
        try:
            items = list(stages.items())
        except Exception:
            log.debug("ignoring unusable chip stage mapping %r", stages)
            return

        cleaned = {}
        for card, stage in items:
            if stage is None:
                continue
            if isinstance(card, bool):
                # `True` is an int and would land on chip 1. Nothing on the
                # wire should ever produce it, which is exactly why it is
                # worth refusing rather than silently attributing.
                continue
            try:
                cleaned[int(card)] = stage
            except (TypeError, ValueError):
                log.debug("ignoring unusable folding chip index %r", card)

        try:
            now = self._clock()
        except Exception:
            # A clock that raises must not cost the booth an event. The worst
            # case is that these stages never go stale, which is the same
            # failure the clock itself already represents.
            log.exception("clock failed while stamping chip stages")
            now = None

        refreshed = {}
        for index, stage in cleaned.items():
            previous = self._chip_stages.get(index)
            # The stamp moves only on a genuine change -- see
            # STAGE_STALE_AFTER_S for why re-assertion must not refresh it.
            if previous is not None and previous[0] == stage:
                refreshed[index] = previous
            else:
                refreshed[index] = (stage, now)
        self._chip_stages = refreshed
        self._push_modes()

    def tick_staleness(self):
        """Stand down any chip nothing has said anything new about for
        `STAGE_STALE_AFTER_S`. Returns whether anything changed.

        Called from ui/app.py's 100ms state tick rather than from this
        module's own 1 Hz poll, because the poll only runs while the panel is
        OPEN (`set_running`) and a panel reopened after a dead spell must not
        come back showing what it was told before the daemon went away.
        """
        if not self.available or not self._chip_stages:
            return False
        try:
            now = self._clock()
        except Exception:
            log.exception("clock failed while checking chip stage staleness")
            return False
        fresh = {index: entry for index, entry in self._chip_stages.items()
                 if entry[1] is None or (now - entry[1]) < STAGE_STALE_AFTER_S}
        if len(fresh) == len(self._chip_stages):
            return False
        log.info("Tensix activity: %d chip(s) stood down after %.0fs with no "
                 "stage update", len(self._chip_stages) - len(fresh),
                 STAGE_STALE_AFTER_S)
        self._chip_stages = fresh
        self._push_modes()
        return True

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
        """What canvas `index` should animate: the mode for THAT chip's own
        stage, or `idle` if that chip has no fold.

        The `viz_mode` call is what folds the booth state in, so `preparing`
        stands every chip down without this method knowing why.

        Guarded on `TypeError` for one specific reason: `stage` comes off the
        wire, `_MODE_BY_STAGE.get(stage)` is a dict lookup, and an unhashable
        value (a list, say) raises out of it. An unknown stage means
        SOMETHING is running and we do not know what, which is
        `_UNKNOWN_STAGE_MODE` — never `idle`, which would claim knowledge the
        booth does not have.
        """
        entry = self._chip_stages.get(index)
        if entry is None:
            return "idle"
        try:
            return viz_mode(self._state, entry[0])
        except TypeError:
            return _UNKNOWN_STAGE_MODE

    def _working_chips(self):
        """The DRAWN canvases that are animating work, in order.

        Drawn, not merely mapped: a card index the machine has no canvas for
        (see `set_chip_stages`) claims nothing, and mode, not mere presence,
        because `msa`/`prep`/`saving` are host-side stages where the chip
        genuinely is not folding.
        """
        return [index for index in range(self._chip_shown)
                if self._mode_for_chip(index) != "idle"]

    def _title_text(self):
        """The header: how much of this machine is working, and — when it is
        exactly one chip — which one and at what.

        Three shapes, and the reason there are three is the whole of this
        task. "TENSIX ACTIVITY · 3 CHIPS FOLDING" is a claim the booth can
        back up because the number is COUNTED off the canvases that are
        actually animating, not read off the chip count; "TENSIX ACTIVITY ·
        CHIP 2 · DENOISING" is the Critical-3 fix, still exactly as true as
        it was when one chip folded; and "TENSIX ACTIVITY · IDLE" claims
        nothing at all, which is the honest answer between folds.
        """
        working = self._working_chips()
        if not working:
            return "TENSIX ACTIVITY · idle"
        if len(working) == 1:
            index = working[0]
            caption = mode_caption(self._mode_for_chip(index))
            return f"TENSIX ACTIVITY · CHIP {index} · {caption}"
        return f"TENSIX ACTIVITY · {len(working)} CHIPS FOLDING"

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
