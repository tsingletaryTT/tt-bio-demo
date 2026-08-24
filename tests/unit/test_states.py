import pytest

from ui.states import (
    StateMachine, points_are_visible, ribbon_may_be_revealed, showcase_ended,
)


def _sm(**kw):
    return StateMachine(idle_timeout_s=45.0, **kw)


def test_it_starts_in_attract():
    assert _sm().state == "attract"


def test_a_touch_opens_the_gallery():
    sm = _sm()
    assert sm.on_touch() == "gallery"


def test_a_pick_starts_folding_and_remembers_the_target():
    sm = _sm()
    sm.on_touch()
    assert sm.on_pick("hemoglobin") == "folding"
    assert sm.selected_target == "hemoglobin"


def test_job_done_moves_to_showcase():
    sm = _sm()
    sm.on_touch(); sm.on_pick("x")
    assert sm.on_event({"type": "job_done", "job_id": "j1"}) == "showcase"


def test_idle_returns_to_attract_from_every_interactive_state():
    for reach in (lambda s: s.on_touch(),
                  lambda s: (s.on_touch(), s.on_pick("x"))):
        sm = _sm()
        reach(sm)
        sm.tick(now=0.0)
        assert sm.tick(now=46.0) == "attract"


def test_the_idle_timer_resets_on_every_touch():
    """A visitor still interacting must not be timed out mid-browse."""
    sm = _sm()
    sm.on_touch()
    sm.tick(now=0.0)
    sm.tick(now=40.0)
    sm.on_touch()                       # still here
    assert sm.tick(now=50.0) != "attract"


def test_attract_does_not_time_out_to_itself_repeatedly():
    sm = _sm()
    sm.tick(now=0.0)
    assert sm.tick(now=1000.0) == "attract"


def test_a_fold_that_fails_does_not_strand_the_visitor_in_folding():
    sm = _sm()
    sm.on_touch(); sm.on_pick("x")
    assert sm.on_event({"type": "job_error", "job_id": "j1",
                        "target_id": "x", "message": "boom"}) != "folding"


def test_not_ready_overrides_whatever_the_visitor_was_doing():
    sm = _sm()
    sm.on_touch()
    assert sm.on_event({"type": "not_ready", "missing": ["x"]}) == "preparing"


def test_recovery_from_preparing_returns_to_attract_not_to_a_stale_gallery():
    sm = _sm()
    sm.on_touch()
    sm.on_event({"type": "not_ready", "missing": ["x"]})
    assert sm.on_event({"type": "job_start", "job_id": "j", "target_id": "t",
                        "model": "m", "card": 0, "n_residues": 20}) == "attract"


def test_an_attract_loop_fold_does_not_look_like_a_visitor_pick():
    """job_start with no pick outstanding is the attract loop, not a visitor."""
    sm = _sm()
    start = {"type": "job_start", "job_id": "j", "target_id": "t",
             "model": "m", "card": 0, "n_residues": 20}
    assert sm.on_event(start) == "attract"
    assert sm.selected_target is None


# ---------------------------------------------------------------------------
# Showcase dwell -- not in the brief's own list. The controller ruling (Task
# 7's brief) requires a minimum guaranteed viewing period for a finished
# structure, because a measured, hardware-reproduced defect showed only
# ~27% of each fold's collapse ever reaching the screen: fold N's finished
# ribbon routinely arrives AFTER fold N+1's job_start, and an unguarded
# state machine would let that job_start (or a job_error, or a touch) cut
# the showcase short. These tests pin that guarantee directly, using an
# explicit `showcase_dwell_s` rather than the constructor default so the
# boundary is exact and independent of whatever default is chosen.
# ---------------------------------------------------------------------------

def _showcased_sm(dwell_s=2.0):
    """A machine that has just finished a visitor's own fold and is now
    showcasing it, with the dwell clock stamped at t=0."""
    sm = _sm(showcase_dwell_s=dwell_s)
    sm.on_touch()
    sm.on_pick("x")
    sm.on_event({"type": "job_done", "job_id": "j1"})
    assert sm.state == "showcase"
    sm.tick(now=0.0)                    # stamps _showcase_entered_at = 0.0
    assert sm.state == "showcase"
    return sm


