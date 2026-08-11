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
