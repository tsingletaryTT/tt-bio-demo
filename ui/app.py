"""GTK application shell for the tt-bio demo -- the booth, assembled.

This module is the one place that knows about all of the others: the
viewer, the two panels, the gallery, the playlist, the telemetry sampler,
the socket client and the state machine. Its job is deliberately NARROW
and gets narrower as it gets longer -- it carries decisions out, it does
not make them:

- what the booth's state IS, and what each state means for the screen,
  belongs to `ui.states`;
- which CELL an event belongs to, whether that cell's dwell has expired,
  which cell the booth is following and what to say about a visitor's pick
  all belong to `ui.slots` (`SlotRouter`, `SlotState`, and the two
  predicates this module reads off a slot: `points_are_visible` and
  `ribbon_may_be_revealed`);
- what four folds on screen at once LOOK like belongs to `ui.quad`,
  including which cells are visible in the booth's default single-protein
  view (`QuadView.set_solo_mode`);
- what a panel looks like belongs to `ui.panels` (including the wire->
  within-stage fraction conversion, which is why this module calls
  `set_stage_from_wire` and never `set_stage`);
- how telemetry is sampled belongs to `ui.telemetry`;
- how a grid of targets is laid out belongs to `ui.gallery`;
- what a line of the diagnostics log SAYS belongs to `ui.diagnostics`
  (this file only decides when to feed it and when to repaint it).

If a booth decision ever appears in this file as an `if` on raw state, that
is the signal it belongs in one of those modules instead.

The three interactive surfaces (Task 10, extended)
---------------------------------------------------
The diagnostics panel (`D`), the Tensix activity panel (`T`) and the `?`
help card are CHROME, not booth state: they are laid over whatever
`ui.states` is doing, and none of them touches it. That is deliberate and
it is what lets `?` work "at any time" -- from attract, gallery, folding,
showcase or preparing -- without the state machine growing a sixth state
and every transition in it growing an opinion about overlays. What they do
borrow from the state machine is its principle that a visitor who walks
away must not leave the booth changed: `_tick_overlays` closes all three
after a period with no input at all (`_HELP_IDLE_S`,
`_DIAGNOSTICS_IDLE_S`, `_RAIL_PANEL_IDLE_S`).

All three start CLOSED on every run and nothing about them is persisted, so
a booth restarted at the venue comes up protein-first. The Tensix panel is
the newest of the three to be demoted to chrome: it shipped visible, and
it is the most eye-catching thing in the rail attached to the least
important claim -- see `_TENSIX_KEYS`.

The sequencing this file exists to get right
---------------------------------------------
Measured on hardware and independently reproduced: fold N's finished ribbon
lands AFTER fold N+1's `job_start`. The daemon does not wait for the UI --
it starts the next fold the moment the last one finishes -- so the arrival
order at this file's door is:

    job_done(N) ... job_start(N+1) ... frame(N+1) ... ribbon(N) ... frame(N+1) ...

The pre-Task-9 wiring reacted to each of those literally: `job_start(N+1)`
cleared the screen, then fold N's ribbon landed and cross-faded itself in
over fold N+1's opening noise cloud (three orders of magnitude wider than
the structure, so beyond the camera's far plane -- the ribbon faded in over
nothing), and the blend then STAYED at 1 until the fold after that, drawing
every remaining frame of fold N+1 at opacity 0. Only ~27% of each fold's
collapse ever reached a visitor's eye, and the finished structure held the
screen properly for ~21% of the cycle. For a demo whose entire premise is
"watch it fold", that was the headline defect.

The fix is the state machine's `showcase` dwell, carried out here in four
places, none of which decide anything on their own:

1. `job_start` never clears the screen (see the next section -- this used
   to be "does not clear DURING A SHOWCASE, defers it until the dwell
   expires", which is the half-fix that left the viewer empty).
2. Point frames arriving during a cell's showcase are SUPPRESSED, not drawn
   -- and not discarded either: they stay in that job's latest-wins slot
   (`ui.client.LatestFrameByJob`), which is what lets step 3 cut straight to
   live diffusion with no blank gap. Suppressing them is also what keeps
   the cross-fade honest: the points still on screen underneath the
   arriving ribbon are fold N's OWN final cloud, so the structure
   condenses out of the cloud it actually came from.
3. When a cell's dwell expires, that cell's buffered frame is drained, so
   the next fold's diffusion appears in the same instant the structure
   stops being that cell's subject.
4. A ribbon that arrives after its own cell's dwell has expired is dropped
   (`SlotState.ribbon_may_be_revealed`): by then the cell is showing the next fold's
   live diffusion, and cross-fading the previous structure over it is the
   same defect by another route.

`_SHOWCASE_DWELL_S` below carries the measurement that sets the trade.

Why the viewer is never cleared until it has something to replace with
-----------------------------------------------------------------------
The sequencing above was tuned against Trp-cage, a 4.4s fold. It does not
survive the long targets, and the way it fails is the whole reason this
section exists.

Only the `diffusion` stage emits `frame` events. `msa`, `prep` and `trunk`
emit progress and no coordinates at all, and trunk is ten refinement cycles
-- about 15 seconds on a 223-residue target. Measured warm fold times on
this booth: Trp-cage 4.4s, FKBP12 11.7s, DHFR 19.7s, trypsin 22.3s. So for
three of the four shipped targets, most of the fold produces nothing to
draw. Scanning a 91-second recording of the real booth frame by frame: 20
of 45 sampled seconds had an EMPTY viewer, and across four proteins there
was exactly one frame range showing a finished structure.

Clearing the screen on `job_start` (immediately, or deferred to the end of
a 2s dwell -- the difference is 2 seconds out of 15) is what produced that.
The `_SHOWCASE_DWELL_S` hold was a fixed budget tuned to one target: 2s out
of a 4.4s Trp-cage cycle is a fair share, 2s out of a 22.3s trypsin cycle
is a glimpse.

So the clear moved. It now happens in `_drain_frames`, at the instant the
new fold's first real frame is about to be drawn -- never before. The
previous structure keeps rotating through msa/prep/trunk and is replaced
the moment there are genuine new coordinates to replace it with. The hold
stops being a fixed number of seconds and becomes "until superseded", which
scales with fold length by construction instead of being tuned to one
target.

What this deliberately does NOT do is invent anything. Nothing is
interpolated, no motion is synthesised, no placeholder geometry is built
for a stage that produced no coordinates. The only thing on screen is a
structure that was really computed -- just an older one, and it is
`set_held` (ui/viewer.py, which dims it) plus the caption overlay built by
`_build_viewer_caption` below (which names it in words) that keep a visitor
from reading it as the fold currently in progress. The pipeline panel is
already reporting the live stage of the NEW fold next to it, so the two
must not be allowed to look like the same claim.

`awaiting_first_frame` is the flag that carries all of this, and it is
cleared by exactly three things: the new fold's first frame landing
(superseded, the ordinary path), `job_error` (that fold will never produce
one), and `not_ready` (the daemon has stopped folding entirely). The last
two matter because the caption asserts "now folding X" -- an assertion that
must stop the moment it stops being true, or a dead daemon leaves a stale
structure under a permanent lie.

Four folds, four of everything that was one
--------------------------------------------
The booth folds on every chip it has. So each of the fields the two
sections above describe is now held PER CELL, in `_SlotView` below, and the
routing decision -- which cell does this event belong to -- is made once, by
`ui.slots.SlotRouter`, and carried out here. There is deliberately no
`self.viewer`: an alias for "the one viewer" is where four folds quietly
become one again, and every one of these fields is one a second fold could
otherwise clobber:

  * the frame buffer (`ui.client.LatestFrameByJob`, one slot per `job_id` --
    a single one would show whichever fold was fastest in all four cells);
  * the ribbon generation counter, keyed per cell, so a `job_done` on chip 3
    cannot mark chip 0's in-flight ribbon build stale;
  * `awaiting_first_frame` / `current_job_id` / `shown_target_id` and the
    dim-and-caption decision they drive;
  * the showcase dwell itself, which lives in `ui.slots.SlotState` -- cell 1
    is mid-diffusion while cell 0 holds a finished structure, and a global
    dwell would suppress frames in all four.

What stays global is what is genuinely about the BOOTH: `ui.states`'s state
machine (attract, gallery, preparing), the idle timeout, the overlays, the
telemetry, the diagnostics log, and the visitor's one pick.

The default view is still ONE protein, large: `QuadView` starts in solo mode
showing the focused cell, and `Q` (`ui.quad.QUAD_KEYS`) turns the other
three on. Both are the same four viewers.
"""

import argparse
import collections
import logging
import pathlib
import sys
import threading
import time
import uuid

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk

from protocol.events import STAGE_BANDS, STAGE_ORDER, unpack_coords
from ui.chipviz import ChipVizPanel
from ui.client import EventClient, LatestFrameByJob
from ui.diagnostics import KIND_MARK, DiagnosticsLog, DiagnosticsPanel
from ui.gallery import Gallery
from ui.geometry import PLDDT_STOPS, ribbon_from_cif
from ui.structure_view import structure_mesh
from ui.panels import PipelinePanel, TelemetryPanel
from ui.playlist import PlaylistError, load_playlist, select_targets
from ui.attract import (CLOSE_DIAGNOSTICS, CLOSE_TENSIX, HIDE_GALLERY,
                        OPEN_DIAGNOSTICS, OPEN_TENSIX, SHOW_GALLERY,
                        Choreography)
from ui.quad import QUAD_HELP_LINE, QUAD_KEYS, QuadView
from ui.slots import MAX_SLOTS, SlotRouter
from ui.states import BoothState, StateMachine
from ui.telemetry import TelemetrySampler
# Not `ui.mark` any more: the easter egg's geometry runs on the chips now, so
# the module the descent is defined in has to be importable from the runner's
# venv too and moved to the repo root beside `protocol/`. See mark.py's own
# docstring; nothing about what it computes changed with the move.
from mark import BRAND_PURPLE, POINTS as MARK_POINTS, MarkCondensation
from ui.viewer import StructureViewer   # the easter egg's own viewer; the
                                        # folds' four live in ui/quad.py

log = logging.getLogger(__name__)

# Operator-neutral copy for the "preparing" overlay. The `missing` list from
# a not_ready event names real filesystem paths and model/config detail --
# useful to an operator reading the log, meaningless (and a mild information
# leak) to a visitor reading the screen. This string is the only thing that
# may ever reach display_message for that state; it never gets composed from
# `missing` in any way.
_PREPARING_MESSAGE = "Getting the booth ready. Please check back shortly."

# ── what the booth says about a visitor's pick ──────────────────────────────
#
# One line across the whole quad (ui/quad.py's notice row), between the tap
# and the fold that answers it. It exists because of one specific failure: a
# visitor taps, four chips are mid-fold, and for twenty seconds the screen
# says nothing -- at which point the booth reads as broken. So this goes up at
# TAP TIME and waits on nothing: `SlotRouter.pick_status` decides which of
# these to show, off the router's own knowledge, with no daemon involved.
#
# Every word here is constrained, and the constraints are not stylistic:
#
# - it says what the booth DOES -- that protein goes next -- and never
#   promises "instantly" or "now". With four chips busy the pick starts when
#   one frees, usually within seconds, and a claim of instant is a claim the
#   booth breaks in front of the person most likely to be watching for it;
# - the longer wait explains itself as the feature it is. A pick waits at the
#   head of the daemon's queue and never pre-empts a running fold (Task 9's
#   ruling: tearing a fold down mid-device-operation is a documented
#   instability source, and pre-empting blanks a cell somebody is watching).
#   That is also exactly why the other three cells keep moving, so saying it
#   is what makes a few seconds read as deliberate rather than broken;
# - no error text, no version numbers, no paths, ever -- a `send_pick` that
#   fails goes to the log and the diagnostics rail and changes nothing here,
#   because a socket being down is not the visitor's problem and is not
#   something they can act on.
#
# The two lines are deliberately worded so that only the SECOND one talks
# about the folds already running. That is what makes it more informative
# rather than merely louder -- and it is what lets a test tell the two apart
# by their content instead of by identity.
_PICK_QUEUED_NOTICE = "NEXT UP: {name} — starting on the next free chip"
_PICK_WAITING_NOTICE = ("NEXT UP: {name} — letting the folds already running "
                        "finish first")

# ── the held-structure caption ──────────────────────────────────────────────
#
# Two lines over the 3D view, shown only while the viewer is holding
# something the running fold has not yet superseded (see the module
# docstring). Every string here is an assertion about what is on screen and
# what the silicon is doing, so each one is written to be true in the exact
# situation it appears in and in no other:
#
# * The empty case is the very first fold after launch -- there is genuinely
#   nothing to hold, and a bare black field says nothing at all. It tells a
#   visitor what is happening and, specifically, WHEN the view will fill,
#   which is the one question an empty screen provokes. "Atoms" is literal:
#   `dump_fn` streams per-step atom coordinates, and only the diffusion
#   stage produces them (protocol/events.py's STAGE_ORDER).
#
# * The held case names the fold that is over and the fold that is running,
#   in that order, one per line. "Previous fold" rather than "finished
#   structure" because what is being held is not always a finished ribbon:
#   if a CIF fails to parse (or a slow ribbon build misses its dwell) the
#   thing on screen is that fold's last diffusion frame, which is a real
#   computed state and genuinely the previous fold, but is not a finished
#   anything. One phrase that is true for both beats two phrases where the
#   wrong one can be shown.
#
# Neither line is ever composed from an exception, a path, or any other
# runner-supplied detail -- only from a target name (or, failing that, no
# name at all). The no-name fallbacks exist because `target_id` is wire
# data: a daemon folding something that is not in this booth's playlist
# must degrade to a caption that claims less, never to a caption that
# claims something false.
_CAPTION_EMPTY_SUB = "Atoms appear here when the diffusion stage begins"

# The same promise, for one quad cell, and shorter because it has to share a
# single small line with a protein name and a stage rather than owning a
# two-line notice under a full-width render.
#
# WHY A CELL NEEDS ITS OWN. On a chip's FIRST fold there is no previous
# structure to hold, so that cell is genuinely black through `msa`, `prep`
# and `trunk` -- up to fifteen seconds for a large protein -- with only a
# name and a stage on it. The booth-wide notice row cannot cover this: it is
# ONE line for FOUR independent folds, so it can only ever speak for one of
# them, which is why this was left open when the quad shipped (CLAUDE.md).
# A visitor looking at a black rectangle captioned "Trypsin · TRUNK" has
# every reason to think that cell is broken.
_CELL_EMPTY_SUB = "atoms appear when diffusion begins"

# The stages that produce no coordinates, so a cell with nothing in it stays
# black through them. Derived from STAGE_BANDS rather than listed, so a new
# stage inserted before diffusion cannot silently stop explaining itself.
_STAGES_BEFORE_COORDINATES = tuple(
    name for name in STAGE_ORDER
    if STAGE_BANDS[name][1] <= STAGE_BANDS["diffusion"][0])


def viewer_hold_caption(*, awaiting_first_frame, has_structure, showcasing,
                        folding_name, held_name):
    """What the viewer should say about what it is showing, as
    `(title, subtitle)` -- or None for "say nothing".

    Pure, and separated from the widget for the usual reason this file
    separates those: what the booth CLAIMS is a decision, and it is
    testable with no display, no GTK and no fold. `_sync_viewer_hold` below
    is the only caller and does nothing but carry the answer out.

    The four arguments are the whole of the situation:

    `awaiting_first_frame` -- a fold is running and has not yet produced a
    single coordinate. False means either nothing is folding or the fold
    that is running already owns the screen; in both cases the viewer is
    showing the current thing and has nothing to explain. This is what makes
    a `job_error` or a `not_ready` (both of which clear the flag) take the
    "now folding X" claim down with them rather than leaving it up over a
    daemon that has stopped.

    `showcasing` -- the finished structure is inside its guaranteed dwell
    (`_SHOWCASE_DWELL_S`). Deliberately silent then, and undimmed: that hold
    is the payoff the whole booth is built around, the structure IS the
    current subject for those two seconds, and a caption demoting it to a
    leftover the instant it finishes fading in would be both ugly and
    premature. The caption appears exactly where the old code went black.

    `has_structure` -- whether the viewer has anything in it at all, which
    picks between the two genuinely different situations: explaining a held
    leftover, and explaining an empty screen on the first fold of the day.
    """
    if showcasing or not awaiting_first_frame:
        return None
    if not has_structure:
        return (f"Folding {folding_name}" if folding_name else "Folding",
                _CAPTION_EMPTY_SUB)
    return (f"Previous fold: {held_name}" if held_name else "Previous fold",
            f"Now folding {folding_name}" if folding_name
            else "Now folding the next target")


# ── the protein caption, under the render ───────────────────────────────────
#
# A visitor used to get a shape and a name and learn nothing. This is the
# line or two that says what the molecule actually IS.
#
# HOW THIS COEXISTS WITH `viewer_hold_caption` ABOVE
#
# The two never make competing claims, because each names its own subject
# and each answers a different question:
#
#   the hold caption   sits at the TOP of the hero slot, appears only during
#                      the silent stages of a new fold, and answers "which
#                      molecule am I looking at, and which one is being
#                      computed?" -- Previous fold: X / Now folding Y.
#   this caption       sits BELOW the render, is always up, and answers
#                      "what IS the molecule in this picture?"
#
# So this one follows the structure ON SCREEN (`shown_target_id`), not the
# fold in flight. That is what makes them agree rather than fight: during
# the hold window the picture is still X, so this caption keeps describing
# X while the top caption announces that Y is coming; then both switch
# together on the new fold's first frame, which is also the moment the
# picture itself changes. Binding this line to the fold in flight instead
# would put a description of Y directly underneath a picture of X -- the
# exact confusion the top caption exists to prevent.
#
# The fallback to `folding_target_id` covers the first fold of the day,
# when nothing has been shown yet and the booth is drawing an empty viewer:
# there is no picture to contradict, and naming what is coming beats naming
# nothing.


def target_info_subject(*, shown_target_id, folding_target_id):
    """Which target the caption under the render describes.

    Pure and id-only, so the choice is testable with no display and no
    playlist -- the same split `viewer_hold_caption` uses. See the block
    above for why "what is on screen" wins over "what is being folded".
    """
    return shown_target_id or folding_target_id


# ── one cell's caption ──────────────────────────────────────────────────────
#
# Two words at most, under the chip label, inside a quarter of the hero
# image. It says what that chip is folding and what it is doing to it, and it
# is the only per-cell text on screen -- so it is also the only place a
# runner-supplied string could reach a visitor. It cannot: the NAME comes
# from this booth's own playlist (`_target_name` returns None rather than a
# raw wire id), and the STAGE is looked up in `STAGE_ORDER` and dropped if it
# is not there. Nothing else from the wire is interpolated, ever -- a
# `job_error`'s `message` least of all.


def cell_caption(*, name, stage, showing=None, empty=False):
    """What one quad cell says about its fold, as a single line.

    `showing` is the protein whose geometry is ACTUALLY on screen in this
    cell, when the caller knows it and it is not `name`. Omitted (or equal to
    `name`) it changes nothing, which is the common case: once diffusion
    starts, the structure being drawn is the current fold's own.

    Pure, for the reason this file keeps making the same split: what the
    booth claims is a decision and must be testable with no display. `""`
    means "say nothing", which is what `QuadView.set_caption` renders as an
    empty caption rather than as a blank claim.
    """
    parts = []
    if showing and name and str(showing) != str(name):
        # THE CELL IS DRAWING SOMETHING ELSE, and must say which.
        #
        # Only `diffusion` produces coordinates, so a cell spends the whole of
        # `trunk` showing the PREVIOUS fold on that chip rather than going
        # black (the hold-until-superseded behaviour -- see this module's
        # second docstring section). Captioning that with the INCOMING fold's
        # name put "Dihydrofolate Reductase · TRUNK" under a picture of
        # trypsin, which is the exact mistake `ui/quad.py`'s module docstring
        # refuses for the notice row: a line that "would label whatever cell 0
        # actually IS folding with the wrong protein's name".
        #
        # Solo view has the notice row to disambiguate ("Previous fold: X /
        # Now folding Y"). A quad cell has ONE line and four cells need four
        # different answers, so the line carries both: what is drawn first,
        # because that is what a visitor is looking at, then what the chip has
        # moved on to.
        parts.append(f"{showing} — now folding {name}")
    elif name:
        parts.append(str(name))
    if stage in STAGE_ORDER:
        parts.append(stage.upper())
    line = " · ".join(parts)
    # NOTHING DRAWN YET, AND A REASON FOR IT. Gated on the STAGE alone, not
    # on having a name: a stage only arrives for a fold that is running, so
    # an idle cell (stage None) still says nothing and cannot promise atoms
    # that are not coming -- while a fold whose `target_id` is not in this
    # booth's playlist, and therefore has no name to show, still explains its
    # own blackness. Requiring a name suppressed the explanation exactly
    # where the booth knows least, which is backwards.
    #
    # Before diffusion only: once points arrive the cell explains itself.
    if empty and stage in _STAGES_BEFORE_COORDINATES:
        return f"{line} — {_CELL_EMPTY_SUB}" if line else _CELL_EMPTY_SUB
    return line


class _SlotView:
    """The app's half of one cell: what it is showing and what it awaits.

    `ui.slots.SlotState` owns the fold's own state -- its phase, its dwell,
    the two events deferred behind that dwell -- and is pure by construction.
    This holds the things that only mean anything next to a widget, and it
    exists because every field on it used to be a single scalar on `DemoApp`
    that four folds would have taken turns overwriting:

    `awaiting_first_frame` / `current_job_id` / `current_target_id` /
    `shown_target_id` / `has_structure` are the hold-until-superseded
    bookkeeping (see the module docstring's second section), per cell.

    `ribbon_generation` / `pending_ribbon` are the ribbon worker's, per cell.
    Keyed on the SLOT and not on the job id: the question a worker asks when
    it finishes is "is my fold still the newest one THIS CELL knows about",
    and a job-keyed counter would answer a different question -- one whose
    answer is always yes, since a job is only ever its own newest.

    `stage` is what this cell's fold is doing RIGHT NOW -- the last `stage`
    event routed to it, cleared when its fold starts, finishes or fails. It
    is per cell for the same reason everything else here is: four chips fold
    at once, and one booth-wide stage would animate whichever fold spoke last
    on all four of the Tensix panel's canvases (see `_chip_stages`).
    """

    __slots__ = ("awaiting_first_frame", "current_job_id", "current_target_id",
                 "shown_target_id", "has_structure", "ribbon_generation",
                 "pending_ribbon", "stage")

    def __init__(self):
        self.awaiting_first_frame = False
        self.current_job_id = None
        self.current_target_id = None
        self.shown_target_id = None
        self.has_structure = False
        self.ribbon_generation = 0
        self.pending_ribbon = None
        self.stage = None


# ── booth timing ────────────────────────────────────────────────────────────
#
# How long a finished structure holds the screen. `ui.states` defaults this
# to 3.0s; the booth passes its own value, and the difference is not a
# style preference -- it is the one number that sets how the cycle is split,
# so it is set here against a measurement rather than inherited.
#
# The arithmetic that produced the 3.0s default assumed a SERIAL cycle: one
# 4.4s fold, then a 3.0s look at the result, so 3.0/(4.4+3.0) = 41% of the
# loop showing the finished structure. The daemon does not work that way. It
# starts fold N+1 the instant fold N finishes and never pauses, so the cycle
# stays 4.4s and the dwell does not extend it -- it DISPLACES live diffusion,
# second for second. Measured against the recorded real fold replayed
# back-to-back (tests/fixtures/streams/real_fold_trpcage.jsonl, a 3.69s
# cycle carrying 30 diffusion frames), the split per cycle is:
#
#     dwell    frames of the collapse seen     cycle spent on the structure
#     3.0s     4 / 30   (13%)                  81%
#     2.0s     14 / 30  (47%)                  54%
#     1.5s     17 / 30  (57%)                  41%
#
# 3.0s would leave LESS of the collapse visible than the defect this task
# exists to fix (~27%), which is the wrong direction for a booth whose whole
# premise is watching it fold. 2.0s is the balance point: both halves of the
# cycle are substantial, and 2.0s is a hard floor of FULLY VISIBLE time --
# it is measured from the reveal, not from `job_done` (see
# `StateMachine.on_structure_revealed`), so unlike the ~1.2s the old
# sequencing left on screen it is not eaten by the ribbon build or the 0.8s
# cross-fade.
#
# What this number no longer sets: how long the finished structure STAYS ON
# SCREEN. That used to be the same question, and the arithmetic above only
# ever worked for Trp-cage -- against a 22.3s trypsin fold, 2.0s is under
# 10% of the cycle and the remaining 20 seconds were spent looking at black
# (see the module docstring). The structure now holds the screen until the
# next fold's first real frame supersedes it, which for a long target is
# fifteen seconds or more and needs no tuning at all.
#
# So this is now a FLOOR on two much narrower things, both of which still
# want a fixed number: how long the hero image is the booth's undimmed,
# uncaptioned subject before it is demoted to a held leftover, and how long
# the next fold's opening noise cloud is kept off the screen so it cannot
# be drawn over a structure a visitor is still looking at. The 2.0s
# measurement above is still the right answer to both.
_SHOWCASE_DWELL_S = 2.0