def test_showcase_holds_through_a_new_job_start_before_the_dwell_elapses():
    """Mutation this catches: removing the `state == SHOWCASE` guard in the
    job_start handler, so the very ordering bug this dwell exists to fix
    (fold N+1's job_start arriving before fold N's dwell is up) cuts the
    showcase short again."""
    sm = _showcased_sm(dwell_s=2.0)
    next_fold_start = {"type": "job_start", "job_id": "j2", "target_id": "t",
                        "model": "m", "card": 0, "n_residues": 20}
    sm.on_event(next_fold_start)
    assert sm.state == "showcase"
    assert sm.tick(now=1.0) == "showcase"       # 1.0s in, dwell is 2.0s


def test_showcase_holds_through_a_job_error_before_the_dwell_elapses():
    """A trailing job_error for some other in-flight job must not be
    mistaken for the showcased fold itself failing after the fact."""
    sm = _showcased_sm(dwell_s=2.0)
    sm.on_event({"type": "job_error", "job_id": "j2", "target_id": "t",
                 "message": "boom"})
    assert sm.state == "showcase"


def test_showcase_holds_through_a_touch_before_the_dwell_elapses():
    """A visitor tapping the screen mid-showcase must not reopen the
    gallery early -- the dwell is a minimum, not a suggestion."""
    sm = _showcased_sm(dwell_s=2.0)
    sm.on_touch()
    assert sm.state == "showcase"


def test_showcase_is_left_once_the_dwell_elapses():
    """Mutation this catches: a dwell check that never fires (e.g. `tick`
    doing nothing at all in showcase, or the wrong comparison direction),
    which would strand the booth in showcase forever once the guard above
    is in place."""
    sm = _showcased_sm(dwell_s=2.0)
    assert sm.tick(now=1.0) == "showcase"       # not yet
    assert sm.tick(now=2.5) == "attract"        # dwell elapsed


def test_leaving_showcase_clears_the_selected_target():
    """Once the dwell elapses and the booth returns to attract, the prior
    visitor's pick must not linger and be mistaken for a still-outstanding
    one (which would make the next attract-loop job_start look like a
    pick, per the attract-loop-vs-pick distinction above)."""
    sm = _showcased_sm(dwell_s=2.0)
    sm.tick(now=3.0)
    assert sm.state == "attract"
    assert sm.selected_target is None


# ---------------------------------------------------------------------------
# The DEFERRED touch (controller ruling, Task 10).
#
# The dwell guarantee above used to be paid for with a swallowed tap: a touch
# arriving during a showcase set only the idle flag and was then thrown away
# when the dwell expired. Measured in Task 9, the booth sits in `showcase`
# 46-50% of every attract cycle, so roughly HALF of all first taps produced
# nothing a visitor could see -- and a visitor who taps a booth and gets no
# response concludes it is broken and leaves.
#
# The ruling: remember the touch, act on it the instant the dwell expires.
# Both promises are kept -- the finished structure still gets its full,
# uninterrupted dwell (the tests above are unchanged and still green), and
# the visitor's tap is never lost.
# ---------------------------------------------------------------------------

def test_a_touch_during_a_showcase_is_not_lost():
    """The headline of this ruling: tap mid-dwell, and when the dwell
    expires the booth opens the gallery instead of falling back to attract.

    Mutation this catches: `on_touch` not recording `_deferred_touch` at all
    (i.e. the pre-Task-10 behavior, where the touch set only the idle flag)
    -- the booth lands in `attract` and the tap is gone.
    """
    sm = _showcased_sm(dwell_s=2.0)
    assert sm.on_touch() == "showcase"          # still showcasing, as ruled
    assert sm.tick(now=2.5) == "gallery"        # ...and the tap was honored


def test_a_deferred_touch_does_not_cut_the_showcase_short():
    """The other half of the ruling, and the one a careless fix breaks: the
    structure keeps every millisecond of its dwell.

    Mutation this catches: `on_touch` moving straight to GALLERY from
    SHOWCASE (the "obvious" fix), which is exactly the behavior Task 7's
    controller ruling forbids -- the finished structure would vanish under
    the visitor's own tap.
    """
    sm = _showcased_sm(dwell_s=2.0)
    sm.on_touch()
    assert sm.state == "showcase"
    assert sm.tick(now=0.5) == "showcase"
    assert sm.tick(now=1.9999) == "showcase"    # right up to the boundary


