#!/usr/bin/env bash
# The ONE shell answer to "where do the model weights live".
#
# Sourced, never executed: scripts/doctor.sh, scripts/setup-venvs.sh,
# scripts/run-demo.sh and (via debian/helpers.sh) the weights postinst all
# source this so there is exactly one derivation of the path in shell, and
# runner/env.py's weights_cache() is the same rule in python.
#
# WHY THIS FILE EXISTS. Four callers used to derive it independently:
#
#     runner/folder.py              Path.home() / ".boltz"
#     scripts/run-demo.sh           ${TT_BIO_DEMO_WEIGHTS:-$HOME/.boltz}
#     scripts/doctor.sh             ${BOLTZ_CACHE:-$HOME/.boltz}
#     debian/..weights.postinst     ${BOLTZ_CACHE:-$HOME/.boltz}
#
# and NONE of them read $TT_BIO_CACHE, the variable tt-bio itself prefers and
# documents as relocating the whole cache. All four agree on ~/.boltz when
# nothing is set, which is precisely why it survived: the disagreement only
# shows up once an operator moves the cache, and then it shows up as the
# doctor pronouncing a booth healthy while a fold loads from an empty
# directory. tests/unit/test_weights_cache_is_derived_once.py is the guard
# against a fifth one appearing.
#
# THE ORDER IS TT-BIO'S, not ours -- $TT_BIO_CACHE, then $BOLTZ_CACHE, then
# ~/.boltz (tt_bio.weights.cache_root). Tests on both the python and shell
# sides pin this against that function, so an upstream change to the
# precedence breaks the suite rather than a venue.

# The home directory, the way python's Path.home() finds it -- $HOME, and
# failing that the PASSWD DATABASE.
#
# This said `${HOME:-/root}` and that was wrong: tt_bio.weights.cache_root()
# and runner/env.py both end at Path.home(), which consults passwd when $HOME
# is unset. A systemd unit without HOME, `env -i`, or a cron context would
# therefore have had the doctor checking /root/.boltz while the fold loaded
# the operator's own -- precisely the split this file exists to end, and
# invisible to a test matrix that always sets HOME. One now does not.
_tt_bio_demo_home() {
    if [ -n "${HOME:-}" ]; then
        printf '%s\n' "$HOME"
        return 0
    fi
    _h="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)"
    printf '%s\n' "${_h:-/root}"
}

# `:-` and not `-` at every level: an EMPTY variable falls through to the next
# one. Not pedantry -- `TT_BIO_CACHE=` is what an exported-but-unset variable
# looks like in a systemd unit or a sourced env file, and treating it as a
# path resolves the cache to whatever directory the caller happened to run
# from.
#
# TWO NAMES, deliberately. `_impl` is what debian/helpers.sh calls, and its
# presence is how that wrapper proves this file actually loaded before
# trusting it -- an earlier arrangement relied on this file redefining the
# wrapper's own name, which recursed 1000 frames deep inside a `configure`
# script whenever the file was readable but empty.
tt_bio_demo_weights_cache_impl() {
    printf '%s\n' "${TT_BIO_CACHE:-${BOLTZ_CACHE:-$(_tt_bio_demo_home)/.boltz}}"
}

# The friendly name, for the scripts that source this file directly
# (doctor.sh, setup-venvs.sh, run-demo.sh).
tt_bio_demo_weights_cache() {
    tt_bio_demo_weights_cache_impl
}
