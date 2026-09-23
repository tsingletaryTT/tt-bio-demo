#!/usr/bin/env bash
# verify-responsive-layout.sh -- screenshot the real booth app at a matrix of window
# sizes, using a headless weston compositor and a mock daemon. Touches no Tenstorrent
# device: runner.mock.MockRunner replays a recorded fixture over a real Unix socket,
# which is all ui.app needs to have something real to draw.
#
# Usage: scripts/verify-responsive-layout.sh [out-dir]
# Requires: weston (apt-get install -y weston), .venvs/venv-ui built.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_UI="${REPO_ROOT}/.venvs/venv-ui"
OUT_DIR="${1:-/tmp/responsive-layout-shots}"
mkdir -p "$OUT_DIR"

# Both the MockRunner invocation below (a relative fixture path) and
# `-m ui.app` (a package import) are cwd-sensitive: without this, running the
# script from anywhere other than the repo root makes both background
# processes die instantly with ModuleNotFoundError/FileNotFoundError -- and,
# without the liveness checks further down, the script would still exit 0 and
# hand back 4 correctly-sized screenshots of a blank weston desktop, reporting
# success for a run that never actually drew the app. Pin cwd explicitly
# rather than relying on the caller's.
cd "$REPO_ROOT"

if ! command -v weston >/dev/null 2>&1; then
  echo "weston not found -- sudo apt-get install -y weston" >&2
  exit 1
fi

SIZES=("1024x768" "1366x768" "1920x1080" "2560x1440")
SOCKET="${OUT_DIR}/mock.sock"

for size in "${SIZES[@]}"; do
  width="${size%x*}"
  height="${size#*x}"
  echo "== ${size} ==" >&2

  wl_socket="verify-${size}"
  rm -f "/run/user/$(id -u)/${wl_socket}"*

  # --debug is load-bearing: without it weston-screenshooter's capture request
  # comes back "Output capture error: unauthorized" for every size. --renderer=
  # pixman is also load-bearing under --backend=headless with no GPU present --
  # without it weston can silently select a no-op renderer and
  # weston-screenshooter dies on `Assertion 'width > 0' failed`.
  weston --debug --backend=headless --renderer=pixman \
    --width="$width" --height="$height" --socket="$wl_socket" --idle-time=0 \
    > "${OUT_DIR}/weston-${size}.log" 2>&1 &
  weston_pid=$!
  sleep 2

  rm -f "$SOCKET"
  "${VENV_UI}/bin/python3" -c "
from runner.mock import MockRunner, load_stream
import time
r = MockRunner('${SOCKET}', load_stream('tests/fixtures/streams/with_question.jsonl'), speed=1.0)
r.start()
time.sleep(3600)
" > "${OUT_DIR}/mock-${size}.log" 2>&1 &
  mock_pid=$!
  sleep 1

  env WAYLAND_DISPLAY="$wl_socket" "${VENV_UI}/bin/python3" -m ui.app \
    --socket "$SOCKET" --playlist "${REPO_ROOT}/playlist/manifest.yaml" \
    > "${OUT_DIR}/app-${size}.log" 2>&1 &
  app_pid=$!
  sleep 5

  # A crashed mock or app process must be a loud failure, not a screenshot of
  # a blank weston desktop reported as success -- the exact failure class a
  # cwd-sensitive invocation hit when run from outside the repo root.
  for pid_var in mock_pid app_pid; do
    pid="${!pid_var}"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "FATAL: ${pid_var} (pid ${pid}) is not running ahead of the" \
           "${size} screenshot -- see ${OUT_DIR}/mock-${size}.log and" \
           "${OUT_DIR}/app-${size}.log" >&2
      kill -TERM "$weston_pid" 2>/dev/null || true
      wait "$weston_pid" 2>/dev/null || true
      exit 1
    fi
  done

  (cd "$OUT_DIR" && WAYLAND_DISPLAY="$wl_socket" weston-screenshooter) \
    > "${OUT_DIR}/shot-${size}.log" 2>&1 || true
  mv "${OUT_DIR}"/wayland-screenshot*.png "${OUT_DIR}/${size}.png" 2>/dev/null || \
    echo "WARNING: no screenshot produced for ${size}, see ${OUT_DIR}/shot-${size}.log" >&2

  kill -TERM "$app_pid" "$mock_pid" "$weston_pid" 2>/dev/null || true
  wait "$app_pid" "$mock_pid" "$weston_pid" 2>/dev/null || true
done

echo "Screenshots in ${OUT_DIR}/*.png -- look at each one." >&2
