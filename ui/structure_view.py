"""One structure, assembled: the cartoon, plus whatever is bound to it.

This is the seam between the three geometry modules and the viewer. It exists
so `ui/app.py` calls one function and does not have to know that a structure
is now built from a secondary-structure assignment, a swept cartoon and a
ball-and-stick ligand -- or that any of those can decline.

FALLING BACK IS THE POINT. The cartoon needs a full backbone (N, CA, C, O)
and a secondary-structure assignment; a nucleic acid has neither, and so does
anything unusual enough to break an assumption. Rather than let that cost the
visitor the whole structure, the plain backbone tube -- which needs only one
anchor atom per residue and has drawn every fold this booth has ever done --
is used instead. A booth that shows a slightly plainer protein is fine. A
booth that shows nothing is not.
"""

import logging

import numpy as np

log = logging.getLogger(__name__)


def structure_mesh(cif_path):
    """(vertices, normals, colors, indices) for everything in `cif_path`."""
    from ui.geometry import ribbon_from_cif

    parts = []
    try:
        from ui.cartoon import cartoon_from_cif
        parts.append(cartoon_from_cif(cif_path))
    except Exception:
        # Not fatal and not silent: the tube still draws the structure.
        log.exception("cartoon failed for %s; falling back to the backbone tube",
                      cif_path)
        try:
            parts.append(ribbon_from_cif(cif_path))
        except Exception:
            # NEITHER DREW A BACKBONE, and that is not necessarily an error:
            # a structure that is nothing but a ligand has no backbone to
            # draw. Letting this raise here would throw away the ligand that
            # was about to be built -- which is how the first version of this
            # function lost the entire molecule for a ligand-only file.
            # Whether there is anything at all to show is decided at the end.
            log.exception("no backbone geometry for %s", cif_path)

    try:
        parts.extend(_ligand_parts(cif_path))
    except Exception:
        # A ligand is an addition. Losing it must never cost the protein.
        log.exception("ligand geometry failed for %s; drawing the structure "
                      "without it", cif_path)

    return _concat(parts)


def _ligand_parts(cif_path):
    import gemmi

    from ui.ligand import ligand_mesh, ligands_from_structure

    st = gemmi.read_structure(str(cif_path))
    st.setup_entities()
    out = []
    for positions, elements in ligands_from_structure(st):
        if len(positions) < 2:
            # A lone ion is a dot with no context; drawing it beside a
            # 200-residue protein reads as a rendering speck, not a molecule.
            continue
        out.append(ligand_mesh(positions, elements))
    if out:
        log.info("drawing %d ligand(s) alongside %s", len(out), cif_path)
    return out


def _concat(parts):
    """Join meshes, rebasing each one's indices past the vertices before it.

    The same rebase `ribbon_from_cif` does per chain, and forgetting it has
    the same consequence: later triangles silently redraw the first mesh
    while their own vertices are never referenced.
    """
    parts = [p for p in parts if p is not None and len(p[0])]
    if not parts:
        from ui.geometry import GeometryError
        raise GeometryError("nothing drawable in this structure")

    verts, norms, colors, indices = [], [], [], []
    offset = 0
    for v, n, c, i in parts:
        verts.append(v)
        norms.append(n)
        colors.append(c)
        indices.append(np.asarray(i, dtype=np.uint32) + np.uint32(offset))
        offset += len(v)
    return (np.concatenate(verts).astype(np.float32),
            np.concatenate(norms).astype(np.float32),
            np.concatenate(colors).astype(np.float32),
            np.concatenate(indices).astype(np.uint32))