def test_the_deferred_touch_takes_effect_exactly_when_the_dwell_expires():
    """Not earlier (that would be cutting the dwell short) and not later
    (that would be a second swallowed tap).

    Mutation this catches: honoring the deferred touch on the tick AFTER the
    one that ends the dwell -- i.e. letting the machine pass through
    `attract` first -- which at a 100ms tick would show the visitor a flash
    of the attract screen before the gallery.
    """
    sm = _showcased_sm(dwell_s=2.0)
    sm.on_touch()
    assert sm.tick(now=1.99) == "showcase"
    assert sm.tick(now=2.00) == "gallery"       # the very tick the dwell ends


def test_a_showcase_nobody_touched_still_returns_to_attract():
    """Mutation this catches: ending every showcase in `gallery` regardless
    of whether anyone touched it, which would leave the unattended attract
    loop parked on a pick grid nobody asked for."""
    sm = _showcased_sm(dwell_s=2.0)
    assert sm.tick(now=2.5) == "attract"


def test_a_deferred_touch_is_consumed_once_not_remembered_forever():
    """A tap honored at the end of one showcase must not re-open the gallery
    at the end of the NEXT one, minutes later, in front of a different
    visitor.

    Mutation this catches: honoring `_deferred_touch` without clearing it.
    """
    sm = _showcased_sm(dwell_s=2.0)
    sm.on_touch()
    assert sm.tick(now=2.5) == "gallery"
    # The visitor walks off; the gallery times out back to attract.
    sm.tick(now=2.6)                            # stamps the idle baseline
    assert sm.tick(now=60.0) == "attract"
    # The attract loop finishes another fold, untouched by anyone.
    sm.on_event({"type": "job_done", "job_id": "j2"})
    sm.tick(now=61.0)
    assert sm.tick(now=64.0) == "attract"


def test_a_degrading_daemon_discards_a_deferred_touch():
    """`not_ready` is the one thing that overrides a showcase outright (see
    the section below). The tap that showcase was holding dies with it: by
    the time the daemon recovers, the visitor who tapped is long gone, and
    `preparing` releases to `attract` precisely so no pre-degrade screen is
    resurrected.

    Mutation this catches: leaving `_deferred_touch` set in the `not_ready`
    branch, which makes the NEXT completed fold's showcase end in a gallery
    opened by a tap from before the outage.
    """
    sm = _showcased_sm(dwell_s=2.0)
    sm.on_touch()
    assert sm.on_event({"type": "not_ready", "missing": ["weights"]}) == "preparing"
    assert sm.on_event({"type": "job_start", "job_id": "j2"}) == "attract"
    sm.on_event({"type": "job_done", "job_id": "j2"})
    sm.tick(now=10.0)
    assert sm.tick(now=13.0) == "attract"


def test_the_idle_clock_of_a_deferred_gallery_starts_when_it_opens():
    """The gallery a deferred touch opens gets a FULL idle window, measured
    from the moment it actually appears -- not from the tap that was made up
    to a whole dwell earlier.

    Mutation this catches: stamping the idle baseline at the touch (or
    leaving a baseline stamped from before the showcase), which would cut
    this visitor's reading time short by however long the dwell ran.
    """
    sm = _showcased_sm(dwell_s=2.0)
    sm.on_touch()
    assert sm.tick(now=2.0) == "gallery"        # opened at t=2.0
    assert sm.tick(now=46.9) == "gallery"       # 44.9s later: still open
    assert sm.tick(now=47.0) == "attract"       # 45.0s later: timed out


# ---------------------------------------------------------------------------
# CONTROLLER RULING (fix round 1, Task 7 review): not_ready is the one
# deliberate exception to "nothing but tick(now) can end a showcase" --
# a daemon that just told us it cannot fold must surface that immediately,
# not after up to showcase_dwell_s of admiring a finished structure. This
# was previously true in the code but false in the docstring, and pinned by
# nothing; this test locks the chosen behavior in.
# ---------------------------------------------------------------------------

def test_not_ready_ends_a_showcase_immediately_even_mid_dwell():
    """Mutation this catches: guarding the not_ready handler to skip while
    `state == "showcase"` (i.e. resurrecting the discarded "let the dwell
    finish first" alternative) -- that would leave state at "showcase"
    here instead of "preparing"."""
    sm = _showcased_sm(dwell_s=3.0)
    assert sm.on_event({"type": "not_ready", "missing": ["x"]}) == "preparing"


