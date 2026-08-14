"""Secondary structure assigned from C-alpha coordinates alone.

WHY THIS HAS TO EXIST AT ALL. The standard way to draw a protein -- the
cartoon every textbook and every paper uses -- needs to know which residues
are in helices and which are in sheets. Crystallographic mmCIF files carry
that in HELIX/SHEET records. **The model's output does not.** A real fold
written by this project's own pipeline contains exactly one category,
`_atom_site`, so `gemmi.read_structure(...).helices` comes back empty and
there is nothing to read. It has to be computed from the coordinates.

HOW THESE TESTS AVOID BEING VACUOUS. "Did it find a helix?" is satisfied by
an implementation that calls everything a helix, and this project has shipped
that shape of test before. So every test here that asserts a positive also
asserts the corresponding negative on geometry that must NOT match:

* ideal helix geometry -> helix, AND ideal strand geometry -> not helix
* ideal strand geometry -> strand, AND ideal helix geometry -> not strand
* random coordinates -> mostly coil

The ideal-geometry fixtures are built from internal coordinates (bond length,
bond angle, dihedral) rather than copied from a structure, so the ground truth
is known by construction rather than by assertion.
"""

import math

import numpy as np
import pytest

from ui.secstruct import COIL, HELIX, STRAND, assign


# ── building chains with known geometry ─────────────────────────────────────

def _chain(n, bond=3.8, angle_deg=89.0, dihedral_deg=50.0):
    """Place `n` C-alphas with the given internal coordinates (NeRF).

    The two parameter sets below are P-SEA's own angular criteria for the two
    structures, so a chain built with them IS an ideal alpha-helix or an ideal
    beta-strand by definition -- not by resemblance to one.
    """
    th = math.radians(angle_deg)
    ta = math.radians(dihedral_deg)
    pts = [np.array([0.0, 0.0, 0.0]),
           np.array([bond, 0.0, 0.0]),
           np.array([bond - bond * math.cos(th), bond * math.sin(th), 0.0])]
    for _ in range(3, n):
        a, b, c = pts[-3], pts[-2], pts[-1]
        bc = (c - b) / np.linalg.norm(c - b)
        nv = np.cross(b - a, bc)
        nv = nv / np.linalg.norm(nv)
        m = np.array([bc, np.cross(nv, bc), nv]).T
        d = np.array([-bond * math.cos(th),
                      bond * math.sin(th) * math.cos(ta),
                      bond * math.sin(th) * math.sin(ta)])
        pts.append(c + m @ d)
    return np.array(pts)


def _mixed(segments, bond=3.8):
    """Build one continuous chain whose internal coordinates CHANGE part way.

    `segments` is [(count, angle_deg, dihedral_deg), ...]. This is how you get
    a short helix genuinely embedded in coil, with no artificial break at the
    join -- concatenating separately-built chains puts a discontinuity at the
    seam, which is itself read as coil and hides whatever the test was for.
    """
    plan = []
    for count, ang, dih in segments:
        plan.extend([(ang, dih)] * count)

    pts = [np.array([0.0, 0.0, 0.0]),
           np.array([bond, 0.0, 0.0])]
    th0 = math.radians(plan[0][0])
    pts.append(np.array([bond - bond * math.cos(th0), bond * math.sin(th0), 0.0]))
    for k in range(3, len(plan)):
        ang, dih = plan[k]
        th, ta = math.radians(ang), math.radians(dih)
        a, b, c = pts[-3], pts[-2], pts[-1]
        bc = (c - b) / np.linalg.norm(c - b)
        nv = np.cross(b - a, bc)
        nv = nv / np.linalg.norm(nv)
        m = np.array([bc, np.cross(nv, bc), nv]).T
        d = np.array([-bond * math.cos(th),
                      bond * math.sin(th) * math.cos(ta),
                      bond * math.sin(th) * math.sin(ta)])
        pts.append(c + m @ d)
    return np.array(pts)


#: Internal coordinates that are neither helix nor strand -- ordinary loop.
_LOOP = (110.0, 120.0)


def _ideal_helix(n=16):
    return _chain(n, angle_deg=89.0, dihedral_deg=50.0)


