"""Task 11: wiring the affinity-questions feature into `ui/app.py`.

Four things this file pins, each already built and tested in isolation by an
earlier task and joined here for the first time:

1. `hello`'s `qa_capable` field reaches the rail panel (`ui.questions.
   QuestionQueuePanel`) and the gallery's ask strip -- both start hidden and
   only one event ever un-hides them.
2. `answer_start`/`answer_done`/`answer_error` forward to the rail panel,
   exactly the way `stage`/`job_done` already forward to the pipeline panel.
3. `answer_done` re-derives the pocket highlight (`ui.pocket.pocket_residues`
   -> `ui.cartoon.cartoon_from_cif`'s `highlight_residues`) ONLY when the
   answered target is still the structure a cell is showing, and drops the
   result harmlessly if the cell has since moved on to a newer fold -- the
   same race `_apply_ribbon` already guards against, at a different call
   site.
4. `_on_ask` (a visitor's tap) and `_ask_next_question` (the attract loop)
   both send `question`, never `pick` -- the daemon's own `_accept_question`
   already folds the target itself (runner/daemon.py).

Everything here runs headless, the same way test_app_pick.py and
test_app_quad.py do: `_appfakes._app` builds a `DemoApp` with a fake quad and
no live socket, and every event is delivered through `_handle_event` exactly
as `EventClient`'s `GLib.idle_add` dispatch would.
"""

import threading
import time

import pytest

from _appfakes import _app, _frame, _hello, _start
from ui.app import DemoApp
from ui.attract import ASK_QUESTION
from ui.playlist import Question


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _RecordingQuestionPanel:
    """Stands in for `ui.questions.QuestionQueuePanel`, recording every call.

    `boom=True` makes every call raise -- the same shape
    `test_app_wiring.RecordingPanel` uses -- so "a panel that explodes must
    not cost the whole event" is directly testable with no real widget.
    """

    def __init__(self, boom=False):
        self.boom = boom
        self.qa_capable_calls = []
        self.starts = []
        self.dones = []
        self.errors = []

    def set_qa_capable(self, capable):
        if self.boom:
            raise RuntimeError("question panel exploded")
        self.qa_capable_calls.append(capable)

    def on_answer_start(self, question_id, target_id):
        if self.boom:
            raise RuntimeError("question panel exploded")
        self.starts.append((question_id, target_id))

    def on_answer_done(self, question_id, target_id, score):
        if self.boom:
            raise RuntimeError("question panel exploded")
        self.dones.append((question_id, target_id, score))

    def on_answer_error(self, question_id, target_id):
        if self.boom:
            raise RuntimeError("question panel exploded")
        self.errors.append((question_id, target_id))


class _RecordingGallery:
    """Stands in for `ui.gallery.Gallery`'s one method this file calls."""

    def __init__(self):
        self.ask_capable_calls = []

    def set_ask_capable(self, capable):
        self.ask_capable_calls.append(capable)


class _RecordingClient:
    """Stands in for `ui.client.EventClient`, recording both message types
    this feature can send -- mirroring test_app_pick.py's own
    `_RecordingClient` exactly, with `send_question` added."""

    def __init__(self, ok=True):
        self.picks = []
        self.questions = []
        self.ok = ok
        self.state = "connected"

    def send_pick(self, target_id):
        self.picks.append(target_id)
        return self.ok

    def send_question(self, question_id, target_id):
        self.questions.append((question_id, target_id))
        return self.ok


def _question(id="q1", target_id="t", question="Does the ligand bind here?",
              ligand_name="LIG", expected_s=None):
    return Question(id=id, target_id=target_id, question=question,
                    ligand_name=ligand_name, expected_s=expected_s)


def _questions_app(cards=(0,), client=None, questions=None, qa_capable=True):
    """A headless `DemoApp` wired up for the affinity-questions surfaces."""
    app = _app(cards)
    app._client = client if client is not None else _RecordingClient()
    app.question_panel = _RecordingQuestionPanel()
    app.gallery = _RecordingGallery()
    app.questions = list(questions) if questions is not None else []
    app.qa_capable = qa_capable
    return app


class _SlowMesh:
    """Stands in for `ui.app.structure_mesh`, recording every call's
    `highlight_residues` and pausing so a test can mutate app state BETWEEN
    a highlight worker starting and finishing -- the same shape
    test_ribbon_async.py's `_SlowGeometry` uses, for the identical reason:
    proving the re-check in `_apply_highlight` actually re-checks."""

    def __init__(self, delay=0.15, verts_tag="mesh"):
        self.delay = delay
        self.verts_tag = verts_tag
        self.calls = []

    def __call__(self, cif_path, highlight_residues=None):
        self.calls.append(highlight_residues)
        time.sleep(self.delay)
        tag = "highlighted" if highlight_residues else "plain"
        return ([f"{self.verts_tag}:{tag}"], ["n"], ["c"], [0])


