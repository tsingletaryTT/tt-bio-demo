"""Which protein residues sit near a ligand -- pure geometry, no model
output involved. See docs/superpowers/specs/2026-09-15-affinity-qa-design.md
section 2: this is the "pocket" half of an affinity question's visual
answer, computed entirely from a .cif the booth already has on disk,
independent of and never blocking on the nesso1 score itself.

The cutoff is a stated, checkable number, not a claim about a "true"
binding site -- see spec section 7's content-honesty rule. The UI's copy
must describe this as "residues nearest the ligand," never as "the
binding site," since a distance cutoff cannot establish the latter.

LIGAND IDENTIFICATION deliberately reuses `ui.ligand.ligands_from_structure`
rather than re-deriving it from `residue.het_flag`. The task brief that
preceded this file guessed `residue.het_flag == 'H'` as the HETATM test;
checked against this project's real gemmi (0.6.4) that attribute exists and
does return 'H'/'A' correctly for both the hand-written fixtures here and
`tests/fixtures/structures/methotrexate.cif` -- but `ui/ligand.py` already
made a considered, documented decision to classify residues by asking gemmi's
tabulated chemical-component dictionary (`gemmi.find_tabulated_residue(name)
.is_amino_acid() / .is_nucleic_acid()`) instead, specifically because a
name-or-record-based heuristic can be wrong on real predicted structures (its
own module docstring: "ASK GEMMI WHAT IT IS; do not sniff atom names" --
methotrexate's glutamate tail carries an atom literally named "CA", which
breaks the analogous atom-name heuristic for what counts as protein).
Nothing in this codebase has ever verified that tt-bio's own predicted-CIF
output sets `group_PDB`/`het_flag` correctly for a ligand entity, whereas
`ligands_from_structure`'s tabulated-residue approach is already covered by
`tests/unit/test_ligand.py` against a real ligand (33 atoms, 35 bonds
matching RDKit). Reusing it here means this module can never quietly
disagree with the one that already draws the ligand on screen, and never
depends on an unverified assumption about record-type flags.

Pure: gemmi structure in, a set of (chain_id, residue_number) pairs out. No
GL, no GTK, no hardware, no model output.
"""

import gemmi

from ui.ligand import ligands_from_structure


def pocket_residues(structure, cutoff_angstrom=5.0):
    """Protein residues with at least one atom within `cutoff_angstrom` of
    any ligand atom, in a `.cif` already parsed into a `gemmi.Structure`.

    Returns a set of `(chain_id, residue_seqid)` pairs -- `chain.name` and
    `residue.seqid.num`, the same identifiers `ui/geometry.py`'s
    `BackboneTrace` already carries per residue, so a caller can intersect
    this set against a trace's `chain_ids` without translating between two
    different residue-naming schemes.

    A structure with no ligand (three of this booth's targets have none, and
    every non-affinity target has none) returns an empty set rather than
    raising -- there is nothing wrong with such a structure, there is simply
    no pocket to report.
    """
    ligand_positions = [
        gemmi.Position(float(x), float(y), float(z))
        for positions, _elements in ligands_from_structure(structure)
        for x, y, z in positions
    ]
    if not ligand_positions:
        return set()

    pocket = set()
    for chain in structure[0]:
        for residue in chain:
            info = gemmi.find_tabulated_residue(residue.name)
            if info is None or not info.is_amino_acid():
                continue
            key = (chain.name, residue.seqid.num)
            if key in pocket:
                continue
            for atom in residue:
                if any(atom.pos.dist(lig_pos) <= cutoff_angstrom
                       for lig_pos in ligand_positions):
                    pocket.add(key)
                    break
    return pocket
