"use strict";

// Real per-chip telemetry, matching ui/panels.py's TelemetryPanel -- one row
// per chip, an em dash for a field the sampler could not read, never a
// fabricated number. `formatChip` is pure (no DOM) so it is directly
// testable; `render` is the thin DOM half.

function formatChip(chip) {
  const temp = chip.temperature_c == null ? "—" : `${chip.temperature_c.toFixed(1)}°C`;
  const power = chip.power_w == null ? null : `${Math.round(chip.power_w)}W`;
  const clock = chip.aiclk_mhz == null ? null : `${Math.round(chip.aiclk_mhz)}MHz`;
  const rest = (power == null && clock == null) ? "—"
    : `${power ?? "—"} · ${clock ?? "—"}`;
  return {
    label: `CHIP ${chip.index} · ${chip.board_type.toUpperCase()}`,
    temp,
    rest,
  };
}

function render(container, chips) {
  container.innerHTML = "";
  for (const chip of chips) {
    const { label, temp, rest } = formatChip(chip);
    const cell = document.createElement("div");
    cell.className = "telemetry-cell";
    cell.innerHTML =
      `<div class="telemetry-label">${label}</div>` +
      `<div class="telemetry-temp">${temp}</div>` +
      `<div class="telemetry-rest">${rest}</div>`;
    container.appendChild(cell);
  }
}

if (typeof module !== "undefined") {
  module.exports = { formatChip, render };
}
if (typeof window !== "undefined") {
  window.Telemetry = { formatChip, render };
}
