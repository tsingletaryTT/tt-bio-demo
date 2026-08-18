#!/usr/bin/env bash
# Reproduce the FKBP12 L1/circular-buffer clash, and show that it is
# grid-dependent. See ISSUE.md in this directory for the full write-up.
#
# Run it from a tt-bio checkout (or anywhere `tt-bio` is on PATH and
# examples/affinity_fkg.yaml exists). Takes about a minute.
#
# ON A SHARED MACHINE, take a chip lease first -- this opens a device:
#     gozer run --chips 1 --who you --reason "tt-bio issue repro" -- ./reproduce.sh
set -uo pipefail

YAML="${1:-examples/affinity_fkg.yaml}"
[ -f "$YAML" ] || { echo "no such input: $YAML" >&2; exit 1; }

# INDEX form, not a BDF: the predict path does int() on this variable, while
# ttnn's own device open accepts a BDF. See ISSUE.md, "two small things".
export TT_VISIBLE_DEVICES="${TT_VISIBLE_DEVICES_INDEX:-0}"

echo "=== 1. default grid: expected to FAIL ==============================="
tt-bio predict "$YAML" --model protenix-v2 --accelerator tenstorrent \
    --single_sequence --out_dir /tmp/fkbp12-default 2>&1 \
  | grep -iE "clash|circular buffer|ok, .* failed" | head -3

echo
echo "=== 2. grid pinned to 11x9 (99 cores): expected to SUCCEED =========="
# TT_BIO_FORCE_GRID landed in cde28838 and is on main, NOT in the 0.6.3
# release. On 0.6.3 this step is a no-op and step 2 will fail like step 1;
# that is expected, and is why the issue quotes main for this half.
TT_BIO_FORCE_GRID=11,9 tt-bio predict "$YAML" --model protenix-v2 \
    --accelerator tenstorrent --single_sequence --out_dir /tmp/fkbp12-11x9 2>&1 \
  | grep -iE "clash|circular buffer|ok, .* failed" | head -3

echo
echo "Expected: step 1 clashes at [(0,0)-(10,9)] with L1 buffer 1155072 and"
echo "the static CB region ending 1159680; step 2 folds. The threshold sits"
echo "between 100 cores (works) and 110 (fails) -- see the table in ISSUE.md."
