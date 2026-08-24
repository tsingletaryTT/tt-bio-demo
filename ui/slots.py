"""Per-fold state: which of the four cells is showing what, right now.

Pure by construction, exactly like `ui/states.py`: protocol events, a clock
reading, and the visitor's pick go in; slot indices and slot states come out.
No GTK, no OpenGL, no timers, no imports beyond the standard library and
`ui.states`. This module must never import torch, tt-bio, or GTK.

Why this module exists
----------------------
The booth used to fold on one chip and render one structure, so "the fold"
and "the booth" were the same thing and one `StateMachine` could speak for
both. Folding on four chips at once splits that in half, and the split is
NOT negotiable per-file taste -- it is the normative table in
docs/superpowers/plans/2026-08-13-multi-chip-folding.md ("Per-fold vs
global: the table that decides the whole UI"). Two lines of it are what this
module is:

  * the showcase dwell, and the two predicates that read it
    (`points_are_visible`, `ribbon_may_be_revealed`), become **per slot** --
    cell 1 is mid-diffusion while cell 0 holds a finished structure, and a
    global dwell would suppress frames in all four;
  * the visitor's pick stays **global**, and lives here, in `SlotRouter` --
    one visitor, one pick. It *selects* a slot (the focus) but is not
    per-slot state, and a second copy of it in `ui/app.py` is how the daemon
    and the screen end up disagreeing about what was asked for.

Everything else the table marks "stays global" (booth state, the idle
timeout, the overlays, telemetry, the diagnostics log) stays in `ui/states.py`
and `ui/app.py` and is not touched from here.

The three hard questions, answered with no display
---------------------------------------------------
1. *Which cell does this event belong to?* `SlotRouter.on_event`. Only
   `job_start` carries `card`; every later event of a fold carries `job_id`
   and nothing else, which is why the UI keys by `job_id` and why an event
   for a job the router has never seen belongs to **no** slot rather than to
   whichever cell happens to be first.
2. *Has this cell's dwell expired?* `SlotState.tick(now)`, per cell,
   measured from the moment the structure was genuinely revealed rather than
   from `job_done` (see `on_structure_revealed`).
3. *Which cell is the booth following?* `SlotRouter.focus_slot`, stated once
   below.

The focus rule
--------------
The focus slot is the slot folding (or showcasing) the visitor's selected
target if there is one; otherwise the slot that most recently entered
`showcase`; otherwise slot 0.

The pick-status rule
--------------------
`"folding"` once some slot holds the selected target; `"queued"` while it is
selected and no slot holds it yet; `"waiting"` once that has been true for
`PICK_PENDING_WARN_S`; `None` when there is no pick.

It exists because the daemon is allowed to take a few seconds to pick the
pick up -- it dispatches to the next chip to free and deliberately does not
preempt a fold already running -- and **a visitor who taps and sees nothing
concludes the booth is broken**. Note that the status is decided here and
*not* by asking whether a fold has started: the router already knows both
that a target was selected and which slots hold which targets, and a second
source of truth in `ui/app.py` is how the screen and the daemon end up
disagreeing. In particular a pick for a target that is *already folding*
resolves to `"folding"` at once -- the daemon queues nothing in that case,
so a router that waited for a `job_start` that will never come would leave
the booth saying NEXT UP forever about something already on screen.

Reused, not reimplemented
-------------------------
`ui.states.points_are_visible`, `ui.states.ribbon_may_be_revealed` and
`ui.states.showcase_ended` are used as-is, against slot states. `SlotPhase`
deliberately spells its showcase state `"showcase"` -- the same string
`BoothState.SHOWCASE` carries -- precisely so those three tested functions
keep working with no second copy of the rule to drift. The first two are
surfaced here as properties on `SlotState` (that is the shape the renderer
wants: "may *this cell* draw points?"); `showcase_ended` is an edge between
two readings and stays a free function, imported from `ui.states` by whoever
holds the pair.
"""

