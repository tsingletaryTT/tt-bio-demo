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
#                                 Default: every target in the manifest. Pass
#                                 `--targets trpcage` for the fast single
#                                 target (20 residues, ~4.4s) while iterating.
#   --all-targets                 Accepted and harmless: running every target
#                                 is now the DEFAULT, so this only restates it.
#                                 (TT_BIO_DEMO_ALL_TARGETS=1)
#                                 The booth shows everything it can do by
#                                 default: a full cycle is ~58s (Trp-cage
#                                 4.4s, FKBP12 11.7s, DHFR 19.7s, trypsin
#                                 22.3s, all measured warm). To fold a single
#                                 target instead -- much faster to iterate on
#                                 -- use `--targets trpcage`.
#
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
#   --devices IDS                 Which physical chips the booth folds on, as
#                                 a comma-separated list of device indices
#                                 (e.g. `--devices 0,1`). (TT_BIO_DEMO_DEVICES)
#                                 Default: EVERY detected chip, capped at
#                                 runner.workers.MAX_WORKERS (4) -- which is
#                                 what a booth wants, so this flag is for
#                                 sharing the machine, not for running it.
#                                 Passed straight through to the daemon's own
#                                 --devices and from there, unvalidated by us,
#                                 to tt_bio.runtime.detect_tenstorrent_devices,
#                                 so `--devices 7` on a four-card box is that
#                                 function's clear error naming the ids that do
#                                 exist -- never a silently smaller booth.
#                                 Omitted from the daemon's command line
#                                 entirely when unset: `--devices ""` is not
#                                 the same request as "every chip", and the
#                                 daemon must not have to guess which was meant.
#                                 NOTE the UI is NOT given this list. It learns
#                                 which chips exist from the daemon's `hello`
#                                 (runner/daemon.py's _hello reports the pool's
#                                 own cards), which is the only source that can
#                                 be right: a chip that failed to come up, or
#                                 was retired mid-session, is a fact the daemon
#                                 has and a command line cannot.
#
#   --solo                        Start the UI showing ONE large protein on a
#                                 booth that would otherwise come up in the
#                                 grid. Without --quad or --solo the booth
#                                 picks: the grid when it has >1 chip.
#   --quad                        Start the UI showing every chip at once
#                                 instead of one large protein. Q toggles
#                                 either way at runtime; this only chooses
#                                 what the booth comes up in. Only means
#                                 anything with more than one chip.
#
#   --questions                    Opt IN to the affinity-Q&A feature: one
#                                 chip is permanently reserved for it
#                                 whenever 2+ chips are detected, taken out
#                                 of the fold rotation. Forwarded straight
#                                 to the daemon's own --questions
#                                 (runner/daemon.py); the UI needs no flag
#                                 of its own, because it already hides the
#                                 question queue panel, the gallery "ask"
#                                 strip and the attract-loop question cue
#                                 whenever the daemon's `hello` reports
#                                 `qa_capable: false` -- exactly what a
#                                 chip-less booth, or one started WITHOUT
#                                 this flag, reports.
#                                 Default: OFF -- every detected chip folds
#                                 and no chip is reserved for Q&A, because
#                                 reserving one costs a 4-chip booth 25% of
#                                 its fold throughput for a feature it may
#                                 never be asked to use. An operator can
#                                 also opt in LIVE, without restarting this
#                                 script by hand: Ctrl+A in the running UI
#                                 (ui/app.py) exits with a sentinel exit
#                                 code that this script catches below and
#                                 turns into a restart with --questions
#                                 added -- see the comment near the UI
#                                 invocation, at the bottom of this file.
#
#   --weights DIR                 tt-bio's weights cache. (TT_BIO_DEMO_WEIGHTS)
#                                 Default: $TT_BIO_CACHE, else $BOLTZ_CACHE,
#                                 else ~/.boltz -- tt-bio's own order --
#                                 EXCEPT from a packaged /opt/tt-bio-demo
#                                 install, where it defaults to the fixed
#                                 /opt/tt-bio-demo/weights the postinst
#                                 populated (still overridden if $TT_BIO_CACHE
#                                 or $BOLTZ_CACHE is already set). Whatever is
#                                 used is pinned for the folding workers too,
#                                 not just the readiness check -- an explicit
#                                 --weights/$TT_BIO_DEMO_WEIGHTS on a packaged
#                                 install re-pins $TT_BIO_CACHE to that SAME
#                                 directory (rather than to the fixed default)
#                                 so preflight and the workers can never
#                                 disagree about which cache is in play.
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

