"""RibbonCache: server-side ribbon precompute, off the dispatch thread.

On a relayed job_done carrying a real cif_path, submits ui.cartoon.
cartoon_from_cif (the exact function the GTK app already trusts) to a
small thread pool, caches the packed result, and publish()es a
ribbon_ready event. A late-joining tab gets the same cached buffers via
GET /ribbon/<job_id> (webview/bridge.py). See
docs/superpowers/specs/2026-09-24-webview-parity-design.md section 8.
"""
import base64
import collections
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_MAX_SIZE = 8
DEFAULT_MAX_WORKERS = 2


def pack_float32_b64(array):
    return base64.b64encode(np.asarray(array, dtype=np.float32).tobytes()).decode("ascii")


def pack_uint32_b64(array):
    return base64.b64encode(np.asarray(array, dtype=np.uint32).tobytes()).decode("ascii")


def unpack_float32_b64(encoded):
    return np.frombuffer(base64.b64decode(encoded), dtype=np.float32)


def unpack_uint32_b64(encoded):
    return np.frombuffer(base64.b64decode(encoded), dtype=np.uint32)


class RibbonCache:
    """Thread-safety note: `_build` runs on a `ThreadPoolExecutor` with
    (by default) 2 workers, so two jobs submitted close together in time
    -- entirely plausible on a 2+-chip booth, where two folds can finish
    within milliseconds of each other -- genuinely race to completion in
    either order. Eviction must therefore be driven by SUBMISSION order,
    not by completion order: `on_job_done` reserves each job's position
    in `self._cache` synchronously, on the calling (dispatch) thread,
    before ever handing it to the pool. A first implementation here
    evicted by completion order instead (an `OrderedDict` populated only
    once `_build` finished) and was measurably flaky -- about half the
    time, in a tight loop of 3 submissions against 2 workers, the
    "oldest" entry by submission order was still on its way to
    completing when a newer one finished first, so it was never the one
    actually evicted. `self._lock` guards every read and mutation of
    `self._cache` for the same reason.
    """

    def __init__(self, daemon_link, max_size=DEFAULT_MAX_SIZE,
                 max_workers=DEFAULT_MAX_WORKERS):
        self._daemon_link = daemon_link
        self._max_size = max_size
        # job_id -> event dict, or None for a job whose build has been
        # submitted but has not finished (or failed) yet. Ordered by
        # SUBMISSION, established the instant on_job_done reserves a slot
        # -- see the class docstring for why this must not be completion
        # order.
        self._cache = collections.OrderedDict()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers,
                                            thread_name_prefix="ribbon-cache")

    def on_job_done(self, event):
        cif_path = event.get("cif_path")
        job_id = event.get("job_id")
        if not cif_path or not job_id:
            return
        with self._lock:
            # Reserved now, synchronously, so this job's place in FIFO
            # order is fixed before any worker thread can race it.
            self._cache[job_id] = None
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
        self._executor.submit(self._build, job_id, cif_path)

    def _build(self, job_id, cif_path):
        from ui.cartoon import cartoon_from_cif
        try:
            vertices, normals, colors, indices = cartoon_from_cif(cif_path)
        except Exception:
            # A ligand-only/nucleic-only/malformed structure has no
            # drawable ribbon -- the browser simply stays on its held
            # point cloud, exactly as if this feature did not exist for
            # this fold. Never a crash, never a half-built event.
            log.info("no ribbon for job %s (cif=%s); cartoon_from_cif could "
                     "not build one", job_id, cif_path, exc_info=True)
            with self._lock:
                # Only clear OUR OWN reservation. A slow build for a
                # long-evicted job_id must not delete some entirely
                # different, later job that happens to reuse the id (it
                # never does in practice -- job ids are unique -- but
                # `is None` rather than a blind `del` keeps this correct
                # even if that ever changed) nor resurrect a slot some
                # newer submission has already reused.
                if self._cache.get(job_id, "missing") is None:
                    del self._cache[job_id]
            return
        result = {
            "type": "ribbon_ready", "job_id": job_id,
            "vertices_b64": pack_float32_b64(vertices),
            "normals_b64": pack_float32_b64(normals),
            "colors_b64": pack_float32_b64(colors),
            "indices_b64": pack_uint32_b64(indices),
            "vertex_count": len(vertices), "index_count": len(indices),
        }
        with self._lock:
            if job_id in self._cache:
                self._cache[job_id] = result
            # else: evicted (by max_size) before this build finished --
            # still a real, completed ribbon, so it is still delivered to
            # every LIVE subscriber below; only the on-demand replay for a
            # late joiner (GET /ribbon/<job_id>) is unavailable for it,
            # exactly as if this build had simply not been cached.
        self._daemon_link.publish(result)

    def get(self, job_id):
        with self._lock:
            return self._cache.get(job_id)

    def shutdown(self):
        self._executor.shutdown(wait=True, cancel_futures=True)