# How often the booth hands the state machine a clock reading. The machine
# owns no timer of its own by design (ui/states.py), so this source is the
# only thing that can end a showcase dwell or fire the 45s idle timeout --
# at 100ms it costs nothing and quantizes both by at most a tenth of a
# second.
_STATE_TICK_MS = 100

# Frame drain cadence: ~30Hz, matching the viewer's own render rate. The
# buffer is latest-wins, so a slower drain loses frames rather than falling
# behind.
_FRAME_DRAIN_MS = 33

# Telemetry repaint cadence. The sampler polls tt-smi every 2s on its own
# thread; repainting at 500ms means a new reading reaches the screen well
# within one sample period without the panel rebuilding its chip cells 30x a
# second for data that changes twice a second.
_TELEMETRY_REPAINT_MS = 500

# ── layout ──────────────────────────────────────────────────────────────────
#
# The side rail is a FIXED column, never a greedy one. Without an explicit
# width and hexpand(False), the panels inside it negotiate their way to two
# thirds of the window and the protein -- the reason anyone stopped to look
# -- ends up squeezed into a corner. Verified on real glass with the
# throwaway composed-screen harness that preceded this file (see
# .superpowers/sdd/2026-08-12-ui-panels/booth-composed.png).
#
# This number is a FLOOR, not a ceiling: GTK gives a widget its own minimum
# whenever that is larger than its size request, so the rail is only as
# fixed as the panels inside it are. It was 430 and it did NOT hold -- the
# telemetry panel's minimum tracked the text in its chip cells, so the rail
# stood at 430 until the first `tt-smi` sample landed, snapped to 531, and
# would have gone to 595 the moment a chip read three digits of degrees.
# That measured lurch is the "jerk on state change" this value now fixes,
# together with ui/panels.py's reserved footprint (see the long block above
# `CHIP_CELL_WIDTH_PX`, which is where the real work is).
#
# 552 is what the reservation costs, exactly: `MAX_CHIP_CELLS` (4) cells of
# `CHIP_CELL_WIDTH_PX` (130) plus the telemetry panel's own 2x16px padding.
# It is ~20px wider than the 531 the booth was ALREADY rendering all day
# with four chips live, so the steady-state screen barely changes -- what
# changes is that it now renders that width in every state, including the
# ones it used to jump between. `tests/unit/test_panels.py` pins the
# arithmetic so a wider cell cannot silently reintroduce the lurch.
_SIDE_RAIL_WIDTH_PX = 552


class _PinnedNaturalBoxLayout(Gtk.BoxLayout):
    """A `Gtk.BoxLayout` that reports its natural WIDTH as its minimum.

    The override has to live on the layout manager, not on the widget:
    `gtk_widget_measure` delegates to the layout manager whenever a widget
    has one, and never calls the widget class's own `measure` vfunc. A
    `do_measure` on a `Gtk.Box` subclass is therefore dead code -- verified
    by measuring, before this class existed, that overriding it changed the
    rail's reported natural width by exactly nothing.
    """

    def do_measure(self, widget, orientation, for_size):
        minimum, natural, min_baseline, nat_baseline = Gtk.BoxLayout.do_measure(
            self, widget, orientation, for_size)
        if orientation == Gtk.Orientation.HORIZONTAL:
            natural = minimum
        return minimum, natural, min_baseline, nat_baseline


class _FixedWidthBox(Gtk.Box):
    """A box whose NATURAL width is pinned to its minimum, so nothing inside
    it can widen the column merely by *wanting* more room.

    This is the second half of the fix `_SIDE_RAIL_WIDTH_PX` above describes.
    That one stopped the rail's MINIMUM from moving; this one stops its
    NATURAL from moving, which turns out to be the number that actually
    reallocates the screen.

    The defect, measured on this booth's own 1920x1080 fullscreen window
    (`get_allocated_width()` / `compute_bounds()` across a `T` press):

        rail        552 -> 584 px wide, left edge x 1350 -> 1318
        hero slot   1332 -> 1300 px wide   <- the protein, moving 32px
        every panel in the rail: 32px left, 32px wider

    Why: `set_size_request` is only a FLOOR (the same lesson as the 101px
    telemetry lurch above). With the Tensix panel hidden the rail measured
    (minimum=588, natural=588); with it shown, (minimum=588, natural=620).
    The minimum never moved -- the panel's own minimum is capped at
    `ui.chipviz.RAIL_INNER_WIDTH_PX` exactly so it would not -- but the
    WebView reports a natural width of 552 (584 with the panel's padding),
    which is 32px past the rail's inner width, and `RAIL_INNER_WIDTH_PX`
    only ever constrained the minimum.

    That 32px matters because `GtkBoxLayout` distributes spare space in two
    passes: first it grows every child from its minimum toward its NATURAL,
    and only the remainder goes to the `hexpand` children. The rail is
    `hexpand(False)`, but pass one still hands it the 32px it asked for --
    and the hero slot, which is the `hexpand` child, pays for it.

    Pinning `natural := minimum` here fixes the whole class of bug in one
    place rather than chasing each child: the rail is exactly as wide as
    what it MUST have, never as wide as what something in it would like.
    Minimum is deliberately left alone, so a child that genuinely cannot
    render narrower still widens the column rather than being clipped.

    Still a `Gtk.Box`, with only its layout manager swapped, so it keeps
    `Gtk.Box`'s `append`/`remove` and -- the part that is easy to lose --
    `Gtk.Box`'s unparenting of its children at dispose. Two routes to the
    same pin were tried and rejected first, both by running them:

    - `do_measure` on a `Gtk.Box` subclass is never called. GTK delegates
      measurement to the layout manager whenever a widget has one, so the
      widget's own vfunc is dead code.
    - a plain `Gtk.Widget` subclass with this layout manager measures
      correctly but leaks its children: PyGObject does not wire a
      `do_dispose` override on a widget subclass, so teardown printed
      "Finalizing ... but it still has children left" for every rail a test
      built.

    Swapping the layout manager on a live `Gtk.Box` is safe here because
    `gtk_box_set_spacing`/`set_orientation` resolve the layout manager
    through `gtk_widget_get_layout_manager()` on each call rather than
    caching it -- verified by setting spacing after the swap and reading it
    back.
    """

    def __init__(self, *, orientation, spacing=0):
        super().__init__(orientation=orientation, spacing=spacing)
        self.set_layout_manager(
            _PinnedNaturalBoxLayout(orientation=orientation, spacing=spacing))


# What the gallery gets to lay its cards out in: the window minus the rail.
# 1920 is this booth's screen; `ui.gallery.grid_shape` turns it into a
# column count, so a different screen simply gets a different one.
_GALLERY_WIDTH_PX = 1920 - _SIDE_RAIL_WIDTH_PX

# tt-bio's ASCII logo, verbatim from the upstream README. Rendered as TEXT
# in a monospace face rather than shipped as a bitmap: crisp at any size,
# and it is the project's own identity mark rather than an approximation of
# it. Pinned bottom-right of the whole screen (not inside the rail) because
# it is an identity mark, not part of the readout -- it should sit still
# while everything else changes.
TT_BIO_LOGO = (
    "████████╗████████╗        ██████╗  ██╗  ██████╗\n"
    "╚══██╔══╝╚══██╔══╝        ██╔══██╗ ██║ ██╔═══██╗\n"
    "   ██║      ██║    █████╗ ██████╔╝ ██║ ██║   ██║\n"
    "   ██║      ██║    ╚════╝ ██╔══██╗ ██║ ██║   ██║\n"
    "   ██║      ██║           ██████╔╝ ██║ ╚██████╔╝\n"
    "   ╚═╝      ╚═╝           ╚═════╝  ╚═╝  ╚═════╝"
)

# Where the playlist lives when nothing is passed on the command line:
# alongside this checkout, resolved off THIS FILE, never off the process's
# working directory (ui/playlist.py's own module docstring explains why that
# distinction has already cost this project once).
_DEFAULT_PLAYLIST = pathlib.Path(__file__).resolve().parent.parent / "playlist" / "manifest.yaml"


def _format_missing(missing):
    """Render a not_ready event's `missing` value for the log without ever
    raising.

    `missing` is trusted to be a list of strings, and normally is one --
    but this is wire data from the daemon, and the whole point of logging
    it is to help an operator diagnose a *different* problem. If some
    future daemon change (or a wire bug) ever sends something else --  a
    single string, a number, `None` -- `"; ".join(missing)` would raise
    inside this log call itself. That exception would be caught by
    _handle_event's outer broad `except Exception`, but at the cost of
    replacing this specific, useful detail with the generic "dropping
    malformed not_ready event", which is exactly the outcome an operator
    at 11pm can least afford: the one signal that could tell them what's
    actually wrong, swallowed by the same guard that's supposed to keep
    the app alive. repr() never raises on anything, so this always
    produces *something* diagnosable instead.
    """
    if isinstance(missing, list):
        return "; ".join(str(item) for item in missing)
    return repr(missing)


# ── visitor input ───────────────────────────────────────────────────────────
#
# Key names as `Gdk.keyval_name` reports them, lowercased. `?` is `question`
# on every layout that has it as a shifted character; `F1` and the dedicated
# `Help` key some keyboards carry are accepted as the same request, because a
# booth operator reaching for help should not have to know which one this
# build wanted.
_HELP_KEYS = frozenset({"question", "f1", "help"})
_DIAGNOSTICS_KEYS = frozenset({"d"})
# The Tensix activity panel (ui/chipviz.py), on the same footing as the
# diagnostics panel: chrome, off by default, one key. It is the most
# eye-catching thing in the rail and it is the LEAST important -- a
# decorative animation next to a protein that is the actual point -- so the
# booth comes up protein-first and a visitor or an operator asks for it.
# Nothing about it is persisted: a restart at the venue is a clean booth.
_TENSIX_KEYS = frozenset({"t"})
# The quad view's key is DECIDED IN `ui/quad.py`, beside the view it opens and
# beside the `?`-card copy that describes it (`QUAD_HELP_LINE`), so the
# binding and the words a visitor reads about it cannot drift apart. Imported
# rather than restated for the same reason -- a second `frozenset({"q"})` here
# would be a second place to change it. `_handle_key` reads it exactly the way
# it reads the two above.
_QUAD_KEYS = QUAD_KEYS

# ── the easter egg ──────────────────────────────────────────────────────────
#
# `Ctrl+G`, for geometry. A cloud of Gaussian noise is pulled into the
# Tenstorrent mark by gradient descent on a signed distance field, and drawn
# through the same `StructureViewer.set_points` a fold does.
#
# WHERE THAT ARITHMETIC RUNS is the interesting part, and it is not here. The
# UI asks the daemon (`EventClient.send_egg`), the daemon gives the request
# the next chip that is already free, and a worker computes the whole descent
# in ttnn (`runner/egg.py`) and streams it back as `egg_frame` events over the
# same socket a fold's frames come down. This process buffers those and plays
# them on its own clock -- exactly the shape `_drain_frames` already has, and
# for the same reason: what arrives off a socket must not decide the cadence
# of what is on screen.
#
# `mark.py`'s numpy descent is still here, and is what runs when no chip
# answers -- every card folding, no daemon, a wedged worker. THE LABEL
# CHANGES WHEN THAT HAPPENS (`_EGG_PROVENANCE`). That is not a nicety: the
# booth's entire claim is that a visitor can trust what it says was computed,
# and an egg that said "computed on the chip" while running on this laptop
# would be the one lie in the building.
#
# Why a CHORD and not a plain letter. Every unbound plain key in this booth is
# a visitor touch (`_handle_key`'s last line) -- that is the whole interaction
# model, and a visitor at a keyboard presses things. Carving a letter out of
# that would mean a visitor who wanted the gallery sometimes got a toy
# instead, which is a worse bug than the egg is a feature. A chord cannot be
# hit by accident, costs the visitor surface nothing, and sits with the two
# chords the booth already reserves for people who know the booth.
#
# It is deliberately NOT on the `?` card. An easter egg that is documented is
# a feature, and this is not one -- see
# `test_the_easter_egg_is_not_advertised_on_the_help_card`, which pins that as
# a decision rather than leaving it as an omission somebody later "fixes".
_EGG_KEYS = frozenset({"g"})

# One frame per tick, at the same cadence `_drain_frames` runs a real fold's
# frames at -- so the egg's collapse is paced like the diffusion trajectory it
# is imitating rather than being a separate kind of motion. One tick is one
# buffered device frame, or (on the fallback) one numpy descent step; both
# produce `mark.STEPS` frames, so the animation is six seconds either way.
_EGG_STEP_MS = _FRAME_DRAIN_MS

# How long the booth waits for a chip's first frame before giving up and
# running the descent here instead.
#
# Longer than the daemon's own `EGG_WAIT_S` (4.0 s) on purpose, and the order
# matters: in the ordinary busy-booth case the daemon answers with an
# `egg_refused` and the fallback starts immediately, so this timer only ever
# fires when nothing answered at all -- a daemon that has died between the
# send and now, a v-mismatched socket, a worker wedged mid-egg.
_EGG_DEVICE_WAIT_MS = 6000

# How many `egg_frame` events may sit in the play buffer. One whole run is
# `mark.STEPS` + 1 frames and the worker delivers them in about a second, so
# the buffer normally holds the entire animation before the third tick has
# fired -- which is exactly why the collapse looks smooth however the chip
# happened to be scheduled. The bound is here so a daemon that streamed
# forever could not grow this without limit; a run that overruns it drops its
# OLDEST unplayed frames, because the newest are the ones nearest the mark.
_EGG_FRAME_BUFFER = 512

# The copy. This is the one place in the booth where a visitor could mistake
# computed decoration for a computed RESULT, so the card says what it is in
# the same register as everything else here: what it is, what it is not, and
# that the booth has not stopped doing its actual job.
_EGG_TITLE = "Not a fold — geometry, for fun"
_EGG_BODY = (
    # The count comes from mark.py rather than being typed here: a number
    # in visitor-facing copy that can drift from the thing it describes is
    # exactly the kind of small lie this booth has already had to fix once.
    f"{MARK_POINTS:,} points of Gaussian noise, pulled into the Tenstorrent "
    "mark by gradient descent on a signed distance field. The mark is a cube "
    "seen corner-on: three faces on an isometric lattice, with a notch. "
    "It lands differently every time."
)
_EGG_DISCLAIMER = (
    # The sentence that stopped saying "and nothing off the chips" when the
    # arithmetic moved onto them. What it must keep saying -- and what the
    # `?`-card omission and the chord both exist to protect -- is that this is
    # not a structure. Where it ran is a separate claim, made separately,
    # below, and only when it is true.
    "Real arithmetic — but no chemistry, no molecule, no protein. "
    "This is not a folded structure."
)
# The provenance line, which is the only line on this card that changes, and
# the only claim in this booth that could be false without anything on screen
# giving it away -- an egg drawn by a chip and an egg drawn here look
# identical. `_egg_provenance_text` is a pure function precisely so a test can
# pin every state without a display.
_EGG_PROVENANCE = {
    "asking": "Asking the booth for a chip…",
    "device": "Computed on chip {card} — like everything else here.",
}

# What the card says when the descent ran HERE, keyed by the daemon's own
# refusal reason.
#
# A separate table from `_EGG_PROVENANCE`, and it is separate because merging
# them was tried and was WRONG: the daemon's reason code for "a chip failed"
# is `device`, and `_EGG_PROVENANCE` uses that same word for "a chip computed
# this". One table meant a chip that died mid-egg put "Computed on chip
# {card}" on screen over a descent that had just fallen back to the host --
# the exact lie this whole line exists to prevent, produced by a key
# collision. Two tables cannot collide.
_EGG_FALLBACK_DEFAULT = "No chip answered, so this one ran on the host CPU."
_EGG_FALLBACK = {
    "busy": "Every chip is busy folding, so this one ran on the host CPU.",
}
_EGG_NOTE = "Any key returns to the booth · the rail on the right is still live"

# How long an overlay a visitor left open survives their walking away.
#
# The state machine's own 45s idle timeout only covers the states it owns
# (gallery, folding); this chrome sits outside those, so it needs its own
# rule or a visitor who opens the help card and leaves would hand the next
# person a booth with a wall of text over the protein. The help card is
# transient by nature and goes first (60s -- comfortably longer than it takes
# to read, short enough that the protein is back before the next visitor
# arrives). The diagnostics panel is deliberately much more patient (5
# minutes): it is also the panel WE want up while standing at the booth
# talking to someone, and having it vanish mid-sentence would be worse than
# useless. Both are measured from the last visitor input of any kind, so
# neither can close while someone is actually pressing things.
_HELP_IDLE_S = 60.0
_DIAGNOSTICS_IDLE_S = 300.0

# The Tensix activity panel gets the diagnostics panel's patience, for the
# diagnostics panel's reason -- it is the other thing WE want up while
# standing at the booth talking to someone, and having it vanish mid-sentence
# would be worse than useless. An alias rather than a second number: the two
# panels are the same kind of chrome opened the same way, and two constants
# that must stay equal are a constant waiting to drift.
_RAIL_PANEL_IDLE_S = _DIAGNOSTICS_IDLE_S

# The easter egg gets the HELP CARD's patience, not the panels'. It covers the
# hero slot, and the one thing the attract loop must never do is show a
# visitor who did not ask for it something that is not a fold. An alias, for
# the same reason as above: it is the same kind of thing, opened the same way,
# and it must go away on its own.
_EGG_IDLE_S = _HELP_IDLE_S

# ── logging a failure that repeats ──────────────────────────────────────────
#
# `_drain_frames` runs every 33ms and `_handle_event` fires per event, so a
# systematically malformed stream (a daemon sending a bad coords_b64, a
# protocol change this build cannot parse) turns a single `log.exception`
# into ~30 tracebacks a second for as long as the booth is up -- which is
# both an unbounded log and, worse, a wall of noise that buries the FIRST
# occurrence, the only one an operator actually needs to read.
#
# ui/chipviz.py already made this trade for its own 1Hz JS failures by
# dropping straight to `log.debug`. This does the same thing without losing
# the signal: the first failure of each kind is logged in full, at error
# level, with its traceback; every `_DROP_LOG_EVERY`-th one after that logs
# a one-line count so a persistent problem stays visible; everything in
# between goes to debug.
_DROP_LOG_EVERY = 100

# ── palette ─────────────────────────────────────────────────────────────────
#
# The same brand constants ui/panels.py names, repeated here rather than
# imported so this file's stylesheet stays readable as a stylesheet -- but
# with the same values and the same rule: `_ACCENT` (#1B8EB1) measures
# 4.40:1 on the dark ground and is therefore a FILL colour only; anywhere
# accent-coloured TEXT is wanted, `_ACCENT_TEXT` (#3299B9, 5.06:1) is what
# clears the 4.5:1 AA floor this project holds every label to.
_DARK_BASE = "#092221"
_BG = "#F1F8F8"
_BG_ALT = "#C7D9D8"
_ACCENT_TEXT = "#3299B9"
_TEAL = "#74C5DF"

# The single source of truth for "which CSS class in _APP_CSS carries an
# explicitly-set background", read by tests/unit/_legibility.py's shared
# walker exactly as ui/panels.py's and ui/gallery.py's equivalents are. Every
# entry maps to the same dark ground: the preparing and help overlays are
# near-opaque washes OF that ground rather than a second surface colour, so
# the contrast a label really has is the contrast against `_DARK_BASE`.
#
# `.booth-root` is on the root overlay as well as the root box, so that the
# logo (and the help card) -- which are overlay children, i.e. siblings of
# the root box rather than its descendants -- still have a
# background-painting ANCESTOR and can therefore be contrast-checked at all.
_BACKGROUND_BY_CLASS = {
    "booth-root": _DARK_BASE,
    "booth-side": _DARK_BASE,
    "preparing-overlay": _DARK_BASE,
    "help-overlay": _DARK_BASE,
    # The easter egg's card (`_build_egg_overlay`). Same near-opaque wash of
    # the same ground as the help card, and registered here for the same
    # reason: its three labels have to be contrast-checkable.
    "egg-overlay": _DARK_BASE,
    # The held-structure caption. Like the two overlays above it is a
    # near-opaque wash OF the dark ground rather than a second surface
    # colour, so the contrast its two labels really have is the contrast
    # against `_DARK_BASE`. It is a small card rather than a full-screen
    # wash for the obvious reason: the structure it is captioning has to
    # stay visible behind it.
    "viewer-caption": _DARK_BASE,
    # The protein caption below the render. Unlike the two overlays it is a
    # real strip in the layout rather than a wash, but it paints the same
    # ground, so its two labels are checked against `_DARK_BASE` too.
    "target-info": _DARK_BASE,
}

# The pLDDT legend's swatches, generated from the ONE ramp the ribbon itself
# is coloured by (ui/geometry.py's `PLDDT_STOPS`) rather than a second list
# of hexes hand-copied into this file. A legend that has drifted from the
# thing it describes is worse than no legend, and hand-copying is how that
# drift happens; tests/unit/test_app_interaction.py pins the two together.
#
# Note these are FILLS, never text: `#0053D6` measures 2.54:1 against the
# dark ground and could not legally be a label colour here. The label next
# to each swatch is ordinary body text on the overlay's own ground.
_PLDDT_LEGEND = (
    ("plddt-very-high", "above 90", "very high — trust the detail"),
    ("plddt-confident", "70 to 90", "confident backbone"),
    ("plddt-low", "50 to 70", "low — treat with care"),
    ("plddt-very-low", "below 50", "very low — likely floppy or disordered"),
)

# The always-on version of that legend, under the render (see
# `_build_confidence_legend`). It says what the colour MEANS and which way
# the ramp runs, and stops there -- the thresholds, and what each band is
# worth, are the `?` card's job.
#
# The swatches are the same `.plddt-*` classes the card uses, so both are
# generated from `PLDDT_STOPS` and neither can drift from the ribbon. This
# legend reads LOW to HIGH, which is the reverse of `PLDDT_STOPS`' own
# order: a ramp a visitor scans left to right should end where the good
# news is, and "less sure -> more sure" is the direction the sentence above
# it reads in. That reversal is derived (`reversed(_PLDDT_LEGEND)`), never a
# second hand-ordered list.
_CONFIDENCE_LEGEND_CAPTION = "Colour: how sure the model is, residue by residue"
_CONFIDENCE_LEGEND_LOW = "less sure"
_CONFIDENCE_LEGEND_HIGH = "more sure"


def _plddt_swatch_css():
    """One `.plddt-*` background rule per ramp stop, in ramp order.

    `PLDDT_STOPS` is ordered high threshold first, which is the order
    `_PLDDT_LEGEND` above reads in, so they zip directly. Built as CSS text
    (not set per-widget) so the swatches live in the same stylesheet as
    everything else and the legibility guard sees them for what they are:
    background-painting classes that must never carry a label.
    """
    rules = []
    for (css_class, _range_text, _meaning), (_threshold, rgb) in zip(
            _PLDDT_LEGEND, PLDDT_STOPS):
        hex_color = "#%02X%02X%02X" % tuple(rgb)
        rules.append(f".{css_class} {{ background-color: {hex_color}; }}")
    return "\n".join(rules)