def _fold_and_finish(app, *, slot=0, card=0, job_id="j1", target_id="fkbp12",
                     cif_path="tests/fixtures/structures/pocket_with_ligand.cif"):
    """Drive one fold from `job_start` through a landed ribbon, so
    `view.shown_target_id`/`has_structure`/`shown_cif_path` are all set the
    way a real booth sets them -- the precondition every highlight test
    needs. Returns the cell's `FakeViewer` (via `app.quad.viewer_for_slot`).
    """
    app._handle_event(_start(job_id, card=card, target_id=target_id))
    # A frame arrives on the socket thread (buffered) and is drawn by the
    # 33ms drain -- never routed through `_handle_event` (see ui/client.py's
    # own `_on_event`/`_handle_event` split, and tests/unit/test_app_quad.py's
    # `_deliver` helper, mirrored here).
    app._on_event(_frame(job_id, n_atoms=4, spread=1.0))
    app._drain_frames()
    app._handle_event({"type": "job_done", "job_id": job_id,
                       "cif_path": cif_path, "wall_s": 1.0,
                       "mean_plddt": 90.0})
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()
    return app.quad.viewer_for_slot(slot)


# ---------------------------------------------------------------------------
# 1. hello's qa_capable reaches both surfaces.
# ---------------------------------------------------------------------------

def test_hello_with_qa_capable_shows_the_panel_and_the_ask_strip():
    """Real questions loaded (`_questions_app`'s own default is an EMPTY
    list -- see the sibling test below for why that case must NOT show the
    panel), so this is the ordinary case: a daemon that reserved a chip AND
    a booth that actually has something to ask it."""
    app = _questions_app(qa_capable=False, questions=[_question()])
    app._handle_event(_hello(cards=[0], qa_capable=True))
    assert app.qa_capable is True
    assert app.question_panel.qa_capable_calls[-1] is True
    assert app.gallery.ask_capable_calls[-1] is True


def test_hello_with_qa_capable_false_hides_both():
    app = _questions_app(qa_capable=True, questions=[_question()])
    app._handle_event(_hello(cards=[0], qa_capable=False))
    assert app.qa_capable is False
    assert app.question_panel.qa_capable_calls[-1] is False
    assert app.gallery.ask_capable_calls[-1] is False


def test_a_daemon_with_no_qa_worker_never_shows_the_capability():
    """The ordinary case for every booth without a second reserved chip:
    `qa_capable` is simply absent or False, and neither surface should ever
    have been told otherwise."""
    app = _questions_app(qa_capable=False, questions=[_question()])
    app._handle_event(_hello(cards=[0]))  # qa_capable=False by default
    assert True not in app.question_panel.qa_capable_calls
    assert True not in app.gallery.ask_capable_calls


# ---------------------------------------------------------------------------
# 1b. qa_capable alone is not enough (PR review, Copilot): a daemon can
# reserve a chip for Q&A while THIS process loaded no questions to ask with
# it (a missing/malformed questions.yaml, or a custom --playlist --
# ui.app.DemoApp._load_questions's own docstring). The rail panel and the
# spotlight cell must not show up advertising a capability with nothing
# behind it, mirroring the gate `Gallery.set_ask_capable` already applies
# to the ask strip.
# ---------------------------------------------------------------------------

def test_qa_capable_with_no_questions_loaded_never_shows_the_rail_panel():
    app = _questions_app(qa_capable=False, questions=[])
    app._handle_event(_hello(cards=[0], qa_capable=True))
    assert app.qa_capable is True, (
        "the daemon's own capability is still recorded truthfully")
    assert True not in app.question_panel.qa_capable_calls, (
        "no questions were loaded, so nothing must ever tell the rail "
        "panel it is capable -- an empty 'No questions queued' panel "
        "would imply a capability this run cannot deliver")


# ---------------------------------------------------------------------------
# 2. answer_* forward to the rail panel.
# ---------------------------------------------------------------------------

def test_answer_start_forwards_to_the_panel():
    app = _questions_app()
    app._handle_event({"type": "answer_start", "question_id": "q1",
                       "target_id": "fkbp12"})
    assert app.question_panel.starts == [("q1", "fkbp12")]


