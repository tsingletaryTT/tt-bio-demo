"""Turn predicted structures into renderable geometry.

Pure numpy and gemmi — no GL, no GTK — so all of it is unit-testable.
"""

from dataclasses import dataclass

import gemmi
import numpy as np


class GeometryError(Exception):
    """A structure could not be read or converted to geometry."""


@dataclass
class BackboneTrace:
    """One anchor atom per residue, along every chain of a structure.

    Named for the backbone rather than for C-alpha because it is no longer
    a protein-only trace: a nucleic-acid residue has no C-alpha at all and
    is anchored on its phosphate instead (see `BACKBONE_ANCHORS`). The
    fields are unchanged, and so is their meaning -- one row per drawable
    residue, in file order, with the chain each row came from.
    """

    coords: np.ndarray      # (N, 3) float32
    plddt: np.ndarray       # (N,) float32, 0-100
    chain_ids: list

    @property
    def n_residues(self):
        return len(self.coords)


# The atom that stands for a residue's position on the backbone, tried in
# this order, PER RESIDUE -- not once per file and not once per chain.
#
# Why this exists: this booth folds DNA as well as protein (see
# examples/dna_dickerson.yaml), and a nucleic-acid residue contains no atom
# named "CA". A CA-only trace therefore found nothing in a DNA duplex, and
# `ribbon_from_cif` drew literally nothing for a fold that had otherwise
# completely succeeded -- 24 residues and 494 atoms of real structure,
# rendered as an empty screen.
#
# Why P, and why in this order:
#
#   "CA"  -- the C-alpha of an amino acid, the conventional protein trace.
#            FIRST on purpose. A handful of modified amino acids carry a
#            phosphate (phosphoserine, phosphothreonine) and so contain an
#            atom named "P" as well; they are still protein and must still
#            trace on their alpha carbon, which trying CA first guarantees
#            no matter what else the residue contains.
#
#   "P"    -- the phosphorus of the phosphate backbone, the conventional
#            nucleic-acid trace (it is what a "backbone" cartoon of DNA or
#            RNA follows in every structural viewer). It is ON the backbone
#            proper, so the two strands of a duplex come out as the two
#            visibly separate helical ribbons a visitor expects, rather
#            than a trace running nearer the helix axis. Measured on this
#            booth's own output: consecutive P atoms sit 5.96-7.16 A apart
#            (a protein's C-alphas sit ~3.8 A apart), which the 1.6 A
#            ribbon radius and the Catmull-Rom spline both handle fine.
#
#   "C1'"  -- the sugar's anomeric carbon, present in EVERY nucleotide
#            including one with no phosphate at all. This is the fallback
#            for a 5'-TERMINAL residue: the 5' end of a strand often
#            carries no phosphate group, and dropping that residue would
#            silently shorten every strand by one. (Not the case in this
#            booth's own output -- protenix-v2 writes a full 5'-phosphate,
#            OP3 included, so all 24 residues of the duplex anchor on P --
#            but it is the case for most experimental PDB entries, and the
#            cost of the fallback is one residue's anchor sitting ~5.1 A
#            off the phosphate line at one end of one strand, against
#            losing the residue entirely.)
#
# Anything with none of the three (a small-molecule ligand entity, a water)
# contributes no anchor at all and is skipped exactly as a CA-less residue
# always was -- which for a one-residue ligand chain means it is dropped by
# `ribbon_from_cif`'s too-short-to-spline rule, as it was before.
BACKBONE_ANCHORS = ("CA", "P", "C1'")


