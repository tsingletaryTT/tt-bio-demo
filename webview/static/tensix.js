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

// The card set `instances` was last built for, as a Set -- `null` before the
// first `init()` call. Compared SET-wise (size + membership), not by array
// position: `hello`'s own `cards` list is not guaranteed to repeat in the
// same order on every delivery, and a card set that is unchanged must never
// be treated as "changed" just because of ordering.
let currentCardSet = null;

function _sameCardSet(cards) {
  if (currentCardSet === null) return false;
  if (currentCardSet.size !== cards.length) return false;
  return cards.every((c) => currentCardSet.has(c));
}

function init(cards) {
  // `hello` is re-delivered on every EventSource reconnect (a normal,
  // designed-for occurrence -- see DaemonLink.last_hello and the SSE
  // keepalive/reconnect handling in webview/bridge.py), not a one-time
  // event. Rebuilding unconditionally on every `hello` would silently pile
  // up a second, third, ... generation of live canvases and orphaned
  // TensixViz instances each still running their own indefinite
  // requestAnimationFrame loop in the background -- the exact "short runs
  // cannot see unbounded growth" class of bug this project's CLAUDE.md has
  // already paid for once. So: the SAME card set (regardless of order) is a
  // total no-op -- no DOM touched, nothing (re)constructed, the existing
  // `instances` map returned unchanged.
  if (_sameCardSet(cards)) return instances;

  // The card set genuinely changed (or this is the first call ever): tear
  // down the previous generation before building the new one.
  //
  // TensixViz has no destroy()/dispose() of its own (only the higher-level
  // CardViz/SystemViz/ClusterViz wrappers this project does not use have
  // one -- confirmed by reading ui/assets/tensix-viz/tensix-viz.js, which
  // this project vendors verbatim and must not hand-edit). What it DOES
  // have is `reset()`: it bumps `_animGen` (so the in-flight
  // requestAnimationFrame closure's own `self._animGen !== gen` guard makes
  // it bail out on its very next tick) and cancels the pending rAF id
  // outright via `cancelAnimationFrame`. That is a real stop, not a
  // workaround -- `activate()` itself calls `reset()` first for exactly
  // this reason every time it switches mode.
  for (const key of Object.keys(instances)) {
    const inst = instances[key];
    if (inst && typeof inst.reset === "function") {
      try {
        inst.reset();
      } catch (e) {
        // A failure to stop cleanly must not block tearing the rest of the
        // panel down and rebuilding it -- the DOM clear below is the
        // belt-and-braces fallback for exactly this case.
      }
    }
    delete instances[key];
    delete lastMode[key];
  }
  const container = (typeof document.getElementById === "function")
    ? document.getElementById("tensix-panel") : null;
  // Clear every existing canvas regardless of whether reset() above
  // actually stopped that instance's loop -- so old canvases never pile up
  // in the DOM even in a worst case where some future library version's
  // reset() does not fully halt it.
  if (container && typeof container.replaceChildren === "function") {
    container.replaceChildren();
  }

  for (const card of cards) {
    const canvas = document.createElement("canvas");
    canvas.width = 86;
    canvas.height = 104;
    // Guarded: a real DOM canvas always has `.dataset`, but the wiring test's
    // fake `document.createElement` stands in a bare object with only
    // `getContext` -- this line exercises no assertion either fake or real,
    // so it is skipped rather than made to fail a test it isn't the subject of.
    if (canvas.dataset) canvas.dataset.card = String(card);
    if (container) container.appendChild(canvas);
    instances[card] = new window.TensixViz(canvas, { arch: "blackhole", showMemory: true });
    lastMode[card] = "idle";
  }
  currentCardSet = new Set(cards);
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
