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

import pathlib
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


def test_frame_index_and_decimation_are_pinned_without_hardware(monkeypatch):
    """The unit fixture everywhere else in this file uses n_step=1 (e.g.
    test_frames_appear_between_the_stages_and_the_completion above), which
    makes select_frame_steps(n_step + 1, target=30) collapse `keep` down to
    {0, 1} -- every raw step survives regardless of the two lines this test
    exists to pin, so neither is ever observed to matter at that size:

    - `index = step + 1` in Folder.fold()'s on_frame: dump_fn's raw step
      range is -1..n_step-1 (-1 is the initial noise draw); the wire's
      frame.step must be 0..n_step. Only the +1 mapping can ever produce a
      frame at step == n_step -- dropping it caps the reachable wire index
      at n_step-1, one short forever.
    - `if index in keep:` in the same function: without it, all n_step+1
      raw steps reach the wire instead of the ~30 select_frame_steps()
      keeps -- bandwidth for no visual gain, the whole reason this exists.

    This drives a full 201-callback fold (n_step=200, tt-bio's real per-fold
    denoising step count) through the real on_frame closure -- no card
    needed, since _run_fold is monkeypatched the same way every other test
    in this file already does it -- so both lines are pinned at the size
    they actually run at.
    """
    dump_steps = [(step, np.full((2, 3), float(step), dtype=np.float32))
                  for step in range(-1, 200)]   # raw steps -1..199: 201 total
    folder = _folder_with_fake_fold(monkeypatch, dump_steps=dump_steps)
    events = _fold(folder, n_step=200)
    frames = [e for e in events if e["type"] == "frame"]

    assert 25 <= len(frames) <= 32, (
        f"expected ~30 frames via select_frame_steps' decimation, got "
        f"{len(frames)} -- the `if index in keep:` guard may be gone")
    steps = [f["step"] for f in frames]
    assert steps == sorted(steps), "frames must arrive in increasing step order"
    assert steps[0] == 0, (
        "the initial noise draw (raw step -1) must be wire index 0")
    assert steps[-1] == 200, (
        "the final denoising step (raw step 199) must be wire index 200 -- "
        "only reachable via index = step + 1; dropping the +1 caps the max "
        "reachable index at 199")


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


def test_a_malformed_run_fold_result_is_wrapped_in_fold_error(monkeypatch):
    """fold()'s tail end used to read result["cif_path"] and
    result["mean_plddt"] *after* the try/except that wraps _run_fold, so a
    _run_fold that returned something not shaped like the documented
    {'cif_path': str, 'mean_plddt': float} raised a raw KeyError/TypeError
    straight out of fold() -- breaking the "Raises FoldError on failure"
    contract for exactly the failure mode most likely once _run_fold talks
    to real tt-bio (an upstream return-shape change, a missing key). Both a
    missing key and a value plddt_to_percent can't coerce to float must
    become FoldError, not whatever exception the malformed access happens
    to raise.
    """
    missing_key = _folder_with_fake_fold(monkeypatch, result={"cif_path": "/tmp/x.cif"})
    with pytest.raises(FoldError, match="mean_plddt"):
        _fold(missing_key)

    bad_value = _folder_with_fake_fold(
        monkeypatch, result={"cif_path": "/tmp/x.cif", "mean_plddt": "not-a-number"},
    )
    with pytest.raises(FoldError, match="not-a-number|could not convert"):
        _fold(bad_value)


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