def _best_anchor_atom(residue):
    """Return the residue's best-supported backbone anchor, or None.

    Walks `BACKBONE_ANCHORS` in order and takes the first name the residue
    actually has -- so the choice is made per residue, which is what lets a
    protein/DNA complex trace its protein chains on C-alpha and its nucleic
    chains on phosphorus within one structure and one pass.

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
    for name in BACKBONE_ANCHORS:
        candidates = [atom for atom in residue if atom.name == name]
        if candidates:
            return min(candidates, key=lambda atom: (-atom.occ, atom.altloc))
    return None


def load_backbone_trace(cif_path):
    """Read one backbone anchor per residue, and its pLDDT, from a CIF file.

    The anchor is chosen per residue by `_best_anchor_atom`: C-alpha for an
    amino acid, the phosphate's P (or the sugar's C1') for a nucleotide.

    pLDDT is taken from the B-factor column, which is where AlphaFold-family
    predictors (including everything tt-bio serves) write per-residue confidence.
    """
    try:
        structure = gemmi.read_structure(str(cif_path))
    except Exception as exc:
        raise GeometryError(f"could not read {cif_path}: {exc}") from exc

    anchors = ", ".join(BACKBONE_ANCHORS)
    no_anchor_error = (
        f"{cif_path} contains no backbone anchor atoms ({anchors})")

    if len(structure) == 0:
        raise GeometryError(no_anchor_error)

    coords, plddt, chain_ids = [], [], []
    for chain in structure[0]:
        for residue in chain:
            atom = _best_anchor_atom(residue)
            if atom is None:
                continue
            coords.append([atom.pos.x, atom.pos.y, atom.pos.z])
            plddt.append(atom.b_iso)
            chain_ids.append(chain.name)

    if not coords:
        raise GeometryError(no_anchor_error)

    return BackboneTrace(
        coords=np.asarray(coords, dtype=np.float32),
        plddt=np.asarray(plddt, dtype=np.float32),
        chain_ids=chain_ids,
    )


# ── Curve and mesh construction ──────────────────────────────────────────

# AlphaFold's confidence ramp. Domain visitors read these colors fluently, so
# we use the convention rather than inventing a brand-consistent one.
#
# Public (no leading underscore) because the `?` help overlay explains this
# ramp to visitors and builds its swatches from THIS tuple rather than from a
# second, hand-copied list of hexes in ui/app.py -- a legend that can drift
# from the ribbon it describes is worse than no legend, and a hand-copy is
# exactly how that drift happens. tests/unit/test_app_interaction.py pins the
# two together.
PLDDT_STOPS = (
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

    # A duplicate point in the *middle* of the centerline is harmless --
    # central differencing bridges it using its two distinct neighbors on
    # either side, so the local tangent there is still well-defined. But a
    # duplicate at the very first or very last point collapses that
    # boundary's one-sided difference to a zero-length vector with no other
    # neighbor to borrow direction from. Normalizing that divides 0/0 into
    # NaN, and every later ring inherits it via parallel transport, so the
    # *entire* mesh comes back all-NaN with nothing but a RuntimeWarning to
    # show for it. A renderer must never hand that silently to OpenGL, so
    # fail loudly and specifically instead -- this is the same "can't derive
    # geometry from this input" situation as the n < 2 guard above.
    if lengths[0, 0] < 1e-9 or lengths[-1, 0] < 1e-9:
        raise GeometryError(
            "tube_mesh centerline has a duplicate leading or trailing "
            "point, so no initial sweep direction can be established"
        )

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
            # (a, b, d) and (b, e, d) wind each triangle so cross(v1-v0, v2-v0)
            # points outward, matching the vertex normals stored in `norms`
            # (radially outward by construction). Getting this backwards is
            # invisible in a unit test that only checks counts/shapes but
            # shows up as an inside-out or culled-away model once backface
            # culling is enabled downstream.
            indices += [a, b, d, b, e, d]

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
    for threshold, rgb in PLDDT_STOPS:
        mask = (v >= threshold) & ~claimed
        out[mask] = np.asarray(rgb, dtype=np.float32) / 255.0
        claimed |= mask
    return out


def _chain_runs(chain_ids):
    """Yield (chain_id, start, stop) for each contiguous run of one chain id.

    Grouped by *contiguity*, not by name: two residues carrying the same
    chain id but separated in the trace by a different chain are two
    physically separate pieces of backbone, and splining through the
    intervening chain is exactly the artifact this splitting exists to
    prevent. (gemmi hands us chains contiguously for every file we produce,
    so in practice this is one run per chain -- but the geometry is wrong,
    not merely surprising, if that ever stops being true.)
    """
    start = 0
    for i in range(1, len(chain_ids) + 1):
        if i == len(chain_ids) or chain_ids[i] != chain_ids[start]:
            yield chain_ids[start], start, i
            start = i


def ribbon_from_cif(cif_path, samples_per_segment=8, radius=1.6, sides=10):
    """Read a CIF and build everything the ribbon renderer needs.

    Returns (vertices, normals, colors, indices) with one color per vertex,
    interpolated from per-residue pLDDT along the spline.

    Each chain is splined and tubed *separately*, then the per-chain buffers
    are concatenated into the single set of arrays the renderer uploads. A
    single spline through every anchor of every chain would draw a tube leg
    from one chain's C-terminus to the next chain's N-terminus -- and since
    that gap is often an ordinary C-alpha--C-alpha distance, the spurious leg
    looks exactly like real backbone. On a complex (the interesting booth
    content) it is the difference between "two subunits" and "one impossible
    protein".

    A DNA duplex is the same rule with a louder failure: its two strands are
    anchored on phosphorus (see `BACKBONE_ANCHORS`), and on this booth's own
    Dickerson-Drew fold the two ends the spurious leg would join sit 18.8 A
    apart -- so the wrong answer there is not a subtle extra loop but a
    girder laid across the top of the double helix. `tests/unit/
    test_geometry_mesh.py` forbids geometry in that gap on the real fold.
    """
    trace = load_backbone_trace(cif_path)

    verts_parts, norms_parts, colors_parts, index_parts = [], [], [], []
    vertex_offset = 0  # running vertex count -- see the index rebase below

    for _chain_id, start, stop in _chain_runs(trace.chain_ids):
        coords = trace.coords[start:stop]

        # A chain with a single anchored residue has no centerline to sweep:
        # catmull_rom returns that lone point and tube_mesh rightly refuses
        # anything shorter than two points. Skip it -- deliberately not
        # fatal, and deliberately not a zero-length stub tube: one stray
        # single-residue chain (a ligand-like entity, a truncated subunit)
        # should not cost the visitor the rest of the structure, and the
        # alternative "draw something anyway" produces a degenerate ring
        # whose tangent is undefined, i.e. NaNs handed to OpenGL.
        #
        # Note the limit of that, because this comment used to overstate it:
        # this skip covers the SHORT-chain case only. It is not a general
        # "one bad chain cannot cost the whole render" guarantee -- tube_mesh
        # also raises GeometryError on a centerline whose leading or
        # trailing points are bit-exactly coincident, and that raise is not
        # caught here, so it aborts the whole ribbon (ui/app.py logs it and
        # leaves the last frame on screen; nothing crashes and nothing
        # reaches the visitor as an error). Left as-is on purpose after
        # review: two C-alphas at bit-exact identical coordinates do not
        # occur in real predicted structures, the cost if it ever happened
        # is one fold with no ribbon, and it is already loud in the log --
        # so the guard is not worth restructuring around a probability of
        # roughly zero. The comment is what was wrong, not the code.
        if len(coords) < 2:
            continue

        centerline = catmull_rom(coords, samples_per_segment)
        chain_verts, chain_norms, chain_indices = tube_mesh(
            centerline, radius=radius, sides=sides
        )

        # This chain's pLDDT, resampled against THIS chain's sample count --
        # not the whole structure's. Slicing the trace but resampling
        # globally (or vice versa) leaves every chain's colors shifted
        # relative to its own residues, which no shape or dtype check sees.
        along = resample_scalar(trace.plddt[start:stop], len(centerline))
        chain_colors = np.repeat(plddt_colors(along), sides, axis=0)

        verts_parts.append(chain_verts)
        norms_parts.append(chain_norms)
        colors_parts.append(chain_colors)

        # tube_mesh indexes each chain from 0, so every chain after the first
        # must be rebased by the number of vertices already emitted. Forget
        # this and the later chains' triangles silently redraw the first
        # chain's tube while their own vertices are never referenced.
        index_parts.append(chain_indices.astype(np.uint32) + np.uint32(vertex_offset))
        vertex_offset += len(chain_verts)

    if not verts_parts:
        # Every chain was too short to draw. Same class of failure as the
        # "no backbone anchor atoms" case in load_backbone_trace: there is no
        # geometry to be had, so say so with a GeometryError the UI already
        # knows how to present, rather than returning empty buffers a
        # renderer would happily and silently draw as nothing.
        raise GeometryError(
            f"{cif_path} has no chain with at least 2 anchored residues to draw"
        )

    return (
        np.concatenate(verts_parts).astype(np.float32),
        np.concatenate(norms_parts).astype(np.float32),
        np.concatenate(colors_parts).astype(np.float32),
        np.concatenate(index_parts).astype(np.uint32),
    )
