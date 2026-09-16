"""Pocket-residue geometry: which protein residues sit near a ligand.

Fixture discipline (CLAUDE.md, 2026-08-13): pocket vs. non-pocket residues
here are distinguished by real, unambiguous 3D position -- not by a
construction that would answer the same way for a right or wrong
implementation. See tests/fixtures/structures/pocket_with_ligand.cif's own
header for the geometry each assertion below depends on.
"""

import gemmi

from ui.pocket import pocket_residues

WITH_LIGAND = "tests/fixtures/structures/pocket_with_ligand.cif"
NO_LIGAND = "tests/fixtures/structures/pocket_no_ligand.cif"


def _structure(path):
    st = gemmi.read_structure(path)
    st.setup_entities()
    return st


def test_only_the_close_residues_are_in_the_pocket():
    """Residue A1 sits 1.0 A from the ligand atom, well inside a 5 A cutoff;
    A4 sits EXACTLY 5.0 A away and must also be included (the cutoff is
    inclusive -- see test_the_cutoff_boundary_is_inclusive). Residue A2
    (9.0 A) and A3 (99.0 A) must not appear."""
    structure = _structure(WITH_LIGAND)
    pocket = pocket_residues(structure, cutoff_angstrom=5.0)
    assert pocket == {("A", 1), ("A", 4)}


def test_widening_the_cutoff_catches_more():
    """At 15 A, residue A2 (9.0 A from the ligand) joins A1 (1.0 A) and A4
    (5.0 A). A3 (99.0 A) is still far outside even this wider cutoff."""
    structure = _structure(WITH_LIGAND)
    pocket = pocket_residues(structure, cutoff_angstrom=15.0)
    assert pocket == {("A", 1), ("A", 2), ("A", 4)}


def test_the_cutoff_boundary_is_inclusive():
    """Residue A4 sits at exactly 5.0 A from the ligand. `pocket_residues`
    promises "within cutoff_angstrom", which this project reads as <=, not
    <. A `<` implementation matches every other assertion in this file
    (nothing else lands exactly on a boundary) and would only be caught
    here."""
    structure = _structure(WITH_LIGAND)
    assert ("A", 4) in pocket_residues(structure, cutoff_angstrom=5.0)


def test_a_residue_exactly_outside_every_used_cutoff_never_appears():
    """A3 sits 99.0 A from the ligand -- outside both cutoffs this file
    exercises. Its presence in the fixture (rather than a fixture with only
    two residues) is what catches an implementation that includes every
    residue, or the last residue, regardless of distance."""
    structure = _structure(WITH_LIGAND)
    assert ("A", 3) not in pocket_residues(structure, cutoff_angstrom=5.0)
    assert ("A", 3) not in pocket_residues(structure, cutoff_angstrom=15.0)


def test_a_non_protein_residue_is_never_counted_even_if_closest():
    """Chain C's DNA nucleotide sits 0.5 A from the ligand -- closer than
    every protein residue in the fixture -- specifically to catch an
    implementation that includes any near polymer atom rather than only
    protein (amino-acid) residues."""
    structure = _structure(WITH_LIGAND)
    pocket = pocket_residues(structure, cutoff_angstrom=5.0)
    assert ("C", 1) not in pocket
    pocket_wide = pocket_residues(structure, cutoff_angstrom=15.0)
    assert ("C", 1) not in pocket_wide


def test_a_structure_with_no_ligand_has_an_empty_pocket():
    """Three of this booth's targets (Trp-cage, DNA duplex, tRNA) have no
    ligand at all. That must not raise -- it is a normal, ligand-less
    structure, not an error."""
    structure = _structure(NO_LIGAND)
    assert pocket_residues(structure) == set()


def test_default_cutoff_is_five_angstrom():
    """`pocket_residues(structure)` with no explicit cutoff must match the
    5 A behaviour, so a caller that forgets to pass cutoff_angstrom gets the
    documented default rather than silently scoring everything or nothing."""
    structure = _structure(WITH_LIGAND)
    assert pocket_residues(structure) == {("A", 1), ("A", 4)}
