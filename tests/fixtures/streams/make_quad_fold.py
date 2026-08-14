"""Generate the quad_fold.jsonl fixture: four folds, four cards, interleaved.

`make_short_fold.py` is one fold on one card, and everything built on top of
it can be right about a single cell while being wrong about four. This is the
stream the quad view is developed and tested against: four folds started at
staggered offsets on cards 0-3, their `stage` and `frame` events interleaved,
their completions staggered, and -- the part no hand-built dict ever
remembers -- **the ordering pathology this project actually measured on the
live daemon**: fold N's `job_done` arriving AFTER fold N+1's `job_start` on
the same card (see ui/viewer.py's `_SUBJECT_*` comment and ui/slots.py's
deferred-terminal queue, both of which exist because of it).

Deliberately asymmetric. Four cells are a symmetric fixture by nature, and a
symmetric fixture is exactly the shape that hides an off-by-one in a slot
index: if every cell folds the same molecule for the same number of frames
with the same coordinates, a view that renders slot 2's data into cell 1
looks perfect. So every job here differs from every other in all three of
the things a confused index would swap:

  * the CARD it runs on,
  * its ATOM COUNT (20 / 107 / 187 / 223 / 24 -- all distinct), and
  * the SHAPE its coordinates converge to (bar / helix / ring / zigzag /
    duplex -- distinguishable by eye in a screenshot and by numpy in a test).

Deterministic: a seeded `default_rng`, drawn in a fixed job order that does
NOT depend on the interleave schedule below, so re-ordering the schedule
changes which events come out in which order but never changes a single
coordinate. `tests/unit/test_quad_fixture.py` re-runs this script and
byte-compares, so a fixture edited by hand instead of regenerated is caught.

Runs under venv-ui: stdlib + numpy, importing only `protocol.events`.

Usage: python3 tests/fixtures/streams/make_quad_fold.py [--out PATH]
"""

import argparse
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from protocol.events import PROTOCOL_VERSION, STAGE_BANDS, pack_coords

DEFAULT_OUT = pathlib.Path(__file__).with_name("quad_fold.jsonl")

# One entry per fold in the stream, in the order their noise is drawn from
# the rng (NOT the order they appear on the wire -- see the module docstring).
#
# `card` is the chip it runs on; note cards 0 gets TWO folds, which is what
# makes the deferred-clear pathology reproducible at all -- a card that only
# ever folds once can never start its next fold before its last one lands.
#
# `shape` names the geometry in `_target_coords` its coordinates converge to.
# `plddt` values are the real measured means from playlist/manifest.yaml's own
# notes (Trp-cage 95.3, FKBP12 50.8, DHFR 52.9, trypsin 39.5, DNA 95.7), so
# nothing in this fixture invents a confidence number.
JOBS = [
    # job_id  card  target_id   n_atoms  frames  shape       cif                              plddt  wall_s
    ("q0a", 0, "trpcage", 20, 4, "bar",
     "tests/fixtures/structures/real_fold_trpcage.cif", 95.3, 4.42),
    ("q1a", 1, "fkbp12", 107, 6, "helix",
     "tests/fixtures/structures/minimal.cif", 50.8, 11.70),
    ("q2a", 2, "dhfr", 187, 4, "ring",
     None, None, None),                                   # this one FAILS
    ("q3a", 3, "trypsin", 223, 7, "zigzag",
     "tests/fixtures/structures/minimal.cif", 39.5, 22.30),
    ("q0b", 0, "dna", 24, 3, "duplex",
     "tests/fixtures/structures/real_fold_dna_duplex.cif", 95.7, 4.62),
]

MODEL = "protenix-v2"
CARDS = [0, 1, 2, 3]

# Per-action wire delays, in the units MockRunner divides by `speed`. Frames
# dominate: 180ms is roughly what a real fold's diffusion callbacks arrive at
# on this booth, which keeps a replay watchable at speed=1 without being so
# slow that the unit tests (which replay at a high speed, or not at all --
# they read the file directly) are affected in any way.
DELAY_MS = {
    "hello": 0, "start": 40, "stage": 30, "frame": 180, "done": 40, "error": 40,
}


