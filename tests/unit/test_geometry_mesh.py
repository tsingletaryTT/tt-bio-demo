import numpy as np
import pytest

from ui.geometry import (
    GeometryError,
    catmull_rom,
    load_ca_trace,
    plddt_colors,
    resample_scalar,
    ribbon_from_cif,
    tube_mesh,
)

# ── Fixtures used by the chain-splitting tests below ─────────────────────
#
# TWO_CHAINS: chains A and B, 3 residues each, 3.8 A apart across the break
#             (see the header comment in the .cif itself for why).
# MINIMAL:    4 residues in chain A, exactly ONE in chain B -- the
#             too-short-to-spline case.
# SINGLE_CHAIN: a real 20-residue Trp-cage fold, one chain, no break.
TWO_CHAINS = "tests/fixtures/structures/two_chains.cif"
MINIMAL = "tests/fixtures/structures/minimal.cif"
SINGLE_CHAIN = "tests/fixtures/structures/real_fold_trpcage.cif"

# The ribbon radius `ribbon_from_cif` defaults to; several assertions below
# reason about how far a vertex can legitimately sit from its own backbone.
RIBBON_RADIUS = 1.6


def test_spline_passes_through_control_points():
    pts = np.array([[0.0, 0, 0], [1.0, 1, 0], [2.0, 0, 0], [3.0, 1, 0]])
    curve = catmull_rom(pts, samples_per_segment=4)
    # Every control point should appear somewhere on the curve.
    for p in pts:
        assert np.min(np.linalg.norm(curve - p, axis=1)) < 1e-4


def test_spline_sample_count_is_predictable():
    pts = np.zeros((5, 3))
    pts[:, 0] = np.arange(5)
    curve = catmull_rom(pts, samples_per_segment=4)
    # 4 segments between 5 points, 4 samples each, plus the final endpoint.
    assert len(curve) == 4 * 4 + 1


def test_spline_of_two_points_is_a_line():
    pts = np.array([[0.0, 0, 0], [10.0, 0, 0]])
    curve = catmull_rom(pts, samples_per_segment=5)
    assert np.allclose(curve[:, 1:], 0.0, atol=1e-6)
    assert curve[0][0] < curve[-1][0]


def test_spline_of_single_point_returns_that_point():
    curve = catmull_rom(np.array([[1.0, 2.0, 3.0]]), samples_per_segment=4)
    assert curve.shape == (1, 3)


def test_tube_mesh_vertex_and_index_counts():
    centerline = np.zeros((10, 3), dtype=np.float32)
    centerline[:, 0] = np.arange(10)
    verts, norms, idx = tube_mesh(centerline, radius=1.0, sides=8)
    assert verts.shape == (10 * 8, 3)
    assert norms.shape == (10 * 8, 3)
    assert idx.shape == ((10 - 1) * 8 * 6,)
    assert idx.dtype == np.uint32


def test_tube_mesh_normals_are_unit_length():
    centerline = np.zeros((6, 3), dtype=np.float32)
    centerline[:, 0] = np.arange(6)
    _, norms, _ = tube_mesh(centerline, radius=2.0, sides=6)
    np.testing.assert_allclose(np.linalg.norm(norms, axis=1), 1.0, atol=1e-5)


def test_tube_mesh_vertices_sit_at_radius_from_a_straight_axis():
    centerline = np.zeros((5, 3), dtype=np.float32)
    centerline[:, 0] = np.arange(5) * 2.0
    radius = 1.7
    verts, _, _ = tube_mesh(centerline, radius=radius, sides=8)
    # Axis is X, so distance in the YZ plane must equal the radius.
    np.testing.assert_allclose(
        np.linalg.norm(verts[:, 1:], axis=1), radius, atol=1e-4
    )


def test_tube_mesh_indices_stay_in_range():
    centerline = np.zeros((7, 3), dtype=np.float32)
    centerline[:, 2] = np.arange(7)
    verts, _, idx = tube_mesh(centerline, sides=5)
    assert idx.max() < len(verts)


def test_tube_mesh_rejects_degenerate_centerline():
    with pytest.raises(Exception):
        tube_mesh(np.zeros((1, 3), dtype=np.float32))


