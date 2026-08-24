"""The cartoon sweep: is a helix actually flat, is an arrow actually an arrow.

The failure modes worth testing here are all VISUAL, and the temptation is to
assert that arrays came back the right shape and call it covered. A renderer
that returns correctly-shaped NaNs, or a ribbon that is secretly still a tube,
passes every shape check there is.

So these measure the geometry: how flat a ring is, whether the arrowhead is
wider than the strand it caps, and whether consecutive frames point the same
way. The last one is the beta-sheet twist, which is the single most visible
way a cartoon renderer can be wrong.
"""

import math
import pathlib

import numpy as np
import pytest

from ui.cartoon import (ARROW_WIDTH, DIMS, NUCLEIC_RADIUS, RING,
                        cartoon_from_cif, section_dims, side_vectors,
                        sweep)
from ui.secstruct import COIL, HELIX, STRAND


def _straight(n, spacing=3.8):
    return np.array([[i * spacing, 0.0, 0.0] for i in range(n)])


def _frames(n, direction=(0.0, 1.0, 0.0)):
    return np.tile(np.asarray(direction, dtype=float), (n, 1))


def _ring_extents(verts, i, side, up):
    """(width, thickness) of ring `i`, measured along the frame axes."""
    r = verts[i * RING:(i + 1) * RING]
    c = r.mean(axis=0)
    w = np.ptp(np.dot(r - c, side))
    h = np.ptp(np.dot(r - c, up))
    return w, h


# ── the ribbon is actually flat ─────────────────────────────────────────────

