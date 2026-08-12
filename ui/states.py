"""The booth's state machine.

Pure by construction: it takes protocol events, touches/picks, and a clock
reading, and returns state. No GTK, no timers of its own, no imports beyond
the standard library -- that is what makes the booth's actual behavior
testable at all (see docs/superpowers/plans/2026-08-12-ui-panels.md, Task 7).
This module must never import torch, tt-bio, or GTK.

The five states
----------------
`attract`, `gallery`, `folding` and `showcase` are the main visitor loop:

    attract --touch--> gallery --pick--> folding --job_done--> showcase
       ^                                                           |
       +---------------------- dwell elapses ---------------------+

`preparing` sits outside that loop: the daemon's `not_ready` event pushes
the booth there unconditionally, regardless of what a visitor was doing,
and only a fresh `job_start` (the daemon actually recovering) releases it
-- back to `attract`, never to whatever screen was up before, because that
screen was built on pre-degrade information (e.g. a gallery the visitor
opened before the daemon went missing weights).

Why showcase needs a minimum dwell
-----------------------------------
Measured on real hardware (see progress.md, "CAMERA REVIEW Important" and
the controller ruling in Task 7's brief): fold N's finished ribbon can
arrive on the wire AFTER fold N+1's `job_start`, because the daemon starts
folding again before the UI has finished rendering the previous result.
Without a machine-enforced minimum viewing period, the booth's camera would
lock onto fold N's ribbon and then get reset by fold N+1's `job_start`
before a visitor -- or the ambient attract loop -- ever really saw it:
measured at ~27% of each fold's collapse actually shown on screen. That is
the demo's headline defect for a booth whose whole premise is "watch it
fold".

`showcase_dwell_s` is the fix: once a structure finishes, the booth is
guaranteed to hold on it for at least this long. Within the normal
visitor loop, nothing -- not a new `job_start`, not a `job_error`, not a
touch -- can cut that short; only `tick(now)` ends a showcase, and only
once the dwell has actually elapsed. The ONE deliberate exception is
`not_ready`: a degrading daemon overrides a showcase in progress exactly
as it overrides everything else (see `on_event`), on the CONTROLLER
RULING that a daemon which can no longer fold must be surfaced to the
visitor immediately -- hiding that behind a decorative dwell serves
nobody. `test_not_ready_ends_a_showcase_immediately_even_mid_dwell`
(tests/unit/test_states.py) pins this choice down so a future edit
cannot silently flip it either way.

Default: 3.0 seconds. The measured warm fold time is 4.35-4.45s (cold
5.7s) and the idle timeout is 45s, so 3.0s sits well inside both: long
enough that a visitor's eye actually registers the finished structure
(more than 2x the ~1.2s the old, unguarded sequencing left on screen), yet
short enough that it does not visually dominate an attract cycle whose own
fold takes about as long. It is a constructor parameter, not a constant,
so a later tuning pass (or a per-target override) doesn't need a code
change here.

Answering Task 9's two questions from state alone
--------------------------------------------------
1. "Should incoming point frames be displayed right now?" -- No, whenever
   `.state == "showcase"`: that is precisely the guarantee this module
   exists to make. Any other state, yes.
2. "Has the showcase dwell elapsed?" -- Whenever `.state` is no longer
   `"showcase"` after a `tick(now)` call, the dwell has elapsed (or the
   booth was never showcasing to begin with). Task 9 does not need to
   track timestamps itself; it only needs to keep calling `tick(now)` and
   read `.state`.

Both are now spelled out as named predicates at the bottom of this module
(`points_are_visible`, `ribbon_may_be_revealed`, `showcase_ended`) rather
than left as `== "showcase"` comparisons inlined at the GTK call site.
They are one line each and could have lived in ui/app.py; they are here
because they are *decisions about booth state*, and the plan's rule for
Task 9 is that the wiring layer makes none of those. Keeping them here
also means each one is tested against the state machine itself, in a file
with no GTK in it at all.
"""

from enum import Enum


