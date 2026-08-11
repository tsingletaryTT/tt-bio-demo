"""Column-major 4x4 matrices for the GL pipeline.

Every matrix here is stored the way OpenGL wants to receive it, so uniforms are
uploaded with transpose=GL_FALSE. To apply one to a column vector in numpy,
transpose it first: `m.T @ v`.
"""

import numpy as np


def identity():
    return np.eye(4, dtype=np.float32)


def perspective(fovy_deg, aspect, near, far):
    """Standard OpenGL perspective projection, depth range [-1, 1]."""
    f = 1.0 / np.tan(np.radians(fovy_deg) / 2.0)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = -1.0
    m[3, 2] = (2.0 * far * near) / (near - far)
    return m


def look_at(eye, target, up):
    """Right-handed view matrix looking from `eye` toward `target`."""
    eye = np.asarray(eye, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    up = np.asarray(up, dtype=np.float64)

    forward = target - eye
    forward /= np.linalg.norm(forward)
    side = np.cross(forward, up)
    side /= np.linalg.norm(side)
    true_up = np.cross(side, forward)

    m = np.eye(4, dtype=np.float32)
    m[0, :3] = side
    m[1, :3] = true_up
    m[2, :3] = -forward
    m[3, 0] = -np.dot(side, eye)
    m[3, 1] = -np.dot(true_up, eye)
    m[3, 2] = np.dot(forward, eye)
    return m


def rotation_y(angle_rad):
    c, s = np.cos(angle_rad), np.sin(angle_rad)
    m = np.eye(4, dtype=np.float32)
    m[0, 0], m[0, 2] = c, -s
    m[2, 0], m[2, 2] = s, c
    return m
