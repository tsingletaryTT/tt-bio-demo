#!/usr/bin/env bash
#
# test.sh — run the whole suite, each half through the one venv that can
# actually run it, and report a single combined pass/fail.
#
# tt-bio-demo is two Python environments (see docs/venv-bootstrap-notes.md):
#   venv-ui     — the GTK4 UI process. Has gi, gemmi, PyOpenGL, numpy. Never
#                 has torch or tt-bio.
#   venv-runner — the compute daemon (runner/). Has torch, ttnn, tt-bio,
#                 numpy. Never has gi. It DOES happen to have a gemmi (a
#                 transitive tt-bio dependency) — but a different version than
#                 venv-ui's, one that silently gives wrong answers on at least
#                 two cases (tests/unit/test_geometry_load.py) instead of
#                 raising ImportError. "It imports fine here" is therefore not
#                 proof a test belongs on this side.
#
# How the split is decided: DIRECTORY, not a marker or a naming convention.
#   tests/unit/runner/   — imports from runner.* (directly or via the module
#                          under test). Runs ONLY under venv-runner.
#   tests/integration/   — hardware-gated (once this directory exists, per
#                          the Phase 3a plan). Runs ONLY under venv-runner.
#   everything else under tests/unit/  — runs ONLY under venv-ui.
#
# A marker was tried first and rejected: pytest must *import* every test
# module under its search path before it can consult that module's markers,
# and tests/unit/'s UI-side modules (ui/app.py, ui/viewer.py, ...) hard-fail
# that import under venv-runner — `ModuleNotFoundError: No module named 'gi'`
# — regardless of any marker on some other file. That turns the whole
# venv-runner invocation into a pytest "Interrupted: N errors during
# collection" (exit 2), which a marker has no power to prevent: the crash
# happens before markers are ever consulted. Only "which directory is this
# file in" decides whether pytest opens the file at all — so that is the
# actual boundary, and it is enforced here via `--ignore` (UI half) and
# explicit paths (runner half), not by relying on pytest.ini's `testpaths`
# default to do the right thing on its own.
#
# Adding a test: if it imports anything from runner/ — even transitively —
# put it in tests/unit/runner/, not tests/unit/. Everything else goes in
# tests/unit/ directly. Guess wrong and it is loud, not silently skipped:
# a runner-side file left in tests/unit/ either explodes the entire UI half's
# collection (if it drags in torch/tt-bio at module scope) or — the sharper
# trap, see the gemmi note above — imports fine under venv-ui and just never
# gets exercised the way it will in production, which is why the rule is
# "imports from runner.*", not "fails to import under venv-ui".
#
# Usage: scripts/test.sh [pytest args...]
#   scripts/test.sh                 # both halves, combined verdict
#   scripts/test.sh -q              # forwarded, unchanged, to BOTH halves
#   scripts/test.sh -k geometry -v  # ditto
#
# Extra arguments are appended, verbatim, to BOTH of the underlying pytest
# invocations below — so `-k foo` filters both halves the same way, and a
# selector matching only one half's tests is fine (the other half just runs
# fewer tests, reported explicitly, never silently skipped as a whole half).
#
#   venv-ui/bin/python3     -m pytest tests/unit --ignore=tests/unit/runner [args...]
#   venv-runner/bin/python3 -m pytest tests/unit/runner [tests/integration] [args...]
#
# (tests/integration is added to the runner invocation only once that
# directory actually exists — see run_half below — so this script does not
# need editing the day the Phase 3a plan creates it.)
#
# Exit status: 0 only if BOTH halves pass. Either half failing outright, OR
# either half's selector matching zero tests (pytest's own exit code 5, "no
# tests ran" — a selector matching nothing must be loud, not quietly green),
# makes this script exit 1. Which half is at fault is named explicitly in the
# combined result at the end, not left for the reader to work out from raw
# pytest output above it.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: scripts/test.sh [pytest args...]

Runs the whole suite in two halves, each through the one venv that can
actually run it, and reports one combined pass/fail:

  venv-ui/bin/python3     -m pytest tests/unit --ignore=tests/unit/runner [args...]
  venv-runner/bin/python3 -m pytest tests/unit/runner [tests/integration] [args...]

How the split is decided: DIRECTORY. tests/unit/runner/ (and tests/integration/,
once it exists) runs only under venv-runner; everything else in tests/unit/
runs only under venv-ui. See this script's header comment for why a pytest
marker was tried and rejected (a UI-side module that fails to import under
venv-runner aborts collection for the whole half before any marker is even
read), and docs/venv-bootstrap-notes.md for the fuller writeup.

Extra arguments (e.g. -q, -k geometry, -v, -x) are appended, UNCHANGED, to
BOTH pytest invocations above.

Exit status: 0 only if BOTH halves pass. Either half failing, or either
half's path selector matching zero tests, makes this exit 1 -- and names
which half in the combined result printed at the end.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

# Matches setup-venvs.sh's default; override with --prefix there and
# TT_BIO_DEMO_PREFIX here if you built the venvs somewhere else.
PREFIX="${TT_BIO_DEMO_PREFIX:-${REPO_ROOT}/.venvs}"
VENV_UI="${PREFIX}/venv-ui"
VENV_RUNNER="${PREFIX}/venv-runner"

