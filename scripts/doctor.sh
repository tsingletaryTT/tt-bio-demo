#!/usr/bin/env bash
# doctor.sh -- is this machine ready to run the booth, and if not, what is
# missing and what is the exact command that fixes it?
#
#   scripts/doctor.sh              # check everything, change nothing
#   scripts/doctor.sh --fix        # also perform the SAFE repairs
#   scripts/doctor.sh --quiet      # only the summary and anything wrong
#
# WORKS FROM EITHER INSTALL. A git checkout and an /opt/tt-bio-demo installed
# from the .deb are the same booth with the files in different places, and an
# operator at a venue should not have to know which one they are looking at.
# `doctor_prefix` finds the tree; every check works against that.
#
# RESUMABLE BY CONSTRUCTION. Every check is independent and every repair is
# idempotent, so this can be run repeatedly as things get fixed and it will
# pick up where the machine actually is rather than where it was. Nothing
# here has to be run in order.
#
# WHAT IT WILL NOT DO, EVER -- these are the project's standing rules, and
# this is the script most tempted to break them because it is the one whose
# job is "make it work":
#
#   * never `tt-smi -r` (or any card reset). A reset on a shared box takes
#     out whatever someone else is running.
#   * never `tt-bio install-deps`. It installs kernel modules; consent is a
#     debconf prompt or a human typing it, not a repair tool deciding.
#   * never `apt install`, `dpkg -i`, or touch kernel modules.
#   * never open a Tenstorrent device. Hardware presence is read from
#     `tt-smi -s` (a read-only snapshot), so running this while somebody else
#     is training is safe.
#
# Exit codes, which are the point of the WARN/FAIL split:
#   0  ready
#   1  something is BROKEN -- the booth cannot fold
#   2  usable, with warnings -- e.g. no hardware attached (fine for UI work)

set -uo pipefail

EXIT_OK=0
EXIT_FAIL=1
EXIT_WARN=2

# ── where things are ────────────────────────────────────────────────────────

# The booth's root, whichever way it was installed.
#
# Order matters: an explicit override, then the directory this script lives
# in (which is right for BOTH a checkout and /opt/tt-bio-demo/scripts/), then
# the packaged location as a last resort for a copy of this script that was
# moved somewhere else.
doctor_prefix() {
    if [ -n "${TT_BIO_DEMO_PREFIX:-}" ]; then
        printf '%s\n' "${TT_BIO_DEMO_PREFIX%/}"
        return 0
    fi
    _self="${BASH_SOURCE[0]:-$0}"
    _dir="$(cd "$(dirname "$_self")/.." 2>/dev/null && pwd)"
    if [ -n "$_dir" ] && [ -d "$_dir/ui" ]; then
        printf '%s\n' "$_dir"
        return 0
    fi
    printf '%s\n' "/opt/tt-bio-demo"
}

# "source" or "package" -- which changes the ADVICE, not the checks. A source
# checkout is told to run scripts/setup-venvs.sh; a packaged install is told
# to use dpkg-reconfigure, because that is where its debconf answers live.
doctor_install_mode() {
    _p="$(doctor_prefix)"
    if [ -d "$_p/.git" ] || [ -d "$_p/tests" ]; then
        printf 'source\n'
    else
        printf 'package\n'
    fi
}

# Where the weights live, derived exactly as tt-bio derives it:
# $TT_BIO_CACHE, then $BOLTZ_CACHE, then ~/.boltz. This used to read only
# $BOLTZ_CACHE, so on a booth whose cache had been relocated with the variable
# tt-bio actually documents, the check that exists to find missing weights
# looked in the directory the operator had moved away from -- reporting a
# working booth as broken, or a broken one as fine, depending which way round
# it was. runner/env.py's weights_cache() is the same rule in python; both
# sides are pinned to tt-bio's own cache_root() by tests.
#
# `:-` and not `-`: an EMPTY variable falls through to the next one. That is
# not pedantry -- `TT_BIO_CACHE=` is what an exported-but-unset variable looks
# like in a systemd unit or a sourced env file, and treating it as a path
# resolves the cache to whatever directory the doctor happened to run from.
#
# The rule itself lives in scripts/weights-cache.sh so the shell scripts and
# the postinst cannot drift apart. Sourced relative to THIS script, which is
# right for both a checkout and /opt/tt-bio-demo/scripts/.
# shellcheck source=weights-cache.sh
. "$(dirname "${BASH_SOURCE[0]:-$0}")/weights-cache.sh"

doctor_weights_cache() {
    tt_bio_demo_weights_cache
}

