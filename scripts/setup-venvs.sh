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
#   scripts/setup-venvs.sh [--prefix PATH] [--force] [--skip-runner] [--strict] [--dev]
#
#   --prefix PATH   Where to create venv-ui/ and venv-runner/. Default:
#                    <repo>/.venvs (gitignored). The Debian postinst passes
#                    /opt/tt-bio-demo here in a later phase.
#   --force         Recreate both venvs from scratch even if they already
#                    look valid. Without this flag, re-running is a cheap
#                    no-op once both venvs are verified.
#   --skip-weights  Do not fetch the model weights. The booth cannot fold
#                    without them (~3.7 GB: the protenix-v2 checkpoint and the
#                    CCD molecule library), so this is an opt-OUT, not an
#                    opt-in — a source install that builds both venvs and
#                    stops leaves a box that looks finished and cannot fold.
#                    That is exactly what a user reported: "I had to discover
#                    a model downloading command". Implied by --skip-runner,
#                    since the fetch runs through venv-runner's own tt-bio.
#   --skip-runner   Only build/verify venv-ui. venv-runner's `pip install
#                    tt-bio` pulls torch + ttnn and can be multiple GB and
#                    slow (see docs/venv-bootstrap-notes.md) — useful while
#                    iterating on the UI side.
#   --strict        Treat a venv-runner whose torch/ttnn/tt_bio stack or
#                    device probe fails as a hard failure (die, exit 1)
#                    instead of a reported-but-nonfatal degraded state. See
#                    "Exit codes" below — without --strict this situation
#                    exits 2.
#   --dev           Also install pytest into venv-runner, so Phase 3a's own
#                    unit tests (tests/unit/test_runner_env.py and everything
#                    after it — see docs/superpowers/plans/2026-08-11-runner-
#                    daemon.md, which runs every step through
#                    `venv-runner/bin/python3 -m pytest`) can actually run
#                    there. NOT on by default: venv-runner is the exact
#                    artifact a Debian postinst builds for a booth machine
#                    (see --prefix above), and test tooling is dead weight
#                    and extra supply-chain surface on that machine — the
#                    same reasoning that keeps `tt-bio install-deps` and the
#                    system SFPI out of this script's default path (see
#                    section 4b below). Pass this when bootstrapping a dev
#                    box for Phase 3a work; leave it off for a production
#                    build. See docs/venv-bootstrap-notes.md for the
#                    reasoning and the reproducibility check performed.
#
# Exit codes:
#   0   everything requested was built/verified and works — including, on a
#       box with Tenstorrent cards physically present, a real device
#       open/close. Also 0 when no Tenstorrent PCI hardware is present at
#       all (see verify_runner_venv's device probe below for why that is a
#       distinct, deliberately checked case).
#   1   a hard failure: bad preconditions, missing apt packages, venv-ui
#       failed verification, venv-runner's `pip install`/SFPI
#       download/hash-verify/extraction itself failed, or (with --strict)
#       venv-runner's stack or device probe failed.
#   2   soft/degraded: venv-runner was created (or already existed) with the
#       pinned tt-bio version and a matching SFPI installed, but its
#       torch/ttnn/tt_bio stack doesn't import, OR Tenstorrent PCI hardware
#       is physically present but not usable (driver not loaded/bound, a
#       failed open_device(), or the probe timed out — a possible wedged
#       card). Without --strict this is reported, not fatal. Automation
#       that needs to distinguish "fully working" from "installed but
#       unusable" should check for exactly this code (or pass --strict to
#       fold it into exit 1). See docs/venv-bootstrap-notes.md for what each
#       cause actually looks like and how it was verified — an earlier draft
#       of this comment claimed this code meant "no Tenstorrent system
#       libraries," which turned out not to be reliably true; see that doc's
#       "Exit codes" section for the correction.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# The one place tt-bio's version is pinned. Bump deliberately, never track
# tt-bio's `main` (see CLAUDE.md conventions — same rule as the runner venv
# tt-bio-demo will eventually shell out to).
# ---------------------------------------------------------------------------
TT_BIO_VERSION="0.7.0"