def test_tube_mesh_faces_wind_outward():
    """Every triangle's face normal must agree with its own vertex normals.

    `norms` is defined as the radially-outward direction at each vertex (by
    construction: `verts[i, j] = centerline[i] + radius * norms[i, j]`), so
    it is ground truth for "outward" here -- not just a reference convention.
    A face whose winding order makes cross(v1-v0, v2-v0) point the opposite
    way from its own vertex normals is genuinely inside-out: under backface
    culling (OpenGL default: cull GL_BACK, front faces wound CCW) the entire
    visible surface would be culled away instead of the inside.
    """
    centerline = np.zeros((10, 3), dtype=np.float32)
    centerline[:, 0] = np.arange(10)
    verts, norms, idx = tube_mesh(centerline, radius=1.0, sides=8)
    tris = idx.reshape(-1, 3)
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    face_normals = np.cross(v1 - v0, v2 - v0)
    vertex_normal_avg = norms[tris[:, 0]] + norms[tris[:, 1]] + norms[tris[:, 2]]
    dots = np.sum(face_normals * vertex_normal_avg, axis=1)
    assert np.all(dots > 0)


def test_tube_mesh_rejects_duplicate_leading_point():
    """A duplicate point at the very start collapses the boundary tangent to
    zero length with no neighbor to borrow direction from, which otherwise
    divides 0/0 into a NaN that silently poisons the whole mesh via parallel
    transport. That must fail loudly instead.
    """
    centerline = np.array(
        [[0.0, 0, 0], [0.0, 0, 0], [1.0, 0, 0], [2.0, 0, 0]], dtype=np.float32
    )
    with pytest.raises(GeometryError):
        tube_mesh(centerline)


def test_tube_mesh_rejects_duplicate_trailing_point():
    centerline = np.array(
        [[0.0, 0, 0], [1.0, 0, 0], [2.0, 0, 0], [2.0, 0, 0]], dtype=np.float32
    )
    with pytest.raises(GeometryError):
        tube_mesh(centerline)


def test_tube_mesh_tolerates_duplicate_midpoint():
    """A duplicate point in the *middle* is fine: central differencing at
    that index bridges it using its two distinct neighbors, so it must not
    raise and must not produce NaNs.
    """
    centerline = np.array(
        [[0.0, 0, 0], [1.0, 0, 0], [1.0, 0, 0], [2.0, 0, 0], [3.0, 0, 0]],
        dtype=np.float32,
    )
    verts, norms, idx = tube_mesh(centerline, sides=6)
    assert not np.isnan(verts).any()
    assert not np.isnan(norms).any()


def test_resample_scalar_stretches_values_to_new_length():
    out = resample_scalar(np.array([0.0, 10.0]), 5)
    np.testing.assert_allclose(out, [0.0, 2.5, 5.0, 7.5, 10.0], atol=1e-5)


def test_resample_scalar_preserves_endpoints():
    values = np.array([90.0, 50.0, 70.0])
    out = resample_scalar(values, 17)
    assert np.isclose(out[0], 90.0)
    assert np.isclose(out[-1], 70.0)


def test_plddt_colors_follow_the_alphafold_ramp():
    colors = plddt_colors(np.array([95.0, 80.0, 60.0, 30.0]))
    assert colors.shape == (4, 3)
    np.testing.assert_allclose(colors[0], np.array([0x00, 0x53, 0xD6]) / 255.0, atol=1e-3)
    np.testing.assert_allclose(colors[1], np.array([0x65, 0xCB, 0xF3]) / 255.0, atol=1e-3)
    np.testing.assert_allclose(colors[2], np.array([0xFF, 0xDB, 0x13]) / 255.0, atol=1e-3)
    np.testing.assert_allclose(colors[3], np.array([0xFF, 0x7D, 0x45]) / 255.0, atol=1e-3)


def test_plddt_colors_are_in_unit_range():
    colors = plddt_colors(np.linspace(0.0, 100.0, 50))
    assert colors.min() >= 0.0 and colors.max() <= 1.0


