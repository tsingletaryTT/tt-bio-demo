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
#   --playlist FILE               The playlist MANIFEST (a YAML file, format
#                                 in ui/playlist.py) that both processes are
#                                 driven from. (TT_BIO_DEMO_PLAYLIST)
#                                 Default: this repo's playlist/manifest.yaml.
#                                 NOTE this takes a FILE, not the directory
#                                 of .yaml fold inputs it used to take: the
#                                 daemon's directory is now BUILT from this
#                                 manifest (see --targets), so the gallery
#                                 can only ever show what the daemon can
#                                 actually fold.
#   --targets a,b,c               Which manifest ids to run, as one
#                                 comma-separated list handed to BOTH
#                                 processes. (TT_BIO_DEMO_TARGETS)
#                                 Default: trpcage — 20 residues, no MSA
#                                 server needed, ~4.4s a fold, and the only
#                                 target whose whole path (fold -> socket ->
#                                 ribbon on screen) has been run end to end.
#   --all-targets                 Run every target in the manifest instead.
#                                 (TT_BIO_DEMO_ALL_TARGETS=1)
#                                 Validated end to end 2026-08-12: a 320s
#                                 live run completed 21 folds across all four
#                                 targets with ZERO client drops or
#                                 reconnects. The UI's 5s read timeout loops
#                                 rather than disconnecting, so the long
#                                 callback-free windows (host featurization,
#                                 then the confidence head and mmCIF write)
#                                 do not break the socket.
#
#   --windowed                    Start the UI in a normal window instead of
#                                 fullscreen. A development convenience, not
#                                 a booth setting -- the kiosk always wants
#                                 fullscreen. Ctrl+F toggles either way at
#                                 runtime, but without this the app seizes
#                                 the whole screen before you can reach it.
#
# Why a manifest and not a directory: the daemon folds .yaml inputs and the
# UI shows a gallery built from the manifest, and until this fix those two
# were chosen INDEPENDENTLY. The turnkey launcher therefore shipped a
# gallery of four targets, each stamped with a measured fold time, over a
# daemon that had exactly one input file — tap "Trypsin · ~74.9s" and a
# 20-residue Trp-cage arrived four seconds later. One manifest, one target
# list, both processes.
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

# Matches scripts/test.sh's own PREFIX handling (and setup-venvs.sh's
# --prefix): production builds these under /opt/tt-bio-demo, and the
# launcher's own test harness points it at a pair of stub interpreters that
# record their argv instead of opening a device.
PREFIX="${TT_BIO_DEMO_PREFIX:-${REPO_ROOT}/.venvs}"
VENV_RUNNER="${PREFIX}/venv-runner"
VENV_UI="${PREFIX}/venv-ui"

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}/tt-bio-demo"

SOCKET="${TT_BIO_DEMO_SOCKET:-${RUNTIME_DIR}/runner.sock}"
LOG_ROOT="${TT_BIO_DEMO_LOG_ROOT:-${RUNTIME_DIR}/logs}"
WEIGHTS="${TT_BIO_DEMO_WEIGHTS:-${HOME}/.boltz}"
MANIFEST="${TT_BIO_DEMO_PLAYLIST:-${REPO_ROOT}/playlist/manifest.yaml}"
TARGETS="${TT_BIO_DEMO_TARGETS:-trpcage}"
LOG_BUDGET_GB="${TT_BIO_DEMO_LOG_BUDGET_GB:-2.0}"
STRUCTURES_BUDGET_GB="${TT_BIO_DEMO_STRUCTURES_BUDGET_GB:-0.2}"

if [[ "${TT_BIO_DEMO_ALL_TARGETS:-0}" == "1" ]]; then
  TARGETS=""
fi

# The fold inputs the daemon reads. Always OURS, never an operator's
# directory: it is generated from the manifest below, every run, so there is
# no path by which it can hold something the UI's gallery does not also
# show.
PLAYLIST_DIR="${RUNTIME_DIR}/playlist"

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
    --playlist)             MANIFEST="$2"; shift 2 ;;
    --targets)              TARGETS="$2"; shift 2 ;;
    --all-targets)          TARGETS=""; shift ;;
    --windowed)             WINDOWED=1; shift ;;
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
mkdir -p "$PLAYLIST_DIR"
PLAYLIST_DIR="$(cd "$PLAYLIST_DIR" && pwd)"
MANIFEST="$(cd "$(dirname "$MANIFEST")" && pwd)/$(basename "$MANIFEST")"