# ── reporting ───────────────────────────────────────────────────────────────
#
# Left-side bars only: a right-hand border breaks the moment the terminal is
# narrower than the author assumed, and a venue laptop is exactly where that
# happens.

_FAILURES=0
_WARNINGS=0
_QUIET=0

say()  { [ "$_QUIET" = "1" ] || printf '%s\n' "$*"; }
head_() { [ "$_QUIET" = "1" ] || { printf '\n╔══════════════════════════════════════════════\n'; printf '║  %s\n' "$*"; printf '╚══════════════════════════════════════════════\n'; }; }
ok()   { [ "$_QUIET" = "1" ] || printf '  [ ok ]  %s\n' "$*"; }
warn() { _WARNINGS=$((_WARNINGS + 1)); printf '  [warn]  %s\n' "$*"; }
fail() { _FAILURES=$((_FAILURES + 1)); printf '  [FAIL]  %s\n' "$*"; }
hint() { printf '          -> %s\n' "$*"; }

# ── the checks ──────────────────────────────────────────────────────────────
#
# Each is a function returning 0/non-zero so it can be called directly by a
# test. None of them exits; the caller decides what a failure means.

doctor_check_layout() {
    _p="$(doctor_prefix)"
    _rc=0
    for d in ui runner protocol playlist examples scripts; do
        if [ -d "$_p/$d" ]; then
            ok "$d/"
        else
            fail "$d/ is missing from $_p"
            _rc=1
        fi
    done
    # The vendored Tensix animation: the venue is offline, so a CDN reference
    # would render an empty panel with no error anyone would notice.
    if [ -f "$_p/ui/assets/tensix-viz/tensix-viz.js" ]; then
        ok "vendored tensix-viz asset"
    else
        warn "ui/assets/tensix-viz/tensix-viz.js is missing"
        hint "the Tensix panel (T) will be blank; everything else still works"
    fi
    return $_rc
}

doctor_check_venvs() {
    _p="$(doctor_prefix)"
    _rc=0
    for v in venv-ui venv-runner; do
        if [ -x "$_p/.venvs/$v/bin/python3" ]; then
            ok "$v"
        else
            fail "$v is not built"
            _rc=1
        fi
    done
    if [ "$_rc" != "0" ]; then
        hint "sudo $_p/scripts/setup-venvs.sh --prefix $_p"
        hint "(downloads gigabytes and takes minutes; safe to re-run)"
    fi
    return $_rc
}

# What the two venvs must actually be able to import. A venv that exists but
# cannot import ttnn is the state setup-venvs.sh calls exit 2, and it looks
# completely healthy to a directory check.
doctor_check_imports() {
    _p="$(doctor_prefix)"
    _rc=0
    _ui="$_p/.venvs/venv-ui/bin/python3"
    _rn="$_p/.venvs/venv-runner/bin/python3"

    if [ -x "$_ui" ]; then
        if "$_ui" -c 'import gi; gi.require_version("Gtk","4.0"); from gi.repository import Gtk; import gemmi, OpenGL' 2>/dev/null; then
            ok "venv-ui imports GTK4, gemmi, PyOpenGL"
        else
            fail "venv-ui cannot import its own stack"
            hint "sudo $_p/scripts/setup-venvs.sh --prefix $_p --force"
            _rc=1
        fi
    fi
    if [ -x "$_rn" ]; then
        # NO DEVICE IS OPENED. Importing ttnn does not claim a chip; this is
        # deliberately not a device probe, so the doctor stays safe to run
        # while somebody else is using the hardware.
        if "$_rn" -c 'import torch, ttnn, tt_bio' 2>/dev/null; then
            ok "venv-runner imports torch, ttnn, tt_bio"
        else
            fail "venv-runner cannot import torch/ttnn/tt_bio"
            hint "this is setup-venvs.sh's exit-2 state: built, but broken"
            hint "sudo $_p/scripts/setup-venvs.sh --prefix $_p --force"
            _rc=1
        fi
    fi
    return $_rc
}