def test_a_helix_ring_is_flat_not_round():
    """The whole point of a cartoon. A helix ribbon that came out round is a
    tube wearing a different name."""
    n = 12
    labels = HELIX * n
    w, h = section_dims(labels, samples_per_residue=1)
    verts, _, _ = sweep(_straight(n), _frames(n), w, h)

    width, thick = _ring_extents(verts, n // 2, [0, 1, 0], [0, 0, 1])
    assert width / thick > 3.0, \
        f"helix ring is {width:.2f} x {thick:.2f} -- not a ribbon"


def test_a_loop_ring_is_round_not_flat():
    """The matched half: the flatness above is the helix, not the sweep. A
    loop must stay a thin round tube, subordinate to both other shapes."""
    n = 12
    w, h = section_dims(COIL * n, samples_per_residue=1)
    verts, _, _ = sweep(_straight(n), _frames(n), w, h)

    width, thick = _ring_extents(verts, n // 2, [0, 1, 0], [0, 0, 1])
    assert 0.7 < width / thick < 1.4, \
        f"loop ring is {width:.2f} x {thick:.2f} -- not round"


def test_a_helix_ribbon_is_wider_than_a_loop():
    n = 12
    hw, hh = section_dims(HELIX * n, 1)
    cw, ch = section_dims(COIL * n, 1)
    assert hw[n // 2] > cw[n // 2] * 2, "a helix ribbon is not wider than a loop"


# ── the arrow is actually an arrow ──────────────────────────────────────────

def test_a_strand_ends_in_a_head_wider_than_its_body():
    n = 20
    w, _ = section_dims(STRAND * n, samples_per_residue=2)
    body = w[len(w) // 4]
    head = w.max()
    assert head > body * 1.5, \
        f"arrowhead ({head:.2f}) is not wider than the strand body ({body:.2f})"


def test_the_arrow_comes_to_a_point():
    """An arrowhead that does not taper is a paddle. The tip must be
    narrower than the body it grew out of."""
    n = 20
    w, _ = section_dims(STRAND * n, samples_per_residue=2)
    assert w[-1] < DIMS[STRAND][0], \
        f"the strand ends at half-width {w[-1]:.2f}, wider than its own body"


def test_the_widest_point_is_near_the_end_of_the_strand():
    n = 20
    w, _ = section_dims(STRAND * n, samples_per_residue=2)
    assert np.argmax(w) > 0.6 * len(w), "the arrowhead is not at the end"


def test_a_helix_has_no_arrowhead():
    """Only strands get arrows. A helix that flares at the end reads as a
    strand, which is a false claim about the structure."""
    n = 20
    w, _ = section_dims(HELIX * n, samples_per_residue=2)
    assert np.ptp(w) < 0.15, f"a helix changed width by {np.ptp(w):.2f}"


# ── the beta pleat, which is the one that ruins a render ────────────────────

def test_consecutive_side_vectors_never_flip():
    """THE BETA-SHEET TWIST. In a pleated sheet the carbonyls alternate
    direction every residue, so the raw side vector flips ~180 degrees each
    step and the ribbon turns inside out at every one. This is how you can
    tell a cartoon renderer was never tried on a sheet.

    Built with deliberately alternating carbonyls, which is what a strand
    actually looks like.
    """
    n = 14
    ca = _straight(n)
    c = ca + np.array([1.2, 0.4, 0.0])
    # O alternates to either side of the chain, as in a real pleated strand.
    o = np.array([c[i] + (0.0, 1.1 if i % 2 == 0 else -1.1, 0.0) for i in range(n)])

    sides = side_vectors(ca, c, o)
    dots = [float(np.dot(sides[i], sides[i - 1])) for i in range(1, n)]
    assert min(dots) > 0.0, \
        f"side vector flipped between residues (min dot {min(dots):.2f})"


def test_the_flip_correction_does_not_flatten_a_real_turn():
    """The correction must not be so eager that it prevents the ribbon
    following a genuine change of direction: aligning to the predecessor is
    a sign choice, not a constraint on the direction itself."""
    n = 24
    ang = np.linspace(0, math.pi, n)
    ca = np.stack([np.cos(ang) * 10, np.sin(ang) * 10, np.zeros(n)], axis=1)
    c = ca + np.array([0.6, 0.6, 0.0])
    o = c + np.array([0.0, 0.0, 1.1])
    sides = side_vectors(ca, c, o)
    assert np.ptp(sides, axis=0).max() > 0.3, \
        "every side vector is identical; the frame is not following the chain"


# ── the arrays the renderer is handed ───────────────────────────────────────

def test_the_mesh_is_finite_and_indexable():
    n = 16
    labels = COIL * 4 + HELIX * 8 + COIL * 4
    w, h = section_dims(labels, 2)
    centre = _straight(len(w))
    verts, norms, idx = sweep(centre, _frames(len(w)), w, h)

    assert np.isfinite(verts).all(), "NaN or inf in the vertices"
    assert np.isfinite(norms).all(), "NaN or inf in the normals"
    assert len(verts) == len(norms)
    assert idx.max() < len(verts), "an index points past the last vertex"
    assert len(idx) % 3 == 0, "indices do not form whole triangles"


def test_normals_are_unit_length():
    """Lighting is wrong in a way that looks like a material bug, not a
    geometry bug, if these drift."""
    n = 10
    w, h = section_dims(HELIX * n, 1)
    _, norms, _ = sweep(_straight(n), _frames(n), w, h)
    lens = np.linalg.norm(norms, axis=1)
    assert np.allclose(lens, 1.0, atol=1e-6), f"normal lengths {lens.min()}..{lens.max()}"


def test_every_ring_has_the_same_vertex_count():
    """The reason shapes can change along one sweep with no joins to solve."""
    n = 12
    labels = HELIX * 6 + COIL * 6
    w, h = section_dims(labels, 1)
    verts, _, _ = sweep(_straight(n), _frames(n), w, h)
    assert len(verts) % RING == 0
    assert len(verts) == len(w) * RING


def test_a_centerline_of_one_point_is_refused():
    with pytest.raises(ValueError):
        sweep(np.zeros((1, 3)), _frames(1), [1.0], [1.0])


def test_a_degenerate_carbonyl_does_not_produce_NaN():
    """A missing or coincident O must not poison the frame -- the renderer
    would upload NaNs and draw nothing, with no error anywhere."""
    n = 10
    ca = _straight(n)
    c = ca + np.array([1.2, 0.0, 0.0])
    o = c.copy()                       # O exactly on C: no carbonyl direction
    sides = side_vectors(ca, c, o)
    assert np.isfinite(sides).all()
    assert np.allclose(np.linalg.norm(sides, axis=1), 1.0)


def test_a_helix_is_framed_from_its_axis_not_its_carbonyls():
    """THE ONE THAT MADE THE DIFFERENCE BETWEEN A CARTOON AND A SHREDDED ONE.

    The peptide-plane frame is correct for strands and wrong inside a helix:
    it rotates WITH the helix, ~72 degrees per residue on this booth's own
    Trp-cage, and a 2.3 A ribbon spun that fast passes through its own
    previous turn. Rendered, it tore into disconnected flaps.

    Framing from the helix axis instead leaves the ribbon turning slowly.
    Measured here as the twist between consecutive residues, with and without
    the labels that select the axis frame.
    """
    n = 18
    # An ideal alpha-helix: radius 2.3 A, 100 degrees and 1.5 A of rise per
    # residue. THE CARBONYLS POINT ALONG THE AXIS, which is the real geometry
    # and the whole reason the carbonyl frame misbehaves here: the tangent of
    # a helix is mostly circumferential, so crossing it with an axial C=O
    # gives a RADIAL side vector -- one that rotates with the helix, 72
    # degrees per residue on real Trp-cage.
    #
    # (An earlier version of this fixture pointed the carbonyls radially
    # instead. That made cross(t, C=O) come out axial, i.e. identical to what
    # the axis frame computes, so the two frames agreed and the test could
    # not fail. Worth stating: the fixture was wrong in a way that hid the
    # very thing it exists to measure.)
    ang = np.arange(n) * math.radians(100.0)
    ca = np.stack([2.3 * np.cos(ang), 2.3 * np.sin(ang), 1.5 * np.arange(n)], axis=1)
    axis_dir = np.array([0.0, 0.0, 1.0])
    radial = ca - np.stack([np.zeros(n), np.zeros(n), 1.5 * np.arange(n)], axis=1)
    radial /= np.linalg.norm(radial, axis=1, keepdims=True)
    c = ca + radial * 0.6 + axis_dir * 0.9
    o = c + axis_dir * 1.2

    def mean_twist(sides):
        d = [abs(math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(sides[i], sides[i - 1])))))))
             for i in range(1, len(sides))]
        return sum(d) / len(d)

    carbonyl = mean_twist(side_vectors(ca, c, o))
    axis_framed = mean_twist(side_vectors(ca, c, o, labels=HELIX * n))

    # MEASURED, not aspirational: on this fixture the carbonyl frame turns
    # ~76 deg/residue (real Trp-cage measures 72) and the axis frame ~45.
    #
    # The residual is not a defect and is not removable: the axis frame is
    # perpendicular to both the tangent and the radius, and a helix tangent
    # is tilted by the pitch, so the width direction rocks back and forth by
    # roughly the pitch angle. What matters is that the carbonyl frame's
    # rotation is MONOTONIC -- it accumulates until the ribbon passes through
    # its own previous turn -- while this one oscillates about the axis and
    # never winds up. The first tears when rendered; the second does not.
    assert axis_framed < carbonyl * 0.7, (
        f"axis framing twists {axis_framed:.0f} deg/residue vs carbonyl "
        f"{carbonyl:.0f} -- not enough of an improvement to stop the tearing")

    # The part that actually distinguishes them: does the frame WIND UP?
    # Summing the signed rotation about the helix axis separates a frame that
    # rotates with the helix from one that rocks around it.
    def winding(sides):
        total = 0.0
        for i in range(1, len(sides)):
            a, b = sides[i - 1], sides[i]
            total += math.atan2(float(np.dot(np.cross(a, b), axis_dir)),
                                float(np.dot(a, b)))
        return abs(math.degrees(total))

    # Measured on this fixture: the carbonyl frame winds 1100 degrees over 18
    # residues -- three full turns, which is the ribbon corkscrewing through
    # itself -- against 389 for the axis frame, about one turn, which is the
    # helix's own pitch and is what a cartoon helix should do.
    assert winding(side_vectors(ca, c, o, labels=HELIX * n)) < \
        winding(side_vectors(ca, c, o)) / 2, \
        "the axis frame is winding up around the helix like the carbonyl one"


