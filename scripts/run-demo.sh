#!/usr/bin/env bash
#
# run-demo.sh — Phase 3a's actual deliverable: launch the real compute
# daemon (venv-runner) and the real GTK4 UI (venv-ui), wired together over a
# Unix socket, so a protein folds on screen driven entirely by live
# computation on a Tenstorrent card. No recorded fixture anywhere in this
# path — that is runner/mock.py, and this script never touches it.
#
# The two processes are separate on purpose (see CLAUDE.md and
# docs/superpowers/specs/2026-08-10-tt-bio-demo-design.md): venv-runner has
# torch/ttnn/tt-bio and no GTK; venv-ui has PyGObject/gemmi/PyOpenGL and no
# torch. This script is the only thing that needs to know both venvs exist —
# everything downstream of it talks over the socket, not over shared Python
# state.
#
# Usage:
#   scripts/run-demo.sh [options]
#
# Options (all optional; env vars in parens are equivalent overrides):
#   --socket PATH                Unix socket the daemon serves and the UI
#                                 connects to. (TT_BIO_DEMO_SOCKET)
#                                 Default: ${XDG_RUNTIME_DIR:-/tmp}/tt-bio-demo/runner.sock
#   --log-root PATH               Where tt-metal's Inspector/Watcher output
#                                 (runner/env.py's TT_METAL_LOGS_PATH) is
#                                 pinned. Always resolved to an absolute path
#                                 under the same runtime directory as
#                                 --socket, so it never lands relative to
#                                 whatever directory you happened to launch
#                                 this script from. (TT_BIO_DEMO_LOG_ROOT)
#                                 Default: <runtime-dir>/logs
#   --playlist DIR                Directory of .yaml fold targets; the
#                                 daemon folds every .yaml it finds there.
#                                 (TT_BIO_DEMO_PLAYLIST) Default: a small
#                                 directory this script builds itself under
#                                 <runtime-dir>/playlist, containing only a
#                                 symlink to this repo's own
#                                 examples/trpcage_no_msa.yaml (20 residues,
#                                 no MSA server needed) — vendored here
#                                 rather than pointed at a sibling tt-boltz
#                                 checkout (this script used to default to
#                                 ~/code/tt-boltz/examples/trpcage_no_msa.yaml;
#                                 an absolute path into a different repository
#                                 that this one has no control over, with no
#                                 loud failure if it ever moved) — and
#                                 deliberately NOT a curated tt-boltz
#                                 playlist, most of which need a running MSA
#                                 server or are far bigger than a booth demo
#                                 needs.
#   --weights DIR                 tt-bio's weights cache. (TT_BIO_DEMO_WEIGHTS)
#                                 Default: ~/.boltz
#   --log-budget-gb N             Forwarded to the daemon's own
#                                 --log-budget-gb (tt-metal log containment;
#                                 see runner/env.py). Default: 2.0
#   --structures-budget-gb N      Forwarded to the daemon's own
#                                 --structures-budget-gb (accumulated .cif
#                                 output; see runner/daemon.py). Default: 0.2
#
# Ctrl-C (or any of EXIT/INT/TERM) tears the daemon down before this script
# exits. That matters on a shared machine: a leaked device handle blocks the
# next run, and CLAUDE.md rules out `tt-smi -r` as a way to recover from one.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VENV_RUNNER="${REPO_ROOT}/.venvs/venv-runner"
VENV_UI="${REPO_ROOT}/.venvs/venv-ui"

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}/tt-bio-demo"

SOCKET="${TT_BIO_DEMO_SOCKET:-${RUNTIME_DIR}/runner.sock}"
LOG_ROOT="${TT_BIO_DEMO_LOG_ROOT:-${RUNTIME_DIR}/logs}"
WEIGHTS="${TT_BIO_DEMO_WEIGHTS:-${HOME}/.boltz}"
PLAYLIST="${TT_BIO_DEMO_PLAYLIST:-${RUNTIME_DIR}/playlist}"
LOG_BUDGET_GB="${TT_BIO_DEMO_LOG_BUDGET_GB:-2.0}"
STRUCTURES_BUDGET_GB="${TT_BIO_DEMO_STRUCTURES_BUDGET_GB:-0.2}"

# Tracked separately from PLAYLIST itself so a later `--playlist` argument
# (or the env var above) can be told apart from "nobody asked for anything
# in particular, so build the small default playlist" — comparing PLAYLIST
# against its own default string would also match a user who happened to
# pass that exact path back in, which is a needless footgun to leave lying
# around.
PLAYLIST_IS_DEFAULT=1
if [[ -n "${TT_BIO_DEMO_PLAYLIST:-}" ]]; then
  PLAYLIST_IS_DEFAULT=0
fi

usage() {
  # Print every leading "#"-comment line after the shebang, stopping at the
  # first non-comment line (`set -euo pipefail`) -- so this never needs its
  # end-line hand-updated again when the header comment above grows. It
  # already went stale once this way: a hardcoded `sed -n '2,45p'` shipped
  # --structures-budget-gb invisibly to --help, because the flag's
  # documentation landed past line 45 and nobody had a reason to notice.
  awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "${BASH_SOURCE[0]}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --socket)               SOCKET="$2"; shift 2 ;;
    --log-root)             LOG_ROOT="$2"; shift 2 ;;
    --playlist)             PLAYLIST="$2"; PLAYLIST_IS_DEFAULT=0; shift 2 ;;
    --weights)              WEIGHTS="$2"; shift 2 ;;
    --log-budget-gb)        LOG_BUDGET_GB="$2"; shift 2 ;;
    --structures-budget-gb) STRUCTURES_BUDGET_GB="$2"; shift 2 ;;
    -h|--help)              usage; exit 0 ;;
    *) echo "run-demo.sh: unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ ! -x "${VENV_RUNNER}/bin/python3" ]]; then
  echo "ERROR: venv-runner not found at ${VENV_RUNNER}." >&2
  echo "Run scripts/setup-venvs.sh first." >&2
  exit 1
