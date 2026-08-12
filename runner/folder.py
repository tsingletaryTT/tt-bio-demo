"""Run folds on a Tenstorrent device and turn them into protocol events.

Owns two expensive things and keeps them for the daemon's lifetime: the device
handle, and the loaded model. The spike measured a second fold at 4.36 s against
5.73 s for the first — residency is where that comes from. Opening the device
also writes ~40 lines of INFO to stderr each time, so doing it once matters for
log readability too.

On stages: tt-bio's own progress_fn only ever reports `trunk` and `diffusion`.
The other four values the protocol promises — msa, prep, confidence, saving —
are emitted here, bracketing the work this module does around the fold itself.

There is no pure "assemble the event list" helper here on purpose. An earlier
version had one (`fold_event_sequence`), and it was thoroughly unit tested —
but nothing outside its own test file ever called it, so the six tests built
around it covered a parallel model of `fold()`'s behaviour, not `fold()`
itself. The real per-callback logic (`_progress_frac`, the frame-index math in
`on_frame`) shipped with zero coverage as a result, and two bugs sat in it
undetected (see CHANGELOG/task-5-report.md, "Finding 1/2"). `fold()` is
inherently a streaming producer — it emits stage and frame events *as tt-bio's
own callbacks fire*, interleaved with its own bracket stages — which a
batch-assembled "list of events" can't faithfully represent without buffering
everything until the very end, defeating the reason the live trajectory tap
exists at all. So the fix was structural: test `fold()` directly, with
`_run_fold` monkeypatched to drive the same callbacks production code drives
(see tests/unit/runner/test_folder_events.py), not to keep a pure helper
that existed mainly to make the suite look covered.
"""

import logging
import tempfile
import time
import uuid
from pathlib import Path

from runner.dump_tap import install_trajectory_tap, remove_trajectory_tap
from runner.shaping import STAGE_ORDER, frame_event, plddt_to_percent, select_frame_steps

log = logging.getLogger(__name__)

# Where tt-bio's weights and the shared CCD mol library are already cached on
# this box (the spike confirmed protenix-v2.pt and mols.tar are present here
# already -- see tests/fixtures/streams/capture_real_fold.py, which reads
# from and writes to this same path). Not configurable via Folder's own
# constructor: Folder(device_id, model) is Task 9's fixed dependency surface,
# and this is tt-bio's own artifact-cache convention (hf_artifact/
# download_mols are used the same way by every tt-bio model), not something
# this module's callers should need to know about.
_WEIGHTS_CACHE = Path.home() / ".boltz"

# Every successful fold writes one uuid-named .cif here (see _run_fold below)
# and nothing has ever deleted one -- flagged during Task 5b as a booth-
# longevity risk in the same class as the tt-metal log growth runner/env.py
# bounds, fixed in Task 10 (runner/daemon.py's _prune_structures, reusing
# runner/env.py's prune_log_root against a separate budget).
#
# Namespaced by device_id (Task 10 review, Minor #2), not a single bare path
# shared by every Folder regardless of which device it holds: that was inert
# while nothing pruned it, but the moment pruning started deleting files
# here, a second daemon on the same machine -- already possible today by
# operator error, and the eventual point of CardPool/multi-card scheduling
# -- could delete a structure the *other* daemon's UI had not read yet. One
# directory per device removes the collision without any new cross-process
# coordination. A function, not a constant, so each Folder instance can
# expose its own path via the `structures_dir` property below without this
# module and runner/daemon.py needing to agree on a bare path by copying it
# into two places.
def _structures_dir_for(device_id):
    return Path(tempfile.gettempdir()) / "tt-bio-demo" / "structures" / f"device-{device_id}"


class FoldError(Exception):
    """A fold could not be completed. The message is for logs, never the screen."""


