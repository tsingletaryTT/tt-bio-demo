"""Unit tests for Folder.fold()'s event emission.

These exercise the real code path -- fold() itself, including the on_frame
and on_progress closures it wires up around the trajectory tap and tt-bio's
progress_fn -- rather than a parallel pure model of it. An earlier version of
this file tested a standalone `fold_event_sequence` helper that nothing in
production called; all six tests passed while the actual per-callback logic
in fold() (the frame-index math, the stage-fraction math) had zero coverage,
and it shipped with two real bugs as a result. See runner/folder.py's module
docstring and .superpowers/sdd/2026-08-11-runner-daemon/task-5-report.md,
"Fix report", for the full story.

_run_fold is the one seam this module controls without hardware: it is
monkeypatched here to synchronously replay tt-bio's own callback shapes
(on_progress(stage, step=, total=), and dump_fn(step, coords) through the
already-installed trajectory tap) so fold()'s real wiring runs end to end.
tt_bio.protenix is faked in sys.modules exactly like
tests/unit/runner/test_dump_tap.py fakes it, so none of this needs torch,
ttnn, or a device.
"""

import sys
import types

import numpy as np
import pytest

from protocol.events import EVENT_TYPES
from runner.folder import FoldError, Folder

_RESULT = {"cif_path": "/tmp/out.cif", "wall_s": 5.73, "mean_plddt": 0.952}


def _fake_protenix(monkeypatch, dump_steps=()):
    """Install a stand-in tt_bio.protenix.edm_sample that replays
    `dump_steps` -- an iterable of (raw_step, coords) pairs -- through
    whatever dump_fn it is called with. This is the same shape
    install_trajectory_tap's wrapper drives the real edm_sample with, so
    calling the faked edm_sample below exercises fold()'s real on_frame.
    """
    mod = types.ModuleType("tt_bio.protenix")

    def edm_sample(*args, dump_fn=None, **kw):
        if dump_fn is not None:
            for raw_step, coords in dump_steps:
                dump_fn(raw_step, coords)
        return "coords-sentinel"

    mod.edm_sample = edm_sample
    pkg = types.ModuleType("tt_bio")
    pkg.protenix = mod
    monkeypatch.setitem(sys.modules, "tt_bio", pkg)
    monkeypatch.setitem(sys.modules, "tt_bio.protenix", mod)
    return mod


def _folder_with_fake_fold(monkeypatch, *, progress_calls=(), dump_steps=(),
                            result=None, raise_from_run_fold=None):
    """A Folder ready to fold(), with _run_fold monkeypatched to synchronously
    replay `progress_calls` through on_progress and `dump_steps` through the
    installed trajectory tap, then return `result` (or raise, for the error
    path). Skips load()'s real device/model work -- fold() only ever checks
    self._loaded, so setting it directly is enough.
    """
    _fake_protenix(monkeypatch, dump_steps)
    folder = Folder()
    folder._loaded = True

    def _run_fold(self, input_path, on_progress, n_step):
        if raise_from_run_fold is not None:
            raise raise_from_run_fold
        for stage, step, total in progress_calls:
            on_progress(stage, step=step, total=total)
        import tt_bio.protenix as protenix
        protenix.edm_sample(dump_fn=None)
        return dict(result) if result is not None else dict(_RESULT)

    monkeypatch.setattr(Folder, "_run_fold", _run_fold)
    return folder


def _fold(folder, *, target_id="trpcage", n_residues=20, card=0, n_step=200):
    events = []
    folder.fold("j1", "/tmp/in.yaml", events.append,
                target_id=target_id, n_residues=n_residues, card=card,
                n_step=n_step)
    return events


def test_the_sequence_starts_with_job_start_and_ends_with_job_done(monkeypatch):
    folder = _folder_with_fake_fold(monkeypatch)
    events = _fold(folder)
    assert events[0]["type"] == "job_start"
    assert events[-1]["type"] == "job_done"


def test_every_emitted_event_is_a_known_protocol_type(monkeypatch):
    folder = _folder_with_fake_fold(
        monkeypatch, progress_calls=[("trunk", 5, 10), ("diffusion", 100, 200)],
    )
    events = _fold(folder)
    for event in events:
        assert event["type"] in EVENT_TYPES


def test_job_start_carries_what_the_ui_needs_to_label_the_screen(monkeypatch):
    folder = _folder_with_fake_fold(monkeypatch)
    events = _fold(folder, target_id="trpcage", card=2, n_residues=20)
    start = events[0]
    assert start["target_id"] == "trpcage"
    assert start["model"] == "protenix-v2"
    assert start["card"] == 2
    assert start["n_residues"] == 20


def test_job_done_reports_plddt_in_percent_not_as_a_fraction(monkeypatch):
    folder = _folder_with_fake_fold(monkeypatch, result={**_RESULT, "mean_plddt": 0.952})
    events = _fold(folder)
    assert events[-1]["mean_plddt"] == pytest.approx(95.2)


