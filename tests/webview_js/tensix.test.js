// tests/webview_js/tensix.test.js
const assert = require("assert");

// A fake TensixViz standing in for the real vendored library, recording
// every call this module makes into it -- so this test exercises the
// WIRING (which card gets which call, with what value), not the real
// library's own canvas rendering (tensix-viz's own test suite covers that).
class FakeTensixViz {
  constructor(canvas, opts) { this.canvas = canvas; this.opts = opts; this.calls = []; }
  activate(mode) { this.calls.push(["activate", mode]); }
  setActivity(a) { this.calls.push(["setActivity", a]); }
  setProgress(p) { this.calls.push(["setProgress", p]); }
}
global.window = global.window || {};
global.window.TensixViz = FakeTensixViz;
global.document = {
  createElement: () => ({ getContext: () => ({}) }),
};

const { Tensix } = require("../../webview/static/tensix.js");

const instances = Tensix.init([0, 1]);
assert.strictEqual(Object.keys(instances).length, 2);

Tensix.onStage(0, "diffusion", 0.55);
// withinStageFrac("diffusion", 0.55) is 0.5 mathematically, but IEEE754
// float subtraction (0.95 - 0.15 !== 0.8 exactly) makes the real value
// 0.5000000000000001 -- the same reason tests/webview_js/tensix_logic.test.js
// already compares this exact case with an epsilon rather than
// assert.strictEqual. Assert the call shape exactly and the value within
// tolerance, rather than a brittle deepStrictEqual on a float literal.
assert.strictEqual(instances[0].calls.length, 2);
assert.deepStrictEqual(instances[0].calls[0], ["activate", "diffusion"]);
assert.strictEqual(instances[0].calls[1][0], "setProgress");
assert.ok(Math.abs(instances[0].calls[1][1] - 0.5) < 1e-6);

Tensix.onTelemetry([{index: 0, power_w: 90.0}, {index: 1, power_w: 15.0}]);
const activityCalls0 = instances[0].calls.filter(c => c[0] === "setActivity");
const activityCalls1 = instances[1].calls.filter(c => c[0] === "setActivity");
assert.ok(Math.abs(activityCalls0[0][1] - 1.0) < 1e-9);
assert.ok(Math.abs(activityCalls1[0][1] - 0.0) < 1e-9);

console.log("tensix.test.js: OK");
