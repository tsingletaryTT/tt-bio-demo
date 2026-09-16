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

# ---------------------------------------------------------------------------
# "source" or "package" -- shared so scripts/doctor.sh and scripts/run-demo.sh
# do not each carry their own copy of the same .git/tests sniff test. Before
# this existed, doctor.sh had its own doctor_install_mode (calling this) and
# run-demo.sh had NO equivalent at all -- it always used the plain,
# home-relative resolver, even from a real packaged install. See docs/
# followups.md's "run-demo.sh resolved home-relative even from a packaged
# install" entry (FIXED) for the full history: a `.deb`'s desktop entry runs
# `/opt/tt-bio-demo/scripts/run-demo.sh` directly (INSTALL.md calls this "the
# normal path"), so a packaged booth's postinst fetched weights to the fixed
# path below while its actual launcher kept resolving the desktop user's own
# $HOME -- the postinst/unit/doctor triangle agreed with each other and
# disagreed with the one script an operator actually runs.
#
# Takes the CALLER's own notion of the application-tree root as $1, rather
# than computing one itself, because the two callers already have two
# slightly different ways of finding it: doctor.sh's doctor_prefix() (which
# honours an explicit $TT_BIO_DEMO_PREFIX override naming the WHOLE app
# tree) and run-demo.sh's $REPO_ROOT (always the checkout this script itself
# lives in -- unaffected by run-demo.sh's OWN $TT_BIO_DEMO_PREFIX, which
# means something different there: where the venvs live, not the app tree).
# Both resolve to the identical real directory for an installed
# /opt/tt-bio-demo booth, so passing either one in here answers the same
# question the same way.
tt_bio_demo_install_mode() {
    _prefix="$1"
    if [ -d "$_prefix/.git" ] || [ -d "$_prefix/tests" ]; then
        printf 'source\n'
    else
        printf 'package\n'
    fi
}

# ---------------------------------------------------------------------------
# THE PACKAGED VARIANT. See docs/followups.md's "root's postinst-time HOME vs
# desktop-user's systemd-service-time HOME" entry (FIXED) for the full
# history: debian/tt-bio-demo-weights.postinst runs as ROOT during
# `dpkg`/`apt install` ($HOME=/root); the booth's compute daemon runs later
# as a `systemd --user` service under the DESKTOP USER's own $HOME. Those are
# two different $HOME-relative defaults that do not agree, so a `.deb`
# install could report every weight fetched successfully while the daemon
# that is supposed to use them looked in an empty directory.
#
# The fix is to sidestep $HOME entirely for a packaged deployment: pin
# $TT_BIO_CACHE to one fixed, non-home-relative path, by CALLING this
# function -- never by repeating the literal value -- from every packaged
# caller: the postinst (below), scripts/doctor.sh's diagnosis of an
# installed booth, scripts/run-demo.sh (the packaged install's actual
# operator-facing launcher -- see docs/followups.md's "run-demo.sh resolved
# home-relative even from a packaged install" entry, FIXED), and
# scripts/tt-bio-demo-daemon-launcher.sh (what the systemd unit's
# ExecStart= actually runs -- a static `Environment=` line in the unit
# ITSELF used to repeat the literal value instead, which is what made it
# unconditional; see docs/followups.md's "the systemd unit's Environment=
# is unconditional" entry, FIXED, for why that could not honour an
# operator's own override and had to move into something that could call
# this function instead). tests/unit/test_packaging.py checks that none of
# these callers keep their own copy of the literal path.
#
# This lives HERE, in the resolver, rather than in the postinst or doctor.sh
# themselves, so that $TT_BIO_CACHE/$BOLTZ_CACHE are read in exactly the two
# files tests/unit/test_weights_cache_is_derived_once.py already treats as
# the sole resolvers -- reading either variable directly anywhere else is
# the exact shape of the bug this project has already fixed once (the doctor
# reading $BOLTZ_CACHE alone while tt-bio itself preferred $TT_BIO_CACHE).
TT_BIO_DEMO_PACKAGED_WEIGHTS_CACHE="/opt/tt-bio-demo/weights"

# An operator who has ALREADY set $TT_BIO_CACHE or $BOLTZ_CACHE themselves
# keeps that choice -- this only fills the gap when NEITHER is set, the same
# "explicit always wins over the pin" rule `tt_bio_demo_weights_cache_impl`
# above already applies at every level of its own fallback chain.
#
# `export`, not a plain assignment: every caller of this function (the
# postinst's own shell before it spawns the python fetch block; doctor.sh's
# process before it spawns tt-bio's status-check subprocesses) needs a CHILD
# PROCESS to see this too, and only an exported variable crosses that
# boundary. Called as a plain top-level statement (never wrapped in the
# caller's own `$(...)`), the export lands in the CALLING shell, not merely
# in a subshell that evaporates on exit -- see the callers' own comments for
# why that distinction matters here.
tt_bio_demo_weights_cache_impl_packaged() {
    if [ -z "${TT_BIO_CACHE:-}" ] && [ -z "${BOLTZ_CACHE:-}" ]; then
        TT_BIO_CACHE="$TT_BIO_DEMO_PACKAGED_WEIGHTS_CACHE"
        export TT_BIO_CACHE
    fi
    tt_bio_demo_weights_cache_impl
}

# The friendly name for a caller that already knows it is diagnosing or
# provisioning a PACKAGED install (scripts/doctor.sh, when it is; and, via
# debian/helpers.sh's own wrapper of the same name, the weights postinst,
# which is ALWAYS a packaged context -- postinst scripts only ever run
# against a real `dpkg`/`apt install`).
tt_bio_demo_weights_cache_packaged() {
    tt_bio_demo_weights_cache_impl_packaged
}
