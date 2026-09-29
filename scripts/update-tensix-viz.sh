#!/usr/bin/env bash
#
# update-tensix-viz.sh -- re-vendor tensix-viz.{js,css} from an upstream
# checkout, and rewrite ui/assets/tensix-viz/PROVENANCE.md's Version/Commit/
# Copied-on lines to match, in one step.
#
# Written because PROVENANCE.md's own "Updating" section used to be three
# manual steps (cp the two files, hand-edit three lines of Markdown, remember
# to update the date) -- the exact shape of copy-paste drift this project has
# been burned by before (the thumbnails-had-two-homes bug, the weights-cache
# variables, the exit-code/env-var duplication). This script is the version
# of that update where nothing is typed by hand.
#
# Usage:
#   scripts/update-tensix-viz.sh [path-to-tensix-viz-checkout]
#
# Default path: ~/code/tensix-viz (this box's usual location for it).
#
# What it does NOT do: touch tensix-viz's own git history, install anything,
# or run npm/node -- it assumes the upstream checkout has already been built
# (tensix-viz.js/tensix-viz.css present and current) and only copies the two
# vendored files plus updates their own attribution record here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST_DIR="${REPO_ROOT}/ui/assets/tensix-viz"
PROVENANCE="${DEST_DIR}/PROVENANCE.md"

UPSTREAM="${1:-${HOME}/code/tensix-viz}"

if [[ ! -d "$UPSTREAM" ]]; then
  echo "update-tensix-viz.sh: no such directory: ${UPSTREAM}" >&2
  exit 1
fi
if [[ ! -f "${UPSTREAM}/tensix-viz.js" || ! -f "${UPSTREAM}/tensix-viz.css" ]]; then
  echo "update-tensix-viz.sh: ${UPSTREAM} has no built tensix-viz.js/tensix-viz.css" \
       "-- run its own build first" >&2
  exit 1
fi
if [[ ! -f "${UPSTREAM}/package.json" ]]; then
  echo "update-tensix-viz.sh: ${UPSTREAM}/package.json not found; is this really" \
       "a tensix-viz checkout?" >&2
  exit 1
fi

# The upstream package's own version string, not this project's --
# tensix-viz.js. `-r` for a bare value with no quotes to strip in shell.
UPSTREAM_VERSION="$(python3 -c "
import json, sys
print(json.load(open(sys.argv[1]))['version'])
" "${UPSTREAM}/package.json")"

if [[ -d "${UPSTREAM}/.git" ]]; then
  UPSTREAM_COMMIT="$(git -C "$UPSTREAM" rev-parse HEAD)"
  UPSTREAM_SUBJECT="$(git -C "$UPSTREAM" log -1 --format=%s)"
  DIRTY="$(git -C "$UPSTREAM" status --porcelain)"
  if [[ -n "$DIRTY" ]]; then
    echo "update-tensix-viz.sh: ${UPSTREAM} has uncommitted changes -- commit" \
         "there first, so this vendored copy names a real, reproducible commit" >&2
    exit 1
  fi
else
  echo "update-tensix-viz.sh: ${UPSTREAM} is not a git checkout -- refusing to" \
       "vendor from it with no commit to attribute the copy to" >&2
  exit 1
fi

cp "${UPSTREAM}/tensix-viz.js" "${UPSTREAM}/tensix-viz.css" "$DEST_DIR/"

COPIED_ON="$(date +%Y-%m-%d)"

python3 - "$PROVENANCE" "$UPSTREAM_VERSION" "$UPSTREAM_COMMIT" "$UPSTREAM_SUBJECT" "$COPIED_ON" <<'PYEOF'
import re
import sys

path, version, commit, subject, copied_on = sys.argv[1:6]
text = open(path).read()

text, n_version = re.subn(
    r"^- \*\*Version:\*\* .*$", f"- **Version:** {version}",
    text, count=1, flags=re.MULTILINE)
text, n_commit = re.subn(
    r"^- \*\*Commit:\*\* .*$",
    f"- **Commit:** `{commit}` (`{subject}`)",
    text, count=1, flags=re.MULTILINE)
text, n_copied = re.subn(
    r"^- \*\*Copied on:\*\* .*$", f"- **Copied on:** {copied_on}",
    text, count=1, flags=re.MULTILINE)

missing = [name for name, n in (("Version", n_version), ("Commit", n_commit),
                                 ("Copied on", n_copied)) if n != 1]
if missing:
    sys.exit(f"update-tensix-viz.sh: PROVENANCE.md's {', '.join(missing)} "
              "line(s) were not found in the expected shape -- it may have "
              "been reworded; update it by hand this once and fix this "
              "script's regex to match.")

open(path, "w").write(text)
PYEOF

echo "update-tensix-viz.sh: vendored tensix-viz ${UPSTREAM_VERSION} (${UPSTREAM_COMMIT:0:12}) from ${UPSTREAM}"
echo "update-tensix-viz.sh: PROVENANCE.md updated. Now:"
echo "  - look at the panel (T in the running booth, or scripts/refresh-screenshots.sh)"
echo "  - run: scripts/test.sh"
echo "  - commit ui/assets/tensix-viz/{tensix-viz.js,tensix-viz.css,PROVENANCE.md} together"
