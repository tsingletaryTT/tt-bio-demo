// tests/webview_js/tensix.test.js
const assert = require("assert");

// A fake TensixViz standing in for the real vendored library, recording
// every call this module makes into it -- so this test exercises the
// WIRING (which card gets which call, with what value), not the real
// library's own canvas rendering (tensix-viz's own test suite covers that).
class FakeTensixViz {
  constructor(canvas, opts) {
    this.canvas = canvas; this.opts = opts; this.calls = [];
    FakeTensixViz.constructedCount++;
  }
  activate(mode) { this.calls.push(["activate", mode]); }
  setActivity(a) { this.calls.push(["setActivity", a]); }
  setProgress(p) { this.calls.push(["setProgress", p]); }
  // Real TensixViz.reset() is what actually halts its indefinite
  // requestAnimationFrame loop (see webview/static/tensix.js's own comment
  // on why) -- recorded here so the teardown test below can prove it is
  // really called on every outgoing instance, not just that a new one gets
  // built.
  reset() { this.calls.push(["reset"]); }
}
FakeTensixViz.constructedCount = 0;
global.window = global.window || {};
global.window.TensixViz = FakeTensixViz;

// A fake `#tensix-panel` container, tracking appends/clears so the
// no-op-vs-rebuild tests below can assert on the DOM side too, not just on
// which FakeTensixViz instances exist.
const fakeContainer = {
  children: [],
  clearedCount: 0,
  appendChild(el) { this.children.push(el); },
  replaceChildren() { this.children = []; this.clearedCount++; },
};
global.document = {
  createElement: () => ({ getContext: () => ({}) }),
  getElementById: (id) => (id === "tensix-panel" ? fakeContainer : null),
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

// ── re-init with the SAME card set must be a total no-op ──────────────────
//
// `hello` is re-delivered on every EventSource reconnect (a designed-for,
// expected occurrence -- see DaemonLink.last_hello / SSE_KEEPALIVE_S in
// webview/bridge.py), not a one-time event, so app.js calls Tensix.init on
// every one. Rebuilding on a re-delivered hello with an unchanged card set
// must not construct anything new, must not touch the DOM, and must not
// disturb the instances a visitor is currently watching animate.
const constructedBeforeNoOp = FakeTensixViz.constructedCount;
const childrenBeforeNoOp = fakeContainer.children.length;
const clearedBeforeNoOp = fakeContainer.clearedCount;
const inst0Before = instances[0];
const inst1Before = instances[1];
// Deliberately reversed order -- the same SET, different order -- since the
// no-op check must be order-independent (a re-delivered `hello`'s own
// `cards` list is not guaranteed to repeat in the same order).
const instancesAfterNoOp = Tensix.init([1, 0]);
assert.strictEqual(FakeTensixViz.constructedCount, constructedBeforeNoOp,
  "same card set must not construct any new TensixViz instance");
assert.strictEqual(fakeContainer.children.length, childrenBeforeNoOp,
  "same card set must not touch the DOM");
assert.strictEqual(fakeContainer.clearedCount, clearedBeforeNoOp,
  "same card set must not clear the panel");
assert.strictEqual(instancesAfterNoOp, instances,
  "same card set must return the existing instances map unchanged");
assert.strictEqual(instancesAfterNoOp[0], inst0Before,
  "same card set must not replace an existing chip's instance");
assert.strictEqual(instancesAfterNoOp[1], inst1Before,
  "same card set must not replace an existing chip's instance");
// The earlier onStage/onTelemetry call history must still be there -- proof
// this is really the SAME object, not a fresh one that merely reuses the key.
assert.ok(inst0Before.calls.some(c => c[0] === "activate"));

// ── re-init with a genuinely DIFFERENT card set must tear down and rebuild ──
const constructedBeforeRebuild = FakeTensixViz.constructedCount;
const clearedBeforeRebuild = fakeContainer.clearedCount;
const instancesAfterRebuild = Tensix.init([0, 2]);
assert.strictEqual(FakeTensixViz.constructedCount, constructedBeforeRebuild + 2,
  "a genuinely different card set must construct fresh instances for every card");
assert.strictEqual(fakeContainer.clearedCount, clearedBeforeRebuild + 1,
  "a genuinely different card set must clear the panel's old canvases");
assert.strictEqual(fakeContainer.children.length, 2,
  "the panel must hold exactly the new card set's canvases, not the old ones piled on top");
assert.notStrictEqual(instancesAfterRebuild[0], inst0Before,
  "chip 0's instance must be a fresh one, not the one from before the rebuild");
// Card 1 dropped out of the new set entirely -- its outgoing TensixViz
// instance must have had its real stop method called (see
// webview/static/tensix.js's own comment: reset() is what actually cancels
// its indefinite requestAnimationFrame loop), or it would keep animating
// forever in the background on a canvas nobody can see.
assert.ok(inst1Before.calls.some(c => c[0] === "reset"),
  "an instance dropped by a rebuild must have had reset() called on it");
assert.strictEqual(Object.keys(instancesAfterRebuild).length, 2);
assert.ok(instancesAfterRebuild[2] instanceof FakeTensixViz);

console.log("tensix.test.js: OK");
