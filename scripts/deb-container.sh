#!/usr/bin/env bash
#
# deb-container.sh -- the ONE place a `docker run` for package-install
# testing is allowed to live in this project (see
# tests/unit/conftest_container.py, which is the only caller).
#
# Runs a shell command inside a disposable, throwaway Ubuntu container.
# Never touches the host's dpkg database (the host's package manager is
# never invoked; only `docker run` is) and never attaches any hardware
# device to the container -- this script adds no `--device` flag of any
# kind, under any circumstance, full stop. That guarantee is worth more
# than any feature this script could gain by relaxing it, so don't add one
# even for a "just for debugging" case.
#
# Usage:
#   deb-container.sh WORKDIR [--env KEY=VAL]... -- SHELL_COMMAND
#
# WORKDIR is a host directory the caller has already prepared (or will let
# this script create empty) and is bind-mounted read-write at /work inside
# the container -- it is how state gets back OUT once the container (and
# the ephemeral filesystem it was given) is gone:
#   WORKDIR/bin/<name>   a staged fake executable (a "shim"); present on
#                        PATH ahead of every real system directory, so it
#                        shadows anything -- a real command or a package's
#                        maintainer script -- that would otherwise be
#                        invoked by that name.
#   WORKDIR/debs/*.deb   .deb files staged for an install.
#   WORKDIR/preseed.cfg  debconf selections staged for an install.
#   WORKDIR/shim.log      written BY a shim as it is invoked (see conftest_container.py).
#   WORKDIR/status        written by this script's own trailer, below, with
#                        `dpkg-query` output for every tt-bio-demo* package.
#
# SHELL_COMMAND is passed to `bash -c` inside the container, from a `cd
# /work` starting point, with /work/bin prepended to PATH.
#
# The container is always torn down automatically the moment its one
# command exits: nothing from a test run is meant to survive on this
# shared box past the one invocation that made it, and a container per
# call (not one long-lived container reused across tests) is the
# deliberate choice here -- see conftest_container.py's module docstring
# for why reuse was rejected (mutable installed state leaking between
# tests is worse than the extra container-start latency).
set -euo pipefail

usage() {
  echo "Usage: $0 WORKDIR [--env KEY=VAL]... -- SHELL_COMMAND" >&2
}

if [[ $# -lt 1 ]]; then
  usage
  exit 64
fi

# Override for the fidelity tier later tasks may add (Dockerfile.qb2); the
# default is the fast tier this task was told to use, already pulled.
IMAGE="${DEB_CONTAINER_IMAGE:-ubuntu:24.04}"

WORKDIR="$1"
shift

ENV_FLAGS=()
while [[ "${1:-}" == "--env" ]]; do
  if [[ $# -lt 2 ]]; then
    echo "--env requires a KEY=VAL argument" >&2
    exit 64
  fi
  ENV_FLAGS+=(-e "$2")
  shift 2
done

if [[ "${1:-}" != "--" ]]; then
  usage
  exit 64
fi
shift

if [[ $# -ne 1 ]]; then
  usage
  exit 64
fi
SHELL_COMMAND="$1"

mkdir -p "${WORKDIR}/bin" "${WORKDIR}/debs"

# The trailer: capture the real command's exit status BEFORE running
# anything else (a later command would clobber $?), then always dump
# every tt-bio-demo* package's dpkg status to /work/status -- regardless
# of whether the command was a plain `run` or an `install` -- so
# Result.status(pkg) has something to read either way, and finally re-exit
# with the real status so the caller's success/failure judgement is about
# the caller's own command, not about whether the status dump worked.
FULL_COMMAND="cd /work
${SHELL_COMMAND}
_rc=\$?
dpkg-query -W -f='\${Package} \${Status}\n' 'tt-bio-demo*' >/work/status 2>/dev/null || true
exit \$_rc"

exec docker run --rm \
  -v "${WORKDIR}:/work" \
  -e "PATH=/work/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  -e "DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-noninteractive}" \
  "${ENV_FLAGS[@]}" \
  "${IMAGE}" \
  bash -c "${FULL_COMMAND}"
