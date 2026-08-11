#!/usr/bin/env bash
#
# test.sh — run the test suite through the project's venv-ui.
#
# Bare `python3` on this box can resolve to a personal Tenstorrent venv that
# has numpy but not gemmi/PyGObject/PyOpenGL (see docs/followups.md and
# docs/venv-bootstrap-notes.md) — tests would pass by accident on the
# numpy-only modules and fail obscurely the moment GTK or gemmi is touched.
# Always go through venv-ui explicitly; this script is that one obvious way.
#
# Usage: scripts/test.sh [pytest args...]
#   scripts/test.sh                          # full suite
#   scripts/test.sh -k geometry -v           # forwarded straight to pytest
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Matches setup-venvs.sh's default; override with --prefix there and
# TT_BIO_DEMO_PREFIX here if you built venv-ui somewhere else.
PREFIX="${TT_BIO_DEMO_PREFIX:-${REPO_ROOT}/.venvs}"
VENV_UI="${PREFIX}/venv-ui"

if [[ ! -x "${VENV_UI}/bin/python3" ]]; then
  echo "ERROR: venv-ui not found at ${VENV_UI}." >&2
  echo "Run scripts/setup-venvs.sh first (pass --prefix if you used a custom one, and set TT_BIO_DEMO_PREFIX here to match)." >&2
  exit 1
fi

cd "$REPO_ROOT"
exec "${VENV_UI}/bin/python3" -m pytest "$@"