def _target_coords(shape, n_atoms):
    """The geometry a job's coordinates converge TO.

    Five visibly different figures, one per fold. This is the fixture's whole
    defence against a slot-index bug: `bar` is a straight line, `ring` is
    flat and closed, `helix` and `duplex` both twist but at different radii
    and opposite handedness, and `zigzag` is a square wave. No two of them
    have the same bounding box, the same extent along any axis, or the same
    number of points.
    """
    t = np.linspace(0.0, 1.0, n_atoms, dtype=np.float64)
    coords = np.zeros((n_atoms, 3), dtype=np.float64)
    if shape == "bar":
        coords[:, 0] = np.linspace(-12.0, 12.0, n_atoms)
    elif shape == "helix":
        coords[:, 0] = 7.0 * np.cos(t * 14.0)
        coords[:, 1] = 7.0 * np.sin(t * 14.0)
        coords[:, 2] = np.linspace(-16.0, 16.0, n_atoms)
    elif shape == "ring":
        coords[:, 0] = 11.0 * np.cos(t * 2.0 * np.pi)
        coords[:, 1] = 11.0 * np.sin(t * 2.0 * np.pi)
    elif shape == "zigzag":
        coords[:, 0] = np.linspace(-14.0, 14.0, n_atoms)
        coords[:, 1] = 9.0 * np.sign(np.sin(t * 34.0))
        coords[:, 2] = 3.0 * np.cos(t * 34.0)
    elif shape == "duplex":
        # Two antiparallel strands, opposite handedness from `helix`.
        strand = np.where(np.arange(n_atoms) % 2 == 0, 1.0, -1.0)
        coords[:, 0] = 4.5 * strand * np.cos(t * -9.0)
        coords[:, 1] = 4.5 * strand * np.sin(t * -9.0)
        coords[:, 2] = np.linspace(-10.0, 10.0, n_atoms)
    else:  # pragma: no cover - the table above is the only caller
        raise ValueError(f"unknown shape: {shape!r}")
    return coords.astype(np.float32)


# The wire order. Written out by hand rather than generated from a round-robin
# because the two things this fixture exists to reproduce are both ORDERING
# facts, and a generated interleave would make them accidental:
#
#   1. Four folds open at once (nothing completes until all four have
#      started), so a single global "latest frame" buffer cannot pass.
#   2. `("q0b", "start")` appears BEFORE `("q0a", "done")` -- card 0's second
#      fold is already streaming coordinates while its first fold's finished
#      ribbon has not landed yet.
#
# Each entry is (job_id, action); "stage:NAME" emits that stage, "frame"
# emits that job's next frame in sequence.
SCHEDULE = [
    ("q0a", "start"), ("q1a", "start"),
    ("q0a", "stage:msa"), ("q1a", "stage:msa"),
    ("q2a", "start"),
    ("q0a", "stage:prep"), ("q1a", "stage:prep"), ("q2a", "stage:msa"),
    ("q3a", "start"),                       # all four cards now in flight
    ("q0a", "stage:trunk"), ("q1a", "stage:trunk"), ("q2a", "stage:prep"),
    ("q3a", "stage:msa"),
    ("q0a", "stage:diffusion"),
    ("q0a", "frame"),
    ("q1a", "stage:diffusion"),
    ("q1a", "frame"),                       # adjacent frames, different jobs
    ("q0a", "frame"),
    ("q2a", "stage:trunk"), ("q3a", "stage:prep"),
    ("q2a", "stage:diffusion"),
    ("q2a", "frame"),
    ("q1a", "frame"),
    ("q3a", "stage:trunk"),
    ("q0a", "frame"),
    ("q3a", "stage:diffusion"),
    ("q3a", "frame"),
    ("q2a", "frame"),
    ("q0a", "frame"),                       # q0a's last frame
    ("q1a", "frame"),
    ("q0a", "stage:confidence"), ("q0a", "stage:saving"),
    ("q3a", "frame"),
    ("q2a", "frame"),
    # ---- the pathology -------------------------------------------------
    # Card 0's NEXT fold starts here, and streams its own noise, while q0a's
    # `job_done` is still in flight behind it.
    ("q0b", "start"),
    ("q0b", "stage:msa"), ("q0b", "stage:prep"),
    ("q1a", "frame"),
    ("q0b", "stage:trunk"),
    ("q3a", "frame"),
    ("q0b", "stage:diffusion"),
    ("q0b", "frame"),
    ("q0a", "done"),                        # <-- AFTER q0b's job_start
    # --------------------------------------------------------------------
    ("q2a", "frame"),                       # q2a's last frame
    ("q1a", "frame"),
    ("q2a", "error"),                       # the failure path, in the fixture
    ("q3a", "frame"),
    ("q0b", "frame"),
    ("q1a", "frame"),                       # q1a's last frame
    ("q1a", "stage:confidence"), ("q1a", "stage:saving"),
    ("q3a", "frame"),
    ("q1a", "done"),
    ("q0b", "frame"),                       # q0b's last frame
    ("q3a", "frame"),
    ("q0b", "stage:confidence"), ("q0b", "stage:saving"),
    ("q3a", "frame"),                       # q3a's last frame
    ("q0b", "done"),
    ("q3a", "stage:confidence"), ("q3a", "stage:saving"),
    ("q3a", "done"),
]


