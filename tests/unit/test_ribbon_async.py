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

from ui.app import DemoApp


class _FakeViewer:
    """Stands in for `ui.viewer.StructureViewer`: counts what would have
    been drawn, without needing a live GL context. Every method the real
    viewer exposes that `_handle_event`/`_drain_pending_ribbon` might call
    on the ribbon-reveal path is present here, so a headless `DemoApp` with
    `app.viewer = _FakeViewer()` never hits an AttributeError on that path.
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

    def begin_crossfade(self):
        self.crossfades += 1


def _join_ribbon_worker(app, timeout=5.0):
    """Block until every ribbon-construction worker thread `app` has
    spawned so far has actually finished, so a test can then call
    `app._drain_pending_ribbon()` -- "what the main loop would do" -- on a
    result that is deterministically already there, instead of racing the
    background thread.

    Joins every thread `DemoApp` has ever recorded (not just the most
    recent one): test 4 fires two `job_done` events back to back, so two
    workers are in flight and both must be allowed to finish before the
    test inspects the outcome, regardless of which one the app considers
    current.
    """
    for worker in list(app._ribbon_threads):
        worker.join(timeout=timeout)
        assert not worker.is_alive(), (
            f"ribbon worker {worker.name!r} did not finish within "
            f"{timeout}s -- ribbon_from_cif is presumably hung")


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

    app = DemoApp(socket_path=None)
    app.viewer = _FakeViewer()
    caller = threading.current_thread().name
    app._handle_event({"type": "job_done", "job_id": "j1", "cif_path": "/tmp/x.cif",
                       "wall_s": 5.0, "mean_plddt": 95.0})
    _join_ribbon_worker(app, timeout=5.0)
    assert slow.thread_name is not None, "ribbon_from_cif was never called"
    assert slow.thread_name != caller


def test_the_viewer_is_updated_after_the_work_completes(monkeypatch):
    from ui import app as mod
    monkeypatch.setattr(mod, "ribbon_from_cif", _SlowGeometry(delay=0.05))
    app = DemoApp(socket_path=None)
    app.viewer = _FakeViewer()
    app._handle_event({"type": "job_done", "job_id": "j1", "cif_path": "/tmp/x.cif",
                       "wall_s": 5.0, "mean_plddt": 95.0})
    _join_ribbon_worker(app, timeout=5.0)
    app._drain_pending_ribbon()            # what the main loop would do
    assert app.viewer.ribbons == 1


def test_a_geometry_failure_leaves_the_previous_view_intact(monkeypatch):
    from ui import app as mod
    from ui.geometry import GeometryError

    def explode(cif_path, **kw):
        raise GeometryError("bad cif")

    monkeypatch.setattr(mod, "ribbon_from_cif", explode)
    app = DemoApp(socket_path=None)
    app.viewer = _FakeViewer()
    app._handle_event({"type": "job_done", "job_id": "j1", "cif_path": "/tmp/x.cif",
                       "wall_s": 5.0, "mean_plddt": 95.0})
    _join_ribbon_worker(app, timeout=5.0)
    app._drain_pending_ribbon()
    assert app.viewer.ribbons == 0
    assert app.viewer.cleared == 0, "a failed ribbon must not blank the screen"


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
    app = DemoApp(socket_path=None)
    app.viewer = _FakeViewer()
    with caplog.at_level(logging.ERROR, logger="ui.app"):
        app._handle_event({"type": "job_done", "job_id": "j1", "cif_path": "/tmp/x.cif",
                           "wall_s": 5.0, "mean_plddt": 95.0})
        _join_ribbon_worker(app, timeout=5.0)
        app._drain_pending_ribbon()
    assert app.viewer.ribbons == 0
    assert app.viewer.cleared == 0
    assert any("truncated atom record" in r.message or
               "truncated atom record" in str(r.exc_info)
               for r in caplog.records), (
        "the failure must be logged through ui.app, not silently dropped "
        "by Python's default thread excepthook")


def test_a_second_fold_supersedes_a_slow_first_one(monkeypatch):
    """Two folds in flight must not race to update the viewer out of order."""
    from ui import app as mod
    monkeypatch.setattr(mod, "ribbon_from_cif", _SlowGeometry(delay=0.2))
    app = DemoApp(socket_path=None)
    app.viewer = _FakeViewer()
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
    assert app.viewer.ribbons == 1, (
        "exactly one ribbon should land -- zero means every result was "
        "dropped, which is not 'the newest fold won'")
    assert app.viewer.last_ribbon[0] == "/tmp/b.cif", (
        "the ribbon that landed must be the second (newest) fold's "
        "('/tmp/b.cif'), not the first's ('/tmp/a.cif')")