# ---------------------------------------------------------------------------
# Robustness paths named by review fix round 1: on_pick/job_error from
# states where they are not the "expected" caller, job_done arriving while
# preparing, and tick(now) fed a non-monotonic clock. None of these are
# hypothetical -- a GTK caller, a racy daemon, or a wall-clock adjustment can
# all produce them, and "verified by hand, not tested" is exactly the gap
# that let nineteen defects through in the previous phase.
# ---------------------------------------------------------------------------

def test_on_pick_from_attract_is_ignored():
    """A pick with no gallery open is not a thing the real UI can produce,
    but a caller bug (or a race between on_touch and on_pick) could still
    fire one. Mutation this catches: dropping the `state == GALLERY` guard
    in on_pick, so a pick from attract silently starts a fold with no
    visible gallery behind it."""
    sm = _sm()
    assert sm.on_pick("x") == "attract"
    assert sm.selected_target is None


def test_on_pick_from_folding_does_not_restart_or_reassign():
    """A double-tap on a gallery card, arriving after the first pick already
    moved the booth to folding, must not reassign the in-flight target.
    Mutation this catches: on_pick accepting from any state instead of only
    `gallery`, which would let a second, different target_id overwrite
    selected_target mid-fold."""
    sm = _sm()
    sm.on_touch()
    sm.on_pick("first")
    assert sm.on_pick("second") == "folding"
    assert sm.selected_target == "first"


def test_job_error_from_attract_does_not_change_state():
    """An attract-loop fold failing (no visitor pick was ever made) has
    nothing to unstick. Mutation this catches: job_error unconditionally
    moving to `gallery` regardless of current state, which would yank the
    ambient attract screen out from under nobody's request."""
    sm = _sm()
    assert sm.on_event({"type": "job_error", "job_id": "j", "target_id": "t",
                        "message": "boom"}) == "attract"


def test_job_error_while_preparing_does_not_recover():
    """A job_error for work that was already in flight when not_ready fired
    must not be mistaken for the daemon recovering -- only job_start does
    that (see test_recovery_from_preparing_returns_to_attract_...).
    Mutation this catches: treating job_error the same as job_start inside
    the preparing branch (a plausible-looking "any daemon activity means
    it's back" shortcut), which would flip this to "attract" instead of
    leaving the not_ready message on screen."""
    sm = _sm()
    sm.on_event({"type": "not_ready", "missing": ["x"]})
    assert sm.on_event({"type": "job_error", "job_id": "j", "target_id": "t",
                        "message": "boom"}) == "preparing"


def test_job_done_while_preparing_does_not_leak_into_showcase():
    """A job_done for work that was already in flight when not_ready fired
    must not paper over the degrade message with a showcase. Mutation this
    catches: removing the preparing branch's unconditional early return for
    non-job_start events, letting a stray job_done fall through to its own
    handler below and flip the state to "showcase" mid-degrade."""
    sm = _sm()
    sm.on_event({"type": "not_ready", "missing": ["x"]})
    assert sm.on_event({"type": "job_done", "job_id": "j1"}) == "preparing"


def test_a_backwards_clock_does_not_prematurely_end_a_showcase():
    """tick(now) takes the clock as a plain argument; a caller will
    eventually hand it a non-monotonic value (wall-clock adjustment, NTP
    step, a restarted GLib source). Mutation this catches: computing
    elapsed dwell time as `abs(now - entered_at)` instead of
    `now - entered_at` (a plausible-looking "defensive" fix for backwards
    clocks that actually makes them WORSE) -- abs() of a large negative
    jump reads as dwell-elapsed and ends the showcase instantly instead of
    just not advancing it."""
    sm = _showcased_sm(dwell_s=3.0)          # entered_at stamped at now=0.0
    assert sm.tick(now=-100.0) == "showcase"


def test_a_backwards_clock_does_not_prematurely_trigger_the_idle_timeout():
    """Same non-monotonic-clock robustness, for the idle timer. Mutation
    this catches: the same `abs()` mistake applied to the idle baseline
    comparison, which would time out to attract on a large backwards jump
    instead of simply not accumulating negative elapsed time."""
    sm = _sm()
    sm.on_touch()
    sm.tick(now=100.0)                        # baseline stamped at 100.0
    assert sm.tick(now=-1000.0) == "gallery"