# The pin, and whether what is installed matches it.
doctor_check_tt_bio_version() {
    _p="$(doctor_prefix)"
    _rn="$_p/.venvs/venv-runner/bin/python3"
    _setup="$_p/scripts/setup-venvs.sh"
    [ -f "$_setup" ] || { warn "setup-venvs.sh not found; cannot read the tt-bio pin"; return 0; }
    _pin="$(sed -n 's/^TT_BIO_VERSION="\([^"]*\)".*/\1/p' "$_setup" | head -n 1)"
    [ -n "$_pin" ] || { warn "setup-venvs.sh no longer declares TT_BIO_VERSION"; return 0; }
    [ -x "$_rn" ] || return 0
    _have="$("$_rn" -c 'import importlib.metadata as m; print(m.version("tt-bio"))' 2>/dev/null)"
    if [ -z "$_have" ]; then
        warn "tt-bio is not installed in venv-runner (pin says $_pin)"
        return 0
    fi
    if [ "$_have" = "$_pin" ]; then
        ok "tt-bio $_have matches the pin"
    else
        warn "tt-bio $_have installed, but the pin says $_pin"
        hint "sudo $_p/scripts/setup-venvs.sh --prefix $_p --force"
    fi
    return 0
}

# Weights: present, and NOT TRUNCATED.
#
# TWO LAYERS, and which one runs is decided by whether tt-bio can be asked.
#
#   1. TT-BIO'S OWN VERIFIER, whenever venv-runner exists. AUTHORITATIVE, and
#      the only layer that can be right about two things the filesystem cannot
#      see: a file of the correct size that is nevertheless a corrupt archive,
#      and a single artifact deliberately RELOCATED with tt-bio's per-artifact
#      overrides ($PROTENIX_CKPT / $TT_BIO_PROTENIX_V2, $TT_BIO_MOLS). It
#      reports where it actually found each one, so the doctor names the real
#      path rather than the one it assumed.
#      NO DEVICE IS OPENED and torch is never imported -- tt_bio.weights pulls
#      in os/shutil/pathlib and nothing else, measured at 0.13 s -- so this
#      stays safe to run while somebody else has the cards.
#
#   2. FILESYSTEM, as the fallback. The only thing available before
#      venv-runner is built, which is exactly when an operator most needs this
#      to say something useful. Size and not existence for the checkpoint,
#      because the realistic failure is an interrupted download that left a
#      short file and an existence check calls that healthy. For the molecule
#      library it is the EXTRACTED directory that matters, not the mols.tar it
#      came from -- tt-bio discards that archive once unpacked and `tt-bio
#      weights --prune` deletes it, so requiring the tar failed booths that
#      could fold perfectly well.
#
# Layer 1 REPLACES layer 2 rather than adding to it. An earlier version ran
# both and let layer 2 only ever escalate, so a relocated artifact was
# reported missing from a cache it had deliberately been moved out of -- the
# same false alarm as the mols.tar one, one layer up.
doctor_ask_tt_bio_about_weights() {
    # Prints "<key> <state> <path>" per artifact, or nothing at all if it
    # cannot ask. A failure to ask is swallowed on purpose: an unaskable
    # tt-bio is not itself a weights fault, and the caller falls back.
    _rn="$1"
    _cache="$2"
    [ -x "$_rn" ] || return 1
    "$_rn" - "$_cache" <<'TT_BIO_STATUS_EOF' 2>/dev/null
import sys
try:
    from tt_bio import weights
except Exception:
    raise SystemExit(1)
root = sys.argv[1]
for art in weights.artifacts_for("protenix-v2"):
    st = weights.status(art.key, root)
    # resolve() honours the per-artifact overrides that status()'s own path
    # does not for a derived row, so an operator who moved just the molecule
    # library still sees where it really is.
    path = st.path or weights.resolve(art.key, root)
    print(f"{art.key} {st.state} {path}")
TT_BIO_STATUS_EOF
}

# The fallback. Split out so the two layers are separately readable and
# separately testable, rather than one function with a mode flag.
doctor_check_weights_from_the_filesystem() {
    _c="$1"
    _rc=0

    # The real checkpoint is 1.86 GB; 1 GB is a floor no truncation this
    # matters for would pass.
    _ckpt="$_c/protenix-v2.pt"
    if [ ! -f "$_ckpt" ]; then
        fail "protenix-v2.pt is missing from $_c"
        _rc=1
    else
        _sz=$(stat -c %s "$_ckpt" 2>/dev/null || echo 0)
        if [ "$_sz" -lt 1000000000 ]; then
            fail "protenix-v2.pt is only $_sz bytes -- truncated download"
            _rc=1
        else
            ok "protenix-v2.pt ($((_sz / 1000000)) MB)"
        fi
    fi

    _mols="$_c/mols"
    if [ -d "$_mols" ] && [ -n "$(ls -A "$_mols" 2>/dev/null)" ]; then
        ok "mols/ (CCD molecule library, unpacked)"
    elif [ -f "$_c/mols.tar" ]; then
        fail "mols.tar is present but was never unpacked to $_mols"
        hint "the first fold would unpack it; the doctor says so here because"
        hint "a venue is the wrong place to discover a 20-second delay"
        _rc=1
    else
        fail "the CCD molecule library is missing from $_c"
        _rc=1
    fi
    return $_rc
}

doctor_check_weights() {
    _p="$(doctor_prefix)"
    _c="$(doctor_weights_cache)"
    _rc=0

    _verdicts="$(doctor_ask_tt_bio_about_weights \
                     "$_p/.venvs/venv-runner/bin/python3" "$_c")"

    if [ -n "$_verdicts" ]; then
        while read -r _key _state _path; do
            [ -n "$_key" ] || continue
            case "$_state" in
                present)
                    ok "$_key ($_path)"
                    ;;
                corrupt|partial)
                    fail "$_key is $_state at $_path -- tt-bio cannot load it"
                    hint "a file of the right size can still be a bad archive"
                    _rc=1
                    ;;
                *)
                    fail "$_key is missing ($_path)"
                    _rc=1
                    ;;
            esac
        done <<VERDICT_EOF