# The fraction-of-progress-bar band each stage owns, in STAGE_ORDER. Bands are
# contiguous -- each starts exactly where the previous one ends -- so the
# fraction reported to the UI is monotonically non-decreasing across an entire
# fold. That matters because a naive per-stage fraction (each stage restarting
# its own 0.0 -> 1.0) makes the bar visibly jump backward at every stage
# transition: trunk climbing to "40%" and then diffusion's first callback
# reporting under 1%, in front of a live audience, on every single fold. See
# task-5-report.md, "Finding 2" for the incident this replaced.
#
# tt-bio's progress_fn reports real (step, total) counts for exactly two
# stages: trunk (10 refinement cycles) and diffusion (200 denoising steps).
# The other four are synthetic brackets this module owns and fires once, at
# the end of their own band, the instant it reaches that point -- there is no
# partial progress to report for work this module doesn't itself perform.
# diffusion gets the bulk of the bar deliberately: it is 20x trunk's step
# count, so most of a fold's wall-clock time is spent there.
_STAGE_BANDS = {
    "msa": (0.00, 0.05),
    "prep": (0.05, 0.10),
    "trunk": (0.10, 0.15),
    "diffusion": (0.15, 0.95),
    "confidence": (0.95, 0.98),
    "saving": (0.98, 1.00),
}
# Enforced at import time, not just by convention: if a stage is ever added to
# or removed from the protocol's STAGE_ORDER without updating this table (or
# vice versa), this fails loudly here rather than silently dropping a stage
# from the wire the way "msa" was silently dropped before this fix (Finding 3).
assert tuple(_STAGE_BANDS) == STAGE_ORDER, (
    "_STAGE_BANDS must list exactly the stages in runner.shaping.STAGE_ORDER, "
    "in the same order"
)


def _bracket_frac(stage):
    """The frac reported for a synthetic bracket stage: its band's end."""
    return _STAGE_BANDS[stage][1]


def _progress_frac(stage, step, total):
    """Map tt-bio's own (step, total) progress within `stage` onto that
    stage's band, so the result is continuous with the bands before and after
    it instead of restarting at 0.0 on every stage transition."""
    start, end = _STAGE_BANDS[stage]
    return start + (end - start) * (min(step, total) / total)