def test_repeated_identical_tick_calls_do_not_advance_the_showcase_dwell():
    """A GLib source firing twice for the same clock reading (or any caller
    calling tick() more than once per frame) must be idempotent -- it must
    not look like time passed twice. Mutation this catches: re-stamping
    `_showcase_entered_at` on every tick instead of only the first one
    after job_done, which would make the dwell perpetually "just started"
    and never elapse."""
    sm = _showcased_sm(dwell_s=2.0)            # entered_at stamped at now=0.0
    for _ in range(5):
        assert sm.tick(now=0.0) == "showcase"
    assert sm.tick(now=2.0) == "attract"


# ---------------------------------------------------------------------------
# The reveal hook, and the three predicates the GTK wiring layer reads.
# ---------------------------------------------------------------------------

def test_the_dwell_restarts_when_the_structure_is_actually_revealed():
    """`job_done` is when the daemon finished; the reveal is when a visitor
    can see anything, and the gap between them is a ribbon build (up to
    ~1.2s) plus a 0.8s cross-fade. Mutation this catches:
    `on_structure_revealed` leaving `_showcase_entered_at` alone (a
    structure whose build ate the whole dwell would flash past)."""
    sm = _sm(showcase_dwell_s=3.0)
    sm.on_event({"type": "job_done", "job_id": "j1"})
    sm.tick(now=0.0)                       # dwell would have started here
    assert sm.tick(now=2.9) == "showcase"
    sm.on_structure_revealed()             # ...but this is when it was seen
    sm.tick(now=2.9)                       # re-stamped at 2.9
    assert sm.tick(now=5.5) == "showcase"
    assert sm.tick(now=5.95) == "attract"


def test_revealing_a_structure_outside_a_showcase_changes_nothing():
    """A ribbon that lands after its own dwell expired must not silently
    re-enter a showcase the booth already left -- by then the next fold's
    live diffusion is on screen. Mutation this catches: dropping the
    `state == SHOWCASE` guard in on_structure_revealed."""
    sm = _sm()
    assert sm.on_structure_revealed() == "attract"
    assert sm.state == "attract"


def test_points_are_visible_everywhere_except_a_showcase():
    """Mutation this catches: inverting the predicate, or narrowing it to
    `folding` (which would blank the ambient attract loop's own folds --
    the attract loop is where the measured defect was reproduced)."""
    for state in ("attract", "gallery", "folding", "preparing"):
        assert points_are_visible(state) is True
    assert points_are_visible("showcase") is False


def test_a_ribbon_may_only_be_revealed_while_showcasing():
    """Mutation this catches: returning True unconditionally, which is
    exactly the pre-task behaviour -- a finished ribbon thrown over
    whatever happened to be on screen."""
    assert ribbon_may_be_revealed("showcase") is True
    for state in ("attract", "gallery", "folding", "preparing"):
        assert ribbon_may_be_revealed(state) is False


def test_showcase_ended_is_an_edge_not_a_level():
    """Mutation this catches: `current != "showcase"` alone (which fires on
    every single tick of every non-showcase state, so the deferred clear
    would run continuously and blank the booth)."""
    assert showcase_ended("showcase", "attract") is True
    assert showcase_ended("showcase", "preparing") is True
    assert showcase_ended("showcase", "showcase") is False
    assert showcase_ended("attract", "attract") is False
    assert showcase_ended("folding", "attract") is False


def test_the_attract_loops_own_job_done_does_not_close_an_open_gallery():
    """Reproduced on screen, not hypothesised (Task 9): the daemon folds
    continuously, so a `job_done` lands every few seconds regardless of
    what the visitor is doing. With `job_done` firing from any state, an
    open gallery was replaced by a showcase about two seconds after a
    visitor touched the booth -- before they could read the cards. Mutation
    this catches: removing the `gallery` guard from the job_done branch."""
    sm = _sm()
    sm.on_touch()
    assert sm.on_event({"type": "job_done", "job_id": "attract-loop"}) == "gallery"
    # ...and the visitor's own pick still leads to a showcase as normal.
    sm.on_pick("trpcage")
    assert sm.on_event({"type": "job_done", "job_id": "theirs"}) == "showcase"


