"""Turn raw fold output into wire events.

Pure functions only — no device, no tt-bio import — so all of this is testable
without hardware.

Two of these encode findings from the Phase 3a spike:

* A real fold emits 201 denoising steps. The UI needs about 30; the rest are
  bandwidth for no visual gain. Note this is *not* about protecting the sampler
  from an expensive device-to-host copy — the spike established there isn't one,
  since coordinates are already host tensors between steps.
* tt-bio reports pLDDT as a fraction. The wire format says 0-100. Without the
  scale, `job_done.mean_plddt` silently reads 0.95 instead of 95.
"""

import numpy as np

from protocol.events import pack_coords

# The full vocabulary the protocol promises. tt-bio itself only ever reports
# `trunk` and `diffusion`; the other four are emitted by the daemon bracketing
# the work it does around the fold.
STAGE_ORDER = ("msa", "prep", "trunk", "diffusion", "confidence", "saving")


def select_frame_steps(total, target=30):
    """Pick ~`target` evenly spaced step indices out of `total`, keeping the ends.

    Fewer steps than the target keeps every one — we never invent frames.
    """
    if total <= 0:
        return []
    if total <= target:
        return list(range(total))
    picks = np.linspace(0, total - 1, target).round().astype(int)
    return sorted(set(int(p) for p in picks))


def frame_event(job_id, step, total, coords):
    """Build a `frame` event from one denoising step's coordinates."""
    arr = np.asarray(coords, dtype=np.float32).reshape(-1, 3)
    return {
        "type": "frame",
        "job_id": job_id,
        "step": int(step),
        "total": int(total),
        "n_atoms": int(arr.shape[0]),
        "coords_b64": pack_coords(arr),
    }


def plddt_to_percent(value):
    """Scale tt-bio's fractional pLDDT to the wire format's 0-100.

    Values already above 1.0 are passed through, so a future tt-bio that changes
    units cannot cause silent double-scaling.
    """
    v = float(value)
    return v * 100.0 if v <= 1.0 else v
