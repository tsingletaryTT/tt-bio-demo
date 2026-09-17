#!/usr/bin/env bash
# tt-bio-demo-daemon-launcher.sh -- what debian/tt-bio-demo.user.service's
# ExecStart= actually runs, instead of invoking runner.daemon directly.
#
# WHY THIS EXISTS. A systemd unit's `Environment=NAME=value` line always
# wins over whatever environment the user manager would otherwise hand the
# service (a `~/.config/environment.d/*.conf` generator, `systemctl --user
# import-environment`, `systemctl --user set-environment`) -- there is no
# way to spell "only if the caller hasn't set this already" in a static
# `Environment=` line. That is backwards for this project's own standing
# rule that an operator's explicit $TT_BIO_CACHE/$BOLTZ_CACHE always wins:
# every OTHER caller of scripts/weights-cache.sh applies the pin through a
# GUARDED function (tt_bio_demo_weights_cache_impl_packaged only assigns
# when NEITHER variable is already set) -- the unit used to be the one
# exception, pinning unconditionally via a literal `Environment=
# TT_BIO_CACHE=/opt/tt-bio-demo/weights` line. See docs/followups.md's "the
# systemd unit's Environment= is unconditional" entry (FIXED) for the full
# history, including why an operator's override needs
# `systemctl --user edit tt-bio-demo` to reach here at all (a `.service`
# file's own `Environment=` is baked in at unit-file level, not read fresh
# per start -- this script is what makes the READ happen at start time,
# where a real check can run).
#
# Started as an ordinary process by ExecStart=, this script inherits
# whatever environment systemd actually assembled for it -- including any
# operator override the unit's own static directives cannot see either.
# It sources the SAME guarded resolver every other packaged caller uses
# (scripts/doctor.sh, debian/tt-bio-demo-weights.postinst), so the guard
# sees a real operator override exactly the way those do, then `exec`s the
# real daemon -- replacing this shell's process image rather than adding a
# supervision layer, so systemd's `Restart=`/`TimeoutStopSec=`/signal
# delivery all still talk to the actual daemon PID, not a wrapper sitting
# in front of it.
#
# Takes the socket and log-root paths as ARGUMENTS rather than deriving
# them, because systemd's `%t` specifier (the runtime directory) is only
# ever expanded inside the unit file's own text -- a script's own $XDG_
# RUNTIME_DIR would usually agree, but "usually" is not the guarantee `%t`
# is. The unit passes them pre-expanded; see debian/tt-bio-demo.user.service.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    printf 'tt-bio-demo-daemon-launcher.sh: expected <socket> <log-root>, got %d arg(s)\n' "$#" >&2
    exit 1
fi
SOCKET="$1"
LOG_ROOT="$2"

PREFIX="/opt/tt-bio-demo"
# shellcheck source=weights-cache.sh
. "${PREFIX}/scripts/weights-cache.sh"

# Bare top-level call, not inside `$(...)`: the export has to land in THIS
# shell so the `exec` below -- which REPLACES this process image but keeps
# its environment -- hands $TT_BIO_CACHE to the daemon. That is what lets
# nesso1/nesso1-ccd/the ESM-2 encoder ("hf-repo" artifacts that silently
# ignore --weights entirely -- see scripts/weights-cache.sh's own comment on
# TT_BIO_DEMO_PACKAGED_WEIGHTS_CACHE) resolve to the same fixed cache the
# postinst populated, not only the flat protenix-v2.pt/mols/ pair which
# --weights below covers on its own.
tt_bio_demo_weights_cache_packaged >/dev/null
WEIGHTS="$(tt_bio_demo_weights_cache_packaged)"

# `${PREFIX}/playlist` (what `--playlist` used to name directly) ships ONLY
# manifest.yaml/questions.yaml -- debian/tt-bio-demo.install puts the real
# fold-input YAMLs in the SIBLING `${PREFIX}/examples`, which manifest.yaml's
# own `input:` entries point `../examples/...` into. Globbing that directory
# for fold targets (runner/daemon.py's `_playlist_files()`) therefore always
# found the two metadata files and nothing else -- and once those two are
# excluded by name (`_NON_FOLD_PLAYLIST_FILENAMES`), a packaged/systemd booth
# enumerated ZERO fold targets and idled forever. (PR review, Copilot.)
#
# The fix: materialize the same per-target symlink farm run-demo.sh already
# builds for the source/dev path, into a RUNTIME directory this service can
# actually write to -- `${PREFIX}/playlist` is root-owned at install time,
# and an unprivileged `systemd --user` service has no business writing
# symlinks into it even where permissions happen to allow it. `%t` (this
# script's own `${LOG_ROOT}` argument's parent) is the same runtime
# directory the socket and logs already live under.
# shellcheck source=materialize-playlist.sh
. "${PREFIX}/scripts/materialize-playlist.sh"
RUNTIME_PLAYLIST_DIR="$(dirname "${LOG_ROOT}")/playlist"
mkdir -p "${RUNTIME_PLAYLIST_DIR}"
tt_bio_demo_materialize_playlist \
    "${PREFIX}/.venvs/venv-ui/bin/python3" "${PREFIX}/playlist/manifest.yaml" \
    "" "${RUNTIME_PLAYLIST_DIR}"

exec "${PREFIX}/.venvs/venv-runner/bin/python3" -m runner.daemon \
    --socket "${SOCKET}" \
    --weights "${WEIGHTS}" \
    --playlist "${RUNTIME_PLAYLIST_DIR}" \
    --log-root "${LOG_ROOT}" \
    --log-budget-gb 2.0 \
    --structures-budget-gb 0.2