def test_answer_done_forwards_the_score_to_the_panel():
    app = _questions_app()
    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "fkbp12", "score": 0.87})
    assert app.question_panel.dones == [("q1", "fkbp12", 0.87)]


def test_answer_error_forwards_to_the_panel():
    app = _questions_app()
    app._handle_event({"type": "answer_error", "question_id": "q1",
                       "target_id": "fkbp12"})
    assert app.question_panel.errors == [("q1", "fkbp12")]


def test_an_exploding_question_panel_does_not_break_handle_event():
    """Mirrors test_app_wiring.py's `boom=True` panel tests: a panel that
    raises must not turn a real event into a dropped/malformed one."""
    app = _questions_app()
    app.question_panel = _RecordingQuestionPanel(boom=True)
    app._handle_event({"type": "answer_start", "question_id": "q1",
                       "target_id": "fkbp12"})  # must not raise
    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "fkbp12", "score": 0.5})  # must not raise
    app._handle_event({"type": "answer_error", "question_id": "q1",
                       "target_id": "fkbp12"})  # must not raise


def test_an_exploding_highlight_rebuild_does_not_cost_the_panel_update(
        monkeypatch, caplog):
    """Item 13 of the deferred-nits batch: `_handle_answer_event`'s
    docstring claims the panel forward and the highlight rebuild are two
    INDEPENDENT opportunities to fail. Before this fix that was only true
    of the panel forward -- an exception from `_maybe_highlight_pocket`
    (called bare, no try/except of its own) propagated out of
    `_handle_answer_event` and was caught only by `_handle_event`'s outer
    handler, which logs the whole event as "dropping malformed", even
    though the panel update just above it had already genuinely succeeded.

    This pins the now-true claim: the panel update lands AND no exception
    escapes AND the outer "dropping malformed" line never fires -- an
    exploding highlight rebuild is invisible to everything except its own
    log line.
    """
    import logging

    app = _questions_app()
    monkeypatch.setattr(app, "_maybe_highlight_pocket",
                        lambda target_id: (_ for _ in ()).throw(
                            RuntimeError("boom")))

    with caplog.at_level(logging.WARNING, logger="ui.app"):
        app._handle_event({"type": "answer_done", "question_id": "q1",
                           "target_id": "fkbp12", "score": 0.87})  # must not raise

    assert app.question_panel.dones == [("q1", "fkbp12", 0.87)], \
        "the panel update must have landed despite the highlight exploding"
    assert not any("dropping malformed" in r.message for r in caplog.records), \
        "a failure isolated to the highlight rebuild must not read as the " \
        "whole event being malformed"


def test_answer_events_do_not_land_in_the_unhandled_branch(caplog):
    """A regression this whole branch would otherwise reintroduce quietly:
    answer_start/_done/_error falling through `_handle_event`'s final
    `else` and logging "unhandled event type" for every single question."""
    import logging
    app = _questions_app()
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        app._handle_event({"type": "answer_start", "question_id": "q1",
                           "target_id": "fkbp12"})
    assert not any("unhandled event type" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. the pocket highlight.
# ---------------------------------------------------------------------------

def test_answer_done_for_the_shown_target_rebuilds_with_a_highlight(monkeypatch):
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    mesh = _SlowMesh(delay=0.0)
    monkeypatch.setattr(mod, "structure_mesh", mesh)

    app = _questions_app()
    viewer = _fold_and_finish(app, target_id="fkbp12")
    # The ordinary ribbon build never asks for a highlight.
    assert mesh.calls == [None]
    assert viewer.shown == ("ribbon", ["mesh:plain"])

    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "fkbp12", "score": 0.9})
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_highlight()

    assert mesh.calls[-1] == {("A", 1)}, "highlight_residues never reached structure_mesh"
    assert viewer.shown == ("ribbon", ["mesh:highlighted"])