# CSS for the booth chrome, in the brand palette from the docs-site theme:
# dark base for every ground, accent/teal for text. Kept as a module-level
# constant (not rebuilt per window) since it never varies. The panels and
# the gallery install their own stylesheets the same way (ui/panels.py,
# ui/gallery.py) -- this one covers only what this file itself builds.
_APP_CSS = f"""
.preparing-overlay {{
    background-color: rgba(9, 34, 33, 0.94); /* #092221, near-opaque */
}}
.preparing-title {{
    color: {_TEAL};
    font-size: 22px;
    font-weight: bold;
}}
.preparing-message {{
    /* _ACCENT_TEXT, not the raw brand accent: #1B8EB1 measures 4.40:1 on
       this ground -- under the 4.5:1 floor the rest of the booth holds to,
       and this is the one line a visitor reads when something has gone
       wrong. Caught by extending the shared legibility guard to cover this
       file's own widgets (Task 10). */
    color: {_ACCENT_TEXT};
    font-size: 15px;
}}
/* The held-structure caption: what the viewer is showing, and what the
   booth is actually computing, said in words next to each other so the two
   cannot be confused. A card at the top of the hero slot, not a wash --
   the structure it describes has to stay visible behind it. See
   `_build_viewer_caption` and `viewer_hold_caption`. */
.viewer-caption {{
    background-color: rgba(9, 34, 33, 0.92); /* #092221, near-opaque */
    border-radius: 8px;
    padding: 10px 20px;
}}
.viewer-caption-title {{
    /* _TEAL, 8.55:1 on #092221 -- the same colour and the same role as
       `.preparing-title`: the one line that says what this screen is. */
    color: {_TEAL};
    font-size: 19px;
    font-weight: bold;
}}
.viewer-caption-sub {{
    /* _BG_ALT, 11.36:1 on #092221. Body weight deliberately: this line is
       the live claim ("now folding X"), and it must read as a caption to
       the title above it rather than compete with it at booth distance. */
    color: {_BG_ALT};
    font-size: 14px;
}}
/* The protein caption, under the render (`_build_target_info`). Sized to be
   read at TWO METRES by someone walking past -- which is why both lines are
   far larger than anything in the rail -- and deliberately quiet in colour
   and weight so it reads as a caption to the picture above it rather than
   competing with it. It paints the booth's own ground (it is a strip below
   the GL area, not a card over it), so it is registered in
   `_BACKGROUND_BY_CLASS` like every other background tier here. */
.target-info {{
    background-color: {_DARK_BASE};
    padding: 10px 32px 18px 32px;
}}
.target-info-name {{
    /* _TEAL, 8.55:1 on #092221 -- the same colour and the same role as
       `.viewer-caption-title`: the line that says what this is. */
    color: {_TEAL};
    font-size: 26px;
    font-weight: bold;
}}
.target-info-tagline {{
    /* _BG_ALT, 11.36:1 on #092221. Body weight on purpose: at 20px this is
       already the largest body text on the screen, and bolding it too would
       have it fighting the protein for attention. */
    color: {_BG_ALT};
    font-size: 20px;
}}
window, .booth-root, .booth-side {{
    background-color: {_DARK_BASE};
}}
.booth-logo {{
    color: {_TEAL};
    font-family: "Berkeley Mono", monospace;
    font-size: 8pt;
}}
.booth-title {{
    color: {_BG};
    font-size: 14pt;
    font-weight: 700;
}}
.booth-sub {{
    color: {_BG_ALT};
    font-size: 10pt;
}}
/* The two affordances that tell a visitor this booth does anything at all
   when you press it. Small, letterspaced, permanently visible -- the panel
   and the help card they open are not. */
.booth-hint {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: {_BG_ALT};
}}
.booth-hint-key {{
    font-family: "Berkeley Mono", monospace;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: {_ACCENT_TEXT};
}}
/* The `?` card. A near-opaque wash of the same ground rather than a
   different surface colour, so it reads as the booth dimming itself rather
   than as a dialog from some other application. */
.help-overlay {{
    background-color: rgba(9, 34, 33, 0.96);
}}
.help-title {{
    color: {_BG};
    font-size: 26px;
    font-weight: 700;
}}
/* The easter egg (`Ctrl+G`, mark.py). Same wash as the help card, because
   it is the same kind of thing: the booth putting something over itself for
   a moment, not another application.

   The BRAND PURPLE the mark is drawn in is deliberately absent from this
   block. #7C68FA measures 4.13:1 on #092221 -- under the 4.5:1 floor every
   label in this booth holds to -- so it is a FILL colour only, on a point
   cloud in a GL uniform (mark.py's `BRAND_PURPLE`), and never on type.
   `_ACCENT` is excluded from type here for exactly the same reason. */
.egg-overlay {{
    background-color: rgba(9, 34, 33, 0.96);
    padding: 36px 40px;
}}
.egg-title {{
    /* _TEAL, 8.55:1 -- the same colour and the same role as
       `.viewer-caption-title`: the one line that says what this is. */
    color: {_TEAL};
    font-size: 24px;
    font-weight: 700;
}}
.egg-body {{
    color: {_BG_ALT};  /* 11.36:1 */
    font-size: 14pt;
}}
.egg-disclaimer {{
    /* Brighter than the body (_BG, 15.46:1): this is the sentence that
       stops the egg being mistaken for a result, so it is the one line on
       the card that must survive being read from across a booth. */
    color: {_BG};
    font-size: 14pt;
    font-weight: 700;
}}
.egg-provenance {{
    /* WHERE this ran, and the one line on the card whose text changes. It
       gets `_TEAL` (8.55:1) -- the booth's "this is a fact about the
       hardware" colour, the same one `.egg-title` and the viewer caption's
       title use -- and never the brand purple, which is 4.13:1 and therefore
       fill-only everywhere in this file. */
    color: {_TEAL};
    font-size: 12pt;
    font-weight: 700;
}}
.egg-note {{
    color: {_BG_ALT};  /* 11.36:1 */
    font-size: 11pt;
}}
.help-body {{
    color: {_BG_ALT};
    font-size: 13pt;
}}
.help-section {{
    color: {_ACCENT_TEXT};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.12em;
}}
.help-key {{
    font-family: "Berkeley Mono", monospace;
    color: {_BG};
    font-size: 12pt;
    font-weight: 700;
}}
.help-desc {{
    color: {_BG_ALT};
    font-size: 12pt;
}}
.help-note {{
    color: {_BG_ALT};
    font-size: 10pt;
    font-style: italic;
}}
.plddt-swatch {{
    border-radius: 3px;
    min-width: 34px;
    min-height: 14px;
}}
/* The always-on legend in the caption strip (`_build_confidence_legend`).
   Deliberately the quietest text on the screen: _ACCENT_TEXT is 5.06:1 on
   this ground where the tagline beside it is 11.36:1, and it is set at
   13px/12px against the tagline's 20px. A legend that competed with the
   protein would be a worse legend, and the whole point of this one is that
   a visitor can ignore it until the moment they wonder why one fold is
   blue and the next is orange. Its swatches are smaller than the help
   card's for the same reason -- 24x9 rather than 34x14 -- and butt against
   each other (spacing 2) so the four bands read as one ramp rather than
   four unrelated chips. */
.confidence-legend-caption {{
    color: {_ACCENT_TEXT};
    font-size: 13px;
}}
.confidence-legend-end {{
    color: {_ACCENT_TEXT};
    font-size: 12px;
}}
.confidence-legend-swatch {{
    border-radius: 2px;
    min-width: 24px;
    min-height: 9px;
}}
{_plddt_swatch_css()}
"""

# ── help copy ───────────────────────────────────────────────────────────────
#
# Plain language, on purpose: the person most likely to press `?` is the one
# who does not already know what any of this is. Every claim here is one the
# booth can actually back up -- the fold IS running on the cards a few feet
# away, the trajectory IS the model's own, and the timings are the measured
# ones from docs/followups.md's 30-fold soak (4.35-4.45s warm), not marketing
# numbers.
_HELP_INTRO = (
    "A protein structure prediction, running right now on Tenstorrent "
    "Blackhole chips a few feet away. This is not a recording or an "
    "animation: the cloud of points collapsing on screen is the model's own "
    "working, streamed off the chip as it computes, and the ribbon at the "
    "end is the structure it just predicted.",

    "A protein is a chain of amino acids that only does its job once it "
    "folds into a particular three-dimensional shape. Predicting that shape "
    "from the sequence of the chain alone is the problem this model solves "
    "— here, in about four and a half seconds per protein.",

    "It works by denoising: the model starts from a cloud of random atom "
    "positions and pulls it, over roughly 200 small steps, into a real "
    "structure. Touch the screen to see everything this booth folds.",

    # The disclosure, in the visitor's own words. The booth folds its
    # playlist in its own order and a tap cannot change that: the socket
    # protocol is one-way (runner/server.py broadcasts, ui/client.py never
    # sends), so `_on_pick` reaches the state machine and nothing further.
    # Stated as a fact rather than an apology, and paired with what IS on
    # offer -- see ui/gallery.py's own module docstring, which carries the
    # same rule for the copy on that screen.
    #
    # The first sentence changed with Task 16 and not before: "one after
    # another" was true of a booth folding on card 0, and four chips fold at
    # once now.
    # The disclosure became an offer with Task 17, in the same commit as
    # `_on_pick` learning to send. What it must NOT become is a promise of an
    # instant fold: with four chips busy the pick starts when one frees,
    # usually within seconds, and "instantly" is a claim the booth breaks in
    # front of the one visitor watching for it. The second half is the wait
    # stated as the feature it is -- not interrupting is exactly why the
    # other three cells keep moving.
    #
    # Kept to the same number of wrapped lines as the copy it replaced: the
    # `?` card is 913px of the booth's 1080 and every line added to it costs
    # the operator keys at the bottom of the KEYS column (see
    # `test_the_help_card_still_fits_the_booth_s_own_screen`).
    "The booth folds four proteins at a time, one on each chip, all day. Tap "
    "any of them to put it next: it starts on the next chip to come free, "
    "because the folds already running are left to finish.",
)

# Every key the booth answers to, and what it does. This table is the ONE
# place the bindings are described to a visitor, and the test
# `test_every_key_the_booth_answers_to_is_listed_in_the_help` walks
# `_handle_key`'s real behavior against it -- so a binding added to the
# handler without a line here fails the suite rather than quietly becoming
# folklore.
_KEY_HELP = (
    ("?  or  F1", "this card — from any screen, at any time"),
    ("Q", "the quad view: all four chips at once, one protein per chip — "
          "press it again for the single large view"),
    ("T", "Tensix activity: the live core-grid animation, one grid per chip"),
    ("D", "diagnostics: the live protocol log in the right-hand rail"),
    ("Esc", "close this card, or close whichever rail panel is open"),
    ("any other key,\nor a tap anywhere",
     "wake the booth and look through the proteins it folds"),
    ("Ctrl + F", "leave or return to fullscreen — for the booth operator"),
    ("Ctrl + Q", "quit the booth — for the booth operator"),
)

_HELP_PANELS = (
    # The quad view's own line, imported from `ui/quad.py` rather than
    # re-typed here: the key, the view and the words describing it are one
    # decision, and a hand-copied second sentence is where a booth ends up
    # documenting a key it no longer has. It leads this column because it is
    # the only entry that changes what the hero image IS, rather than what
    # sits in the rail beside it.
    QUAD_HELP_LINE,

    "Pipeline — one row per stage of a fold: msa, prep, trunk, diffusion, "
    "confidence, saving. The bright row is the stage running right now; "
    "diffusion owns most of the bar because it does most of the work.",

    # The cadence here is `ui/telemetry.py`'s TelemetrySampler(period_s=2.0)
    # -- one `tt-smi` snapshot every two seconds, on its own thread. This
    # paragraph used to say "read from the driver twice a second", which was
    # wrong twice over: it is 4x the real rate (500ms is `_TELEMETRY_REPAINT_MS`,
    # the REPAINT cadence, not the sample rate) and it is a `tt-smi`
    # subprocess, not a driver read. The chip panel below it genuinely does
    # read the driver, once a second, and says so.
    "Chips — temperature, power draw and clock speed for every Tenstorrent "
    "chip in this machine, taken from a tt-smi snapshot every two seconds. A "
    "Blackhole p300c board carries two chips, so the four chips here are two "
    "boards. It is independent of the fold, so the silicon keeps breathing "
    "even if a fold stalls.",

    # Every claim in this paragraph was checked against the rendered pixels
    # before it was written. An earlier draft said each grid was "driven by
    # that chip's own clock" -- the per-chip feed IS wired (ui/chipviz.py),
    # but at this size it makes no visible difference, so the sentence was
    # cut rather than left as a nice-sounding thing the screen does not
    # actually do. What IS live and per-chip is the clock number, and the
    # temperatures directly above it.
    # Rewritten with Task 16, in the same commit as the behaviour. This
    # paragraph was walked back once (whole-branch review, Critical 3) to say
    # the fold "runs on one chip" and that the others "sit idle" -- true then,
    # a lie now that all four fold at once. What it must NOT do is overshoot
    # in the other direction: the panel counts the chips that are actually
    # animating work, so a chip between folds really is drawn resting and the
    # card has to say so or it promises four grids of motion at every moment.
    "Tensix activity (press T) — one animated Tensix core grid per chip, in "
    "the same left-to-right order as the readouts above it. Each grid follows "
    "its own chip's fold: a spreading ring while that chip is denoising atom "
    "positions, a steady glow while it is reasoning about which residues "
    "touch, and quiet when it is between folds. Four chips fold at the same "
    "time, so the header says how many are working right now — or names the "
    "one, if only one is. The number beside it is the fastest clock any of "
    "these chips is running at, read from the driver every second. It is a "
    "picture of the work, not a trace of individual cores.",
)

_APP_CSS_INSTALLED = False


def _ensure_app_css_installed():
    """Install `_APP_CSS` once, against the default display.

    Same shape (and same reason) as ui/panels.py's and ui/gallery.py's
    installers: guarded on a display existing at all, so building a widget
    from this module never hard-requires one -- which is what lets the
    legibility tests construct the help card and the side rail directly,
    without running `do_activate` or a main loop.
    """
    global _APP_CSS_INSTALLED
    if _APP_CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        log.debug("no default display; skipping app CSS install")
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_APP_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _APP_CSS_INSTALLED = True


