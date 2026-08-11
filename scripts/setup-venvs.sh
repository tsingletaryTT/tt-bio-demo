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
# bare-python3 gotcha in docs/followups.md. That gotcha predates this script;
# the script exists so nobody has to remember it by hand again.
#
# This script does NOT run `tt-bio install-deps` or touch any Tenstorrent
# system package / kernel module. Installing system-level TT dependencies is
# a Debian-packaging-phase decision that needs explicit consent, not
# something a venv bootstrap script should do on its own.
#
# Usage:
#   scripts/setup-venvs.sh [--prefix PATH] [--force] [--skip-runner] [--strict]
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
#   --strict        Treat a venv-runner whose torch/ttnn/tt_bio stack fails
#                    to import as a hard failure (die, exit 1) instead of a
#                    reported-but-nonfatal degraded state. See "Exit codes"
#                    below — without --strict this situation exits 2, which
#                    is expected pre-Phase-3 on a box with no Tenstorrent
#                    system libraries installed.
#
# Exit codes:
#   0   everything requested was built/verified and works.
#   1   a hard failure: bad preconditions, missing apt packages, venv-ui
#       failed verification, venv-runner's `pip install` itself failed, or
#       (with --strict) venv-runner's stack failed to import.
#   2   soft/degraded: venv-runner was created (or already existed) with the
#       pinned tt-bio version installed, but torch/ttnn/tt_bio does not
#       import. Without --strict this is reported, not fatal — see
#       docs/venv-bootstrap-notes.md for why that's the expected state on a
#       box without `tt-bio install-deps` run. Automation that needs to
#       distinguish "fully working" from "installed but unusable" should
#       check for exactly this code (or pass --strict to fold it into exit 1).
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
STRICT=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--prefix PATH] [--force] [--skip-runner] [--strict]

Creates <prefix>/venv-ui and <prefix>/venv-runner. Default prefix:
${REPO_ROOT}/.venvs

Exit codes: 0 fully working, 1 hard failure, 2 venv-runner installed but its
torch/ttnn/tt_bio stack does not import (folded into 1 by --strict).
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
    --strict)
      STRICT=1
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

# Scratch files, cleaned up on any exit path via one trap. mktemp rather than
# a $$-based name: predictable temp paths are worth avoiding even for a
# low-stakes case like this.
#   VERIFY_TMP     buffers a verification check's output so it can be
#                  replayed after the fact (success -> stdout, failure ->
#                  stderr).
#   RM_ERR_TMP     buffers rm -rf's stderr so safe_rm_rf can quote it in die().
#   VERIFY_RUNDIR  cwd for verify_runner_venv's python subprocess. `import
#                  ttnn` writes debug/inspector artifacts (a `generated/`
#                  tree) relative to the process's cwd, not to an absolute
#                  cache dir — discovered by this script itself littering the
#                  repo root with one. Running the check from a scratch dir
#                  instead means it litters something we throw away anyway.
VERIFY_TMP="$(mktemp)"
RM_ERR_TMP="$(mktemp)"
VERIFY_RUNDIR="$(mktemp -d)"
trap 'rm -f "$VERIFY_TMP" "$RM_ERR_TMP"; rm -rf "$VERIFY_RUNDIR"' EXIT

# rm -rf wrapped so a permissions failure (e.g. re-running as a non-root user
# against a root-owned /opt/tt-bio-demo the Debian postinst created) dies with
# a remedy instead of bash's raw "Permission denied" under set -e.
safe_rm_rf() {
  local target="$1"
  rm -rf "$target" 2>"$RM_ERR_TMP" || {
    local err
    err="$(cat "$RM_ERR_TMP" 2>/dev/null || true)"
    die "could not remove $target: ${err:-permission denied}. If this belongs to another user (e.g. root, from a prior Debian install), rerun as that user or with sudo."
  }
}

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

