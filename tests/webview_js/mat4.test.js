const assert = require("assert");
const { identity, perspective, lookAt, rotationY, multiply } =
  require("../../webview/static/mat4.js");

const id = identity();
assert.strictEqual(id.length, 16);
assert.strictEqual(id[0], 1); assert.strictEqual(id[5], 1);
assert.strictEqual(id[10], 1); assert.strictEqual(id[15], 1);
assert.strictEqual(id[1], 0);

// lookAt test with off-axis camera that would expose the row/column bug.
// PyOpenGL uploads numpy's C-order (row-major) buffer, GL interprets as
// column-major, so the actual GL matrix is the transpose of the numpy array
// as written. This test verifies the correct transposed layout by checking
// off-diagonal elements that depend on basis-vector placement.
// Verified against ui.mathutil.look_at([3,2,5], [0,0,0], [0,1,0]) which
// produces (in C-order flat): [0.8574929, -0.16692446, 0.48666427, ...]
const view = lookAt([3.0, 2.0, 5.0], [0.0, 0.0, 0.0], [0.0, 1.0, 0.0]);
// view[1] = trueUp[0] ≈ -0.16692446
assert.ok(Math.abs(view[1] - (-0.16692446)) < 1e-5,
  "lookAt off-diagonal [1]: trueUp[0] must be correct (catches row/column transpose bug)");
// view[2] = -forward[0] ≈ 0.48666427
assert.ok(Math.abs(view[2] - 0.48666427) < 1e-5,
  "lookAt off-diagonal [2]: -forward[0] must be correct (catches row/column transpose bug)");
// view[8] = side[2] ≈ -0.51449573
assert.ok(Math.abs(view[8] - (-0.51449573)) < 1e-5,
  "lookAt off-diagonal [8]: side[2] must be correct (catches row/column transpose bug)");

const proj = perspective(60, 1.0, 0.1, 100.0);
assert.ok(proj[0] > 0 && proj[5] > 0);
assert.ok(Math.abs(proj[11] - (-1.0)) < 1e-6);

// rotationY test: check BOTH sign positions, not just one.
// Verified against ui.mathutil.rotation_y(π/2) which produces (in C-order):
// [~0, 0, -1, 0, 0, 1, 0, 0, 1, 0, ~0, 0, ...]
const rot = rotationY(Math.PI / 2);
assert.ok(Math.abs(rot[0]) < 1e-6, "cos(90deg) ~ 0");
// rot[2] = -sin(π/2) = -1.0
assert.ok(Math.abs(rot[2] - (-1.0)) < 1e-6,
  "rotationY[2] = -sin(90deg) = -1 (catches sign-swap bug)");
// rot[8] = sin(π/2) = 1.0 -- checking both prevents sign-swap mutations
assert.ok(Math.abs(rot[8] - 1.0) < 1e-6,
  "rotationY[8] = sin(90deg) = 1 (verifies sign is not swapped)");

const m = multiply(identity(), identity());
assert.deepStrictEqual(Array.from(m), Array.from(identity()));

console.log("mat4.test.js: OK");