# Ctrl+A in the running UI (ui/app.py's `_request_qa_restart`) exits with
# this sentinel when it wants the booth restarted with --questions added --
# see the comment near the UI invocation below for the restart mechanism
# itself. Must match `QUESTIONS_RESTART_EXIT_CODE` in ui/app.py exactly;
# tests/unit/test_run_demo_sh.py pins the two against each other so this
# cannot silently drift.
QUESTIONS_RESTART_EXIT_CODE=42
# Set on the UI child's own environment (only) so it can tell "launched by
# this script, which knows how to catch the sentinel above and restart"
# apart from a bare `python3 -m ui.app` or the packaged systemd/.desktop
# deployment -- see ui/app.py's own comment above `RUN_DEMO_SH_ENV_VAR` for
# why that distinction matters. Must match the name ui/app.py reads.
RUN_DEMO_SH_ENV_VAR="TT_BIO_DEMO_RUN_DEMO_SH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Absolute path to this script itself, for the Ctrl+A restart's `exec` near
# the bottom of this file. `$0` would work for the common case, but this
# script `cd`s to `$REPO_ROOT` before it ever reaches that exec (see the
# `cd "$REPO_ROOT"` below), so `$0` stops resolving the instant the operator
# invoked this script with a RELATIVE path from some other directory --
# and by the time that exec fails, the old daemon is already dead and the
# EXIT/INT/TERM traps are already cleared, so the failure vanishes the whole
# booth with nothing left to clean up. `SCRIPT_DIR` was already made
# absolute above, from `BASH_SOURCE[0]` before any `cd`, so anchoring to it
# here survives the later `cd` regardless of how this script was invoked.
SELF="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"

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
# shellcheck source=weights-cache.sh
. "${SCRIPT_DIR}/weights-cache.sh"
# The weights DEFAULT comes from the shared resolver -- but which resolver
# depends on whether THIS checkout is a source tree or the packaged
# /opt/tt-bio-demo tree, same distinction scripts/doctor.sh already draws
# (doctor_install_mode, now backed by the shared tt_bio_demo_install_mode
# above). Before this, this script always used the plain, home-relative
# resolver even from a packaged install -- and this script is not a dev
# convenience, it is the packaged install's actual operator-facing launcher:
# debian/com.tenstorrent.ttbio.demo.desktop's Exec= runs it directly, and
# INSTALL.md calls it "the normal path". So a real `.deb` install had the
# postinst fetching to the fixed /opt/tt-bio-demo/weights (see
# scripts/weights-cache.sh's own big comment on
# TT_BIO_DEMO_PACKAGED_WEIGHTS_CACHE) while launching the booth via the
# desktop entry resolved the desktop user's own $HOME/.boltz instead --
# worse than before that fix, because the postinst and this launcher used to
# at least agree (both home-relative). See docs/followups.md's "run-demo.sh
# resolved home-relative even from a packaged install" entry (FIXED).
#
# The ACTUAL resolution (and, in packaged mode, the $TT_BIO_CACHE pin) is
# deferred until AFTER argument parsing -- see the block right after the
# `while` loop below -- rather than decided here. It used to be decided
# here, unconditionally, before the loop had even seen whether the operator
# passed their own --weights: that pinned $TT_BIO_CACHE to the fixed
# packaged path regardless, so `run-demo.sh --weights /mnt/usb` on a
# packaged install had preflight (which reads the daemon's own --weights
# argv directly) approve /mnt/usb while the folding WORKERS -- whose
# runner_environ() only fills in $BOLTZ_CACHE from --weights when NEITHER
# $TT_BIO_CACHE NOR $BOLTZ_CACHE is already present in the environment --
# inherited $TT_BIO_CACHE=/opt/tt-bio-demo/weights from this script instead,
# and loaded from there. Preflight and the fold silently disagreed. See
# tests/unit/test_run_demo_sh.py's
# test_an_explicit_weights_override_is_what_the_workers_actually_load_from.
#
# $REPO_ROOT, not this script's OWN $TT_BIO_DEMO_PREFIX (which chooses where
# the VENVS live -- see the PREFIX assignment above -- a different question):
# $REPO_ROOT is always the checkout this script itself lives in, so it
# answers "source or package" exactly the way doctor.sh's doctor_prefix()
# does for a real /opt/tt-bio-demo install (both resolve to the identical
# directory there).
WEIGHTS="${TT_BIO_DEMO_WEIGHTS:-}"
# Set as soon as we know an operator asked for a specific cache -- via
# $TT_BIO_DEMO_WEIGHTS here, or via --weights in the argument loop below --
# so the deferred block after that loop knows whether to pin $TT_BIO_CACHE to
# the packaged DEFAULT or to whatever the operator actually gave us.
WEIGHTS_EXPLICIT=0
[ -n "$WEIGHTS" ] && WEIGHTS_EXPLICIT=1
MANIFEST="${TT_BIO_DEMO_PLAYLIST:-${REPO_ROOT}/playlist/manifest.yaml}"
TARGETS="${TT_BIO_DEMO_TARGETS-}"   # empty == every target in the manifest
DEVICES="${TT_BIO_DEMO_DEVICES-}"   # empty == every chip the daemon detects
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