# ── CA-less polymer chains: nucleic acids in a mixed structure ──────────────
#
# `cartoon_from_cif`'s own docstring promises this, and until 2026-08-24 the
# code did the opposite:
#
#   "A chain with no C-alphas at all -- a nucleic acid, a ligand -- has no
#    secondary structure and no peptide plane, so it is swept as plain round
#    tube using the anchors `ui.geometry` already chooses for it."
#
# What it actually did was `continue`, dropping the chain. Two consequences,
# both observed on the running booth: a protein+nucleic complex drew the
# protein and SILENTLY OMITTED the nucleic acid, and a pure DNA/tRNA fold
# raised GeometryError (every chain dropped, nothing left to build), which the
# viewer caught and answered with the old tube renderer -- the right picture
# reached by an exception, with an ERROR traceback on a third of all folds.

_MIXED = (pathlib.Path(__file__).resolve().parents[1]
          / "fixtures" / "structures" / "protein_nucleic_cartoon.cif")


def _nucleic_anchor_positions(cif_path):
    """Where the nucleic chains actually are, straight from gemmi."""
    import gemmi
    st = gemmi.read_structure(str(cif_path))
    st.setup_entities()
    pts = []
    for chain in st[0]:
        if any(r.find_atom("CA", "*") is not None for r in chain):
            continue                      # a protein chain; not what we want
        for res in chain:
            for name in ("P", "C1'"):
                atom = res.find_atom(name, "*")
                if atom is not None:
                    pts.append([atom.pos.x, atom.pos.y, atom.pos.z])
                    break
    return np.asarray(pts, dtype=np.float64)


