const assert = require("assert");
const { identity, perspective, lookAt, rotationY, multiply } =
  require("../../webview/static/mat4.js");

const id = identity();
assert.strictEqual(id.length, 16);
assert.strictEqual(id[0], 1); assert.strictEqual(id[5], 1);
assert.strictEqual(id[10], 1); assert.strictEqual(id[15], 1);
assert.strictEqual(id[1], 0);

// look_at from (0,0,5) toward the origin, up=(0,1,0): forward is -Z, so the
// view matrix's third COLUMN (indices 8,9,10 in column-major [col][row])
// should be +Z (since m[:3,2] = -forward = -(-1) = +1 on Z).
const view = lookAt([0, 0, 5], [0, 0, 0], [0, 1, 0]);
assert.ok(Math.abs(view[10] - 1.0) < 1e-6,
  "look_at's forward/-forward basis must match ui/mathutil.py's own convention " +
  "(m[:3,2] = -forward) -- this is the exact bug class this project already " +
  "paid to find once (rows vs columns for a column-major matrix)");

const proj = perspective(60, 1.0, 0.1, 100.0);
assert.ok(proj[0] > 0 && proj[5] > 0);
assert.ok(Math.abs(proj[11] - (-1.0)) < 1e-6);

const rot = rotationY(Math.PI / 2);
assert.ok(Math.abs(rot[0]) < 1e-6, "cos(90deg) ~ 0");
assert.ok(Math.abs(rot[2] - 1.0) < 1e-6, "sin(90deg) = 1 per ui/mathutil.py's rotation matrix");

const m = multiply(identity(), identity());
assert.deepStrictEqual(Array.from(m), Array.from(identity()));

console.log("mat4.test.js: OK");