def test_answer_done_arriving_before_the_ribbon_still_gets_highlighted(monkeypatch):
    """The CRITICAL defect (final whole-branch review): in production,
    nesso1 (~8-12s, on its own dedicated, idle chip) usually finishes
    scoring BEFORE the fold itself does -- a question's fold pick queues
    behind whatever chip is already folding at VISITOR_PRIORITY, and the
    fold itself then takes 9.7-17.4s for the two slower question targets
    (DHFR/trypsin). So `answer_done` routinely arrives while this cell has
    no ribbon of its own AT ALL yet -- `view.shown_cif_path` is still None
    -- which is exactly the ordering Task 12's mock-stream fixture never
    produced (it happened to order `job_done` before the answer, the one
    ordering that happens to work with the old, purely reactive code).

    Before the fix, `_maybe_highlight_pocket`'s guard
    (`view.shown_cif_path` truthy) fails here, the answer is dropped, and
    nothing ever re-checks once the ribbon actually does arrive -- so the
    highlight silently never appears, on essentially every question asked
    about those two targets.
    """
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    mesh = _SlowMesh(delay=0.0)
    monkeypatch.setattr(mod, "structure_mesh", mesh)

    app = _questions_app()
    app._handle_event(_start("j1", card=0, target_id="dhfr"))
    app._on_event(_frame("j1", n_atoms=4, spread=1.0))
    app._drain_frames()

    # The answer lands well before this fold's own job_done/ribbon -- there
    # is no ribbon on this cell at all yet (shown_cif_path is None).
    view = app._slot_view(0)
    assert view.shown_cif_path is None
    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "dhfr", "score": 0.9})
    app._drain_pending_highlight()  # nothing to apply yet -- must not raise
    assert mesh.calls == [], "no ribbon exists yet; nothing should be built"

    app._handle_event({"type": "job_done", "job_id": "j1",
                       "cif_path": "/fold1/dhfr.cif", "wall_s": 1.0,
                       "mean_plddt": 90.0})
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()

    viewer = app.quad.viewer_for_slot(0)
    assert mesh.calls == [{("A", 1)}], (
        "the ribbon build never received highlight_residues even though "
        "an answer for this exact target had already arrived")
    assert viewer.shown == ("ribbon", ["mesh:highlighted"]), (
        "an answer that beat its own fold's ribbon to arrive was never "
        "applied once the ribbon was actually built")


def test_a_ribbon_built_plain_still_picks_up_a_late_arriving_answer(monkeypatch):
    """The narrower race the spawn-time snapshot alone cannot close: an
    answer lands for this cell's target AFTER `_spawn_ribbon_worker` already
    snapshotted `_answered_pockets` (finding nothing, so this build goes out
    plain) but BEFORE that build is actually applied
    (`_drain_pending_ribbon`/`_apply_ribbon`). The fix's safety net --
    `_apply_ribbon` calling `_maybe_highlight_pocket` again once
    `shown_cif_path` is genuinely set to this ribbon's own file -- is what
    catches this; without it the answer would be silently dropped exactly
    like the Critical bug, just in a narrower window.
    """
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    mesh = _SlowMesh(delay=0.0)
    monkeypatch.setattr(mod, "structure_mesh", mesh)

    app = _questions_app()
    app._handle_event(_start("j1", card=0, target_id="dhfr"))
    app._on_event(_frame("j1", n_atoms=4, spread=1.0))
    app._drain_frames()

    # No answer yet at job_done/spawn time -- the ordinary ribbon build goes
    # out plain, exactly like every non-question fold.
    app._handle_event({"type": "job_done", "job_id": "j1",
                       "cif_path": "/fold1/dhfr.cif", "wall_s": 1.0,
                       "mean_plddt": 90.0})
    assert app._join_ribbon_workers(timeout=5.0)
    assert mesh.calls == [None]

    # The answer lands in the gap: the plain build has already FINISHED
    # (joined above) but has not been DRAINED/APPLIED yet -- the exact
    # window between a worker finishing and the main loop's idle callback
    # actually running.
    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "dhfr", "score": 0.9})
    app._drain_pending_ribbon()
    viewer = app.quad.viewer_for_slot(0)
    assert viewer.shown[0] == "ribbon"

    # The safety net inside _apply_ribbon should have spawned a highlight
    # rebuild the instant the plain ribbon landed and shown_cif_path became
    # genuinely valid for this fold.
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_highlight()

    assert mesh.calls[-1] == {("A", 1)}, (
        "the safety-net rebuild never fired for the late-arriving answer")
    assert viewer.shown == ("ribbon", ["mesh:highlighted"])