def test_a_nucleic_chain_is_drawn_alongside_a_protein_one():
    """The defect: on a mixed structure the nucleic acid vanished.

    The fixture is two REAL folds off this booth's own cards -- a 20-residue
    Trp-cage and a 24-nucleotide duplex -- with the nucleic chains translated
    +200 A in x so "did the nucleic acid get drawn" is a question about
    geometry and not about a coordinate coincidence.
    """
    verts, norms, colors, indices = cartoon_from_cif(_MIXED)
    nucleic = _nucleic_anchor_positions(_MIXED)
    assert len(nucleic) > 0, "fixture has no nucleic chain; test is vacuous"

    # Every nucleic anchor must have cartoon surface near it. 6 A is generous
    # for a tube swept at ~1.6 A radius through those very points, and still
    # nowhere near the protein 200 A away.
    d = np.linalg.norm(verts[None, :, :] - nucleic[:, None, :], axis=2)
    nearest = d.min(axis=1)
    assert nearest.max() < 6.0, (
        f"the nucleic chain is missing from the cartoon: its worst-covered "
        f"anchor is {nearest.max():.1f} A from any vertex")


def test_the_protein_is_still_a_cartoon_when_a_nucleic_chain_is_present():
    """The fix must not cost the protein its secondary structure: a mixed
    structure's protein half must still be built by the cartoon sweep, not
    demoted to a tube along with its neighbour."""
    mixed = cartoon_from_cif(_MIXED)[0]
    # The protein chain alone, for comparison.
    import gemmi
    st = gemmi.read_structure(str(_MIXED))
    st.setup_entities()
    keep = [c.name for c in st[0]
            if any(r.find_atom("CA", "*") is not None for r in c)]
    assert keep, "fixture has no protein chain"
    for name in [c.name for c in st[0] if c.name not in keep]:
        st[0].remove_chain(name)
    st.setup_entities()
    only_protein = pathlib.Path("/tmp") / "cartoon_protein_only.cif"
    st.make_mmcif_document().write_file(str(only_protein))

    protein_only = cartoon_from_cif(only_protein)[0]
    assert len(protein_only) > 0
    # The protein's own vertices are unchanged by the nucleic chain's presence
    # -- the mixed mesh is the protein's mesh plus the nucleic tube.
    assert len(mixed) > len(protein_only), \
        "adding a nucleic chain added no geometry"
    near_protein = mixed[mixed[:, 0] < 100.0]
    assert len(near_protein) == len(protein_only), (
        "the protein half of the mixed cartoon is not the same mesh it is "
        "on its own")


