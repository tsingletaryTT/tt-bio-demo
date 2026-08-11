"""Generate the short_fold.jsonl fixture: a synthetic 12-residue fold.

Coordinates start as noise and converge to a straight line, which is enough
to exercise the point-cloud renderer and the frame pipeline deterministically.
"""

import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from protocol.events import PROTOCOL_VERSION, pack_coords

OUT = pathlib.Path(__file__).with_name("short_fold.jsonl")
N_ATOMS = 12
N_FRAMES = 6

rng = np.random.default_rng(1234)
target = np.zeros((N_ATOMS, 3), dtype=np.float32)
target[:, 0] = np.linspace(-10.0, 10.0, N_ATOMS)
noise = rng.normal(scale=8.0, size=(N_ATOMS, 3)).astype(np.float32)

events = [
    {"type": "hello", "version": PROTOCOL_VERSION, "cards": [0, 1, 2, 3],
     "models": ["protenix-v2"], "preflight": "ok", "_delay_ms": 0},
    {"type": "job_start", "job_id": "j1", "target_id": "synthetic",
     "model": "protenix-v2", "card": 0, "n_residues": N_ATOMS, "_delay_ms": 50},
    {"type": "stage", "job_id": "j1", "stage": "trunk", "frac": 0.3, "_delay_ms": 50},
]

for i in range(N_FRAMES):
    t = (i + 1) / N_FRAMES
    coords = noise * (1.0 - t) + target * t
    events.append({
        "type": "frame", "job_id": "j1", "step": i + 1, "total": N_FRAMES,
        "n_atoms": N_ATOMS, "coords_b64": pack_coords(coords), "_delay_ms": 100,
    })

events += [
    {"type": "stage", "job_id": "j1", "stage": "confidence", "frac": 0.9, "_delay_ms": 50},
    {"type": "job_done", "job_id": "j1", "cif_path": "tests/fixtures/structures/minimal.cif",
     "wall_s": 1.25, "mean_plddt": 82.4, "_delay_ms": 50},
]

OUT.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events))
print(f"wrote {OUT} ({len(events)} events)")
