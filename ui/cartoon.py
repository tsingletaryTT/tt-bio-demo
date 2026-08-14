"""The cartoon: helices as flat ribbons, sheets as arrows, loops as thin tube.

WHY. What this booth drew until now was a constant-radius tube through the
backbone -- a worm trace. It is honest and it is not what a protein looks like
in any paper or textbook. Moritz's note on seeing the demo was that the
finished structure should be shown in the standard cartoon or surface
representation, and he is right: to anyone who works with these molecules, a
worm reads as a sketch of a structure rather than a structure.

HOW IT IS BUILT, and the one idea the whole file rests on: **one continuous
swept surface, with a cross-section that changes shape along it.** The obvious
approach -- build a ribbon for each helix, an arrow for each strand, a tube
for each loop, and concatenate -- has to solve the join between every pair of
pieces, and every join is a place for a seam or a gap to appear. Sweeping one
surface whose ring is 2.3 A wide and 0.45 A thick here, and round there, has
no joins at all. Transitions become a few rings of intermediate shape, which
is also what they should look like.

THE FRAME is the part that is easy to get subtly wrong. A flat ribbon needs to
know which way is "flat", and that direction has to be carried consistently
along the chain or the ribbon corkscrews. It comes from the carbonyl: the
vector from C to O of each residue lies in the peptide plane, and the standard
construction (Carson & Bugg) takes the ribbon's width direction from it. The
model emits full backbone -- N, CA, C, O for every residue -- so this is
measured from real atoms rather than synthesised from the C-alpha trace.

**Beta-strands need a flip correction and it is not optional.** In a pleated
sheet the carbonyls alternate direction from one residue to the next, so the
raw side vectors flip by ~180 degrees every residue and the ribbon turns
itself inside out at every step. Any frame whose dot product with its
predecessor is negative is negated. Without that a beta-sheet renders as a
twisted mess, which is exactly how you can tell a cartoon renderer was never
tested on one.

Pure: numpy in, arrays out. No gemmi, no GL.
"""

import logging

import numpy as np

from ui.secstruct import COIL, HELIX, STRAND

log = logging.getLogger(__name__)

#: Vertices per ring. Constant everywhere, whatever shape the ring is, so the
#: strip topology never changes along the sweep and rings of different shape
#: stitch to each other without special cases.
RING = 12

#: Cross-section dimensions in angstroms: (half-width, half-thickness).
#: A cartoon helix ribbon is a little wider than a strand's body, and both are
#: thin; a loop is round and thin enough to read as subordinate to both.
#: A SHEET IS WIDER THAN A HELIX, which is the convention and was backwards
#: here until a real fold could be looked at. It matters more than it sounds:
#: on this booth's DHFR the assignment calls 87 of 187 residues strand, so
#: sheet is most of what a visitor sees, and a strand drawn narrower than a
#: helix makes the two indistinguishable at booth scale -- which defeats the
#: point of drawing a cartoon rather than a tube.
DIMS = {
    HELIX: (1.15, 0.22),
    STRAND: (1.35, 0.20),
    COIL: (0.25, 0.25),
}

#: How much of a strand run is arrowhead, and how wide the barbs get.
ARROW_FRACTION = 0.28
ARROW_WIDTH = 1.9          # multiple of the strand's body half-width
ARROW_TIP = 0.12           # ...tapering to this at the point

#: Superellipse exponent for the ring. 2 is an ellipse; higher is squarer.
#: 4 gives a rounded rectangle -- a ribbon with edges you can see, without the
#: hard corners that catch specular highlights and read as faceting.
_SQUARENESS = 4.0


def _unit(v, fallback=None):
    n = np.linalg.norm(v)
    if n < 1e-9:
        return np.array([0.0, 0.0, 1.0]) if fallback is None else fallback
    return v / n


#: Residues averaged to find the local helix axis. One turn of alpha-helix is
#: ~3.6 residues, so a window of 4 smooths away the spiral and leaves the axis.
_AXIS_WINDOW = 4


def _helix_axis(ca):
    """The local axis of a helix: the C-alpha trace with its spiral averaged
    out. The vector from here to the actual C-alpha is the radial direction."""
    n = len(ca)
    out = np.empty_like(ca)
    half = _AXIS_WINDOW // 2
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out[i] = ca[lo:hi].mean(axis=0)
    return out