# Apt packages venv-ui inherits via --system-site-packages, the system
# libraries the GTK app needs to actually run, and the tools venv-runner's
# SFPI vendoring (see section 4b below) shells out to.
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
  curl
  xz-utils
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

# Checks torch, ttnn, tt_bio, and tt_bio.tenstorrent individually — NOT just
# `import tt_bio` — because `import tt_bio` alone does not exercise them.
# Verified empirically (not assumed) by diffing sys.modules before/after a
# bare `import tt_bio` in this exact venv: it pulls in nothing but stdlib.
# tt_bio's own top-level __init__.py is deliberately lazy (just reads its own
# installed version via importlib.metadata); the real work — and the real
# `import torch, ttnn` — lives one level down, in modules like tenstorrent.py,
# protenix.py, boltz2.py, worker.py. So a bare `import tt_bio` reports success
# even in a venv where torch has been wiped out by an interrupted install:
# confirmed by copying venv-runner, deleting its torch/ tree and dist-info,
# and re-running this check against the copy (see docs/venv-bootstrap-notes.md).
#
# torch and ttnn are checked directly, by name, so a failure names the actual
# missing/broken piece. tt_bio.tenstorrent — the shared Tenstorrent-compute
# primitives module every model family (boltz2, protenix, opendde, ...) is
# built on, per its own docstrings — is then imported as a real smoke test
# that tt_bio's own code loads against the torch/ttnn actually present, not
# just that the lazy top-level package does nothing and reports success.
#
# Even that is not enough, though: torch/ttnn/tt_bio.tenstorrent all import
# fine even when `ttnn.open_device()` cannot actually work — confirmed on
# this box, where a version-skewed SFPI toolchain (see ensure_sfpi_installed
# below) breaks JIT kernel compilation with a TT_THROW deep inside
# open_device, well after every import above has already succeeded. Imports
# alone cannot catch that class of failure because none of them open a
# device. So this also does a real, best-effort device probe:
# `ttnn.get_num_devices()` first distinguishes "no Tenstorrent hardware on
# this box" (legitimate on a packaging/CI machine, not a failure) from
# "hardware is present" — only when it's present does open_device(0) get
# tried, and only a failure in that case counts as a real, reportable
# problem. This deliberately still never calls anything that needs
# `tt-bio install-deps`'s system libraries/kernel modules beyond what
# opening a device already requires on this box.
verify_runner_venv() {
  local venv="$1"
  # Run from VERIFY_RUNDIR, not the caller's cwd: `import ttnn` writes a
  # `generated/` debug-artifact tree relative to the process's working
  # directory (see VERIFY_RUNDIR's definition above for how that was found).
  (cd "$VERIFY_RUNDIR" && "${venv}/bin/python3" - <<'PY'
import os, sys

# Match tt_bio's own suppression (see its main.py) so a plain import doesn't
# dump ttnn/tt-metal debug logging and nanobind leak-tracker noise into what
# is meant to be a one-line health check.
os.environ.setdefault("LOGURU_LEVEL", "WARNING")
os.environ.setdefault("TT_METAL_LOGGER_LEVEL", "FATAL")

try:
    import torch
except Exception as e:
    print(f"VERIFY-FAIL: torch import failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
try:
    import ttnn
except Exception as e:
    print(f"VERIFY-FAIL: ttnn import failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
try:
    import tt_bio
except Exception as e:
    print(f"VERIFY-FAIL: tt_bio import failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
try:
    import tt_bio.tenstorrent
except Exception as e:
    print(f"VERIFY-FAIL: tt_bio.tenstorrent import failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)

stack_ok = (
    f"torch {torch.__version__} ok, ttnn ok, "
    f"tt_bio {getattr(tt_bio, '__version__', 'unknown')} ok, tt_bio.tenstorrent ok"
)

# Device probe. get_num_devices() itself can only fail if the ttnn stack is
# broken in a way imports above didn't already catch, so a failure here is
# still a real failure, not a "no hardware" signal.
try:
    n = ttnn.get_num_devices()
except Exception as e:
    print(f"VERIFY-FAIL: ttnn.get_num_devices() failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)

if n == 0:
    print(f"{stack_ok}, device probe SKIPPED (0 Tenstorrent devices detected)")
    sys.exit(0)

try:
    dev = ttnn.open_device(device_id=0)
    ttnn.close_device(dev)
except Exception as e:
    print(
        f"VERIFY-FAIL: ttnn.open_device(0) failed with {n} device(s) detected: "
        f"{type(e).__name__}: {e}",
        file=sys.stderr,
    )
    sys.exit(1)

print(f"{stack_ok}, device probe OK ({n} device(s) detected, opened+closed device 0)")
PY
  )
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
      safe_rm_rf "$VENV_UI"
    elif verify_ui_venv "$VENV_UI" >"$VERIFY_TMP" 2>&1; then
      log "venv-ui: already present and verified at $VENV_UI — skipping (use --force to rebuild)"
      cat "$VERIFY_TMP"; rm -f "$VERIFY_TMP"
      UI_STATUS="already valid (skipped)"
      return 0
    else
      warn "venv-ui: existing venv at $VENV_UI failed verification — rebuilding"
      cat "$VERIFY_TMP" >&2; rm -f "$VERIFY_TMP"
      safe_rm_rf "$VENV_UI"
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
RUNNER_DEGRADED=0   # 1 iff venv-runner has tt-bio installed but its stack won't import (see exit code 2 below)

# ---------------------------------------------------------------------------
# 4b. venv-runner's SFPI toolchain
# ---------------------------------------------------------------------------
#
# ttnn JIT-compiles Tenstorrent device kernels with a RISC-V toolchain called
# SFPI. tt-bio 0.6.2's ttnn==0.68.0 requires SFPI 7.35.3 specifically (see
# <ttnn>/tt_metal/sfpi-version, the wheel's own manifest) — but this box also
# has a *different* SFPI (7.61.0) installed system-wide at
# /opt/tenstorrent/sfpi, shared with the user's other Tenstorrent projects.
# ttnn's shared objects have two SFPI paths baked in: that absolute system
# path, and a *relative* <ttnn>/runtime/sfpi, which the wheel ships empty.
# Whichever is present wins the relative one if present at all — no
# environment variable involved (confirmed: unsetting TT_METAL_HOME makes no
# difference either way). Version skew between the two breaks kernel
# compilation: confirmed on this box, `ttnn.open_device()` throws
# `TT_THROW @ tt_metal/jit_build/build.cpp:60` ("lto1: internal compiler
# error in lto_read_decls") with only the system 7.61.0 in play, and clean
# open_device()/close_device() with a vendored 7.35.3 dropped into
# <ttnn>/runtime/sfpi instead — moving that vendored copy aside reintroduces
# the exact same failure, which is what actually demonstrates the mechanism
# rather than just correlating with it.
#
# So venv-runner gets its own private, version-matched SFPI rather than
# relying on (or mutating) the system one. This is deliberately *not* what
# `tt-bio install-deps` would do for SFPI — that installs/upgrades the
# *system* copy, which would fix this venv but could just as easily break
# whichever other Tenstorrent project on this box is pinned to 7.61.0.
# Vendoring is the same "give each thing its own environment" principle the
# rest of this script is built on, applied one level deeper — and it also
# means `tt-bio install-deps` is no longer needed for SFPI at all, which is
# one less system-mutating step a turnkey Debian install has to take.
#
# The version and its hashes are read out of the manifest the venv's own
# ttnn wheel ships — never hardcoded here — so a future TT_BIO_VERSION bump
# that changes ttnn's required SFPI is picked up automatically instead of
# silently installing a stale one.

# Sources <ttnn>/tt_metal/sfpi-version — a handful of `name='value'` shell
# assignments (sfpi_repo, sfpi_version, sfpi_hashtype, and one sha256 hash
# per platform/package-format) — into the caller's own local variables.
# tt-metal's own dockerfile/scripts/install-sfpi.sh reads this same file the
# same way; predeclaring e.g. `local sfpi_version=""` in the *caller* before
# calling this is what makes the assignments land there and not leak
# globally — bash's dynamic scoping resolves a plain assignment to the
# nearest enclosing local of that name up the call stack, and this function
# deliberately declares none of its own so the caller's locals are that
# nearest enclosing scope.
read_sfpi_manifest() {
  local ttnn_dir="$1"
  local manifest="${ttnn_dir}/tt_metal/sfpi-version"
  [[ -r "$manifest" ]] || die "SFPI manifest not found at $manifest — does this ttnn wheel still ship one? (this script needs updating if the layout changed)"
  # shellcheck disable=SC1090
  source "$manifest"
}

# Prints "<arch>_<distro>", matching tenstorrent/sfpi's own sfpi-info.sh
# algorithm (the canonical generator of both the manifest and its own
# install scripts): arch is exactly `uname -m`; distro is /etc/os-release's
# ID, UNLESS ID_LIKE names a debian or fedora ancestor, in which case that
# wins — so e.g. Ubuntu's ID=ubuntu, ID_LIKE=debian resolves to "debian",
# because the manifest ships one binary per upstream family, not one per
# downstream distro.
sfpi_platform_key() {
  local arch dist="" id="" id_like="" like
  arch="$(uname -m)"
  if [[ -r /etc/os-release ]]; then
    id="$(. /etc/os-release; printf '%s' "${ID:-}")"
    id_like="$(. /etc/os-release; printf '%s' "${ID_LIKE:-}")"
  fi
  dist="$id"
  for like in $id_like; do
    case "$like" in
      debian) dist=debian; break ;;
      fedora) dist=fedora; break ;;
    esac
  done
  printf '%s_%s\n' "$arch" "$dist"
}

# Ensures <venv>'s ttnn has the SFPI version its own manifest declares,
# vendored at <ttnn>/runtime/sfpi. Idempotent via a receipt file this
# function writes itself immediately after a hash-verified install — an
# existing runtime/sfpi *without* a matching receipt (hand-placed by a
# person, left over from a different ttnn version, or anything else this
# function didn't itself just verify) is never trusted on the strength of
# merely existing; it's replaced.
ensure_sfpi_installed() {
  local venv="$1"
  local purelib ttnn_dir
  purelib="$("${venv}/bin/python3" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
  ttnn_dir="${purelib}/ttnn"
  [[ -d "$ttnn_dir" ]] || die "ensure_sfpi_installed: no ttnn package found under $purelib — did tt-bio's install actually bring in ttnn?"

  local sfpi_repo="" sfpi_version="" sfpi_hashtype="" sfpi_build="" sfpi_base=""
  read_sfpi_manifest "$ttnn_dir"
  if [[ -z "$sfpi_repo" || -z "$sfpi_version" || -z "$sfpi_hashtype" ]]; then
    die "ensure_sfpi_installed: could not parse sfpi_repo/sfpi_version/sfpi_hashtype out of ${ttnn_dir}/tt_metal/sfpi-version"
  fi

  local platform_key hash_var expected_hash
  platform_key="$(sfpi_platform_key)"
  hash_var="sfpi_${platform_key}_txz_hash"
  expected_hash="${!hash_var:-}"
  if [[ -z "$expected_hash" ]]; then
    die "ensure_sfpi_installed: no SFPI ${sfpi_version} .txz release published for this platform ($platform_key). ${ttnn_dir}/tt_metal/sfpi-version only lists:
$(grep -E '^sfpi_[a-z0-9_]+_hash=' "${ttnn_dir}/tt_metal/sfpi-version")"
  fi

  local sfpi_dir="${ttnn_dir}/runtime/sfpi"
  local receipt="${sfpi_dir}/.tt-bio-demo-sfpi-receipt"
  local receipt_line="version=${sfpi_version} sha256=${expected_hash}"

  if [[ -f "$receipt" ]] && grep -qxF "$receipt_line" "$receipt" 2>/dev/null; then
    log "venv-runner: SFPI ${sfpi_version} already vendored at ${sfpi_dir} (verified receipt) — skipping (use --force to reinstall)"
    return 0
  fi

  if [[ -d "$sfpi_dir" ]]; then
    log "venv-runner: ${sfpi_dir} exists but has no matching receipt (untrusted — hand-placed, stale, or from a different SFPI version) — replacing it"
    safe_rm_rf "$sfpi_dir"
  fi

  local filename url download_path
  filename="sfpi_${sfpi_version}_${platform_key}.txz"
  url="${sfpi_repo}/releases/download/${sfpi_version}/${filename}"
  download_path="${VERIFY_RUNDIR}/${filename}"

  log "venv-runner: downloading SFPI ${sfpi_version} (${platform_key}) from ${url}"
  if ! curl -fsSL -o "$download_path" "$url"; then
    rm -f "$download_path"
    die "venv-runner: SFPI download failed: $url"
  fi

  local actual_hash
  actual_hash="$(sha256sum "$download_path" | cut -d' ' -f1)"
  if [[ "$actual_hash" != "$expected_hash" ]]; then
    rm -f "$download_path"
    die "venv-runner: SFPI ${sfpi_version} sha256 mismatch for ${filename} — expected ${expected_hash}, got ${actual_hash}. This is a compiler toolchain fetched over the network; refusing to install one that doesn't match its published hash. Not retrying automatically."
  fi
  log "venv-runner: SFPI ${sfpi_version} sha256 verified"

  mkdir -p "${ttnn_dir}/runtime"
  # The tarball's own top-level entry is "sfpi/", so extracting into
  # runtime/ (not runtime/sfpi/) lands it at runtime/sfpi/... directly,
  # rather than the doubly-nested runtime/sfpi/sfpi/... a naive
  # `mkdir sfpi && tar -C sfpi` would produce.
  tar -xJf "$download_path" -C "${ttnn_dir}/runtime"
  rm -f "$download_path"
  [[ -d "$sfpi_dir" ]] || die "venv-runner: extracted the SFPI tarball but ${sfpi_dir} doesn't exist — unexpected tarball layout (expected a top-level 'sfpi/' entry)"

  printf '%s\n' "$receipt_line" >"$receipt"
  log "venv-runner: SFPI ${sfpi_version} installed to ${sfpi_dir}"
}

create_runner_venv() {
  if [[ "$SKIP_RUNNER" -eq 1 ]]; then
    log "venv-runner: --skip-runner given, not touching it"
    RUNNER_STATUS="skipped (--skip-runner)"
    return 0
  fi

  if [[ -d "$VENV_RUNNER" ]]; then
    if [[ "$FORCE" -eq 1 ]]; then
      log "venv-runner: --force given, removing existing venv at $VENV_RUNNER"
      safe_rm_rf "$VENV_RUNNER"
    else
      local installed
      installed="$(installed_tt_bio_version "$VENV_RUNNER")"
      if [[ "$installed" == "$TT_BIO_VERSION" ]]; then
        # Cheap once installed (a receipt check, not a re-download) — run on
        # every idempotent pass so an existing venv whose SFPI is missing,
        # stale, or untrusted gets fixed without redoing the pip install.
        ensure_sfpi_installed "$VENV_RUNNER"
        if verify_runner_venv "$VENV_RUNNER" >"$VERIFY_TMP" 2>&1; then
          log "venv-runner: already has tt-bio==$TT_BIO_VERSION with a matching SFPI, and its torch/ttnn/tt_bio stack (plus device probe) checks out — skipping (use --force to rebuild)"
          cat "$VERIFY_TMP"; rm -f "$VERIFY_TMP"
          RUNNER_STATUS="already valid (skipped)"
          RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)" || true
          return 0
        else
          # pip already reports the pinned version installed, and SFPI is in
          # place, but the deep import/device check still fails. Re-running
          # pip would not fix a missing system library or a genuinely broken
          # card, and would re-pay the multi-GB download for nothing — so
          # don't, unless --force says to.
          RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)" || true
          if [[ "$STRICT" -eq 1 ]]; then
            cat "$VERIFY_TMP" >&2; rm -f "$VERIFY_TMP"
            die "venv-runner: tt-bio==$TT_BIO_VERSION is installed but its torch/ttnn/tt_bio stack or device probe fails (--strict). Rerun with --force to rebuild, or without --strict to treat this as a reported-but-nonfatal degraded state."
          fi
          warn "venv-runner: tt-bio==$TT_BIO_VERSION is installed but its torch/ttnn/tt_bio stack or device probe fails:"
          cat "$VERIFY_TMP" >&2; rm -f "$VERIFY_TMP"
          warn "venv-runner: leaving the venv as-is (see docs/venv-bootstrap-notes.md for what this usually means). Use --force to rebuild anyway, or --strict to make this fatal."
          RUNNER_STATUS="pinned version installed, import/device check FAILS (left as-is)"
          RUNNER_DEGRADED=1
          return 0
        fi
      else
        log "venv-runner: found tt-bio version '${installed:-none}', want $TT_BIO_VERSION — rebuilding"
        safe_rm_rf "$VENV_RUNNER"
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
    RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)" || true
    log "venv-runner: pip install finished in ${RUNNER_INSTALL_SECONDS}s, venv is now ${RUNNER_SIZE}"
  else
    t1=$(date +%s)
    RUNNER_INSTALL_SECONDS=$((t1 - t0))
    RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)" || true
    die "venv-runner: 'pip install tt-bio==${TT_BIO_VERSION}' FAILED after ${RUNNER_INSTALL_SECONDS}s (venv left at $VENV_RUNNER, ${RUNNER_SIZE}, for inspection). Not retrying automatically — see pip's own error above."
  fi

  ensure_sfpi_installed "$VENV_RUNNER"
  RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)" || true   # SFPI adds ~435M; refresh the reported size

  log "venv-runner: verifying torch/ttnn/tt_bio import and a real device probe"
  if verify_runner_venv "$VENV_RUNNER"; then
    RUNNER_STATUS="created and verified"
  else
    # pip and SFPI both succeeded but the deep check still fails — most
    # likely Tenstorrent system libraries/kernel modules are absent on this
    # box (SFPI itself is vendored per-venv now, so it's no longer the
    # likely cause; see docs/venv-bootstrap-notes.md). Report precisely; do
    # not paper over it.
    if [[ "$STRICT" -eq 1 ]]; then
      die "venv-runner: pip install succeeded but its torch/ttnn/tt_bio stack or device probe fails (--strict, see VERIFY-FAIL line above)."
    fi
    RUNNER_STATUS="created; pip install OK but import/device check FAILS (see above)"
    RUNNER_DEGRADED=1
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
echo "system package or kernel module install. SFPI — the piece install-deps"
echo "used to matter for here — is now vendored per-venv above, matched to"
echo "what this ttnn build needs, so it no longer depends on install-deps at"
echo "all. If the import/device check still failed above, the likely cause is"
echo "a missing or mismatched Tenstorrent driver/kernel module instead — see"
echo "docs/venv-bootstrap-notes.md."
if [[ "$RUNNER_DEGRADED" -eq 1 ]]; then
  echo
  echo "exit code: 2 (venv-runner installed but degraded — see 'status' above)"
fi
echo "==============================================================================="

# See the "Exit codes" block in the header comment. --strict already turned
# a degraded runner into a die() (exit 1) earlier; this only fires when
# --strict was NOT given, so exit 2 is reachable exactly when the summary
# above reports a degraded (but non-fatal) venv-runner.
if [[ "$RUNNER_DEGRADED" -eq 1 ]]; then
  exit 2
fi
