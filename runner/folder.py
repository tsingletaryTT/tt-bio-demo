"""Run folds on a Tenstorrent device and turn them into protocol events.

Owns two expensive things and keeps them for the daemon's lifetime: the device
handle, and the loaded model. The spike measured a second fold at 4.36 s against
5.73 s for the first — residency is where that comes from. Opening the device
also writes ~40 lines of INFO to stderr each time, so doing it once matters for
log readability too.

On stages: tt-bio's own progress_fn only ever reports `trunk` and `diffusion`.
The other four values the protocol promises — msa, prep, confidence, saving —
are emitted here, bracketing the work this module does around the fold itself.
"""

import logging
import time

from runner.dump_tap import install_trajectory_tap, remove_trajectory_tap
from runner.shaping import frame_event, plddt_to_percent, select_frame_steps

log = logging.getLogger(__name__)


class FoldError(Exception):
    """A fold could not be completed. The message is for logs, never the screen."""


def fold_event_sequence(stages, frames, result, *, job_id, target_id, model,
                        card, n_residues):
    """Assemble the ordered event list for one completed fold.

    Pure: takes what a fold produced and returns what should go on the wire, so
    ordering and payload shape are testable without a device.
    """
    events = [{
        "type": "job_start", "job_id": job_id, "target_id": target_id,
        "model": model, "card": card, "n_residues": n_residues,
    }]
    for stage, frac in stages:
        events.append({"type": "stage", "job_id": job_id,
                       "stage": stage, "frac": float(frac)})
    events.extend(frames)
    events.append({
        "type": "job_done", "job_id": job_id,
        "cif_path": result["cif_path"],
        "wall_s": float(result["wall_s"]),
        "mean_plddt": plddt_to_percent(result["mean_plddt"]),
    })
    return events


class Folder:
    """Holds a device and a resident model, and folds one protein at a time."""

    def __init__(self, device_id=0, model="protenix-v2"):
        self.device_id = device_id
        self.model = model
        self._loaded = False

    def load(self):
        """Open the device and load model weights. Call once, at startup."""
        if self._loaded:
            return
        t0 = time.monotonic()
        # Imported here rather than at module scope: importing tt_bio pulls in
        # torch and ttnn, which the unit tests must not need.
        from tt_bio.tenstorrent import get_device
        self._device = get_device()
        self._loaded = True
        log.info("device %d open, model %s resident in %.2fs",
                 self.device_id, self.model, time.monotonic() - t0)

    def close(self):
        if not self._loaded:
            return
        from tt_bio.tenstorrent import cleanup
        cleanup()
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

        # Stages tt-bio does not report: emitted around the work we do.
        emit({"type": "stage", "job_id": job_id, "stage": "prep", "frac": 0.15})

        keep = set(select_frame_steps(n_step + 1, target=30))
        wall0 = time.monotonic()

        def on_frame(sample, step, coords):
            # step -1 is the initial noise draw; index it as 0 for the wire.
            index = step + 1
            if index in keep:
                emit(frame_event(job_id, step=index, total=n_step, coords=coords))

        def on_progress(stage, step=None, total=None):
            if total:
                frac = 0.4 if stage == "trunk" else 0.9
                emit({"type": "stage", "job_id": job_id, "stage": stage,
                      "frac": frac * (step / total)})

        handle = install_trajectory_tap(on_frame)
        try:
            result = self._run_fold(input_path, on_progress, n_step)
        except Exception as exc:
            raise FoldError(f"fold failed for {target_id}: {exc}") from exc
        finally:
            remove_trajectory_tap(handle)

        emit({"type": "stage", "job_id": job_id, "stage": "confidence", "frac": 0.95})
        emit({"type": "stage", "job_id": job_id, "stage": "saving", "frac": 0.99})
        emit({"type": "job_done", "job_id": job_id,
              "cif_path": result["cif_path"],
              "wall_s": time.monotonic() - wall0,
              "mean_plddt": plddt_to_percent(result["mean_plddt"])})

    def _run_fold(self, input_path, on_progress, n_step):
        """Invoke tt-bio. Returns {'cif_path': str, 'mean_plddt': float}.

        Kept separate so the event plumbing above can be read without tt-bio's
        API in the way, and so this is the only method an upgrade has to touch.
        """
        raise NotImplementedError(
            "wire this to tt-bio's predict path in Step 5, using the working "
            "invocation in tests/fixtures/streams/capture_real_fold.py"
        )