def test_a_later_unrelated_refold_of_the_same_target_does_not_reuse_a_consumed_answer(
        monkeypatch):
    """Once an answer has been visually applied to one ribbon, a LATER,
    unrelated re-fold of the same target (the attract loop rotating back to
    it, with no new question asked) must not resurrect the old highlight --
    see `_answered_pockets`' docstring in `DemoApp.__init__` for the full
    reasoning."""
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    mesh = _SlowMesh(delay=0.0)
    monkeypatch.setattr(mod, "structure_mesh", mesh)

    app = _questions_app()
    app._handle_event(_start("j1", card=0, target_id="dhfr"))
    app._on_event(_frame("j1", n_atoms=4, spread=1.0))
    app._drain_frames()
    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "dhfr", "score": 0.9})
    app._handle_event({"type": "job_done", "job_id": "j1",
                       "cif_path": "/fold1/dhfr.cif", "wall_s": 1.0,
                       "mean_plddt": 90.0})
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()
    viewer = app.quad.viewer_for_slot(0)
    assert viewer.shown == ("ribbon", ["mesh:highlighted"])
    assert "dhfr" not in app._answered_pockets, (
        "the answer should be consumed once visually applied")

    # Let the dwell expire and re-fold the SAME target, with no new
    # question asked.
    app._tick_state_at(0.0)
    app._tick_state_at(99.0)
    app._handle_event(_start("j2", card=0, target_id="dhfr"))
    app._on_event(_frame("j2", n_atoms=4, spread=90.0))
    app._drain_frames()
    app._handle_event({"type": "job_done", "job_id": "j2",
                       "cif_path": "/fold2/dhfr.cif", "wall_s": 1.0,
                       "mean_plddt": 91.0})
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()

    assert mesh.calls[-1] is None, (
        "a later, unrelated re-fold of the same target reused a consumed "
        "answer's highlight")
    assert viewer.shown == ("ribbon", ["mesh:plain"])


def test_answer_done_for_a_different_target_touches_nothing(monkeypatch):
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    mesh = _SlowMesh(delay=0.0)
    monkeypatch.setattr(mod, "structure_mesh", mesh)

    app = _questions_app()
    viewer = _fold_and_finish(app, target_id="fkbp12")
    before = viewer.shown

    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "some_other_target", "score": 0.9})
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_highlight()

    assert viewer.shown == before, "a highlight for a different target changed the screen"
    assert mesh.calls == [None], "no highlight rebuild should have been spawned at all"


def test_a_highlight_arriving_after_the_cell_moved_on_is_dropped(monkeypatch):
    """The re-check in `_apply_highlight`: a slow rebuild must not paint a
    finished ribbon back over a cell that has since started a new fold --
    the exact defect this project's hold-until-superseded work exists to
    prevent, at a different call site."""
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    mesh = _SlowMesh(delay=0.15)
    monkeypatch.setattr(mod, "structure_mesh", mesh)

    app = _questions_app()
    viewer = _fold_and_finish(app, target_id="fkbp12")
    before = viewer.shown

    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "fkbp12", "score": 0.9})
    # Simulate the cell moving on to a new fold WHILE the worker is still
    # inside its 0.15s sleep -- the same race a real booth can produce
    # (structure_mesh costs up to ~1.2s at 3000 residues; the daemon never
    # pauses between folds).
    view = app._slot_view(0)
    view.shown_target_id = "a_newer_fold"
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_highlight()

    assert viewer.shown == before, "a stale highlight was drawn after the cell moved on"


def test_shown_cif_path_is_cleared_the_instant_a_new_folds_frames_start(monkeypatch):
    """The narrower half of the stale-cif defect, pinned directly: once a
    new fold's frames start drawing on a cell, `shown_cif_path` must not
    still be pointing at the OUTGOING fold's `.cif` -- it goes to `None` in
    lockstep with `has_structure` going `False`, at the exact handover point
    in `_draw_frame` (`viewer.clear_structure()` / `view.has_structure =
    False`). Before this fix, `shown_cif_path` was left untouched there,
    while `shown_target_id` (a few lines later, once the frame lands) WAS
    renamed to the incoming fold -- so a reader checking `shown_target_id`
    and `shown_cif_path` together saw a self-consistent-looking but wrong
    pair: the new target's id next to the old target's file.
    """
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    monkeypatch.setattr(mod, "structure_mesh", _SlowMesh(delay=0.0))

    app = _questions_app()
    _fold_and_finish(app, job_id="j-dhfr", target_id="dhfr",
                     cif_path="/fold1/dhfr.cif")
    view = app._slot_view(0)
    assert view.shown_cif_path == "/fold1/dhfr.cif"
    assert view.has_structure is True

    # Let the showcase dwell expire, the same way the daemon's own next
    # fold would arrive well after it (the daemon never pauses between
    # folds; the dwell is what makes room for the NEXT job_start below).
    app._tick_state_at(0.0)
    app._tick_state_at(99.0)

    # Trypsin starts folding in the SAME cell; its first frame lands.
    app._handle_event(_start("j-trypsin", card=0, target_id="trypsin"))
    app._on_event(_frame("j-trypsin", n_atoms=4, spread=90.0))
    app._drain_frames()

    assert view.shown_target_id == "trypsin"
    assert view.has_structure is True
    assert view.shown_cif_path is None, (
        "shown_cif_path still names an older fold's .cif once a new fold's "
        "frames have started drawing on this cell")


