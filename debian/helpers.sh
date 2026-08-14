# debian/helpers.sh -- every branch the maintainer scripts need.
#
# Maintainer scripts are the least testable code in a Debian package and the
# most damaging when they are wrong: a postinst that exits 0 having done half
# its job leaves apt believing the package is configured. So anything with a
# decision in it lives HERE, as a shell function a test can call directly,
# and the maintainer scripts stay a readable sequence of calls.
#
# POSIX sh, NOT bash. Debian maintainer scripts run under /bin/sh, which is
# dash on Ubuntu -- no `[[`, no arrays, no `local` (it is near-universal but
# not POSIX; used nowhere here), no `$'...'`. `sh -n` over this file is a
# test (`test_helpers_are_posix_sh_not_bashisms`).
#
# Every function is prefixed `tt_bio_demo_` because this file is SOURCED into
# maintainer scripts that also source debconf's confmodule; a bare `log` or
# `prefix` would be a collision waiting to happen.

# Where the application tree is installed. Must agree with
# debian/tt-bio-demo.install -- if the two drift, the maintainer scripts
# operate confidently on an empty directory, so a test pins them together.
# Overridable so the same helpers can be exercised against a staging tree.
tt_bio_demo_prefix() {
    printf '%s\n' "${TT_BIO_DEMO_PREFIX:-/opt/tt-bio-demo}"
}

# A timestamped line on stderr, tagged so it is findable in a journal that
# also holds apt's own output. stderr, not stdout: several helpers here
# return their answer ON stdout and a log line mixed into that would be
# captured by the caller as part of the value.
tt_bio_demo_log() {
    printf '[tt-bio-demo] %s\n' "$*" >&2
}

tt_bio_demo_have_command() {
    command -v "$1" >/dev/null 2>&1
}

# The tt-bio release pin, READ FROM scripts/setup-venvs.sh rather than
# copied.
#
# The project's standing rule is that tt-bio is pinned to a release tag and
# that the pin lives in exactly one place. A second copy here would not be
# wrong on the day it was written -- it would be wrong the first time
# somebody bumped the other one, and it would be wrong silently, producing a
# package that builds a venv at a version its own README disclaims.
#
# Candidates in order: an explicit override, the installed tree (the real
# case, at postinst time), then the working directory (the development case,
# where the tests run from the repo root). There is no portable way for a
# SOURCED POSIX script to find its own path -- `$0` is still the caller's --
# so this searches rather than computing a path relative to itself.
tt_bio_demo_setup_venvs_path() {
    for candidate in \
        "${TT_BIO_DEMO_SETUP_VENVS:-}" \
        "$(tt_bio_demo_prefix)/scripts/setup-venvs.sh" \
        "${PWD}/scripts/setup-venvs.sh"
    do
        if [ -n "$candidate" ] && [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

tt_bio_demo_pinned_version() {
    path=$(tt_bio_demo_setup_venvs_path) || {
        tt_bio_demo_log "cannot find setup-venvs.sh to read the tt-bio pin from"
        return 1
    }
    version=$(sed -n 's/^TT_BIO_VERSION="\([^"]*\)".*/\1/p' "$path" | head -n 1)
    if [ -z "$version" ]; then
        tt_bio_demo_log "$path no longer declares TT_BIO_VERSION"
        return 1
    fi
    printf '%s\n' "$version"
}

# Verify a downloaded file against an expected SHA-256.
#
# THE IMPORTANT CASE IS THE MISSING FILE. The realistic failure is a download
# that produced nothing -- a 404 written to disk as an empty file, a mirror
# that hung up, a full disk -- and a verifier that treats absence as success
# converts that into a booth with no weights and a package apt considers
# installed. So absence fails, an empty expected hash fails (that is what an
# unset "$EXPECTED" expands to at the call site), and only a genuine match
# succeeds.
tt_bio_demo_verify_sha256() {
    path="$1"
    expected="$2"

    if [ -z "$path" ] || [ -z "$expected" ]; then
        tt_bio_demo_log "checksum check called without both a path and a hash"
        return 1
    fi
    if [ ! -f "$path" ]; then
        tt_bio_demo_log "checksum check failed: $path does not exist"
        return 1
    fi
    if ! tt_bio_demo_have_command sha256sum; then
        tt_bio_demo_log "checksum check failed: sha256sum is not available"
        return 1
    fi

    actual=$(sha256sum "$path" | cut -d' ' -f1)
    if [ "$actual" != "$expected" ]; then
        tt_bio_demo_log "checksum MISMATCH for $path"
        tt_bio_demo_log "  expected $expected"
        tt_bio_demo_log "  actual   $actual"
        return 1
    fi
    return 0
}

# Where tt-bio actually is, as an absolute path.
#
# NEVER `command -v tt-bio` from a maintainer script. dpkg runs maintainer
# scripts with a FIXED PATH of /usr/sbin:/usr/bin:/sbin:/bin -- not the
# caller's, and nothing ever adds a venv to it. tt-bio is pip-installed into
# <prefix>/.venvs/venv-runner/, so a PATH lookup cannot find it on any real
# machine, and code guarded by one is dead code that looks fine in review and
# in a container where a shim happens to sit in /usr/bin.
#
# Checked in order: the venv this project builds, then PATH (for an operator
# who installed tt-bio system-wide themselves).
tt_bio_demo_tt_bio_bin() {
    candidate="$(tt_bio_demo_prefix)/.venvs/venv-runner/bin/tt-bio"
    if [ -x "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
    fi
    if tt_bio_demo_have_command tt-bio; then
        command -v tt-bio
        return 0
    fi
    return 1
}
