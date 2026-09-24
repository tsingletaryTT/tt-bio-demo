# webview_js tests

Plain Node.js scripts, no test framework -- matching this project's own "no framework,
minimal dependency" ethos already applied to `tests/unit/test_run_webview_sh.py`'s shell
tests. Each `*.test.js` file is a standalone script using Node's built-in `assert`; a
non-zero exit means a failure. Run all of them with `scripts/test-webview-js.sh`.

Files under test (`webview/static/*.js`) are loaded with plain `require()` after being
adapted with `module.exports` at the bottom of each file -- see any `*.test.js` for the
pattern. This does not change how a browser loads them (a browser never sees
`module.exports`; `typeof module !== "undefined"` guards it in every source file).
