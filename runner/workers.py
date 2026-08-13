"""What a worker is: device assignment, per-worker environment, and the
parent<->worker control vocabulary.

This module answers two questions the rest of multi-chip folding depends on:

1. Which physical chips exist, and which logical worker owns which one
   (``worker_specs``)?
2. Exactly what environment does a given worker's child process need, so
   that it opens the chip it was assigned and nothing else, and does not
   trample its siblings' logs or oversubscribe the host
   (``worker_environ``)?

Both questions are answered by calling into tt-bio's OWN worker machinery
(``tt_bio.runtime.detect_tenstorrent_devices``, ``tt_bio.runtime.build_local_workers``,
``tt_bio.main._build_worker_device_assignments``) rather than re-deriving any
of it here. Two things in particular are worth NOT reinventing:

- P300 mesh-graph-descriptor handling. A lone P300 chip is a custom
  topology; without the 1x1 Blackhole MGD the chip opens and then behaves
  strangely (per the multi-chip spec) -- and ``_build_worker_device_assignments``
  already carries this exactly right.
- Mutual exclusion between four workers opening a device at once.
  ``tt_bio.tenstorrent.get_device()`` already serializes device bring-up
  host-wide through a flock (``_device_init_lock``, because concurrent UMD
  device init can deadlock and bring a chip up "remote-only"). This module
  does not need its own lock; ``Folder.load()`` gets one for free.

Import discipline: ``tt_bio.runtime`` is ttnn/torch-free (measured: ~0.03s to
import, vs ~1.4s for ``tt_bio.main``, which pulls in both at module scope --
see the Task 1 brief's "one correction the plan carries"). Both are still
imported LAZILY here, inside the three ``_``-prefixed seam functions below,
so that importing this module never costs a single test an unwanted tt-bio
dependency, and so a test can replace the seams wholesale without a tt-bio
install at all (see ``test_the_module_imports_without_tt_bio``). Importing
``tt_bio.main`` does pull in ttnn, but merely importing ttnn opens no
device -- and the parent process calling ``worker_specs``/``worker_environ``
never opens one either; only the child worker does, in its OWN process,
handed a complete environment via ``Popen(env=...)`` before its interpreter
even starts. That is stronger than "set before the import."
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The cap handed to detect_tenstorrent_devices. Deliberately equal to the
# UI's MAX_SLOTS (four cells in the quad view) but a *different* constant in
# a different venv: the runner may not import ui.*, and the two constants
# answer different questions ("how many chips may we fold on" vs "how many
# cells fit on screen").
MAX_WORKERS = 4

# Events leave a worker on this dedicated, inherited fd -- never stdout.
# tt-metal writes to fd 1/2 from C++ during device bring-up and kernel
# compilation; an event stream sharing either would be shredded mid-line.
EVENT_FD = 3

# Namespace for parent<->worker control lines, which must never reach the
# UI socket (see is_control / runner/pool.py's reader loop, once it exists).
CONTROL_PREFIX = "worker."
CONTROL_READY = f"{CONTROL_PREFIX}ready"   # model resident, device open, ready for a first job
CONTROL_IDLE = f"{CONTROL_PREFIX}idle"     # finished a job (success or failure) -- the dispatch-readiness signal
CONTROL_FATAL = f"{CONTROL_PREFIX}fatal"   # about to exit; cannot serve again


class WorkerSpecError(Exception):
    """A worker specification could not be built.

    Wraps both of tt-bio's own failure modes so callers only need to catch
    one exception type: a requested device id that does not exist (a typo
    like ``--device_ids 7`` on a four-card box), and the degenerate case of
    no chips detected at all.
    """


@dataclass(frozen=True)
class WorkerSpec:
    """Everything one worker needs to know about the one chip it owns.

    ``card`` is the physical device index (what the UI and telemetry call
    "chip N"). ``visible_devices`` and ``logical_device_id`` are the two
    values that decide, together, which physical chip a worker's ttnn
    actually opens (see the module docstring on ``tt_bio.device_lease``:
    ``TT_VISIBLE_DEVICES`` picks the physical chip(s), ``get_device`` then
    opens logical id ``TT_BIO_LOGICAL_DEVICE_ID`` within that set, default
    0). Pinning one physical chip to ``visible_devices`` and always using
    logical id 0 is what makes "worker for chip 3" and "the chip that
    actually opens" the same chip -- the exact thing the hardware spike
    existed to verify (chip 1 pinned this way drew 33.0 W mid-fold against
    13-17 W idle on 0/2/3; no silent fallback to chip 0).
    """

    card: int
    label: str
    visible_devices: str
    logical_device_id: int
    mesh_graph_descriptor: str | None


def control(kind, **fields):
    """Build one control-line dict. A thin constructor, not a class, because
    every control line is forwarded as-is (see runner/worker.py, once it
    exists) -- there is nothing to validate beyond the type tag itself."""
    return {"type": kind, **fields}


def is_control(event):
    """True if `event` is a parent<->worker control line, not a protocol event.

    Used to strip control lines out of a worker's output before anything
    from `protocol.events` ever sees them -- a control line's `type` is
    deliberately outside `protocol.events.EVENT_TYPES` (see
    `test_control_lines_are_distinguishable_from_protocol_events`), so this
    is a simple prefix check, not a lookup against the protocol vocabulary.
    """
    kind = event.get("type") if isinstance(event, dict) else None
    return isinstance(kind, str) and kind.startswith(CONTROL_PREFIX)


# ---------------------------------------------------------------------------
# The tt-bio seam. Each function below does exactly one lazy import and one
# call-through -- nothing else -- so a test can replace any of the three
# (`monkeypatch.setattr(mod, "_detect_tenstorrent_devices", ...)`) without a
# tt-bio install, per the Task 1 brief ("the seam"). Do not add logic here;
# it belongs in worker_specs, where it can be exercised against the fakes.

def _detect_tenstorrent_devices(device_ids, num_devices, max_workers):
    from tt_bio.runtime import detect_tenstorrent_devices
    return detect_tenstorrent_devices(device_ids, num_devices, max_workers)


def _build_local_workers(accelerator, jobs, devices):
    from tt_bio.runtime import build_local_workers
    return build_local_workers(accelerator, jobs, devices)


def _worker_device_assignments(devices):
    # Lives on tt_bio.main, not tt_bio.runtime -- importing it pulls in ttnn
    # and torch (see the module docstring's import-discipline section), but
    # opens no device.
    from tt_bio.main import _build_worker_device_assignments
    return _build_worker_device_assignments(devices)


def worker_specs(device_ids=None, max_workers=MAX_WORKERS):
    """Build one WorkerSpec per chip this booth will fold on.

    Mirrors tt_bio.main._local_workers' own three-call sequence exactly
    (detect -> build_local_workers -> _build_worker_device_assignments) so
    this module answers "which chips, pinned how" the identical way tt-bio's
    own CLI does, rather than a parallel reimplementation that could drift.

    `num_devices` (tt-bio's "how many of the detected chips to use" knob) is
    passed as 0, meaning "all of them" -- worker_specs has no equivalent
    parameter of its own; the booth always wants every detected chip, capped
    only by `max_workers`. `device_ids`, when given, is passed straight
    through to detect_tenstorrent_devices UNFILTERED -- that is what turns a
    typo like "7" into a clear error instead of a silently-empty booth (see
    test_a_requested_device_list_is_passed_through_for_validation).
    """
    try:
        devices = _detect_tenstorrent_devices(device_ids, 0, max_workers)
    except ValueError as exc:
        raise WorkerSpecError(str(exc)) from exc

    if not devices:
        raise WorkerSpecError(
            "No Tenstorrent devices detected under /dev/tenstorrent; "
            "the booth needs at least one chip to fold on."
        )

    # One dummy job per device, exactly as tt_bio.main._local_workers does --
    # build_local_workers truncates to len(jobs), and this call must not lose
    # any of the devices detect_tenstorrent_devices already decided on.
    slots = _build_local_workers("tenstorrent", [object()] * len(devices), devices)
    assignments = _worker_device_assignments([int(slot.device_id) for slot in slots])

    specs = []
    for slot in slots:
        card = int(slot.device_id)
        assignment = assignments[card]
        specs.append(WorkerSpec(
            card=card,
            label=slot.label,
            visible_devices=str(assignment["visible_devices"]),
            logical_device_id=int(assignment["logical_device_id"]),
            # Only set for P300 chips that need the 1x1 MGD; absent from the
            # assignment dict entirely otherwise (see
            # tt_bio.main._build_worker_device_assignments), hence .get, not
            # an index that would KeyError on every non-P300 chip.
            mesh_graph_descriptor=assignment.get("mesh_graph_descriptor"),
        ))
    return specs


def worker_environ(spec, *, log_root, n_workers, base=None):
    """Build the environment one worker's child process should be launched with.

    `base` behaves exactly like runner/env.py's `runner_environ`: `None`
    means "start from the current process environment", an explicit dict
    (including `{}`) is used as-is and never mutated in place. Every
    variable below is filled in with `setdefault` -- an operator who set it
    deliberately keeps their choice -- with ONE deliberate exception:

    `TT_VISIBLE_DEVICES` is a plain assignment, never setdefault. It is the
    single variable that decides which physical chip a worker opens (see
    WorkerSpec's docstring and the hardware spike this design is built on),
    and `tt_bio.runtime.detect_tenstorrent_devices` itself *honours* an
    ambient `TT_VISIBLE_DEVICES` by narrowing its own enumeration to it (see
    that function's docstring). A stale value inherited from the parent's
    shell would therefore not just mispin one worker -- it would silently
    collapse every worker in the booth onto the same one chip. An operator
    who wants to constrain which chips the booth uses at all has
    `device_ids`/`worker_specs(device_ids=...)` for that; this function's
    caller has already decided which single chip THIS worker gets, and that
    decision must never lose to an ambient leftover.
    """
    env = dict(os.environ if base is None else base)

    env["TT_VISIBLE_DEVICES"] = spec.visible_devices
    env.setdefault("TT_BIO_LOGICAL_DEVICE_ID", str(spec.logical_device_id))
    if spec.mesh_graph_descriptor is not None:
        env.setdefault("TT_MESH_GRAPH_DESC_PATH", spec.mesh_graph_descriptor)

    # One log tree per card, not one shared tree for the whole booth: four
    # writers into one root makes a crash unattributable, and the pruner's
    # oldest-first sweep (runner/env.py's prune_log_root) would delete
    # another worker's evidence right out from under it.
    card_root = Path(log_root).resolve() / f"card-{spec.card}"
    env.setdefault("TT_METAL_LOGS_PATH", str(card_root))

    # Same reasoning as runner/env.py's runner_environ: Inspector's
    # mesh_workloads_log.yaml is opened once at device bring-up and held
    # open, appending, for the process's entire life -- unlinking it later
    # frees no space while that fd stays open. Four co-resident workers is
    # four of those. Nothing in this codebase reads Inspector's output.
    env.setdefault("TT_METAL_INSPECTOR", "0")

    # tt_bio.runtime.host_thread_cap documents this exact case: an external
    # launcher that runs one single-card fold per chip leaves each child
    # process seeing n_workers == 1 by default, sizing its torch/OMP/BLAS
    # pools to every core on the box -- and N co-resident workers doing that
    # oversubscribe the host N-fold. This IS that launcher, so it always
    # passes the real n_workers through rather than letting each worker
    # assume it has the box to itself.
    from tt_bio.runtime import HOST_THREAD_VARS, host_thread_cap
    cap = str(host_thread_cap(n_workers))
    for var in HOST_THREAD_VARS:
        env.setdefault(var, cap)

    return env