from collections import OrderedDict
from enum import Enum

from ui.states import points_are_visible as _points_are_visible
from ui.states import ribbon_may_be_revealed as _ribbon_may_be_revealed


# How many folds the quad view can show at once. A booth with more chips
# than this folds on all of them and shows the first four: better than
# crashing, and better than silently drawing the fifth over the first.
MAX_SLOTS = 4

# How long a visitor's pick may sit unstarted before the booth should say
# something more than "next up". Measured against the pick, not the wall
# clock: the daemon dispatches a pick to the next chip to free rather than
# preempting a fold in flight, so a wait of a few seconds is normal and a
# wait of ten is worth explaining.
PICK_PENDING_WARN_S = 10.0

# Upper bound on the job_id -> slot map. Four jobs per cell is enough to
# route late events for a cell's previous folds (a `frame` that lost a race
# with its own `job_done`, a trailing `stage`) while keeping the map a fixed
# size: an all-day booth folds thousands of jobs, and a dict that remembers
# every one is a leak with a screen attached.
JOB_MAP_LIMIT = MAX_SLOTS * 4


class SlotPhase(str, Enum):
    """What one cell of the quad is doing, as plain strings.

    Subclassing `str` means `slot.state == "folding"` (the form every test
    and every other module uses) just works, and -- the load-bearing part --
    `SlotPhase.SHOWCASE` compares equal to `BoothState.SHOWCASE`, so
    `ui/states.py`'s three predicates work on slot states unchanged.
    """

    IDLE = "idle"
    FOLDING = "folding"
    SHOWCASE = "showcase"