def test_frames_appear_between_the_stages_and_the_completion(monkeypatch):
    dump_steps = [(-1, np.zeros((3, 3), dtype=np.float32)),
                  (0, np.ones((3, 3), dtype=np.float32))]
    folder = _folder_with_fake_fold(
        monkeypatch, dump_steps=dump_steps, progress_calls=[("trunk", 5, 10)],
    )
    events = _fold(folder, n_step=1)
    kinds = [e["type"] for e in events]
    assert "frame" in kinds, "expected the tapped dump_fn calls to produce frame events"
    assert kinds.index("stage") < kinds.index("frame")
    assert kinds.index("frame") < kinds.index("job_done")


def test_all_six_protocol_stages_are_emitted_in_order(monkeypatch):
    folder = _folder_with_fake_fold(
        monkeypatch,
        progress_calls=[("trunk", 10, 10), ("diffusion", 200, 200)],
    )
    events = _fold(folder)
    emitted = [e["stage"] for e in events if e["type"] == "stage"]
    assert emitted == ["msa", "prep", "trunk", "diffusion", "confidence", "saving"]


def test_progress_fraction_never_decreases_across_a_full_fold(monkeypatch):
    """Regression for Finding 2: the fraction used to collapse from ~40% back
    to under 1% the instant diffusion's first progress callback arrived,
    because each stage's fraction restarted at 0.0 instead of continuing from
    where the previous stage left off.
    """
    progress_calls = (
        [("trunk", s, 10) for s in range(1, 11)]
        + [("diffusion", s, 200) for s in range(1, 201)]
    )
    folder = _folder_with_fake_fold(monkeypatch, progress_calls=progress_calls)
    events = _fold(folder)
    fracs = [e["frac"] for e in events if e["type"] == "stage"]
    assert fracs == sorted(fracs), "fraction must never decrease across a fold"
    assert 0.0 <= fracs[0]
    assert fracs[-1] <= 1.0


def test_msa_stage_fires_even_though_this_input_always_skips_real_msa(monkeypatch):
    """Regression for Finding 3: msa was silently never emitted, which the
    protocol's six-segment pipeline panel would have shown as permanently
    pending rather than as a stage that ran (briefly) and finished.
    """
    folder = _folder_with_fake_fold(monkeypatch)
    events = _fold(folder)
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert "msa" in stages
    assert stages[0] == "msa", "msa must be the first stage on the wire"


def test_fold_before_load_raises_fold_error():
    folder = Folder()
    with pytest.raises(FoldError, match="load"):
        folder.fold("j1", "/tmp/in.yaml", lambda e: None,
                    target_id="t", n_residues=20)


def test_a_run_fold_failure_is_wrapped_in_fold_error(monkeypatch):
    folder = _folder_with_fake_fold(
        monkeypatch, raise_from_run_fold=RuntimeError("tt-bio blew up"),
    )
    with pytest.raises(FoldError, match="tt-bio blew up"):
        _fold(folder)


def test_a_broken_trajectory_tap_is_wrapped_in_fold_error(monkeypatch):
    """install_trajectory_tap() calls check_tap_supported(), which raises
    TapUnavailable -- not FoldError -- when tt-bio's internals no longer
    match what the tap expects (see runner/dump_tap.py). fold()'s own
    docstring promises "Raises FoldError on failure"; that must hold
    regardless of which step inside fold() is the one that goes wrong.

    Regression: install_trajectory_tap() used to be called before fold()'s
    own try block began, so TapUnavailable escaped fold() directly instead
    of being wrapped -- exactly the kind of exception the daemon's fold loop
    relies on FoldError-only handling to catch.
    """
    # A tt_bio.protenix with no edm_sample at all: check_tap_supported's own
    # definition of "the tap cannot work" (see test_dump_tap.py).
    mod = types.ModuleType("tt_bio.protenix")
    pkg = types.ModuleType("tt_bio")
    pkg.protenix = mod
    monkeypatch.setitem(sys.modules, "tt_bio", pkg)
    monkeypatch.setitem(sys.modules, "tt_bio.protenix", mod)

    folder = Folder()
    folder._loaded = True
    with pytest.raises(FoldError, match="edm_sample"):
        _fold(folder)


def test_an_unexpected_progress_stage_is_dropped_not_fatal(monkeypatch):
    """tt-bio only ever reports trunk/diffusion today, but progress_fn is
    external instrumentation this module doesn't control -- an unrecognized
    stage name must not crash an otherwise-fine fold.
    """
    folder = _folder_with_fake_fold(
        monkeypatch, progress_calls=[("some_future_stage", 1, 2)],
    )
    events = _fold(folder)
    assert events[-1]["type"] == "job_done"
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert "some_future_stage" not in stages