# ── the per-target showcase dwell ───────────────────────────────────────────
#
# A finished structure holds the screen for as long as the INCOMING fold can
# afford, because holding it is paid for by suppressing that fold's opening
# frames (the daemon starts it before this one is even drawn). So the dwell is
# a maximum, capped by the incoming target's measured `first_frame_s`.
#
# The measurement that forced this: a flat 7.0s dwell put Trp-cage's visible
# collapse at 0/30 frames, against the 40% floor
# test_live_diffusion_is_visible_for_a_substantial_share_of_each_cycle holds.

def _showcase_then_next(sm, next_target_id):
    """Finish a fold, then tell the machine what is coming. Returns the dwell
    the resulting showcase will actually serve."""
    sm.on_event({"type": "job_done", "job_id": "n"})
    assert sm.state == "showcase"
    sm.on_event({"type": "job_start", "job_id": "n+1",
                 "target_id": next_target_id})
    return sm.effective_dwell_s


def test_a_slow_starting_incoming_target_gets_the_full_dwell():
    """Albumin does not reach its first coordinates for 82.7s, so a 7s hold
    in front of it costs its collapse nothing."""
    sm = _sm(showcase_dwell_s=7.0, dwell_floor_s=2.0,
             dwell_caps={"hsa": 82.7, "trpcage": 1.9})
    assert _showcase_then_next(sm, "hsa") == 7.0


def test_a_fast_starting_incoming_target_is_capped_to_the_floor():
    """Trp-cage reaches coordinates in 1.9s. A 7s hold in front of it would
    suppress its entire collapse, which is the defect this cap exists for."""
    sm = _sm(showcase_dwell_s=7.0, dwell_floor_s=2.0,
             dwell_caps={"hsa": 82.7, "trpcage": 1.9})
    assert _showcase_then_next(sm, "trpcage") == 2.0


def test_a_mid_range_target_gets_its_own_measured_number():
    """Not just two buckets: the cap is the measurement, clamped."""
    sm = _sm(showcase_dwell_s=7.0, dwell_floor_s=2.0,
             dwell_caps={"fkbp12": 5.3})
    assert _showcase_then_next(sm, "fkbp12") == pytest.approx(5.3)


def test_an_unmeasured_incoming_target_gets_the_floor_not_the_maximum():
    """A long hold is a bet that the incoming fold can afford it, and only a
    measurement settles that. With no number the booth declines the bet --
    which is also what makes this mechanism inert on a playlist that measures
    nothing."""
    sm = _sm(showcase_dwell_s=7.0, dwell_floor_s=2.0, dwell_caps={})
    assert _showcase_then_next(sm, "something-nobody-timed") == 2.0


def test_a_dwell_already_being_served_is_never_widened():
    """Two job_starts, the second slower-starting than the first. The hold
    must not GROW under a visitor who is already looking at it -- only the
    narrowest answer seen so far may apply."""
    sm = _sm(showcase_dwell_s=7.0, dwell_floor_s=2.0,
             dwell_caps={"trpcage": 1.9, "hsa": 82.7})
    sm.on_event({"type": "job_done", "job_id": "n"})
    sm.on_event({"type": "job_start", "job_id": "a", "target_id": "trpcage"})
    assert sm.effective_dwell_s == 2.0
    sm.on_event({"type": "job_start", "job_id": "b", "target_id": "hsa"})
    assert sm.effective_dwell_s == 2.0, "a served dwell was widened"


def test_a_narrow_dwell_does_not_leak_into_the_next_showcase():
    """One short target in the rotation must not pin every later hold to its
    cap. Each new showcase starts from the maximum again."""
    sm = _sm(showcase_dwell_s=7.0, dwell_floor_s=2.0,
             dwell_caps={"trpcage": 1.9, "hsa": 82.7})
    sm.on_event({"type": "job_done", "job_id": "n"})
    sm.on_event({"type": "job_start", "job_id": "a", "target_id": "trpcage"})
    assert sm.effective_dwell_s == 2.0
    # That showcase ends; a later fold finishes and albumin is next.
    sm.tick(0.0)
    sm.tick(100.0)
    sm.on_event({"type": "job_done", "job_id": "a"})
    sm.on_event({"type": "job_start", "job_id": "b", "target_id": "hsa"})
    assert sm.effective_dwell_s == 7.0, "the narrowed dwell leaked forward"
