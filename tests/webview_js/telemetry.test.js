const assert = require("assert");
const { formatChip } = require("../../webview/static/telemetry.js");

assert.deepStrictEqual(
  formatChip({index: 0, board_type: "p300c", temperature_c: 47.8, power_w: 19.0, aiclk_mhz: 800.0, board_id: "B0"}),
  {label: "CHIP 0 · P300C", temp: "47.8°C", rest: "19W · 800MHz"});

assert.deepStrictEqual(
  formatChip({index: 2, board_type: "p300c", temperature_c: null, power_w: null, aiclk_mhz: null, board_id: null}),
  {label: "CHIP 2 · P300C", temp: "—", rest: "—"});

console.log("telemetry.test.js: OK");