def test_ribbon_from_cif_produces_consistent_buffers():
    from ui.geometry import ribbon_from_cif

    verts, norms, colors, idx = ribbon_from_cif(
        "tests/fixtures/structures/minimal.cif", samples_per_segment=4, sides=6
    )
    assert verts.shape == norms.shape == colors.shape
    assert verts.shape[1] == 3
    assert len(verts) % 6 == 0
    assert idx.max() < len(verts)
    assert colors.min() >= 0.0 and colors.max() <= 1.0
    assert verts.dtype == np.float32 and idx.dtype == np.uint32


def test_ribbon_from_cif_colors_align_ring_by_ring_with_known_bfactors():
    """Pins the vertex-color <-> centerline-ring correspondence to the
    fixture's own known B-factors (95/80/60/40), so a future shift in how
    `ribbon_from_cif` zips resampled pLDDT onto tube_mesh's rings fails a
    test instead of only being caught by a human diffing colors by hand (as
    happened in review: `test_ribbon_from_cif_produces_consistent_buffers`
    above checks only shapes/dtypes/index range, and would pass identically
    whether colors were correctly aligned per ring or shifted by one ring).

    Checks alignment two independent ways:
      1. Against a fresh recomputation using the same lower-level functions
         `ribbon_from_cif` composes -- catches an internal shift anywhere
         along the ribbon, ring by ring, not just at the ends.
      2. Against the fixture's real B-factors converted through the
         documented AlphaFold ramp thresholds by hand -- these two expected
         colors are typed in from the ramp table, not derived from the code
         under test, so a bug that happened to reproduce itself identically
         in both the implementation and the (1)-style recomputation would
         still be caught here.
    """
    cif_path = MINIMAL
    samples_per_segment, sides = 4, 6

    trace = load_ca_trace(cif_path)
    assert list(trace.plddt) == [95.0, 80.0, 60.0, 40.0, 88.0]  # pin the input
    assert trace.chain_ids == ["A", "A", "A", "A", "B"]         # ...and its chains

    # Only chain A (residues 0-3) is splined: chain B's single residue is too
    # short to tube and is skipped, so the ribbon's rings all come from chain
    # A's own four residues and its own four B-factors. Those four span all
    # four ramp bands (95 / 80 / 60 / 40), so an off-by-one in how the
    # per-chain pLDDT slice is taken changes the ramp visibly here.
    #
    # catmull_rom's first curve sample is exactly the first control point
    # (p[0]) and its last is exactly appended as the last control point
    # (p[-1]) -- see catmull_rom's implementation -- so combined with
    # resample_scalar's exact endpoint preservation, ring 0 below must carry
    # chain A's first CA atom's B-factor (95) and the last ring chain A's
    # last CA atom's (40), not an approximation.
    chain_a = slice(0, 4)
    centerline = catmull_rom(trace.coords[chain_a], samples_per_segment)
    expected_ring_colors = plddt_colors(
        resample_scalar(trace.plddt[chain_a], len(centerline))
    )

    _, _, colors, _ = ribbon_from_cif(
        cif_path, samples_per_segment=samples_per_segment, sides=sides
    )
    actual_ring_colors = colors.reshape(-1, sides, 3)

    assert len(actual_ring_colors) == len(expected_ring_colors)
    for i, expected in enumerate(expected_ring_colors):
        # Every vertex within a ring must carry that ring's color -- an
        # off-by-one in the np.repeat/reshape bookkeeping would break this
        # even if the *set* of colors present elsewhere looked fine.
        np.testing.assert_allclose(
            actual_ring_colors[i], np.tile(expected, (sides, 1)), atol=1e-6
        )

    # 95 -> ">90" blue; 40 -> "<50" orange. Typed straight from the ramp
    # table in ui/geometry.py's PLDDT_STOPS, independent of (1) above.
    np.testing.assert_allclose(
        actual_ring_colors[0, 0], np.array([0x00, 0x53, 0xD6]) / 255.0, atol=1e-3
    )
    np.testing.assert_allclose(
        actual_ring_colors[-1, 0], np.array([0xFF, 0x7D, 0x45]) / 255.0, atol=1e-3
    )


# ── Chain splitting ──────────────────────────────────────────────────────
#
# `ribbon_from_cif` must spline each chain separately. A single spline
# through every C-alpha of every chain draws a tube leg from one chain's
# C-terminus to the next chain's N-terminus -- and because that gap is a
# perfectly ordinary C-alpha--C-alpha distance, the leg is indistinguishable
# from real backbone by length, direction or curvature. The only thing that
# gives it away is that it puts geometry somewhere geometry must not be.


