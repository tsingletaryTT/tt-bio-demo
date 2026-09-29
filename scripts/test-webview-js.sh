#!/usr/bin/env bash
# scripts/test-webview-js.sh -- run every tests/webview_js/*.test.js under plain node.
# No framework: each file is a standalone script using Node's built-in `assert`; a
# non-zero exit from `node` is a failure. See tests/webview_js/README.md.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEST_DIR="${REPO_ROOT}/tests/webview_js"

if ! command -v node >/dev/null 2>&1; then
  echo "test-webview-js.sh: node not found -- install Node.js to run these tests" >&2
  exit 1
fi

fail=0
count=0
for test_file in "$TEST_DIR"/*.test.js; do
  [[ -e "$test_file" ]] || continue
  count=$((count + 1))
  echo "== $(basename "$test_file") ==" >&2
  if ! node "$test_file"; then
    fail=1
  fi
done

if [[ "$count" -eq 0 ]]; then
  echo "test-webview-js.sh: no *.test.js files found under $TEST_DIR" >&2
  exit 1
fi

if [[ "$fail" -ne 0 ]]; then
  echo "test-webview-js.sh: one or more JS tests FAILED" >&2
  exit 1
fi
echo "test-webview-js.sh: all $count JS test file(s) passed" >&2
