// webview/static/tensix.js
"use strict";

// Real per-chip Tensix activity, driven by the SAME vendored tensix-viz.js
// the GTK app ships (served verbatim by the bridge -- see webview/bridge.py's
// _STATIC_CONTENT_TYPES and Step 5 below). Mirrors ui/chipviz.py's
// build_page_html wrapper: one TensixViz instance per chip canvas, .activate
// on stage change, .setActivity from telemetry, .setProgress from the wire's
// own stage.frac.

const { vizMode, withinStageFrac, powerActivity } =
  (typeof require !== "undefined") ? require("./tensix_logic.js") : window.TensixLogic;

const instances = {};
let lastMode = {};

function init(cards) {
  for (const key of Object.keys(instances)) delete instances[key];
  for (const card of cards) {
    const canvas = document.createElement("canvas");
    canvas.width = 86;
    canvas.height = 104;
    // Guarded: a real DOM canvas always has `.dataset`, but the wiring test's
    // fake `document.createElement` stands in a bare object with only
    // `getContext` -- this line exercises no assertion either fake or real,
    // so it is skipped rather than made to fail a test it isn't the subject of.
    if (canvas.dataset) canvas.dataset.card = String(card);
    const container = (typeof document.getElementById === "function")
      ? document.getElementById("tensix-panel") : null;
    if (container) container.appendChild(canvas);
    instances[card] = new window.TensixViz(canvas, { arch: "blackhole", showMemory: true });
    lastMode[card] = "idle";
  }
  return instances;
}

function onStage(card, stage, frac) {
  const inst = instances[card];
  if (!inst) return;
  const mode = vizMode(false, stage);
  if (mode !== lastMode[card]) {
    inst.activate(mode);
    lastMode[card] = mode;
  }
  const progress = withinStageFrac(stage, frac);
  if (progress != null) inst.setProgress(progress);
}

function onNotReady(cards) {
  for (const card of cards) {
    const inst = instances[card];
    if (inst && lastMode[card] !== "idle") {
      inst.activate("idle");
      lastMode[card] = "idle";
    }
  }
}

function onTelemetry(chips) {
  for (const chip of chips) {
    const inst = instances[chip.index];
    if (!inst || chip.power_w == null) continue;
    inst.setActivity(powerActivity(chip.power_w));
  }
}

const Tensix = { init, onStage, onNotReady, onTelemetry };
if (typeof module !== "undefined") module.exports = { Tensix };
if (typeof window !== "undefined") window.Tensix = Tensix;
