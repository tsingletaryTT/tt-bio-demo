const assert = require("assert");

// A small but real classList stub (add/remove/toggle/contains all backed by
// one Set) -- the brief's original mock only had add()/remove(), which
// qa_panel.js's `classList.toggle("qa-error", ...)` call does not have in a
// real DOM either; real elements always expose toggle, so the stub should
// too rather than the implementation avoiding a method every actual browser
// provides.
function makeClassList() {
  const classes = new Set();
  return {
    add(c) { classes.add(c); },
    remove(c) { classes.delete(c); },
    toggle(c, force) {
      const on = force !== undefined ? force : !classes.has(c);
      if (on) classes.add(c); else classes.delete(c);
      return on;
    },
    contains(c) { return classes.has(c); },
  };
}

function makeDom() {
  return {
    pending: { textContent: "" },
    inFlight: { innerHTML: "", classList: makeClassList() },
    answered: { textContent: "", classList: makeClassList() },
  };
}
global.document = { createElement: () => ({ style: {} }) };

const { QaPanel } = require("../../webview/static/qa_panel.js");

// Ordinary case: real, distinct text on all three rows, no error class.
const dom = makeDom();
QaPanel.render(dom, {pending: "Does X bind Y? (not yet timed)",
                     in_flight: "Checking — Does A bind B?",
                     answered: "score: 0.94/1.0 — nesso1's predicted probability the ligand binds",
                     answered_is_error: false});
assert.strictEqual(dom.pending.textContent, "Does X bind Y? (not yet timed)");
assert.strictEqual(dom.answered.textContent.includes("0.94"), true);
assert.strictEqual(dom.answered.classList.contains("qa-error"), false);

// Error case: the answered row gets the qa-error class.
const domErr = makeDom();
QaPanel.render(domErr, {pending: "No questions queued",
                        in_flight: "No question in flight",
                        answered: "Does A bind B? — not answered (error)",
                        answered_is_error: true});
assert.strictEqual(domErr.answered.classList.contains("qa-error"), true);

// A later, non-error render on the SAME dom clears the class again --
// classList.toggle(cls, false), not a one-way add.
QaPanel.render(domErr, {pending: "No questions queued",
                        in_flight: "No question in flight",
                        answered: "score: 0.5/1.0 — nesso1's predicted probability the ligand binds",
                        answered_is_error: false});
assert.strictEqual(domErr.answered.classList.contains("qa-error"), false);

console.log("qa_panel.test.js: OK");
