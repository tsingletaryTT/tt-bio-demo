"""`structure_mesh`'s `highlight_residues` pass-through (Task 11).

This module is the seam between the geometry modules and the viewer
(ui/structure_view.py's own docstring), and Task 10 gave `cartoon_from_cif`
a `highlight_residues` parameter that nothing here threaded through yet --
`structure_mesh` took only `cif_path`. These tests pin that the seam now
carries it, without changing anything about the ordinary no-highlight path
Task 2 and Task 9's tests already cover.
"""

import gemmi

from ui.pocket import pocket_residues
from ui.structure_view import structure_mesh

WITH_LIGAND = "tests/fixtures/structures/pocket_with_ligand.cif"


def _pocket(path):
    st = gemmi.read_structure(path)
    st.setup_entities()
    return pocket_residues(st)


def test_highlight_residues_reaches_the_cartoon_build():
    """The colours structure_mesh returns for a highlighted residue must
    differ from the colours it returns for the same structure with no
    highlight -- the same real fixture and cutoff test_pocket.py already
    uses, so this is checking the WIRING (structure_mesh -> cartoon_from_cif)
    rather than re-deriving pocket geometry."""
    pocket = _pocket(WITH_LIGAND)
    assert pocket, "fixture must have a real pocket or this test proves nothing"

    plain = structure_mesh(WITH_LIGAND)
    highlighted = structure_mesh(WITH_LIGAND, highlight_residues=pocket)

    plain_colors = plain[2]
    highlighted_colors = highlighted[2]
    assert plain_colors.shape == highlighted_colors.shape
    assert not (plain_colors == highlighted_colors).all(), (
        "highlight_residues changed nothing about the returned colours")


def test_no_highlight_residues_is_unchanged_from_the_old_signature():
    """`highlight_residues=None` (the default, and every call site before
    Task 11) must reproduce exactly the old, one-argument call -- so this
    task cannot have moved anything about the ordinary path."""
    explicit_none = structure_mesh(WITH_LIGAND, highlight_residues=None)
    omitted = structure_mesh(WITH_LIGAND)
    for a, b in zip(explicit_none, omitted):
        assert (a == b).all()


def test_an_empty_highlight_set_is_the_same_as_no_highlight():
    empty = structure_mesh(WITH_LIGAND, highlight_residues=set())
    omitted = structure_mesh(WITH_LIGAND)
    for a, b in zip(empty, omitted):
        assert (a == b).all()
