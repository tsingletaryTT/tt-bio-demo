"""Pins that `ribbon_from_cif` runs off the GTK main loop (Task 2 of Phase
3b) -- see docs/followups.md for the measured cost this exists to hide:
78 ms at 150 residues, 163 ms at 400, 407 ms at 1000, 1221 ms at 3000. Before
this task the whole cost ran inline inside `_handle_event`'s `job_done`
branch, which itself runs via `GLib.idle_add` -- so it froze the spin, the
cross-fade, and (once Task 3 lands) telemetry, for up to 1.2s at exactly the
reveal moment.

The brief's tests are transcribed verbatim below. `_FakeViewer` and
`_join_ribbon_worker` are scaffolding the brief deliberately left unwritten
-- the implementation itself is not given either; both are designed here,
guided by what the tests need.
"""
import threading
import time

from _appfakes import _FakeQuad, _start
from ui.app import DemoApp


class _FakeViewer:
    """Stands in for `ui.viewer.StructureViewer`: counts what would have
    been drawn, without needing a live GL context. Every method the real
    viewer exposes that `_handle_event`/`_drain_pending_ribbon` might call
    on the ribbon-reveal path is present here, so a headless `DemoApp` with
    `_viewer(app) = _FakeViewer()` never hits an AttributeError on that path.
    """

    def __init__(self):
        self.ribbons = 0
        self.cleared = 0
        self.crossfades = 0
        self.last_ribbon = None

    def set_ribbon(self, verts, norms, colors, indices):
        self.ribbons += 1
        self.last_ribbon = (verts, norms, colors, indices)

    def clear_structure(self):
        self.cleared += 1

    def set_held(self, held):
        # Dimming a held structure (ui/viewer.py's `set_held`) is nothing
        # to do with which ribbon lands, but the app reconciles it from
        # `_sync_viewer_hold` on every event -- so this stub exists for the
        # same reason the class's own docstring gives for the others.
        self.held = bool(held)

    def begin_crossfade(self):
        self.crossfades += 1


def _fold_app(cards=(0,), jobs=("j1",)):
    """A headless `DemoApp` with one cell per card and a fake viewer in each.

    A fold now has to START somewhere before it can finish: `job_done`
    carries only a `job_id`, and the router binds a job to a cell at
    `job_start` (the only event carrying a `card`). So every test below opens
    its fold properly rather than posting a `job_done` into the void -- which
    is not scaffolding, it is the arrival order the daemon really produces.
    """
    app = DemoApp(socket_path=None)
    app.quad = _FakeQuad(len(cards), cards=list(cards),
                         viewer_factory=_FakeViewer)
    app.attach_cards(list(cards))
    for index, job_id in enumerate(jobs):
        app._handle_event(_start(job_id, card=cards[index % len(cards)]))
    return app


def _viewer(app, slot=0):
    return app.quad.viewer_for_slot(slot)


def _join_ribbon_worker(app, timeout=5.0):
    """Block until every ribbon-construction worker thread `app` has
    spawned so far has actually finished, so a test can then call
    `app._drain_pending_ribbon()` -- "what the main loop would do" -- on a
    result that is deterministically already there, instead of racing the
    background thread.

    `DemoApp._join_ribbon_workers` is the app's own seam for this (it joins
    EVERY thread it has recorded, not just the newest -- two folds finishing
    back to back put two workers in flight). This wrapper adds the assertion:
    the app logs a hung worker and carries on, which is right for a booth and
    useless for a test.
    """
    assert app._join_ribbon_workers(timeout=timeout), (
        "a ribbon worker did not finish in time -- ribbon_from_cif is "
        "presumably hung")


class _SlowGeometry:
    """Stands in for ribbon_from_cif, recording which thread called it.

    The returned "verts" slot is `cif_path` itself, not a fixed placeholder
    -- this is what lets a test with two folds in flight (see
    test_a_second_fold_supersedes_a_slow_first_one) tell *which* fold's
    result actually reached the viewer, not merely how many did. A fixed
    return value would make the two folds' results indistinguishable, and a
    test that can't tell them apart can't assert which one won a race.
    """

    def __init__(self, delay=0.3):
        self.delay = delay
        self.thread_name = None
        self.calls = 0

    def __call__(self, cif_path, **kw):
        self.thread_name = threading.current_thread().name
        self.calls += 1
        time.sleep(self.delay)
        return (cif_path, "norms", "colors", "indices")