fi
if [[ ! -x "${VENV_UI}/bin/python3" ]]; then
  echo "ERROR: venv-ui not found at ${VENV_UI}." >&2
  echo "Run scripts/setup-venvs.sh first." >&2
  exit 1
fi

# Resolve to absolute paths now, before either process starts, so nothing
# downstream depends on the CWD this script happened to be launched from
# (runner/env.py's own docstring makes the same point about the daemon's log
# root specifically; this generalizes it to every path this script hands
# out).
mkdir -p "$(dirname "$SOCKET")"
SOCKET="$(cd "$(dirname "$SOCKET")" && pwd)/$(basename "$SOCKET")"
mkdir -p "$LOG_ROOT"
LOG_ROOT="$(cd "$LOG_ROOT" && pwd)"
mkdir -p "$PLAYLIST"
PLAYLIST="$(cd "$PLAYLIST" && pwd)"

# Vendored in this repo (examples/trpcage_no_msa.yaml) rather than pointed
# at a sibling tt-boltz checkout -- this used to default to
# ~/code/tt-boltz/examples/trpcage_no_msa.yaml, an absolute path into a
# different repository this one does not control, with nothing here to
# notice if it ever moved (see tests/integration/test_real_fold.py's own
# fix for the matching hazard on the test side). This file always exists on
# this branch, so the "input not found" warning below is now a real
# problem with this checkout, not an expected day-one state.
DEFAULT_PLAYLIST_INPUT="${REPO_ROOT}/examples/trpcage_no_msa.yaml"
if [[ "$PLAYLIST_IS_DEFAULT" -eq 1 ]]; then
  if [[ -f "$DEFAULT_PLAYLIST_INPUT" ]]; then
    # -f (not -n): re-point the symlink every run rather than trusting a
    # stale one left over from an old checkout.
    ln -sf "$DEFAULT_PLAYLIST_INPUT" "${PLAYLIST}/trpcage_no_msa.yaml"
  else
    echo "WARNING: default playlist input not found at ${DEFAULT_PLAYLIST_INPUT}." >&2
    echo "Pass --playlist DIR pointing at your own .yaml fold target(s)." >&2
  fi
fi

DAEMON_LOG="${RUNTIME_DIR}/daemon.log"

echo "run-demo.sh: tt-metal log root (Inspector/Watcher output): ${LOG_ROOT}"
echo "run-demo.sh: daemon stderr log:                            ${DAEMON_LOG}"
echo "run-demo.sh: socket:                                       ${SOCKET}"
echo "run-demo.sh: playlist:                                     ${PLAYLIST}"
echo "run-demo.sh: weights:                                      ${WEIGHTS}"

DAEMON_PID=""

cleanup() {
  if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    echo "run-demo.sh: stopping daemon (pid ${DAEMON_PID})..." >&2
    # SIGTERM, not SIGKILL: the daemon's own signal handler (runner/daemon.py
    # main()) runs Daemon.stop(), which unwinds the fold loop, closes the
    # Folder (releasing the device and its flock DeviceLease), and unlinks
    # the socket. A leaked device handle blocks the next run on this shared
    # machine, and CLAUDE.md rules out `tt-smi -r` as the fix.
    kill -TERM "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
}
# The EXIT trap covers the normal "UI window closed" path. INT/TERM get
# their own traps that call `exit` explicitly: bash does not exit a script
# on a trapped signal unless told to, and without an explicit exit here
# Ctrl-C would run cleanup and then leave the script sitting at whatever
# came next instead of actually stopping. Both traps also fire the EXIT
# trap on their way out; cleanup is idempotent (kill -0 on an already-dead
# pid just fails) so running it twice is harmless.
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

echo "run-demo.sh: starting daemon..." >&2
"${VENV_RUNNER}/bin/python3" -m runner.daemon \
  --socket "$SOCKET" \
  --weights "$WEIGHTS" \
  --playlist "$PLAYLIST" \
  --log-root "$LOG_ROOT" \
  --log-budget-gb "$LOG_BUDGET_GB" \
  --structures-budget-gb "$STRUCTURES_BUDGET_GB" \
  >"$DAEMON_LOG" 2>&1 &
DAEMON_PID=$!
echo "run-demo.sh: daemon pid ${DAEMON_PID}; tailing its own log at ${DAEMON_LOG}" >&2

# No wait-for-socket loop here on purpose: ui/client.py's EventClient
# already tolerates a socket that does not exist yet (FileNotFoundError is
# one of the exceptions its reconnect loop treats as "runner unavailable,
# try again shortly") and retries every reconnect_delay (1.0s default) — the
# exact resilience path this project's spec calls out as central, so
# starting the UI immediately exercises it on every single run rather than
# only when something goes wrong.
echo "run-demo.sh: starting UI..." >&2
"${VENV_UI}/bin/python3" -m ui.app --socket "$SOCKET"
