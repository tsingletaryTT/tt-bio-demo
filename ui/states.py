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

What this module is NOT, once the booth folds on four chips
------------------------------------------------------------
This module speaks for the BOOTH -- one screen, one visitor -- and nothing
below is per-fold. With four folds in flight at once (see
docs/superpowers/plans/2026-08-13-multi-chip-folding.md and its normative
"Per-fold vs global" table), the two halves separate:

  * The showcase dwell that decides what is actually drawn now lives in
    `ui/slots.py`, one per cell (`SlotState.showcase_dwell_s`). It has to:
    cell 1 is mid-diffusion while cell 0 holds a finished structure, and one
    global dwell would suppress frames in all four. The dwell described
    below is therefore the BOOTH's, not any one fold's.
  * `BoothState.SHOWCASE` stays global and FOLLOWS THE FOCUS SLOT -- it
    exists so the gallery, the 45s idle timeout and the `preparing` overlay
    keep working off one state machine. Frame suppression no longer reads
    it.
  * The three predicates at the bottom of this file are unchanged and are
    NOT duplicated in `ui/slots.py`. `SlotState` spells its showcase state
    with the same `"showcase"` string `BoothState.SHOWCASE` carries,
    precisely so they work on a slot as-is: `ui/slots.py` calls
    `points_are_visible`/`ribbon_may_be_revealed` from here to back its own
    per-cell properties, and `showcase_ended(previous, current)` is imported
    from here by whoever holds the pair.

Everything else here -- the five booth states, the idle timeout, the
deferred touch, `preparing` -- stays exactly as described below and as
tested in tests/unit/test_states.py.

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
once the dwell has actually elapsed. A touch is not DISCARDED, though --
see "The deferred touch" below. The ONE deliberate exception is
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

The deferred touch
-------------------
The dwell above used to be paid for out of the visitor's pocket: a touch
arriving mid-showcase set only the idle flag and was then thrown away when
the dwell expired. Measured in Task 9, the booth sits in `showcase` for
46-50% of every attract cycle -- so roughly HALF of all first taps did
nothing a visitor could see, and a visitor who taps a booth and gets no
response concludes it is broken and walks away.

CONTROLLER RULING (Task 10): a touch during a showcase is REMEMBERED, and
acted on the instant the dwell expires -- at most `showcase_dwell_s`
later. Both promises are then kept at once: the finished structure still
gets every millisecond of the hold it was guaranteed (nothing about the
dwell itself changed; the pre-existing tests pinning it are untouched and
still green), and the visitor's tap is never lost -- the showcase ends in
`gallery` rather than `attract`.