# Bounds on the two network/hardware-adjacent operations that could otherwise
# hang this script indefinitely in an unattended postinst with nobody present
# to notice. Generous on purpose: SFPI is ~80MB (fine even on a slow venue
# link) and the device probe's happy path (imports + open/close) is a couple
# of seconds on this box, but a wedged Tenstorrent card — a documented
# hardware state needing a warm reset — could plausibly hang rather than
# fail fast, and imports alone don't rule that out.
SFPI_DOWNLOAD_CONNECT_TIMEOUT_SECONDS=15
SFPI_DOWNLOAD_MAX_TIME_SECONDS=300
DEVICE_PROBE_TIMEOUT_SECONDS=120

SYSTEM_PYTHON="/usr/bin/python3"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PREFIX="${REPO_ROOT}/.venvs"
FORCE=0
SKIP_RUNNER=0
SKIP_WEIGHTS=0
STRICT=0
DEV=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--prefix PATH] [--force] [--skip-runner]
       [--skip-weights] [--strict] [--dev]

Creates <prefix>/venv-ui and <prefix>/venv-runner. Default prefix:
${REPO_ROOT}/.venvs

--skip-weights skips the ~3.7 GB model-weight download. It is on by
default because a booth without weights cannot fold; the download is
resumable and re-running this script is a cheap no-op once they are present.

--dev also installs pytest into venv-runner, for running this phase's own
unit tests there (see the header comment) — off by default, since
venv-runner is the same artifact a production/Debian build creates and test
tooling shouldn't ship on a booth machine.

