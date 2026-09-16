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

# Hoisted to module scope (item 11 of the deferred-nits batch) because both
# `_colored` and `cartoon_from_cif` below need it in their own, separate
# function scopes -- each importing it locally would be the same redundant
# import twice, not two different imports. Safe to hoist: nothing in
# `ui.geometry` imports `ui.cartoon` back (no circularity), and it is
# already an unconditional module-scope dependency of `ui.pocket` and
# `ui.geometry` itself, so this costs nothing this module wasn't already
# pulling in transitively the moment either ran.
from ui.geometry import resample_scalar

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

#: Radius of the plain round tube a CA-less POLYMER chain is swept as -- a
#: nucleic acid, which has no secondary structure and no peptide plane to
#: build a ribbon frame from.
#:
#: 1.6 A is not a new choice: it is `ui.geometry.ribbon_from_cif`'s default,
#: which is what has drawn DNA and tRNA on this booth since the tube renderer
#: shipped. So teaching the cartoon to draw these chains changes WHICH code
#: draws them, not how they look -- deliberately, because the tube was already
#: the right picture for a molecule with no helices or sheets to show.
#:
#: NOT `DIMS[COIL]` (0.25 A): a loop is drawn thin so it reads as subordinate
#: to the helices and sheets around it, and a nucleic backbone is not
#: subordinate to anything -- in a protein/DNA complex it is half the subject.
NUCLEIC_RADIUS = 1.6

#: How much of a strand run is arrowhead, and how wide the barbs get.
ARROW_FRACTION = 0.28
ARROW_WIDTH = 1.9          # multiple of the strand's body half-width
ARROW_TIP = 0.12           # ...tapering to this at the point

#: Superellipse exponent for the ring. 2 is an ellipse; higher is squarer.
#: 4 gives a rounded rectangle -- a ribbon with edges you can see, without the
#: hard corners that catch specular highlights and read as faceting.
_SQUARENESS = 4.0

#: Additive brightness boost applied to every RGB channel of a residue's
#: pLDDT colour when `cartoon_from_cif`'s `highlight_residues` names it.
#:
#: WHY ADDITIVE, AND WHY HERE RATHER THAN A SHADER UNIFORM. `ui/shaders.py`'s
#: RIBBON_VERT/RIBBON_FRAG take exactly one colour per vertex and have no
#: spare attribute, uniform, or outline pass to carry a second "is this
#: emphasised" signal -- adding one would mean a new vertex attribute, a new
#: VBO layout, and a shader recompile for what is, underneath, a per-vertex
#: number this module already owns. `plddt_colors` already turns a per-
#: residue scalar into the vertex colour array every renderer uploads
#: as-is, so the least invasive way to add emphasis is to brighten THAT
#: array before it leaves this module -- no GL-side change at all, and the
#: existing fragment shader's lighting/rim/opacity math runs unmodified on
#: top of it.
#:
#: The pLDDT colour is not replaced: this is added to it and clipped to
#: [0, 1], so a residue's confidence colour stays legible underneath the
#: emphasis rather than being swapped for an unrelated highlight colour --
#: two different claims (confidence, proximity to a ligand) that must both
#: stay visible at once (spec section 6).
#:
#: 0.12 is small enough that every PLDDT_STOPS colour except the two
#: channels already at 1.0 in the "low" (0.50) stop stays under 1.0 after
#: the boost -- i.e. the boost is losslessly subtractable for a residue in
#: the "very high" or "confident" bands, which is what
#: tests/unit/test_cartoon.py's highlight fixture exercises. A "low"-band
#: highlighted residue still visibly brightens; it just cannot be
#: subtracted back to bit-exact equality once a channel has clipped, which
#: is a property of clipping near white, not a defect in the boost itself.
POCKET_HIGHLIGHT_BOOST = 0.12


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