def _ideal_strand(n=12):
    return _chain(n, angle_deg=124.0, dihedral_deg=-170.0)


def _beta_hairpin(arm=7, separation=4.8):
    """Two ideal strands lying antiparallel, ~`separation` A apart.

    A LONE strand is not a sheet -- see `_drop_unpaired_strands`. So the
    fixture for "this is a strand" has to be two of them side by side, which
    is what a beta-sheet actually is. Built by taking one ideal strand,
    reversing it, and setting it down alongside the first with a turn between.
    """
    a = _ideal_strand(arm)
    axis = a[-1] - a[0]
    axis /= np.linalg.norm(axis)
    # Any direction perpendicular to the strand axis will do for the offset.
    perp = np.cross(axis, [0.0, 0.0, 1.0])
    if np.linalg.norm(perp) < 1e-6:
        perp = np.cross(axis, [0.0, 1.0, 0.0])
    perp /= np.linalg.norm(perp)

    b = a[::-1] + perp * separation
    turn = np.array([a[-1] + axis * 2.6 + perp * separation * 0.25,
                     a[-1] + axis * 3.2 + perp * separation * 0.75])
    return np.vstack([a, turn, b])


def _fraction(labels, code):
    return sum(1 for c in labels if c == code) / max(1, len(labels))


# ── helices ─────────────────────────────────────────────────────────────────

def test_an_ideal_helix_is_called_a_helix():
    labels = assign(_ideal_helix())
    assert _fraction(labels, HELIX) > 0.6, f"ideal helix assigned {labels!r}"


def test_an_ideal_strand_is_NOT_called_a_helix():
    """The negative half of the pair. Without this, an implementation that
    labels everything H passes the test above."""
    labels = assign(_ideal_strand())
    assert _fraction(labels, HELIX) < 0.2, f"ideal strand assigned {labels!r}"


# ── strands ─────────────────────────────────────────────────────────────────

def test_a_beta_hairpin_is_called_strand():
    labels = assign(_beta_hairpin())
    assert _fraction(labels, STRAND) > 0.4, f"hairpin assigned {labels!r}"


def test_a_LONE_extended_run_is_not_called_a_strand():
    """The half that keeps the test above honest, and the reason the pairing
    rule exists at all.

    A single extended segment with nothing beside it is not a sheet. This is
    the polyproline case: Trp-cage's C-terminal tail is extended, so C-alpha
    geometry alone calls it a strand, and the booth would draw a sheet arrow
    on the molecule it shows most often -- in front of people who know that
    Trp-cage has no sheet in it.
    """
    labels = assign(_ideal_strand(12))
    assert STRAND not in labels, \
        f"a lone extended run was called a sheet: {labels!r}"


def test_an_ideal_helix_is_NOT_called_a_strand():
    labels = assign(_ideal_helix())
    assert _fraction(labels, STRAND) < 0.2, f"ideal helix assigned {labels!r}"


# ── coil ────────────────────────────────────────────────────────────────────

def test_random_coordinates_are_mostly_coil():
    """Not a structure at all. An assigner that sees regular structure in
    noise would paint a predicted fold with confident-looking helices that
    are not there -- the exact dishonesty this booth exists not to do."""
    rng = np.random.default_rng(20260814)
    pts = np.cumsum(rng.normal(scale=3.0, size=(60, 3)), axis=0)
    labels = assign(pts)
    assert _fraction(labels, COIL) > 0.7, f"noise assigned {labels!r}"


def test_a_short_run_is_not_promoted_to_a_helix():
    """Four residues of helical geometry, genuinely embedded in loop, are not
    a helix. A single turn of alpha-helix is ~3.6 residues, so anything under
    five is not yet a turn -- and a cartoon renderer draws it as a stub that
    reads as a rendering fault.

    The fixture is ONE continuous chain that changes conformation part way,
    so there is no artificial break at the join for the assigner to hide
    behind. An earlier version of this test concatenated displaced fragments
    and could not fail: the discontinuity destroyed the geometry, so no
    candidate was generated with or without the minimum-run rule.
    """
    pts = _mixed([(8, *_LOOP), (4, 89.0, 50.0), (8, *_LOOP)])
    labels = assign(pts)
    assert HELIX not in labels, f"a 4-residue run became a helix: {labels!r}"