# Captured BEFORE the parsing loop below consumes every positional argument
# via its own `shift`s. Without this, by the time execution reaches the
# Ctrl+A restart branch near the bottom of this file, `$#` is 0 and `"$@"`
# is empty -- so `exec "$0" "$@" --questions` was ALWAYS just `exec "$0"
# --questions`, silently discarding every flag the operator originally
# passed (--quad, --solo, --devices, --targets, --windowed, ...) across the
# restart. `--devices` was the sharpest edge: a booth launched with
# `--devices 0,1` to share this machine would come back after Ctrl+A
# claiming EVERY detected chip. tests/unit/test_run_demo_sh.py's
# test_the_restart_loop_relaunches_once_with_questions_added is the
# regression test for this -- it passes --devices through a simulated
# restart and asserts it survives.
RUN_DEMO_ARGV=("$@")

while [[ $# -gt 0 ]]; do
  case "$1" in
    --socket)               SOCKET="$2"; shift 2 ;;
    --log-root)             LOG_ROOT="$2"; shift 2 ;;
    --playlist)             MANIFEST="$2"; shift 2 ;;
    --targets)              TARGETS="$2"; shift 2 ;;
    --devices)              DEVICES="$2"; shift 2 ;;
    --all-targets)          TARGETS=""; shift ;;
    --windowed)             WINDOWED=1; shift ;;
    --quad)                 QUAD=1; shift ;;
    --solo)                 SOLO=1; shift ;;
    --questions)            QUESTIONS=1; shift ;;
    --weights)              WEIGHTS="$2"; WEIGHTS_EXPLICIT=1; shift 2 ;;
    --log-budget-gb)        LOG_BUDGET_GB="$2"; shift 2 ;;
    --structures-budget-gb) STRUCTURES_BUDGET_GB="$2"; shift 2 ;;
    -h|--help)              usage; exit 0 ;;
    *) echo "run-demo.sh: unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

# Resolve the weights cache now that argument parsing has decided whether
# the operator supplied their own --weights/$TT_BIO_DEMO_WEIGHTS -- see the
# long comment above WEIGHTS_EXPLICIT for why this cannot happen earlier.
if [ "$WEIGHTS_EXPLICIT" -eq 1 ]; then
  if [ "$(tt_bio_demo_install_mode "$REPO_ROOT")" = "package" ]; then
    # Pin $TT_BIO_CACHE to the SAME directory the operator just gave us --
    # not to the fixed packaged default -- so preflight (which checks
    # $WEIGHTS directly) and the folding workers (whose runner_environ()
    # reads $TT_BIO_CACHE from the environment this daemon process
    # inherits, and backs off from also setting $BOLTZ_CACHE the instant
    # $TT_BIO_CACHE is already present) resolve the EXACT same directory.
    # This also makes the hf-repo artifacts (nesso1/nesso1-ccd/the ESM-2
    # encoder -- see scripts/weights-cache.sh's own
    # TT_BIO_DEMO_PACKAGED_WEIGHTS_CACHE comment) honour the override too,
    # since those ignore --weights/root= entirely and only ever look at
    # $TT_BIO_CACHE. Through tt_bio_demo_weights_cache_pin_to, not a bare
    # `export TT_BIO_CACHE=...` here -- this project restricts which files
    # may read/write these two variables directly
    # (tests/unit/test_weights_cache_is_derived_once.py), specifically
    # because a caller pinning its own copy is how this project's
    # cache-location bugs have shipped before; the pin function is a plain
    # top-level call (not `$(...)`) so the export lands in THIS shell, not a
    # subshell that evaporates on exit.
    tt_bio_demo_weights_cache_pin_to "$WEIGHTS" >/dev/null
  fi