def _contiguous_chain_runs(chain_ids):
    """(chain_id, start, stop) for each contiguous run of one chain id.

    Deliberately re-derived here from `CaTrace.chain_ids` rather than
    imported from `ui.geometry`, so these tests say what the ribbon *should*
    be split on instead of agreeing with however the implementation happens
    to split it.
    """
    runs = []
    for i, chain_id in enumerate(chain_ids):
        if runs and runs[-1][0] == chain_id and runs[-1][2] == i:
            runs[-1][2] = i + 1
        else:
            runs.append([chain_id, i, i + 1])
    return [tuple(run) for run in runs]


def _expected_chain_vertex_spans(cif_path, samples_per_segment, sides):
    """Half-open (chain_id, start, stop) vertex ranges, one per drawable chain.

    Ring counts come from `catmull_rom` (the same primitive the ribbon uses),
    but the *layout* -- chains in file order, each chain's vertices
    contiguous, spans packed end to end with no gaps -- is asserted here
    rather than read back out of the implementation.
    """
    trace = load_ca_trace(cif_path)
    spans, start = [], 0
    for chain_id, lo, hi in _contiguous_chain_runs(trace.chain_ids):
        if hi - lo < 2:
            continue  # too short to tube: contributes no geometry at all
        n_rings = len(catmull_rom(trace.coords[lo:hi], samples_per_segment))
        spans.append((chain_id, start, start + n_rings * sides))
        start += n_rings * sides
    return spans


def test_no_ribbon_segment_spans_the_chain_break():
    """two_chains.cif's break is 3.8 A wide and collinear with chain A's last
    segment, so a spurious leg across it looks exactly like ordinary backbone:
    its triangles are the same size and shape as every other triangle in the
    mesh, so no edge-length or curvature test can find it. What actually
    distinguishes correct geometry is that the gap between chain A's
    C-terminus and chain B's N-terminus contains NO geometry at all.

    Both chains end/begin with a segment parallel to the gap, so their
    terminal rings lie in the plane perpendicular to it -- i.e. exactly at
    t = 0 and t = 1 along the gap axis. Forbidding vertices in the middle 80%
    (0.1 < t < 0.9, a 0.38 A margin at each end) is therefore comfortably
    clear of legitimate geometry, while a leg splined across the break at
    samples_per_segment=4 puts rings at roughly t = 0.25, 0.5 and 0.75.
    """
    trace = load_ca_trace(TWO_CHAINS)
    assert trace.chain_ids == ["A", "A", "A", "B", "B", "B"]  # pin the fixture

    a_c_terminus = trace.coords[2].astype(np.float64)
    b_n_terminus = trace.coords[3].astype(np.float64)
    gap = b_n_terminus - a_c_terminus
    assert 3.5 < np.linalg.norm(gap) < 4.1, "the break must look like real backbone"

    verts, _, _, idx = ribbon_from_cif(TWO_CHAINS, samples_per_segment=4, sides=6)

    # Fractional position of every vertex along the gap axis.
    t = (verts.astype(np.float64) - a_c_terminus) @ gap / (gap @ gap)
    inside_gap = (t > 0.1) & (t < 0.9)
    assert not inside_gap.any(), (
        f"{int(inside_gap.sum())} vertices sit inside the chain break "
        f"(gap fractions {np.sort(t[inside_gap])[:6]}) -- the ribbon is "
        f"splining straight through the chain boundary"
    )

    # Each chain's tube_mesh indices are 0-based, so concatenating chains
    # means offsetting each chain's indices by the running vertex count.
    # Forget that and chain B's triangles silently re-draw chain A's tube
    # while chain B's own vertices are never referenced by anything --
    # invisible to the gap check above, and invisible to `idx.max() <
    # len(verts)`, but not to this.
    assert len(np.unique(idx)) == len(verts), (
        f"{len(verts) - len(np.unique(idx))} of {len(verts)} vertices are "
        f"referenced by no triangle (idx.max()={idx.max()}) -- a chain's "
        f"indices were probably concatenated without a vertex offset"
    )

    # And no single triangle may draw from two chains' vertex spans.
    spans = _expected_chain_vertex_spans(TWO_CHAINS, samples_per_segment=4, sides=6)
    assert [chain_id for chain_id, _, _ in spans] == ["A", "B"]
    assert len(verts) == spans[-1][2], (
        f"expected {spans[-1][2]} vertices from per-chain splines, got {len(verts)}"
    )
    owner = np.full(len(verts), -1, dtype=np.int64)
    for ordinal, (_, start, stop) in enumerate(spans):
        owner[start:stop] = ordinal
    tri_owners = owner[idx.reshape(-1, 3)]
    assert (tri_owners == tri_owners[:, :1]).all(), "a triangle bridges two chains"