class BoothState(str, Enum):
    """The booth's five display states, as plain strings.

    Subclassing `str` means `sm.state == "attract"` (the form every test
    and every other module uses) just works, without callers needing to
    know this is an enum at all.
    """

    ATTRACT = "attract"
    GALLERY = "gallery"
    FOLDING = "folding"
    SHOWCASE = "showcase"
    PREPARING = "preparing"


class StateMachine:
    """Governs `BoothState` transitions for one booth.

    Construction takes only durations; all state lives on the instance.
    Callers drive it with `on_event` (protocol events off the wire),
    `on_touch`/`on_pick` (visitor input), and `tick(now)` (a clock
    reading, for the two time-based transitions: the idle timeout and the
    showcase dwell). Every method returns the resulting `.state`.
    """

    def __init__(self, idle_timeout_s=45.0, showcase_dwell_s=3.0):
        self.idle_timeout_s = idle_timeout_s
        self.showcase_dwell_s = showcase_dwell_s

        self.state = BoothState.ATTRACT
        self.selected_target = None

        # Idle-timeout bookkeeping.
        #
        # on_touch/on_pick receive no clock reading of their own (that is
        # the whole point of keeping this machine pure -- see the module
        # docstring), so a touch cannot stamp "now" directly. Instead a
        # touch only sets `_idle_dirty`; the NEXT tick(now) call is what
        # stamps `_idle_baseline = now` and clears the flag. That means
        # the idle clock always effectively starts from the tick closest
        # to the real touch, not from whatever tick happened to run
        # before it -- which is exactly what lets a touch late in an idle
        # window (test: "the idle timer resets on every touch") actually
        # push the deadline out, rather than a touch only mattering if it
        # happens to land before the first tick after gallery/folding was
        # entered.
        self._idle_dirty = False
        self._idle_baseline = None

        # Showcase-dwell bookkeeping, the same deferred-stamp trick:
        # `job_done` sets `_showcase_entered_at = None` (unknown -- it has
        # no clock reading either), and the first tick(now) after that
        # stamps it.
        self._showcase_entered_at = None

    # -- protocol events -----------------------------------------------

    def on_event(self, event):
        """Advance state given one decoded protocol event (see
        protocol.events.EVENT_TYPES) and return the resulting state."""
        etype = event.get("type")

        # not_ready is the daemon's degrade path. It must be visible to a
        # visitor immediately -- that is what `preparing` is for -- and it
        # overrides whatever the visitor was doing, unconditionally --
        # INCLUDING an in-progress `showcase`, deliberately: this is the one
        # documented exception to the dwell guarantee (see module
        # docstring). A daemon that just told us it cannot fold must not be
        # hidden behind up to `showcase_dwell_s` of "look at this finished
        # structure" first.
        if etype == "not_ready":
            self.state = BoothState.PREPARING
            self.selected_target = None
            return self.state

        if self.state == BoothState.PREPARING:
            # Only a fresh job_start signals the daemon has actually
            # recovered. Recovering into `gallery` (whatever the visitor
            # had open before the degrade) would show a screen built on
            # stale, pre-degrade information; `attract` is the only
            # destination that doesn't imply anything false about what's
            # currently available. Every other event type is ignored
            # while preparing -- a stray job_done/job_error/stage/frame
            # from work in flight when not_ready fired must not paper
            # over the degrade message before the daemon is truly back.
            if etype == "job_start":
                self.state = BoothState.ATTRACT
                self.selected_target = None
            return self.state

        if etype == "job_start":
            # A showcase is already holding a finished structure for its
            # mandated dwell (see module docstring: this is the fix for
            # the measured "only ~27% of the collapse shown" defect).
            # The daemon routinely starts the NEXT fold before the UI has
            # even finished rendering the previous ribbon -- that ordering
            # is exactly the bug this dwell exists to survive -- so a
            # job_start arriving mid-showcase must not cut it short.
            if self.state == BoothState.SHOWCASE:
                return self.state
            # The daemon folds continuously to keep the attract loop
            # alive; most job_start events have no visitor pick behind
            # them at all. Only treat this as "a visitor's fold started"
            # when a pick is actually outstanding -- otherwise leave
            # whatever screen is already up (almost always `attract`)
            # alone.
            if self.selected_target is not None:
                self.state = BoothState.FOLDING
            return self.state

        if etype == "job_done":
            # Every finished structure gets a full showcase dwell,
            # whether it came from a visitor's own pick or the ambient
            # attract loop's continuous folding -- the measured defect
            # this guards against was reproduced on attract-loop cycles,
            # not only visitor picks. A job_done that arrives while an
            # earlier showcase is still being held (rare, but possible if
            # the daemon runs ahead) starts a fresh dwell for the NEW
            # ribbon rather than extending the old one.
            #
            # ...with ONE exception: a visitor standing at the booth with
            # the gallery open. This branch used to fire from `gallery`
            # too, flagged in Task 7 as "believed unreachable while the
            # protocol stays strictly serial". It is not unreachable; it
            # is the ORDINARY case, reproduced on screen against the mock
            # runner during Task 9 (see that task's report and its
            # screenshot sequence): the daemon's attract loop finishes a
            # fold every ~4s no matter what the visitor is doing, so an
            # open gallery was being torn down and replaced by a showcase
            # within about two seconds -- long before anyone could read
            # three blurbs and choose one. `gallery` is the only state
            # that means "a human is mid-decision", and nothing the
            # ambient loop finishes on its own is worth interrupting that
            # for. (`folding` deliberately still showcases: there the
            # visitor is watching a fold, and its completion is precisely
            # what they are waiting to see.)
            if self.state == BoothState.GALLERY:
                return self.state
            self.state = BoothState.SHOWCASE
            self._showcase_entered_at = None
            return self.state

        if etype == "job_error":
            # A failed fold must not strand a visitor on a folding screen
            # that will never complete. This only means something while
            # actually folding -- an attract-loop failure never moved the
            # booth off `attract` in the first place, so there is nothing
            # to unstick.
            if self.state == BoothState.FOLDING:
                self.state = BoothState.GALLERY
                self.selected_target = None
            return self.state

        # hello, stage, frame, card_state: informational only. The
        # pipeline/telemetry panels (Task 5) and the viewer (Task 2/camera
        # fix) consume these directly; they never move the booth between
        # its five display states.
        return self.state

    def on_structure_revealed(self):
        """The finished structure is now genuinely ON SCREEN -- restart the
        dwell from this instant.

        `job_done` says the daemon finished; it does not say the visitor can
        see anything yet. Between the two sits the ribbon build (measured at
        up to ~1.2s for 3000 residues, on a worker thread -- see ui/app.py)
        and then the viewer's own 0.8s cross-fade. A dwell measured from
        `job_done` is therefore shortened by exactly that much: for a large
        structure it can be entirely consumed before the ribbon has even
        finished fading in, which would put this module back to guaranteeing
        nothing at all -- the very defect it exists to fix.

        Deliberately a no-op outside `showcase`. If the ribbon arrives after
        the dwell has already expired, the booth has moved on to the next
        fold's live diffusion and that reveal's moment has passed (ui/app.py
        drops such a ribbon rather than throwing it over live diffusion --
        see `ribbon_may_be_revealed`); silently re-entering a showcase we
        already left would be the same bug wearing a different hat.

        Re-stamping is spelled the same way `job_done` spells it: set
        `_showcase_entered_at` back to None and let the NEXT `tick(now)`
        supply the clock reading, because this method -- like every other
        non-tick entry point here -- is given no clock of its own, by
        design (see the module docstring on purity).
        """
        if self.state == BoothState.SHOWCASE:
            self._showcase_entered_at = None
        return self.state

    # -- visitor input ----------------------------------------------------

    def on_touch(self):
        """A visitor touched the booth. Opens the gallery from attract;
        otherwise just resets the idle clock (see `tick`)."""
        self._idle_dirty = True
        if self.state == BoothState.ATTRACT:
            self.state = BoothState.GALLERY
        return self.state

    def on_pick(self, target_id):
        """A visitor picked a target off the gallery. Only meaningful from
        `gallery` -- a pick with no gallery open is not a thing the booth
        UI can produce, so it is ignored elsewhere rather than guessed
        at."""
        self._idle_dirty = True
        if self.state == BoothState.GALLERY:
            self.selected_target = target_id
            self.state = BoothState.FOLDING
        return self.state

    # -- clock --------------------------------------------------------

    def tick(self, now):
        """Advance the two time-based transitions given a clock reading,
        and return the resulting state. Never blocks, never sleeps, owns
        no timer of its own -- the caller (Task 9's GTK wiring) decides how
        often to call this."""

        if self.state == BoothState.SHOWCASE:
            if self._showcase_entered_at is None:
                # First tick since job_done -- this is "now" for dwell
                # purposes.
                self._showcase_entered_at = now
            elif now - self._showcase_entered_at >= self.showcase_dwell_s:
                self.state = BoothState.ATTRACT
                self.selected_target = None
                self._showcase_entered_at = None
                # A fresh idle window starts once the booth returns to
                # attract; nothing to time out from yet.
                self._idle_dirty = False
                self._idle_baseline = None
            return self.state

        if self.state in (BoothState.GALLERY, BoothState.FOLDING):
            if self._idle_dirty or self._idle_baseline is None:
                self._idle_baseline = now
                self._idle_dirty = False
            elif now - self._idle_baseline >= self.idle_timeout_s:
                self.state = BoothState.ATTRACT
                self.selected_target = None
                self._idle_baseline = None
            return self.state

        # attract, preparing: neither times out on its own. attract has
        # nowhere further to fall back to; preparing is released only by
        # a real job_start (on_event, above), never by the clock.
        return self.state