$_verdicts
VERDICT_EOF
    else
        doctor_check_weights_from_the_filesystem "$_c" || _rc=1
    fi

    if [ "$_rc" != "0" ]; then
        # THE COMMAND, always. This hint used to read "they download on the
        # first fold, or fetch them ahead of time" and named nothing runnable,
        # so a user had to go and discover `tt-bio weights --download` for
        # themselves -- and reported it. A hint without a command is not a
        # hint.
        if [ "$(doctor_install_mode)" = "package" ]; then
            hint "sudo dpkg-reconfigure tt-bio-demo-weights"
            hint "or, directly:"
        else
            hint "fetch them with:"
        fi
        hint "$_p/.venvs/venv-runner/bin/tt-bio weights --download protenix-v2"
        hint "about 3.7 GB, resumable, and THE VENUE IS OFFLINE -- do it first"
    fi
    return $_rc
}

# Every manifest entry must name an input file that exists. The failure this
# catches is a booth that runs fine for two targets and dies on the third,
# in front of people.
doctor_check_playlist() {
    _p="$(doctor_prefix)"
    _m="$_p/playlist/manifest.yaml"
    if [ ! -f "$_m" ]; then
        fail "playlist/manifest.yaml is missing"
        return 1
    fi
    # Parsed with the UI venv's python when it exists (it has yaml), else with
    # a deliberately small sed. The sed path is not a general YAML parser and
    # does not pretend to be -- it reads the one `input:` key this checks.
    _py="$_p/.venvs/venv-ui/bin/python3"
    _missing=""
    if [ -x "$_py" ] && "$_py" -c 'import yaml' 2>/dev/null; then
        _missing="$("$_py" - "$_m" <<'PYEOF' 2>/dev/null
import pathlib, sys, yaml
m = pathlib.Path(sys.argv[1])
entries = yaml.safe_load(m.read_text()) or []
for e in entries:
    p = (m.parent / e["input"]).resolve()
    if not p.exists():
        print(f'{e.get("id","?")}:{p}')
PYEOF
)"
    else
        while IFS= read -r rel; do
            [ -n "$rel" ] || continue
            case "$rel" in
                /*) _abs="$rel" ;;
                *)  _abs="$_p/playlist/$rel" ;;
            esac
            [ -f "$_abs" ] || _missing="${_missing}?:${_abs}
"
        done <<EOF
$(sed -n 's/^[[:space:]]*input:[[:space:]]*//p' "$_m")
EOF
    fi
    if [ -n "$_missing" ]; then
        printf '%s\n' "$_missing" | while IFS= read -r line; do
            [ -n "$line" ] && fail "playlist input missing: ${line#*:}"
        done
        hint "a manifest entry whose input is absent kills the booth mid-loop"
        return 1
    fi
    ok "every playlist input exists"
    return 0
}

# Hardware, READ ONLY. `tt-smi -s` is a snapshot; it does not open a device,
# so this is safe to run while someone else has the cards.
doctor_check_hardware() {
    if ! command -v tt-smi >/dev/null 2>&1; then
        warn "tt-smi not found -- cannot see whether any chips are attached"
        hint "the UI still runs without hardware; folding does not"
        return 0
    fi
    _snap="$(tt-smi -s 2>/dev/null)"
    _n=$(printf '%s' "$_snap" | grep -c '"board_type"' 2>/dev/null || echo 0)
    if [ "${_n:-0}" -gt 0 ]; then
        ok "$_n Tenstorrent chip(s) visible"
    else
        warn "no Tenstorrent chips visible"
        hint "fine for UI work; the booth cannot fold without them"
    fi
    return 0
}