def test_ribbon_construction_does_not_run_on_the_calling_thread(monkeypatch):
    """The main loop must not be the thread that pays the 1.2s cost."""
    from ui import app as mod
    slow = _SlowGeometry()
    monkeypatch.setattr(mod, "ribbon_from_cif", slow)

    app = _fold_app()
    caller = threading.current_thread().name
    app._handle_event({"type": "job_done", "job_id": "j1", "cif_path": "/tmp/x.cif",
                       "wall_s": 5.0, "mean_plddt": 95.0})
    _join_ribbon_worker(app, timeout=5.0)
    assert slow.thread_name is not None, "ribbon_from_cif was never called"
    assert slow.thread_name != caller


def test_the_viewer_is_updated_after_the_work_completes(monkeypatch):
    from ui import app as mod
    monkeypatch.setattr(mod, "ribbon_from_cif", _SlowGeometry(delay=0.05))
    app = _fold_app()
    app._handle_event({"type": "job_done", "job_id": "j1", "cif_path": "/tmp/x.cif",
                       "wall_s": 5.0, "mean_plddt": 95.0})
    _join_ribbon_worker(app, timeout=5.0)
    app._drain_pending_ribbon()            # what the main loop would do
    assert _viewer(app).ribbons == 1


def test_a_geometry_failure_leaves_the_previous_view_intact(monkeypatch):
    from ui import app as mod
    from ui.geometry import GeometryError

    def explode(cif_path, **kw):
        raise GeometryError("bad cif")

    monkeypatch.setattr(mod, "ribbon_from_cif", explode)
    app = _fold_app()
    app._handle_event({"type": "job_done", "job_id": "j1", "cif_path": "/tmp/x.cif",
                       "wall_s": 5.0, "mean_plddt": 95.0})
    _join_ribbon_worker(app, timeout=5.0)
    app._drain_pending_ribbon()
    assert _viewer(app).ribbons == 0
    assert _viewer(app).cleared == 0, "a failed ribbon must not blank the screen"


def test_a_non_geometry_error_also_leaves_the_previous_view_intact(monkeypatch, caplog):
    """Coverage gap closed post-hoc, not in the brief: the constraints say
    "a worker thread that raises must not freeze anything either" -- not
    "a worker thread that raises GeometryError". A real corrupt/truncated
    CIF can make gemmi or numpy raise something ribbon_from_cif never
    wraps in GeometryError.

    caplog is load-bearing here, not decoration: an implementation whose
    worker only catches GeometryError would let a ValueError escape the
    thread's target function entirely. Python's own default thread
    excepthook then swallows it -- no viewer call, no _pending_ribbon
    write, no ui.app log record, just a silently dead thread -- which
    would make `ribbons == 0` and `cleared == 0` hold *by accident*,
    indistinguishable from this app's actual catch-log-and-continue
    behavior on the assertions alone. Only checking that the failure was
    actually logged through this app's own logger tells the two apart.
    """
    import logging
    from ui import app as mod

    def explode(cif_path, **kw):
        raise ValueError("truncated atom record")

    monkeypatch.setattr(mod, "ribbon_from_cif", explode)
    app = _fold_app()
    with caplog.at_level(logging.ERROR, logger="ui.app"):
        app._handle_event({"type": "job_done", "job_id": "j1", "cif_path": "/tmp/x.cif",
                           "wall_s": 5.0, "mean_plddt": 95.0})
        _join_ribbon_worker(app, timeout=5.0)
        app._drain_pending_ribbon()
    assert _viewer(app).ribbons == 0
    assert _viewer(app).cleared == 0
    assert any("truncated atom record" in r.message or
               "truncated atom record" in str(r.exc_info)
               for r in caplog.records), (
        "the failure must be logged through ui.app, not silently dropped "
        "by Python's default thread excepthook")