# Everything below runs modules out of this checkout (`-m ui.app`,
# `-m runner.daemon`, `-m ui.playlist`), which needs the repo root on
# sys.path. Python puts the CWD there for `-m`, so cd here rather than
# depending on the operator having launched this from the right place --
# and cd AFTER the path resolution above, so a relative --socket/--log-root
# still means what the operator typed.
cd "$REPO_ROOT"

# ── the one playlist, expanded for both processes ───────────────────────────
#
# The manifest is the source of truth (see the header). This turns the
# selected entries into the directory of .yaml inputs the daemon folds, one
# symlink per target, NAMED BY MANIFEST ID -- runner/daemon.py derives its
# target_id from the filename stem, so `trypsin.yaml -> examples/
# affinity_tryp.yaml` is what makes the daemon's own `job_start
# target_id=trypsin` line up with the id the gallery showed the visitor
# (the diagnostics panel used to print "visitor picked trypsin" directly
# above "▶ fold affinity_tryp").
#
# ui/playlist.py is the parser for both sides, so a manifest the UI would
# refuse is one this script refuses too, here, before anything starts --
# with the module's one-line message, never a traceback.
if ! PLAYLIST_LINES="$("${VENV_UI}/bin/python3" -m ui.playlist "$MANIFEST" \
                        ${TARGETS//,/ } 2>&1)"; then
  echo "ERROR: ${PLAYLIST_LINES}" >&2
  echo "run-demo.sh: --playlist must name a playlist MANIFEST (see ${REPO_ROOT}/playlist/manifest.yaml)," >&2
  echo "and every --targets id must appear in it." >&2
  exit 1
fi

# Wipe first: a target dropped from --targets between two runs must not keep
# folding because last run's symlink is still lying there. Only symlinks are
# removed, and only from a directory this script owns (see PLAYLIST_DIR).
find "$PLAYLIST_DIR" -maxdepth 1 -type l -name '*.yaml' -delete
PLAYLIST_COUNT=0
while IFS=$'\t' read -r target_id input_path; do
  [[ -n "$target_id" ]] || continue
  if [[ ! -f "$input_path" ]]; then
    echo "ERROR: playlist target '${target_id}' names an input that does not exist:" >&2
    echo "  ${input_path}" >&2
    echo "The gallery must never offer something the daemon cannot fold." >&2
    exit 1
  fi
  # -f (not -n): re-point every run rather than trusting a stale link left
  # over from an older checkout.
  ln -sf "$input_path" "${PLAYLIST_DIR}/${target_id}.yaml"
  PLAYLIST_COUNT=$((PLAYLIST_COUNT + 1))
done <<< "$PLAYLIST_LINES"

if [[ "$PLAYLIST_COUNT" -eq 0 ]]; then
  echo "ERROR: ${MANIFEST} selected no targets; there would be nothing to fold." >&2
  exit 1
fi

DAEMON_LOG="${RUNTIME_DIR}/daemon.log"

echo "run-demo.sh: tt-metal log root (Inspector/Watcher output): ${LOG_ROOT}"
echo "run-demo.sh: daemon stderr log:                            ${DAEMON_LOG}"
echo "run-demo.sh: socket:                                       ${SOCKET}"
echo "run-demo.sh: playlist manifest:                            ${MANIFEST}"
echo "run-demo.sh: targets (${PLAYLIST_COUNT}):                              ${TARGETS:-<all>}"
echo "run-demo.sh: fold inputs built for the daemon:             ${PLAYLIST_DIR}"
echo "run-demo.sh: weights:                                      ${WEIGHTS}"
if [[ -z "$TARGETS" || "$TARGETS" != "trpcage" ]]; then
  echo "run-demo.sh: NOTE — targets other than trpcage are 62–75s folds whose" >&2
  echo "run-demo.sh: end-to-end path (fold → socket → ribbon) has not been" >&2
  echo "run-demo.sh: validated yet. Watch it before leaving it unattended." >&2
fi

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
  --playlist "$PLAYLIST_DIR" \
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
# The SAME manifest and the SAME target list the daemon's fold inputs were
# built from, a few lines above. Passing only --socket here is what shipped
# a four-card gallery over a one-target daemon; tests/unit/test_run_demo_sh.py
# now fails if these two ever drift apart again.
UI_ARGS=""
# --windowed is a development convenience, not a booth setting: the
# kiosk always wants fullscreen. Without it the app seizes the whole
# screen before anyone can reach Ctrl+F, which is hostile on a shared
# desktop. Ctrl+F still toggles either way at runtime.
[ "${WINDOWED:-0}" = "1" ] && UI_ARGS="--windowed"

"${VENV_UI}/bin/python3" -m ui.app \
  --socket "$SOCKET" \
  --playlist "$MANIFEST" \
  --targets "$TARGETS" \
  ${UI_ARGS}