class SlotState:
    """One cell of the quad: one chip's current fold and its own dwell.

    Driven by the three lifecycle events of a fold (`job_start`, `job_done`,
    `job_error`), by `on_structure_revealed()` when the finished ribbon is
    genuinely on screen, and by `tick(now)`. Every method returns the
    resulting `.state`.

    Stale events are IGNORED, not applied: a late `job_error` for the fold
    this cell has already moved on from would otherwise blank a cell that is
    happily mid-diffusion on the next one.
    """

    def __init__(self, showcase_dwell_s=2.0, card=None, dwell_caps=None,
                 dwell_floor_s=None):
        self.showcase_dwell_s = showcase_dwell_s

        # PER-TARGET DWELL. `showcase_dwell_s` is the MAXIMUM a finished
        # structure may hold this cell; `dwell_caps` is {target_id: seconds}
        # -- each target's measured time to its own first frame
        # (`Target.first_frame_s`) -- and `dwell_floor_s` the minimum any
        # dwell may be clamped to.
        #
        # Why a cap keyed on the INCOMING target rather than the one being
        # shown: holding fold N on screen is paid for by suppressing fold
        # N+1's opening frames, because the daemon starts N+1 the instant N
        # ends and its first frame is what supersedes the structure. So the
        # affordable dwell is a property of what is coming, not of what is
        # finished. A target that reaches coordinates in ~1s cannot have a
        # long hold in front of it without its whole collapse going unseen
        # (measured: a flat 7s dwell put Trp-cage's visible collapse at
        # 0/30 frames); one that takes 80s can afford any hold at all.
        #
        # Empty/None means "one number covers every target", which is what
        # every existing caller and test does -- so this is inert unless a
        # playlist supplies measurements.
        self.dwell_caps = dict(dwell_caps or {})
        self.dwell_floor_s = (showcase_dwell_s if dwell_floor_s is None
                              else dwell_floor_s)

        # The dwell THIS showcase is actually serving. Starts as the maximum
        # and is narrowed the moment the incoming job_start tells us what is
        # next; `tick` reads this, never `showcase_dwell_s` directly.
        self._effective_dwell_s = showcase_dwell_s

        # Which physical chip this cell is showing. Fixed for the life of
        # the cell when a router builds it; a bare SlotState learns it from
        # the first job_start it applies (only job_start carries `card`).
        self.card = card

        self.state = SlotPhase.IDLE
        self.job_id = None
        self.target_id = None

        # Dwell bookkeeping, spelled exactly the way ui/states.py spells it:
        # nothing outside `tick` is given a clock reading, so job_done (and
        # on_structure_revealed) set this to None -- "unknown" -- and the
        # next tick(now) stamps it. That is what keeps this module pure.
        self._showcase_entered_at = None

        # Events that arrived while this cell was holding a finished
        # structure for its guaranteed dwell, replayed in order the instant
        # the dwell expires. Bounded by construction: a deferred `job_start`
        # REPLACES the list (a card's newer fold supersedes an older
        # deferred one), and only that job's own terminal event may be
        # appended after it -- so at most two entries, ever.
        self._deferred = []

    # -- fold lifecycle ---------------------------------------------------

    def on_job_start(self, event):
        """A fold started on this cell's chip.

        Mid-showcase this is DEFERRED, not applied. The daemon starts the
        next fold on a chip the instant the last one finishes -- that
        ordering is exactly what the dwell exists to survive -- so a
        `job_start` arriving during the dwell must not cut it short. The
        clear still belongs to `job_start`; the dwell only delays it.
        """
        if self.state == SlotPhase.SHOWCASE:
            self._deferred = [("job_start", event)]
            # We now know what is coming, so we know what this hold can
            # afford. Narrow it (never widen: a dwell already being served
            # must not grow under a visitor's feet), and if the narrowed
            # dwell is already spent the next tick ends the showcase at once.
            self._effective_dwell_s = min(self._effective_dwell_s,
                                          self._dwell_for(event.get("target_id")))
            return self.state
        self._apply_job_start(event)
        return self.state

    def on_job_done(self, event):
        """This cell's fold finished: hold the result for the dwell.

        Only for the fold this cell is actually running. A `job_done` for
        some other job -- one this cell has moved on from, or one it never
        ran -- is ignored.
        """
        if self.state == SlotPhase.SHOWCASE:
            self._defer_terminal("job_done", event)
            return self.state
        if self.state != SlotPhase.FOLDING or event.get("job_id") != self.job_id:
            return self.state
        self._apply_job_done(event)
        return self.state

    def on_job_error(self, event):
        """This cell's fold failed: end it with no showcase at all.

        There is nothing to hold on screen, so the cell goes straight to
        `idle`. Same staleness guard as `on_job_done`, and here it is the
        one that matters most: a late `job_error` for fold N applied while
        fold N+1 is mid-diffusion would blank a cell that is fine.
        """
        if self.state == SlotPhase.SHOWCASE:
            self._defer_terminal("job_error", event)
            return self.state
        if self.state != SlotPhase.FOLDING or event.get("job_id") != self.job_id:
            return self.state
        self._apply_job_error(event)
        return self.state

    def on_structure_revealed(self):
        """The finished structure is genuinely ON SCREEN in this cell --
        restart this cell's dwell from this instant.

        `job_done` says the daemon finished; it does not say the visitor can
        see anything yet. Between the two sit the ribbon build (measured at
        up to ~1.2s for 3000 residues, on a worker thread) and the viewer's
        0.8s cross-fade. A dwell measured from `job_done` is shortened by
        exactly that much -- for a large structure, entirely consumed before
        the ribbon has finished fading in, which would put the guarantee
        back to guaranteeing nothing.

        Deliberately a no-op outside `showcase`: a ribbon that outlasts its
        own dwell has missed its moment, and silently re-entering a showcase
        this cell already left is the same defect wearing a different hat.
        """
        if self.state == SlotPhase.SHOWCASE:
            self._showcase_entered_at = None
        return self.state

    # -- clock ------------------------------------------------------------

    def tick(self, now):
        """Advance this cell's dwell given a clock reading.

        Owns no timer; the caller decides how often to call it. Only this
        method can end a showcase.
        """
        if self.state != SlotPhase.SHOWCASE:
            return self.state

        if self._showcase_entered_at is None:
            # First tick since the structure was revealed (or since
            # job_done, if the reveal never arrived) -- this is "now" for
            # dwell purposes.
            self._showcase_entered_at = now
            return self.state

        if now - self._showcase_entered_at < self._effective_dwell_s:
            return self.state

        # The dwell has been paid in full. Drop back to idle, then replay
        # whatever arrived while it ran -- the deferred job_start is
        # APPLIED here, not dropped: it is the fold this cell should now be
        # showing, and dropping it would leave the cell stuck on a
        # structure whose fold ended seconds ago.
        self._showcase_entered_at = None
        deferred, self._deferred = self._deferred, []
        self.state = SlotPhase.IDLE
        for kind, event in deferred:
            if kind == "job_start":
                self._apply_job_start(event)
            elif kind == "job_done":
                self._apply_job_done(event)
            elif kind == "job_error":
                self._apply_job_error(event)
        return self.state

    # -- the two questions the renderer asks about one cell ---------------

    @property
    def points_are_visible(self):
        """Should a diffusion frame for this cell be put on screen now?

        No while THIS cell holds a finished structure -- and note what those
        suppressed frames are: the daemon starts the next fold on this chip
        before the last ribbon has been built, so a frame arriving
        mid-showcase is the next fold's opening noise, orders of magnitude
        wider than the structure it would be drawn over.

        Per cell, not per booth: cell 1 being mid-diffusion has nothing to
        do with whether cell 0 is holding a result.
        """
        return _points_are_visible(self.state)

    @property
    def ribbon_may_be_revealed(self):
        """Is this still the moment to cross-fade this cell's ribbon in?

        Only while this cell is showcasing. A build that outlasts its own
        dwell would otherwise land on a cell that has moved on to the next
        fold's live diffusion.
        """
        return _ribbon_may_be_revealed(self.state)

    @property
    def pending_job_id(self):
        """The job id of a `job_start` deferred behind this cell's dwell, if
        any. Read by `SlotRouter` so that job's routing entry is never
        evicted while it is still waiting to be applied."""
        for kind, event in self._deferred:
            if kind == "job_start":
                return event.get("job_id")
        return None

    # -- unconditional appliers (guards live in the on_* methods) ---------

    def _apply_job_start(self, event):
        self.job_id = event.get("job_id")
        self.target_id = event.get("target_id")
        if event.get("card") is not None:
            self.card = event["card"]
        self.state = SlotPhase.FOLDING
        self._showcase_entered_at = None

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
        """The longest dwell affordable in front of `target_id`.

        Its measured `first_frame_s`, clamped into
        [dwell_floor_s, showcase_dwell_s]. An unmeasured or unknown target
        gets the FLOOR, not the maximum. A long hold is a bet that the
        incoming fold can afford to have its opening frames suppressed, and
        only a measurement settles that; with no number the cell declines the
        bet and keeps the behaviour it had before per-target dwells existed.
        """
        cap = self.dwell_caps.get(target_id)
        if cap is None:
            return self.dwell_floor_s
        return max(self.dwell_floor_s, min(self.showcase_dwell_s, cap))

    def _apply_job_done(self, event):
        self.state = SlotPhase.SHOWCASE
        # A NEW showcase starts at the maximum; the incoming job_start
        # narrows it (see on_job_start). Without this reset a cell would
        # inherit the last fold's narrowed dwell forever -- so one Trp-cage
        # in the rotation would pin every later hold on this chip to 2s.
        self._effective_dwell_s = self.showcase_dwell_s
        # Unknown until the next tick supplies a clock reading; restamped
        # again if/when the ribbon is actually revealed.
        self._showcase_entered_at = None

    def _apply_job_error(self, event):
        self.state = SlotPhase.IDLE
        self._showcase_entered_at = None
        # `job_id`/`target_id` are deliberately left alone. The cell keeps
        # naming the fold it last showed, which is what lets the viewer hold
        # the last real structure (dimmed and captioned) rather than going
        # blank, and what lets the router still route this job's trailing
        # events somewhere sane.

    def _defer_terminal(self, kind, event):
        """Queue a `job_done`/`job_error` behind the dwell -- but only for
        the fold whose own `job_start` is already queued there.

        Anything else is stale by definition: this cell is showcasing fold
        N, so a terminal event can only legitimately belong to fold N+1 (the
        one waiting behind the dwell) -- fold N's own terminal event is what
        put the cell into `showcase` in the first place.
        """
        pending = self.pending_job_id
        if pending is None or event.get("job_id") != pending:
            return
        if self._deferred and self._deferred[-1][0] != "job_start":
            # One terminal event per deferred fold; a fold cannot finish
            # twice, and this is what keeps the queue at two entries.
            return
        self._deferred.append((kind, event))


