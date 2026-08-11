import numpy as np

from ui.mathutil import identity, look_at, perspective, rotation_y


def test_identity_is_identity():
    np.testing.assert_allclose(identity(), np.eye(4, dtype=np.float32))


def test_perspective_has_expected_shape_and_dtype():
    m = perspective(45.0, 16 / 9, 0.1, 100.0)
    assert m.shape == (4, 4)
    assert m.dtype == np.float32


def test_perspective_maps_near_plane_to_minus_one():
    near, far = 0.5, 50.0
    m = perspective(60.0, 1.0, near, far)
    point = np.array([0.0, 0.0, -near, 1.0], dtype=np.float32)
    clip = m.T @ point          # column-major storage, so transpose to apply
    assert np.isclose(clip[2] / clip[3], -1.0, atol=1e-5)


def test_perspective_maps_far_plane_to_plus_one():
    near, far = 0.5, 50.0
    m = perspective(60.0, 1.0, near, far)
    point = np.array([0.0, 0.0, -far, 1.0], dtype=np.float32)
    clip = m.T @ point
    assert np.isclose(clip[2] / clip[3], 1.0, atol=1e-5)


def test_look_at_places_target_on_negative_z_axis():
    eye = np.array([0.0, 0.0, 10.0])
    view = look_at(eye, np.zeros(3), np.array([0.0, 1.0, 0.0]))
    origin = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
    seen = view.T @ origin
    np.testing.assert_allclose(seen[:3], [0.0, 0.0, -10.0], atol=1e-5)


def test_look_at_maps_eye_to_the_view_space_origin_when_off_axis():
    # A regression guard: an axis-aligned eye (see the test above) leaves the
    # rotation part of the view matrix as the identity, so a row/column
    # transposition bug in the rotation block is invisible from that case
    # alone. Placing the eye off-axis exercises the actual rotation.
    eye = np.array([5.0, 0.0, 0.0])
    view = look_at(eye, np.zeros(3), np.array([0.0, 1.0, 0.0]))
    eye_h = np.array([5.0, 0.0, 0.0, 1.0], dtype=np.float32)
    seen = view.T @ eye_h
    np.testing.assert_allclose(seen[:3], [0.0, 0.0, 0.0], atol=1e-5)


def test_look_at_rotation_block_is_orthonormal():
    eye = np.array([3.0, 4.0, 5.0])
    view = look_at(eye, np.zeros(3), np.array([0.0, 1.0, 0.0]))
    upper = view.T[:3, :3]
    np.testing.assert_allclose(upper @ upper.T, np.eye(3), atol=1e-6)


def test_rotation_y_by_ninety_degrees_maps_x_to_minus_z():
    m = rotation_y(np.pi / 2)
    v = np.array([1.0, 0.0, 0.0, 1.0], dtype=np.float32)
    np.testing.assert_allclose((m.T @ v)[:3], [0.0, 0.0, -1.0], atol=1e-6)


def test_rotation_y_leaves_the_y_axis_fixed():
    m = rotation_y(1.1)
    v = np.array([0.0, 3.0, 0.0, 1.0], dtype=np.float32)
    np.testing.assert_allclose((m.T @ v)[:3], [0.0, 3.0, 0.0], atol=1e-6)


def test_rotation_is_orthonormal():
    for angle in (0.7, -1.3):
        upper = rotation_y(angle)[:3, :3]
        np.testing.assert_allclose(upper @ upper.T, np.eye(3), atol=1e-6)
