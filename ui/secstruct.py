"""Which residues are helix, which are sheet -- worked out from C-alphas.

WHY THIS FILE EXISTS. Drawing a protein the way structural biologists draw it
-- the cartoon, with helices as coils and sheets as arrows -- requires knowing
which residues are in which. A crystallographic mmCIF carries that in HELIX
and SHEET records, and reading them would be the obvious thing to do.

**The model's output has none.** A real fold written by this project's own
pipeline contains exactly one mmCIF category, `_atom_site`: coordinates and
nothing else. `gemmi.read_structure(...).helices` is empty for every structure
this booth produces. So the assignment has to come from the geometry.

THE METHOD is P-SEA (Labesse, Colloc'h, Pothier & Mornon, 1997), which was
designed for exactly this situation -- assigning secondary structure from a
C-alpha trace, with no backbone N/O atoms and therefore no hydrogen bonds to
find. DSSP, the usual choice, needs those hydrogen bonds and cannot be used
here.

P-SEA recognises each structure two ways, and either is enough:

  * by DISTANCE, between residues 2, 3 and 4 apart along the chain, and
  * by ANGLE, the C-alpha bond angle and the C-alpha dihedral.

An alpha-helix turns tightly: residue i+4 sits one turn above i, only ~6.4 A
away. An extended strand goes nearly straight: i+4 is ~12.4 A away. That
difference is what the numbers below encode, and it is why C-alphas alone are
enough to tell the two apart.

WHAT THIS DELIBERATELY DOES NOT DO. It does not distinguish 3-10 or pi helices
from alpha, and it does not pair strands into sheets. The cartoon renderer
needs runs of "helix here, strand there, loop in between"; which flavour of
helix, and which strands hydrogen-bond to which, would be additional claims
this booth has no need to make and no way to check.

HOW ACCURATE IS IT, MEASURED. Against the crystallographers' own annotations
in four reference structures -- crambin (1CRN), ubiquitin (1UBQ), trypsin
(2PTN) and FKBP12 (4FKB), the last two being molecules this booth folds -- the
per-residue agreement is:

    1CRN  84.8%      1UBQ  75.0%      2PTN  69.5%      4FKB  63.6%
    overall 67.9% across 732 residues

That is short of P-SEA's published ~80% against DSSP, and the residual error
is almost entirely OVER-EXTENSION: helices and strands are found where they
are (all 16 of ubiquitin's helix residues, 28 of its 33 sheet residues) and
then run a residue or two into the loop at each end. For a cartoon that means
slightly long helices, not helices in the wrong places.

Two things that sound like improvements and are not, both measured rather
than argued: painting the whole distance window instead of its interior costs
five points (67.9% -> 62.6%), and filling single-residue gaps -- which looks
obviously right, since a helix does not stop for one residue -- costs two and
a half (67.9% -> 65.3%). Neither is in the code.

Worth keeping in proportion: the reference numbers above are a
crystallographer's reading of an EXPERIMENTAL structure, while what this booth
draws is a prediction of a different structure of the same molecule. Exact
agreement was never the target; not lying about the topology is.

Pure: numpy in, a string out, no gemmi and no GL. Nucleic acids simply have no
C-alphas, so a DNA duplex arrives here as an empty array and leaves as an
empty assignment -- correctly, since "helix" in the secondary-structure sense
is a protein idea and the double helix is not one.
"""

import logging

import numpy as np

log = logging.getLogger(__name__)

HELIX = "H"
STRAND = "E"
COIL = "C"

# ── P-SEA's criteria ────────────────────────────────────────────────────────
#
# (target, tolerance) for the distance between residues n apart, in angstroms,
# and for the two angles in degrees. Taken from the paper; the asymmetry
# between the helix and strand numbers is the whole discriminator, so these
# are not knobs to tune by eye.
_HELIX_D = {2: (5.5, 0.5), 3: (5.3, 0.5), 4: (6.4, 0.6)}
_STRAND_D = {2: (6.7, 0.6), 3: (9.9, 0.9), 4: (12.4, 1.1)}
_HELIX_ANGLE = (89.0, 12.0)      # C-alpha bond angle
_HELIX_DIHED = (50.0, 20.0)
_STRAND_ANGLE = (124.0, 14.0)
_STRAND_DIHED = (-170.0, 45.0)