# ---------------------------------------------------------------------------
# The three questions the GTK wiring layer asks about state, named.
#
# Each is a one-line comparison and each could trivially have been inlined
# in ui/app.py. They live here because they are decisions about what the
# booth's state MEANS, and Task 9's own rule is that the wiring layer makes
# none of those -- it only carries them out. Being here also makes each one
# testable with no GTK, no viewer and no display (tests/unit/test_states.py).
# ---------------------------------------------------------------------------

def points_are_visible(state):
    """Should a diffusion frame arriving right now be put on screen?

    No while `showcase` is holding a finished structure -- and note what
    those suppressed frames actually ARE: the daemon starts fold N+1 before
    fold N's ribbon has even been built, so a frame arriving mid-showcase is
    fold N+1's *opening noise*, three orders of magnitude wider than the
    finished structure it would be drawn over. Letting it through is what
    put the point cloud beyond the camera's far plane and left fold N's
    ribbon cross-fading in over nothing (measured; see the module docstring).

    Suppressed, not discarded: ui/app.py leaves the frame in its one-slot
    latest-wins buffer, so the instant the dwell expires the booth cuts
    straight to live diffusion rather than to an empty screen.
    """
    return state != BoothState.SHOWCASE


def ribbon_may_be_revealed(state):
    """Is this still the moment to cross-fade a finished structure in?

    Only while showcasing. A ribbon build that outlasts its own dwell
    (possible for a large structure) would otherwise land on a booth that
    has already moved on to the next fold's live diffusion, and cross-fading
    the previous structure in over it is the headline defect arriving by a
    different route. The right answer then is to drop it: its fold is over.
    """
    return state == BoothState.SHOWCASE


def showcase_ended(previous, current):
    """Did the showcase dwell just expire, between these two states?

    This is the edge, not the level -- the one instant at which the booth
    owes the screen a transition (ui/app.py: apply the `job_start` clear it
    deferred, then show the newest buffered diffusion frame). Answering it
    from a (previous, current) pair rather than a timestamp is what keeps
    the caller free of any clock bookkeeping of its own.
    """
    return previous == BoothState.SHOWCASE and current != BoothState.SHOWCASE
