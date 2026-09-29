#!/usr/bin/env bash
#
# run-webview.sh — start the browser viewer (webview/), the optional
# companion to the GTK4 booth. It is a SECOND client of an ALREADY-RUNNING
# daemon, never a second daemon: it connects to the same Unix socket
# scripts/run-demo.sh's daemon serves, over the same protocol/events.py
# wire ui/client.py speaks, and opens no Tenstorrent device itself (see
# webview/README.md). Running this while scripts/run-demo.sh's GTK booth is
# also running is exactly the intended use, not a race — the daemon already
# supports several simultaneous clients (runner/daemon.py's own comment on
# `_qa_lock`), and the browser can even nudge the booth (pick a target) while
# the GTK window keeps its own view.
#
# What this script deliberately does NOT do: start a daemon. If nothing is
# listening on --socket yet, the bridge starts anyway and shows
# "reconnecting…" until one appears — start scripts/run-demo.sh (or
# --mock below) first if you want it to have something to show right away.
#
# Usage:
#   scripts/run-webview.sh [options]
#
# Options (env vars in parens are equivalent overrides):
#   --socket PATH   Daemon socket to attach to. (TT_BIO_DEMO_SOCKET)
#                   Default: the SAME default scripts/run-demo.sh uses
#                   (${XDG_RUNTIME_DIR:-/tmp}/tt-bio-demo/runner.sock) --
#                   so a plain `scripts/run-demo.sh` in one terminal and a
#                   plain `scripts/run-webview.sh` in another just agree,
#                   with nothing to type twice.
#   --port N        TCP port to serve on. Default: 8080 (webview/bridge.py's
#                   own default). This box may already have something on
#                   8080 — pick another (`ss -tln` shows what's taken) rather
#                   than fighting it.
#   --host HOST     Interface to bind. Default: 127.0.0.1 (localhost only).
#                   Pass 0.0.0.0 to reach it from another machine (a phone,
#                   a laptop over ssh -L, ...) — see webview/README.md for
#                   why that's the one thing this script does not default to.
#   --mock [FILE]   No hardware, no running daemon required: replay a
#                   recorded protocol fixture instead (default:
#                   tests/fixtures/streams/with_question.jsonl, which
#                   exercises both an ordinary fold and the affinity Q&A
#                   panel). Starts its own runner.mock.MockRunner on a
#                   throwaway socket under the runtime dir and points the
#                   bridge at THAT — still never opens a device. Useful for
#                   trying the viewer with no chips in reach at all.
#   --open          Also open the viewer in a browser on this machine's own
#                   display, if one is available ($DISPLAY set and a browser
#                   on $PATH). Off by default: this script is equally at
#                   home over ssh with no display at all.
#
# Stopping: Ctrl-C. The trap below tears down the bridge (and, in --mock
# mode, the MockRunner) — it never touches a daemon, because this script
# never started one. Stopping the booth itself is scripts/run-demo.sh's own
# Ctrl-C, in whichever terminal that is running.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SELF="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"

usage() {
  awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "${BASH_SOURCE[0]}"
}

# Same PREFIX convention as run-demo.sh/test.sh: a source checkout's own
# .venvs by default, /opt/tt-bio-demo under the packaged tree.
PREFIX="${TT_BIO_DEMO_PREFIX:-${REPO_ROOT}/.venvs}"
VENV_UI="${PREFIX}/venv-ui"

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}/tt-bio-demo"
SOCKET="${TT_BIO_DEMO_SOCKET:-${RUNTIME_DIR}/runner.sock}"
HOST="127.0.0.1"
PORT="8080"
MOCK=0
MOCK_STREAM="tests/fixtures/streams/with_question.jsonl"
OPEN_BROWSER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --socket)  SOCKET="$2"; shift 2 ;;
    --port)    PORT="$2"; shift 2 ;;
    --host)    HOST="$2"; shift 2 ;;
    --mock)
      MOCK=1
      # Optional bare value: only consume $2 as the fixture path if it
      # doesn't look like the next flag (or is simply absent).
      if [[ $# -ge 2 && "$2" != --* ]]; then
        MOCK_STREAM="$2"; shift 2
      else
        shift 1
      fi
      ;;
    --open)    OPEN_BROWSER=1; shift 1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "run-webview.sh: unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ ! -x "${VENV_UI}/bin/python3" ]]; then
  echo "ERROR: venv-ui not found at ${VENV_UI}." >&2
  echo "Run scripts/setup-venvs.sh first (see README.md's Quick start)." >&2
  exit 1
fi

MOCK_PID=""
BRIDGE_PID=""

cleanup() {
  if [[ -n "$BRIDGE_PID" ]] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
    echo "run-webview.sh: stopping bridge (pid ${BRIDGE_PID})..." >&2
    kill -TERM "$BRIDGE_PID" 2>/dev/null || true
    wait "$BRIDGE_PID" 2>/dev/null || true
  fi
  if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
    echo "run-webview.sh: stopping mock runner (pid ${MOCK_PID})..." >&2
    kill -TERM "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

if [[ "$MOCK" -eq 1 ]]; then
  MOCK_STREAM_PATH="${REPO_ROOT}/${MOCK_STREAM}"
  if [[ ! -f "$MOCK_STREAM_PATH" ]]; then
    echo "run-webview.sh: no such fixture: ${MOCK_STREAM_PATH}" >&2
    echo "run-webview.sh: fixtures live under tests/fixtures/streams/" >&2
    exit 1
  fi
  mkdir -p "$RUNTIME_DIR"
  SOCKET="${RUNTIME_DIR}/mock-webview.sock"
  rm -f "$SOCKET"
  echo "run-webview.sh: replaying ${MOCK_STREAM} on a mock daemon (no hardware, no real daemon needed)..." >&2
  "${VENV_UI}/bin/python3" -c "
from runner.mock import MockRunner, load_stream
import sys, time
r = MockRunner(sys.argv[1], load_stream(sys.argv[2]), speed=1.0)
r.start()
while True:
    time.sleep(3600)
" "$SOCKET" "$MOCK_STREAM_PATH" &
  MOCK_PID=$!
  # Give the mock runner a moment to bind before the bridge's first connect
  # attempt -- not required for correctness (the bridge tolerates a socket
  # that doesn't exist yet, same as ui/client.py), just avoids a guaranteed
  # first-attempt failure landing in the log for no reason.
  sleep 0.3
fi

echo "run-webview.sh: socket:  ${SOCKET}" >&2
echo "run-webview.sh: serving: http://${HOST}:${PORT}/" >&2

cd "$REPO_ROOT"
"${VENV_UI}/bin/python3" -m webview.bridge \
  --daemon-socket "$SOCKET" \
  --host "$HOST" \
  --port "$PORT" &
BRIDGE_PID=$!

if [[ "$OPEN_BROWSER" -eq 1 ]]; then
  if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v xdg-open >/dev/null 2>&1; then
    sleep 0.5
    xdg-open "http://${HOST}:${PORT}/" >/dev/null 2>&1 || true
  else
    echo "run-webview.sh: --open asked for a browser, but no display/xdg-open is available; open the URL above yourself." >&2
  fi
fi

wait "$BRIDGE_PID"