class SlotRouter:
    """The quad: one `SlotState` per chip, plus the one global pick.

    Built from the daemon's card list (`hello`'s cards, in order). Routes
    every event to the cell it belongs to, answers "which cell is the booth
    following" and "what should the booth say about the visitor's pick", and
    owns no clock of its own.
    """

    def __init__(self, cards, showcase_dwell_s=2.0, dwell_caps=None,
                 dwell_floor_s=None):
        # Kept so `add_card` below can build a cell that keeps the same dwell
        # as the ones built here -- one cell holding a finished structure for
        # a different length of time than its neighbours would be a bug
        # nobody would think to look for. (The per-target caps are shared for
        # the same reason: two cells showing the same target must agree.)
        self.showcase_dwell_s = showcase_dwell_s
        self.dwell_caps = dict(dwell_caps or {})
        self.dwell_floor_s = dwell_floor_s
        # More chips than cells: fold on all of them, show the first four.
        self.cards = list(cards)[:MAX_SLOTS]
        self.slots = [self._new_slot(card) for card in self.cards]
        self._slot_by_card = {card: index for index, card in enumerate(self.cards)}

        # job_id -> slot index, oldest first, bounded (see JOB_MAP_LIMIT).
        self._job_slots = OrderedDict()

        # The visitor's pick. Global on purpose: one booth, one visitor, one
        # pick. `_selected_at` is a clock reading handed in by the caller --
        # this class never reads a clock itself.
        self.selected_target = None
        self._selected_at = 0.0

        # Focus bookkeeping. `_showcase_seq[i]` is a monotonic ticket
        # stamped each time slot i ENTERS showcase, so "the slot that most
        # recently entered showcase" is a max rather than a scan of
        # timestamps this class is not given. `_showcase_keys[i]` is how
        # entry is detected: a (state, job_id) pair changing INTO a
        # showcase, which catches re-entry within a single tick (a deferred
        # fold that both starts and finishes behind one dwell) that a plain
        # before/after state comparison would miss.
        self._showcase_seq = [None] * len(self.slots)
        self._showcase_keys = [None] * len(self.slots)
        self._seq = 0

    # -- lookup -----------------------------------------------------------

    def slot_for_card(self, card):
        """Which cell shows this chip, or None if this chip has no cell."""
        return self._slot_by_card.get(card)

    def _new_slot(self, card):
        """Build one cell. The ONLY place a SlotState is constructed here, so
        a cell added later cannot be given a different dwell policy than the
        ones built at startup."""
        return SlotState(showcase_dwell_s=self.showcase_dwell_s,
                         card=card,
                         dwell_caps=self.dwell_caps,
                         dwell_floor_s=self.dwell_floor_s)

    def add_card(self, card):
        """Give a chip the router has not seen before its own cell.

        Returns the new slot index, or None if the chip already has one, is
        not usable as a card number, or the quad is already full.

        This exists because the card list does NOT always arrive up front.
        The daemon greets a UI that connects during the model-load window
        with `not_ready` rather than `hello` (runner/daemon.py's `_hello`) --
        model load stretched to 6.4-9.2s under four-way contention on the
        hardware spike, and the socket accepts long before that -- and
        `hello` is only ever sent at accept time. So the ordinary startup on
        the real booth is: connect, `not_ready`, and then `card_state` and
        `job_start` events naming chips the router has never heard of.
        Without this the booth would show ONE cell for the rest of the day
        while four chips folded behind it. Measured, not imagined: that is
        exactly what the first live run of the quad did.

        APPENDS, and never reorders. Existing slots keep their indices and
        their state, so learning about chip 3 cannot disturb the fold cell 0
        is in the middle of -- which is the whole reason this is a method
        here rather than a rebuild in `ui/app.py`.
        """
        if card is None or isinstance(card, bool) or not isinstance(card, int):
            return None
        if card in self._slot_by_card:
            return None
        if len(self.slots) >= MAX_SLOTS:
            # More chips than cells: fold on all of them, show the first
            # four. The same rule `__init__` applies to an over-long list.
            return None
        index = len(self.slots)
        self.cards.append(card)
        self.slots.append(self._new_slot(card))
        self._slot_by_card[card] = index
        self._showcase_seq.append(None)
        self._showcase_keys.append(None)
        return index

    def slot_for_job(self, job_id):
        """Which cell this job's events belong to, or None if the router
        cannot (or can no longer) route them."""
        return self._job_slots.get(job_id)

    @property
    def tracked_jobs(self):
        """The job ids this router can still route, oldest first.

        Public because boundedness is a behaviour, and a test that asserted
        it against a private field would be testing something adjacent to
        the behaviour rather than the behaviour itself -- this project's
        recurring test defect (docs/followups.md).
        """
        return tuple(self._job_slots)

    # -- events -----------------------------------------------------------

    def on_event(self, event):
        """Route one decoded protocol event and return its slot index, or
        None if it belongs to no cell.

        `job_start` routes by `card` -- it is the only event that carries
        one -- and that is where a job id is bound to a cell. Every later
        event of that fold routes by `job_id` alone. An event for a job the
        router never saw (a frame that beat its own `job_start` through the
        UI's idle queue, or one whose card has no cell) belongs to no slot:
        drawing it into whichever cell happens to be first is worse than
        dropping it.
        """
        etype = event.get("type")

        if etype == "job_start":
            index = self.slot_for_card(event.get("card"))
            if index is None:
                return None
            # Apply first, remember second: `_remember` prunes the map, and
            # pruning must be able to see this job as one a cell is using
            # (running, or deferred behind its dwell) before it decides what
            # is safe to drop.
            self._mutate(index, lambda slot: slot.on_job_start(event))
            self._remember(event.get("job_id"), index)
            return index

        job_id = event.get("job_id")
        if job_id is None:
            # hello, not_ready, card_state: booth-wide, not per-fold. The
            # global machinery in ui/states.py handles those.
            return None

        index = self._job_slots.get(job_id)
        if index is None:
            return None

        if etype == "job_done":
            self._mutate(index, lambda slot: slot.on_job_done(event))
        elif etype == "job_error":
            self._mutate(index, lambda slot: slot.on_job_error(event))
        # stage / frame carry no state transition; they are routed (the
        # caller needs the index) and nothing else.
        return index

    def tick(self, now):
        """Advance every cell's dwell and return the indices of the cells
        whose state actually CHANGED.

        Only the changed ones: this is called at UI frame rate, and a caller
        that redrew, re-cleared or re-captioned all four cells on every tick
        would repaint the whole quad a hundred times a second.
        """
        changed = []
        for index, slot in enumerate(self.slots):
            before = slot.state
            after = self._mutate(index, lambda s: s.tick(now))
            if after != before:
                changed.append(index)
        return changed

    # -- the visitor's pick -----------------------------------------------

    def select_target(self, target_id, now=0.0):
        """A visitor picked a target. Acknowledgeable immediately.

        Nothing about this waits on the daemon answering: the socket may be
        down and all four chips may be mid-fold, and the visitor is standing
        there either way.
        """
        self.selected_target = target_id
        self._selected_at = now

    def release_target(self):
        """Forget the visitor's pick."""
        self.selected_target = None
        self._selected_at = 0.0

    def pick_status(self, now):
        """What the booth should say about the visitor's pick right now:
        `"folding"`, `"queued"`, `"waiting"`, or None if there is no pick.

        See the module docstring for the rule and why it is decided here.
        """
        if self.selected_target is None:
            return None
        if self._slot_holding_target() is not None:
            return "folding"
        # Elapsed since the PICK, not since the epoch: a window compared
        # against the raw clock reading would report "waiting" for every
        # pick made after the booth had been up for PICK_PENDING_WARN_S.
        if now - self._selected_at >= PICK_PENDING_WARN_S:
            return "waiting"
        return "queued"

    # -- the focus --------------------------------------------------------

    @property
    def focus_slot(self):
        """The cell the booth is following. See the module docstring."""
        if not self.slots:
            return 0
        held = self._slot_holding_target()
        if held is not None:
            return held
        best, best_seq = 0, None
        for index, seq in enumerate(self._showcase_seq):
            if seq is not None and (best_seq is None or seq > best_seq):
                best, best_seq = index, seq
        return best

    def _slot_holding_target(self):
        """The cell folding or showcasing the visitor's pick, or None.

        An `idle` cell holds nothing, even though it keeps naming the last
        target it showed -- that is what releases the pick once its fold is
        over, so the focus goes back to following the action instead of
        staying pinned to a finished cell for the rest of the day.
        """
        if self.selected_target is None:
            return None
        for index, slot in enumerate(self.slots):
            if slot.state != SlotPhase.IDLE and slot.target_id == self.selected_target:
                return index
        return None

    # -- bookkeeping ------------------------------------------------------

    def _mutate(self, index, action):
        """Run one state transition on a cell and keep the router's own
        derived state (focus tickets, the pick) in step with it."""
        slot = self.slots[index]
        held_before = self._slot_holding_target() is not None
        result = action(slot)

        # Focus: stamp a ticket whenever this cell enters a showcase.
        key = (slot.state, slot.job_id)
        if slot.state == SlotPhase.SHOWCASE and key != self._showcase_keys[index]:
            self._seq += 1
            self._showcase_seq[index] = self._seq
        self._showcase_keys[index] = key

        # The pick is released once the cell that WAS holding it stops --
        # its dwell expiring, its fold erroring out, or a new fold taking
        # the cell. Otherwise the focus would stay pinned to a finished cell
        # for the rest of the day and the booth would stop following the
        # action. A pick nothing has picked up yet is untouched: it is
        # waiting for the daemon, not finished.
        if held_before and self._slot_holding_target() is None:
            self.release_target()
        return result

    def _remember(self, job_id, index):
        """Bind a job id to a cell, newest last, and keep the map bounded."""
        if job_id is None:
            return
        self._job_slots.pop(job_id, None)
        self._job_slots[job_id] = index
        self._evict()

    def _evict(self):
        """Drop the oldest jobs no cell is still using, down to the limit.

        Never evicts a job a cell is currently showing or has deferred
        behind its dwell -- a long fold on one chip can easily be older than
        JOB_MAP_LIMIT starts on the other three, and evicting it would make
        its own `job_done` unroutable.
        """
        while len(self._job_slots) > JOB_MAP_LIMIT:
            live = self._live_job_ids()
            for job_id in list(self._job_slots):  # oldest first
                if job_id not in live:
                    del self._job_slots[job_id]
                    break
            else:
                # Every tracked job is live. Cannot happen with
                # JOB_MAP_LIMIT >= 2 * MAX_SLOTS, but growing the map is
                # still the right answer if it ever does.
                return

    def _live_job_ids(self):
        live = set()
        for slot in self.slots:
            live.add(slot.job_id)
            live.add(slot.pending_job_id)
        live.discard(None)
        return live