def build_events():
    """The full event list, `_delay_ms` included, ready to be written out."""
    rng = np.random.default_rng(20260813)

    # Drawn in JOBS order, before the schedule is walked at all, so the
    # coordinates are a function of the job table alone.
    spec, noise, targets = {}, {}, {}
    for job_id, card, target_id, n_atoms, frames, shape, cif, plddt, wall_s in JOBS:
        spec[job_id] = dict(card=card, target_id=target_id, n_atoms=n_atoms,
                            frames=frames, shape=shape, cif=cif,
                            plddt=plddt, wall_s=wall_s)
        # Scale the starting noise with the molecule so a big fold's first
        # frames are a big cloud, as they are on the wire.
        noise[job_id] = rng.normal(
            scale=9.0, size=(n_atoms, 3)).astype(np.float32)
        targets[job_id] = _target_coords(shape, n_atoms)

    events = [{"type": "hello", "version": PROTOCOL_VERSION, "cards": CARDS,
               "models": [MODEL], "preflight": "ok",
               "_delay_ms": DELAY_MS["hello"]}]

    emitted_frames = {job_id: 0 for job_id in spec}

    for job_id, action in SCHEDULE:
        job = spec[job_id]
        if action == "start":
            events.append({
                "type": "job_start", "job_id": job_id,
                "target_id": job["target_id"], "model": MODEL,
                "card": job["card"], "n_residues": job["n_atoms"],
                "_delay_ms": DELAY_MS["start"]})
        elif action.startswith("stage:"):
            stage = action.split(":", 1)[1]
            # The band's START: this event says the stage is now underway,
            # which keeps the whole-fold fraction monotonically
            # non-decreasing across the fold (see STAGE_BANDS' own comment on
            # why a per-stage 0->1 fraction makes the bar jump backward).
            events.append({
                "type": "stage", "job_id": job_id, "stage": stage,
                "frac": STAGE_BANDS[stage][0], "_delay_ms": DELAY_MS["stage"]})
        elif action == "frame":
            emitted_frames[job_id] += 1
            step = emitted_frames[job_id]
            total = job["frames"]
            t = step / total
            coords = noise[job_id] * (1.0 - t) + targets[job_id] * t
            events.append({
                "type": "frame", "job_id": job_id, "step": step,
                "total": total, "n_atoms": job["n_atoms"],
                "coords_b64": pack_coords(coords.astype(np.float32)),
                "_delay_ms": DELAY_MS["frame"]})
        elif action == "done":
            events.append({
                "type": "job_done", "job_id": job_id, "cif_path": job["cif"],
                "wall_s": job["wall_s"], "mean_plddt": job["plddt"],
                "_delay_ms": DELAY_MS["done"]})
        elif action == "error":
            events.append({
                "type": "job_error", "job_id": job_id,
                "target_id": job["target_id"],
                "message": "the worker holding this chip exited mid-fold",
                "_delay_ms": DELAY_MS["error"]})
        else:  # pragma: no cover - SCHEDULE is the only caller
            raise ValueError(f"unknown action: {action!r}")

    # Cheap self-checks, here rather than only in the test file: a generator
    # that can silently emit a job with no ending, or drop a job's last
    # frame, produces a fixture whose own tests then describe the wrong
    # stream. These cost nothing and fail at write time, where the fix is.
    for job_id, job in spec.items():
        assert emitted_frames[job_id] == job["frames"], (
            f"{job_id}: SCHEDULE emits {emitted_frames[job_id]} frames, "
            f"JOBS declares {job['frames']}")
    started = {e["job_id"] for e in events if e["type"] == "job_start"}
    ended = {e["job_id"] for e in events
             if e["type"] in ("job_done", "job_error")}
    assert started == ended == set(spec), (
        f"started={sorted(started)} ended={sorted(ended)}")

    return events


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT,
                        help="where to write the JSONL (default: %(default)s)")
    args = parser.parse_args(argv)
    events = build_events()
    args.out.write_text(
        "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events))
    print(f"wrote {args.out} ({len(events)} events)")


if __name__ == "__main__":
    main()