def test_a_single_chain_structure_is_unchanged():
    """Regression guard: the common case must not gain a seam.

    A one-chain structure has to come out byte-for-byte what a single spline
    through all of its C-alphas produces -- if per-chain splitting ever
    started at the wrong boundary (per residue, per segment, per anything
    that is not a chain id) this fixture's 20 residues would be chopped up
    and the buffers would stop matching.
    """
    trace = load_ca_trace(SINGLE_CHAIN)
    assert set(trace.chain_ids) == {"A"}  # pin the fixture: one chain only
    assert trace.n_residues == 20

    verts, norms, colors, idx = ribbon_from_cif(
        SINGLE_CHAIN, samples_per_segment=4, sides=6
    )

    centerline = catmull_rom(trace.coords, 4)
    want_verts, want_norms, want_idx = tube_mesh(centerline, radius=1.6, sides=6)
    want_colors = np.repeat(
        plddt_colors(resample_scalar(trace.plddt, len(centerline))), 6, axis=0
    )

    np.testing.assert_allclose(verts, want_verts, atol=1e-5)
    np.testing.assert_allclose(norms, want_norms, atol=1e-5)
    np.testing.assert_allclose(colors, want_colors, atol=1e-6)
    np.testing.assert_array_equal(idx, want_idx)


def test_each_chain_contributes_its_own_geometry():
    """Splitting must not become dropping: every residue of every drawable
    chain still has ribbon surface wrapped around it.

    Checked by position rather than by counting: each C-alpha must have a
    vertex within radius + 0.5 A of it. The two chains are 3.8 A apart, so if
    either chain were dropped its residues would have nothing nearer than the
    other chain's tube -- far outside that tolerance.
    """
    trace = load_ca_trace(TWO_CHAINS)
    verts, _, _, _ = ribbon_from_cif(TWO_CHAINS, samples_per_segment=4, sides=6)
    assert len(verts) > 0

    for i, ca in enumerate(trace.coords):
        nearest = float(np.linalg.norm(verts - ca, axis=1).min())
        assert nearest < RIBBON_RADIUS + 0.5, (
            f"residue {i} of chain {trace.chain_ids[i]} has no ribbon around "
            f"it (nearest vertex {nearest:.2f} A away) -- its chain looks dropped"
        )

    # Both chains must also contribute a comparable share of the mesh: each
    # has 3 residues, so neither may be a token stub.
    ca_by_chain = {"A": trace.coords[:3], "B": trace.coords[3:]}
    distances = {
        chain_id: np.linalg.norm(verts[:, None, :] - cas[None, :, :], axis=2).min(axis=1)
        for chain_id, cas in ca_by_chain.items()
    }
    owned_by_a = distances["A"] < distances["B"]
    assert owned_by_a.sum() == len(verts) - owned_by_a.sum() == len(verts) // 2


def test_a_chain_too_short_to_spline_is_skipped_not_fatal():
    """minimal.cif's chain B has one residue -- a tube needs at least two.

    That chain must be skipped, not fatal: no exception, no zero-length tube
    stub sitting on top of the lone residue, no NaNs, and the rest of the
    structure still renders exactly as it would on its own.
    """
    trace = load_ca_trace(MINIMAL)
    assert _contiguous_chain_runs(trace.chain_ids) == [("A", 0, 4), ("B", 4, 5)]

    verts, norms, colors, idx = ribbon_from_cif(MINIMAL, samples_per_segment=4, sides=6)

    assert idx.max() < len(verts)
    assert not np.isnan(verts).any()
    assert not np.isnan(norms).any()
    assert not np.isnan(colors).any()

    # What is drawn is chain A's tube and nothing else.
    want_verts, _, want_idx = tube_mesh(
        catmull_rom(trace.coords[0:4], 4), radius=1.6, sides=6
    )
    np.testing.assert_allclose(verts, want_verts, atol=1e-5)
    np.testing.assert_array_equal(idx, want_idx)

    # ...and nothing was drawn around the lone chain-B residue: a degenerate
    # one-ring stub there would put vertices at exactly the tube radius.
    lone_residue = trace.coords[4]
    nearest = float(np.linalg.norm(verts - lone_residue, axis=1).min())
    assert nearest > RIBBON_RADIUS + 0.5, (
        f"something was drawn around the one-residue chain B (nearest vertex "
        f"{nearest:.2f} A away)"
    )


