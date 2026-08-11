#!/usr/bin/env bash
#
# setup-venvs.sh — create the two project-owned virtualenvs tt-bio-demo needs.
#
# tt-bio-demo is two processes in two Python environments (see CLAUDE.md and
# docs/superpowers/specs/2026-08-10-tt-bio-demo-design.md, "Environment
# incompatibility"):
#
#   venv-ui/      GTK4 UI process. Built from /usr/bin/python3 WITH
#                 --system-site-packages so it inherits the apt-installed
#                 python3-gi, python3-gemmi, python3-opengl, python3-numpy —
#                 bindings that are painful to get right via pip because they
#                 wrap system libraries (GObject introspection, GTK4, the
#                 system OpenGL/GL stack) that apt already manages correctly.
#
#   venv-runner/  Compute daemon process (Phase 3). Built from
#                 /usr/bin/python3 WITHOUT system site packages, then
#                 `pip install tt-bio` — which pulls torch and ttnn. Isolated
#                 from venv-ui on purpose: a torch/ttnn stack and PyGObject
#                 have no business sharing a site-packages, and keeping them
#                 apart gives fault isolation for free (see CLAUDE.md).
#
# Both venvs are built from the *same* /usr/bin/python3 (Python 3.12.3 on
# this box), never from a personal Tenstorrent venv on $PATH — see the
# "Use /usr/bin/python3" gotcha in docs/followups.md. That gotcha predates
# this script; the script exists so nobody has to remember it by hand again.
#
# This script does NOT run `tt-bio install-deps` or touch any Tenstorrent
# system package / kernel module. Installing system-level TT dependencies is
# a Debian-packaging-phase decision that needs explicit consent, not
# something a venv bootstrap script should do on its own.
#
# Usage:
#   scripts/setup-venvs.sh [--prefix PATH] [--force] [--skip-runner]
#
#   --prefix PATH   Where to create venv-ui/ and venv-runner/. Default:
#                    <repo>/.venvs (gitignored). The Debian postinst passes
#                    /opt/tt-bio-demo here in a later phase.
#   --force         Recreate both venvs from scratch even if they already
#                    look valid. Without this flag, re-running is a cheap
#                    no-op once both venvs are verified.
#   --skip-runner   Only build/verify venv-ui. venv-runner's `pip install
#                    tt-bio` pulls torch + ttnn and can be multiple GB and
#                    slow (see docs/venv-bootstrap-notes.md) — useful while
#                    iterating on the UI side.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# The one place tt-bio's version is pinned. Bump deliberately, never track
# tt-bio's `main` (see CLAUDE.md conventions — same rule as the runner venv
# tt-bio-demo will eventually shell out to).
# ---------------------------------------------------------------------------
TT_BIO_VERSION="0.6.2"

SYSTEM_PYTHON="/usr/bin/python3"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PREFIX="${REPO_ROOT}/.venvs"
FORCE=0
SKIP_RUNNER=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--prefix PATH] [--force] [--skip-runner]

Creates <prefix>/venv-ui and <prefix>/venv-runner. Default prefix:
${REPO_ROOT}/.venvs
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      [[ $# -ge 2 ]] || { echo "ERROR: --prefix needs an argument" >&2; exit 1; }
      PREFIX="$2"
      shift 2
      ;;
    --prefix=*)
      PREFIX="${1#*=}"
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --skip-runner)
      SKIP_RUNNER=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

