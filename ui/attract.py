"""The booth showing off its own instruments, when nobody is standing there.

WHY THIS EXISTS. `D` opens the live protocol tap and `T` opens the Tensix
activity panel, and almost nobody presses either. A visitor walks up, watches
a protein condense, and walks away never knowing the booth could show them
what the chips were doing while it happened. The two most interesting things
in the rail were reachable only by someone who already knew they were there.

So when the booth has been left alone, it demonstrates them itself: opens the
diagnostics tap for a while, closes it, opens the Tensix panel for a while,
closes it, and goes quiet again. A passer-by sees the instruments; nobody has
to be told a keyboard shortcut.

THE RULES THIS MUST NOT BREAK, and they are what most of the code below is
for:

1. **It never fights a visitor.** Nothing starts until the booth has been idle
   longer than its own visitor timeout, so the choreography cannot begin while
   someone is still interacting. The first touch stops it.

2. **It never leaves the booth changed.** Everything it opens, it closes --
   and if a visitor interrupts mid-cue, whatever it opened is closed on the
   way out. The booth a person walks up to looks the same as the booth that
   was left.

3. **It does not take a panel away from a visitor.** If a visitor opened the
   diagnostics themselves, the choreography must not close it: ownership is
   tracked, and it only ever closes what it opened. Getting this wrong would
   be worse than not having the feature -- a panel that shuts while you are
   reading it feels broken.

PURE, like `ui/states.py` and `ui/slots.py` and for the same reason: what the
booth decides to do on its own is exactly the kind of thing that has to be
testable with no display and no clock of its own. This module reads a clock it
is handed and returns actions; it touches no widget.
"""

import logging

log = logging.getLogger(__name__)

# ── the cues ────────────────────────────────────────────────────────────────
#
# One cycle, as offsets in seconds from the moment the choreography starts.
# Read it as a score: open, hold, close, rest, open, hold, close, rest.
#
# The holds are long enough to notice and read (the tap is text, and text
# needs dwelling on), and the rests are long enough that the booth spends most
# of its idle time showing the protein -- which is the thing people actually
# stop for. A booth that flickered panels every few seconds would be a booth
# nobody could photograph.

SHOW_GALLERY = "show_gallery"
HIDE_GALLERY = "hide_gallery"
OPEN_DIAGNOSTICS = "open_diagnostics"
CLOSE_DIAGNOSTICS = "close_diagnostics"
OPEN_TENSIX = "open_tensix"
CLOSE_TENSIX = "close_tensix"

#: Seconds of no input at all before any of this begins. Deliberately longer
#: than the state machine's own 45s idle timeout, so the booth has already
#: returned to its attract loop before the instruments start moving -- the
#: choreography is something that happens to an empty booth, never to a
#: visitor who has merely paused.
START_AFTER_IDLE_S = 60.0

#: The score. (offset_s, action). Total cycle length is CYCLE_S.
SCORE = (
    (0.0, OPEN_DIAGNOSTICS),
    (18.0, CLOSE_DIAGNOSTICS),
    (33.0, OPEN_TENSIX),
    (51.0, CLOSE_TENSIX),
    # The menu, last in the cycle and briefest of the three. A visitor who
    # never touches the booth has no way of learning it can be driven at all,
    # so it shows them -- but the gallery is the one cue that replaces the
    # protein rather than sitting beside it, and the protein is what people
    # stop for. Eight seconds is long enough to register "I could pick one"
    # and short enough not to interrupt a fold anybody is watching.
    (66.0, SHOW_GALLERY),
    (74.0, HIDE_GALLERY),
)
CYCLE_S = 90.0