else
  if [ "$(tt_bio_demo_install_mode "$REPO_ROOT")" = "package" ]; then
    # Bare top-level call, not inside `$(...)`: the export has to land in
    # THIS shell, not a subshell that evaporates on exit, so the daemon
    # process started below (which does `import tt_bio` itself) inherits
    # $TT_BIO_CACHE too -- not just the flat --weights argv value. That is
    # what makes nesso1/nesso1-ccd/the ESM-2 encoder ("hf-repo" artifacts
    # that SILENTLY IGNORE --weights/root= entirely -- see
    # scripts/weights-cache.sh's own TT_BIO_DEMO_PACKAGED_WEIGHTS_CACHE
    # comment) resolve to the SAME fixed cache the postinst populated,
    # rather than to whatever $HOME the desktop session launching this
    # script happens to have. Same "prime once, capture again" shape
    # debian/tt-bio-demo-weights.postinst and scripts/doctor.sh's
    # doctor_prime_weights_cache already use.
    tt_bio_demo_weights_cache_packaged >/dev/null
    WEIGHTS="$(tt_bio_demo_weights_cache_packaged)"
  else
    WEIGHTS="$(tt_bio_demo_weights_cache)"
  fi
fi

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
# `tt_bio_demo_materialize_playlist` (scripts/materialize-playlist.sh) is
# what actually does this now -- factored out so the packaged/systemd
# deployment's own launcher can build the SAME farm into ITS OWN runtime
# directory, rather than enumerating the installed (and never-populated-
# with-real-inputs) `/opt/tt-bio-demo/playlist/` directly. See that file's
# own header for the packaged-deployment bug this replaces.
# shellcheck source=materialize-playlist.sh
. "${SCRIPT_DIR}/materialize-playlist.sh"
if ! tt_bio_demo_materialize_playlist \
    "${VENV_UI}/bin/python3" "$MANIFEST" "$TARGETS" "$PLAYLIST_DIR"; then
  exit 1
fi
PLAYLIST_COUNT="$(find "$PLAYLIST_DIR" -maxdepth 1 -type l -name '*.yaml' | wc -l)"

DAEMON_LOG="${RUNTIME_DIR}/daemon.log"

echo "run-demo.sh: tt-metal log root (Inspector/Watcher output): ${LOG_ROOT}"
echo "run-demo.sh: daemon stderr log:                            ${DAEMON_LOG}"
echo "run-demo.sh: socket:                                       ${SOCKET}"
echo "run-demo.sh: playlist manifest:                            ${MANIFEST}"
echo "run-demo.sh: targets (${PLAYLIST_COUNT}):                              ${TARGETS:-<all>}"
echo "run-demo.sh: fold inputs built for the daemon:             ${PLAYLIST_DIR}"
echo "run-demo.sh: chips:                                        ${DEVICES:-<all detected>}"
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
# `--devices` is appended only when the operator asked for one, as a real
# array rather than an unquoted string: an empty DEVICES must produce NO flag
# at all (see the header). `--devices ""` would reach
# detect_tenstorrent_devices as an explicit empty selection, which is a
# different request from "every chip" and not one the booth ever means.
#
# Spelled as an `if` rather than `[[ ... ]] && DEVICE_ARGS=(...)`: under
# `set -e` the one-liner's exit status is that of a failed test whenever
# DEVICES is empty, which is the common case, and relying on bash's
# and-list exemption to not abort the launcher there is a subtlety the
# next reader should not have to know.
DEVICE_ARGS=()
if [[ -n "$DEVICES" ]]; then
  DEVICE_ARGS=(--devices "$DEVICES")
fi
# --questions is a bare boolean, same shape as --preflight-only on the
# daemon's own side: appended only when the operator asked for it (directly,
# or via the restart loop below after Ctrl+A), so the daemon's own default
# (never reserve a chip for Q&A) is what a plain run-demo.sh invocation
# still gets.
QUESTIONS_ARGS=()
if [[ "${QUESTIONS:-0}" == "1" ]]; then
  QUESTIONS_ARGS=(--questions)
