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

# `:-` and not `-` at every level: an EMPTY variable falls through to the next
# one. Not pedantry -- `TT_BIO_CACHE=` is what an exported-but-unset variable
# looks like in a systemd unit or a sourced env file, and treating it as a
# path resolves the cache to whatever directory the caller happened to run
# from.
tt_bio_demo_weights_cache() {
    printf '%s\n' "${TT_BIO_CACHE:-${BOLTZ_CACHE:-${HOME:-/root}/.boltz}}"
}
