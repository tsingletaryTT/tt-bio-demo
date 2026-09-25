const assert = require("assert");
const { HelpPanel } = require("../../webview/static/help_panel.js");

// Ordinary case: real content from a /help.json-shaped payload renders into
// the container's innerHTML -- the same shape webview/bridge.py's
// build_help_payload() produces (see tests/unit/test_webview_bridge.py).
const container = { innerHTML: "" };
HelpPanel.render(container, {
  intro: ["A protein folds itself."],
  keys: [["? or F1", "this card, any time"]],
  panels: ["The pipeline panel shows six stages."],
  plddt_legend: [["plddt-high", "90+", "very high, trust it"]],
});
assert.ok(container.innerHTML.includes("A protein folds itself."));
assert.ok(container.innerHTML.includes("this card, any time"));
assert.ok(container.innerHTML.includes("The pipeline panel shows six stages."));
assert.ok(container.innerHTML.includes("plddt-high"));

// Escaping: help text is real, human-written copy (ui.app's own strings),
// never HTML -- so a "<" from ordinary punctuation (or, in principle, any
// future text containing markup-looking characters) must render as inert
// text, never as a tag, attribute or broken markup. Exercised across all
// four render sites (intro paragraph, key label, key meaning, panel
// paragraph, legend range/meaning) since each is a separate template
// literal in help_panel.js and any one of them skipping escapeHtml would
// be a real injection hole in a page whose /help.json content already
// travels over an unauthenticated-by-default HTTP surface (this bridge's
// own module docstring: a non-loopback bind is the documented, supported
// mode).
const xssContainer = { innerHTML: "" };
HelpPanel.render(xssContainer, {
  intro: ['<img src=x onerror="window.pwned=1">'],
  keys: [["<b>Q</b>", "quad view & more <script>alert(1)</script>"]],
  panels: ["Tensix panel — 'quotes' & <tags>"],
  plddt_legend: [["<script>bad</script>", "90+ & up", "very <high>"]],
});
assert.ok(
  !xssContainer.innerHTML.includes("<img"),
  "raw <img> tag leaked into rendered markup"
);
assert.ok(
  !xssContainer.innerHTML.includes("<script>"),
  "raw <script> tag leaked into rendered markup"
);
assert.ok(
  !xssContainer.innerHTML.includes("<b>Q</b>"),
  "raw <b> tag leaked into rendered markup"
);
assert.ok(
  !xssContainer.innerHTML.includes("<tags>"),
  "raw <tags> leaked into rendered markup"
);
assert.ok(
  !xssContainer.innerHTML.includes("<high>"),
  "raw <high> leaked into rendered markup"
);
// The escaped forms are present in some encoded shape (entities), proving
// the content was not silently dropped, only neutralized.
assert.ok(xssContainer.innerHTML.includes("&lt;img"));
assert.ok(xssContainer.innerHTML.includes("&lt;script&gt;"));
assert.ok(xssContainer.innerHTML.includes("&amp;"));

console.log("help_panel.test.js: OK");
