const assert = require("assert");
const { vizMode, modeCaption, powerActivity, withinStageFrac } =
  require("../../webview/static/tensix_logic.js");

assert.strictEqual(vizMode(true, "diffusion"), "idle");
assert.strictEqual(vizMode(false, null), "idle");
assert.strictEqual(vizMode(false, "diffusion"), "diffusion");
assert.strictEqual(vizMode(false, "trunk"), "thinking");
assert.strictEqual(vizMode(false, "confidence"), "inference");
assert.strictEqual(vizMode(false, "msa"), "idle");
assert.strictEqual(vizMode(false, "some-future-stage"), "inference");

assert.strictEqual(modeCaption("diffusion"), "denoising");
assert.strictEqual(modeCaption("thinking"), "refining");
assert.strictEqual(modeCaption("unknown-mode"), "unknown-mode");

assert.ok(Math.abs(powerActivity(15.0) - 0.0) < 1e-9);
assert.ok(Math.abs(powerActivity(90.0) - 1.0) < 1e-9);
assert.ok(powerActivity(52.5) > 0.5, "the curve boosts the midpoint above linear 0.5");

const withinDiffusion = withinStageFrac("diffusion", 0.55);
assert.ok(Math.abs(withinDiffusion - 0.5) < 1e-6);

console.log("tensix_logic.test.js: OK");