The flag (`_deferred_touch`) is bounded by construction: it is set only
while showcasing, consumed by the very tick that ends that showcase, and
dropped outright if `not_ready` tears the showcase down instead (by the
time a degraded daemon recovers, the visitor who tapped is long gone, and
`preparing` releases to `attract` precisely so that no pre-degrade screen
gets resurrected). So it can never survive past the one showcase it was
recorded during.

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

    def __init__(self, idle_timeout_s=45.0, showcase_dwell_s=3.0,
                 dwell_caps=None, dwell_floor_s=None):
        self.idle_timeout_s = idle_timeout_s
        self.showcase_dwell_s = showcase_dwell_s

        # PER-TARGET DWELL, the same policy ui/slots.py applies per cell and
        # for the same reason: a hold on a finished structure is paid for by
        # suppressing the NEXT fold's opening frames, so what it can afford
        # is a property of the target that is COMING. `dwell_caps` is
        # {target_id: seconds}, each target's measured `first_frame_s`;
        # `dwell_floor_s` is the minimum a cap may narrow a dwell to.
        # Absent (every existing caller and test) it is inert and this
        # behaves exactly as a single fixed dwell.
        self.dwell_caps = dict(dwell_caps or {})
        self.dwell_floor_s = (showcase_dwell_s if dwell_floor_s is None
                              else dwell_floor_s)
        self._effective_dwell_s = showcase_dwell_s

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

        # "A visitor tapped while a structure was being showcased." Set by
        # `on_touch` only while showcasing; consumed by the tick that ends
        # that showcase (which then opens the gallery instead of returning
        # to attract); dropped by `not_ready`. See "The deferred touch" in
        # the module docstring for the ruling behind it.
        self._deferred_touch = False

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
            # A tap this showcase was holding for later dies with the
            # showcase. Honoring it after the daemon recovers would open a
            # gallery, minutes later, for a visitor who has already left --
            # the same "screen built on pre-degrade information" this
            # branch's release-to-attract rule exists to prevent.
            self._deferred_touch = False
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
                # Knowing what is next is knowing what this hold can afford.
                # Narrow only -- a dwell already being served must never grow
                # under a visitor who is looking at it.
                self._effective_dwell_s = min(
                    self._effective_dwell_s,
                    self._dwell_for(event.get("target_id")))
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
            # A finished structure gets a full showcase dwell whether it
            # came from a visitor's own pick or from the ambient attract
            # loop's continuous folding -- the measured defect this guards
            # against was reproduced on attract-loop cycles, not only
            # visitor picks. A job_done that arrives while an earlier
            # showcase is still being held (rare, but possible if the daemon
            # runs ahead) starts a fresh dwell for the NEW ribbon rather
            # than extending the old one.
            #
            # This branch is NOT reached from every state, and the two it is
            # not reached from are both deliberate:
            #
            #   - `preparing` never gets here at all -- the `not_ready`
            #     branch above returns first, so a stray job_done from work
            #     still in flight when the daemon degraded cannot paper over
            #     the degrade message (test:
            #     test_job_done_while_preparing_does_not_leak_into_showcase).
            #   - `gallery` returns immediately below.
            #
            # ...which leaves `attract`, `folding` and an earlier
            # `showcase`. The `gallery` exception exists because a visitor
            # is standing at the booth with the gallery open. This branch
            # used to fire from `gallery` too, flagged in Task 7 as
            # "believed unreachable while the
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
            # A fresh showcase starts at the maximum; the next job_start
            # narrows it. Without this reset one short target in the
            # rotation would pin every later hold to its cap for the rest
            # of the session.
            self._effective_dwell_s = self.showcase_dwell_s
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
        """A visitor touched the booth.

        Opens the gallery from `attract`. From `showcase` the gallery is
        opened LATER, by the tick that ends the dwell -- the touch is
        recorded here and honored there, never discarded (see "The deferred
        touch" in the module docstring; the state returned is still
        `showcase`, because the finished structure keeps the screen it was
        guaranteed). From every other state it just resets the idle clock
        (see `tick`).
        """
        self._idle_dirty = True
        if self.state == BoothState.ATTRACT:
            self.state = BoothState.GALLERY
        elif self.state == BoothState.SHOWCASE:
            self._deferred_touch = True
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

    @property
    def effective_dwell_s(self):
        """How long THIS showcase actually holds, in seconds.

        `showcase_dwell_s` is the maximum any showcase may take;
        this is what the one currently being served will take, after the
        incoming target's cap has been applied. Read this, not
        `showcase_dwell_s`, when you need the number the clock is being
        compared against -- they differ whenever the next fold cannot afford
        the full hold.
        """
        return self._effective_dwell_s

    def _dwell_for(self, target_id):
        """The longest dwell affordable in front of `target_id` -- its
        measured `first_frame_s` clamped into
        [dwell_floor_s, showcase_dwell_s].

        An unmeasured or unknown target gets the FLOOR, not the maximum.
        A long hold is a bet that the incoming fold can afford to have its
        opening frames suppressed, and only a measurement settles that; with
        no number, the booth declines the bet and keeps the behaviour it had
        before per-target dwells existed. It also makes this whole mechanism
        a no-op on any playlist that measures nothing, which is how it was
        verified against the existing suite.
        """
        cap = self.dwell_caps.get(target_id)
        if cap is None:
            return self.dwell_floor_s
        return max(self.dwell_floor_s, min(self.showcase_dwell_s, cap))

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
            elif now - self._showcase_entered_at >= self._effective_dwell_s:
                # Where the booth goes now is the ONE thing the deferred
                # touch changes: a visitor who tapped during the dwell gets
                # the gallery they asked for, in the same instant the
                # structure's guaranteed hold ends -- not one tick later
                # (which at 100ms would flash the attract screen in between)
                # and not never (which is the defect this ruling fixes).
                touched, self._deferred_touch = self._deferred_touch, False
                self.state = BoothState.GALLERY if touched else BoothState.ATTRACT
                self.selected_target = None
                self._showcase_entered_at = None
                if touched:
                    # The gallery opens NOW, so its 45s idle window starts
                    # now -- stamped directly rather than left for the next
                    # tick, so this visitor gets the full window and not
                    # one shortened by however long the dwell ran.
                    self._idle_baseline = now
                    self._idle_dirty = False
                else:
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