# Somewhere to put logs, and enough room for the weights.
doctor_check_space() {
    _c="$(doctor_weights_cache)"
    _dir="$_c"
    [ -d "$_dir" ] || _dir="$(dirname "$_c")"
    _avail=$(df -Pk "$_dir" 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -z "$_avail" ]; then
        warn "cannot determine free space at $_dir"
        return 0
    fi
    _gb=$((_avail / 1000000))
    # 3.7 GB of weights, plus tt-metal's own log churn (bounded by the
    # daemon's budgets, but it still needs somewhere to churn).
    if [ "$_gb" -lt 8 ]; then
        warn "only ${_gb} GB free at $_dir (weights alone are 3.7 GB)"
    else
        ok "${_gb} GB free at $_dir"
    fi
    return 0
}

# A packaged install should have its user unit; a source checkout has none
# and that is not a defect.
doctor_check_service() {
    [ "$(doctor_install_mode)" = "package" ] || return 0
    if [ -f /usr/lib/systemd/user/tt-bio-demo.service ]; then
        ok "systemd --user unit installed"
    else
        warn "no systemd --user unit at /usr/lib/systemd/user/tt-bio-demo.service"
        hint "run-demo.sh still works; the unit is for unattended operation"
    fi
    if [ -f /usr/lib/systemd/system/tt-bio-demo.service ]; then
        fail "a SYSTEM unit is installed; this booth is a --user service"
        hint "a system unit cannot reach the user's runtime socket"
        return 1
    fi
    return 0
}

# A display to draw on. Checked last because it is the one thing that is
# routinely absent in a terminal session and routinely fine.
doctor_check_display() {
    if [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; then
        ok "a display is available"
    else
        warn "no DISPLAY or WAYLAND_DISPLAY -- the UI cannot open a window here"
        hint "expected over plain ssh; run from the desktop session"
    fi
    return 0
}

# ── main ────────────────────────────────────────────────────────────────────

doctor_main() {
    _fix=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --fix)   _fix=1; shift ;;
            --quiet) _QUIET=1; shift ;;
            -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]:-$0}"; return 0 ;;
            *) printf 'doctor.sh: unknown argument: %s\n' "$1" >&2; return 1 ;;
        esac
    done

    _p="$(doctor_prefix)"
    _mode="$(doctor_install_mode)"
    head_ "tt-bio-demo doctor"
    say "  prefix:  $_p"
    say "  mode:    $_mode install"
    say "  weights: $(doctor_weights_cache)"

    head_ "application tree";      doctor_check_layout
    head_ "virtual environments";  doctor_check_venvs && doctor_check_imports
    head_ "tt-bio version";        doctor_check_tt_bio_version
    head_ "model weights";         doctor_check_weights
    head_ "playlist";              doctor_check_playlist
    head_ "hardware";              doctor_check_hardware
    head_ "disk";                  doctor_check_space
    head_ "service";               doctor_check_service
    head_ "display";               doctor_check_display

    # --fix does the repairs that are SAFE and LOCAL. Everything that touches
    # the system (venvs, weights, kernel modules) is printed rather than run,
    # because those are the ones an operator must be able to decide about --
    # and because this box is shared.
    if [ "$_fix" = "1" ]; then
        head_ "--fix"
        _c="$(doctor_weights_cache)"
        if [ ! -d "$_c" ]; then
            mkdir -p "$_c" && ok "created $_c"
        fi
        say "  nothing else is repaired automatically: building venvs and"
        say "  downloading weights are large, networked, and this box may be"
        say "  shared. The exact commands are printed above."
    fi

    head_ "summary"
    if [ "$_FAILURES" -gt 0 ]; then
        printf '  %d failure(s), %d warning(s) -- the booth is NOT ready.\n' \
            "$_FAILURES" "$_WARNINGS"
        return $EXIT_FAIL
    fi
    if [ "$_WARNINGS" -gt 0 ]; then
        printf '  0 failures, %d warning(s) -- usable, with caveats above.\n' "$_WARNINGS"
        return $EXIT_WARN
    fi
    say "  everything checks out. The booth is ready."
    return $EXIT_OK
}

# Sourced with DOCTOR_LIB_ONLY=1 by the tests, which call the functions
# directly. Without it, running this file runs the checks.
if [ -z "${DOCTOR_LIB_ONLY:-}" ]; then
    doctor_main "$@"
    exit $?
fi
