import numpy as np
import pytest

from ui.geometry import GeometryError, load_backbone_trace

FIXTURE = "tests/fixtures/structures/minimal.cif"


def _load_fixture():
    """Shared setup for the tests that all just need a loaded trace."""
    return load_backbone_trace(FIXTURE)


def test_loads_every_ca_atom():
    trace = _load_fixture()
    assert trace.n_residues == 5
    assert trace.coords.shape == (5, 3)
    assert trace.coords.dtype == np.float32


def test_coordinates_match_the_file():
    trace = _load_fixture()
    np.testing.assert_allclose(trace.coords[0], [0.0, 0.0, 0.0], atol=1e-5)
    np.testing.assert_allclose(trace.coords[3], [11.4, 2.0, 1.5], atol=1e-5)


def test_plddt_comes_from_the_b_factor_column():
    trace = _load_fixture()
    np.testing.assert_allclose(trace.plddt, [95.0, 80.0, 60.0, 40.0, 88.0], atol=1e-4)


def test_chain_ids_are_recorded_per_residue():
    trace = _load_fixture()
    assert trace.chain_ids == ["A", "A", "A", "A", "B"]


def test_missing_file_raises_geometry_error():
    with pytest.raises(GeometryError, match="could not read"):
        load_backbone_trace("tests/fixtures/structures/does-not-exist.cif")


def test_structure_without_any_anchor_atom_raises(tmp_path):
    empty = tmp_path / "empty.cif"
    empty.write_text("data_empty\n#\n")
    with pytest.raises(GeometryError, match="no backbone anchor atoms"):
        load_backbone_trace(str(empty))


def test_residue_without_ca_is_skipped_but_others_stay_aligned():
    """A residue with none of the anchor atoms at all (the fixture's middle
    residue is recorded with only an N) must be dropped without breaking the
    1:1 correspondence between coords, plddt, and chain_ids for the residues
    that do have one.

    Still true now that P and C1' are anchors too: N is not one of the
    three, so this residue is still anchorless and still dropped.
    """
    trace = load_backbone_trace("tests/fixtures/structures/skips_missing_ca.cif")
    assert trace.n_residues == 2
    np.testing.assert_allclose(
        trace.coords, [[0.0, 0.0, 0.0], [7.6, 2.0, 0.0]], atol=1e-5
    )
    np.testing.assert_allclose(trace.plddt, [90.0, 55.0], atol=1e-4)
    assert trace.chain_ids == ["A", "A"]


def test_alternate_locations_pick_the_highest_occupancy_conformer():
    """residue.find_atom("CA", "*") is not occupancy-aware: against gemmi
    0.6.4 it returns the first CA in file order regardless of occupancy.
    The fixture lists the low-occupancy (0.30) conformer first and the
    high-occupancy (0.70) one second specifically to catch a regression
    back to that file-order behavior.
    """
    trace = load_backbone_trace("tests/fixtures/structures/alt_locs.cif")
    assert trace.n_residues == 1
    np.testing.assert_allclose(trace.coords[0], [5.0, 5.0, 5.0], atol=1e-5)
    np.testing.assert_allclose(trace.plddt, [10.0], atol=1e-4)


def test_missing_b_factor_column_raises():
    """gemmi's mmCIF atom_site reader requires occupancy and
    B_iso_or_equiv to recognize the loop as atom records at all; drop the
    column and gemmi silently returns zero models, which is the same
    "empty structure" path as test_structure_without_any_anchor_atom_raises.
    """
    with pytest.raises(GeometryError, match="no backbone anchor atoms"):
        load_backbone_trace("tests/fixtures/structures/missing_bfactor_column.cif")


def test_missing_b_factor_value_defaults_to_fifty():
    """A present B_iso_or_equiv column with an unknown ("?") value for one
    atom does not fail to parse -- gemmi substitutes its own placeholder of
    50.0 (not 0.0). Pinning this matters because it means such a residue
    renders as *medium* confidence, not lowest confidence, which a naive
    reading of "missing means unknown" might assume.
    """
    trace = load_backbone_trace("tests/fixtures/structures/missing_bfactor_value.cif")
    np.testing.assert_allclose(trace.plddt, [50.0], atol=1e-4)


# ── Nucleic acids ────────────────────────────────────────────────────────
#
# A nucleic-acid residue contains no atom named "CA". Before
# `BACKBONE_ANCHORS`, `load_backbone_trace` therefore found nothing at all in
# a DNA fold that had otherwise completely succeeded, and the booth drew an
# empty screen for 494 real atoms of double helix. These tests are what fail
# if that regresses.

# A REAL fold off this booth's own hardware -- see the fixture's own header.
# 2 chains x 12 nucleotides, 494 atoms, not one "CA" anywhere in it.
DNA_DUPLEX = "tests/fixtures/structures/real_fold_dna_duplex.cif"

# Hand-written protein+DNA complex. Every number in it is chosen so that a
# wrong anchor choice gives a DIFFERENT answer -- see its own header.
PROTEIN_DNA = "tests/fixtures/structures/protein_dna_complex.cif"


