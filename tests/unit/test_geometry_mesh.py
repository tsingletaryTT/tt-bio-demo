import numpy as np
import pytest

from ui.geometry import (
    GeometryError,
    catmull_rom,
    plddt_colors,
    resample_scalar,
    tube_mesh,
)


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