def test_answer_done_does_not_paint_a_stale_ribbon_over_a_different_folds_diffusion(
        monkeypatch):
    """The Critical defect, reproduced end to end: DHFR's ribbon is shown,
    trypsin starts folding in the same cell and its first frame lands (so
    the cell is genuinely showing trypsin's live diffusion point cloud, with
    no ribbon of its own yet), and then an answer for TRYPSIN lands. Without
    the fix, the guard in `_maybe_highlight_pocket` reads `shown_target_id
    == "trypsin"`, `has_structure`, and a truthy (but stale) `shown_cif_path`
    -- all true -- and rebuilds a highlighted ribbon from DHFR's `.cif`,
    which `_apply_highlight` then paints directly over trypsin's live
    diffusion, snapping the camera to DHFR's geometry (the exact camera cut
    spec section 6 forbids).
    """
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    mesh = _SlowMesh(delay=0.0)
    monkeypatch.setattr(mod, "structure_mesh", mesh)

    app = _questions_app()
    viewer = _fold_and_finish(app, job_id="j-dhfr", target_id="dhfr",
                              cif_path="/fold1/dhfr.cif")
    assert mesh.calls == [None]  # the ordinary ribbon build, no highlight

    # Let the showcase dwell expire so the next fold is not deferred.
    app._tick_state_at(0.0)
    app._tick_state_at(99.0)
    app._handle_event(_start("j-trypsin", card=0, target_id="trypsin"))
    app._on_event(_frame("j-trypsin", n_atoms=4, spread=90.0))
    app._drain_frames()
    assert viewer.shown[0] == "points", (
        "precondition: the cell should be showing trypsin's own live "
        "diffusion, not a ribbon")
    before = viewer.shown

    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "trypsin", "score": 0.9})
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_highlight()

    assert mesh.calls == [None], (
        "a highlight rebuild was spawned from a stale .cif path even "
        "though trypsin has no ribbon of its own on screen yet")
    assert viewer.shown == before, (
        "trypsin's live diffusion was replaced by a ribbon rebuilt from an "
        "older fold's stale .cif path"
    )


def test_a_highlight_for_an_older_ribbon_of_the_same_target_is_dropped(monkeypatch):
    """The narrower in-flight variant `_apply_highlight`'s own re-check
    closes: a highlight rebuild is spawned from ribbon A of some target,
    and while it is still in flight a NEWER ribbon (B) of the SAME target
    lands on the same cell. `shown_target_id` alone cannot see this race --
    it reads the same target the whole time -- so the fix also re-checks
    `shown_cif_path` against the exact path the in-flight rebuild was built
    from.

    Updated by the Critical fix (whole-branch review): the answer for
    `dhfr` is still UNCONSUMED when fold 2's ribbon is spawned (fold 1's
    highlight rebuild -- built from fold 1's now-stale `.cif` -- has not
    applied yet), so `_spawn_ribbon_worker`'s own snapshot now finds it and
    bakes the highlight directly into fold 2's build. That is the correct,
    intended outcome of the fix (the answer is not orphaned; it lands on
    whichever ribbon of `dhfr` is actually current), not a regression of
    this test's ORIGINAL guarantee -- which is narrower and still holds:
    the STALE highlight rebuilt from fold 1's `.cif` must never overwrite
    fold 2's ribbon once fold 2 is showing, highlighted or not.
    """
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    mesh = _SlowMesh(delay=0.15)
    monkeypatch.setattr(mod, "structure_mesh", mesh)

    app = _questions_app()
    viewer = _fold_and_finish(app, job_id="j1", target_id="dhfr",
                              cif_path="/fold1/dhfr.cif")

    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "dhfr", "score": 0.9})
    # While that rebuild is still sleeping, a SECOND fold of the same
    # target lands a NEWER ribbon on this cell -- same target id, different
    # .cif. The answer is still unconsumed, so this build's own spawn-time
    # snapshot picks it up and bakes the highlight in directly.
    app._handle_event(_start("j2", card=0, target_id="dhfr"))
    app._on_event(_frame("j2", n_atoms=4, spread=1.0))
    app._drain_frames()
    app._handle_event({"type": "job_done", "job_id": "j2",
                       "cif_path": "/fold2/dhfr.cif", "wall_s": 1.0,
                       "mean_plddt": 91.0})
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_ribbon()
    after_second_fold = viewer.shown
    assert after_second_fold == ("ribbon", ["mesh:highlighted"]), (
        "fold 2's own build should have baked in the still-unconsumed "
        "answer for dhfr")

    # Now the first (stale) highlight rebuild -- built from fold 1's .cif,
    # which this cell no longer shows -- lands. It must not touch the
    # screen: `_apply_highlight`'s own `shown_cif_path` re-check is what
    # drops it.
    app._drain_pending_highlight()

    assert viewer.shown == after_second_fold, (
        "a highlight rebuilt from an OLDER ribbon of the same target was "
        "painted over a newer ribbon of that same target"
    )


