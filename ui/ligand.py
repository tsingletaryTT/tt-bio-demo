"""The small molecules the booth already folds and used to throw away.

WHAT THIS IS FOR. Three of the five targets ship a bound ligand and have from
the beginning: FKBP12 with SB3, trypsin with benzamidine, and DHFR with
**methotrexate** -- the drug the gallery blurb for that card already names
("one of the oldest cancer drugs, methotrexate, is shaped to jam it shut").
All three are computed on every loop and then silently dropped, because
`ui.geometry` traces a backbone through C-alpha / P / C1' anchors and a
small molecule has none of them. The compute was already paid for; only the
drawing was missing.

Drawing them turns three cards from "a protein" into "a drug bound to its
target", which is the thing the demo is actually about.

BONDS ARE INFERRED FROM DISTANCE, not read from a chemistry library. tt-bio's
CCD cache does carry real connectivity, but it is an RDKit object living in
venv-runner, and venv-ui has no rdkit and should not grow one -- the split
between the two environments is what keeps the UI unable to be taken down by
the compute stack. Distance is enough, and that is measured rather than
assumed: methotrexate in 4DFR has 33 atoms and exactly 35 atom pairs within
bonding distance, against the 35 bonds RDKit reports for the same molecule
from the CCD. Same answer, no dependency.

COLOURS ARE BY ELEMENT, not by confidence. This is the one place the booth
deliberately steps outside its own confidence ramp, and it is a considered
exception: every chemist reads CPK colouring without being told, a ligand is
a handful of atoms rather than a chain of residues, and the per-residue
confidence the ramp shows does not mean the same thing for a small molecule.
The legend in the UI describes the ramp for the *structure*; the ligand is
visibly a different kind of object and is coloured as one.

Pure: numpy in, arrays out. No gemmi, no GL, no rdkit.
"""

import logging

import numpy as np

log = logging.getLogger(__name__)

#: Covalent radii in angstroms (Cordero et al. 2008), for the elements that
#: turn up in drug-like ligands and the ions that turn up beside them. A flat
#: distance cutoff mis-handles sulfur -- an S-S bond is 2.05 A, longer than
#: any C-C -- so the cutoff is derived per pair instead.
_RADII = {
    "H": 0.31, "C": 0.76, "N": 0.71, "O": 0.66, "F": 0.57,
    "P": 1.07, "S": 1.05, "CL": 1.02, "BR": 1.20, "I": 1.39,
    "B": 0.84, "SE": 1.20,
}
_DEFAULT_RADIUS = 1.20          # unknown element: be generous rather than silent

#: Slack added to the sum of covalent radii before two atoms count as bonded.
#: 0.45 A is the usual choice and it reproduces methotrexate's connectivity
#: exactly -- see the module docstring.
_BOND_SLACK = 0.45

#: CPK, the convention every chemist already reads.
_ELEMENT_COLOURS = {
    "C": (0.62, 0.66, 0.67), "N": (0.19, 0.31, 0.97), "O": (0.94, 0.26, 0.21),
    "S": (0.96, 0.80, 0.22), "P": (0.98, 0.55, 0.15), "F": (0.35, 0.85, 0.42),
    "CL": (0.35, 0.85, 0.42), "BR": (0.61, 0.31, 0.15), "I": (0.58, 0.31, 0.72),
    "H": (0.92, 0.92, 0.92),
    # Ions that appear beside a ligand and are not one.
    "ZN": (0.49, 0.50, 0.69), "MG": (0.54, 1.00, 0.00), "CA": (0.24, 1.00, 0.00),
    "NA": (0.67, 0.36, 0.95), "K": (0.56, 0.25, 0.83), "FE": (0.88, 0.40, 0.20),
    "MN": (0.61, 0.48, 0.78), "CU": (0.78, 0.50, 0.20),
}
_DEFAULT_COLOUR = (0.85, 0.45, 0.85)     # loud on purpose: an unnamed element

ATOM_RADIUS = 0.26      # ball-and-stick, not space-filling: the protein is
BOND_RADIUS = 0.13      # the subject and the ligand must not swamp its pocket


def element_colour(symbol):
    return _ELEMENT_COLOURS.get((symbol or "").upper(), _DEFAULT_COLOUR)


def infer_bonds(positions, elements, slack=_BOND_SLACK):
    """Which atoms are bonded, from geometry alone.

    Two atoms are bonded when they are closer than the sum of their covalent
    radii plus `slack`. Per-pair rather than a flat cutoff so sulfur and
    phosphorus behave.

    Ions are not bonded to anything: a lone Zn or Cl sitting in a pocket is
    near the protein, not covalently attached to it, and drawing a stick to it
    would be a chemical claim that is simply false.
    """
    p = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    n = len(p)
    if n < 2:
        return []
    radii = np.array([_RADII.get((e or "").upper(), _DEFAULT_RADIUS) for e in elements])
    bonds = []
    for i in range(n):
        for j in range(i + 1, n):
            limit = radii[i] + radii[j] + slack
            if float(np.linalg.norm(p[i] - p[j])) <= limit:
                bonds.append((i, j))
    return bonds