class DemoApp(Gtk.Application):
    def __init__(self, socket_path=None, playlist_path=None, target_ids=None,
                 clock=None, windowed=False, quad=False):
        super().__init__(application_id="com.tenstorrent.ttbiodemo")
        self.socket_path = socket_path
        # Start windowed instead of fullscreen. The booth always wants
        # fullscreen, but a developer on a shared desktop does not want an
        # app that seizes the whole screen before they can reach Ctrl+F.
        self.windowed = windowed
        self.playlist_path = playlist_path
        # Which manifest entries this run actually offers. None/empty means
        # "all of them". scripts/run-demo.sh passes the SAME selection here
        # that it uses to build the daemon's fold directory, which is what
        # stops the gallery advertising a target the daemon has no input
        # file for -- see ui/playlist.py's module docstring.
        self.target_ids = list(target_ids) if target_ids else []

        # The booth's one source of truth for what is on screen. Everything
        # else in this class either feeds it (events, touches, the clock) or
        # reads it (`_sync_to_state`).
        self.states = StateMachine(showcase_dwell_s=_SHOWCASE_DWELL_S)
        # What `_sync_to_state` last acted on, so it can spot the EDGE of a
        # transition (`showcase_ended`) rather than its level.
        self._last_state = self.states.state

        # Injectable purely so tests can drive the two time-based
        # transitions -- the showcase dwell and the 45s idle timeout -- by
        # hand instead of sleeping through them. Production always gets
        # time.monotonic (never time.time: a wall-clock adjustment mid-fold
        # must not end a showcase or fire an idle timeout).
        self._clock = clock if clock is not None else time.monotonic

        # The UI samples tt-smi itself rather than reading chip telemetry off
        # the socket, so a wedged or dead daemon still leaves the silicon
        # visibly breathing (spec section 6, ui/telemetry.py). Constructed
        # here, started in do_activate: constructing it starts no thread, so
        # a headless test can swap it out.
        self.sampler = TelemetrySampler()

        # Widgets, all created in do_activate (which needs a display). Named
        # here so every one of them is a real attribute from construction --
        # a headless test substitutes a recorder, and `_sync_to_state` and
        # the tick callbacks all tolerate `None`.
        #
        # There is NO `self.viewer`, deliberately, and it is not aliased to
        # `self.quad`'s first cell either: an alias is where four folds
        # quietly become one again. Everything that draws goes through
        # `_viewer_for(slot)`, which is the one place a slot index becomes a
        # widget.
        self.quad = None
        self.gallery = None
        self.screens = None
        self.telemetry_panel = None
        self.pipeline_panel = None
        # The Tensix activity panel (ui/chipviz.py). May legitimately stay
        # None (headless tests, and the moment before do_activate runs) and
        # may legitimately exist-but-be-unavailable (no WebKit, no chips);
        # `_sync_chipviz` tolerates both.
        self.chipviz_panel = None
        self.targets = []
        self._preparing_box = None
        self._preparing_message_label = None
        self._window = None
        # The slot in the widget tree the quad lives in, so a `hello` naming a
        # different set of chips can replace it. None until do_activate, which
        # is exactly what makes `attach_cards` safe to call headlessly: with
        # no widget tree there is no widget to rebuild, and a test's fake quad
        # is left precisely as the test installed it.
        self._viewer_page = None
        # The quad is the OPTIONAL view (see ui/quad.py). The booth comes up
        # showing one big protein -- the focused cell, alone -- and `Q` turns
        # the other three on. A plain bool a headless test can drive, exactly
        # like `help_visible` and the two panel flags, and NOT read back off
        # the widgets: the decision must be testable with no display.
        #
        # `--quad` seeds it the other way for the two people the toggle does
        # not serve: an operator running a four-chip booth who wants the grid
        # up all day, and anyone recording the booth, for whom "press Q at
        # the right moment" is a step that fails silently. It chooses the
        # START only -- `Q` still toggles from wherever the flag left it, and
        # `_ensure_quad` reads this flag when it builds the real widget on
        # the daemon's `hello`, which is what keeps the bool and the cells on
        # screen from disagreeing.
        self.quad_visible = bool(quad)

        # ── the easter egg (Ctrl+G; mark.py, `_EGG_KEYS`) ─────────────
        #
        # It gets its OWN viewer rather than borrowing the protein's. That is
        # the whole reason it cannot disturb a fold in flight: `_drain_frames`
        # keeps feeding the real viewer the whole time the egg is up, so
        # dismissing it puts a visitor back exactly where the booth would have
        # been -- rather than in front of a viewer that has to be cleared and
        # then wait for the next frame, which on a 223-residue target is
        # twenty seconds of nothing (the defect the 2026-08-13 "never clear
        # until superseded" change exists to prevent).
        #
        # `egg_visible` is a plain bool a headless test can drive, exactly
        # like `help_visible` and the two panel flags.
        self.egg_visible = False
        self.egg_viewer = None
        self._egg_box = None
        self._egg = None
        # The cloud drawn while the booth is waiting for an answer: a real
        # `MarkCondensation` that is NEVER stepped. Two things fall out of it
        # being real rather than a placeholder. A visitor sees noise
        # immediately instead of an empty box for up to `_EGG_DEVICE_WAIT_MS`;
        # and if no chip answers, this exact object becomes the fallback, so
        # the descent begins from the cloud already on screen rather than
        # cutting to a different one. A chip's own first frame replaces it,
        # and the swap is invisible because both are unstructured Gaussian
        # noise -- which is also why showing it claims nothing: the card says
        # "asking", and nothing about a noise cloud asserts where it came
        # from.
        self._egg_preview = None
        self._egg_source_id = None
        # Which press this is, so frames from an egg the visitor has already
        # dismissed cannot be drawn into the next one.
        self._egg_id = None
        # "asking" | "device" | "cpu" -- and it is `egg_source` (public, no
        # underscore) for the same reason `egg_visible` is: a headless test
        # asserting that the label matches where the arithmetic ran must be
        # able to read it without reaching into a private field, which is
        # this project's recurring test defect (docs/followups.md).
        self.egg_source = None
        self.egg_card = None
        # Set by an `egg_refused` so the tick that follows can act on it on
        # the main loop. Carries the daemon's short reason code ("busy" /
        # "device"), never any text from the wire.
        self._egg_refusal = None
        # `_egg_deadline` is the wall-clock instant after which the booth
        # stops waiting -- for a first frame while asking, and for the NEXT
        # frame once a chip is answering. Both are the same question ("has
        # this gone quiet?") and both want the same patience.
        self._egg_deadline = None
        # Whether the chip's LAST frame has been drawn. The egg's timer ends
        # on this, and emphatically not on "the buffer happens to be empty":
        # a chip that pauses mid-run (a cold ttnn kernel cache does exactly
        # this, for nine seconds) would otherwise retire the timer, and the
        # rest of the descent would arrive with nothing left to draw it. That
        # is not hypothetical -- it is what the first live run did.
        self._egg_device_done = False
        self._egg_provenance_label = None
        # Filled by the reader thread, drained by `_tick_egg` on the main
        # loop. A deque rather than `LatestFrame`: a fold's frames are
        # advisory and latest-wins is right for them, but an egg's frames ARE
        # the animation and dropping the middle of it would turn a collapse
        # into a jump cut.
        self._egg_frames = collections.deque(maxlen=_EGG_FRAME_BUFFER)
        self._egg_frames_lock = threading.Lock()

        # ── the two interactive surfaces this task adds ──────────────────
        #
        # The log is created here, not in do_activate, for the same reason
        # `_frames` is: events can arrive before (or without) any widget, and
        # a bounded buffer that is always present means no call site has to
        # ask whether diagnostics exist yet. The PANEL may legitimately be
        # None (headless tests, and the moment before do_activate runs);
        # every method below tolerates that.
        self.diagnostics = DiagnosticsLog()
        self.diagnostics_panel = None
        self._diagnostics_toggle_label = None
        self._tensix_toggle_label = None
        self._help_box = None

        # Visibility is tracked as plain booleans, NOT read back off the
        # widgets: `_handle_key`'s decisions have to be testable without a
        # display, and a widget-derived answer would also be wrong for the
        # window between construction and realization.
        self.diagnostics_visible = False
        self.help_visible = False
        # The Tensix activity panel starts CLOSED, every run. It is the one
        # panel that animates whether or not anything is happening, and a
        # booth whose first impression is four blinking core grids has sold
        # the wrong thing -- the protein is the hero. `T` opens it. See
        # `_TENSIX_KEYS`.
        self.chipviz_visible = False

        # When the last visitor input of any kind happened, on the injected
        # clock -- the only thing that can close an overlay nobody is using
        # (see `_HELP_IDLE_S`). None means "nobody has touched this booth
        # yet", which is exactly when there is nothing to time out.
        self._last_input_at = None
        # The booth demonstrating its own panels to an empty room. Pure and
        # clock-driven (ui/attract.py); it decides, this file applies. Nothing
        # starts until the booth has been idle longer than its own visitor
        # timeout, so it can never begin while somebody is still there.
        self.attract = Choreography()

        # The clock reading the last state tick was given (`_tick_state_at`),
        # and when the visitor's current pick was made, on that same reading.
        #
        # Two separate things that both have to be the TICK's reading rather
        # than `self._clock()`: `SlotRouter.pick_status` is asked with the
        # tick's `now`, so a pick stamped off a different reading would have
        # its ten-second window measured between two clocks. 0.0 rather than
        # None because a pick before the first tick cannot happen on the real
        # booth (it needs a window, a gallery and a touch, all of which come
        # up after `_start_timers`) and a None here would only add a branch
        # to every reader.
        self._tick_now = 0.0
        self._pick_at = 0.0
        # The last stage LOGGED per slot, so the log carries one line per
        # stage transition rather than one per stage event. See the `stage`
        # branch in `_handle_event` for the measurement behind this.
        # Cleared for a slot when a new fold starts on it, so a fold whose
        # first stage matches the previous fold's last one still says so.
        self._last_logged_stage = {}

        # Neutral copy for the preparing overlay plus the raw `missing`
        # detail, which goes to the log and NEVER to the screen. The state
        # itself is not stored here -- `display_state` reads through to the
        # machine (see that property), which is the plan's named integration
        # seam: two places that can disagree about whether the booth is
        # preparing is exactly what Task 9 had to collapse.
        self.missing = []
        self.display_message = ""

        # Diffusion frames, one slot PER JOB, latest wins within each. Created
        # here rather than in `_start_client` so `_drain_frames` is callable
        # with no socket at all (headless tests, and a booth started with no
        # --socket).
        self._frames = LatestFrameByJob()
        self._client = None
        # What the socket last said it was doing, so a quad rebuilt on a
        # `hello` starts out knowing (`_ensure_quad`). Not read back off a
        # widget, for the usual reason: the widgets may not exist yet.
        self._connection_state = "disconnected"

        # How many times each kind of repeating failure has been logged --
        # see _note_dropped and _DROP_LOG_EVERY. Per-app rather than
        # module-level so one booth session's counts cannot leak into
        # another's (or into another test's).
        self._drop_counts = {}

        # ── what each cell is holding, and what it is waiting for ────────
        #
        # See the module docstring's second and fourth sections. Together
        # these are the whole of "hold the previous structure until the new
        # fold has something real to show", once per cell -- they live on
        # `_SlotView` above and are built by `attach_cards` below:
        #
        # `awaiting_first_frame` -- a fold has started and has not yet
        # produced a single coordinate. True from `job_start` until that
        # fold's first frame is DRAWN (not merely received: a frame
        # suppressed during a showcase, or one that fails to decode, has not
        # superseded anything). Also cleared by `job_error` and `not_ready`,
        # which are the two ways a fold stops without ever producing one --
        # the caption asserts "now folding X" and must not outlive the fold
        # it names.
        #
        # `current_job_id` -- which fold that is, so a straggling frame
        # from the PREVIOUS fold cannot be mistaken for the new fold's first
        # one and wipe a finished structure in favour of an older fold's
        # noise. `frame` events carry `job_id` (runner/shaping.py's
        # `frame_event`), and with four folds in flight a frame that carries
        # none can no longer be placed at all: it belongs to no cell, and
        # drawing it into whichever cell happens to be first would be worse
        # than dropping it. That is the one rule this section changed when
        # the booth grew from one chip to four.
        #
        # `current_target_id` / `shown_target_id` -- what is being folded
        # and whose coordinates are on screen, for the caption. Kept as ids
        # and resolved to display names only at the moment of captioning
        # (`_target_name`), so nothing here depends on the playlist having
        # loaded.
        #
        # `has_structure` -- whether that cell's viewer has anything in it
        # at all. Deliberately a separate flag from `shown_target_id`
        # rather than `is not None` on it: a `job_start` with no `target_id`
        # is wire-shaped input we must tolerate, and conflating "no name for
        # what is on screen" with "nothing is on screen" would put the booth
        # back to captioning a populated viewer as an empty one.
        #
        # `_slots` and `self.router` are built together, by `attach_cards`,
        # so a slot index is always valid in both or in neither.
        self.cards = []
        self.router = None
        self._slots = []
        # The caption's current copy, as `viewer_hold_caption` returns it
        # (a (title, subtitle) pair, or None for "nothing to say"). Held as
        # a plain field, not read back off the widgets, for the same reason
        # `diagnostics_visible` is: the decision has to be testable with no
        # display, and the widgets do not exist until `do_activate`.
        self._caption = None
        self._caption_box = None
        self._caption_title_label = None
        self._caption_sub_label = None

        # The protein caption under the render, as a (name, tagline) pair or
        # None. Same split as `_caption` above and for the same reason: the
        # decision is a plain field a headless test can read, and the widgets
        # do not exist until `do_activate`. `tagline` may be None on its own
        # (a manifest entry without one) -- the name still shows.
        self._target_info = None
        self._target_info_box = None
        self._target_info_caption_box = None
        self._target_info_name_label = None
        self._target_info_tagline_label = None
        # The colour key beside that caption (`_build_confidence_legend`).
        # Stateless once built -- it describes the ramp, not the fold -- so
        # nothing ever updates it; the handle exists so tests and any future
        # layout work can find it.
        self._confidence_legend_box = None

        # ribbon_from_cif costs up to ~1.2s at 3000 residues (measured,
        # docs/followups.md) and must never run on the thread that calls
        # _handle_event -- for real traffic that's the GTK main loop, via
        # _on_event's GLib.idle_add dispatch. See _spawn_ribbon_worker,
        # _ribbon_worker_main, and _drain_pending_ribbon below for the full
        # scheme; the fields here are just its state:
        #
        # _ribbon_lock guards every slot's two fields against concurrent
        # access from worker threads and the main thread at once. ONE lock
        # for four cells, not four: it is held for two comparisons and an
        # assignment, and four locks would be four chances to take them in a
        # different order somewhere.
        #
        # `_SlotView.ribbon_generation` is a monotonic counter PER CELL,
        # bumped once per job_done on that cell that actually starts a worker
        # -- it is the answer to "which fold is the newest one in flight ON
        # THIS CHIP", independent of which worker happens to finish first.
        # Per cell is the whole point: with one global counter a job_done on
        # chip 3 bumps the generation and chip 0's in-flight ribbon is
        # dropped as stale, silently, every cycle, forever.
        #
        # `_SlotView.pending_ribbon` holds at most one not-yet-applied result
        # per cell, as (generation, cif_path, outcome) -- never more, because
        # a fold superseded before its result is even applied should never
        # reach the screen at all (see _ribbon_worker_main's docstring for
        # the ordering argument).
        #
        # _ribbon_threads is bookkeeping only (test joins + shutdown
        # visibility) -- nothing about correctness depends on this list;
        # the generation check is what actually decides what lands.
        self._ribbon_lock = threading.Lock()
        self._ribbon_threads = []

        # One cell, chip 0, until something says otherwise -- `do_activate`
        # (a booth with no socket still renders) or a `hello` (the daemon's
        # real card list). Built HERE and not merely declared, so that every
        # method below can assume a router exists: an event can reach this
        # app before any widget does, and "is there a router yet" is not a
        # question thirty call sites should each have to ask.
        self.attach_cards([0])

    # ── the cells, and the chips they show ───────────────────────────────

    def attach_cards(self, cards):
        """Build the per-cell state for this booth's chips, and size the quad.

        Called from exactly two places, and the two are the same decision
        arriving from different directions: `do_activate` with a single-card
        default, so a booth with no socket at all still renders a cell; and
        `hello`, with the daemon's real card list. A booth on two chips must
        build TWO cells, not four empty ones, which is why this takes a list
        of card numbers and never a count.

        Idempotent on the card list. A dropped socket reconnects and says
        `hello` again, and rebuilding the router on every reconnect would
        blank four cells and forget four folds for no reason at all.
        """
        cards = list(cards)[:MAX_SLOTS]
        if not cards:
            # A daemon that reported no chips still gets a cell to say so in.
            # Zero cells would mean no viewer, no caption and nothing on
            # screen -- the booth would look broken rather than idle.
            cards = [0]

        if self.router is None or self.cards != cards:
            self.cards = cards
            self.router = SlotRouter(cards, showcase_dwell_s=_SHOWCASE_DWELL_S)
            self._slots = [_SlotView() for _ in cards]
            # A frame buffered against the old shape names a job no cell can
            # claim any more; start clean rather than draining ghosts.
            self._frames = LatestFrameByJob()

        self._ensure_quad(cards)
        self._sync_viewer_hold()

    def _note_card(self, card):
        """Give a chip the booth has just heard of its own cell.

        `hello` is not a promise the booth will ever get. The daemon greets a
        UI that connects during the model-load window with `not_ready`
        instead (runner/daemon.py's `_hello`), and it only ever greets at
        accept time -- so on the real booth the ordinary startup is: connect,
        `not_ready`, and then `card_state` and `job_start` events naming
        chips the router has never heard of. Found by looking at it: the
        first live run of the quad logged four folds on four chips and drew
        one cell, because the card list never arrived.

        Appending rather than rebuilding (`SlotRouter.add_card`) is what
        makes this safe to do at any moment: existing cells keep their
        indices, their folds and their cameras.
        """
        if self.router is None:
            return
        index = self.router.add_card(card)
        if index is None:
            return
        self._slots.append(_SlotView())
        self.cards = list(self.router.cards)
        log.info("chip %s named by the daemon; giving it cell %d", card, index)
        self._ensure_quad(self.cards)

    def _ensure_quad(self, cards):
        """Make the on-screen quad show exactly these chips.

        A no-op with no widget tree (`_viewer_page is None`): headless tests
        install their own fake quad and this must not replace it. A no-op
        too when the real quad already shows these cards, which is what keeps
        a reconnect from tearing down four live GL contexts.
        """
        if self._viewer_page is None:
            return
        if isinstance(self.quad, QuadView) and list(self.quad.cards) == list(cards):
            return
        quad = QuadView(cards)
        quad.set_solo_mode(not self.quad_visible)
        quad.set_focus(self.router.focus_slot)
        try:
            quad.set_connection_state(self._connection_state)
        except Exception:
            log.exception("connection state not applied to the new quad")
        self._viewer_page.set_child(quad)
        self.quad = quad
        for viewer in quad.viewers:
            viewer.start_animation()
        log.info("quad now showing chip(s) %s", list(cards))

    def _viewer_for(self, slot):
        """The `StructureViewer` cell `slot` draws into, or None.

        The ONE place a slot index becomes a widget. None is ordinary, not an
        error: a headless test builds no quad at all, and a `hello` naming
        more chips than the quad on screen was built for is wire-shaped input
        rather than a programming mistake.
        """
        if self.quad is None or slot is None:
            return None
        try:
            return self.quad.viewer_for_slot(slot)
        except Exception:
            log.exception("no viewer for slot %r", slot)
            return None

    # ── the one source of truth ─────────────────────────────────────────
    #
    # Task 1 gave this class a plain `display_state` field so the preparing
    # screen could ship before a state machine existed; Task 7 then built
    # the machine, with its own `preparing` state. The plan named this the
    # phase's one integration seam and required Task 9 to collapse them
    # rather than leave two fields that can disagree. This is that collapse:
    # `display_state` is now a read-through, kept only because it is the
    # name the rest of the codebase (and Task 1's tests) already use.

    @property
    def display_state(self):
        """The booth's state, as a plain string. Read-only: to change it,
        drive the machine (`_handle_event`, `_on_touch`, `_on_pick`,
        `_tick_state`)."""
        return self.states.state

    def do_activate(self):
        # RE-ACTIVATION PRESENTS THE WINDOW; IT DOES NOT BUILD A SECOND ONE.
        # GApplication is single-instance over D-Bus, so launching the booth
        # while it is already running does not start a new process -- it
        # activates this one. Without this guard that meant a second window,
        # a second socket client, a second 33 ms frame timer, `self.viewer`
        # rebound to the new window's and the first window orphaned on screen
        # with nothing driving it. Entirely plausible at a venue: staff double
        # -click the desktop entry, or run the launcher again because the
        # booth is on another workspace and looks absent.
        #
        # Presenting is what they actually wanted -- it raises the booth they
        # could not find.
        if self._window is not None:
            log.info("already running; presenting the existing window")
            self._window.present()
            return
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("tt-bio")
        window.set_default_size(1280, 800)
        self._window = window

        _ensure_app_css_installed()

        # The preparing overlay sits on top of the viewer, not in place of
        # it, and its visibility is driven purely by the booth state -- it
        # has no dependency on the viewer ever having held a ribbon or even
        # a single frame of points, so it renders correctly from the very
        # first activate, before any fold (or even any connection) happens.
        #
        # Overlay ORDER is load-bearing: the caption goes on first and the
        # preparing overlay second, so the preparing wash covers the caption
        # rather than the other way round. `not_ready` takes the caption
        # down on its own (it clears `_awaiting_first_frame`), so this is
        # belt-and-braces for the one frame between the two -- but a
        # visitor-facing "now folding X" printed on top of "the booth cannot
        # fold right now" is exactly the contradiction worth two lines of
        # care.
        viewer_page = Gtk.Overlay()
        viewer_page.set_hexpand(True)
        viewer_page.set_vexpand(True)
        self._viewer_page = viewer_page
        # The quad goes in as this overlay's CHILD, so both overlays below
        # sit over whichever view is up -- and so a `hello` naming a
        # different set of chips can swap the whole grid without disturbing
        # them. `attach_cards` is what actually builds it (`_ensure_quad`),
        # from the card list this booth has so far: `[0]` until the daemon
        # says otherwise, which is what lets a booth with no socket at all
        # come up showing a viewer rather than a hole.
        self.attach_cards(self.cards)
        viewer_page.add_overlay(self._build_viewer_caption())
        viewer_page.add_overlay(self._build_preparing_overlay())

        # The hero slot holds either the protein or the gallery; the rail
        # stays put across both, so the silicon keeps visibly breathing
        # while a visitor is choosing what to fold.
        self.screens = Gtk.Stack()
        self.screens.set_hexpand(True)
        self.screens.set_vexpand(True)
        # The hero column: the render, and directly under it the caption
        # that says what the molecule is. A column rather than another
        # overlay because this text must never sit on top of the structure
        # -- see `_build_target_info`. `viewer_page` keeps vexpand, so the
        # caption takes only the height its two lines need and the protein
        # keeps the rest.
        viewer_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        viewer_column.set_hexpand(True)
        viewer_column.set_vexpand(True)
        viewer_column.append(viewer_page)
        viewer_column.append(self._build_target_info())
        self.screens.add_named(viewer_column, "viewer")
        self._build_gallery()

        # The hero slot, with the easter egg laid over it and NOTHING else.
        #
        # Wrapping the stack rather than the window is what makes the egg's
        # own claim -- "the rail on the right is still live" -- checkable by a
        # visitor instead of merely asserted: the pipeline and the chip
        # telemetry stay uncovered beside it. Doing it structurally also means
        # no arithmetic against `_SIDE_RAIL_WIDTH_PX`, which was the first
        # attempt and left the rail's heading half washed out.
        hero = Gtk.Overlay()
        hero.set_hexpand(True)
        hero.set_vexpand(True)
        hero.set_child(self.screens)
        hero.add_overlay(self._build_egg_overlay())

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root.add_css_class("booth-root")
        root.append(hero)
        root.append(self._build_side_rail())

        logo = Gtk.Label(label=TT_BIO_LOGO)
        logo.add_css_class("booth-logo")
        logo.set_halign(Gtk.Align.END)
        logo.set_valign(Gtk.Align.END)
        logo.set_margin_end(28)
        logo.set_margin_bottom(22)

        root_overlay = Gtk.Overlay()
        # The overlay paints the same ground as the box inside it, so its
        # own overlay children (the logo, the help card) sit on a known,
        # explicitly-set background rather than on whatever the desktop
        # theme would otherwise show through -- which is also what makes
        # them contrast-checkable (see `_BACKGROUND_BY_CLASS`).
        root_overlay.add_css_class("booth-root")
        root_overlay.set_child(root)
        root_overlay.add_overlay(logo)
        root_overlay.add_overlay(self._build_help_overlay())
        window.set_child(root_overlay)

        # A kiosk: no chrome, no window management, the protein as large as
        # the glass allows. --windowed opts out for development; Ctrl+F
        # toggles either way at runtime.
        if not self.windowed:
            window.fullscreen()
        self._connect_visitor_input(window)
        window.present()

        # Every cell spins on its own tick callback -- `_ensure_quad` starts
        # any cell it builds later, so this only ever covers the ones already
        # on screen when the window is presented.
        for viewer in (self.quad.viewers if self.quad is not None else ()):
            viewer.start_animation()
        self._sync_to_state(force=True)

        self.sampler.start()
        # NOT `self.chipviz_panel.set_running(True)` any more. The panel is
        # closed by default (see `chipviz_visible`), and a closed panel polls
        # nothing: `_set_chipviz_visible` starts and stops the AICLK source
        # with the panel, so the booth's default state costs no sysfs reads
        # and no JS at all. `_sync_chipviz` still runs, because the panel has
        # to be aimed at the right mode BEFORE it is ever shown -- otherwise
        # the first thing a visitor pressing `T` mid-fold would see is an
        # idle animation over a running fold.
        self._sync_chipviz()
        self._start_timers()

        if self.socket_path:
            self._start_client()

    def do_shutdown(self):
        """Stop the two background threads this app owns before GTK tears
        the process down. Both are daemon threads, so this is tidiness
        rather than a correctness requirement -- but `TelemetrySampler.stop`
        joins, which makes an in-flight `tt-smi` call finish against a live
        process rather than one already unwinding."""
        try:
            self.sampler.stop()
            if self.chipviz_panel is not None:
                # Removes the GLib poll source outright (ui/chipviz.py's
                # `set_running`), rather than leaving a timer registered
                # against a widget that is going away.
                self.chipviz_panel.set_running(False)
            if self._client is not None:
                self._client.stop()
            # The easter egg's timer, if a visitor left it running.
            self._stop_egg_source()
            self._log_frame_buffer_summary()
        except Exception:
            log.exception("error during shutdown")
        Gtk.Application.do_shutdown(self)

    def _log_frame_buffer_summary(self):
        """One line, at shutdown, saying whether the renderer kept up.

        `LatestFrameByJob` counted its drops from the beginning and nothing
        ever read them (docs/followups.md: "the cheapest available signal
        that the renderer is falling behind", never surfaced). Reported here
        rather than live because the two numbers mean opposite things and
        only one is news:

        - `dropped` is a frame superseded by a newer one for the same job.
          At 30 Hz against a 33 ms drain that is the buffer working, and a
          large number is expected.
        - `evicted` is a whole job's frames thrown away because more jobs had
          frames waiting than the buffer has slots. That one is worth
          reading, and on a healthy four-chip booth it stays at zero.

        A live indicator is a separate decision -- it needs a rate and a
        threshold, and neither has been measured yet -- so this is the honest
        cheap half: an operator reading the log after an all-day run gets an
        answer, and nothing costs anything per frame.
        """
        frames = getattr(self, "_frames", None)
        if frames is None:
            return
        dropped = getattr(frames, "dropped", 0)
        evicted = getattr(frames, "evicted", 0)
        if evicted:
            log.warning("frame buffer: %d job(s) evicted for want of a slot, "
                        "%d frame(s) superseded -- the renderer fell behind",
                        evicted, dropped)
        else:
            log.info("frame buffer: no jobs evicted, %d frame(s) superseded "
                     "(normal; latest-wins)", dropped)

    # ── layout ───────────────────────────────────────────────────────────

    def _build_side_rail(self):
        """The fixed-width column: identity, then what the machine is doing,
        then what the silicon is doing.

        `set_hexpand(False)` plus an explicit width is load-bearing, not a
        preference -- see `_SIDE_RAIL_WIDTH_PX`. So is `_FixedWidthBox`:
        `set_size_request` pins only the floor, and it was the rail's
        NATURAL width (which the Tensix panel moved by 32px) that shifted
        the hero slot every time a visitor pressed `T`.
        """
        _ensure_app_css_installed()
        side = _FixedWidthBox(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        side.add_css_class("booth-side")
        side.set_size_request(_SIDE_RAIL_WIDTH_PX, -1)
        side.set_hexpand(False)
        side.set_valign(Gtk.Align.START)
        for margin in ("set_margin_top", "set_margin_bottom",
                       "set_margin_start", "set_margin_end"):
            getattr(side, margin)(18)

        title = Gtk.Label(label="Folding on Tenstorrent Blackhole")
        title.add_css_class("booth-title")
        title.set_xalign(0.0)
        title.set_wrap(True)
        # The claim this line carries is that the trajectory is LIVE and is
        # being computed on the hardware present -- not replayed from a file.
        # The wording matches `_HELP_INTRO`'s own "a few feet away", which is
        # this project's established way of saying it.
        subtitle = Gtk.Label(
            label="live diffusion trajectory, computed a few feet away")
        subtitle.add_css_class("booth-sub")
        subtitle.set_xalign(0.0)
        subtitle.set_wrap(True)
        side.append(title)
        side.append(subtitle)

        self.pipeline_panel = PipelinePanel()
        self.telemetry_panel = TelemetryPanel()
        # Directly BELOW the telemetry panel, sharing its left-to-right chip
        # order, so animation N sits under chip N's readout: the two are one
        # instrument in two halves (the numbers, then the picture), which is
        # what makes the animation legible as "these four chips, right here"
        # rather than as generic decoration. Hides itself when unavailable
        # (no WebKit, no chips, no bundled assets) -- see ui/chipviz.py.
        self.chipviz_panel = ChipVizPanel()
        for panel in (self.pipeline_panel, self.telemetry_panel,
                      self.chipviz_panel):
            panel.set_hexpand(False)
            panel.set_vexpand(False)
            side.append(panel)
        # Hidden until `T`. Applied AFTER the append so it is the panel's
        # settled state, and via the same setter the key uses so there is
        # exactly one place that decides what "the Tensix panel is open"
        # means -- including the part the key cannot do, which is leaving an
        # UNAVAILABLE panel (no WebKit, no chips) hidden regardless.
        self._set_chipviz_visible(self.chipviz_visible)

        # Below the progress legend and the chip readout, in the space the
        # rail was leaving empty (see .superpowers/.../booth-wired.png): the
        # hint row, always visible, and the diagnostics panel it opens,
        # hidden until asked for. The protein is the hero; this is for the
        # curious visitor and for us.
        side.append(self._build_hint_row())
        self.diagnostics_panel = DiagnosticsPanel()
        self.diagnostics_panel.set_visible(self.diagnostics_visible)
        self.diagnostics_panel.refresh(self.diagnostics, force=True)
        side.append(self.diagnostics_panel)
        return side

    def _build_hint_row(self):
        """The small affordances that say the booth is interactive.

        All of them are clickable AND keyed, because the user asked for both
        ("with a press of a button or a click"), and because a booth may or
        may not have a keyboard in front of the public. Each click handler
        CLAIMS its gesture sequence, which is what stops the window-wide
        "any click is a visitor touch" gesture (see `_connect_visitor_input`)
        from also opening the gallery underneath the thing the visitor just
        pressed.
        """
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        row.set_halign(Gtk.Align.START)

        self._tensix_toggle_label = self._build_hint(
            row, self._tensix_hint_text(), self._toggle_chipviz)
        self._diagnostics_toggle_label = self._build_hint(
            row, self._diagnostics_hint_text(), self._toggle_diagnostics)
        self._build_hint(row, "?  HELP", self._show_help)
        return row

    def _build_hint(self, row, text, on_click):
        """One clickable hint label. Returns the label so its text can be
        updated (the diagnostics one changes as the panel opens/closes)."""
        label = Gtk.Label(label=text, xalign=0.0)
        label.add_css_class("booth-hint")
        click = Gtk.GestureClick()
        click.connect("pressed",
                      lambda gesture, *_args: self._on_hint_pressed(gesture, on_click))
        label.add_controller(click)
        row.append(label)
        return label

    def _on_hint_pressed(self, gesture, on_click):
        """One hint label was clicked or tapped.

        The `set_state(CLAIMED)` is the load-bearing line, not boilerplate:
        this gesture and the window-wide "any click is a visitor touch" one
        both sit in the bubble phase, so without claiming the sequence BOTH
        fire and the visitor gets a gallery they did not ask for on top of
        the panel they did. Split out of the lambda that installs it so that
        claim is directly testable with no display and no synthesized input
        (tests/unit/test_app_interaction.py).
        """
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._note_input()
        on_click()

    def _diagnostics_hint_text(self):
        return ("▾  DIAGNOSTICS  ·  D" if self.diagnostics_visible
                else "▸  DIAGNOSTICS  ·  D")

    def _tensix_hint_text(self):
        return ("▾  TENSIX  ·  T" if self.chipviz_visible
                else "▸  TENSIX  ·  T")

    def _build_gallery(self):
        """Load the playlist and build the pick grid, or ship without one.

        A manifest that is missing or malformed must not take the booth
        down and must not put a stack trace on the screen: it costs the
        gallery (a touch then simply leaves the protein up, and the idle
        timeout returns to attract on its own) and nothing else. The
        detail goes to the log, where an operator can act on it.
        """
        path = self.playlist_path or _DEFAULT_PLAYLIST
        try:
            self.targets = select_targets(load_playlist(path), self.target_ids)
        except PlaylistError:
            log.exception("playlist %s could not be loaded; the booth will "
                          "run without a gallery", path)
            self.targets = []
            return
        self.gallery = Gallery(self.targets, on_pick=self._on_pick,
                               width_px=_GALLERY_WIDTH_PX)
        # Cards at their natural width, centred -- NOT stretched to fill.
        # `Gallery` sets hexpand(True) on itself, which is right for a grid
        # that fills a window and wrong for this booth's one-target
        # playlist: rendered and looked at, a single card stretched to the
        # full 1490px hero slot is a 1300px-wide empty thumbnail tile with
        # one small letter adrift in it (verified on screen before this
        # line existed). Natural width is `columns x ~348px`, and
        # `grid_shape` already caps the column count at 400px per column,
        # so this can never want more room than the slot has. Vertical
        # expansion is left alone so a playlist taller than the screen
        # still scrolls.
        self.gallery.set_hexpand(False)
        self.gallery.set_halign(Gtk.Align.CENTER)
        self.screens.add_named(self.gallery, "gallery")

    def _build_preparing_overlay(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_hexpand(True)
        box.set_vexpand(True)
        box.set_halign(Gtk.Align.FILL)
        box.set_valign(Gtk.Align.FILL)
        box.add_css_class("preparing-overlay")

        title = Gtk.Label(label="Preparing")
        title.add_css_class("preparing-title")
        title.set_halign(Gtk.Align.CENTER)
        title.set_valign(Gtk.Align.CENTER)
        title.set_vexpand(True)

        message = Gtk.Label()
        message.add_css_class("preparing-message")
        message.set_halign(Gtk.Align.CENTER)
        message.set_wrap(True)
        message.set_justify(Gtk.Justification.CENTER)

        box.append(title)
        box.append(message)

        self._preparing_box = box
        self._preparing_message_label = message
        return box

    def _build_viewer_caption(self):
        """The two lines that keep a held structure honest.

        A small card pinned to the TOP-CENTRE of the hero slot, sized to its
        content (`Align.CENTER` on both axes rather than `FILL`) -- unlike
        the preparing overlay, this one must not cover the thing it is
        talking about. Top rather than bottom: the booth's logo already owns
        the bottom-right corner, and the protein is framed around the centre
        of the slot (measured at a median 68% of frame height, see
        ui/viewer.py's camera notes), so the top strip is the one place a
        card can sit without landing on the structure.

        Starts invisible and is driven only by `_sync_viewer_hold`. Both
        labels take a colour-bearing class from `_APP_CSS` -- see that
        stylesheet and the legibility guard in
        tests/unit/test_app_interaction.py, which walks this tree.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("viewer-caption")
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.START)
        box.set_margin_top(24)
        # Never grows the hero slot: the caption is chrome over the viewer,
        # and an overlay child that expands would fight the GL area for it.
        box.set_hexpand(False)
        box.set_vexpand(False)
        box.set_visible(False)

        title = Gtk.Label()
        title.add_css_class("viewer-caption-title")
        title.set_halign(Gtk.Align.CENTER)

        sub = Gtk.Label()
        sub.add_css_class("viewer-caption-sub")
        sub.set_halign(Gtk.Align.CENTER)
        sub.set_wrap(True)
        sub.set_justify(Gtk.Justification.CENTER)

        box.append(title)
        box.append(sub)

        self._caption_box = box
        self._caption_title_label = title
        self._caption_sub_label = sub
        # A tree built after the booth already had something to say (a
        # re-activate, or a test constructing this in isolation) must not
        # come up blank; `_sync_viewer_hold` is idempotent and re-applies
        # whatever the current answer is.
        self._sync_viewer_hold()
        return box

    def _build_target_info(self):
        """The caption under the render: what this protein actually is.

        A strip in the layout, below the GL area -- NOT an overlay over it.
        An overlay would either cover the structure or have to dodge it;
        this is a caption, so it goes where a caption goes, under the
        picture, in space the viewer gives up rather than space it loses.

        Left-aligned rather than centred so the eye finds the start of the
        line in the same place every time as the copy changes length, which
        is the difference between reading it and re-finding it. Both labels
        take a colour-bearing class from `_APP_CSS` -- see that stylesheet
        and the legibility guard in tests/unit/test_app_interaction.py.

        The strip is a ROW, not a column: the caption's two lines on the
        left, and the confidence legend (`_build_confidence_legend`) sitting
        at the far right of the same row, bottom-aligned with the tagline.
        Beside rather than below on purpose -- stacked, the legend would add
        its own height to this strip, and every pixel this strip takes is a
        pixel the protein above it loses. Beside, the strip is still exactly
        as tall as the caption's two lines, and the legend costs the render
        nothing at all. `tests/unit/test_app_interaction.py` measures that.
        """
        _ensure_app_css_installed()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=32)
        box.add_css_class("target-info")
        box.set_halign(Gtk.Align.FILL)
        # Never steals height from the protein: this strip is exactly as
        # tall as its two lines, and the viewer above it takes the rest.
        box.set_vexpand(False)

        caption = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        # Takes whatever the legend does not: the tagline wraps into it
        # rather than pushing the legend off the end of the strip.
        caption.set_hexpand(True)

        name = Gtk.Label()
        name.add_css_class("target-info-name")
        name.set_xalign(0.0)
        name.set_wrap(True)

        tagline = Gtk.Label()
        tagline.add_css_class("target-info-tagline")
        tagline.set_xalign(0.0)
        tagline.set_wrap(True)

        caption.append(name)
        caption.append(tagline)
        box.append(caption)
        box.append(self._build_confidence_legend())

        self._target_info_box = box
        # The half of the strip that disappears when the playlist cannot
        # name what is on screen -- the caption only. The legend describes
        # the RIBBON, which is still there and still coloured either way, so
        # it is deliberately not hidden with the words about the molecule.
        self._target_info_caption_box = caption
        self._target_info_name_label = name
        self._target_info_tagline_label = tagline
        # Idempotent, like `_build_viewer_caption`: a tree built after the
        # booth already knows what it is folding must not come up blank.
        self._sync_target_info()
        return box

    def _build_confidence_legend(self):
        """The subtle, always-on key to the ribbon's colours.

        Four booth targets out of five come back in visibly different
        colours -- Trp-cage deep blue at pLDDT 95.3, the DNA duplex blue at
        95.7, and FKBP12/DHFR/trypsin yellow-to-orange at 50.8/52.9/39.5,
        because those three are folded with no evolutionary alignment to
        lean on. Without a key that difference reads as decoration. With
        one, a visitor can see for themselves that the model is telling
        them how much of what it drew to believe.

        Deliberately NOT an explanation: one line saying what the colour
        is, then the ramp itself with its two ends named. Anyone who wants
        the thresholds and what each band is worth presses `?`, where
        `_PLDDT_LEGEND` is spelled out in full. Both are built from the
        same tuple and therefore from `ui.geometry.PLDDT_STOPS`; neither
        contains a copy of the ramp's colours.

        Returns a widget that never expands: `hexpand` stays False and the
        row is bottom-aligned, so it takes its natural width at the end of
        the caption strip and adds no height to it (the caption's two lines
        are taller than the whole legend). See `_build_target_info`.
        """
        _ensure_app_css_installed()
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        column.set_hexpand(False)
        column.set_halign(Gtk.Align.END)
        # Bottom-aligned against the caption beside it: the legend sits on
        # the tagline's baseline rather than floating in the middle of a
        # strip whose height is set by two much larger lines.
        column.set_valign(Gtk.Align.END)

        caption = Gtk.Label(label=_CONFIDENCE_LEGEND_CAPTION, xalign=1.0)
        caption.add_css_class("confidence-legend-caption")
        column.append(caption)

        ramp = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ramp.set_halign(Gtk.Align.END)

        low = Gtk.Label(label=_CONFIDENCE_LEGEND_LOW)
        low.add_css_class("confidence-legend-end")
        ramp.append(low)

        # The swatches themselves, low confidence first -- see
        # `_CONFIDENCE_LEGEND_CAPTION`'s comment for why this walks the ramp
        # backwards, and why the reversal is derived rather than retyped.
        # A swatch is a painted BOX, never a coloured label: `#0053D6`
        # measures 2.54:1 on this ground and could not legally be text here.
        swatches = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        swatches.set_valign(Gtk.Align.CENTER)
        for css_class, _range_text, _meaning in reversed(_PLDDT_LEGEND):
            swatch = Gtk.Box()
            swatch.add_css_class("confidence-legend-swatch")
            swatch.add_css_class(css_class)
            swatches.append(swatch)
        ramp.append(swatches)

        high = Gtk.Label(label=_CONFIDENCE_LEGEND_HIGH)
        high.add_css_class("confidence-legend-end")
        ramp.append(high)

        column.append(ramp)
        self._confidence_legend_box = column
        return column

    def _target_tagline(self, target_id):
        """A target's one-line description from the playlist, or None.

        None, never invented text, when the playlist has no tagline for it
        -- the caption then shows the name by itself. Same rule as
        `_target_name`: this booth does not fabricate copy about a molecule.
        """
        if not target_id:
            return None
        for target in self.targets:
            if target.id == target_id:
                return target.tagline
        return None

    def _sync_target_info(self):
        """Point the caption at whichever protein is on screen.

        Idempotent and tolerant of every collaborator being absent, like
        `_sync_viewer_hold`, which is what calls it -- so the caption
        reconciles on every event, touch, pick and 100ms tick, and headless
        tests that build no widgets at all still drive the decision.
        """
        # The FOCUS cell's, because this line sits under the hero image and
        # describes the molecule in it. Four cells' worth of it would be four
        # captions under one picture.
        focus = self._slot_view(self.router.focus_slot if self.router else 0)
        subject = target_info_subject(
            shown_target_id=focus.shown_target_id if focus else None,
            folding_target_id=focus.current_target_id if focus else None,
        )
        name = self._target_name(subject)
        self._target_info = (
            (name, self._target_tagline(subject)) if name else None)

        if self._target_info_caption_box is None:
            return
        try:
            # No name means the playlist cannot identify what is on screen.
            # The words go away rather than showing a blank line or a raw
            # wire id -- the same choice `_target_name` makes. Only the
            # words: the legend beside them describes the ribbon's colours,
            # which are on screen and meaningful whether or not this booth
            # can put a name to the molecule they belong to.
            self._target_info_caption_box.set_visible(
                self._target_info is not None)
            if self._target_info is None:
                return
            shown_name, shown_tagline = self._target_info
            self._target_info_name_label.set_label(shown_name)
            # A target with no tagline shows its name alone: the empty label
            # is hidden outright so it does not leave a gap of leading under
            # the name.
            self._target_info_tagline_label.set_label(shown_tagline or "")
            self._target_info_tagline_label.set_visible(bool(shown_tagline))
        except Exception:
            log.exception("protein caption update dropped")

    # ── the `?` card ─────────────────────────────────────────────────────

    def _build_egg_overlay(self):
        """The easter egg's card: the mark, condensing, and what it is not.

        It covers the HERO SLOT only -- see `do_activate`, where it is laid
        over the screen stack rather than over the window. The claim on the
        card ("the rail on the right is still live") is then something a
        visitor can check with their own eyes, and an assertion a visitor can
        check is worth more than one they have to take on trust. That is also
        why this is a widget over the booth rather than anything that touches
        the state machine, the socket, or the protein's viewer.
        """
        _ensure_app_css_installed()
        ground = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        ground.add_css_class("egg-overlay")
        ground.set_hexpand(True)
        ground.set_vexpand(True)
        ground.set_halign(Gtk.Align.FILL)
        ground.set_valign(Gtk.Align.FILL)
        # The wash is FULL BLEED and the padding is in the CSS, not on the
        # widget: a margin here would leave the hero slot's own corners
        # showing through around the card -- which put the pLDDT legend,
        # unwashed and looking live, under an easter egg the first time this
        # was looked at on real glass.

        title = self._help_label(_EGG_TITLE, "egg-title")
        title.set_halign(Gtk.Align.CENTER)
        ground.append(title)

        # Its own viewer, its own colour, and no turntable: the mark is a
        # plane figure, so the spin that makes a protein readable would put
        # this edge-on inside five seconds. See ui/viewer.py's two setters.
        self.egg_viewer = StructureViewer()
        self.egg_viewer.set_hexpand(True)
        self.egg_viewer.set_vexpand(True)
        self.egg_viewer.set_point_color(BRAND_PURPLE)
        self.egg_viewer.set_spin_rate(0.0)
        ground.append(self.egg_viewer)

        for text, css_class in ((_EGG_BODY, "egg-body"),
                                (_EGG_DISCLAIMER, "egg-disclaimer"),
                                (_EGG_PROVENANCE["asking"], "egg-provenance"),
                                (_EGG_NOTE, "egg-note")):
            label = self._help_label(text, css_class, wrap=True)
            label.set_halign(Gtk.Align.CENTER)
            label.set_justify(Gtk.Justification.CENTER)
            if css_class == "egg-provenance":
                # The one label on this card that is rewritten while it is up.
                # Kept so `_sync_egg_provenance` can reach it; the card is
                # built once and lives for the process, so there is no
                # lifetime question here beyond "may be None headlessly".
                self._egg_provenance_label = label
            ground.append(label)

        ground.set_visible(False)
        self._egg_box = ground
        return ground

    def _build_help_overlay(self):
        """The help card: what this booth is, every key, and what the panels
        on the right actually mean.

        Written for a visitor who has never heard of any of this -- no
        jargon that isn't unpacked in the same sentence -- and true: the
        fold really is running on the chips a few feet away while this card is
        on top of it (the overlay is a widget, not a modal loop; every GLib
        source underneath keeps firing, which is the point of building it
        this way and is asserted in tests/unit/test_app_interaction.py).
        """
        _ensure_app_css_installed()
        ground = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        ground.add_css_class("help-overlay")
        ground.set_hexpand(True)
        ground.set_vexpand(True)
        ground.set_halign(Gtk.Align.FILL)
        ground.set_valign(Gtk.Align.FILL)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        card.set_halign(Gtk.Align.CENTER)
        # vexpand AND valign=CENTER: in a vertical box, `valign` alone only
        # centres a child inside the (natural-height) cell it was given, so
        # the card would sit at the top of a 1080px screen with half the
        # glass empty below it -- looked at, on real glass, before this line
        # existed. Expanding first gives it the whole column to centre in.
        card.set_vexpand(True)
        card.set_valign(Gtk.Align.CENTER)
        card.set_size_request(980, -1)
        for margin in ("set_margin_top", "set_margin_bottom",
                       "set_margin_start", "set_margin_end"):
            getattr(card, margin)(40)

        card.append(self._help_label("What you are looking at", "help-title"))
        for paragraph in _HELP_INTRO:
            card.append(self._help_label(paragraph, "help-body", wrap=True))

        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=56)
        columns.set_homogeneous(True)
        columns.append(self._build_help_keys())
        columns.append(self._build_help_panels())
        card.append(columns)

        card.append(self._help_label(
            "The fold carries on behind this card — nothing is paused.",
            "help-note", wrap=True))

        ground.append(card)
        ground.set_visible(False)
        self._help_box = ground
        return ground

    @staticmethod
    def _help_label(text, css_class, wrap=False):
        """One label on the help card.

        Every label built here takes a colour-bearing class from `_APP_CSS`
        -- the project's legibility rule ("an explicitly-set background
        implies an explicitly-set foreground") applies to this card exactly
        as it does to the panels, and the shared guard in
        tests/unit/_legibility.py now walks this tree to prove it.
        """
        label = Gtk.Label(label=text, xalign=0.0)
        label.add_css_class(css_class)
        label.set_wrap(wrap)
        if wrap:
            label.set_max_width_chars(88)
        return label

    def _build_help_keys(self):
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        column.append(self._help_label("KEYS", "help-section"))
        grid = Gtk.Grid(column_spacing=20, row_spacing=8)
        for row_index, (keys, meaning) in enumerate(_KEY_HELP):
            key_label = self._help_label(keys, "help-key")
            key_label.set_valign(Gtk.Align.START)
            meaning_label = self._help_label(meaning, "help-desc", wrap=True)
            meaning_label.set_max_width_chars(34)
            grid.attach(key_label, 0, row_index, 1, 1)
            grid.attach(meaning_label, 1, row_index, 1, 1)
        column.append(grid)
        return column

    def _build_help_panels(self):
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        column.append(self._help_label("THE QUAD, AND THE PANELS ON THE RIGHT",
                                       "help-section"))
        for paragraph in _HELP_PANELS:
            label = self._help_label(paragraph, "help-desc", wrap=True)
            label.set_max_width_chars(52)
            column.append(label)

        # Per RESIDUE, not per atom. ui/geometry.py's `load_backbone_trace`
        # reads one pLDDT per residue -- its anchor atom's B-factor -- and that is
        # what colours the ribbon a visitor is looking at while reading
        # this legend. ui/diagnostics.py's STAGE_TEACHING carried the same
        # error and is fixed with it.
        column.append(self._help_label(
            "pLDDT — the model's own confidence, per residue:", "help-desc",
            wrap=True))
        for css_class, range_text, meaning in _PLDDT_LEGEND:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            # A swatch is a painted BOX, never a coloured label: the top of
            # the ramp (#0053D6) measures 2.54:1 on this ground and would be
            # illegible as text. The words next to it are ordinary body
            # text on the card's own ground.
            swatch = Gtk.Box()
            swatch.add_css_class("plddt-swatch")
            swatch.add_css_class(css_class)
            swatch.set_valign(Gtk.Align.CENTER)
            row.append(swatch)
            row.append(self._help_label(f"{range_text}  ·  {meaning}",
                                        "help-desc", wrap=True))
            column.append(row)
        return column

    def _connect_visitor_input(self, window):
        """Any tap, click or keypress reaches the booth through here.

        Which of those a venue actually has is a booth-setup decision (the
        plan leaves touchscreen hardware out of this phase on purpose), so
        all three are wired to the same place. The gesture sits on the
        window in the default bubble phase, so a tap on a gallery card (or
        on a hint label, which CLAIMS its sequence -- see `_build_hint`) is
        that widget's first.

        Almost every key is still just "a visitor touched the booth". The
        handful that are not -- `?`, `D`, `Esc`, and the operator's two
        Ctrl chords -- are decided in `_handle_key`, which takes a plain
        key NAME and a plain bool so that decision is testable with no GTK,
        no window and no display.
        """
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_args: self._on_click())
        window.add_controller(click)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        window.add_controller(keys)

    def _on_key_pressed(self, _controller, keyval, _keycode, modifiers):
        """GTK adapter: turn a keyval + modifier mask into the two plain
        values `_handle_key` reasons about, and nothing else."""
        name = Gdk.keyval_name(keyval) or ""
        ctrl = bool(modifiers & Gdk.ModifierType.CONTROL_MASK)
        return self._handle_key(name, ctrl=ctrl)

    # ── the booth's screen, reconciled against the machine ───────────────

    def _sync_to_state(self, force=False):
        """Make the screen agree with `self.states`. Called after every
        single thing that can move the machine -- events, touches, picks,
        the clock -- and idempotent, so calling it more often than
        necessary costs nothing.

        Note what is NOT here any more: the buffered-frame drain that used
        to fire on the booth-wide `showcase_ended` edge. A cell's dwell is
        that cell's own now (`ui.slots.SlotState`), and draining all four
        cells because ONE of them finished a showcase is the single global
        buffer's defect wearing the state machine's clothes. `_tick_state_at`
        drains the cells the router reports as actually changed.
        """
        state = self.states.state
        previous, self._last_state = self._last_state, state

        # BEFORE the early return below, not after: leaving a showcase is a
        # state change, but so is the ordinary tick that finds nothing has
        # moved, and the caption has to appear the instant the hold begins
        # rather than at the next transition. Idempotent and cheap (it does
        # nothing at all unless its own answer changed), so running it on
        # every tick costs a comparison.
        self._sync_viewer_hold()
        # Before the early return too, and for the same reason: the pick's
        # status changes without the booth's state changing at all (the
        # daemon starts the fold; the ten-second window passes; the pick
        # expires), and a visitor standing in front of a stale banner is
        # exactly the failure the banner exists to prevent. Idempotent --
        # `QuadView.set_notice` no-ops on an unchanged string.
        self._sync_quad_notice(self._tick_now)

        if state == previous and not force:
            return

        if self.screens is not None:
            self.screens.set_visible_child_name(
                "gallery" if state == "gallery" and self.gallery is not None
                else "viewer")

        if self._preparing_box is not None:
            # `incompatible` gets the SAME neutral overlay as `not_ready`
            # (Task 3's ruling, landed with Task 17's copy). From a visitor's
            # side the two are one thing: a booth that is not going to fold
            # anything for them. The two protocol version numbers that
            # produced it go to the log and the diagnostics rail and never to
            # the glass -- `_PREPARING_MESSAGE` is a constant and is never
            # composed from anything.
            is_preparing = (state == "preparing"
                            or self._connection_state == "incompatible")
            self._preparing_box.set_visible(is_preparing)
            if is_preparing:
                self._preparing_message_label.set_label(self.display_message)

    # ── holding the previous structure ───────────────────────────────────

    def _target_name(self, target_id):
        """A target's display name from the playlist, or None.

        None, never the raw `target_id`, when the playlist does not know it:
        ids are wire data and read like internals (`trpcage_no_msa`), and
        `viewer_hold_caption` already has a claims-less fallback for every
        line that would have used one. An operator who needs the id has the
        log line `job_start` writes and the diagnostics tap, both of which
        name it in full.
        """
        if not target_id:
            return None
        for target in self.targets:
            if target.id == target_id:
                return target.name
        return None

    def _end_fold_in_flight(self, slot):
        """The fold that was running on cell `slot` has stopped without
        producing a frame (`job_error`), or the daemon has stopped folding at
        all (`not_ready`, which ends all of them).

        Takes down that cell's "now folding X" claim and nothing else. What
        is on screen is deliberately left alone: it is a real structure that
        was really computed, and replacing it with an empty viewer is the
        defect this file's second docstring section exists to remove -- a
        failed fold is a reason to stop asserting, not a reason to blank the
        booth. The next fold's first frame will supersede it in the ordinary
        way. And it is ONE cell: a fold failing on chip 1 says nothing at all
        about the three folds still running beside it.
        """
        view = self._slot_view(slot)
        if view is None:
            return
        view.awaiting_first_frame = False
        view.current_job_id = None
        view.current_target_id = None
        self._sync_viewer_hold()

    def _slot_view(self, slot):
        """Cell `slot`'s `_SlotView`, or None if there is no such cell.

        The bounds check, in one place, for the same reason `QuadView._cell`
        is: a slot index reaches this class from wire-shaped data (a `card`
        the router turned into an index), so an out-of-range one is ordinary
        input and must not raise out of a GLib callback.
        """
        if not isinstance(slot, int) or isinstance(slot, bool):
            return None
        if 0 <= slot < len(self._slots):
            return self._slots[slot]
        return None

    def _sync_viewer_hold(self):
        """Reconcile, for every cell, the two things that say "the structure
        on screen belongs to a fold that is over, and a new one is being
        computed": that cell's dim, and the caption over the hero.

        Idempotent and safe to call from anywhere -- it is called from
        `_sync_to_state` (so every event, touch, pick and 100ms tick
        reconciles it) and directly from the branches that change the answer
        without changing the booth's state: `job_start`, `job_error` and the
        frame drain.

        The DIM is per cell, because whether a structure is a leftover is a
        fact about the fold that produced it. The words are not: there is one
        hero caption, over one hero image, and it speaks for the cell the
        booth is following (`SlotRouter.focus_slot`). Two cells' captions in
        one overlay would be two claims about one picture.

        Tolerates every collaborator being absent, like the rest of this
        file: headless tests substitute a quad and build no caption at all,
        and every half below is independently guarded.
        """
        focus = self.router.focus_slot if self.router is not None else 0
        caption = None
        for index, view in enumerate(self._slots):
            slot_caption = viewer_hold_caption(
                awaiting_first_frame=view.awaiting_first_frame,
                has_structure=view.has_structure,
                showcasing=self.router.slots[index].state == "showcase",
                folding_name=self._target_name(view.current_target_id),
                held_name=self._target_name(view.shown_target_id),
            )
            if index == focus:
                caption = slot_caption
            viewer = self._viewer_for(index)
            if viewer is not None:
                # Dim exactly when the caption explains why. Deriving both
                # from the one decision is what stops a dimmed structure from
                # ever appearing without the words that make it honest --
                # and, in the other direction, stops the caption from
                # appearing over a viewer with nothing in it to dim
                # (`slot_caption` is truthy in the empty case too, hence the
                # second term).
                viewer.set_held(slot_caption is not None and view.has_structure)
        self._caption = caption

        # Driven from here rather than from its own timer so the two
        # captions can never disagree about which fold is which: they are
        # reconciled from the same fields in the same pass. See the block
        # above `target_info_subject` for how the two divide the work.
        self._sync_target_info()

        if self._caption_box is None:
            return
        if caption is None:
            self._caption_box.set_visible(False)
            return
        title, sub = caption
        self._caption_title_label.set_label(title)
        self._caption_sub_label.set_label(sub)
        self._caption_box.set_visible(True)

    # ── visitor input ────────────────────────────────────────────────────

    def _on_touch(self):
        """A visitor touched the booth."""
        self._note_input()
        self.states.on_touch()
        self._sync_to_state()

    def _on_click(self):
        """A click or tap anywhere on the window.

        One rule, and it is the same one the keyboard follows: if the help
        card is up, this dismisses it and does nothing else. A visitor who
        taps to get rid of the help card has not asked to open a gallery
        behind it, and finding one there would be exactly the "why did it
        do that" moment this whole task exists to remove.
        """
        self._note_input()
        if self.help_visible:
            self._set_help_visible(False)
            return
        self._on_touch()

    def _handle_key(self, name, ctrl=False):
        """Decide what one key press means. Returns True if the booth
        consumed it (GTK's "handled"), which is always -- a kiosk has
        nowhere else for a key to go.

        Deliberately takes a NAME and a BOOL rather than a GDK keyval and a
        modifier mask: this is the whole of the booth's keyboard policy, it
        is the part a visitor and an operator both depend on, and it is
        therefore the part that must be testable without a display. The GTK
        adapter is `_on_key_pressed`, three lines above, and it decides
        nothing.

        Order matters, and each step is here for a reason:

        1. The operator's Ctrl chords work from ANY screen, including with
           the help card up -- if the booth needs to be quit or unfullscreened
           at a venue, no visitor-facing state may stand in the way.
        2. With the help card up, ANY key closes it and nothing else
           happens. `?` and `Esc` are the documented ways out (per the
           user's request), but a visitor pressing something random while a
           wall of text is up means "get rid of this", not "and also open
           the gallery behind it".
        3. `?` opens the card from anywhere -- attract, gallery, folding,
           showcase, preparing. It is chrome; it does not touch booth state.
        4. `D` toggles diagnostics, `T` toggles the Tensix activity panel and
           `Q` toggles the quad; `Esc` closes whichever of the two panels is
           open. `Q` is deliberately NOT closed by `Esc`: the quad is a view
           of the same four folds, not something laid over them, so there is
           nothing for `Esc` to get out of the way of.
        5. Everything else is a visitor touch, exactly as before.
        """
        lowered = (name or "").lower()
        # ORDER MATTERS. A visitor pressing the key for a panel the
        # choreography opened means "close that" -- so the panel becomes
        # theirs BEFORE `_note_input` interrupts, otherwise the interrupt
        # closes it and the toggle immediately reopens it, and the key a
        # visitor pressed to dismiss something appears to do nothing.
        if lowered in _DIAGNOSTICS_KEYS:
            self.attract.disown("diagnostics")
        elif lowered in _TENSIX_KEYS:
            self.attract.disown("tensix")
        self._note_input()

        if ctrl and lowered == "q":
            log.info("operator quit (ctrl+q)")
            self.quit()
            return True
        if ctrl and lowered == "f":
            self._toggle_fullscreen()
            return True
        if ctrl and lowered in _EGG_KEYS:
            self._toggle_egg()
            return True
        if ctrl:
            # An unbound chord is swallowed rather than treated as a touch:
            # a stray Ctrl+something must not open the gallery.
            return True

        if self.help_visible:
            self._set_help_visible(False)
            return True

        if self.egg_visible:
            # Same rule as the help card, for the same reason: something is
            # covering the booth, and any key means "get rid of this" rather
            # than "and also open the gallery behind it".
            self._set_egg_visible(False)
            return True

        if lowered in _HELP_KEYS:
            self._show_help()
            return True
        if lowered in _DIAGNOSTICS_KEYS:
            self._toggle_diagnostics()
            return True
        if lowered in _TENSIX_KEYS:
            self._toggle_chipviz()
            return True
        if lowered in _QUAD_KEYS:
            self._toggle_quad()
            return True
        if lowered == "escape":
            # Nothing to close but the two rail panels; and if both are shut
            # too, Escape does nothing at all -- notably it does NOT count
            # as a touch, so a visitor cannot back out of a screen into a
            # gallery they did not ask for.
            if self.diagnostics_visible:
                self._set_diagnostics_visible(False)
            if self.chipviz_visible:
                self._set_chipviz_visible(False)
            return True

        self._on_touch()
        return True

    # ── chrome: the two rail panels and the help card ────────────────────
    #
    # None of these is booth STATE -- they are chrome laid over whatever the
    # state machine is doing, which is precisely why `?` can work "at any
    # time" without ui/states.py growing a sixth state and every transition
    # in it growing an opinion about overlays. The one thing they do borrow
    # from the state machine is its idea that a visitor who walks away
    # should not leave the booth changed: `_tick_overlays` closes all three
    # after a period of no input at all.

    def _note_input(self):
        """Stamp "a human did something just now", for the overlay idle
        timers. Cheap and idempotent; called from every input path."""
        try:
            self._last_input_at = self._clock()
        except Exception:
            # A clock that raises must not cost the booth a keypress.
            log.exception("clock failed while stamping visitor input")
        # Someone is here: stop demonstrating, and put back whatever the
        # demonstration had opened. Anything the VISITOR opened is not ours
        # to close and `Choreography` will not name it.
        try:
            for cue in self.attract.interrupted():
                if cue == CLOSE_DIAGNOSTICS and self.diagnostics_visible:
                    self._set_diagnostics_visible(False)
                elif cue == CLOSE_TENSIX and self.chipviz_visible:
                    self._set_chipviz_visible(False)
                # HIDE_GALLERY IS DELIBERATELY NOT APPLIED HERE, and there is
                # deliberately no branch for it below. If someone arrives
                # while the menu is up, that menu is the screen they are
                # looking at and the thing they are most likely about to use
                # -- snatching it back the instant they touch anything would
                # be the worst possible moment. `interrupted()` releases
                # ownership either way, so the choreography will not close it
                # later; the state machine's own 45s idle timeout returns the
                # booth to the protein if they walk away.
                #
                # This comment used to sit directly above an `elif cue ==
                # HIDE_GALLERY` branch that applied the hide, and the promise
                # it makes was broken for every key that returns before
                # `_on_touch` -- `D`, `T`, `Q`, `?`. It survived because the
                # test for it pressed an UNBOUND key, which falls through to
                # `_on_touch()`, and that touch re-opened the gallery in the
                # same call: the bug put itself back before anything could
                # observe it. See
                # `test_a_visitor_reaching_for_a_panel_keeps_the_menu_too`,
                # which presses `D` and could not pass with that branch here.
                #
                # SHOW_GALLERY has no branch either, for a duller reason:
                # `interrupted()` returns closes only, so a show cue can
                # never arrive on this path. It is `_tick_attract` that
                # applies both.
        except Exception:
            log.exception("attract choreography could not be interrupted")

    def _toggle_diagnostics(self):
        self._set_diagnostics_visible(not self.diagnostics_visible)

    def _set_diagnostics_visible(self, visible):
        self.diagnostics_visible = visible
        if self.diagnostics_panel is not None:
            self.diagnostics_panel.set_visible(visible)
            if visible:
                # Repaint immediately rather than waiting up to 100ms for
                # the next tick: the panel must never appear empty (or
                # showing the state it had when it was last closed) in the
                # instant a visitor opens it.
                self.diagnostics_panel.refresh(self.diagnostics, force=True)
        if self._diagnostics_toggle_label is not None:
            self._diagnostics_toggle_label.set_label(self._diagnostics_hint_text())

    def _toggle_chipviz(self):
        self._set_chipviz_visible(not self.chipviz_visible)

    def _set_chipviz_visible(self, visible):
        """Open or close the Tensix activity panel.

        Two things this does beyond flipping `set_visible`, both of them the
        reason the key handler goes through here rather than at the widget:

        - an UNAVAILABLE panel (no WebKit, no chips, no bundled assets --
          ui/chipviz.py hides itself and sets `available` False) stays
          hidden. Showing an empty 146px box because someone pressed `T` on
          a machine with no Tenstorrent card in it would be worse than the
          key appearing to do nothing;
        - the panel's 1Hz AICLK poll and its JS evaluation follow the
          panel. Closed means closed: `set_running(False)` removes the GLib
          source outright, so the booth's default state costs no sysfs
          reads and no WebView work at all.

        `self.chipviz_visible` still tracks what was ASKED for, not what is
        on screen, so the hint reads honestly on a machine where the panel
        cannot be shown -- and, like the other two overlays, it is a plain
        bool a headless test can drive.
        """
        self.chipviz_visible = visible
        panel = self.chipviz_panel
        if panel is not None:
            available = getattr(panel, "available", True)
            try:
                panel.set_visible(bool(visible) and available)
                # Guarded with the visibility change rather than after it:
                # a WebView that fails to stop must not leave the panel's
                # visibility half-applied. See `_sync_chipviz` for the same
                # rule applied to the same widget.
                panel.set_running(bool(visible) and available)
            except Exception:
                log.exception("Tensix activity panel visibility change dropped")
        if self._tensix_toggle_label is not None:
            self._tensix_toggle_label.set_label(self._tensix_hint_text())

    # ── the quad ─────────────────────────────────────────────────────────

    def _toggle_quad(self):
        self._set_quad_visible(not self.quad_visible)

    def _set_quad_visible(self, visible):
        """Show all four chips at once, or go back to one large protein.

        The booth's default is one: four cells is the answer to "is this
        really running on four chips", and the single large view is the
        answer to "what does a protein look like" -- which is the question
        most visitors actually walked up with. So this is a toggle, by
        request, and the single view is what it starts from.

        It is not chrome. Nothing is covered, nothing is hidden behind it,
        and `_tick_overlays` deliberately does NOT close it after an idle
        period the way it closes the help card and the two rail panels: a
        visitor who walks away from the quad leaves a booth showing four
        honest folds, which is a perfectly good attract screen and not a
        state anyone needs rescuing from.

        The mechanism is `QuadView.set_solo_mode`, which hides the cells that
        are not the focus. One widget tree either way -- see the comment
        above that method for why the hero is a cell of the quad rather than
        a fifth viewer with its own camera.
        """
        self.quad_visible = bool(visible)
        if self.quad is None:
            return
        try:
            self.quad.set_solo_mode(not self.quad_visible)
        except Exception:
            log.exception("quad view toggle dropped")

    # ── the easter egg ───────────────────────────────────────────────────

    def _toggle_egg(self):
        self._set_egg_visible(not self.egg_visible)

    def _set_egg_visible(self, visible):
        """Open or close the easter egg (mark.py, runner/egg.py).

        Opening it asks the DAEMON to run a fresh descent on a chip and starts
        the one repeating source this feature owns; the numpy descent is not
        built here at all, and is built later only if no chip answers (see
        `_fall_back_to_cpu_egg`). A visitor who asks for it twice sees it
        collapse twice, from a fresh random seed both times, rather than being
        handed a finished logo.

        Closing it REMOVES that source rather than leaving it firing 30 times
        a second against a hidden widget, which is the same rule
        `ChipVizPanel.set_running` follows and for the same reason: this booth
        runs unattended all day. It also forgets the egg's id, so frames for
        it that are still in flight are dropped rather than drawn into
        whatever the next press puts up.

        Nothing here touches the state machine, the protein's viewer or the
        rail. It does touch the socket -- one ~60-byte message, through the
        same bounded outbox and background sender thread every client message
        uses, so the main loop never waits on it. The fold in flight keeps
        streaming into the viewer underneath, so dismissing this returns the
        booth to whatever it would have been showing anyway.
        """
        visible = bool(visible)
        self.egg_visible = visible
        if visible:
            # A wall of text under a toy helps nobody, and the help card's
            # own copy is about the fold this is temporarily covering.
            self._set_help_visible(False)
        if self._egg_box is not None:
            self._egg_box.set_visible(visible)
        self._stop_egg_source()
        self._egg = None
        self._egg_preview = None
        self._egg_refusal = None
        self._egg_deadline = None
        self._egg_device_done = False
        with self._egg_frames_lock:
            self._egg_frames.clear()
        if not visible:
            self._egg_id = None
            self.egg_source = None
            self.egg_card = None
            return
        try:
            self._egg_id = uuid.uuid4().hex
            self.egg_card = None
            self.egg_source = "asking"
            self._sync_egg_provenance()
            self._egg_preview = MarkCondensation()
            if self.egg_viewer is not None:
                self.egg_viewer.clear_structure()
                # Drawn, not stepped. See `_egg_preview`.
                self.egg_viewer.set_points(self._egg_preview.points())
            asked = self._ask_for_an_egg(self._egg_id)
            self._egg_deadline = time.monotonic() + _EGG_DEVICE_WAIT_MS / 1000.0
            if not asked:
                # No daemon, or a protocol this build refuses to speak to.
                # Nothing is ever coming, so do not make the visitor sit out
                # a timeout to learn it.
                self._fall_back_to_cpu_egg("cpu")
            self._egg_source_id = GLib.timeout_add(_EGG_STEP_MS, self._tick_egg)
        except Exception:
            # Fail-soft, like everything else a visitor can reach: an egg
            # that cannot be built is an egg that does not play. Nothing on
            # screen ever shows the reason.
            log.exception("easter egg could not be started")
            self.egg_visible = False
            self._egg = None
            self._egg_id = None
            if self._egg_box is not None:
                self._egg_box.set_visible(False)

    def _ask_for_an_egg(self, egg_id):
        """Send one `egg` message. Returns whether it was queued.

        Its own method so the "there is no daemon" path is one branch rather
        than a `getattr` chain inside `_set_egg_visible`, and so a test can
        drive both answers without a socket.
        """
        client = getattr(self, "_client", None)
        if client is None:
            return False
        try:
            return bool(client.send_egg(egg_id))
        except Exception:
            # `send_egg` promises not to raise; this is the belt-and-braces
            # every GLib callback in this file wears, because an exception
            # here would freeze the key handler for the life of the process.
            log.exception("could not ask the daemon for an easter egg")
            return False

    def _fall_back_to_cpu_egg(self, reason):
        """Run the descent here instead, and say so on the card.

        `reason` is the daemon's own short code ("busy") or "cpu" for "nobody
        answered at all"; it selects one of two sentences and never reaches
        the screen as text. Idempotent -- a refusal arriving just after the
        timeout has already fired must not restart the descent halfway
        through it.
        """
        if self._egg is not None:
            return
        self.egg_card = None
        self.egg_source = "cpu"
        self._egg_refusal = reason
        # The cloud already on screen, if there is one: the descent picks up
        # from exactly what the visitor has been looking at rather than
        # cutting to a different draw of the same distribution.
        self._egg = self._egg_preview or MarkCondensation()
        self._egg_preview = None
        log.info("easter egg falling back to the host CPU (%s), seed %d",
                 self._egg_refusal, self._egg.seed)
        self._sync_egg_provenance()
        if self.egg_viewer is not None:
            self.egg_viewer.set_points(self._egg.points())

    def _egg_provenance_text(self):
        """The one line on the card that changes. Pure, and total.

        Pure so that "does the label match where the arithmetic ran" is a
        question a test can ask directly, in one call, rather than by
        rendering a widget and reading a string off it -- and total so that a
        state this method has never heard of produces the CAUTIOUS sentence
        (the CPU one) rather than a stale claim about a chip. The device
        sentence is reachable from exactly one branch, and that branch needs
        both a `device` source and a card number that came off the wire.
        """
        if self.egg_source == "device" and self.egg_card is not None:
            return _EGG_PROVENANCE["device"].format(card=self.egg_card)
        if self.egg_source == "asking":
            return _EGG_PROVENANCE["asking"]
        return _EGG_FALLBACK.get(self._egg_refusal, _EGG_FALLBACK_DEFAULT)

    def _sync_egg_provenance(self):
        """Put `_egg_provenance_text` on the card. Tolerates no widget."""
        if self._egg_provenance_label is None:
            return
        try:
            self._egg_provenance_label.set_label(self._egg_provenance_text())
        except Exception:
            log.exception("easter egg provenance label update dropped")

    def _stop_egg_source(self):
        """Remove the egg's timer if it is registered. Idempotent."""
        if self._egg_source_id is not None:
            GLib.source_remove(self._egg_source_id)
            self._egg_source_id = None

    def _tick_egg(self):
        """One frame of the mark, on the main loop.

        This source is meant to STOP -- once the cloud has settled there is
        nothing left to draw and the mark simply holds until dismissed -- so
        unlike the booth's other repeating sources it can return False. The
        rule those sources exist to satisfy is still met: `keep` is decided
        before the try and an escaping exception leaves it False, so a failure
        stops the timer cleanly rather than freezing it or spraying a
        traceback 30 times a second. Whatever the cloud had reached stays on
        screen, still captioned as geometry.
        """
        keep = False
        try:
            keep = self._advance_egg()
        except Exception:
            log.exception("easter egg step dropped; leaving it where it landed")
        if not keep:
            self._egg_source_id = None
        return keep

    def _advance_egg(self):
        """Draw one frame of the egg. True to keep the timer.

        Three sources of a frame, checked in this order, and the order is the
        policy:

        1. **A chip's frame, if one is waiting.** Device frames win outright:
           if the chip answered at all, that is what a visitor watches.
        2. **The fallback's next step**, once one has been started.
        3. **Neither**, in which case this decides whether to keep waiting or
           to start the fallback -- either because the daemon refused (which
           is the ordinary busy-booth answer and arrives in well under a
           second) or because `_EGG_DEVICE_WAIT_MS` has passed with silence.

        Returning True while waiting is what keeps the card alive during (3);
        the idle timeout above still closes the whole thing if the visitor
        walks away.
        """
        if not self.egg_visible:
            return False
        frame = self._take_egg_frame()
        if frame is not None:
            return self._draw_egg_frame(frame)
        if self._egg is not None:
            points = self._egg.step()
            if self.egg_viewer is not None:
                self.egg_viewer.set_points(points)
            return not self._egg.done
        if self.egg_source == "device":
            if self._egg_device_done:
                # Every frame this run was going to send has been drawn. The
                # mark holds until the visitor dismisses it.
                return False
            if self._egg_deadline is not None and time.monotonic() >= self._egg_deadline:
                # The chip stopped mid-run and said nothing (the daemon sends
                # `egg_refused` when a worker dies, so this is the case where
                # the DAEMON went away too). Leave what is on screen and its
                # caption, both of which are still true of what a chip drew,
                # and stop the timer rather than spinning all day.
                log.info("the chip went quiet mid-egg; leaving it where it "
                         "landed")
                return False
            return True                  # buffer momentarily empty; wait
        refusal = self._egg_refusal
        if refusal is not None:
            self._fall_back_to_cpu_egg(refusal)
            return True
        if self._egg_deadline is not None and time.monotonic() >= self._egg_deadline:
            log.info("no chip answered the easter egg in %.1fs; running it here",
                     _EGG_DEVICE_WAIT_MS / 1000.0)
            self._fall_back_to_cpu_egg("cpu")
            return True
        return True                      # still waiting for a first frame

    def _take_egg_frame(self):
        """The oldest unplayed `egg_frame` for the CURRENT egg, or None.

        Frames for a superseded press are dropped here rather than at the
        socket, because `_egg_id` is main-loop state and the reader thread has
        no business reading it.

        Frames arriving after the booth has ALREADY started the fallback are
        dropped too, even though they carry the right id. That is the cold
        ttnn kernel cache again: the chip's first run takes ten seconds and
        the booth gives up at six, so the frames turn up mid-way through a
        descent the visitor is already watching. Cutting to them would be a
        jump, and would change the caption from CPU to chip half way through
        an animation that started on the CPU. Whichever one is drawing, the
        card says so, and it says so for the whole run.
        """
        with self._egg_frames_lock:
            while self._egg_frames:
                frame = self._egg_frames.popleft()
                if (frame.get("egg_id") == self._egg_id
                        and self.egg_source != "cpu"):
                    return frame
        return None

    def _draw_egg_frame(self, frame):
        """Draw one device frame. True to keep the timer.

        This is also where "computed on the chip" becomes true on screen: the
        claim is made when the FIRST frame from that chip is actually drawn,
        not when the request was sent and not when the event arrived -- so
        there is no window in which the card claims a chip that has produced
        nothing.
        """
        coords = unpack_coords(frame["coords_b64"])
        if self.egg_source != "device":
            self.egg_source = "device"
            self.egg_card = frame.get("card")
            # The waiting cloud is gone the moment a chip's own frame lands,
            # so a later refusal cannot resurrect it half way through what
            # the chip is drawing.
            self._egg_preview = None
            log.info("easter egg computed on chip %s (seed %s)",
                     self.egg_card, frame.get("seed"))
            self._sync_egg_provenance()
        if self.egg_viewer is not None:
            self.egg_viewer.set_points(coords)
        # The run is over when its LAST frame has been drawn -- not when the
        # buffer happens to be empty. Until then the deadline is pushed out,
        # so a chip that pauses is waited for and a chip that has stopped
        # entirely is eventually given up on.
        self._egg_device_done = frame.get("step") == frame.get("total")
        self._egg_deadline = (None if self._egg_device_done
                              else time.monotonic() + _EGG_DEVICE_WAIT_MS / 1000.0)
        return not self._egg_device_done

    def _show_help(self):
        self._set_help_visible(True)

    def _set_help_visible(self, visible):
        self.help_visible = visible
        if self._help_box is not None:
            self._help_box.set_visible(visible)

    def _toggle_fullscreen(self):
        """Operator escape hatch: get to the desktop without killing the
        booth. A no-op if there is no window yet (headless tests)."""
        if self._window is None:
            return
        if self._window.is_fullscreen():
            self._window.unfullscreen()
        else:
            self._window.fullscreen()

    def _tick_overlays(self, now):
        """Close chrome a visitor walked away from.

        Runs off the same 100ms tick as the state machine. All three timers
        are measured from the last input of ANY kind, so no overlay can
        close while someone is still pressing things -- and none can
        outlive the visitor who opened it, which is what the booth's own
        idle timeout guarantees for the screens the state machine owns.
        """
        if self._last_input_at is None:
            return
        idle_s = now - self._last_input_at
        if self.help_visible and idle_s >= _HELP_IDLE_S:
            log.info("help overlay closed after %.0fs idle", idle_s)
            self._set_help_visible(False)
        if self.diagnostics_visible and idle_s >= _DIAGNOSTICS_IDLE_S:
            log.info("diagnostics panel closed after %.0fs idle", idle_s)
            self._set_diagnostics_visible(False)
        if self.egg_visible and idle_s >= _EGG_IDLE_S:
            log.info("easter egg closed after %.0fs idle", idle_s)
            self._set_egg_visible(False)
        if self.chipviz_visible and idle_s >= _RAIL_PANEL_IDLE_S:
            log.info("Tensix activity panel closed after %.0fs idle", idle_s)
            self._set_chipviz_visible(False)

        self._tick_attract(now, idle_s)

    def _tick_attract(self, now, idle_s):
        """Let the booth show off its own instruments to an empty room.

        `D` and `T` are the two most interesting things in the rail and almost
        nobody presses either, so after a long idle the booth opens them
        itself, holds them long enough to read, and closes them again.

        The decision is `ui.attract`'s and is pure; this only applies it. Two
        guards live here rather than there because they need the widgets:
        a cue is skipped if the panel is already in the state it asks for
        (so we never fight `_tick_overlays` above), and every apply is
        wrapped -- a choreography that raised would take down the tick that
        also closes chrome and drives the state machine.
        """
        try:
            cues = self.attract.tick(now, idle_s)
        except Exception:
            log.exception("attract choreography tick failed; skipping")
            return
        for cue in cues:
            try:
                # A SKIPPED OPEN MUST DISOWN. `Choreography` takes ownership
                # when it EMITS an open, but the panel may already be up --
                # because a visitor opened it. Applying nothing while still
                # claiming it would mean the matching close later shuts a
                # panel somebody is reading, which is the one failure mode
                # this feature must not have.
                if cue == OPEN_DIAGNOSTICS:
                    if self.diagnostics_visible:
                        self.attract.disown("diagnostics")
                    else:
                        self._set_diagnostics_visible(True)
                elif cue == CLOSE_DIAGNOSTICS and self.diagnostics_visible:
                    self._set_diagnostics_visible(False)
                elif cue == OPEN_TENSIX:
                    if self.chipviz_visible:
                        self.attract.disown("tensix")
                    else:
                        self._set_chipviz_visible(True)
                elif cue == CLOSE_TENSIX and self.chipviz_visible:
                    self._set_chipviz_visible(False)
                elif cue == SHOW_GALLERY:
                    # ONLY FROM ATTRACT. The gallery is a state, not chrome,
                    # and this must never interrupt a fold somebody is
                    # watching or cut a finished structure's dwell short.
                    # From anywhere else the cue is dropped and its ownership
                    # released, so the matching hide cannot fire either.
                    if self.states.state == BoothState.ATTRACT:
                        self.states.on_touch()
                        self._sync_to_state()
                    else:
                        self.attract.disown("gallery")
                elif cue == HIDE_GALLERY:
                    # Back to the protein. Guarded on the state actually
                    # being the gallery: if a visitor has since picked a
                    # target, the booth is folding and must be left alone.
                    if self.states.state == BoothState.GALLERY:
                        self.states.state = BoothState.ATTRACT
                        self._sync_to_state()
            except Exception:
                log.exception("attract cue %r could not be applied", cue)

    def _on_pick(self, target_id):
        """A visitor picked a target off the gallery, and the booth asks the
        daemon to fold it next.

        Four things, in this order, and the order is the design:

        1. the ROUTER is told first (`select_target`). It is what decides the
           focus and what the booth says about the pick, and it must know
           before anything below can reconcile the screen against it.
        2. the STATE MACHINE closes the gallery, exactly as it always did.
        3. the DAEMON is asked (`_send_pick`), guarded. The request is a
           request: the daemon takes it at the head of its priority queue
           (Task 9) and dispatches it to the next chip to free, and it
           deliberately does NOT pre-empt a fold already running -- tearing a
           fold down mid-device-operation is a documented instability source,
           and pre-empting would blank a cell somebody is watching. With four
           chips the wait is bounded by the earliest-finishing of four folds.
        4. the SCREEN is reconciled, which puts the acknowledgement up.

        Nothing here waits on the daemon answering, and that is the whole
        point of the ordering: the acknowledgement is on screen at tap time
        whether the socket is healthy, wedged or absent. A visitor who taps
        and sees nothing concludes the booth is broken, and they are the one
        person in the room guaranteed to be watching.

        A failed send costs nothing visible. It is logged and it reaches the
        diagnostics rail; it never reaches the glass (see
        `_PICK_QUEUED_NOTICE`).
        """
        log.info("visitor picked %s", target_id)
        self._note_input()
        self._note_diagnostics(
            self.diagnostics.note, f"visitor picked {target_id}", KIND_MARK)
        if self._connection_state == "incompatible":
            # THE ONE CASE WHERE A TAP MUST NOT BE ACKNOWLEDGED. Everything
            # below promises a fold; this booth has already decided it cannot
            # speak to this daemon and is showing the "getting the booth
            # ready" overlay. Acknowledging anyway put "NEXT UP: FKBP12 --
            # starting on the next free chip" on top of that overlay: two
            # contradictory sentences on one screen, one of them a promise
            # nothing was ever going to keep (verified against a daemon with
            # PROTOCOL_VERSION 999). The overlay is the honest answer to the
            # tap, and it is already on the glass.
            log.info("pick %s dropped: this build cannot speak to that daemon",
                     target_id)
            self._note_diagnostics(
                self.diagnostics.note,
                f"pick {target_id} dropped: incompatible daemon", KIND_MARK)
            return
        if self.router is not None:
            # The SAME clock reading the state tick uses, kept in
            # `_tick_now`. A pick stamped off `self._clock()` while
            # `pick_status` is asked with the tick's reading would measure
            # the ten-second window between two different clocks, and the
            # booth would announce a long wait for a pick made a moment ago.
            self.router.select_target(target_id, now=self._tick_now)
            # Recorded here too, and from the same reading, because the
            # expiry below needs it and reaching into the router's private
            # `_selected_at` for it would be a second source of truth for one
            # number.
            self._pick_at = self._tick_now
        self.states.on_pick(target_id)
        self._send_pick(target_id)
        self._sync_to_state()
        # The focus rule is the router's (ui/slots.py) and this is what
        # applies it. It matters at tap time for exactly one case, and it is
        # the case that would otherwise do nothing at all: a pick for a
        # target ALREADY folding. The daemon queues nothing then, so a UI
        # that waited for a `job_start` would wait forever.
        self._sync_focus()

    def _send_pick(self, target_id):
        """Ask the daemon to fold `target_id` next. Returns whether it queued.

        Its own method so the three "nobody is going to answer" paths are one
        branch each rather than a `getattr` chain inside `_on_pick`, and so a
        test can drive every answer without a socket. The shape (and the
        reasoning) is `_ask_for_an_egg`'s.

        Nothing is sent to a daemon this build has refused to interpret. That
        is not merely tidy: talking to it anyway would be the booth promising
        "your tap folds this" to a daemon on the other end that has no idea
        how to honour it. `EventClient.send` refuses too, but the guard
        belongs here as well -- the UI should not be forming the request at
        all once it has declared it cannot speak to this daemon.
        """
        if self._connection_state == "incompatible":
            log.info("not asking a daemon this build cannot interpret to "
                     "fold %s", target_id)
            return False
        client = getattr(self, "_client", None)
        if client is None:
            return False
        try:
            return bool(client.send_pick(target_id))
        except Exception:
            # `send_pick` promises not to raise; this is the belt-and-braces
            # every GLib callback in this file wears, because an exception
            # here would freeze the gallery's tap handler for the life of the
            # process. The message may name a socket path and must never
            # reach a screen -- `log.exception` is the whole of its audience.
            log.exception("could not ask the daemon to fold %s", target_id)
            return False

    def _sync_quad_notice(self, now):
        """Say one thing across the quad about the visitor's pick, or stop
        saying it.

        Driven ENTIRELY by `SlotRouter.pick_status(now)` -- see ui/slots.py,
        which owns that rule and explains why it is decided there. A second
        opinion here about whether a pick has started is how the screen and
        the daemon end up disagreeing, and it is also how the booth ends up
        announcing NEXT UP over the protein it is already folding.

        `folding` and `None` both clear it: once the pick has become the
        thing on screen there is nothing left to announce, and a banner still
        naming it is the booth talking over itself.
        """
        if self.quad is None or self.router is None:
            return
        try:
            status = self.router.pick_status(now)
        except Exception:
            log.exception("pick status unavailable; clearing the notice")
            status = None
        if status == "queued":
            text = _PICK_QUEUED_NOTICE.format(
                name=self._pick_display_name(self.router.selected_target))
        elif status == "waiting":
            text = _PICK_WAITING_NOTICE.format(
                name=self._pick_display_name(self.router.selected_target))
        else:
            text = None
        try:
            self.quad.set_notice(text)
        except Exception:
            log.exception("quad notice update dropped")

    def _pick_display_name(self, target_id):
        """What to CALL the visitor's pick on screen.

        `_target_name` deliberately refuses to fall back to the raw id --
        ids are wire data and read like internals (`trpcage_no_msa`). This
        one does fall back, and the difference is deliberate: the visitor
        just tapped a card, so the booth naming something else, or nothing,
        would be worse than naming it plainly. Underscores and hyphens become
        spaces so the fallback reads as words rather than as a filename.

        A fallback only happens when the playlist has changed under a running
        booth; on the ordinary path the playlist's own display name wins.
        """
        name = self._target_name(target_id)
        if name:
            return name
        return str(target_id or "").replace("_", " ").replace("-", " ").strip()

    def _expire_stale_pick(self, now):
        """Let go of a pick the daemon never started.

        A visitor who picks and walks away must not pin the focus, or the
        notice, for the rest of the day.

        The number is the booth's own idle timeout and NOT a second one:
        `self.states.idle_timeout_s` is already "how long before the booth
        decides the visitor has gone", and a separate constant here would be
        a second thing to get wrong and to drift. It rides the existing 100ms
        state tick for the same reason -- no second timer.

        A pick that IS being folded is never expired here. It is released by
        ui/slots.py when the cell holding it stops (Task 12), which is the
        right moment: expiring it early would drop the focus off the hero
        mid-fold.
        """
        if self.router is None or self.router.selected_target is None:
            return
        try:
            if self.router.pick_status(now) == "folding":
                return
        except Exception:
            log.exception("pick status unavailable while expiring a pick")
            return
        if (now - self._pick_at) < self.states.idle_timeout_s:
            return
        log.info("visitor's pick %r expired after %.0fs with no fold",
                 self.router.selected_target, now - self._pick_at)
        self.router.release_target()

    # ── GLib sources ─────────────────────────────────────────────────────

    def _start_timers(self):
        """Register every repeating source the booth needs, in one place.

        One place on purpose: each of these is a thing that silently stops
        the booth if it is missing (no state tick means no showcase ever
        ends and the idle timeout never fires; no frame drain means no
        diffusion; no telemetry repaint means the silicon stops breathing),
        and a test can therefore check that all three exist by looking at
        one call.
        """
        GLib.timeout_add(_FRAME_DRAIN_MS, self._drain_frames)
        GLib.timeout_add(_STATE_TICK_MS, self._tick_state)
        GLib.timeout_add(_TELEMETRY_REPAINT_MS, self._tick_telemetry)

    def _tick_state(self):
        """Hand the state machine a clock reading, then reconcile the
        screen. The machine owns no timer of its own (ui/states.py), so
        this source is the only thing that can end a showcase dwell or fire
        the 45s idle timeout.

        Guarded, with the `return True` OUTSIDE the try, for the reason
        spelled out in `_drain_frames`: this is a REPEATING source, and an
        exception escaping it removes the source permanently -- a booth
        frozen mid-showcase with nothing on screen saying so.

        The work itself is `_tick_state_at`, which takes the clock reading
        rather than fetching one. That split is the test seam: driving four
        cells' dwells apart from each other needs the clock supplied, and a
        test that had to monkeypatch `self._clock` to do it would be testing
        the patch as much as the tick.
        """
        try:
            self._tick_state_at(self._clock())
        except Exception:
            log.exception("state tick failed")
        return True

    def _tick_state_at(self, now):
        """One state tick, with the clock supplied. See `_tick_state`.

        Unguarded on purpose -- its caller owns the guard, and a test that
        drives this directly wants a failure to be visible rather than
        logged.
        """
        # Stamped before anything reads it: `_on_pick` uses this to time the
        # visitor's pick on the same clock `pick_status` is asked with, and
        # `_sync_to_state` uses it to decide what the notice says.
        self._tick_now = now
        self.states.tick(now)
        # Before the router's own tick, so a pick that has just expired
        # cannot be reported as still queued for one more frame.
        self._expire_stale_pick(now)
        # Every cell's own dwell, and ONLY the cells that actually moved.
        # This is called at UI frame rate; re-captioning and re-draining all
        # four on every tick would repaint the whole quad a hundred times a
        # second (`SlotRouter.tick`).
        changed = self.router.tick(now)
        self._sync_to_state()
        if changed:
            # The dwell that just expired was suppressing that cell's frames.
            # Draining here is what lets the cell cut straight to live
            # diffusion rather than to a blank gap -- and it is a drain of
            # the whole buffer, which is exactly right: every OTHER cell's
            # frame is either drawable (and overdue) or suppressed again and
            # put straight back.
            self._drain_frames()
            for slot in changed:
                self._sync_cell_caption(slot)
            self._sync_focus()
            self._sync_viewer_hold()
        self._tick_overlays(now)
        # The pipeline panel's own staleness check, on the same tick.
        # It is reset by `job_start` and nothing else, so without this a
        # daemon that died mid-fold would leave e.g. "DIFFUSION 62%" on
        # screen for the rest of the day -- see ui/panels.py's
        # PIPELINE_STALE_AFTER_S. The panel owns the clock and the
        # threshold; this only gives it a chance to look.
        #
        # Its own guard, like `_sync_chipviz` and `_note_diagnostics`:
        # everything after this line in the tick (the diagnostics
        # repaint) must not be pre-empted by a panel misbehaving.
        if self.pipeline_panel is not None:
            try:
                self.pipeline_panel.tick()
            except Exception:
                log.exception("pipeline staleness check dropped")
        # The Tensix panel's own staleness check, on the same tick and for
        # the same reason: a daemon that dies mid-fold would otherwise leave
        # four core grids animating "denoising" for the rest of the day. See
        # ui/chipviz.py's STAGE_STALE_AFTER_S; the panel owns the clock and
        # the threshold, this only gives it a chance to look.
        self._tick_chipviz_staleness()
        # The diagnostics panel repaints from here rather than from
        # every appended line: a 30Hz frame stream would otherwise
        # re-label twenty rows thirty times a second to show a list a
        # human reads at reading speed. `refresh` is a no-op when the
        # log has not moved (revision check) and the panel is skipped
        # entirely while it is closed.
        if self.diagnostics_panel is not None and self.diagnostics_visible:
            try:
                self.diagnostics_panel.refresh(self.diagnostics)
            except Exception:
                log.exception("diagnostics repaint dropped")

    def _tick_telemetry(self):
        """Repaint the telemetry panel from the SAMPLER, never from the
        socket -- that independence is why ui/telemetry.py exists, and it
        is what leaves the silicon visibly breathing when the daemon dies.

        `latest()` is a tri-state (None = tt-smi gave no usable answer,
        [] = tt-smi answered and reported no devices, [readings] = normal)
        and all three are passed straight through: the panel renders them
        as three distinguishable things, and collapsing the first two here
        (`or []`) would turn "we cannot read the hardware" into "there is
        no hardware".

        Same repeating-source guard shape as `_tick_state`.
        """
        try:
            if self.telemetry_panel is not None:
                self.telemetry_panel.update(self.sampler.latest(),
                                            self.sampler.age_s())
        except Exception:
            log.exception("telemetry repaint failed")
        return True

    def _start_client(self):
        self._client = EventClient(
            self.socket_path, self._on_event,
            on_state_change=lambda s: GLib.idle_add(self._on_state, s),
        )
        self._client.start()

    def _on_event(self, event):
        kind = event["type"]
        if kind == "frame":
            self._frames.put(event)
        elif kind == "egg_frame":
            # Buffered, not idle_add'ed one per frame: a whole run is ~180
            # events delivered in about a second, and 180 idle callbacks
            # queued behind whatever else the main loop is doing would draw
            # the entire collapse in a fraction of a second. The egg's own
            # timer plays them at the cadence the animation was designed for.
            # This runs on the READER thread, so it touches nothing but the
            # deque and its lock.
            with self._egg_frames_lock:
                self._egg_frames.append(event)
        else:
            GLib.idle_add(self._handle_event, event)

    def _handle_event(self, event):
        # Runs via GLib.idle_add. Measured directly on this PyGObject/GLib
        # version: an uncaught exception here does NOT crash the process --
        # PyGObject's default exception hook logs a traceback and GLib
        # permanently removes the offending source instead of rescheduling
        # it. For a one-shot idle callback that's harmless (it was only
        # going to fire once anyway); but see _drain_frames below, where the
        # equivalent failure on a *repeating* source is a silent, permanent
        # freeze -- worse than a crash for an unattended booth demo, since
        # nothing signals that it happened. Guard this one too for the same
        # "never let wire-shaped data reach an unguarded field access"
        # reason: `event` always has a valid "type" (decode() guarantees
        # that much, nothing else), but e.g. a non-numeric "frac" still
        # raises -- now where the `stage` branch coerces it, on purpose,
        # rather than inside a log line's %.0f formatting as it did until
        # that line became one-per-transition.
        try:
            kind = event["type"]

            # The daemon's card list arrives before anything it can route:
            # `hello` is what decides how many cells this booth has, and a
            # `job_start` for chip 2 belongs to no cell until it does.
            if kind == "hello":
                self.attach_cards(event.get("cards") or [0])
            elif kind in ("job_start", "card_state"):
                # The two events that name a chip without being the greeting.
                # A booth that connected before the daemon's workers were
                # ready never got a `hello` at all, so these are how it
                # learns what hardware it is watching. See `_note_card`.
                self._note_card(event.get("card"))

            # WHICH CELL, decided once, by ui/slots.py -- and decided BEFORE
            # any rendering, because every branch below needs the answer and
            # a second opinion about it in this file is exactly what that
            # module exists to prevent. `None` means "no cell": a fold on a
            # chip this booth has no cell for, or an event for a job the
            # router has never seen. Drawing those into whichever cell
            # happens to be first is worse than dropping them.
            slot = self.router.on_event(event)

            # The machine first, and the screen reconciled second, BEFORE
            # any of the per-event rendering below: what the booth is doing
            # must not depend on a panel or a viewer call succeeding. (The
            # rendering work below is what can plausibly fail on
            # wire-shaped data; the machine only reads event["type"].)
            self.states.on_event(event)
            if kind == "not_ready":
                # Set before the sync below so the overlay it makes visible
                # already carries its copy. Neutral by construction: the
                # `missing` detail names real filesystem paths and must
                # never reach the screen, only the log.
                self.missing = event.get("missing", [])
                self.display_message = _PREPARING_MESSAGE
            self._sync_to_state()
            self._note_diagnostics(self.diagnostics.note_event, event)
            # Re-aim the Tensix animation. A `stage` event carries the one
            # thing that says what a chip's SILICON is doing; the three
            # lifecycle events say that a chip's fold has begun or ended, and
            # every other event only refreshes the booth state (which is what
            # turns the animation off while the daemon is `preparing`). See
            # ui/chipviz.py's `viz_mode` for why the stage, not the screen,
            # decides.
            #
            # PER CELL, and that is the load-bearing word. Four chips fold at
            # once, so one booth-wide stage would be the same claim the panel
            # was walked back for (Critical 3): whichever fold sent the last
            # `stage` event would be animated on all four canvases. Each
            # cell's own stage is stored on its own `_SlotView` and the panel
            # is handed the whole picture below.
            if kind in ("stage", "job_start", "job_done", "job_error"):
                view = self._slot_view(slot)
                if view is not None:
                    # `job_start` clears it as surely as `job_done` does: a
                    # fold that has begun but has not said what it is doing
                    # yet is not folding on silicon (it is in `msa`/`prep`,
                    # both host-side), and inheriting the PREVIOUS fold's
                    # stage would animate denoising for a fold that has not
                    # reached it.
                    view.stage = (event.get("stage") if kind == "stage"
                                  else None)
            elif kind == "not_ready":
                # The daemon has stopped folding entirely -- every cell, not
                # just one.
                for view in self._slots:
                    view.stage = None
            self._sync_chipviz()

            if kind == "job_start":
                log.info("folding %s (%s residues) on chip %s",
                         event.get("target_id"), event.get("n_residues"),
                         event.get("card"))
                # A new fold on this chip: its first stage is a transition
                # even if it repeats the last fold's final stage.
                self._last_logged_stage.pop(slot, None)
                view = self._slot_view(slot)
                if view is None:
                    # A fold on a chip this booth has no cell for. Logged
                    # above, and nothing else: it is a real fold on real
                    # silicon and the log should say so, but there is no
                    # honest place to draw it.
                    log.warning("no cell for chip %r; its fold will not be "
                                "shown", event.get("card"))
                    return False
                if slot == self.router.focus_slot:
                    # ONE panel, ONE bar. Reset it only for the fold the
                    # booth is actually following -- four folds feeding one
                    # progress bar makes it run backwards.
                    self._reset_pipeline_panel()
                # NOT a clear. This branch used to blank the viewer here
                # (or, during a showcase, arrange for it to be blanked two
                # seconds later), and on a long target that is fifteen
                # seconds of black screen: `msa`, `prep` and `trunk` emit
                # progress and no coordinates, so there is nothing to draw
                # until diffusion starts. The clear now belongs to the first
                # frame that can replace what is on screen -- see
                # `_drain_frames` and the module docstring's second section.
                #
                # Recorded, not applied, and recorded ON THIS CELL: which
                # fold this is, so a straggling frame from the previous one
                # cannot pose as this fold's first, and what it is folding,
                # for the caption.
                view.awaiting_first_frame = True
                view.current_job_id = event.get("job_id")
                view.current_target_id = event.get("target_id")
                # The state machine has already been driven and the screen
                # already reconciled (both above, before this branch), so
                # these are what put the captions up for the flags just set.
                self._sync_cell_caption(slot)
                self._sync_focus()
                self._sync_viewer_hold()
            elif kind == "not_ready":
                # The daemon's preflight or model load hasn't finished.
                # `missing` names exactly what's wrong (e.g. real filesystem
                # paths) -- that detail is exactly what an operator needs
                # and exactly what must never reach the screen (constraint:
                # no raw error text on display), so it goes to the log at a
                # level an operator watching the booth will actually see.
                if self.missing:
                    log.warning("booth not ready: %s", _format_missing(self.missing))
                else:
                    log.warning("booth not ready (no detail given)")
                # Nothing is folding any more, so nothing is coming to
                # supersede what is on screen. Whatever the viewer holds
                # stays (blanking it would only add an empty screen behind
                # the preparing overlay), but the caption's "now folding X"
                # is now false and comes down with the fold that made it
                # true. See `viewer_hold_caption`.
                for index in range(len(self._slots)):
                    self._end_fold_in_flight(index)
            elif kind == "stage":
                # ONE LINE PER STAGE TRANSITION, NOT PER STAGE EVENT. The
                # daemon emits progress continuously, so logging every event
                # measured 17.0 MB / 488,048 lines in 2h07m of four-chip
                # folding -- 8.0 MB/h, ~64 MB across a conference day, and
                # growing with the number of chips. `run-demo.sh` leaves the
                # UI's stdout on a terminal so an operator never sees it, but
                # under systemd or `nohup` it goes to the journal or a file
                # and NO BUDGET IN THIS CODEBASE COVERS IT (docs/followups.md;
                # the same gap as daemon.log's, on the health path rather than
                # the failure path).
                #
                # The transition is the part worth keeping: "this fold
                # reached diffusion" is the line anyone reads a log for, and
                # it is bounded by folds x stages instead of by wall time.
                # Per SLOT, because four chips interleave and a booth-wide
                # "last stage" would log every time the chips disagreed --
                # which is always.
                stage = event.get("stage")
                # `frac` IS WIRE DATA AND IS COERCED HERE, deliberately
                # before the slot check below, so a malformed one is caught
                # for every event and not only for the focused cell's.
                #
                # This used to happen by accident: the log line above was
                # `"stage %s %.0f%%" % (stage, 100.0 * frac)`, and a
                # non-numeric `frac` raised inside that formatting -- which
                # is what `_handle_event`'s own guard cites as the reason it
                # is broad. Logging one line per stage TRANSITION rather than
                # per event removed the arithmetic, and with it a type check
                # nobody had written down (caught by
                # `test_malformed_event_is_still_dropped_not_raised`). A
                # value the booth is about to draw a progress bar from should
                # be checked on purpose, not as a side effect of formatting.
                frac = float(event.get("frac", 0.0))
                if self._last_logged_stage.get(slot) != stage:
                    self._last_logged_stage[slot] = stage
                    log.info("chip %s: stage %s", event.get("card"), stage)
                # Every cell says its own stage, under its own chip label.
                self._sync_cell_caption(slot, stage=event.get("stage"))
                if slot is not None and slot == self.router.focus_slot:
                    # ONE panel, ONE bar, fed by ONE fold: the cell the booth
                    # is following. Two jobs feeding it makes it run
                    # backwards -- 62%, then 8%, then 71% -- which reads as a
                    # broken booth rather than as two folds.
                    #
                    # The wire carries a WHOLE-FOLD fraction; the panel
                    # wants a within-stage one. set_stage_from_wire is the
                    # one place that conversion lives, which is precisely
                    # why this call site uses it and never set_stage.
                    self._call_pipeline_panel(
                        "set_stage_from_wire", stage, frac)
            elif kind == "job_done":
                log.info("done in %.2fs", event.get("wall_s", 0.0))
                self._sync_cell_caption(slot)
                self._sync_focus()
                self._sync_viewer_hold()
                cif_path = event.get("cif_path")
                if cif_path and slot is not None:
                    # ribbon_from_cif's cost moved off this thread -- see
                    # _spawn_ribbon_worker. The result comes back later, via
                    # _drain_pending_ribbon on the main loop, not here.
                    self._spawn_ribbon_worker(slot, cif_path)
                elif cif_path:
                    log.warning("job_done for %s belongs to no cell; nothing "
                                "to render", event.get("job_id"))
                else:
                    # A misconfigured runner sending job_done with nothing
                    # to render used to no-op here silently; log it so it's
                    # diagnosable instead of just "the ribbon never showed up".
                    log.warning("job_done for %s has no cif_path; nothing "
                                "to render", event.get("job_id"))
            elif kind == "job_error":
                # Per the protocol spec, `message` may hold arbitrary detail
                # from the runner and must never reach the display -- log it
                # in full so a failed fold is still diagnosable from the
                # logs, which is all this branch does.
                log.error("job %s failed: %s", event.get("job_id"),
                          event.get("message"))
                # This fold will never produce a frame, so it can never
                # supersede what is on screen. The held structure stays --
                # it is real, it was really computed, and blanking it would
                # trade an honest old protein for the empty viewer this
                # whole change exists to remove -- but the caption stops
                # claiming a fold is running, because one is not. A daemon
                # that dies right here therefore leaves a structure with no
                # in-flight claim over it, not a permanent lie; and the
                # pipeline panel's own staleness check (ui/panels.py) takes
                # the stage readout down beside it.
                #
                # ONE cell, and that is the load-bearing word: a fold failing
                # on chip 1 says nothing about the three still running.
                # `message` is not passed to anything that draws -- the
                # caption below is rebuilt from this booth's own playlist and
                # `STAGE_ORDER`, never from the wire.
                self._end_fold_in_flight(slot)
                self._sync_cell_caption(slot)
                self._sync_focus()
            elif kind == "egg_refused":
                # The daemon could not give the egg a chip. `message`, if it
                # is there at all, is runner-side detail and must never reach
                # a screen (the same rule `job_error` follows) -- only the
                # short `reason` code is used, and only to choose between two
                # sentences this file owns.
                log.info("easter egg refused (%s): %s", event.get("reason"),
                         event.get("message"))
                if event.get("egg_id") == self._egg_id:
                    # Recorded, not acted on: the fallback is started by
                    # `_advance_egg`, on the egg's own timer, so that every
                    # transition this feature makes happens in one method.
                    self._egg_refusal = event.get("reason")
            elif kind == "card_state":
                # Acted on at the top of this method, and ONLY for the chip
                # number it names: that is what lets a booth which never got
                # a `hello` still learn what hardware it is watching.
                #
                # Nothing else in it reaches the screen, and that must stay
                # true. `card_state` carries per-chip status, and this file
                # rendering it would be exactly the coupling ui/telemetry.py
                # exists to prevent -- the chip panel is fed from an
                # independent tt-smi sampler so that a dead daemon still
                # leaves the silicon visibly breathing. Nothing on the wire
                # feeds that panel.
                log.debug("card %s is %s", event.get("card"),
                          event.get("state"))
            elif kind == "hello":
                # Already acted on, at the top of this method -- it is the
                # one event that changes how many cells this booth has, so it
                # has to be applied before anything tries to route by one.
                # Named here so it does not land in the "unhandled event
                # type" branch below, which would be a warning per reconnect.
                log.info("daemon says hello: chips %s, models %s",
                         event.get("cards"), event.get("models"))
            else:
                # A future protocol addition should be visible in the logs,
                # not silently dropped the way job_error was before this fix.
                # (`card_state` used to land here; it has its own branch
                # above now, because the booth reads the chip number off it
                # to size the quad -- and only the chip number.)
                log.warning("unhandled event type %r", kind)
        except Exception:
            # Rate-limited: a protocol change this build cannot parse would
            # otherwise write one traceback per event for the rest of the
            # day. See _note_dropped.
            kind = event.get("type")
            self._note_dropped(f"event:{kind!r}", f"dropping malformed {kind!r} event")
        return False

    # ── the one pipeline bar ─────────────────────────────────────────────
    #
    # Guarded the same way `_sync_chipviz` and `_note_diagnostics` are, and
    # for the same reason: a panel is the least important thing happening in
    # `_handle_event`, and a failure inside one must not pre-empt the
    # rendering below it or turn a perfectly good event into a "dropping
    # malformed ..." log line. Before this the panel calls sat bare inside
    # the branch, so an exploding panel cost the whole event.

    def _call_pipeline_panel(self, method, *args):
        if self.pipeline_panel is None:
            return
        try:
            getattr(self.pipeline_panel, method)(*args)
        except Exception:
            log.exception("pipeline panel %s dropped", method)

    def _reset_pipeline_panel(self):
        self._call_pipeline_panel("reset")

    def _sync_focus(self):
        """Mark the cell the booth is following, on screen.

        The rule is `SlotRouter.focus_slot`'s and is not restated here --
        a second copy of it is how the daemon and the screen end up
        disagreeing about which fold the booth is about. In solo mode this
        also decides which single cell IS the screen (`QuadView.set_focus`).
        """
        if self.quad is None or self.router is None:
            return
        try:
            self.quad.set_focus(self.router.focus_slot)
        except Exception:
            log.exception("quad focus update dropped")

    def _sync_cell_caption(self, slot, stage=None):
        """Say what cell `slot` is folding, and what it is doing to it.

        Only the cell the event belonged to: three cells re-captioned because
        the fourth got a `stage` event is the single-viewer defect in its
        smallest form. Everything that reaches the screen from here goes
        through `cell_caption`, which takes a playlist name and a stage from
        `STAGE_ORDER` and nothing else -- see the comment above it.
        """
        if self.quad is None:
            return
        view = self._slot_view(slot)
        if view is None:
            return
        shown = self._target_name(view.shown_target_id)
        name = self._target_name(view.current_target_id) or shown
        # `has_structure` and not `shown_target_id` alone: the id survives the
        # cell being cleared, and a caption claiming to show trypsin over an
        # empty cell would be a worse lie than the one this fixes.
        showing = shown if view.has_structure else None
        try:
            self.quad.set_caption(
                slot, cell_caption(name=name, stage=stage, showing=showing,
                                   empty=not view.has_structure))
        except Exception:
            log.exception("quad caption for slot %r dropped", slot)

    def _chip_stages(self):
        """`{chip index: that chip's own current stage}` for every cell.

        The mapping the Tensix panel wants (ui/chipviz.py's
        `set_chip_stages`), built from the only place that knows it: each
        cell's own `_SlotView.stage`, set by that cell's `stage` events and
        cleared when its fold starts, ends or fails. A cell between folds
        contributes `None`, which the panel draws as a resting chip.

        Keyed by CHIP, not by slot. `self.cards` is the daemon's own card
        list in the order the cells were built, and the panel's canvases are
        in `chip_dirs()` order -- the same left-to-right order the telemetry
        readouts above it use. Handing it slot indices instead would put chip
        2's fold under chip 0's thermometer on any booth whose card list does
        not happen to start at zero.
        """
        stages = {}
        for slot, view in enumerate(self._slots):
            if slot < len(self.cards):
                stages[self.cards[slot]] = view.stage
        return stages

    def _sync_chipviz(self):
        """Tell the Tensix activity panel what the booth is doing and which
        chips are doing what.

        Its own guard, for the same reason `_note_diagnostics` has one: an
        animation is the least important thing happening in `_handle_event`,
        and a failure inside a WebView must never pre-empt the rendering
        below it or turn a perfectly good event into a "dropping malformed
        ..." log line. Tolerates the panel being absent (headless tests) and
        being present-but-unavailable (no WebKit, no chips), because both are
        ordinary.
        """
        if self.chipviz_panel is None:
            return
        try:
            self.chipviz_panel.set_state(self.states.state)
            self.chipviz_panel.set_chip_stages(self._chip_stages())
        except Exception:
            log.exception("Tensix activity panel update dropped")

    def _tick_chipviz_staleness(self):
        """Give the Tensix panel a chance to stand down a chip nothing has
        said anything about for a while (ui/chipviz.py's
        `STAGE_STALE_AFTER_S`).

        On the state tick rather than on events, because the case it exists
        for is precisely that no more events are coming. Guarded like every
        other panel call for the reason `_sync_chipviz` spells out.
        """
        if self.chipviz_panel is None:
            return
        try:
            self.chipviz_panel.tick_staleness()
        except Exception:
            log.exception("Tensix activity staleness check dropped")

    def _note_dropped(self, key, message):
        """Log a failure that can repeat every frame, without flooding.

        See `_DROP_LOG_EVERY`. `key` groups failures that are the same
        problem (the frame stream, one event type); `message` is already
        formatted, because this is only ever called from an `except` block
        where the cost of one f-string is irrelevant.

        MUST be called from inside an `except` block -- the first-occurrence
        branch uses `log.exception`, whose whole value here is the traceback
        of the failure that started it.
        """
        count = self._drop_counts.get(key, 0) + 1
        self._drop_counts[key] = count
        if count == 1:
            log.exception("%s (first occurrence; repeats are logged at debug, "
                          "with a count every %d)", message, _DROP_LOG_EVERY)
        elif count % _DROP_LOG_EVERY == 0:
            log.warning("%s (%d times now)", message, count)
        else:
            log.debug("%s (repeat %d)", message, count, exc_info=True)

    def _note_diagnostics(self, method, *args):
        """Feed the diagnostics log without ever letting it cost the booth
        anything.

        Its own guard, deliberately, even though every call site is already
        inside a broad `except`: a diagnostics line is the least important
        thing happening in any of those methods, and it must not be able to
        pre-empt the rendering below it or turn a perfectly good event into
        a "dropping malformed ..." log entry. `ui.diagnostics` is written not
        to raise on wire-shaped data (`_safe`/`_num`); this is the belt to
        that module's braces.
        """
        try:
            method(*args)
        except Exception:
            log.exception("diagnostics line dropped")

    # ── ribbon construction off the main loop ───────────────────────────
    #
    # ribbon_from_cif is pure numpy/gemmi (ui/geometry.py's own docstring:
    # "no GL, no GTK"), so it is safe to run on a plain background thread
    # with no GTK/GL calls anywhere in it. Only the hand-back to the viewer
    # touches GTK, and that always happens on the main loop, via
    # _drain_pending_ribbon.
    #
    # Ordering: the daemon folds continuously (the attract loop), so a slow
    # structure's worker can still be running when the next job_done
    # arrives. Rather than trying to cancel an in-flight worker -- there is
    # no way to interrupt a thread already inside gemmi/numpy C calls, and
    # daemon threads make that unnecessary anyway (see _spawn_ribbon_worker)
    # -- every worker is stamped with a monotonic generation number at
    # spawn time, and both the worker (when it finishes) and the drain
    # step (when the main loop actually applies a result) check that
    # generation against "what is the newest fold we know about right now".
    # A stale result is simply dropped, silently, at whichever of those two
    # points notices first. This makes "only the newest lands" a single
    # integer comparison, correct no matter which worker happens to finish
    # first -- no thread cancellation, no comparing job_id strings (which
    # would need the daemon to guarantee an ordering the protocol doesn't
    # actually promise), and no queue that could let a superseded ribbon
    # sit and apply later.

    def _spawn_ribbon_worker(self, slot, cif_path):
        """Move ribbon_from_cif's cost onto a background thread, for one cell.

        Threads, not e.g. a process pool: the payload (a cif_path string
        in, four numpy arrays out) is small, gemmi/numpy release the GIL
        for the bulk of their work same as any C-backed numeric library,
        and a thread pool would add a shutdown-coordination problem this
        task doesn't need -- daemon=True below already makes a worker in
        flight at app-exit harmless (see the class docstring above and the
        module-level shutdown note in the task brief): a daemon thread is
        killed outright when the process exits, never blocking it, and it
        never touches a GTK widget itself (only _drain_pending_ribbon does,
        and only from the main loop).

        The generation is bumped on THIS CELL's counter and no other. With
        one counter for the booth, a `job_done` on chip 3 bumps the
        generation and chip 0's in-flight ribbon build is dropped as stale --
        silently, every cycle, forever, and with four chips folding that is
        most ribbons the booth ever builds.
        """
        view = self._slot_view(slot)
        if view is None:
            log.warning("no cell for a finished fold; %s not rendered", cif_path)
            return
        with self._ribbon_lock:
            view.ribbon_generation += 1
            generation = view.ribbon_generation

        # Bookkeeping only (test joins + a bound on how many dead Thread
        # objects accumulate across a long attract-loop session) -- prune
        # finished workers before adding the new one rather than letting
        # this list grow for the life of the process.
        self._ribbon_threads = [t for t in self._ribbon_threads if t.is_alive()]

        worker = threading.Thread(
            target=self._ribbon_worker_main,
            args=(slot, generation, cif_path),
            name=f"ribbon-worker-{slot}-{generation}",
            daemon=True,
        )
        self._ribbon_threads.append(worker)
        worker.start()

    def _join_ribbon_workers(self, timeout=5.0):
        """Block until every ribbon worker spawned so far has finished.

        A test seam, and used by `do_shutdown` for tidiness. Returns whether
        they all finished: a caller that cares (a test) can assert on it, and
        the booth itself only logs -- a worker still inside gemmi at shutdown
        is a daemon thread and the process is going away regardless.

        Joins EVERY thread this app has recorded, not just the newest: four
        chips finishing within a few milliseconds of each other put four
        workers in flight at once, and a test that inspected the outcome
        after joining only the last one would be racing three others.
        """
        alive = []
        for worker in list(self._ribbon_threads):
            worker.join(timeout=timeout)
            if worker.is_alive():
                alive.append(worker.name)
        if alive:
            log.warning("ribbon worker(s) %s still running after %.1fs",
                        alive, timeout)
        return not alive

    def _ribbon_worker_main(self, slot, generation, cif_path):
        """Runs entirely off the main loop -- must never raise out of this
        method. A plain threading.Thread whose target raises doesn't freeze
        anything the way an uncaught exception in a GLib source would (see
        _handle_event's docstring), but it would still just vanish into
        Python's default thread excepthook with nothing for the app to act
        on and nothing applied or logged through this app's own channel.
        Catch broadly -- deliberately not just GeometryError, since
        anything ribbon_from_cif raises must produce the same "log it,
        leave the screen alone" outcome, not a silent thread death.
        """
        try:
            # The cartoon, with any bound ligand drawn beside it. Falls
            # back to the plain backbone tube if either raises -- see
            # ui/structure_view.py. Same four arrays either way.
            result = structure_mesh(cif_path)
        except Exception as exc:
            outcome = ("error", exc)
        else:
            outcome = ("ok", result)

        with self._ribbon_lock:
            view = self._slot_view(slot)
            if view is None:
                # The booth was re-sized (a `hello` naming different chips)
                # while this worker ran. Its cell is gone; so is the fold it
                # belonged to, as far as the screen is concerned.
                return
            # Stale: a newer fold has started ON THIS CELL since this worker
            # was spawned -- drop this result outright, it must never reach
            # the screen even if nothing else is waiting to replace it. On
            # this cell, and not on any other: another chip finishing a fold
            # in the meantime is not a reason to throw this one away.
            stale = generation != view.ribbon_generation
            # Superseded-in-slot: a *result* from a newer generation is
            # already waiting to be drained. Guards the opposite race from
            # `stale` above -- a straggler finishing after a faster, newer
            # worker must not clobber the newer result that's already
            # sitting in the slot before the main loop got to it.
            superseded_in_slot = (
                view.pending_ribbon is not None
                and generation < view.pending_ribbon[0]
            )
            if not stale and not superseded_in_slot:
                view.pending_ribbon = (generation, cif_path, outcome)

        # Wake the main loop regardless of whether this worker's result was
        # the one actually stored -- GLib.idle_add is safe to call from any
        # thread (the same pattern EventClient's on_state_change already
        # uses, from its own background thread, into this same app; see
        # ui/client.py), and _drain_pending_ribbon is a correct, cheap
        # no-op when there is nothing left to apply.
        GLib.idle_add(self._drain_pending_ribbon)

    def _drain_pending_ribbon(self):
        """Runs on the main loop (scheduled via GLib.idle_add, from a
        ribbon worker thread). Applies the newest still-relevant
        ribbon-construction result to the viewer, or silently discards it
        if a newer fold has started since -- see the class-level comment
        above this section for why a generation check, not thread
        cancellation, is what makes "only the newest lands" correct here.

        Guarded the same broad way as every other GLib-invoked callback in
        this file (_handle_event, _on_state, _drain_frames): this is a
        one-shot idle source, so an uncaught exception here wouldn't cause
        the *repeating*-source freeze those methods' comments warn about,
        but the app must still never show a stack trace or die on e.g. a
        viewer that was torn down mid-drain (app shutting down with a
        worker's result still in flight) -- so treat that case the same as
        any other malformed-input failure: log it, don't crash, leave
        whatever is on screen exactly as it is.
        """
        with self._ribbon_lock:
            # Every cell's, in one pass: four workers can finish inside one
            # idle callback, and asking about only the newest would leave the
            # other three sitting in their cells until something else woke
            # the main loop.
            pending = []
            for slot, view in enumerate(self._slots):
                if view.pending_ribbon is not None:
                    pending.append((slot, view.pending_ribbon,
                                    view.ribbon_generation))
                    view.pending_ribbon = None

        for slot, (generation, cif_path, outcome), current_generation in pending:
            self._apply_ribbon(slot, generation, current_generation,
                               cif_path, outcome)
        return False

    def _apply_ribbon(self, slot, generation, current_generation, cif_path,
                      outcome):
        """Put one cell's finished structure on screen, or decide not to."""
        if generation != current_generation:
            # A newer fold started ON THIS CELL after this result was
            # produced (it was stored back when it was still current) --
            # it's stale now.
            return

        if not self.router.slots[slot].ribbon_may_be_revealed:
            # The dwell this structure was built for has already expired
            # (a slow build can outlast it), so this cell has moved on to
            # the next fold's live diffusion. Cross-fading this ribbon in
            # now would hide that diffusion behind a structure from a fold
            # that is over -- the headline defect, arriving late instead of
            # early. THIS cell's dwell, not the booth's: another chip
            # showcasing is no licence to draw over this one.
            log.info("ribbon for %s finished after its showcase ended; "
                     "not revealing it over live diffusion", cif_path)
            return

        kind, payload = outcome
        viewer = self._viewer_for(slot)
        try:
            if kind == "error":
                # Leave whatever's on screen (the last diffusion frame, or
                # a previous ribbon) exactly as it is and just log -- never
                # a stack trace on screen, never a crash. set_ribbon and
                # begin_crossfade below are only reached on success, so a
                # bad CIF simply forfeits the ribbon reveal for this job
                # instead of corrupting the current view. And it forfeits it
                # for ONE cell: three good folds beside it are untouched.
                log.error("could not build ribbon for %s", cif_path,
                          exc_info=payload)
                return
            verts, norms, colors, idx = payload
            if viewer is not None:
                viewer.set_ribbon(verts, norms, colors, idx)
                viewer.begin_crossfade()
            view = self._slot_view(slot)
            if view is not None:
                # This cell is now showing a real structure -- which is what
                # lets the NEXT fold on this chip be held against it rather
                # than against nothing.
                #
                # `shown_target_id` is deliberately NOT touched here, and the
                # reason is a defect this line caused and looking at the real
                # booth found. By the time a ribbon lands, the daemon has
                # usually already started the next fold on the same chip, so
                # `current_target_id` names the fold that is STARTING, not
                # the one this ribbon belongs to. Copying it here labelled a
                # held structure with the wrong protein's name, under the
                # picture, in front of a visitor. The moment the picture
                # really changes is the moment a frame is drawn, and that is
                # where `shown_target_id` is set -- see `_draw_frame`.
                view.has_structure = True
            # The structure is only NOW something a visitor can see, so
            # this is when its dwell starts -- not back at job_done,
            # which is separated from this instant by the build above
            # and the 0.8s cross-fade below it.
            self.router.slots[slot].on_structure_revealed()
            if slot == self.router.focus_slot:
                # The booth-wide machine follows the cell the booth is
                # following, so the attract/idle timing a visitor experiences
                # is measured against the fold they are actually looking at.
                self.states.on_structure_revealed()
            self._sync_cell_caption(slot)
            self._sync_viewer_hold()
        except Exception:
            log.exception("dropping ribbon result for %s", cif_path)

    def _on_state(self, state):
        # The display must survive the runner dying: log the transition and
        # keep rendering whatever is already on screen. This runs via
        # GLib.idle_add (on_state_change is invoked from EventClient's
        # background thread; see ui/client.py's own docstring on why GTK/GL
        # calls must never happen there directly), so it's a one-shot idle
        # source like _handle_event -- no repeating-source freeze risk to a
        # *timer*.
        #
        # It still needs its own guard, though: connection_state's setter
        # (ui/viewer.py) deliberately raises ValueError on anything outside
        # {"connected", "disconnected", "incompatible"}, so that a *future*
        # mismatch -- someone adding a state to EventClient without teaching
        # this setter about it -- fails loudly wherever it's set, instead of
        # being stored silently and discovered much later. But
        # EventClient._set_state only invokes on_state_change when the state
        # actually *changes* (see ui/client.py), so if that new, unrecognized
        # state ever becomes the client's steady state, an unguarded raise
        # here would fire once and then never be retried: there'd be no
        # further transition to trigger it again, and connection_state would
        # stay stuck reporting whatever the last valid transition left it at
        # -- silently, on exactly the runner-disconnect path this task
        # exists to harden. Guard it like every other GLib-invoked callback
        # in this file, so the validator can do its job (a bad value still
        # gets logged, loudly, right here) without bricking this channel.
        #
        # Note what this does NOT do: nothing here touches the telemetry
        # panel. A dead daemon must leave the silicon visibly breathing, and
        # it does, because the panel is fed by ui/telemetry.py's own tt-smi
        # sampler on its own thread, with no dependency on this connection
        # at all.
        try:
            log.info("runner connection: %s", state)
            self._note_diagnostics(self.diagnostics.note_connection, state)
            # Remembered as well as applied, so a quad rebuilt on the `hello`
            # that follows a reconnect does not come up claiming the socket
            # is still down.
            self._connection_state = state
            if state == "incompatible":
                # This build has refused to interpret this daemon's protocol
                # (ui/client.py sets it and never retries), so nothing is
                # ever going to fold. To a visitor that is the same thing as
                # `not_ready`, and it gets the same neutral words: the
                # version numbers are in the log line ui/client.py already
                # wrote and in the diagnostics rail, and they never reach the
                # glass. `_sync_to_state` below is what puts the overlay up.
                self.display_message = _PREPARING_MESSAGE
            if self.quad is not None:
                # EVERY cell (`QuadView.set_connection_state`): a quad where
                # one cell knows the daemon is gone and three do not is worse
                # than one where none of them do.
                self.quad.set_connection_state(state)
            # The overlay is decided in one place, and a connection state is
            # not a booth state -- so this has to ask for the reconcile
            # explicitly, with `force`, since `self.states.state` has not
            # moved.
            self._sync_to_state(force=True)
        except Exception:
            log.exception("dropping unrecognized connection state %r", state)
        return False

    def _drain_frames(self):
        """Put each fold's newest diffusion frame into its own cell -- unless
        that cell is holding a finished structure.

        Suppressed rather than discarded (see the module docstring, step 2):
        a suppressed frame goes straight back into its job's latest-wins
        slot, so the moment that cell's dwell expires `_tick_state_at` drains
        it and the cell cuts straight to live diffusion. Taking it here and
        throwing it away would leave a blank cell for one frame interval at
        exactly the transition a visitor is watching.

        PER CELL, both halves of that. Four folds stream at once, so a single
        buffer would show whichever one was fastest in all four cells, and a
        single "is a showcase running" question would suppress all four
        because one of them finished.

        Guarded, with the `return True` OUTSIDE the try: this is a REPEATING
        GLib source and an exception escaping it removes the source
        permanently -- a silent freeze, worse than a crash for an unattended
        booth since nothing signals it happened.
        """
        try:
            for job_id, frame in self._frames.take_all().items():
                slot = self.router.slot_for_job(job_id)
                if slot is None:
                    # An event for a job the router has never seen (a frame
                    # that beat its own `job_start` through the idle queue, a
                    # fold on a chip with no cell, or a frame carrying no
                    # `job_id` at all). It belongs to NO cell; drawing it
                    # into whichever cell happens to be first would put one
                    # fold's coordinates under another fold's caption.
                    continue
                if not self.router.slots[slot].points_are_visible:
                    self._frames.put(frame)
                    continue
                self._draw_frame(slot, frame)
        except Exception:
            # Rate-limited: see _note_dropped. Only reachable if the router
            # or the buffer itself misbehaves -- a single frame's own failure
            # is caught inside `_draw_frame`, so one bad payload cannot cost
            # the other three cells their frames.
            self._note_dropped("frames", "dropping a frame drain")
        return True

    def _draw_frame(self, slot, frame):
        """Draw one decoded frame into one cell.

        This is also where that cell's PREVIOUS structure is finally cleared
        -- see the module docstring's second section. The clear used to live
        in `job_start`, which on a long target meant blanking the viewer
        fifteen seconds before there was anything to put in its place. Here
        it is a single instant: clear, then draw, with a real frame already
        decoded and in hand. One cell's clear, at that: chip 3 starting a
        fold must not blank the finished structure a visitor is looking at on
        chip 0.
        """
        view = self._slot_view(slot)
        viewer = self._viewer_for(slot)
        if view is None or viewer is None:
            return

        if view.awaiting_first_frame:
            # Is this frame actually the new fold's, or a straggler from the
            # one before it? The daemon does not stop the world between
            # folds, so a frame emitted by fold N can arrive after fold
            # N+1's `job_start`; treating it as N+1's first would replace a
            # finished structure with the OLDER fold's noise cloud -- an
            # honest picture of the wrong thing, and worse than what it
            # replaced. `job_id` is on every frame (runner/shaping.py's
            # `frame_event`), so this is a comparison, not a guess.
            #
            # A frame that got this far has already been routed to this cell
            # BY its job id, so "which fold is this" is settled; what is left
            # is whether it is the fold this cell is currently waiting on, or
            # one of that cell's own earlier folds still draining.
            frame_job = frame.get("job_id")
            if frame_job is not None and frame_job != view.current_job_id:
                log.debug("frame from job %r arrived while waiting for %r's "
                          "first frame; holding the previous structure",
                          frame_job, view.current_job_id)
                return

        # unpack_coords raises protocol.events.ProtocolError on truncated
        # base64 or a byte count that isn't a whole number of 3-vectors, and
        # decode() only validates that "type" is present and known -- so a
        # malformed coords_b64 payload reaches here unguarded. This source is
        # a REPEATING GLib.timeout_add, unlike _handle_event's one-shot
        # idle_add: confirmed by direct reproduction, an uncaught exception
        # here doesn't crash the process, it permanently removes this 33ms
        # timeout source -- a silent freeze, worse than a crash for an
        # unattended booth since nothing signals it happened. Drop the bad
        # frame and keep going; the next one arrives in <=33ms regardless.
        # Catch Exception broadly (not just ProtocolError) since
        # set_points() itself reshapes wire-shaped data too.
        try:
            coords = unpack_coords(frame["coords_b64"])
            if view.awaiting_first_frame:
                # Decoded FIRST, cleared second. A frame that fails to
                # unpack must not be able to blank the booth: it raises
                # above this line, the structure on screen survives, and the
                # flag stays set so the next good frame does the handover
                # instead.
                #
                # `clear_structure` is what hands the camera back from the
                # outgoing ribbon to the point cloud and resets
                # `_camera_framed` (ui/viewer.py), so the `set_points` just
                # below snaps to this frame's own spread. That pairing is
                # unchanged from when the clear lived in `job_start` -- it
                # is only much closer together now, with no window at all in
                # which the camera belongs to a structure that is gone. And
                # it is THIS cell's camera: each cell owns its own, which is
                # what stops one fold's noise rescaling another's ribbon.
                viewer.clear_structure()
                view.has_structure = False
            viewer.set_points(coords)
            view.has_structure = True
            if view.awaiting_first_frame:
                # The handover is complete: this fold now owns this cell.
                view.awaiting_first_frame = False
                view.shown_target_id = view.current_target_id
                self._sync_cell_caption(slot)
                self._sync_viewer_hold()
            # Logged where it is DRAWN, not where it arrives: the buffer
            # between socket and screen is latest-wins, so this is the only
            # place that can honestly say "a visitor saw this frame".
            self._note_diagnostics(self.diagnostics.note_frame, frame, coords)
        except Exception:
            # Rate-limited, and this is the call site that most needs it:
            # this source runs every 33ms, so a systematically malformed
            # frame stream produced ~30 tracebacks a second. See
            # _note_dropped. Per FRAME, so one cell's bad payload costs the
            # other three nothing.
            self._note_dropped("frame", "dropping malformed frame")


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="tt-bio demo UI")
    parser.add_argument("--socket", default=None,
                        help="runner socket path; omit to show an empty viewer")
    parser.add_argument("--playlist", default=None,
                        help=f"playlist manifest (default: {_DEFAULT_PLAYLIST})")
    parser.add_argument("--targets", default=None,
                        help="comma-separated manifest ids to offer; omit for "
                             "every target in the manifest. MUST match what "
                             "the daemon was given (scripts/run-demo.sh does "
                             "this for you) -- a gallery card whose target the "
                             "daemon has no input file for can never be folded")
    parser.add_argument("--windowed", action="store_true",
                        help="start in a normal window instead of fullscreen "
                             "(Ctrl+F toggles either way at runtime)")
    parser.add_argument("--quad", action="store_true",
                        help="start showing all the booth's chips at once "
                             "instead of one large protein (Q toggles either "
                             "way at runtime). Only worth it with more than "
                             "one chip")
    args = parser.parse_args(argv)
    target_ids = [part.strip() for part in (args.targets or "").split(",")
                  if part.strip()]
    return DemoApp(socket_path=args.socket,
                   playlist_path=args.playlist,
                   target_ids=target_ids,
                   windowed=args.windowed,
                   quad=args.quad).run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
