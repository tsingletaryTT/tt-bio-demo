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
    cache.shutdown()