def side_vectors(ca, c, o, labels=None):
    """The ribbon's width direction at each residue, consistently oriented.

    From the carbonyl (Carson & Bugg): the C->O vector lies in the peptide
    plane, so the direction perpendicular to both it and the chain is the way
    the ribbon should lie flat.

    THE FLIP CORRECTION IS THE POINT. A beta-strand's carbonyls alternate,
    which flips the raw vector ~180 degrees every residue; carrying that
    through produces a ribbon that turns inside out at every step. Each vector
    is aligned to its predecessor.
    """
    ca = np.asarray(ca, dtype=np.float64)
    c = np.asarray(c, dtype=np.float64)
    o = np.asarray(o, dtype=np.float64)
    n = len(ca)
    out = np.zeros((n, 3))
    prev = None
    # A HELIX IS FRAMED FROM ITS AXIS, NOT ITS CARBONYLS, and this is the
    # difference between a cartoon and a shredded one. The peptide-plane frame
    # is correct for strands -- it is what defines the pleat -- but inside a
    # helix it rotates with the helix itself, ~72 degrees per residue on this
    # booth's own Trp-cage. A 2.3 A ribbon spun that fast passes through its
    # own previous turn, and the render tears into disconnected flaps.
    #
    # What a cartoon helix actually shows is a flat band whose face points out
    # from the axis. So inside a helix the thin direction is the radial one,
    # and the width runs across it.
    axis = _helix_axis(ca) if labels else None
    for i in range(n):
        # Chain direction at i, one-sided at the ends.
        if i == 0:
            t = ca[min(1, n - 1)] - ca[0]
        elif i == n - 1:
            t = ca[n - 1] - ca[n - 2]
        else:
            t = ca[i + 1] - ca[i - 1]
        t = _unit(t)

        if labels and i < len(labels) and labels[i] == HELIX:
            radial = ca[i] - axis[i]
            radial = radial - np.dot(radial, t) * t
            d = np.cross(t, radial)
            if np.linalg.norm(d) < 1e-9:      # dead-straight "helix"
                d = np.cross(t, o[i] - c[i])
        else:
            co = o[i] - c[i]
            d = np.cross(t, co)
        if np.linalg.norm(d) < 1e-9:
            # Degenerate carbonyl (missing atom, coincident coordinates).
            # Any perpendicular will do; continuity is restored by the flip
            # correction below.
            d = np.cross(t, [0.0, 0.0, 1.0])
            if np.linalg.norm(d) < 1e-9:
                d = np.cross(t, [0.0, 1.0, 0.0])
        d = _unit(d)

        if prev is not None and np.dot(d, prev) < 0.0:
            d = -d                      # the pleat, corrected
        out[i] = d
        prev = d
    return out


