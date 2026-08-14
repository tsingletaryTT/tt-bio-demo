#!/usr/bin/env bash
# Build all four .deb packages and print what they contain.
#
#   scripts/build-deb.sh [--out DIR]
#
# The printed report is what a reviewer reads INSTEAD OF INSTALLING: name,
# version, architecture, installed size, dependencies and file count for each
# package. That matters here because these packages install kernel modules
# and this box is shared -- so this script deliberately contains no `dpkg -i`,
# no `apt install`, and no `tt-bio install-deps`, and a test enforces that.
# To actually exercise an install, use the disposable container harness
# (tests/unit/conftest_container.py), never the host.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${REPO}/dist"

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "build-deb.sh: unknown argument: $1" >&2; exit 1 ;;
  esac
done

command -v dpkg-buildpackage >/dev/null || {
  echo "ERROR: dpkg-buildpackage not found." >&2
  echo "       The build needs the debhelper and devscripts packages." >&2
  exit 1
}

mkdir -p "$OUT"
cd "$REPO"

echo "building (this compiles nothing; it is a pure data package)..."
dpkg-buildpackage -us -uc -b

# dpkg-buildpackage writes to the PARENT directory and offers no way to
# redirect it. The parent here is a shared workspace holding other projects,
# so every artefact it produced gets swept -- including the .buildinfo and
# .changes files, which are easy to forget precisely because they are not the
# thing you were building.
shopt -s nullglob
for f in "${REPO}/.."/tt-bio-demo*_*.deb \
         "${REPO}/.."/tt-bio-demo*_*.buildinfo \
         "${REPO}/.."/tt-bio-demo*_*.changes; do
  mv "$f" "$OUT/"
done
shopt -u nullglob

echo
echo "╔══════════════════════════════════════════════════════════"
echo "║  built packages -> ${OUT}"
echo "╚══════════════════════════════════════════════════════════"

for deb in "$OUT"/*.deb; do
  [ -e "$deb" ] || continue
  name=$(dpkg-deb --field "$deb" Package)
  version=$(dpkg-deb --field "$deb" Version)
  arch=$(dpkg-deb --field "$deb" Architecture)
  isize=$(dpkg-deb --field "$deb" Installed-Size)
  depends=$(dpkg-deb --field "$deb" Depends | tr '\n' ' ' | sed 's/  */ /g')
  files=$(dpkg-deb --contents "$deb" | grep -vc '/$' || true)
  disk=$(du -h "$deb" | cut -f1)

  echo
  echo "╔══ ${name} ${version} (${arch})"
  echo "║  .deb on disk     ${disk}"
  echo "║  installed size   ${isize} KiB"
  echo "║  files shipped    ${files}"
  echo "║  depends          ${depends:-<none>}"
  # The maintainer scripts are where the damage lives; say which are present
  # so a reviewer knows what will run and can read them before anything does.
  # From the control tarball, not from `dpkg-deb --info`: that output is a
  # human-readable table whose columns shift (an executable script gets a `*`
  # marker, a plain control file does not), and parsing it with sed reported
  # "<none>" for packages that plainly have a postinst. The tar listing is
  # machine-readable and cannot drift.
  scripts_present=$(dpkg-deb --ctrl-tarfile "$deb" 2>/dev/null \
      | tar -t 2>/dev/null \
      | sed 's|^\./||' \
      | grep -E '^(preinst|postinst|prerm|postrm|config|templates)$' \
      | sort | tr '\n' ' ' || true)
  # `|| true` is load-bearing: grep exits 1 when a package has NO maintainer
  # scripts (the metapackage), and under `set -e` that killed the report loop
  # after the first such package. Found by running it, not by reading it.
  echo "║  maintainer hooks ${scripts_present:-<none>}"
  echo "╚══════════════════════════════════════════════════════════"
done

echo
echo "Nothing was installed, deliberately: these packages load kernel modules"
echo "and this box is shared. To exercise an install, use the throwaway container:"
echo "    .venvs/venv-ui/bin/python3 -m pytest tests/unit/test_packaging.py"