fi
"${VENV_RUNNER}/bin/python3" -m runner.daemon \
  --socket "$SOCKET" \
  --weights "$WEIGHTS" \
  --playlist "$PLAYLIST_DIR" \
  --log-root "$LOG_ROOT" \
  ${DEVICE_ARGS[@]+"${DEVICE_ARGS[@]}"} \
  --log-budget-gb "$LOG_BUDGET_GB" \
  --structures-budget-gb "$STRUCTURES_BUDGET_GB" \
  ${QUESTIONS_ARGS[@]+"${QUESTIONS_ARGS[@]}"} \
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
[ "${WINDOWED:-0}" = "1" ] && UI_ARGS="${UI_ARGS} --windowed"
# --quad chooses the START view only; Q still toggles at runtime. Worth it
# for an operator running a multi-chip booth who wants the grid up all day,
# and for recording, where "press Q at the right moment" fails silently.
[ "${QUAD:-0}" = "1" ] && UI_ARGS="${UI_ARGS} --quad"
[ "${SOLO:-0}" = "1" ] && UI_ARGS="${UI_ARGS} --solo"

# Tell the UI it was launched by THIS script, so its own Ctrl+A handler
# (`_request_qa_restart`) knows the sentinel exit code below actually means
# something here -- see ui/app.py's comment above `RUN_DEMO_SH_ENV_VAR`.
# Exported (not just set for the one command) so it reaches the UI process
# the ordinary way a child inherits its parent's environment.
export "${RUN_DEMO_SH_ENV_VAR}"=1

# Captured explicitly rather than left to `set -e`: under `set -e` a bare
# nonzero exit from the last command in the script would abort here with no
# chance to look at the code first. `|| UI_EXIT=$?` on its own is an
# or-list, so the STATEMENT's exit status is that of the assignment (0),
# not of the UI -- same reasoning as the DEVICE_ARGS `if` above.
UI_EXIT=0
"${VENV_UI}/bin/python3" -m ui.app \
  --socket "$SOCKET" \
  --playlist "$MANIFEST" \
  --targets "$TARGETS" \
  ${UI_ARGS} || UI_EXIT=$?

if [[ "$UI_EXIT" -eq "$QUESTIONS_RESTART_EXIT_CODE" ]]; then
  echo "run-demo.sh: UI asked to restart with --questions enabled (Ctrl+A)..." >&2
  # Tear down the OLD daemon before handing off. This is NOT redundant with
  # `trap cleanup EXIT` below: `exec` replaces this process image outright
  # and never fires a bash trap on the way out, so without this explicit
  # call the old daemon's socket and device handle would leak across the
  # restart -- exactly the "leaked device handle blocks the next run"
  # failure `cleanup`'s own comment warns about, just triggered by a
  # different exit path than the ones that comment already covers.
  cleanup
  # Not load-bearing for correctness (an exec never runs these either way),
  # but leaving `trap ... EXIT` armed against a script image that is about
  # to be replaced wholesale reads as a bug to the next person who greps
  # for `trap` here. Cleared explicitly so it is obviously not one.
  trap - EXIT INT TERM
  # Invariant: --questions is never already present in the ORIGINAL argument
  # list here. The only way to reach this branch is the UI's sentinel exit
  # code, and the UI itself (`_request_qa_restart`) refuses to produce that
  # code once Q&A is already enabled (`qa_capable`) -- so a booth already
  # started with --questions can never ask to restart into --questions a
  # second time, and this loop cannot fire twice in a row growing the
  # argument list. Guarded anyway, defensively, rather than trusting that
  # invariant blindly across whatever this script becomes later.
  #
  # Scans `RUN_DEMO_ARGV` (captured before the parsing loop, at the top of
  # this file), NOT `"$@"` -- by this point in the script `"$@"` has been
  # `shift`ed down to nothing by that same loop, which is exactly the bug
  # this guard used to be dead code against: it could never find
  # "--questions" in an already-empty list, so it always fell through to the
  # `exec` below regardless. `${RUN_DEMO_ARGV[@]+"${RUN_DEMO_ARGV[@]}"}` (not
  # a bare `"${RUN_DEMO_ARGV[@]}"`) is the same guard `DEVICE_ARGS`/
  # `QUESTIONS_ARGS` use elsewhere in this file, for the identical reason:
  # under `set -u`, some bash versions treat expanding an unset/empty array
  # as an unbound-variable error.
  for arg in ${RUN_DEMO_ARGV[@]+"${RUN_DEMO_ARGV[@]}"}; do
    if [[ "$arg" == "--questions" ]]; then
      echo "run-demo.sh: --questions is already set; restarting without" \
           "adding it again" >&2
      exec "$SELF" ${RUN_DEMO_ARGV[@]+"${RUN_DEMO_ARGV[@]}"}
    fi
  done
  exec "$SELF" ${RUN_DEMO_ARGV[@]+"${RUN_DEMO_ARGV[@]}"} --questions
fi

exit "$UI_EXIT"