require_venv() {
  local venv_dir="$1" label="$2"
  if [[ ! -x "${venv_dir}/bin/python3" ]]; then
    echo "ERROR: ${label} not found at ${venv_dir}." >&2
    echo "Run scripts/setup-venvs.sh first (pass --prefix there and set TT_BIO_DEMO_PREFIX here to match, if you used a custom prefix)." >&2
    exit 1
  fi
}

require_pytest() {
  local venv_dir="$1" label="$2" remedy="$3"
  if ! "${venv_dir}/bin/python3" -c "import pytest" >/dev/null 2>&1; then
    echo "ERROR: pytest is not importable in ${label} (${venv_dir})." >&2
    echo "$remedy" >&2
    exit 1
  fi
}

require_venv "$VENV_UI" "venv-ui"
require_venv "$VENV_RUNNER" "venv-runner"
require_pytest "$VENV_UI" "venv-ui" \
  "venv-ui is built --system-site-packages and should always have pytest via apt's python3-pytest. Rerun scripts/setup-venvs.sh --force to rebuild it."
require_pytest "$VENV_RUNNER" "venv-runner" \
  "'pip install tt-bio' does not pull in pytest -- it is not one of tt-bio's own dependencies. Rerun scripts/setup-venvs.sh --dev to add pytest to venv-runner (see docs/venv-bootstrap-notes.md, '--dev, and why it's a flag, not automatic')."

cd "$REPO_ROOT"

# Extra args the caller passed to this script, forwarded verbatim to BOTH
# pytest invocations below. Captured into a real array (not left as "$@")
# because run_half's own positional params shadow the outer "$@" once it's
# called, and this needs to survive that.
EXTRA_ARGS=("$@")

# Runs one half, streaming its output live (via `tee`) while also keeping a
# copy to pull the final summary line from for the combined report below.
# Args after python_bin are the pytest path/selector arguments for this half
# (e.g. `tests/unit --ignore=tests/unit/runner`); EXTRA_ARGS is appended after
# those.
#
# `set +e` / `set -e` bracketing the pipeline is load-bearing: under
# `set -e` + `pipefail`, a failing pytest makes the *pipeline's* own exit
# status non-zero (pipefail reports the rightmost non-zero, which is
# pytest's, not tee's) -- and since a pipeline counts as a single command for
# `-e` purposes, that would abort this script on the spot, before the second
# half ever ran and before ${PIPESTATUS[0]} could even be read. Disabling -e
# for just this statement, then reading PIPESTATUS on the very next line
# (nothing else may run in between -- an intervening command would itself
# reset PIPESTATUS), captures the real exit code without letting it kill the
# script early.
run_half() {
  local label="$1" python_bin="$2"; shift 2
  local logfile
  logfile="$(mktemp)"
  echo
  echo "==================== ${label} half: ${python_bin} -m pytest $* ${EXTRA_ARGS[*]:-} ===================="
  set +e
  "$python_bin" -m pytest "$@" "${EXTRA_ARGS[@]}" 2>&1 | tee "$logfile"
  local rc=${PIPESTATUS[0]}
  set -e
  local summary
  summary="$(grep -E '[0-9]+ (passed|failed|error|skipped|deselected|warning)' "$logfile" | tail -1)"
  rm -f "$logfile"
  if [[ "$rc" -eq 5 ]]; then
    echo "ERROR: ${label} half matched ZERO tests (pytest exit 5 -- \"no tests ran\"). That is a failure in its own right, not an empty-but-green half -- check the path selector above and, if you passed -k/-m yourself, that it actually matches something in this half." >&2
  fi
  printf -v "${label}_RC" '%s' "$rc"
  printf -v "${label}_SUMMARY" '%s' "${summary:-(no summary line found)}"
}

run_half UI "${VENV_UI}/bin/python3" tests/unit --ignore=tests/unit/runner

RUNNER_PATHS=(tests/unit/runner)
if [[ -d "${REPO_ROOT}/tests/integration" ]]; then
  RUNNER_PATHS+=(tests/integration)
fi
run_half RUNNER "${VENV_RUNNER}/bin/python3" "${RUNNER_PATHS[@]}"

echo
echo "==================== combined result ===================="
overall_rc=0
if [[ "$UI_RC" -eq 0 ]]; then
  echo "UI half:     passed   (${UI_SUMMARY})"
else
  echo "UI half:     FAILED (exit ${UI_RC})   (${UI_SUMMARY})"
  overall_rc=1
fi
if [[ "$RUNNER_RC" -eq 0 ]]; then
  echo "runner half: passed   (${RUNNER_SUMMARY})"
else
  echo "runner half: FAILED (exit ${RUNNER_RC})   (${RUNNER_SUMMARY})"
  overall_rc=1
fi
if [[ "$overall_rc" -eq 0 ]]; then
  echo "OVERALL: PASS (both halves green)"
else
  echo "OVERALL: FAIL -- see the half(s) marked FAILED above"
fi
echo "==========================================================="

exit "$overall_rc"