def test_each_chains_colors_come_from_its_own_residues():
    """Per-chain color alignment, asserted by position rather than by index.

    Splitting the ribbon means slicing pLDDT per chain and resampling it
    against *that chain's* sample count. Resampling the whole structure's
    pLDDT once and handing the same array to every chain, or resampling a
    chain's slice to the wrong length, leaves each chain's colors shifted
    relative to its own residues -- which a shape/dtype check cannot see.

    two_chains.cif puts chain A entirely in the ">90" blue band (93-97) and
    chain B entirely in the "<50" orange band (35-45), so every vertex of a
    correctly colored ribbon is exactly one of two RGB triples, decided by
    which chain the vertex is physically wrapped around. Any leakage of one
    chain's confidence into the other's geometry shows up as a vertex whose
    color does not match the backbone it sits on.
    """
    blue = np.array([0x00, 0x53, 0xD6], dtype=np.float64) / 255.0    # >90
    orange = np.array([0xFF, 0x7D, 0x45], dtype=np.float64) / 255.0  # <50

    trace = load_ca_trace(TWO_CHAINS)
    assert list(trace.plddt) == [95.0, 97.0, 93.0, 40.0, 35.0, 45.0]  # pin input

    verts, _, colors, _ = ribbon_from_cif(TWO_CHAINS, samples_per_segment=4, sides=6)
    assert len(colors) == len(verts)

    # Classify every vertex by which chain's backbone it is nearest to. The
    # chains are 3.8 A apart and the tube radius is 1.6 A, so this is
    # unambiguous -- and it is derived from vertex *positions*, never from
    # the order the implementation happened to concatenate its buffers in.
    def nearest_ca_distance(cas):
        return np.linalg.norm(verts[:, None, :] - cas[None, :, :], axis=2).min(axis=1)

    near_a = nearest_ca_distance(trace.coords[:3])
    near_b = nearest_ca_distance(trace.coords[3:])
    on_chain_a = near_a < near_b
    assert on_chain_a.any() and (~on_chain_a).any(), "expected geometry on both chains"

    np.testing.assert_allclose(
        colors[on_chain_a], np.tile(blue, (int(on_chain_a.sum()), 1)), atol=1e-3,
        err_msg="chain A geometry is not carrying chain A's own >90 pLDDT color",
    )
    np.testing.assert_allclose(
        colors[~on_chain_a], np.tile(orange, (int((~on_chain_a).sum()), 1)), atol=1e-3,
        err_msg="chain B geometry is not carrying chain B's own <50 pLDDT color",
    )


def test_a_structure_with_no_drawable_chain_fails_presentably():
    """If skipping short chains leaves nothing at all, say so.

    Returning empty buffers instead would be worse than an error: the
    renderer would upload them happily and the booth would show a blank
    viewport with no indication anything went wrong. GeometryError is the
    exception the UI's error path already knows how to turn into presentable
    text (never a stack trace), and it is what this call raised before chain
    splitting existed, when `tube_mesh` refused the one-point centerline.
    """
    # alt_locs.cif is a single residue in a single chain, resolved down to
    # one C-alpha by occupancy -- one residue total, nothing to spline.
    trace = load_ca_trace("tests/fixtures/structures/alt_locs.cif")
    assert trace.n_residues == 1

    with pytest.raises(GeometryError) as excinfo:
        ribbon_from_cif("tests/fixtures/structures/alt_locs.cif")
    assert "alt_locs.cif" in str(excinfo.value)
