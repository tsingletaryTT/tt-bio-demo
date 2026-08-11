import numpy as np
import pytest

from ui.geometry import GeometryError, load_ca_trace

FIXTURE = "tests/fixtures/structures/minimal.cif"


def _load_fixture():
    """Shared setup for the tests that all just need a loaded trace."""
    return load_ca_trace(FIXTURE)


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
        load_ca_trace("tests/fixtures/structures/does-not-exist.cif")


def test_structure_without_ca_atoms_raises(tmp_path):
    empty = tmp_path / "empty.cif"
    empty.write_text("data_empty\n#\n")
    with pytest.raises(GeometryError, match="no C-alpha atoms"):
        load_ca_trace(str(empty))


def test_residue_without_ca_is_skipped_but_others_stay_aligned():
    """A residue with no CA atom (e.g. GLY recorded with only N) must be
    dropped without breaking the 1:1 correspondence between coords, plddt,
    and chain_ids for the residues that do have one.
    """
    trace = load_ca_trace("tests/fixtures/structures/skips_missing_ca.cif")
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
    trace = load_ca_trace("tests/fixtures/structures/alt_locs.cif")
    assert trace.n_residues == 1
    np.testing.assert_allclose(trace.coords[0], [5.0, 5.0, 5.0], atol=1e-5)
    np.testing.assert_allclose(trace.plddt, [10.0], atol=1e-4)


def test_missing_b_factor_column_raises():
    """gemmi's mmCIF atom_site reader requires occupancy and
    B_iso_or_equiv to recognize the loop as atom records at all; drop the
    column and gemmi silently returns zero models, which is the same
    "empty structure" path as test_structure_without_ca_atoms_raises.
    """
    with pytest.raises(GeometryError, match="no C-alpha atoms"):
        load_ca_trace("tests/fixtures/structures/missing_bfactor_column.cif")


def test_missing_b_factor_value_defaults_to_fifty():
    """A present B_iso_or_equiv column with an unknown ("?") value for one
    atom does not fail to parse -- gemmi substitutes its own placeholder of
    50.0 (not 0.0). Pinning this matters because it means such a residue
    renders as *medium* confidence, not lowest confidence, which a naive
    reading of "missing means unknown" might assume.
    """
    trace = load_ca_trace("tests/fixtures/structures/missing_bfactor_value.cif")
    np.testing.assert_allclose(trace.plddt, [50.0], atol=1e-4)
