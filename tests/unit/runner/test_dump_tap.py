import sys
import types

import numpy as np
import pytest

from runner.dump_tap import (
    TapUnavailable,
    check_tap_supported,
    install_trajectory_tap,
    remove_trajectory_tap,
)


def _fake_protenix(monkeypatch, *, fold_passes_dump_fn=False, with_edm=True):
    """Install a stand-in tt_bio.protenix so these tests need no torch or hardware."""
    mod = types.ModuleType("tt_bio.protenix")

    if with_edm:
        def edm_sample(diffusion_module, cond, n_atoms, *, dump_fn=None, **kw):
            # Two steps of a two-atom "trajectory", shaped like the real thing.
            for step in (-1, 0):
                if dump_fn is not None:
                    dump_fn(step, np.full((1, 2, 3), float(step), dtype=np.float32))
            return "coords-sentinel"
        mod.edm_sample = edm_sample

    def fold():
        kwargs = {"dump_fn": None} if fold_passes_dump_fn else {}
        return sys.modules["tt_bio.protenix"].edm_sample(None, None, 2, **kwargs)
    mod.fold = fold

    pkg = types.ModuleType("tt_bio")
    pkg.protenix = mod
    monkeypatch.setitem(sys.modules, "tt_bio", pkg)
    monkeypatch.setitem(sys.modules, "tt_bio.protenix", mod)
    return mod


def test_check_passes_when_edm_sample_accepts_dump_fn(monkeypatch):
    _fake_protenix(monkeypatch)
    assert check_tap_supported() is None


def test_check_raises_when_edm_sample_is_missing(monkeypatch):
    _fake_protenix(monkeypatch, with_edm=False)
    with pytest.raises(TapUnavailable, match="edm_sample"):
        check_tap_supported()


def test_check_raises_when_edm_sample_lost_its_dump_fn_parameter(monkeypatch):
    mod = _fake_protenix(monkeypatch)
    mod.edm_sample = lambda diffusion_module, cond, n_atoms, **kw: None
    with pytest.raises(TapUnavailable, match="dump_fn"):
        check_tap_supported()


def test_tap_receives_every_step_as_an_n_by_3_float32_array(monkeypatch):
    mod = _fake_protenix(monkeypatch)
    seen = []
    handle = install_trajectory_tap(lambda s, st, c: seen.append((s, st, c)))
    try:
        mod.fold()
    finally:
        remove_trajectory_tap(handle)

    assert [(s, st) for s, st, _ in seen] == [(0, -1), (0, 0)]
    for _, st, coords in seen:
        assert coords.shape == (2, 3)
        assert coords.dtype == np.float32
        # The relay must preserve the actual values, not just shape/dtype --
        # nothing else in this file checks that. The fake source coordinates
        # are `np.full(..., float(step))`, so step -1's array is all -1.0;
        # a mutation that e.g. multiplied the relayed array by 0.0 would
        # pass every assertion above (0.0 has the right shape and dtype
        # too) while silently corrupting every frame the UI ever draws.
        # Checking step -1 specifically (not just step 0, whose expected
        # value already happens to be 0.0 and so can't tell a real zero
        # apart from an accidentally-zeroed one) is what actually catches
        # that.
        assert np.allclose(coords, float(st)), (
            f"relay must preserve coordinate values, got {coords} for step {st}")


def test_tap_intercepts_even_when_the_caller_passes_dump_fn_itself(monkeypatch):
    # The upstream patch makes Protenix.fold always pass dump_fn=. A tap using
    # setdefault would silently stop firing; this pins that it does not.
    mod = _fake_protenix(monkeypatch, fold_passes_dump_fn=True)
    seen = []
    handle = install_trajectory_tap(lambda s, st, c: seen.append(st))
    try:
        mod.fold()
    finally:
        remove_trajectory_tap(handle)
    assert seen == [-1, 0], "tap was pre-empted by the caller's own dump_fn"


def test_removing_the_tap_restores_the_original_function(monkeypatch):
    mod = _fake_protenix(monkeypatch)
    original = mod.edm_sample
    handle = install_trajectory_tap(lambda s, st, c: None)
    assert mod.edm_sample is not original
    remove_trajectory_tap(handle)
    assert mod.edm_sample is original


def test_removing_twice_is_harmless(monkeypatch):
    mod = _fake_protenix(monkeypatch)
    handle = install_trajectory_tap(lambda s, st, c: None)
    remove_trajectory_tap(handle)
    remove_trajectory_tap(handle)


def test_the_wrapped_function_still_returns_what_the_caller_expects(monkeypatch):
    mod = _fake_protenix(monkeypatch)
    handle = install_trajectory_tap(lambda s, st, c: None)
    try:
        assert mod.fold() == "coords-sentinel"
    finally:
        remove_trajectory_tap(handle)


def test_a_raising_callback_does_not_break_the_fold(monkeypatch):
    # A bug in the consumer must not take down a fold that is otherwise fine.
    mod = _fake_protenix(monkeypatch)

    def boom(sample, step, coords):
        raise ValueError("consumer bug")

    handle = install_trajectory_tap(boom)
    try:
        assert mod.fold() == "coords-sentinel"
    finally:
        remove_trajectory_tap(handle)