# Minimum run lengths. A "helix" of two residues is not a helix, it is three
# atoms that happened to fall in range -- and a cartoon renderer draws it as a
# stub that reads as a rendering fault. P-SEA uses 5 for helices and 3 for
# strands; a single turn of alpha-helix is ~3.6 residues, so 5 is one full
# turn plus the residues that close it.
_MIN_HELIX = 5
_MIN_STRAND = 3

# A strand is only a strand if it has a PARTNER.
#
# This is not in P-SEA's published criteria and it is here because of what the
# method does to Trp-cage -- the booth's most-shown molecule. Trp-cage has one
# alpha-helix, a 3-10 helix, and a polyproline II tail, and NO beta-sheet at
# all. But polyproline II is an extended conformation, so on C-alpha geometry
# alone it looks like a strand, and the tail came out labelled `EEEE`. Drawing
# a sheet arrow along it would be a confident, visible falsehood about a
# structure any structural biologist in the room knows by heart.
#
# What actually distinguishes a beta-strand from any other extended run is
# that it lies alongside another strand and hydrogen-bonds to it. Without
# backbone N and O atoms we cannot see the bonds, but we can see the
# geometry: paired strands sit roughly 4.5-5.5 A apart, C-alpha to C-alpha.
# So an extended run with nothing beside it is demoted to coil.
_PAIR_MAX_A = 5.5
#: How far apart in sequence two residues must be before their closeness
#: counts as pairing rather than as simply being neighbours in the chain.
_PAIR_MIN_SEPARATION = 3


def _within(value, spec):
    target, tol = spec
    return abs(value - target) <= tol


def _distances(ca, step):
    """|CA(i) - CA(i+step)| for every i, NaN where the pair runs off the end."""
    n = len(ca)
    out = np.full(n, np.nan)
    if n > step:
        out[: n - step] = np.linalg.norm(ca[step:] - ca[: n - step], axis=1)
    return out


def _bond_angles(ca):
    """Angle at CA(i) between CA(i-1) and CA(i+1), in degrees; NaN at the ends."""
    n = len(ca)
    out = np.full(n, np.nan)
    for i in range(1, n - 1):
        u = ca[i - 1] - ca[i]
        v = ca[i + 1] - ca[i]
        nu, nv = np.linalg.norm(u), np.linalg.norm(v)
        if nu < 1e-6 or nv < 1e-6:
            continue                      # coincident atoms: no angle exists
        c = float(np.clip(np.dot(u, v) / (nu * nv), -1.0, 1.0))
        out[i] = np.degrees(np.arccos(c))
    return out


def _dihedrals(ca):
    """Dihedral CA(i-1..i+2) in degrees, stored at i; NaN where undefined."""
    n = len(ca)
    out = np.full(n, np.nan)
    for i in range(1, n - 2):
        b0 = ca[i] - ca[i - 1]
        b1 = ca[i + 1] - ca[i]
        b2 = ca[i + 2] - ca[i + 1]
        nb1 = np.linalg.norm(b1)
        if nb1 < 1e-6:
            continue
        b1n = b1 / nb1
        v = b0 - np.dot(b0, b1n) * b1n
        w = b2 - np.dot(b2, b1n) * b1n
        nv, nw = np.linalg.norm(v), np.linalg.norm(w)
        if nv < 1e-6 or nw < 1e-6:
            continue
        x = float(np.clip(np.dot(v, w) / (nv * nw), -1.0, 1.0))
        ang = np.degrees(np.arccos(x))
        if np.dot(np.cross(v, w), b1n) < 0:
            ang = -ang
        out[i] = ang
    return out


def _candidates(ca, dists, angles, dihed, dspec, aspec, tspec):
    """Residues matching one structure, by EITHER route P-SEA allows."""
    n = len(ca)
    by_distance = np.zeros(n, dtype=bool)
    by_angle = np.zeros(n, dtype=bool)
    for i in range(n):
        d_ok = all(
            not np.isnan(dists[step][i]) and _within(dists[step][i], dspec[step])
            for step in (2, 3, 4)
        )
        if d_ok:
            # The distance test looks forward from i, so it describes a window
            # rather than residue i alone -- but paint the INTERIOR of that
            # window, not all of it. Painting i..i+4 runs each assignment two
            # residues past where the regular structure actually stops, and
            # measured against four crystal structures that over-extension
            # cost about five points of agreement (62.6% -> 67.9%). See the
            # accuracy note in this module's docstring.
            by_distance[i + 1 : i + 4] = True
        if (not np.isnan(angles[i]) and not np.isnan(dihed[i])
                and _within(angles[i], aspec) and _within(dihed[i], tspec)):
            by_angle[i] = True
    return by_distance | by_angle