def test_a_highlight_never_calls_begin_crossfade(monkeypatch):
    """Spec section 6: no camera fly-to/cut when a highlight arrives. This
    is the narrowest observable proxy available headlessly -- the real
    camera reframe happens inside the real `set_ribbon`, which this fake
    does not model, but `begin_crossfade` is the one call this file's own
    ribbon-reveal path uses to visibly change what is on screen, and the
    highlight path must not call it."""
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    monkeypatch.setattr(mod, "structure_mesh", _SlowMesh(delay=0.0))

    app = _questions_app()
    viewer = _fold_and_finish(app, target_id="fkbp12")
    crossfades_before = viewer.crossfades

    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "fkbp12", "score": 0.9})
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_highlight()

    assert viewer.crossfades == crossfades_before


def test_a_highlight_build_failure_leaves_the_screen_intact(monkeypatch):
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})

    def explode(cif_path, highlight_residues=None):
        raise RuntimeError("boom")

    app = _questions_app()
    viewer = _fold_and_finish(app, target_id="fkbp12")
    before = viewer.shown
    monkeypatch.setattr(mod, "structure_mesh", explode)

    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "fkbp12", "score": 0.9})
    assert app._join_ribbon_workers(timeout=5.0)
    app._drain_pending_highlight()  # must not raise

    assert viewer.shown == before


def test_the_highlight_rebuild_runs_off_the_main_thread(monkeypatch):
    import ui.app as mod
    monkeypatch.setattr(mod, "_compute_pocket_residues",
                        lambda cif_path: {("A", 1)})
    mesh = _SlowMesh(delay=0.0)
    thread_names = []

    def recording_mesh(cif_path, highlight_residues=None):
        thread_names.append(threading.current_thread().name)
        return mesh(cif_path, highlight_residues=highlight_residues)

    monkeypatch.setattr(mod, "structure_mesh", recording_mesh)

    app = _questions_app()
    _fold_and_finish(app, target_id="fkbp12")
    caller = threading.current_thread().name

    app._handle_event({"type": "answer_done", "question_id": "q1",
                       "target_id": "fkbp12", "score": 0.9})
    assert app._join_ribbon_workers(timeout=5.0)

    highlight_calls = [n for n in thread_names if n != caller]
    assert highlight_calls, "structure_mesh was never called for the highlight rebuild"


# ---------------------------------------------------------------------------
# 4. the two trigger paths send `question`, never a separate `pick`.
# ---------------------------------------------------------------------------

def test_on_ask_sends_a_question_not_a_pick():
    app = _questions_app()
    app._on_ask("q1", "fkbp12")
    assert app._client.questions == [("q1", "fkbp12")]
    assert app._client.picks == []


def test_on_ask_moves_the_focus_and_acknowledges_like_a_pick():
    """runner/daemon.py's `_accept_question` folds the target itself, so a
    visitor's ask deserves the identical tap-time feedback a plain pick
    gives (spec: both trigger paths send only `question`)."""
    app = _questions_app(cards=(0, 1, 2, 3))
    app._on_ask("q1", "hemoglobin")
    assert app.router.selected_target == "hemoglobin"


def test_on_ask_with_no_daemon_at_all_does_not_raise():
    app = _questions_app()
    app._client = None
    app._on_ask("q1", "hemoglobin")  # must not raise


def test_nothing_is_asked_of_a_daemon_the_ui_has_refused():
    app = _questions_app()
    app._connection_state = "incompatible"
    app._on_ask("q1", "hemoglobin")
    assert app._client.questions == []


def test_ask_next_question_round_robins_through_the_playlist():
    questions = [_question(id="q1", target_id="a"), _question(id="q2", target_id="b")]
    app = _questions_app(questions=questions)
    app._ask_next_question()
    app._ask_next_question()
    app._ask_next_question()
    assert app._client.questions == [("q1", "a"), ("q2", "b"), ("q1", "a")]


