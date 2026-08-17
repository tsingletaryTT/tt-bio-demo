#!/usr/bin/env bash
#
# build.sh — render the two-page booth one-pager to PDF.
#
#   page 1  what the booth is doing        (for a visitor, or a colleague)
#   page 2  how to operate it              (for whoever is running it)
#
# Output: docs/tt-bio-demo-onepager.pdf — checked in, because the thing that
# gets printed at a venue should not require a working toolchain to obtain.
# Rebuild it whenever the fold times, the key bindings or VERSION change.
#
# WHY A TEMPLATE PLUS A BUILD STEP rather than one HTML file: the hero
# screenshot is embedded as a base64 data URI so the PDF and the HTML preview
# are both self-contained (no missing-image surprises when the file is emailed
# or opened from a different directory). That substitution is what this script
# does, along with reading VERSION so the sheet cannot claim a version the
# repo no longer is.
#
# Requires: google-chrome (headless print-to-pdf), python3. Both are already
# assumed present on a dev box; this never runs on a booth machine.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
OUT="${REPO}/docs/tt-bio-demo-onepager.pdf"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

CHROME="${CHROME:-google-chrome}"
command -v "${CHROME}" >/dev/null || {
    echo "error: ${CHROME} not found. Set CHROME=/path/to/chrome." >&2
    exit 1
}

# ── 1. inline the screenshot and the version ────────────────────────────────
python3 - "$REPO" "$HERE" "$WORK" <<'PY'
import base64, pathlib, sys
repo, here, work = (pathlib.Path(a) for a in sys.argv[1:4])

def data_uri(rel):
    """Embed a PNG as a data: URI so the page carries its own images."""
    return "data:image/png;base64," + base64.b64encode((repo / rel).read_bytes()).decode()

html = (here / "onepager.html.tmpl").read_text()
html = html.replace("__IMG_QUAD__", data_uri("docs/screenshots/06-quad-four-chips.png"))
html = html.replace("__VERSION__", (repo / "VERSION").read_text().strip())
(work / "onepager.html").write_text(html)
PY

# ── 2. render ───────────────────────────────────────────────────────────────
# --no-pdf-header-footer suppresses Chrome's default date/URL furniture, which
# would otherwise print over the design's own margins.
"${CHROME}" --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
            --print-to-pdf="${OUT}" "${WORK}/onepager.html" 2>/dev/null

# ── 3. assert it is still exactly two pages ─────────────────────────────────
# The layout is fixed-height with `overflow: hidden`, so content that grows past
# a page is CLIPPED rather than reflowed -- it would not add a third page, it
# would silently cut the footer off. Page count catches the "spilled to page 3"
# failure; the eyeball check below is what catches clipping. Both matter.
python3 - "$OUT" <<'PY'
import re, sys, pathlib
pdf = pathlib.Path(sys.argv[1])
pages = len(re.findall(rb'/Type\s*/Page[^s]', pdf.read_bytes()))
print(f"{pdf.relative_to(pdf.parents[2])}: {pages} page(s), {pdf.stat().st_size // 1024} KB")
if pages != 2:
    sys.exit(f"error: expected exactly 2 pages, got {pages}")
PY

cat <<EOF

Look at it before sending it anywhere -- a clipped footer still counts as
two pages:

    pdftoppm -png -r 90 ${OUT} /tmp/onepager && xdg-open /tmp/onepager-1.png

Paper size is US Letter (\`@page { size: Letter }\` in the template). For a
venue that prints A4, change that one line and rebuild.
EOF