def test_a_full_length_helix_in_the_same_fixture_IS_found():
    """The matched half: the fixture shape itself is not what suppresses the
    helix above. Same construction, a helix long enough to be one."""
    pts = _mixed([(8, *_LOOP), (12, 89.0, 50.0), (8, *_LOOP)])
    labels = assign(pts)
    assert HELIX in labels, f"an embedded 12-residue helix was missed: {labels!r}"


# ── shapes that must not crash ──────────────────────────────────────────────

@pytest.mark.parametrize("n", [0, 1, 2, 3, 4])
def test_chains_too_short_to_assign_are_all_coil(n):
    labels = assign(np.zeros((n, 3)))
    assert len(labels) == n
    assert set(labels) <= {COIL}


def test_the_label_string_is_one_character_per_residue():
    for n in (5, 12, 33):
        assert len(assign(_ideal_helix(n))) == n


def test_coincident_points_do_not_raise():
    """Two atoms at the same coordinate is degenerate input the renderer has
    already been bitten by once (ui/geometry.py's tube refusal)."""
    pts = _ideal_helix(12)
    pts[5] = pts[4]
    assign(pts)          # must not raise


def test_input_is_not_mutated():
    pts = _ideal_helix(12)
    before = pts.copy()
    assign(pts)
    assert np.array_equal(pts, before)


# ── a real predicted fold ───────────────────────────────────────────────────

def test_a_real_trp_cage_fold_has_a_helix_and_is_not_all_helix():
    """Trp-cage's defining feature is a short alpha-helix near its N-terminus,
    with the rest a loop and a polyproline tail. So a correct assignment finds
    a helix AND leaves a substantial part of the chain unassigned -- which is
    what makes this test able to fail in both directions.

    Driven through the real fixture written by this project's own pipeline,
    the one whose mmCIF has no HELIX records to read.
    """
    import pathlib

    import gemmi

    cif = (pathlib.Path(__file__).resolve().parents[1]
           / "fixtures" / "structures" / "real_fold_trpcage.cif")
    st = gemmi.read_structure(str(cif))
    st.setup_entities()
    assert not st.helices, "fixture now HAS helix records; this test is moot"

    ca = np.array([[a.pos.x, a.pos.y, a.pos.z]
                   for ch in st[0] for r in ch for a in r if a.name == "CA"])
    assert len(ca) >= 15, f"fixture has only {len(ca)} CA atoms"

    labels = assign(ca)
    assert HELIX in labels, f"no helix found in Trp-cage: {labels!r}"
    assert _fraction(labels, HELIX) < 0.8, \
        f"Trp-cage assigned almost entirely helix: {labels!r}"
    # Trp-cage is an alpha-helix, a 3-10 helix and a polyproline II tail.
    # There is no beta-sheet anywhere in it, and drawing one would be a
    # visible falsehood about the molecule this booth shows most.
    assert STRAND not in labels, \
        f"a sheet was found in Trp-cage, which has none: {labels!r}"


# ── the over-extension guard ────────────────────────────────────────────────

def test_a_helix_does_not_bleed_far_into_the_flanking_coil():
    """Over-extension is this assigner's known failure mode, and it is bounded
    rather than eliminated -- so it is pinned.

    An ideal helix with a long, clearly non-helical tail on each end: the
    helix must be found, and must not run more than a couple of residues into
    either tail. Measured against four crystal structures, painting the whole
    distance window instead of its interior cost five points of agreement;
    this is the test that notices if that regresses.
    """
    lead, hel = 8, 14
    pts = _mixed([(lead, *_LOOP), (hel, 89.0, 50.0), (10, *_LOOP)])
    labels = assign(pts)
    assert HELIX in labels, labels

    first_h = min(i for i, c in enumerate(labels) if c == HELIX)
    last_h = max(i for i, c in enumerate(labels) if c == HELIX)
    assert first_h >= lead - 2, \
        f"helix started {lead - first_h} residues before it exists: {labels!r}"
    assert last_h <= lead + hel, (
        f"helix ran {last_h - (lead + hel) + 1} residues past its end: {labels!r}")
