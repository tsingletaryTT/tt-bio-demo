"use strict";

// HelpPanel.render -- pure DOM update for the `?` help overlay, driven
// entirely by webview/bridge.py's GET /help.json response (see
// build_help_payload() there). Every string this renders is real,
// human-written copy from ui.app's own help-content functions (the exact
// same ones the native GTK `?` card is built from) -- this file does no
// text composition of its own, only escaping and markup, so there is
// never a second, hand-typed copy of the booth's help wording to drift
// from the real one. Same pattern as qa_panel.js: a plain object with one
// `render(container, data)` function, dual-exported for Node's test
// harness (module.exports) and the browser (window.HelpPanel).

// /help.json's content is real copy, not attacker input, but it does
// travel over this bridge's public HTTP surface (the module docstring in
// webview/bridge.py documents a non-loopback bind as a supported mode) --
// so every piece of text below is escaped before it goes into innerHTML,
// the same discipline as if it were untrusted. Un-escaped text containing
// "<"/">"/"&" would otherwise be parsed as markup and could break the
// card's own layout, not just look wrong.
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function render(container, data) {
  const intro = data.intro.map(p => `<p>${escapeHtml(p)}</p>`).join("");
  const keys = data.keys.map(([k, m]) =>
    `<div class="help-key-row"><span class="help-key">${escapeHtml(k)}</span>` +
    `<span class="help-desc">${escapeHtml(m)}</span></div>`).join("");
  const panels = data.panels.map(p => `<p>${escapeHtml(p)}</p>`).join("");
  const legend = data.plddt_legend.map(([cls, range, meaning]) =>
    `<div class="help-legend-row"><span class="plddt-swatch ${escapeHtml(cls)}"></span>` +
    `${escapeHtml(range)} · ${escapeHtml(meaning)}</div>`).join("");
  container.innerHTML =
    `<h2>What you are looking at</h2>${intro}` +
    `<h3>Keys</h3>${keys}` +
    `<h3>The panels</h3>${panels}` +
    `<h3>Confidence colour</h3>${legend}`;
}

const HelpPanel = { render };
if (typeof module !== "undefined") module.exports = { HelpPanel };
if (typeof window !== "undefined") window.HelpPanel = HelpPanel;