def _sphere(centre, radius, rings=6, sectors=10):
    verts, norms, idx = [], [], []
    for r in range(rings + 1):
        phi = np.pi * r / rings
        for s in range(sectors):
            th = 2.0 * np.pi * s / sectors
            nx = np.sin(phi) * np.cos(th)
            ny = np.cos(phi)
            nz = np.sin(phi) * np.sin(th)
            norms.append([nx, ny, nz])
            verts.append(centre + radius * np.array([nx, ny, nz]))
    for r in range(rings):
        for s in range(sectors):
            a = r * sectors + s
            b = r * sectors + (s + 1) % sectors
            c = (r + 1) * sectors + s
            d = (r + 1) * sectors + (s + 1) % sectors
            idx.extend([a, c, b, b, c, d])
    return np.array(verts), np.array(norms), np.array(idx, dtype=np.uint32)


def _cylinder(a, b, radius, sides=8):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    axis = b - a
    length = np.linalg.norm(axis)
    if length < 1e-9:
        return None
    t = axis / length
    ref = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(t, ref))) > 0.9:
        ref = np.array([0.0, 1.0, 0.0])
    u = np.cross(t, ref)
    u /= np.linalg.norm(u)
    v = np.cross(t, u)

    verts, norms, idx = [], [], []
    for k in range(sides):
        th = 2.0 * np.pi * k / sides
        d = np.cos(th) * u + np.sin(th) * v
        verts.append(a + radius * d)
        norms.append(d)
    for k in range(sides):
        th = 2.0 * np.pi * k / sides
        d = np.cos(th) * u + np.sin(th) * v
        verts.append(b + radius * d)
        norms.append(d)
    for k in range(sides):
        k2 = (k + 1) % sides
        idx.extend([k, sides + k, k2, k2, sides + k, sides + k2])
    return np.array(verts), np.array(norms), np.array(idx, dtype=np.uint32)


def ligand_mesh(positions, elements, bonds=None):
    """Ball-and-stick geometry for one small molecule.

    Returns (vertices, normals, colors, indices) -- the same four arrays the
    ribbon and cartoon paths return, so a caller can concatenate them into one
    upload without knowing which produced what.

    A bond is drawn as two half-cylinders, each in its own atom's colour, so
    the join reads as the bond between a carbon and an oxygen rather than as
    one ambiguous grey stick.
    """
    p = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    if len(p) == 0:
        raise ValueError("a ligand needs at least one atom")
    if bonds is None:
        bonds = infer_bonds(p, elements)

    vs, ns, cs, ids = [], [], [], []
    offset = 0

    def add(chunk, colour):
        nonlocal offset
        if chunk is None:
            return
        v, n, i = chunk
        vs.append(v)
        ns.append(n)
        cs.append(np.tile(np.asarray(colour, dtype=np.float64), (len(v), 1)))
        ids.append(i + np.uint32(offset))
        offset += len(v)

    for k in range(len(p)):
        add(_sphere(p[k], ATOM_RADIUS), element_colour(elements[k]))

    for i, j in bonds:
        mid = (p[i] + p[j]) / 2.0
        add(_cylinder(p[i], mid, BOND_RADIUS), element_colour(elements[i]))
        add(_cylinder(mid, p[j], BOND_RADIUS), element_colour(elements[j]))

    return (np.concatenate(vs).astype(np.float32),
            np.concatenate(ns).astype(np.float32),
            np.concatenate(cs).astype(np.float32),
            np.concatenate(ids).astype(np.uint32))


def ligands_from_structure(st):
    """Pull every drawable small molecule out of a gemmi structure.

    Yields (positions, elements) per non-polymer residue. Waters are skipped:
    a predicted structure has none, and a crystal one has hundreds that would
    bury the molecule they surround.

    Takes an already-parsed structure rather than a path so the caller reads
    the file once -- the cartoon path needs the same structure.
    """
    import gemmi

    out = []
    for chain in st[0]:
        for res in chain:
            if res.name in ("HOH", "WAT", "DOD"):
                continue
            # ASK GEMMI WHAT IT IS; do not sniff atom names. The obvious
            # test -- "does it have an atom called CA?" -- is wrong, and
            # methotrexate is the counter-example this booth actually folds:
            # its glutamate tail has atoms named CA, CB, CG and CD, so a
            # name check classifies the drug as protein and drops it. That
            # is the exact bug this module exists to fix, reintroduced one
            # layer up.
            info = gemmi.find_tabulated_residue(res.name)
            if info is not None and (info.is_amino_acid() or info.is_nucleic_acid()):
                continue                      # polymer: the ribbon draws it
            atoms = [a for a in res]
            if not atoms:
                continue
            pos = np.array([[a.pos.x, a.pos.y, a.pos.z] for a in atoms])
            els = [a.element.name for a in atoms]
            out.append((pos, els))
    return out