def test_a_second_fold_supersedes_a_slow_first_one(monkeypatch):
    """Two folds in flight must not race to update the viewer out of order."""
    from ui import app as mod
    monkeypatch.setattr(mod, "ribbon_from_cif", _SlowGeometry(delay=0.2))
    app = _fold_app(jobs=("j1", "j2"))
    app._handle_event({"type": "job_done", "job_id": "j1", "cif_path": "/tmp/a.cif",
                       "wall_s": 5.0, "mean_plddt": 95.0})
    app._handle_event({"type": "job_done", "job_id": "j2", "cif_path": "/tmp/b.cif",
                       "wall_s": 5.0, "mean_plddt": 95.0})
    _join_ribbon_worker(app, timeout=10.0)
    app._drain_pending_ribbon()
    # A bare "<= 1" count can't tell "the newest fold's ribbon landed" apart
    # from "every result was dropped" -- both leave ribbons at 0 or 1. Pin
    # the actual contract: exactly one ribbon lands, and it's identifiably
    # the second (superseding) fold's, not the first's. _SlowGeometry
    # returns cif_path as the "verts" slot precisely so last_ribbon can be
    # checked against which fold produced it.
    assert _viewer(app).ribbons == 1, (
        "exactly one ribbon should land -- zero means every result was "
        "dropped, which is not 'the newest fold won'")
    assert _viewer(app).last_ribbon[0] == "/tmp/b.cif", (
        "the ribbon that landed must be the second (newest) fold's "
        "('/tmp/b.cif'), not the first's ('/tmp/a.cif')")


class _VariableGeometry:
    """Like `_SlowGeometry`, but with a per-CIF delay.

    `_SlowGeometry`'s constant delay is what made the test above unable to
    fail: with both folds equally slow, the newest fold is also the LAST to
    finish, so "the newest generation wins" and "the last writer wins" are
    indistinguishable -- and the whole-branch review confirmed it by
    deleting `self._ribbon_generation += 1` from ui/app.py entirely and
    watching all 459 UI tests stay green.

    Delays are keyed by cif_path rather than by call order: the two workers
    are separate threads, and which one reaches this callable first is not
    something a test should be asserting on by accident.
    """

    def __init__(self, delays):
        self.delays = dict(delays)
        self.calls = 0

    def __call__(self, cif_path, **kw):
        self.calls += 1
        time.sleep(self.delays[cif_path])
        return (cif_path, "norms", "colors", "indices")


def test_a_stale_first_fold_cannot_clobber_a_faster_newer_one(monkeypatch):
    """The ordering the generation counter actually exists for: fold 1 is
    slow (0.60s) and fold 2 is fast (0.02s), so the STALE result is the last
    one to finish.

    Without the generation stamp, the late straggler is simply the most
    recent writer and lands on screen -- the booth would show fold 1's
    structure while fold 2's is the current one. With it, worker 1 sees its
    generation is no longer current and drops its own result.

    Mutation this catches (verified): deleting `self._ribbon_generation += 1`
    from `_spawn_ribbon_worker`.
    """
    from ui import app as mod
    monkeypatch.setattr(mod, "ribbon_from_cif", _VariableGeometry(
        {"/tmp/slow-first.cif": 0.60, "/tmp/fast-second.cif": 0.02}))

    app = _fold_app(jobs=("j1", "j2"))
    app._handle_event({"type": "job_done", "job_id": "j1",
                       "cif_path": "/tmp/slow-first.cif",
                       "wall_s": 5.0, "mean_plddt": 95.0})
    app._handle_event({"type": "job_done", "job_id": "j2",
                       "cif_path": "/tmp/fast-second.cif",
                       "wall_s": 5.0, "mean_plddt": 95.0})
    _join_ribbon_worker(app, timeout=10.0)
    # Drained once, AFTER both workers are done -- so the only thing that can
    # decide which result is on screen is the generation check inside the
    # workers themselves, not the order the main loop happened to run in.
    app._drain_pending_ribbon()

    assert _viewer(app).ribbons == 1, (
        "exactly one ribbon should land -- zero means every result was "
        "dropped, which is not 'the newest fold won'")
    assert _viewer(app).last_ribbon[0] == "/tmp/fast-second.cif", (
        "the superseded first fold clobbered the newest one: it finished "
        "LAST (0.60s vs 0.02s), so nothing but the generation stamp can "
        "stop it reaching the screen")