class Folder:
    """Holds a device and a resident model, and folds one protein at a time."""

    def __init__(self, device_id=0, model="protenix-v2"):
        self.device_id = device_id
        self.model = model
        self._loaded = False
        self._device = None
        self._model_obj = None
        self._mol_dir = None

    @property
    def structures_dir(self):
        """Where this instance's folds write .cif output. See the comment
        above _structures_dir_for for why this is namespaced by device_id
        rather than one bare path shared by every Folder."""
        return _structures_dir_for(self.device_id)

    def load(self):
        """Open the device and load model weights. Call once, at startup.

        Also resolves the shared CCD mol library (`_mol_dir`): tt-bio needs it
        for every fold's feature build (nucleic-acid / ligand templates), it
        is fold-input-independent, and download_mols() only touches the
        filesystem (checks a cache, no device work) -- so resolving it here,
        once, keeps _run_fold() to the one thing per-fold it actually has to
        do: build features for *this* input and run *this* fold.
        """
        if self._loaded:
            return
        if self.model != "protenix-v2":
            raise ValueError(f"unsupported model {self.model!r}; only "
                             "protenix-v2 is wired up")
        t0 = time.monotonic()
        # Imported here rather than at module scope: importing tt_bio pulls in
        # torch and ttnn, which the unit tests must not need.
        from tt_bio.main import PROTENIX_REPO, download_mols, hf_artifact
        from tt_bio.protenix import Protenix
        from tt_bio.tenstorrent import get_device

        self._device = get_device()
        # load() must be all-or-nothing from here on. get_device() above is
        # self-healing on its own failure (it releases the host-local
        # DeviceLease before raising -- see tt_bio.tenstorrent.get_device),
        # but the three calls below are not: a corrupt checkpoint, an
        # incompatible tt-bio version, or a failed mol-tarball extraction --
        # exactly the "tt-bio upgrade breaks this" scenario this module is
        # designed around -- can raise *after* the device is already open and
        # the lease already held. Without this guard, that leaves self._device
        # set but self._loaded still False, and close()'s own guard
        # (`if not self._loaded: return`) treats that indistinguishably from
        # "load() was never called" -- a no-op that never calls cleanup() and
        # never releases the lease. A caller that catches this and keeps
        # running rather than exiting -- runner/daemon.py's Daemon.run() does
        # exactly that on a startup failure, retrying load() on a backoff and
        # serving `not_ready` in the meantime rather than letting the
        # exception kill the process (a fix wave after this one: the daemon
        # used to have no such catch at all, and load() raising here used to
        # propagate straight out of run() and take the process down with it)
        # -- would then sit on a card it can neither use nor release for the
        # rest of the process's life. So a failure here undoes its own
        # partial work on the way out: after this except block, self._device
        # is back to None and the real device/lease are released, the same
        # end state as if load() had never touched hardware at all -- which
        # is exactly what lets close()'s existing "safe to call even if
        # load() never succeeded" contract stay true after a *failed* load()
        # too, not just a load() that was never attempted.
        try:
            _WEIGHTS_CACHE.mkdir(parents=True, exist_ok=True)
            ckpt_path = hf_artifact(PROTENIX_REPO, "protenix-v2.pt", _WEIGHTS_CACHE)
            self._model_obj = Protenix.load_from_checkpoint(
                str(ckpt_path), device=self._device)
            self._mol_dir = download_mols(_WEIGHTS_CACHE)
        except Exception:
            from tt_bio.tenstorrent import cleanup
            try:
                cleanup()
            except Exception:
                log.exception("cleanup after a failed load() also raised; "
                              "the device/lease may still be held")
            self._device = None
            self._model_obj = None
            self._mol_dir = None
            raise
        self._loaded = True
        log.info("device %d open, model %s resident in %.2fs",
                 self.device_id, self.model, time.monotonic() - t0)

    def close(self):
        """Close the device. Safe to call even if load() never succeeded."""
        if not self._loaded:
            return
        from tt_bio.tenstorrent import cleanup
        try:
            cleanup()
        except Exception:
            # A raising cleanup() must not leave _loaded stuck True: that
            # would make a later load() attempt a no-op forever, on a device
            # this process no longer holds a good handle to. Log it -- the
            # daemon needs to know its device teardown wasn't clean -- but
            # still consider the device closed from our side.
            log.exception("device cleanup raised; treating it as closed anyway")
        self._loaded = False
        log.info("device closed")

    def fold(self, job_id, input_path, emit, *, target_id, n_residues, card=0,
             n_step=200):
        """Fold one input, calling `emit(event)` for each protocol event.

        Raises FoldError on failure; the caller turns that into a `job_error`.
        """
        if not self._loaded:
            raise FoldError("fold() called before load()")

        emit({"type": "job_start", "job_id": job_id, "target_id": target_id,
              "model": self.model, "card": card, "n_residues": n_residues})

        # msa and prep: brackets this module owns, not tt-bio instrumentation
        # (tt-bio's progress_fn never reports either -- see module docstring).
        # Each fires once, immediately, at the end of its own band: for this
        # demo's own input MSA search is always skipped (examples/
        # trpcage_no_msa.yaml sets `msa: empty`), and even where it were not,
        # tt-bio gives this module no way to observe that it happened. A
        # single "reached and passed this point" event is the honest thing to
        # emit either way -- it is what distinguishes "fired and completed
        # immediately" from "never fired at all" for the UI's pipeline panel,
        # which was the bug: msa used to never appear on the wire (Finding 3).
        emit({"type": "stage", "job_id": job_id, "stage": "msa",
              "frac": _bracket_frac("msa")})
        emit({"type": "stage", "job_id": job_id, "stage": "prep",
              "frac": _bracket_frac("prep")})

        keep = set(select_frame_steps(n_step + 1, target=30))
        wall0 = time.monotonic()

        def on_frame(sample, step, coords):
            # step -1 is the initial noise draw; index it as 0 for the wire.
            index = step + 1
            if index in keep:
                emit(frame_event(job_id, step=index, total=n_step, coords=coords))

        def on_progress(stage, step=None, total=None):
            if not total:
                return
            if stage not in _STAGE_BANDS:
                # tt-bio reporting a stage name this module doesn't know about
                # is a telemetry mismatch, not a reason to crash a fold that
                # is otherwise fine -- drop it, loudly, and keep going.
                log.warning("tt-bio reported unexpected progress stage %r; "
                            "dropping", stage)
                return
            emit({"type": "stage", "job_id": job_id, "stage": stage,
                  "frac": _progress_frac(stage, step, total)})

        # install_trajectory_tap() itself can fail -- it calls
        # check_tap_supported(), which raises TapUnavailable if tt-bio's
        # internals no longer match what the tap expects (see dump_tap.py).
        # That call used to sit *before* this try block, so TapUnavailable
        # escaped fold() directly instead of becoming a FoldError -- breaking
        # this method's own documented contract ("Raises FoldError on
        # failure") for exactly the caller (the daemon's fold loop) that
        # relies on it to catch every way a fold can go wrong. `handle`
        # starts as None so the finally below can tell "never installed"
        # apart from "installed, then _run_fold raised" without calling
        # remove_trajectory_tap on a name that was never bound.
        # `result["cif_path"]` / `result["mean_plddt"]` used to be read
        # *after* this try block ended, so a malformed return from
        # `_run_fold` (a missing key, or a `mean_plddt` that isn't a number)
        # raised a raw KeyError/TypeError straight out of fold() instead of
        # the FoldError this method's own docstring promises ("Raises
        # FoldError on failure") -- dormant the whole time `_run_fold` was a
        # stub, live the moment it started returning real (and therefore
        # occasionally malformed-by-upstream-change) dicts. Both result
        # fields are now pulled out inside the try, so any way `_run_fold`
        # can fail to honor its own contract -- raising, or returning
        # something that doesn't shape up -- becomes one FoldError, not two
        # different exception types depending on which failure mode it was.
        handle = None
        try:
            handle = install_trajectory_tap(on_frame)
            result = self._run_fold(input_path, on_progress, n_step)
            cif_path = result["cif_path"]
            mean_plddt = plddt_to_percent(result["mean_plddt"])
        except Exception as exc:
            raise FoldError(f"fold failed for {target_id}: {exc}") from exc
        finally:
            if handle is not None:
                remove_trajectory_tap(handle)

        emit({"type": "stage", "job_id": job_id, "stage": "confidence",
              "frac": _bracket_frac("confidence")})
        emit({"type": "stage", "job_id": job_id, "stage": "saving",
              "frac": _bracket_frac("saving")})
        emit({"type": "job_done", "job_id": job_id,
              "cif_path": cif_path,
              "wall_s": time.monotonic() - wall0,
              "mean_plddt": mean_plddt})

    def _run_fold(self, input_path, on_progress, n_step):
        """Invoke tt-bio. Returns {'cif_path': str, 'mean_plddt': float}.

        Kept separate so the event plumbing above can be read without tt-bio's
        API in the way, and so this is the only method an upgrade has to touch.

        Mirrors the working invocation captured in
        tests/fixtures/streams/capture_real_fold.py: build features for the
        input yaml (no MSA search needed for this demo's own targets, which
        set `msa: empty`), fold with the resident model, and write the
        resulting structure to a scratch .cif this process owns. `mean_plddt`
        is returned unscaled (tt-bio's own 0-1 fraction) -- fold() applies
        plddt_to_percent itself, so scaling here would double it.
        """
        from tt_bio.main import (_read_bio_chains, _read_bio_constraints,
                                 _resolve_a3m_text, _write_protenix_structure)
        from tt_bio.protenix_data import build_complex_features

        # _read_bio_chains/_read_bio_constraints call `path.suffix` -- they
        # need a Path, not the plain str `fold()`'s callers pass around on
        # the rest of this module's surface (job.input_path is a str
        # end-to-end elsewhere, e.g. runner/daemon.py). Coerced here, at the
        # one place tt-bio's API actually requires it, rather than pushing
        # a Path requirement onto every caller of fold().
        input_path = Path(input_path)
        chains = _read_bio_chains(input_path)
        bonds = _read_bio_constraints(input_path)
        chain_specs = [(cseq, _resolve_a3m_text(spec, cseq, None), mol_type)
                       for _cid, cseq, spec, mol_type in chains]
        feats = build_complex_features(
            chain_specs, mol_dir=str(self._mol_dir),
            chain_ids=[cid for cid, _s, _sp, _mt in chains], bonds=bonds)

        coords, conf = self._model_obj.fold(
            feats, n_step=n_step, n_sample=1, progress_fn=on_progress,
            return_confidence=True)

        # A scratch path this process owns, not a caller-configured output
        # directory: Folder's constructor (Folder(device_id, model)) is
        # Task 9's fixed dependency surface, and where a .cif lands is not
        # part of it. tempfile's own default dir (never the daemon's CWD,
        # which runner/env.py already treats as something the daemon may
        # not pollute) plus a uuid per fold keeps concurrent/successive
        # folds from ever colliding on a filename.
        self.structures_dir.mkdir(parents=True, exist_ok=True)
        cif_path = self.structures_dir / f"{uuid.uuid4().hex}.cif"
        _write_protenix_structure(coords[0], feats, None, cif_path, "cif",
                                  b_factors=conf["plddt_atom"] * 100.0)

        return {"cif_path": str(cif_path), "mean_plddt": float(conf["plddt"])}