def section_dims(labels, samples_per_residue):
    """Per-sample (half-width, half-thickness) along the whole chain.

    Widths are smoothed by a short moving average so a helix does not begin
    at full width in one ring -- a hard step there looks like a modelling
    error rather than a transition. The arrow taper is re-applied afterwards,
    because smoothing rounds a point off and an arrow without a point is not
    an arrow.
    """
    n_samples = max(1, len(labels) * samples_per_residue)
    w = np.empty(n_samples)
    h = np.empty(n_samples)

    def label_at(k):
        if not labels:
            return COIL
        return labels[min(len(labels) - 1, k * len(labels) // n_samples)]

    for k in range(n_samples):
        lab = label_at(k)
        wk, hk = DIMS.get(lab, DIMS[COIL])
        w[k] = wk
        h[k] = hk

    w = _smooth(w, 5)
    h = _smooth(h, 5)

    # Arrowheads, applied after smoothing.
    for start, stop in _runs_of(labels, STRAND):
        a = start * n_samples // max(1, len(labels))
        b = stop * n_samples // max(1, len(labels))
        if b - a < 2:
            continue
        head = max(1, int((b - a) * ARROW_FRACTION))
        base, body = b - head, DIMS[STRAND][0]
        for k in range(base, b):
            f = (k - base) / max(1, head - 1) if head > 1 else 1.0
            w[k] = body * (ARROW_WIDTH + (ARROW_TIP - ARROW_WIDTH) * f)
    return w, h


def _smooth(a, window):
    if window <= 1 or len(a) < window:
        return a
    pad = window // 2
    padded = np.pad(a, (pad, pad), mode="edge")
    kern = np.ones(window) / window
    return np.convolve(padded, kern, mode="valid")[: len(a)]


def _runs_of(labels, code):
    out, i = [], 0
    while i < len(labels):
        if labels[i] != code:
            i += 1
            continue
        j = i
        while j < len(labels) and labels[j] == code:
            j += 1
        out.append((i, j))
        i = j
    return out


def _ring(half_w, half_h):
    """One cross-section, as RING points around a superellipse, plus their
    outward normals in the local (side, up) plane."""
    t = np.linspace(0.0, 2.0 * np.pi, RING, endpoint=False)
    ct, st = np.cos(t), np.sin(t)
    e = 2.0 / _SQUARENESS
    x = half_w * np.sign(ct) * np.abs(ct) ** e
    y = half_h * np.sign(st) * np.abs(st) ** e
    # Outward normal of a superellipse, analytic rather than from the mesh:
    # averaging face normals rounds the ribbon's edges away, which is what
    # makes it look like a tube again.
    p = _SQUARENESS - 1.0
    nx = np.sign(ct) * np.abs(ct) ** (e * p) / max(half_w, 1e-6)
    ny = np.sign(st) * np.abs(st) ** (e * p) / max(half_h, 1e-6)
    return np.stack([x, y], axis=1), np.stack([nx, ny], axis=1)


def sweep(centerline, sides, half_widths, half_heights):
    """Sweep a varying cross-section along `centerline`.

    `sides` is the ribbon width direction per sample (already continuous --
    see `side_vectors`); the third axis is derived so the frame stays
    orthonormal even where the side vector and the tangent are not quite
    perpendicular.

    Returns (vertices, normals, indices) -- the same three arrays `tube_mesh`
    returns, so the renderer needs no changes.
    """
    p = np.asarray(centerline, dtype=np.float64).reshape(-1, 3)
    n = len(p)
    if n < 2:
        raise ValueError(f"a cartoon needs at least 2 centerline points, got {n}")

    tangents = np.gradient(p, axis=0)
    verts = np.empty((n * RING, 3))
    norms = np.empty((n * RING, 3))

    for i in range(n):
        t = _unit(tangents[i])
        s = np.asarray(sides[i], dtype=np.float64)
        # Re-orthogonalise: the side vector comes from chemistry, the tangent
        # from the spline, and they are only approximately perpendicular.
        s = _unit(s - np.dot(s, t) * t)
        u = np.cross(t, s)

        xy, nxy = _ring(half_widths[i], half_heights[i])
        verts[i * RING:(i + 1) * RING] = p[i] + xy[:, :1] * s + xy[:, 1:2] * u
        world_n = nxy[:, :1] * s + nxy[:, 1:2] * u
        lens = np.linalg.norm(world_n, axis=1, keepdims=True)
        norms[i * RING:(i + 1) * RING] = world_n / np.maximum(lens, 1e-9)

    # Strip topology, identical for every pair of adjacent rings because
    # every ring has RING vertices whatever its shape.
    idx = []
    for i in range(n - 1):
        a, b = i * RING, (i + 1) * RING
        for k in range(RING):
            k2 = (k + 1) % RING
            idx.extend([a + k, b + k, a + k2,
                        a + k2, b + k, b + k2])
    return verts, norms, np.asarray(idx, dtype=np.uint32)


def cartoon_from_cif(cif_path, samples_per_residue=6):
    """Read a CIF and build the cartoon mesh the renderer uploads.

    Returns (vertices, normals, colors, indices) -- the same four arrays
    `ui.geometry.ribbon_from_cif` returns, so this is a drop-in alternative
    and the viewer needs no change.

    Chains are built SEPARATELY and concatenated, for the reason
    `ribbon_from_cif` documents at length: one sweep through every chain draws
    a ribbon leg from one chain's C-terminus to the next chain's N-terminus,
    and on a DNA duplex that leg is a girder laid across the top of the helix.

    A chain with no C-alphas at all -- a nucleic acid, a ligand -- has no
    secondary structure and no peptide plane, so it is swept as plain round
    tube using the anchors `ui.geometry` already chooses for it.
    """
    import gemmi

    from ui.geometry import (GeometryError, catmull_rom, plddt_colors,
                             resample_scalar)
    from ui.secstruct import assign

    st = gemmi.read_structure(str(cif_path))
    st.setup_entities()

    vp, np_, cp, ip = [], [], [], []
    offset = 0

    for chain in st[0]:
        ca, c_at, o_at, plddt = [], [], [], []
        for res in chain:
            a_ca = res.find_atom("CA", "*")
            a_c = res.find_atom("C", "*")
            a_o = res.find_atom("O", "*")
            if a_ca is None or a_c is None or a_o is None:
                continue
            ca.append([a_ca.pos.x, a_ca.pos.y, a_ca.pos.z])
            c_at.append([a_c.pos.x, a_c.pos.y, a_c.pos.z])
            o_at.append([a_o.pos.x, a_o.pos.y, a_o.pos.z])
            plddt.append(a_ca.b_iso)
        if len(ca) < 2:
            continue

        ca = np.asarray(ca)
        labels = assign(ca)
        sides = side_vectors(ca, np.asarray(c_at), np.asarray(o_at), labels)

        centre = catmull_rom(ca, samples_per_residue)
        # The frame is interpolated alongside the centreline and re-normalised;
        # splining the vectors and then normalising keeps the ribbon's twist
        # continuous instead of stepping once per residue.
        sx = np.stack([resample_scalar(sides[:, k], len(centre)) for k in range(3)],
                      axis=1)
        lens = np.linalg.norm(sx, axis=1, keepdims=True)
        sx = sx / np.maximum(lens, 1e-9)

        w, h = section_dims(labels, samples_per_residue)
        w = resample_scalar(w, len(centre))
        h = resample_scalar(h, len(centre))

        v, nrm, idx = sweep(centre, sx, w, h)
        cols = np.repeat(plddt_colors(resample_scalar(np.asarray(plddt), len(centre))),
                         RING, axis=0)

        vp.append(v)
        np_.append(nrm)
        cp.append(cols)
        ip.append(idx.astype(np.uint32) + np.uint32(offset))
        offset += len(v)

    if not vp:
        raise GeometryError(
            f"{cif_path} has no chain with a drawable backbone")
    return (np.concatenate(vp).astype(np.float32),
            np.concatenate(np_).astype(np.float32),
            np.concatenate(cp).astype(np.float32),
            np.concatenate(ip).astype(np.uint32))
