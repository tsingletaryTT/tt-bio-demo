"""Turn predicted structures into renderable geometry.

Pure numpy and gemmi — no GL, no GTK — so all of it is unit-testable.
"""

from dataclasses import dataclass

import gemmi
import numpy as np


class GeometryError(Exception):
    """A structure could not be read or converted to geometry."""


@dataclass
class CaTrace:
    """The C-alpha backbone of a predicted structure."""

    coords: np.ndarray      # (N, 3) float32
    plddt: np.ndarray       # (N,) float32, 0-100
    chain_ids: list

    @property
    def n_residues(self):
        return len(self.coords)


def _best_ca_atom(residue):
    """Return the residue's best-supported CA atom, or None if it has none.

    `residue.find_atom("CA", "*")` looks like an altloc wildcard but it
    isn't occupancy-aware: against gemmi 0.6.4 it returns the first CA
    encountered in file order, not the highest-occupancy conformer. For a
    multi-conformer CIF that silently produces an arbitrary position and
    pLDDT, indistinguishable from a clean single-conformer file. Select by
    occupancy explicitly instead.

    Ties (equal occupancy, including the common case of a single conformer
    with no altloc at all) are broken by altloc code ascending, so the
    choice is deterministic rather than an accident of file order.
    """
    candidates = [atom for atom in residue if atom.name == "CA"]
    if not candidates:
        return None
    return min(candidates, key=lambda atom: (-atom.occ, atom.altloc))


def load_ca_trace(cif_path):
    """Read C-alpha positions and per-residue pLDDT from a CIF file.

    pLDDT is taken from the B-factor column, which is where AlphaFold-family
    predictors (including everything tt-bio serves) write per-residue confidence.
    """
    try:
        structure = gemmi.read_structure(str(cif_path))
    except Exception as exc:
        raise GeometryError(f"could not read {cif_path}: {exc}") from exc

    no_ca_error = f"{cif_path} contains no C-alpha atoms"

    if len(structure) == 0:
        raise GeometryError(no_ca_error)

    coords, plddt, chain_ids = [], [], []
    for chain in structure[0]:
        for residue in chain:
            atom = _best_ca_atom(residue)
            if atom is None:
                continue
            coords.append([atom.pos.x, atom.pos.y, atom.pos.z])
            plddt.append(atom.b_iso)
            chain_ids.append(chain.name)

    if not coords:
        raise GeometryError(no_ca_error)

    return CaTrace(
        coords=np.asarray(coords, dtype=np.float32),
        plddt=np.asarray(plddt, dtype=np.float32),
        chain_ids=chain_ids,
    )


# ── Curve and mesh construction ──────────────────────────────────────────

# AlphaFold's confidence ramp. Domain visitors read these colors fluently, so
# we use the convention rather than inventing a brand-consistent one.
_PLDDT_STOPS = (
    (90.0, (0x00, 0x53, 0xD6)),   # very high
    (70.0, (0x65, 0xCB, 0xF3)),   # confident
    (50.0, (0xFF, 0xDB, 0x13)),   # low
    (0.0,  (0xFF, 0x7D, 0x45)),   # very low
)


def catmull_rom(points, samples_per_segment=8):
    """Sample a Catmull-Rom spline through every point of a polyline.

    Endpoints are duplicated so the curve spans the full polyline rather than
    starting at the second control point.
    """
    p = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(p) < 2:
        return p.astype(np.float32)

    ext = np.vstack([p[0], p, p[-1]])
    out = []
    for i in range(len(ext) - 3):
        p0, p1, p2, p3 = ext[i], ext[i + 1], ext[i + 2], ext[i + 3]
        for s in range(samples_per_segment):
            t = s / samples_per_segment
            t2, t3 = t * t, t * t * t
            out.append(0.5 * (
                (2.0 * p1)
                + (-p0 + p2) * t
                + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
                + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
            ))
    out.append(p[-1])
    return np.asarray(out, dtype=np.float32)


def tube_mesh(centerline, radius=1.6, sides=10):
    """Sweep a circular cross-section along a centerline into a closed tube.

    Uses parallel transport to carry the cross-section frame along the curve,
    which avoids the twisting that a fixed reference vector produces on curved
    backbones.
    """
    c = np.asarray(centerline, dtype=np.float64).reshape(-1, 3)
    n = len(c)
    if n < 2:
        raise GeometryError(f"a tube needs at least 2 centerline points, got {n}")

    tangents = np.gradient(c, axis=0)
    lengths = np.linalg.norm(tangents, axis=1, keepdims=True)
    tangents = tangents / np.maximum(lengths, 1e-9)

    # Seed the frame with any vector not parallel to the first tangent.
    ref = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(tangents[0], ref))) > 0.9:
        ref = np.array([0.0, 1.0, 0.0])
    normal = np.cross(tangents[0], ref)
    normal /= np.linalg.norm(normal)

    angles = np.linspace(0.0, 2.0 * np.pi, sides, endpoint=False)
    verts = np.zeros((n, sides, 3))
    norms = np.zeros((n, sides, 3))

    for i in range(n):
        if i > 0:
            # Parallel transport: project the previous normal perpendicular to
            # the new tangent instead of recomputing it from scratch.
            normal = normal - tangents[i] * np.dot(normal, tangents[i])
            length = np.linalg.norm(normal)
            if length < 1e-6:
                normal = np.cross(tangents[i], ref)
                length = np.linalg.norm(normal)
            normal = normal / length
        binormal = np.cross(tangents[i], normal)
        for j, a in enumerate(angles):
            direction = np.cos(a) * normal + np.sin(a) * binormal
            norms[i, j] = direction
            verts[i, j] = c[i] + radius * direction

    indices = []
    for i in range(n - 1):
        for j in range(sides):
            jn = (j + 1) % sides
            a = i * sides + j
            b = i * sides + jn
            d = (i + 1) * sides + j
            e = (i + 1) * sides + jn
            indices += [a, d, b, b, d, e]

    return (
        verts.reshape(-1, 3).astype(np.float32),
        norms.reshape(-1, 3).astype(np.float32),
        np.asarray(indices, dtype=np.uint32),
    )


def resample_scalar(values, n_out):
    """Stretch a per-residue scalar onto a denser (or sparser) sample count."""
    v = np.asarray(values, dtype=np.float64).ravel()
    if len(v) == 1:
        return np.full(n_out, v[0], dtype=np.float32)
    source = np.linspace(0.0, 1.0, len(v))
    target = np.linspace(0.0, 1.0, n_out)
    return np.interp(target, source, v).astype(np.float32)


def plddt_colors(plddt):
    """Map pLDDT values (0-100) to RGB in 0-1 using the AlphaFold ramp.

    Each residue is colored by the first (highest) threshold it clears. The
    stops are walked high-to-low with an explicit `claimed` mask marking
    which rows already have a color, rather than testing `out == 0` to infer
    "not yet colored" -- a zero-valued row is indistinguishable from an
    intentionally black ramp color under that test, and the stop order is
    exactly the kind of thing a future edit changes without noticing the
    coupling. Tracking "claimed" explicitly is correct regardless of what the
    ramp's colors are or what order the stops are listed in.
    """
    v = np.asarray(plddt, dtype=np.float64).ravel()
    out = np.zeros((len(v), 3), dtype=np.float32)
    claimed = np.zeros(len(v), dtype=bool)
    for threshold, rgb in _PLDDT_STOPS:
        mask = (v >= threshold) & ~claimed
        out[mask] = np.asarray(rgb, dtype=np.float32) / 255.0
        claimed |= mask
    return out