# Normalize to an absolute path without requiring the directory to exist yet.
case "$PREFIX" in
  /*) : ;;
  *) PREFIX="$(pwd)/${PREFIX}" ;;
esac

VENV_UI="${PREFIX}/venv-ui"
VENV_RUNNER="${PREFIX}/venv-runner"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Preconditions — fail with an exact remedy, not partway through a build.
# ---------------------------------------------------------------------------

[[ -x "$SYSTEM_PYTHON" ]] || die "$SYSTEM_PYTHON not found. This script targets the system CPython, not a personal venv (see docs/followups.md)."

PYVER="$("$SYSTEM_PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
if ! "$SYSTEM_PYTHON" -c '
import sys
v = sys.version_info[:2]
ok = (3, 10) <= v < (3, 13) and v != (3, 11)
sys.exit(0 if ok else 1)
'; then
  die "system python ($PYVER) is outside tt-bio's supported range (requires-python >=3.10,<3.13,!=3.11.*)"
fi
log "system python: $SYSTEM_PYTHON ($PYVER) — within tt-bio's supported range"

# Apt packages venv-ui inherits via --system-site-packages, plus the venv
# module itself and the system libraries the GTK app needs to actually run.
REQUIRED_APT_PKGS=(
  python3-venv
  python3-pip
  python3-gi
  python3-gi-cairo
  gir1.2-gtk-4.0
  python3-gemmi
  python3-opengl
  python3-numpy
  libgl1
  libglu1-mesa
)
MISSING_PKGS=()
for pkg in "${REQUIRED_APT_PKGS[@]}"; do
  dpkg -s "$pkg" >/dev/null 2>&1 || MISSING_PKGS+=("$pkg")
done
if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
  die "missing apt packages. Install them first with:
  sudo apt install -y ${MISSING_PKGS[*]}"
fi
log "all required apt packages present (${#REQUIRED_APT_PKGS[@]} checked)"

mkdir -p "$PREFIX" || die "could not create prefix directory: $PREFIX (permissions?)"

# Scratch file for buffering a verification check's output so it can be
# replayed after the fact (success -> stdout, failure -> stderr). mktemp
# instead of a $$-based name: predictable temp paths are worth avoiding even
# for a low-stakes case like this, and a trap means it's cleaned up on any
# exit path, not just the happy one.
VERIFY_TMP="$(mktemp)"
trap 'rm -f "$VERIFY_TMP"' EXIT

# ---------------------------------------------------------------------------
# 2. Verification helpers — run standalone so idempotency checks and
#    post-create checks share exactly one definition of "works".
# ---------------------------------------------------------------------------

# Prints a diagnostic and exits nonzero on failure; prints a one-line summary
# on success. Deliberately verbose about *which* import failed and why —
# a silent half-built environment is worse than a loud, specific error.
verify_ui_venv() {
  local venv="$1"
  "${venv}/bin/python3" - <<'PY'
import sys
try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
except Exception as e:
    print(f"VERIFY-FAIL: gi/Gtk4 import failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
try:
    import gemmi
except Exception as e:
    print(f"VERIFY-FAIL: gemmi import failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
try:
    import OpenGL
except Exception as e:
    print(f"VERIFY-FAIL: OpenGL import failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
try:
    import numpy
except Exception as e:
    print(f"VERIFY-FAIL: numpy import failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
print(
    f"gi/Gtk {Gtk.get_major_version()}.{Gtk.get_minor_version()} ok, "
    f"gemmi {gemmi.__version__}, OpenGL {OpenGL.__version__}, numpy {numpy.__version__}"
)
PY
}

verify_runner_venv() {
  local venv="$1"
  "${venv}/bin/python3" - <<'PY'
import sys
try:
    import tt_bio
except Exception as e:
    print(f"VERIFY-FAIL: tt_bio import failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
print(f"tt_bio ok (version={getattr(tt_bio, '__version__', 'unknown')})")
PY
}

# Version of tt-bio actually installed in a venv, or empty if none/unreadable.
#
# Deliberately not `pip show ... | awk '{...; exit}'`: awk's early `exit` closes
# its end of the pipe before pip finishes writing the rest of `pip show`'s
# output (Summary, Home-page, Location, ...), so pip gets SIGPIPE and the
# pipeline's exit status becomes nonzero under `pipefail`. In a plain
# assignment (not an `if`/`&&` condition) that trips `set -e` and kills the
# whole script with no error message — silent, and exactly backwards from
# this script's "fail loudly" goal. Capturing pip's full output first and
# parsing it with no live pipe underneath avoids the race entirely; the
# trailing `|| true` also means a venv with no tt-bio installed (pip show
# exits nonzero) reads as "no version" instead of aborting the script.
installed_tt_bio_version() {
  local venv="$1" out line
  out="$("${venv}/bin/python3" -m pip show tt-bio 2>/dev/null || true)"
  while IFS= read -r line; do
    case "$line" in
      Version:*)
        printf '%s\n' "${line#Version: }"
        return 0
        ;;
    esac
  done <<<"$out"
}

# ---------------------------------------------------------------------------
# 3. venv-ui
# ---------------------------------------------------------------------------

UI_STATUS="skipped"

create_ui_venv() {
  if [[ -d "$VENV_UI" ]]; then
    if [[ "$FORCE" -eq 1 ]]; then
      log "venv-ui: --force given, removing existing venv at $VENV_UI"
      rm -rf "$VENV_UI"
    elif verify_ui_venv "$VENV_UI" >"$VERIFY_TMP" 2>&1; then
      log "venv-ui: already present and verified at $VENV_UI — skipping (use --force to rebuild)"
      cat "$VERIFY_TMP"; rm -f "$VERIFY_TMP"
      UI_STATUS="already valid (skipped)"
      return 0
    else
      warn "venv-ui: existing venv at $VENV_UI failed verification — rebuilding"
      cat "$VERIFY_TMP" >&2; rm -f "$VERIFY_TMP"
      rm -rf "$VENV_UI"
    fi
  fi

  log "venv-ui: creating (--system-site-packages) at $VENV_UI"
  "$SYSTEM_PYTHON" -m venv --system-site-packages "$VENV_UI"
  "${VENV_UI}/bin/python3" -m pip install --upgrade pip >/dev/null

  log "venv-ui: verifying imports (gi/Gtk4, gemmi, OpenGL, numpy)"
  if ! verify_ui_venv "$VENV_UI"; then
    die "venv-ui: post-create verification failed (see VERIFY-FAIL line above). The venv was created but is not usable; not leaving it half-built silently."
  fi
  UI_STATUS="created and verified"
}

# ---------------------------------------------------------------------------
# 4. venv-runner
# ---------------------------------------------------------------------------

RUNNER_STATUS="skipped"
RUNNER_INSTALL_SECONDS=""
RUNNER_SIZE=""

create_runner_venv() {
  if [[ "$SKIP_RUNNER" -eq 1 ]]; then
    log "venv-runner: --skip-runner given, not touching it"
    RUNNER_STATUS="skipped (--skip-runner)"
    return 0
  fi

  if [[ -d "$VENV_RUNNER" ]]; then
    if [[ "$FORCE" -eq 1 ]]; then
      log "venv-runner: --force given, removing existing venv at $VENV_RUNNER"
      rm -rf "$VENV_RUNNER"
    else
      local installed
      installed="$(installed_tt_bio_version "$VENV_RUNNER")"
      if [[ "$installed" == "$TT_BIO_VERSION" ]]; then
        if verify_runner_venv "$VENV_RUNNER" >"$VERIFY_TMP" 2>&1; then
          log "venv-runner: already has tt-bio==$TT_BIO_VERSION and imports cleanly — skipping (use --force to rebuild)"
          cat "$VERIFY_TMP"; rm -f "$VERIFY_TMP"
          RUNNER_STATUS="already valid (skipped)"
          RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)"
          return 0
        else
          # pip already reports the pinned version installed but the import
          # fails. Re-running pip would not fix a missing system library, and
          # would re-pay the multi-GB download for nothing — so don't.
          warn "venv-runner: tt-bio==$TT_BIO_VERSION is installed but 'import tt_bio' fails:"
          cat "$VERIFY_TMP" >&2; rm -f "$VERIFY_TMP"
          warn "venv-runner: leaving the venv as-is (see docs/venv-bootstrap-notes.md for what this usually means). Use --force to rebuild anyway."
          RUNNER_STATUS="pinned version installed, import FAILS (left as-is)"
          RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)"
          return 0
        fi
      else
        log "venv-runner: found tt-bio version '${installed:-none}', want $TT_BIO_VERSION — rebuilding"
        rm -rf "$VENV_RUNNER"
      fi
    fi
  fi

  log "venv-runner: creating (isolated, no system site packages) at $VENV_RUNNER"
  "$SYSTEM_PYTHON" -m venv "$VENV_RUNNER"
  "${VENV_RUNNER}/bin/python3" -m pip install --upgrade pip >/dev/null

  log "venv-runner: installing tt-bio==${TT_BIO_VERSION} — this pulls torch + ttnn and is expected to be multiple GB and slow"
  local t0 t1
  t0=$(date +%s)
  if "${VENV_RUNNER}/bin/python3" -m pip install "tt-bio==${TT_BIO_VERSION}"; then
    t1=$(date +%s)
    RUNNER_INSTALL_SECONDS=$((t1 - t0))
    RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)"
    log "venv-runner: pip install finished in ${RUNNER_INSTALL_SECONDS}s, venv is now ${RUNNER_SIZE}"
  else
    t1=$(date +%s)
    RUNNER_INSTALL_SECONDS=$((t1 - t0))
    RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)"
    die "venv-runner: 'pip install tt-bio==${TT_BIO_VERSION}' FAILED after ${RUNNER_INSTALL_SECONDS}s (venv left at $VENV_RUNNER, ${RUNNER_SIZE}, for inspection). Not retrying automatically — see pip's own error above."
  fi

  log "venv-runner: verifying 'import tt_bio'"
  if verify_runner_venv "$VENV_RUNNER"; then
    RUNNER_STATUS="created and verified"
  else
    # pip succeeded but the import doesn't work — most likely Tenstorrent
    # system libraries/kernel modules are absent on this box. That is
    # expected here: this script deliberately never installs them (see
    # header comment). Report precisely; do not paper over it.
    RUNNER_STATUS="created; pip install OK but 'import tt_bio' FAILS (see above)"
  fi
}

# ---------------------------------------------------------------------------
# 5. Run it
# ---------------------------------------------------------------------------

create_ui_venv
create_runner_venv

# ---------------------------------------------------------------------------
# 6. Summary
# ---------------------------------------------------------------------------

echo
echo "==================== tt-bio-demo venv bootstrap summary ===================="
echo "prefix:        $PREFIX"
echo
echo "venv-ui:       $VENV_UI"
echo "  status:      $UI_STATUS"
echo "  activate:    source ${VENV_UI}/bin/activate"
echo "  run tests:   scripts/test.sh   (or: ${VENV_UI}/bin/python3 -m pytest)"
echo "  run the app: ${VENV_UI}/bin/python3 -m ui.app"
echo
echo "venv-runner:   $VENV_RUNNER"
echo "  status:      $RUNNER_STATUS"
if [[ -n "$RUNNER_INSTALL_SECONDS" ]]; then
  echo "  install took: ${RUNNER_INSTALL_SECONDS}s"
fi
if [[ -n "$RUNNER_SIZE" ]]; then
  echo "  on-disk size: ${RUNNER_SIZE}"
fi
echo "  activate:    source ${VENV_RUNNER}/bin/activate"
echo "  pinned:      tt-bio==${TT_BIO_VERSION}"
echo
echo "NOT run by this script (deliberately, needs explicit consent — Debian"
echo "packaging phase owns this): 'tt-bio install-deps' / any Tenstorrent"
echo "system package or kernel module install. If 'import tt_bio' failed above,"
echo "that is almost certainly why — see docs/venv-bootstrap-notes.md."
echo "==============================================================================="
