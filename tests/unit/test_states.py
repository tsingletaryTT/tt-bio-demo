import pytest

from ui.states import StateMachine


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
