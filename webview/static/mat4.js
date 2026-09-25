"use strict";

// Column-major 4x4 matrices for WebGL, ported directly from ui/mathutil.py
// -- including its look_at basis, which this project already found and
// fixed a real bug in once (rows where column-major storage needs
// columns, wrong for every off-axis camera -- 2026-08-11 review). Ported
// from the ALREADY-CORRECTED source, not re-derived from scratch, so that
// bug class is not available to reintroduce here. Every matrix is a flat
// Float32Array(16); `gl.uniformMatrix4fv(loc, false, m)` (transpose=false)
// matches this convention exactly, the same way ui/viewer.py's own
// GL.glUniformMatrix4fv(..., GL_FALSE, ...) does.

function identity() {
  const m = new Float32Array(16);
  m[0] = m[5] = m[10] = m[15] = 1;
  return m;
}

function perspective(fovyDeg, aspect, near, far) {
  const f = 1.0 / Math.tan((fovyDeg * Math.PI / 180) / 2.0);
  const m = new Float32Array(16);
  m[0] = f / aspect;
  m[5] = f;
  m[10] = (far + near) / (near - far);
  m[11] = -1.0;
  m[14] = (2.0 * far * near) / (near - far);
  return m;
}

function _sub(a, b) { return [a[0]-b[0], a[1]-b[1], a[2]-b[2]]; }
function _cross(a, b) {
  return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
}
function _dot(a, b) { return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]; }
function _norm(v) {
  const n = Math.hypot(v[0], v[1], v[2]);
  return [v[0]/n, v[1]/n, v[2]/n];
}

function lookAt(eye, target, up) {
  const forward = _norm(_sub(target, eye));
  const side = _norm(_cross(forward, up));
  const trueUp = _cross(side, forward);
  const m = identity();
  // PyOpenGL uploads the numpy array's raw C-order (row-major) buffer, and GL
  // interprets it as column-major, so the actual GL matrix is the transpose of
  // the numpy array as written. Therefore, Python's m[:3, 0] = side (rows 0-2
  // of column 0) becomes GL's ROW 0. Each basis vector becomes a ROW, spread
  // at indices k, k+4, k+8 (for row k in column-major indexing).
  m[0] = side[0];     m[4] = side[1];     m[8]  = side[2];
  m[1] = trueUp[0];   m[5] = trueUp[1];   m[9]  = trueUp[2];
  m[2] = -forward[0]; m[6] = -forward[1]; m[10] = -forward[2];
  m[12] = -_dot(side, eye);
  m[13] = -_dot(trueUp, eye);
  m[14] = _dot(forward, eye);
  return m;
}

function rotationY(angleRad) {
  const c = Math.cos(angleRad), s = Math.sin(angleRad);
  const m = identity();
  m[0] = c; m[2] = -s;
  m[8] = s; m[10] = c;
  return m;
}

function multiply(a, b) {
  const out = new Float32Array(16);
  for (let col = 0; col < 4; col++) {
    for (let row = 0; row < 4; row++) {
      let sum = 0;
      for (let k = 0; k < 4; k++) sum += a[k*4 + row] * b[col*4 + k];
      out[col*4 + row] = sum;
    }
  }
  return out;
}

const Mat4 = { identity, perspective, lookAt, rotationY, multiply };
if (typeof module !== "undefined") module.exports = Mat4;
if (typeof window !== "undefined") window.Mat4 = Mat4;