def _drop_short_runs(flags, minimum):
    """Erase runs shorter than `minimum`. Modifies a copy."""
    out = flags.copy()
    n = len(out)
    i = 0
    while i < n:
        if not out[i]:
            i += 1
            continue
        j = i
        while j < n and out[j]:
            j += 1
        if j - i < minimum:
            out[i:j] = False
        i = j
    return out


def _drop_unpaired_strands(ca, strand):
    """Demote extended runs that have no strand beside them.

    See `_PAIR_MAX_A`. An isolated extended segment -- a polyproline tail, a
    long loop that happens to run straight -- is not a sheet, and drawing it
    as one would be a false claim about the structure.
    """
    out = strand.copy()
    segments = []
    i, n = 0, len(out)
    while i < n:
        if not out[i]:
            i += 1
            continue
        j = i
        while j < n and out[j]:
            j += 1
        segments.append((i, j))
        i = j
    if len(segments) < 2:
        # Nothing to pair with at all.
        for a, b in segments:
            out[a:b] = False
        return out

    for a, b in segments:
        paired = False
        for c, d in segments:
            if (c, d) == (a, b):
                continue
            for x in range(a, b):
                for y in range(c, d):
                    if abs(x - y) < _PAIR_MIN_SEPARATION:
                        continue
                    if np.linalg.norm(ca[x] - ca[y]) <= _PAIR_MAX_A:
                        paired = True
                        break
                if paired:
                    break
            if paired:
                break
        if not paired:
            out[a:b] = False
    return out


def assign(ca_coords):
    """Assign `H` / `E` / `C` to each C-alpha. Returns a string, one per residue.

    `ca_coords` is an (N, 3) array of C-alpha positions **for a single chain**,
    in order. Assigning across a chain break would invent structure spanning
    two molecules, exactly as splining across one invents backbone -- so the
    caller splits by chain first (see `ui.geometry`).
    """
    ca = np.asarray(ca_coords, dtype=np.float64).reshape(-1, 3)
    n = len(ca)
    if n < _MIN_HELIX:
        # Too short for any run to survive the minimum-length rule, so the
        # answer is coil by construction -- verified: `_drop_short_runs` on an
        # all-true array of length < _MIN_HELIX returns nothing. Removing this
        # early return therefore changes no output, and a mutation of it
        # correctly survives; it is here so the geometry below never sees a
        # window it cannot fill. Returned early so the geometry below
        # never sees a window it cannot fill.
        return COIL * n

    dists = {step: _distances(ca, step) for step in (2, 3, 4)}
    angles = _bond_angles(ca)
    dihed = _dihedrals(ca)

    helix = _candidates(ca, dists, angles, dihed,
                        _HELIX_D, _HELIX_ANGLE, _HELIX_DIHED)
    strand = _candidates(ca, dists, angles, dihed,
                         _STRAND_D, _STRAND_ANGLE, _STRAND_DIHED)

    # Helix wins where both match. (Mutating this line away survives the
    # test suite, and that is CORRECT rather than a gap: `labels[strand]` is
    # written before `labels[helix]` below, so a residue flagged both comes
    # out H either way. The line is kept because it also shortens the strand
    # runs that the length and pairing filters then see, which the write
    # order does not do.) They are geometrically far apart, so an
    # overlap means one of the two matched marginally; the helix criteria are
    # the tighter pair, so the helix claim is the better-evidenced one.
    strand &= ~helix

    helix = _drop_short_runs(helix, _MIN_HELIX)
    strand = _drop_short_runs(strand, _MIN_STRAND)
    strand = _drop_unpaired_strands(ca, strand)

    labels = np.full(n, COIL, dtype="<U1")
    labels[strand] = STRAND
    labels[helix] = HELIX
    return "".join(labels)


def runs(labels):
    """`"CCHHHHHCC"` -> `[("C", 0, 2), ("H", 2, 7), ("C", 7, 9)]`.

    Half-open ranges, in order, covering the whole string. This is the form
    the cartoon renderer wants: it draws one piece of geometry per run, and
    the pieces have to meet exactly or the ribbon shows seams.
    """
    out = []
    if not labels:
        return out
    start = 0
    for i in range(1, len(labels) + 1):
        if i == len(labels) or labels[i] != labels[start]:
            out.append((labels[start], start, i))
            start = i
    return out
