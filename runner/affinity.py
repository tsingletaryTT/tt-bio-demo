"""Answers affinity questions on a dedicated chip, resident, the same
model-residency pattern runner/folder.py's Folder uses for protenix-v2 --
see docs/superpowers/specs/2026-09-15-affinity-qa-design.md section 5 for
why this chip is never shared with folding.

Deliberately does NOT depend on any fold having happened: nesso1 scores from
the input file's sequence + ligand alone (confirmed in
docs/spike-nesso1-affinity.md section 3.4 -- run in a fresh process with
tt_bio.protenix/tt_bio.opendde never imported) -- the pocket visualization
that pairs with this score is computed entirely client-side, in
ui/pocket.py, from a .cif this module never sees.

Realistic per-question latency is ~8-12s even with the model held resident
across calls -- tt_bio.nesso1_input.prepare()'s host-side YAML/RDKit/
feature-tensor cost (~6-8s) is paid on EVERY call, repeat target or not (see
docs/spike-nesso1-affinity.md section 4). Do not size timeouts or UI
"in-flight" copy around an assumption that residency makes this near-instant
-- it makes the on-device forward pass warm (~2-4s), not the host prep.

Weights (nesso1, nesso1-ccd) are NOT bundled with tt-bio and are not fetched
by this module -- see load()'s docstring. Provisioning (postinst /
doctor.sh) picking these up alongside protenix-v2's is a flagged follow-up,
not something this task solves.
"""

import logging
import tempfile
from pathlib import Path

import torch

from tt_bio.nesso1 import DEFAULT_SEED, Nesso1
from tt_bio.nesso1_input import CLI_PREDICT_ARGS, collate, prepare

log = logging.getLogger(__name__)


# Namespaced by device_id, same reasoning as runner/folder.py's
# _structures_dir_for: a shared scratch dir is exactly what makes the ESM-2
# embedding cache (see module docstring / spike doc section 4) actually help
# on a repeat question for the same target, but two AffinityScorers on two
# different devices in the same process (or two daemons on one machine)
# writing prepare()'s parsed structures/conformers/embeddings into the same
# directory is the same cross-daemon collision Folder's own comment already
# worries about, one artifact type over.
def _affinity_out_dir(device_id):
    return Path(tempfile.gettempdir()) / "tt-bio-demo" / "affinity" / f"device-{device_id}"


class AffinityScorer:
    """Holds a device and a resident nesso1 model, and answers one affinity
    question at a time.

    Mirrors Folder's shape (device_id in the constructor, a load() that opens
    the device and loads weights once, a per-call method that emits protocol
    events via a callback) so runner/daemon.py can wire this up the same way
    it already wires up Folder.
    """

    def __init__(self, device_id=0):
        self.device_id = device_id
        self._loaded = False
        self._model = None  # set by load(); a tt_bio.nesso1.Nesso1 instance.

    def load(self):
        """Load nesso1, once, for this worker's lifetime. Opening the device
        is Nesso1.from_pretrained's own job (through tt_bio.tenstorrent.
        get_device(), which -- per this project's own 0.6.3 upgrade notes --
        already calls ensure_p300_mesh_descriptor() internally); no separate
        device-open step is needed here, mirroring how Folder.load() relies
        on get_device() alone. self.device_id selects the chip via
        TT_VISIBLE_DEVICES in this worker's own process environment (set by
        whatever spawns it), not a constructor argument threaded into this
        call.

        Weights are NOT bundled with tt-bio: confirmed missing on a fresh
        cache in the spike (docs/spike-nesso1-affinity.md section 2.1). This
        call does not fetch them -- provisioning (postinst / doctor.sh) is
        where `tt_bio.weights.fetch("nesso1")` and `.fetch("nesso1-ccd")`
        belong, the same place protenix-v2's weights are fetched. Calling
        this before those exist raises whatever tt-bio itself raises for a
        missing checkpoint; that is deliberately not swallowed here.
        """
        if self._loaded:
            return
        torch.set_grad_enabled(False)
        self._model = Nesso1.from_pretrained(use_tenstorrent=True)
        # screen()'s own override (see its module comment, and
        # docs/spike-nesso1-affinity.md section 2.2): routes triangle ops
        # through tt-bio's fused kernels rather than the CPU-only
        # cuEquivariance path the checkpoint's use_kernels: true would select.
        self._model.use_kernels = False
        self._model.predict_args.update(CLI_PREDICT_ARGS)
        self._loaded = True

    def score(self, question_id, target_id, input_path, emit):
        """Score one question. Never raises -- same rule as Folder.fold():
        a wedged or erroring Q&A worker must not crash the daemon thread
        driving it.

        Unlike Folder.fold(), a missing load() is not checked here as a
        precondition raise: `_score_real` (the one seam a caller might
        legitimately replace, e.g. in tests) is where "not loaded" turns
        into a caught exception -- an answer_error event, not a crash --
        because a Q&A worker answering a visitor's question mid-startup-
        race should degrade the same way any other _score_real failure
        does, not take down the thread driving it.
        """
        emit({"type": "answer_start", "question_id": question_id,
              "target_id": target_id})
        try:
            result = self._score_real(input_path)
        except Exception as exc:
            log.exception("question %s (target %s) failed", question_id,
                           target_id)
            emit({"type": "answer_error", "question_id": question_id,
                  "target_id": target_id, "message": str(exc)})
            return
        emit({"type": "answer_done", "question_id": question_id,
              "target_id": target_id, **result})

    def _score_real(self, input_path):
        """The actual nesso1 call, confirmed against real hardware in
        docs/spike-nesso1-affinity.md sections 2.2 and 3.2. Uses the
        lower-level prepare()/collate()/model.predict() path rather than
        screen() so the model loaded in load() stays resident across
        questions instead of being reloaded from the checkpoint every call
        (screen() itself calls Nesso1.from_pretrained() internally, which
        this module deliberately avoids repeating).

        Returns the two fields spec section 7's content-honesty rule cares
        about: affinity_probability_binary (a literal [0, 1] probability of
        being a binder -- safe to show a visitor verbatim, no unit claim
        needed) and affinity_pred_value (continuous; same log10(IC50 uM)
        scale as tt-bio's Boltz-2 affinity head per the spike's evidence,
        but NOT confirmed by an explicit tt-bio doc string -- so this is
        secondary/supporting detail in the UI, never the headline number,
        and its copy must hedge accordingly).
        """
        if not self._loaded:
            raise RuntimeError("score() called before load()")
        out_dir = _affinity_out_dir(self.device_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        dataset, _manifest, failed = prepare(Path(input_path), out_dir)
        if failed:
            raise ValueError(f"nesso1 could not parse {input_path!r}")
        feats = collate(dataset[0])
        torch.manual_seed(DEFAULT_SEED)  # pins RDKit's conformer draw --
        # screen()'s own docstring: without this, upstream repeats differ by
        # up to 0.058 in reported affinity.
        with torch.no_grad():
            pred = self._model.predict(feats)
        return {
            "score": float(pred["affinity_probability_binary"].reshape(-1)[0]),
            "affinity_pred_value": float(pred["affinity_pred_value"].reshape(-1)[0]),
        }
