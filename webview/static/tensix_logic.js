"use strict";

// Direct ports of ui/chipviz.py's pure mode/activity logic and
// protocol/events.py's within_stage_frac -- see
// tests/unit/test_webview_js_constants.py for the drift test that keeps
// these literal tables from silently disagreeing with the Python source.

const MODE_BY_STAGE = {
  msa: "idle", prep: "idle", trunk: "thinking",
  diffusion: "diffusion", confidence: "inference", saving: "idle",
};
const UNKNOWN_STAGE_MODE = "inference";

const MODE_CAPTION = {
  idle: "idle", inference: "scoring", thinking: "refining", diffusion: "denoising",
};

const POWER_FLOOR_W = 15.0;
const POWER_CEILING_W = 90.0;
const POWER_CURVE = 0.6;

// Verbatim from protocol/events.py's STAGE_BANDS (2026-09-24)
const STAGE_BANDS = {
  msa: [0.00, 0.05], prep: [0.05, 0.10], trunk: [0.10, 0.15],
  diffusion: [0.15, 0.95], confidence: [0.95, 0.98], saving: [0.98, 1.00],
};

function vizMode(notReady, stage) {
  if (notReady) return "idle";
  if (stage == null) return "idle";
  return Object.prototype.hasOwnProperty.call(MODE_BY_STAGE, stage)
    ? MODE_BY_STAGE[stage] : UNKNOWN_STAGE_MODE;
}

function modeCaption(mode) {
  return Object.prototype.hasOwnProperty.call(MODE_CAPTION, mode) ? MODE_CAPTION[mode] : mode;
}

function powerActivity(watts) {
  const span = POWER_CEILING_W - POWER_FLOOR_W;
  if (!(span > 0) || typeof watts !== "number" || !isFinite(watts)) return 0.0;
  const fraction = Math.max(0.0, Math.min(1.0, (watts - POWER_FLOOR_W) / span));
  return Math.pow(fraction, POWER_CURVE);
}

function withinStageFrac(stage, frac) {
  const band = STAGE_BANDS[stage];
  // Unknown stage: pass through the clamped frac (symmetric with Python's
  // within_stage_frac -- see protocol/events.py line 217-218).
  if (!band) {
    if (typeof frac !== "number" || !isFinite(frac)) return null;
    return Math.max(0.0, Math.min(1.0, frac));
  }
  // Known stage: convert wire frac to within-stage frac.
  if (typeof frac !== "number" || !isFinite(frac)) return null;
  const [lo, hi] = band;
  const span = hi - lo;
  if (span <= 0) return 0.0;
  return Math.max(0.0, Math.min(1.0, (frac - lo) / span));
}

if (typeof module !== "undefined") {
  module.exports = { vizMode, modeCaption, powerActivity, withinStageFrac,
                      MODE_BY_STAGE, MODE_CAPTION, UNKNOWN_STAGE_MODE,
                      POWER_FLOOR_W, POWER_CEILING_W, POWER_CURVE, STAGE_BANDS };
}
if (typeof window !== "undefined") {
  window.TensixLogic = { vizMode, modeCaption, powerActivity, withinStageFrac };
}