def test_a_dna_duplex_is_traced_on_its_phosphate_backbone():
    """The bug this whole anchor mechanism exists for, stated on the real
    structure: a DNA duplex must produce a trace at all.

    The B-factor assertions are the load-bearing half. Chain A's first
    nucleotide carries BOTH a P (B 86.68) and a C1' (B 93.94), so the pLDDT
    this test pins says which of the two the trace actually anchored on --
    an assertion on the residue count alone would be equally happy with the
    sugar trace, which is a different (and visibly different) curve.

    Mutation this catches: reordering `BACKBONE_ANCHORS` to try C1' before
    P; dropping P from it entirely (24 -> 0 residues, GeometryError).
    """
    trace = load_backbone_trace(DNA_DUPLEX)

    assert trace.n_residues == 24
    assert trace.chain_ids == ["A"] * 12 + ["B"] * 12

    # The phosphorus of chain A's 5'-terminal DC, and its own B-factor. The
    # C1' of that SAME residue is at (10.609, -14.086, -5.127) with B 93.87,
    # 5.6 A away and one ramp band up, so these two lines say which of the
    # two atoms the trace took -- not merely that it took something.
    np.testing.assert_allclose(
        trace.coords[0], [15.880, -12.320, -5.521], atol=1e-3)
    np.testing.assert_allclose(trace.plddt[0], 86.684, atol=1e-3)

    # ...and the same for chain B's first nucleotide, so the assertion is
    # not satisfied by a trace that happens to get the very first row right.
    np.testing.assert_allclose(trace.plddt[12], 86.531, atol=1e-3)

    # Consecutive phosphates sit 5.96-7.16 A apart on this fold, roughly
    # twice a protein's 3.8 A C-alpha spacing. Pinned because it is the
    # number the ribbon's radius and spline sampling have to cope with, and
    # because a trace that silently mixed P and C1' anchors within a strand
    # would show up here as a ~5 A step where the anchor changed.
    for chain in (trace.coords[:12], trace.coords[12:]):
        steps = np.linalg.norm(np.diff(chain, axis=0), axis=1)
        assert 5.9 < steps.min() and steps.max() < 7.2


def test_one_structure_anchors_its_protein_and_its_dna_differently():
    """A complex is traced chain by chain, with the anchor chosen per
    RESIDUE -- C-alpha through the protein chain, phosphorus through the
    nucleic one -- in a single pass over a single structure.

    The whole trace is pinned coordinate by coordinate rather than
    chain-count by chain-count: the fixture gives every candidate anchor in
    a residue a distinct position AND a distinct B-factor, so this is an
    assertion about which ATOM each residue anchored on, not merely about
    how many residues survived.

    Mutation this catches: choosing the anchor once per file (whichever
    name the first residue had would then win for every residue, dropping
    all four of the other chain's residues).
    """
    trace = load_backbone_trace(PROTEIN_DNA)

    assert trace.n_residues == 8
    assert trace.chain_ids == ["A"] * 4 + ["B"] * 4
    np.testing.assert_allclose(trace.coords, [
        [0.0, 0.0, 0.0],      # ALA  CA
        [3.8, 0.0, 0.0],      # GLY  CA
        [7.6, 0.0, 0.0],      # LEU  CA
        [11.4, 0.0, 0.0],     # SEP  CA  (not its P at y=4.0)
        [0.0, 20.0, 0.0],     # DA   C1' (no phosphate on this 5' end)
        [6.6, 20.0, 0.0],     # DT   P   (not its C1' at y=23.0)
        [13.2, 20.0, 0.0],    # DG   P
        [19.8, 20.0, 0.0],    # DC   P
    ], atol=1e-4)
    np.testing.assert_allclose(
        trace.plddt, [95.0, 93.0, 91.0, 97.0, 61.0, 62.0, 63.0, 64.0],
        atol=1e-4)


def test_a_phosphorylated_amino_acid_still_traces_on_its_c_alpha():
    """Phosphoserine is protein, and contains an atom named "P".

    Anchoring it on that P would move the residue 4 A off the backbone and
    recolour it from 97 (">90", blue) to 20 ("<50", orange). This is the
    reason `BACKBONE_ANCHORS` tries CA FIRST rather than in any convenient
    order, so it is asserted rather than left implied by the constant.

    Mutation this catches: `BACKBONE_ANCHORS = ("P", "CA", "C1'")`.
    """
    trace = load_backbone_trace(PROTEIN_DNA)
    np.testing.assert_allclose(trace.coords[3], [11.4, 0.0, 0.0], atol=1e-4)
    np.testing.assert_allclose(trace.plddt[3], 97.0, atol=1e-4)


def test_a_nucleotide_with_no_phosphate_falls_back_to_its_sugar():
    """The 5' end of a strand often carries no phosphate group at all.

    Dropping such a residue -- which is what a P-only nucleic anchor does --
    silently shortens every strand by one at the end a viewer looks at
    first. The C1' fallback keeps it, ~5 A off the phosphate line, which is
    the cheaper of the two errors.

    Mutation this catches: `BACKBONE_ANCHORS = ("CA", "P")` -- chain B then
    has 3 residues, not 4, and the trace starts at the second nucleotide.
    """
    trace = load_backbone_trace(PROTEIN_DNA)
    assert trace.chain_ids.count("B") == 4
    np.testing.assert_allclose(trace.coords[4], [0.0, 20.0, 0.0], atol=1e-4)
    np.testing.assert_allclose(trace.plddt[4], 61.0, atol=1e-4)
