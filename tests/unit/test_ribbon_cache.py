"""RibbonCache: server-side ribbon precompute via ui.cartoon.cartoon_from_cif,
off the dispatch thread, cached and packed for the wire."""
import base64
import pathlib
import struct
import time

import numpy as np

from webview.ribbon_cache import RibbonCache, unpack_float32_b64

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "structures"


class _FakeLink:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


def _wait_for(predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_a_real_cif_produces_a_ribbon_ready_event():
    link = _FakeLink()
    cache = RibbonCache(link)
    cache.on_job_done({"type": "job_done", "job_id": "j1",
                       "cif_path": str(FIXTURES / "real_fold_trpcage.cif"),
                       "mean_plddt": 95.3})
    assert _wait_for(lambda: len(link.published) == 1)
    event = link.published[0]
    assert event["type"] == "ribbon_ready"
    assert event["job_id"] == "j1"
    assert event["vertex_count"] > 0
    verts = unpack_float32_b64(event["vertices_b64"])
    assert len(verts) == event["vertex_count"] * 3
    cache.shutdown()


def test_get_returns_the_cached_buffers_for_an_on_demand_request():
    link = _FakeLink()
    cache = RibbonCache(link)
    cache.on_job_done({"type": "job_done", "job_id": "j2",
                       "cif_path": str(FIXTURES / "real_fold_trpcage.cif")})
    assert _wait_for(lambda: cache.get("j2") is not None)
    assert cache.get("does-not-exist") is None
    cache.shutdown()


def test_a_cif_with_no_drawable_backbone_publishes_nothing_and_does_not_crash():
    """Review Focus: cartoon_from_cif raising GeometryError (a ligand-only
    or malformed structure) must degrade to 'no ribbon', never a crash."""
    import pathlib as _p
    bad_cif = _p.Path("/tmp/ribbon_cache_test_empty.cif")
    bad_cif.write_text("data_empty\n#\n")
    link = _FakeLink()
    cache = RibbonCache(link)
    cache.on_job_done({"type": "job_done", "job_id": "j3", "cif_path": str(bad_cif)})
    time.sleep(0.3)  # give the worker a chance; nothing should ever arrive
    assert link.published == []
    assert cache.get("j3") is None
    cache.shutdown()
    bad_cif.unlink()


def test_a_job_done_with_no_cif_path_is_ignored():
    link = _FakeLink()
    cache = RibbonCache(link)
    cache.on_job_done({"type": "job_done", "job_id": "j4"})
    time.sleep(0.1)
    assert link.published == []
    cache.shutdown()


def test_the_cache_evicts_the_oldest_entry_past_max_size():
    link = _FakeLink()
    cache = RibbonCache(link, max_size=2)
    for i in range(3):
        cache.on_job_done({"type": "job_done", "job_id": f"j{i}",
                           "cif_path": str(FIXTURES / "real_fold_trpcage.cif")})
    assert _wait_for(lambda: cache.get("j2") is not None)
    assert cache.get("j0") is None, "the oldest entry must have been evicted"
    # Review Focus (Finding 3): eviction bounds only the on-demand cache
    # replay (GET /ribbon/<job_id>), never LIVE delivery. j0's build can
    # finish (and its ribbon_ready is published) AFTER its slot has
    # already been evicted by j2's reservation -- a future change that
    # accidentally gated publish() on "still in cache" would pass every
    # assertion above while silently breaking live delivery for exactly
    # this case, so it is pinned explicitly here.
    assert _wait_for(lambda: any(
        e.get("type") == "ribbon_ready" and e.get("job_id") == "j0"
        for e in link.published)), (
        "j0's ribbon_ready must still reach live subscribers even though "
        "its cache slot was evicted before (or while) its build finished")
    cache.shutdown()


def test_a_bug_in_the_packing_tail_is_logged_not_silently_swallowed(monkeypatch, caplog):
    """Review Focus: _build's tail (everything AFTER cartoon_from_cif
    succeeds -- packing, the cache write, publish()) is fire-and-forgotten
    to a ThreadPoolExecutor with no `.result()`/`add_done_callback` call.
    A real bug there (as opposed to an undrawable structure) must be
    logged loudly, not silently absorbed into an unretrieved Future."""
    import webview.ribbon_cache as ribbon_cache_module

    def _boom(_array):
        raise ValueError("boom: a real bug in the packing path")

    monkeypatch.setattr(ribbon_cache_module, "pack_float32_b64", _boom)
    link = _FakeLink()
    cache = RibbonCache(link)
    with caplog.at_level("ERROR", logger="webview.ribbon_cache"):
        cache.on_job_done({"type": "job_done", "job_id": "j-boom",
                           "cif_path": str(FIXTURES / "real_fold_trpcage.cif")})
        assert _wait_for(lambda: len(caplog.records) > 0)
    record = caplog.records[-1]
    assert record.levelname == "ERROR"
    assert "j-boom" in record.getMessage()
    assert record.exc_info is not None
    assert "boom" in str(record.exc_info[1])
    # No half-built event ever reached a subscriber, and the failed job's
    # placeholder was cleaned up rather than left stuck as a permanent
    # cache slot.
    assert link.published == []
    assert cache.get("j-boom") is None
    cache.shutdown()