def _fake_tt_bio_load_stack(monkeypatch, *, fail_at):
    """Install fake tt_bio.main / tt_bio.protenix / tt_bio.tenstorrent
    modules that support forcing any one of Folder.load()'s three fallible
    post-device-open calls -- weights.fetch, Protenix.load_from_checkpoint,
    download_mols -- to raise, while counting get_device()/cleanup() calls
    so a test can observe whether the device was actually released. No
    torch/ttnn/hardware needed: get_device() here returns a plain sentinel
    object, exactly the same faking approach _fake_protenix above uses for
    tt_bio.protenix.edm_sample.
    """
    calls = {"get_device": 0, "cleanup": 0}

    tenstorrent_mod = types.ModuleType("tt_bio.tenstorrent")

    def get_device(trace_region_size=0):
        calls["get_device"] += 1
        return object()

    def cleanup():
        calls["cleanup"] += 1

    tenstorrent_mod.get_device = get_device
    tenstorrent_mod.cleanup = cleanup

    main_mod = types.ModuleType("tt_bio.main")

    # tt-bio 0.7.0: the checkpoint is resolved through tt_bio.weights.fetch(key)
    # rather than tt_bio.main.hf_artifact(repo, filename, dest), which no longer
    # exists. The fake follows the real import so this test keeps exercising the
    # call Folder.load() actually makes.
    weights_mod = types.ModuleType("tt_bio.weights")

    def fetch(key, *, root=None, force=False):
        if fail_at == "weights_fetch":
            raise RuntimeError("checkpoint download failed")
        return (root or pathlib.Path("/nonexistent")) / f"{key}.pt"

    weights_mod.fetch = fetch

    def download_mols(cache):
        if fail_at == "download_mols":
            raise RuntimeError("mol tarball extraction failed")
        return cache / "mols"

    main_mod.download_mols = download_mols

    protenix_mod = types.ModuleType("tt_bio.protenix")

    class _FakeProtenixModel:
        @classmethod
        def load_from_checkpoint(cls, path, device=None):
            if fail_at == "load_from_checkpoint":
                raise RuntimeError("incompatible checkpoint format")
            return object()

    protenix_mod.Protenix = _FakeProtenixModel

    pkg = types.ModuleType("tt_bio")
    pkg.main = main_mod
    pkg.weights = weights_mod
    pkg.protenix = protenix_mod
    pkg.tenstorrent = tenstorrent_mod

    monkeypatch.setitem(sys.modules, "tt_bio", pkg)
    monkeypatch.setitem(sys.modules, "tt_bio.main", main_mod)
    monkeypatch.setitem(sys.modules, "tt_bio.weights", weights_mod)
    monkeypatch.setitem(sys.modules, "tt_bio.protenix", protenix_mod)
    monkeypatch.setitem(sys.modules, "tt_bio.tenstorrent", tenstorrent_mod)
    return calls


@pytest.mark.parametrize("fail_at", ["weights_fetch", "load_from_checkpoint", "download_mols"])
def test_a_failed_load_releases_the_device_it_already_opened(monkeypatch, fail_at):
    """Regression: load() opens the device via get_device() *before* any of
    its three later fallible calls run. A failure in any of those three used
    to leave self._device set and self._loaded False -- and close()'s own
    guard (`if not self._loaded: return`) treats that indistinguishably from
    "load() was never called," so it never called cleanup() and never
    released the device or its host-local DeviceLease. A daemon that catches
    this startup failure and keeps running (runner/daemon.py's Daemon.run()
    does exactly that, retrying load() and serving `not_ready` in the
    meantime instead of exiting) would then hold a card it can neither use
    nor release for the rest of the process's life.

    This forces each of the three fallible steps to raise in turn, without
    hardware, and asserts the device was actually released -- cleanup()
    called, self._device back to None -- rather than only that _loaded
    stayed False, since _loaded is exactly the flag that lied about this.
    """
    calls = _fake_tt_bio_load_stack(monkeypatch, fail_at=fail_at)
    folder = Folder()

    with pytest.raises(RuntimeError):
        folder.load()

    assert calls["get_device"] == 1, "the device was opened before the failure"
    assert calls["cleanup"] == 1, "a failed load() must release what it opened"
    assert folder._device is None, "no device handle must survive a failed load()"
    assert folder._loaded is False

    # close() must remain the safe no-op its docstring promises -- and must
    # not call cleanup() a second time on top of load()'s own cleanup, now
    # that load() has already released everything itself.
    folder.close()
    assert calls["cleanup"] == 1