def test_a_pure_nucleic_structure_builds_a_cartoon_instead_of_raising():
    """A DNA duplex or a tRNA is now a cartoon result -- a round tube, which
    is the correct picture for something with no secondary structure -- rather
    than a GeometryError the viewer has to catch and paper over."""
    import gemmi
    st = gemmi.read_structure(str(_MIXED))
    st.setup_entities()
    for name in [c.name for c in st[0]
                 if any(r.find_atom("CA", "*") is not None for r in c)]:
        st[0].remove_chain(name)
    st.setup_entities()
    only_nucleic = pathlib.Path("/tmp") / "cartoon_nucleic_only.cif"
    st.make_mmcif_document().write_file(str(only_nucleic))

    verts, norms, colors, indices = cartoon_from_cif(only_nucleic)
    assert len(verts) > 0 and len(indices) > 0
    assert len(colors) == len(verts), "one colour per vertex"
    assert len(norms) == len(verts), "one normal per vertex"


def test_a_one_residue_polymer_chain_is_skipped_not_fatal():
    """A chain with a single anchor cannot be a tube -- `tube_mesh` needs two
    centreline points -- and must be SKIPPED, not allowed to raise.

    Without the `len(anchors) < 2` guard the exception escapes and the whole
    structure falls back to the tube renderer, which is precisely the bug the
    nucleic-tube path exists to fix, reintroduced for every structure that
    happens to carry a stray one-residue chain. Verified: removing that guard
    leaves this test red and the other four green.
    """
    import gemmi
    st = gemmi.read_structure(str(_MIXED))
    st.setup_entities()
    # Keep the protein, and cut one nucleic chain down to a single residue.
    nucleic = [c.name for c in st[0]
               if not any(r.find_atom("CA", "*") is not None for r in c)]
    assert nucleic, "fixture has no nucleic chain"
    for name in nucleic[1:]:
        st[0].remove_chain(name)
    victim = st[0][nucleic[0]]
    while len(victim) > 1:
        del victim[len(victim) - 1]
    st.setup_entities()
    path = pathlib.Path("/tmp") / "cartoon_one_residue_chain.cif"
    st.make_mmcif_document().write_file(str(path))

    verts = cartoon_from_cif(path)[0]
    assert len(verts) > 0, "the protein chain should still have been drawn"


def test_the_nucleic_tube_is_not_drawn_as_thin_as_a_cartoon_loop():
    """`NUCLEIC_RADIUS`, not `DIMS[COIL]`.

    A loop is deliberately thin (0.25 A) so it reads as subordinate to the
    helices and sheets around it. A nucleic backbone is not subordinate to
    anything -- in a protein/DNA complex it is half the subject -- so it is
    swept at the same 1.6 A `ui.geometry.ribbon_from_cif` has always used.
    Measured as the spread of surface around the chain's own anchors.
    """
    import gemmi
    st = gemmi.read_structure(str(_MIXED))
    st.setup_entities()
    for name in [c.name for c in st[0]
                 if any(r.find_atom("CA", "*") is not None for r in c)]:
        st[0].remove_chain(name)
    st.setup_entities()
    path = pathlib.Path("/tmp") / "cartoon_nucleic_radius.cif"
    st.make_mmcif_document().write_file(str(path))

    verts = cartoon_from_cif(path)[0]
    anchors = _nucleic_anchor_positions(path)
    # Distance from each vertex to its nearest anchor. The tube's surface sits
    # about NUCLEIC_RADIUS from the centreline, so the bulk of it must lie
    # well outside a 0.25 A coil and inside a generous bound.
    d = np.linalg.norm(verts[:, None, :] - anchors[None, :, :], axis=2).min(axis=1)
    assert np.median(d) > DIMS[COIL][0] * 2, (
        f"the nucleic tube is only {np.median(d):.2f} A from its anchors -- "
        f"that is coil-thin, not {NUCLEIC_RADIUS} A")
    assert np.median(d) < NUCLEIC_RADIUS * 2.0