class Choreography:
    """Decides which panel cues are due. Owns no widgets and no clock.

    Drive it by calling `tick(now, idle_s)` on whatever cadence the app
    already has, and apply whatever it returns. Call `interrupted()` the
    moment a visitor does anything.
    """

    def __init__(self, start_after_idle_s=START_AFTER_IDLE_S,
                 score=SCORE, cycle_s=CYCLE_S):
        self._start_after = float(start_after_idle_s)
        self._score = tuple(sorted(score))
        self._cycle_s = float(cycle_s)
        # When the current cycle began, in the caller's clock. None = not
        # running.
        self._cycle_start = None
        # How far through the score we have already emitted, so a tick that
        # arrives late (a slow frame, a GC pause) still fires the cues it
        # skipped over rather than silently dropping them -- otherwise a
        # stalled booth could open a panel and never emit its close.
        self._next_cue = 0
        # What WE opened. Only these may be closed by us. See rule 3.
        self._owned = set()

    # ── state a caller may want to see ──────────────────────────────────
    @property
    def running(self):
        return self._cycle_start is not None

    @property
    def owned(self):
        """The panels this choreography opened and is responsible for."""
        return frozenset(self._owned)

    def owns(self, action_target):
        return action_target in self._owned

    # ── the tick ────────────────────────────────────────────────────────
    def tick(self, now, idle_s):
        """Return the list of cues due at `now`, given `idle_s` of no input.

        Empty list is the normal answer, which is the point: this is called
        on the booth's ordinary tick and must be nearly free.
        """
        if idle_s < self._start_after:
            # Not idle enough. If we were mid-cycle, someone has touched
            # something -- but `interrupted()` is what handles that, because
            # only the caller knows whether the idle clock was reset by a
            # visitor or by us.
            return []

        if self._cycle_start is None:
            self._cycle_start = now
            self._next_cue = 0
            log.info("attract choreography started after %.0fs idle", idle_s)

        elapsed = now - self._cycle_start
        if elapsed < 0:
            # A clock that went backwards (NTP step, a test being unkind).
            # Restart the cycle rather than emitting the whole score at once.
            self._cycle_start = now
            self._next_cue = 0
            return []

        actions = []
        while self._next_cue < len(self._score) and \
                elapsed >= self._score[self._next_cue][0]:
            actions.append(self._score[self._next_cue][1])
            self._next_cue += 1

        if elapsed >= self._cycle_s:
            # Next cycle. Any cue not reached is dropped deliberately: the
            # score is written so the closes always precede the cycle end.
            self._cycle_start = now
            self._next_cue = 0

        return [a for a in (self._apply_ownership(a) for a in actions) if a]

    def _apply_ownership(self, action):
        """Record what we open, and refuse to emit a close for what we do not
        own -- the visitor-opened-it case (rule 3)."""
        if action == OPEN_DIAGNOSTICS:
            self._owned.add("diagnostics")
            return action
        if action == OPEN_TENSIX:
            self._owned.add("tensix")
            return action
        if action == CLOSE_DIAGNOSTICS:
            if "diagnostics" not in self._owned:
                return None
            self._owned.discard("diagnostics")
            return action
        if action == CLOSE_TENSIX:
            if "tensix" not in self._owned:
                return None
            self._owned.discard("tensix")
            return action
        if action == SHOW_GALLERY:
            self._owned.add("gallery")
            return action
        if action == HIDE_GALLERY:
            if "gallery" not in self._owned:
                return None
            self._owned.discard("gallery")
            return action
        return action

    def disown(self, panel):
        """Hand a panel back to the visitor.

        Called when a visitor toggles a panel themselves: from that moment it
        is theirs, and the choreography's pending close for it must not fire.
        """
        self._owned.discard(panel)

    def interrupted(self):
        """A visitor did something. Stop, and say what needs closing.

        Returns the close actions for everything still open on our account,
        so the caller can put the booth back the way it was found. Idempotent.
        """
        closes = []
        if "diagnostics" in self._owned:
            closes.append(CLOSE_DIAGNOSTICS)
        if "tensix" in self._owned:
            closes.append(CLOSE_TENSIX)
        if "gallery" in self._owned:
            # A visitor arriving mid-showcase gets the gallery they can see,
            # not one yanked away -- but the booth must not be left believing
            # IT opened the menu, so ownership is released either way. The
            # caller decides what to do with a gallery a visitor is now
            # looking at; see `ui/app.py`.
            closes.append(HIDE_GALLERY)
        self._owned.clear()
        if self._cycle_start is not None:
            log.info("attract choreography interrupted by input")
        self._cycle_start = None
        self._next_cue = 0
        return closes
