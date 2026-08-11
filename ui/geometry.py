"""Turn predicted structures into renderable geometry.

Pure numpy and gemmi — no GL, no GTK — so all of it is unit-testable.
"""

from dataclasses import dataclass

import gemmi
import numpy as np


class GeometryError(Exception):
    """A structure could not be read or converted to geometry."""


@dataclass
class CaTrace:
    """The C-alpha backbone of a predicted structure."""

    coords: np.ndarray      # (N, 3) float32
    plddt: np.ndarray       # (N,) float32, 0-100
    chain_ids: list

    @property
    def n_residues(self):
        return len(self.coords)


def _best_ca_atom(residue):
    """Return the residue's best-supported CA atom, or None if it has none.

    `residue.find_atom("CA", "*")` looks like an altloc wildcard but it
    isn't occupancy-aware: against gemmi 0.6.4 it returns the first CA
    encountered in file order, not the highest-occupancy conformer. For a
    multi-conformer CIF that silently produces an arbitrary position and
    pLDDT, indistinguishable from a clean single-conformer file. Select by
    occupancy explicitly instead.

    Ties (equal occupancy, including the common case of a single conformer
    with no altloc at all) are broken by altloc code ascending, so the
    choice is deterministic rather than an accident of file order.
    """
    candidates = [atom for atom in residue if atom.name == "CA"]
    if not candidates:
        return None
    return min(candidates, key=lambda atom: (-atom.occ, atom.altloc))


def load_ca_trace(cif_path):
    """Read C-alpha positions and per-residue pLDDT from a CIF file.

    pLDDT is taken from the B-factor column, which is where AlphaFold-family
    predictors (including everything tt-bio serves) write per-residue confidence.
    """
    try:
        structure = gemmi.read_structure(str(cif_path))
    except Exception as exc:
        raise GeometryError(f"could not read {cif_path}: {exc}") from exc

    no_ca_error = f"{cif_path} contains no C-alpha atoms"

    if len(structure) == 0:
        raise GeometryError(no_ca_error)

    coords, plddt, chain_ids = [], [], []
    for chain in structure[0]:
        for residue in chain:
            atom = _best_ca_atom(residue)
            if atom is None:
                continue
            coords.append([atom.pos.x, atom.pos.y, atom.pos.z])
            plddt.append(atom.b_iso)
            chain_ids.append(chain.name)

    if not coords:
        raise GeometryError(no_ca_error)

    return CaTrace(
        coords=np.asarray(coords, dtype=np.float32),
        plddt=np.asarray(plddt, dtype=np.float32),
        chain_ids=chain_ids,
    )