Exit codes: 0 fully working (including "no Tenstorrent hardware present"),
1 hard failure, 2 venv-runner installed but its stack doesn't import or its
device probe fails (folded into 1 by --strict). See the header comment.
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
    --skip-weights)
      SKIP_WEIGHTS=1
      shift
      ;;
    --strict)
      STRICT=1
      shift
      ;;
    --dev)
      DEV=1
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
# open_device (and, on at least one run, an outright segfault instead of a
# catchable exception for the identical cause), well after every import
# above has already succeeded. Imports alone cannot catch that class of
# failure because none of them open a device. So this also does a real,
# best-effort device probe.
#
# That probe cannot simply trust `ttnn.get_num_devices() == 0` as "no
# hardware", though — traced through tt-metal: it calls
# GetNumAvailableDevices() -> Cluster::number_of_user_devices() -> UMD's
# PCIDevice::enumerate_devices(), which does
# `if (!std::filesystem::exists("/dev/tenstorrent/")) return {};` with no
# exception. /dev/tenstorrent/* is created by the tt-kmd kernel module, so a
# box with cards physically present whose driver is merely unloaded,
# missing, or failed to bind reports the exact same 0 as a card-less
# packaging machine — the two are indistinguishable from get_num_devices()
# alone, and only one of them is a real failure. So this probe first
# establishes physical presence independently, by counting Tenstorrent PCI
# devices (vendor 0x1e52) via sysfs — PCI enumeration happens in the kernel
# regardless of whether any driver is bound, unlike /dev/tenstorrent. Three
# states fall out of combining the two signals:
#   PCI count == 0                        -> no cards at all: pass, probe skipped
#   PCI count > 0, get_num_devices() > 0  -> cards present and usable: try opening one
#   PCI count > 0, get_num_devices() == 0 -> cards present, driver absent/unbound: FAIL
#
# The whole probe (imports included) runs under `timeout` — a wedged card is
# a documented Tenstorrent hardware state needing a warm reset, and a
# version-mismatched toolchain hanging instead of crashing is not something
# either failure mode reproduced here rules out. An unattended postinst with
# no timeout would rather hang forever than report a failure.
verify_runner_venv() {
  local venv="$1"
  local rc
  # Run from VERIFY_RUNDIR, not the caller's cwd: `import ttnn` writes a
  # `generated/` debug-artifact tree relative to the process's working
  # directory (see VERIFY_RUNDIR's definition above for how that was found).
  (cd "$VERIFY_RUNDIR" && timeout --kill-after=10s "${DEVICE_PROBE_TIMEOUT_SECONDS}s" "${venv}/bin/python3" - <<'PY'
import os, sys
from pathlib import Path

# Match tt_bio's own suppression (see its main.py) so a plain import doesn't
# dump ttnn/tt-metal debug logging and nanobind leak-tracker noise into what
# is meant to be a one-line health check.
os.environ.setdefault("LOGURU_LEVEL", "WARNING")
os.environ.setdefault("TT_METAL_LOGGER_LEVEL", "FATAL")

TENSTORRENT_PCI_VENDOR_ID = "0x1e52"


def physical_tt_pci_device_count(root="/sys/bus/pci/devices"):
    """Count Tenstorrent PCI devices via sysfs, independent of the tt-kmd
    kernel driver's load/bind state (see the big comment above this
    function's caller in setup-venvs.sh for why `ttnn.get_num_devices()`
    alone cannot make this distinction). `root` is a parameter, not a
    hardcoded path, specifically so this can be exercised against a fake
    sysfs tree in tests without touching real hardware or drivers.
    """
    count = 0
    try:
        for vendor_path in Path(root).glob("*/vendor"):
            try:
                if vendor_path.read_text().strip().lower() == TENSTORRENT_PCI_VENDOR_ID:
                    count += 1
            except OSError:
                continue
    except OSError:
        pass
    return count


pci_count = physical_tt_pci_device_count()

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

if pci_count == 0:
    print(f"{stack_ok}, device probe SKIPPED (0 Tenstorrent PCI devices detected)")
    sys.exit(0)

# get_num_devices() itself can only fail if the ttnn stack is broken in a
# way imports above didn't already catch, so a failure here is still a real
# failure, not a "no hardware" signal.
try:
    n = ttnn.get_num_devices()
except Exception as e:
    print(f"VERIFY-FAIL: ttnn.get_num_devices() failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)

if n == 0:
    print(
        f"VERIFY-FAIL: {pci_count} Tenstorrent PCI device(s) present but "
        f"ttnn.get_num_devices() reports 0 -- the tt-kmd driver is most "
        f"likely not loaded or not bound (this is NOT the same as no "
        f"hardware; see docs/venv-bootstrap-notes.md)",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    dev = ttnn.open_device(device_id=0)
    ttnn.close_device(dev)
except Exception as e:
    print(
        f"VERIFY-FAIL: ttnn.open_device(0) failed with {n} usable device(s) "
        f"reported ({pci_count} PCI device(s) present): {type(e).__name__}: {e}",
        file=sys.stderr,
    )
    sys.exit(1)

print(
    f"{stack_ok}, device probe OK ({pci_count} PCI device(s) present, "
    f"{n} usable, opened+closed device 0)"
)
PY
  )
  rc=$?
  if [[ $rc -eq 124 ]]; then
    echo "VERIFY-FAIL: device probe timed out after ${DEVICE_PROBE_TIMEOUT_SECONDS}s — treating as a probe failure, not a hang (possible wedged card; may need a warm reset)" >&2
  fi
  return "$rc"
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
TEST_DEPS_STATUS="not installed (pass --dev to add pytest for Phase 3a's unit tests)"

# Reports whether pytest is importable in venv-runner via TEST_DEPS_STATUS,
# and — only when --dev was passed — installs it if it isn't there yet.
# Called unconditionally at both sites in create_runner_venv below (not
# itself gated on $DEV) so the summary's "test deps" line always reflects
# reality, including on a plain re-run after an earlier --dev run already
# put pytest there; only the actual `pip install` is gated.
#
# No version pin, unlike tt-bio/SFPI: pytest is dev tooling with no coupling
# to what venv-runner actually runs (a version mismatch there doesn't break
# kernel compilation the way SFPI skew does), so "already importable" is a
# good enough idempotency check without a receipt file.
#
# Installing is deliberately gated behind --dev rather than unconditional:
# venv-runner is not just a dev convenience, it's the exact venv a Debian
# postinst builds for a real booth machine (--prefix /opt/tt-bio-demo, see
# the header comment) — test tooling and its dependency chain have no
# business shipping there, same reasoning that keeps `tt-bio install-deps`
# and the system SFPI out of this script by default (section 4b below).
ensure_test_deps_installed() {
  local venv="$1"
  if "${venv}/bin/python3" -c "import pytest" >/dev/null 2>&1; then
    TEST_DEPS_STATUS="installed (pytest)"
    return 0
  fi
  if [[ "$DEV" -ne 1 ]]; then
    TEST_DEPS_STATUS="not installed (pass --dev to add pytest for Phase 3a's unit tests)"
    return 0
  fi
  log "venv-runner: --dev given, installing pytest so this phase's unit tests can run here"
  "${venv}/bin/python3" -m pip install pytest >/dev/null || die "venv-runner: 'pip install pytest' failed (--dev)"
  TEST_DEPS_STATUS="installed (pytest, just added via --dev)"
}

# ---------------------------------------------------------------------------
# 4b. venv-runner's SFPI toolchain
# ---------------------------------------------------------------------------
#
# ttnn JIT-compiles Tenstorrent device kernels with a RISC-V toolchain called
# SFPI. tt-bio 0.6.3's ttnn==0.68.0 requires SFPI 7.35.3 specifically (see
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

  # sfpi_build/sfpi_base are captured (via read_sfpi_manifest's dynamic
  # scoping, see its own comment) but never read here — they exist so the
  # manifest's assignments to them land in a real local instead of leaking
  # into the global namespace, not because this function needs their value.
  # shellcheck disable=SC2034
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

  # Note an existing-but-untrusted directory, but do NOT touch it yet. It
  # only gets removed once a fully downloaded, hash-verified, extracted
  # replacement is ready to take its place — never before. A transient
  # network blip during download/verify must leave the old (even if
  # wrong-version) directory exactly as it was, not an empty one; a naive
  # "remove first, then fetch" ordering was the previous shape of this
  # function and a review round caught it turning "wrong version present"
  # into "nothing present" on a failed download.
  if [[ -d "$sfpi_dir" ]]; then
    log "venv-runner: ${sfpi_dir} exists but has no matching receipt (untrusted — hand-placed, stale, or from a different SFPI version) — will replace it once a verified copy is ready"
  fi

  local filename url download_path
  filename="sfpi_${sfpi_version}_${platform_key}.txz"
  url="${sfpi_repo}/releases/download/${sfpi_version}/${filename}"
  download_path="${VERIFY_RUNDIR}/${filename}"

  log "venv-runner: downloading SFPI ${sfpi_version} (${platform_key}) from ${url}"
  if ! curl -fsSL \
       --connect-timeout "$SFPI_DOWNLOAD_CONNECT_TIMEOUT_SECONDS" \
       --max-time "$SFPI_DOWNLOAD_MAX_TIME_SECONDS" \
       -o "$download_path" "$url"; then
    rm -f "$download_path"
    die "venv-runner: SFPI download failed or timed out: $url (existing ${sfpi_dir}, if any, is untouched)"
  fi

  local actual_hash
  actual_hash="$(sha256sum "$download_path" | cut -d' ' -f1)"
  if [[ "$actual_hash" != "$expected_hash" ]]; then
    rm -f "$download_path"
    die "venv-runner: SFPI ${sfpi_version} sha256 mismatch for ${filename} — expected ${expected_hash}, got ${actual_hash}. This is a compiler toolchain fetched over the network; refusing to install one that doesn't match its published hash (existing ${sfpi_dir}, if any, is untouched). Not retrying automatically."
  fi
  log "venv-runner: SFPI ${sfpi_version} sha256 verified"

  # Extract to a staging directory, never straight into runtime/ — so a
  # partial or failed extraction can never leave runtime/sfpi half-written,
  # and the existing directory (if any) still isn't touched yet.
  mkdir -p "${ttnn_dir}/runtime"
  local staging_parent
  staging_parent="$(mktemp -d "${ttnn_dir}/runtime/.sfpi-staging.XXXXXX")" || die "venv-runner: could not create a staging directory under ${ttnn_dir}/runtime"
  if ! tar -xJf "$download_path" -C "$staging_parent"; then
    rm -f "$download_path"
    safe_rm_rf "$staging_parent"
    die "venv-runner: extracting the SFPI tarball failed (existing ${sfpi_dir}, if any, is untouched). Not retrying automatically."
  fi
  rm -f "$download_path"
  # The tarball's own top-level entry is "sfpi/", so it lands at
  # staging_parent/sfpi/... — check for exactly that, not a doubly-nested
  # staging_parent/sfpi/sfpi/... a different tarball layout would produce.
  if [[ ! -d "${staging_parent}/sfpi" ]]; then
    safe_rm_rf "$staging_parent"
    die "venv-runner: extracted the SFPI tarball but ${staging_parent}/sfpi doesn't exist — unexpected tarball layout (expected a top-level 'sfpi/' entry). Existing ${sfpi_dir}, if any, is untouched."
  fi
  printf '%s\n' "$receipt_line" >"${staging_parent}/sfpi/.tt-bio-demo-sfpi-receipt"

  # Swap the verified copy into place. Renaming the old directory aside
  # first (an O(1) rename, not a recursive copy) rather than deleting it
  # outright shrinks the window in which runtime/sfpi doesn't exist to the
  # time between two renames, instead of the time a `rm -rf` of a ~435MB
  # tree takes — and if this process is killed in that narrow window, the
  # old copy is still recoverable at its ".old" name rather than gone.
  local old_aside="${sfpi_dir}.old.$$"
  if [[ -d "$sfpi_dir" ]]; then
    mv "$sfpi_dir" "$old_aside" || die "venv-runner: could not move aside the existing ${sfpi_dir} to install the verified replacement"
  fi
  if ! mv "${staging_parent}/sfpi" "$sfpi_dir"; then
    # Put the old one back rather than leave neither in place.
    [[ -d "$old_aside" ]] && mv "$old_aside" "$sfpi_dir"
    safe_rm_rf "$staging_parent"
    die "venv-runner: could not move the verified SFPI install into place at ${sfpi_dir}"
  fi
  safe_rm_rf "$old_aside"
  safe_rm_rf "$staging_parent"

  log "venv-runner: SFPI ${sfpi_version} installed to ${sfpi_dir}"
}

create_runner_venv() {
  if [[ "$SKIP_RUNNER" -eq 1 ]]; then
    log "venv-runner: --skip-runner given, not touching it"
    RUNNER_STATUS="skipped (--skip-runner)"
    TEST_DEPS_STATUS="skipped (--skip-runner)"
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
        ensure_test_deps_installed "$VENV_RUNNER"
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

  # THE [tenstorrent] EXTRA IS LOAD-BEARING as of tt-bio 0.6.4. Up to 0.6.3 ttnn
  # was a base dependency; 0.6.4 moved it into an optional extra so the CPU/GPU
  # path and the CLI install without a Tenstorrent SDK (upstream issue #6).
  # A plain `pip install tt-bio==<version>` therefore installs NO ttnn (still
  # true at 0.7.0, where ttnn==0.68.0 remains behind the extra), and this
  # script fails a few steps later at ensure_sfpi_installed with "no ttnn
  # package found" -- which is how we found it. Without the extra the venv is
  # useless to the booth: no ttnn, no device.
  log "venv-runner: installing tt-bio[tenstorrent]==${TT_BIO_VERSION} — this pulls torch + ttnn and is expected to be multiple GB and slow"
  local t0 t1
  t0=$(date +%s)
  if "${VENV_RUNNER}/bin/python3" -m pip install "tt-bio[tenstorrent]==${TT_BIO_VERSION}"; then
    t1=$(date +%s)
    RUNNER_INSTALL_SECONDS=$((t1 - t0))
    RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)" || true
    log "venv-runner: pip install finished in ${RUNNER_INSTALL_SECONDS}s, venv is now ${RUNNER_SIZE}"
  else
    t1=$(date +%s)
    RUNNER_INSTALL_SECONDS=$((t1 - t0))
    RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)" || true
    die "venv-runner: 'pip install tt-bio[tenstorrent]==${TT_BIO_VERSION}' FAILED after ${RUNNER_INSTALL_SECONDS}s (venv left at $VENV_RUNNER, ${RUNNER_SIZE}, for inspection). Not retrying automatically — see pip's own error above."
  fi

  ensure_sfpi_installed "$VENV_RUNNER"
  RUNNER_SIZE="$(du -sh "$VENV_RUNNER" 2>/dev/null | cut -f1)" || true   # SFPI adds ~435M; refresh the reported size
  ensure_test_deps_installed "$VENV_RUNNER"

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
# 4c. The model weights
# ---------------------------------------------------------------------------

WEIGHTS_STATUS="not attempted"

# Where the weights live, derived exactly as tt-bio derives it: $TT_BIO_CACHE,
# then $BOLTZ_CACHE, then ~/.boltz. Reported in the summary so an operator can
# see WHICH directory was filled -- this project had four callers deriving
# that path four different ways, and none of them read $TT_BIO_CACHE.
# runner/env.py's weights_cache() and scripts/doctor.sh's
# doctor_weights_cache() are the same rule; all three are pinned to tt-bio's
# own cache_root() by tests.
#
# `:-` and not `-`: an EMPTY variable falls through, because `TT_BIO_CACHE=`
# is what an exported-but-unset variable looks like in a unit file.
# shellcheck source=weights-cache.sh
. "${SCRIPT_DIR}/weights-cache.sh"

weights_cache_dir() {
  tt_bio_demo_weights_cache
}

# The 3.7 GB the booth cannot fold without: the protenix-v2 checkpoint and the
# CCD molecule library it loads alongside.
#
# WHY THIS IS HERE AT ALL. Before it, a source install built both venvs and
# stopped. The .deb had covered weights since Phase 3b (debian/
# tt-bio-demo-weights.postinst, behind a debconf question); a git checkout had
# nothing, so the first fold either pulled gigabytes silently or, at a venue,
# failed. A user hit exactly that and reported it: "I had to discover a model
# downloading command".
#
# WHY IT SHELLS OUT TO `tt-bio weights --download` rather than importing
# tt_bio.weights and calling fetch(). It is the same command the docs,
# scripts/doctor.sh and the README all tell an operator to run, so there is
# one command to keep true instead of two things that can disagree about what
# "fetch the weights" means. tt-bio resolves its own cache ($TT_BIO_CACHE,
# then $BOLTZ_CACHE, then ~/.boltz — see runner/env.py's weights_cache), and
# re-running is cheap: it verifies rather than re-downloading.
#
# WHY A FAILURE HERE IS NOT FATAL. The venvs above are the expensive,
# hard-to-redo part and they are fine; what failed is a resumable download
# over what may be a conference-hotel connection. Turning that into exit 1
# would throw away a good bootstrap and tell the operator nothing they can
# act on. So: warn, print the command, carry on.
fetch_weights() {
  local tt_bio="${VENV_RUNNER}/bin/tt-bio"
  local cmd="${tt_bio} weights --download protenix-v2"

  if [[ "$SKIP_RUNNER" -eq 1 ]]; then
    # Nothing to fetch WITH: the fetch runs through venv-runner's own tt-bio.
    log "weights: --skip-runner given, not fetching (needs venv-runner)"
    WEIGHTS_STATUS="skipped (--skip-runner)"
    return 0
  fi
  if [[ "$SKIP_WEIGHTS" -eq 1 ]]; then
    log "weights: --skip-weights given, not fetching"
    log "weights: the booth cannot fold until they are present:"
    log "weights:     ${cmd}"
    WEIGHTS_STATUS="skipped (--skip-weights) — the booth cannot fold yet"
    return 0
  fi
  if [[ ! -x "$tt_bio" ]]; then
    # venv-runner is absent or degraded (this script's own exit 2). Say what
    # to run once it is fixed rather than dying on top of an existing fault.
    warn "weights: no usable tt-bio at ${tt_bio}; not fetching"
    warn "weights: once venv-runner works, fetch them with:"
    warn "weights:     ${cmd}"
    WEIGHTS_STATUS="not fetched (no usable venv-runner)"
    return 0
  fi

  log "weights: fetching the protenix-v2 checkpoint and CCD molecule library"
  log "weights: ~3.7 GB, resumable, verified — a no-op if already present"
  if "$tt_bio" weights --download protenix-v2; then
    WEIGHTS_STATUS="present and verified"
    log "weights: present and verified — the booth can fold offline"
  else
    warn "weights: the download did not complete. The venvs above are fine;"
    warn "weights: this is resumable. Re-run this script, or directly:"
    warn "weights:     ${cmd}"
    WEIGHTS_STATUS="INCOMPLETE — re-run; the booth cannot fold yet"
  fi
  return 0
}

# Sourced with SETUP_VENVS_LIB_ONLY=1 by tests/unit/test_setup_venvs_weights.py,
# which calls the functions above directly — the same arrangement doctor.sh
# uses with DOCTOR_LIB_ONLY.
#
# This guard is not a nicety. Without it, merely SOURCING this file builds both
# venvs: the first draft of that test file did exactly that and pip-installed
# torch into five pytest tmp directories — 30 GB — on a box already at 100%
# disk. `return` when sourced, `exit` when somebody runs it with the variable
# set by accident.
if [[ -n "${SETUP_VENVS_LIB_ONLY:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi

# ---------------------------------------------------------------------------
# 5. Run it
# ---------------------------------------------------------------------------

create_ui_venv
create_runner_venv
fetch_weights

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
echo "  test deps:   $TEST_DEPS_STATUS"
echo
echo "weights:       $(weights_cache_dir)"
echo "  status:      $WEIGHTS_STATUS"
echo "  fetch/check: ${VENV_RUNNER}/bin/tt-bio weights --download protenix-v2"
if [[ "$DEV" -eq 1 ]]; then
  echo "  run tests:   ${VENV_RUNNER}/bin/python3 -m pytest tests/unit/test_runner_env.py -v"
fi
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
