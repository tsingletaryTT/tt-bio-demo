"""The bound small molecules the booth computes and used to discard.

The load-bearing claim in `ui/ligand.py` is that bonds inferred from distance
match real chemistry. That is not asserted here, it is CHECKED: against
methotrexate, whose connectivity is known, and which is the ligand this
booth's own DHFR target folds on every loop.
"""

import numpy as np
import pytest

from ui.ligand import (BOND_RADIUS, element_colour, infer_bonds, ligand_mesh,
                       ligands_from_structure)


def _benzene():
    """Six carbons in a ring, 1.39 A apart -- so six bonds, not fifteen."""
    ang = np.arange(6) * np.pi / 3.0
    r = 1.39
    return (np.stack([r * np.cos(ang), r * np.sin(ang), np.zeros(6)], axis=1),
            ["C"] * 6)


def test_a_ring_gets_exactly_its_ring_bonds():
    pos, els = _benzene()
    assert len(infer_bonds(pos, els)) == 6, "a benzene ring is not six bonds"


def test_atoms_far_apart_are_not_bonded():
    """The half that keeps the test above honest: an implementation that
    bonds everything to everything also produces a connected ring."""
    pos = np.array([[0.0, 0, 0], [8.0, 0, 0], [16.0, 0, 0]])
    assert infer_bonds(pos, ["C", "C", "C"]) == []


def test_sulfur_bonds_further_than_carbon():
    """A flat cutoff gets this wrong. S-S is 2.05 A, longer than any C-C, so
    the limit has to come from the elements rather than from a constant."""
    pos = np.array([[0.0, 0, 0], [2.05, 0, 0]])
    assert infer_bonds(pos, ["S", "S"]), "an S-S bond was missed"
    assert infer_bonds(pos, ["C", "C"]) == [], "a 2.05 A C-C bond was invented"


def test_a_lone_ion_is_bonded_to_nothing():
    """A zinc sitting in a pocket is near the protein, not attached to it.
    Drawing a stick would be a chemical claim that is false."""
    assert infer_bonds(np.zeros((1, 3)), ["ZN"]) == []


def test_elements_get_their_conventional_colours():
    assert element_colour("O")[0] > element_colour("O")[2], "oxygen is not red"
    assert element_colour("N")[2] > element_colour("N")[0], "nitrogen is not blue"
    assert element_colour("C") != element_colour("O")
    assert element_colour("Xx") == element_colour("Zz"), "unknown elements differ"


def test_the_mesh_is_finite_and_indexable():
    pos, els = _benzene()
    v, n, c, i = ligand_mesh(pos, els)
    assert np.isfinite(v).all() and np.isfinite(n).all()
    assert len(v) == len(n) == len(c)
    assert i.max() < len(v)
    assert len(i) % 3 == 0


def test_each_half_of_a_bond_takes_its_own_atoms_colour():
    """A carbon-oxygen stick that is uniformly grey hides which end is which."""
    pos = np.array([[0.0, 0, 0], [1.4, 0, 0]])
    _, _, colours, _ = ligand_mesh(pos, ["C", "O"])

    def present(rgb):
        # np.isclose, not set membership: the mesh returns float32 and the
        # reference colours are float64, so the two print identically and
        # compare unequal. An earlier version of this test failed on exactly
        # that and the assertion message showed the value it claimed was
        # missing.
        return bool(np.isclose(colours, np.asarray(rgb, dtype=np.float32),
                               atol=1e-6).all(axis=1).any())

    assert present(element_colour("C")), "no carbon-coloured geometry"
    assert present(element_colour("O")), "no oxygen-coloured geometry"


def test_an_empty_ligand_is_refused():
    with pytest.raises(ValueError):
        ligand_mesh(np.zeros((0, 3)), [])


# ── against real chemistry ──────────────────────────────────────────────────

def test_methotrexate_connectivity_matches_the_real_molecule():
    """THE ONE THAT MATTERS. Methotrexate is what this booth folds into DHFR,
    and its structure is known: 33 atoms, 35 bonds.

    Distance inference has to reproduce that, or every ligand the booth draws
    is subtly wrong in a way a chemist will see immediately. Built from the
    real geometry of MTX as deposited, embedded here so the test needs no
    network.
    """
    import pathlib

    import gemmi

    cif = (pathlib.Path(__file__).resolve().parents[1]
           / "fixtures" / "structures" / "methotrexate.cif")
    st = gemmi.read_structure(str(cif))
    st.setup_entities()

    found = ligands_from_structure(st)
    assert found, "no ligand found in the fixture"
    pos, els = found[0]
    assert len(pos) == 33, f"MTX should have 33 atoms, fixture has {len(pos)}"

    bonds = infer_bonds(pos, els)
    assert len(bonds) == 35, (
        f"distance inference found {len(bonds)} bonds; methotrexate has 35")


def test_a_protein_chain_is_not_mistaken_for_a_ligand():
    """`ligands_from_structure` must leave the polymer to the ribbon, or the
    booth draws the whole backbone twice -- once as a cartoon and once as a
    ball-and-stick thicket."""
    import pathlib

    import gemmi

    cif = (pathlib.Path(__file__).resolve().parents[1]
           / "fixtures" / "structures" / "real_fold_trpcage.cif")
    st = gemmi.read_structure(str(cif))
    st.setup_entities()
    assert ligands_from_structure(st) == [] or all(
        len(p) < 10 for p, _ in ligands_from_structure(st))