def test_ask_next_question_sends_only_a_question_never_a_pick():
    app = _questions_app(questions=[_question(id="q1", target_id="a")])
    app._ask_next_question()
    assert app._client.picks == []


def test_ask_next_question_does_nothing_with_no_questions_loaded():
    app = _questions_app(questions=[])
    app._ask_next_question()
    assert app._client.questions == []


def test_ask_next_question_does_nothing_when_the_booth_is_not_qa_capable():
    """A safety net, not the source of truth (the daemon ignores a question
    with no chip reserved for it regardless) -- but the booth should not
    narrate a capability it has already told its own panel is not there."""
    questions = [_question(id="q1", target_id="a")]
    app = _questions_app(questions=questions, qa_capable=False)
    app._ask_next_question()
    assert app._client.questions == []


def test_ask_next_question_is_not_a_visitor_touch():
    """The attract loop acting on its own must not reset the idle clock --
    doing so would let ASK_QUESTION keep the choreography running forever."""
    app = _questions_app(questions=[_question(id="q1", target_id="a")])
    assert app._last_input_at is None
    app._ask_next_question()
    assert app._last_input_at is None


def test_the_attract_cues_ask_question_branch_calls_ask_next_question():
    """Wires `ui.attract`'s ASK_QUESTION cue to `_ask_next_question` --
    driven through the real `_tick_attract`, with a stub choreography so the
    test does not have to run the clock through the whole 82s+ cycle."""

    class _StubChoreography:
        def tick(self, now, idle_s):
            return [ASK_QUESTION]

    questions = [_question(id="q1", target_id="a")]
    app = _questions_app(questions=questions)
    app.attract = _StubChoreography()
    app._last_input_at = 0.0  # so _tick_overlays' idle branch is reachable
    app._tick_attract(now=1000.0, idle_s=1000.0)
    assert app._client.questions == [("q1", "a")]


def test_an_ask_question_cue_cannot_freeze_the_attract_tick():
    """Same guard shape every other cue in `_tick_attract` gets: a raise
    inside one cue's handling must not stop the others (or the tick loop
    itself) from running."""

    class _StubChoreography:
        def tick(self, now, idle_s):
            return [ASK_QUESTION]

    app = _questions_app(questions=[])

    def boom():
        raise RuntimeError("boom")

    app.attract = _StubChoreography()
    app._ask_next_question = boom
    app._tick_attract(now=1000.0, idle_s=1000.0)  # must not raise


# ---------------------------------------------------------------------------
# Task 2: the attract-loop question is now chosen adaptively
# (ui/questioning.py) instead of by blind round-robin.
# ---------------------------------------------------------------------------

def test_ask_next_prefers_the_on_screen_target():
    """Relevance beats the id tie-break: with 'beta' on screen, the beta
    question is asked even though the alpha question sorts first by id."""
    app = _questions_app(questions=[
        _question(id="qa", target_id="alpha"),
        _question(id="qb", target_id="beta"),
    ])
    # Put 'beta' on the hero screen (focus slot 0).
    app._handle_event(_start("j1", card=0, target_id="beta"))
    app._ask_next_question()
    assert app._client.questions == [("qb", "beta")]


def test_ask_next_falls_back_to_pool_when_on_screen_target_has_no_question():
    """The on-screen target has no question of its own, so the selector falls
    back to the whole pool rather than asking nothing."""
    app = _questions_app(questions=[_question(id="qa", target_id="alpha")])
    app._handle_event(_start("j1", card=0, target_id="beta"))
    app._ask_next_question()
    assert app._client.questions == [("qa", "alpha")]


def test_ask_next_does_not_immediately_repeat():
    """Two questions about the same on-screen target: the second ask must be
    the other one, not a repeat of the first."""
    app = _questions_app(questions=[
        _question(id="q1", target_id="alpha"),
        _question(id="q2", target_id="alpha"),
    ])
    app._handle_event(_start("j1", card=0, target_id="alpha"))
    app._ask_next_question()
    app._ask_next_question()
    asked = [qid for qid, _ in app._client.questions]
    assert asked == ["q1", "q2"]


def test_ask_next_never_immediately_repeats_over_many_asks():
    app = _questions_app(questions=[
        _question(id=f"q{i}", target_id="t") for i in range(3)
    ])
    app._handle_event(_start("j1", card=0, target_id="t"))
    for _ in range(8):
        app._ask_next_question()
    asked = [qid for qid, _ in app._client.questions]
    for a, b in zip(asked, asked[1:]):
        assert a != b, f"immediate repeat in {asked}"
    # The recency window stays bounded by the pool size.
    assert len(app._recently_asked) <= 3