def _colored(plddt_values, seqids, chain_id, n_samples, highlight_residues):
    """Per-sample RGB for one chain: the pLDDT ramp, plus an additive
    brightness boost for any residue named in `highlight_residues`.

    `plddt_values` and `seqids` are per-RESIDUE, one entry per anchored
    residue of this chain in file order -- both are resampled onto the
    same `n_samples` the rest of this chain's geometry already uses, the
    same way `plddt_colors(resample_scalar(...))` always was before this
    helper existed (see `ribbon_from_cif`'s own comment on why a chain must
    be resampled against its OWN sample count, never the whole structure's
    or another chain's).

    `highlight_residues` is a set of `(chain_id, seqid)` pairs -- the exact
    shape `ui.pocket.pocket_residues` returns -- or `None`/empty for no
    highlight at all, in which case this returns exactly what
    `plddt_colors` alone would have.
    """
    from ui.geometry import plddt_colors

    base = plddt_colors(resample_scalar(np.asarray(plddt_values), n_samples))

    if not highlight_residues:
        return base

    mask = np.asarray(
        [1.0 if (chain_id, seqid) in highlight_residues else 0.0
         for seqid in seqids], dtype=np.float64)
    boost = resample_scalar(mask, n_samples) * POCKET_HIGHLIGHT_BOOST

    return np.clip(base + boost[:, None], 0.0, 1.0).astype(np.float32)


def cartoon_from_cif(cif_path, samples_per_residue=6, highlight_residues=None):
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

    `highlight_residues`, when given, is a set of `(chain_id, residue_seqid)`
    pairs -- exactly `ui.pocket.pocket_residues`'s output shape -- naming
    residues (an affinity question's pocket) to emphasise on top of their
    existing pLDDT colour. The emphasis is ADDITIVE, never a replacement
    colour: see `POCKET_HIGHLIGHT_BOOST`. `None` (the default) is identical
    to every call made before this parameter existed -- an empty set changes
    nothing, for a caller that has computed a pocket and found it empty.

    The highlight mask is resampled the same continuous way pLDDT itself is
    (`_colored`), so it ramps smoothly between samples rather than stepping
    at a hard per-residue boundary: a residue immediately NEAR a highlighted
    one picks up a partial gradient boost rather than a clean on/off edge.
    This is deliberate, not a rounding artefact -- see
    tests/unit/test_cartoon.py's `test_highlighted_residues_keep_their_
    plddt_color_but_gain_emphasis` for the geometry that makes it visible.
    """
    import gemmi

    from ui.geometry import GeometryError, _best_anchor_atom, catmull_rom, tube_mesh
    from ui.secstruct import assign

    highlight_residues = highlight_residues or set()

    st = gemmi.read_structure(str(cif_path))
    st.setup_entities()

    vp, np_, cp, ip = [], [], [], []
    offset = 0

    for chain in st[0]:
        ca, c_at, o_at, plddt, seqids = [], [], [], [], []
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
            seqids.append(res.seqid.num)

        if len(ca) < 2:
            # No peptide plane here, so no ribbon -- but this may still be a
            # POLYMER worth drawing. `_best_anchor_atom` is the same per
            # residue choice ui.geometry makes (CA, then P, then C1'), so a
            # nucleic chain anchors on its phosphate and a ligand -- which has
            # none of the three -- yields nothing and is correctly skipped
            # here. Ligands are drawn separately (see ui/ligand.py); a ligand
            # swept as a tube through its own atoms would be a scribble.
            anchors, anchor_plddt, anchor_seqids = [], [], []
            for res in chain:
                atom = _best_anchor_atom(res)
                if atom is not None:
                    anchors.append([atom.pos.x, atom.pos.y, atom.pos.z])
                    anchor_plddt.append(atom.b_iso)
                    anchor_seqids.append(res.seqid.num)
            if len(anchors) < 2:
                continue

            centre = catmull_rom(np.asarray(anchors), samples_per_residue)
            v, nrm, idx = tube_mesh(centre, radius=NUCLEIC_RADIUS, sides=RING)
            cols = np.repeat(
                _colored(anchor_plddt, anchor_seqids, chain.name,
                         len(centre), highlight_residues),
                RING, axis=0)
            vp.append(v)
            np_.append(nrm)
            cp.append(cols)
            ip.append(idx.astype(np.uint32) + np.uint32(offset))
            offset += len(v)
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
        cols = np.repeat(
            _colored(plddt, seqids, chain.name, len(centre), highlight_residues),
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
