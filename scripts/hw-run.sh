#!/usr/bin/env bash
#
# hw-run.sh -- the one way to run anything that opens a Tenstorrent chip.
#
#   scripts/hw-run.sh --chips N --reason "why" [--one-chip] -- command [args...]
#
# Why it exists. On 2026-10-13 a test run launched as
#   gozer run --chips 1 ... scripts/test.sh --hw <two test files>
# ran the whole integration directory plus the four-card pool test under a lease
# for one board, and the machine froze. `test.sh --hw` opens every card, and extra
# file arguments do not narrow it. This script refuses that shape and gives every
# hardware command the same entry point, with or without tt-gozer.
#
#   With gozer on this machine: takes a lease (`gozer run --chips N`).
#   --one-chip narrows TT_VISIBLE_DEVICES to the first chip of whatever was granted.
#   Without gozer: refuses if ANY card on this box has a device node held by a
#   process, then pins TT_VISIBLE_DEVICES to the first N chips. Two limits, stated
#   plainly:
#     - Nothing is leased and nothing fences other users. `fuser` run as a normal
#       user only sees processes that user owns, so "idle" means "no process of
#       yours holds it", not "nobody holds it".
#     - UMD expands TT_VISIBLE_DEVICES to a whole board when a board has 2 chips or
#       fewer, so the sibling chip of a pinned chip may be opened too. That is why
#       every card is checked, not just the N pinned ones. Without gozer,
#       --one-chip does not isolate a chip; it only pins the first card.
#   The script never resets a chip.
#
# Exit codes: 2 usage, 10 queued (from gozer), 11 chips busy or not checkable,
# 12 refused, otherwise the command's own status.
set -euo pipefail

PCI_ROOT="${TT_HW_PCI_ROOT:-/sys/bus/pci/devices}"
DEV_ROOT="${TT_HW_DEV_ROOT:-/dev/tenstorrent}"
TT_VENDOR_ID="0x1e52"

die() { echo "hw-run: $1" >&2; exit "${2:-2}"; }

# Print the header comment (from line 3 up to the first code line), without the
# leading "# " on each line. Stops at `set -euo`, so the code is never printed.
usage() {
  sed -n '3,/^set -euo/{/^set -euo/d;p}' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# Every Tenstorrent PCI address, in stable sorted order. The Nth address is
# /dev/tenstorrent/N, the same mapping tt-smi uses.
list_cards() {
  local entry vendor
  for entry in "$PCI_ROOT"/*; do
    [[ -r "$entry/vendor" ]] || continue
    vendor="$(tr 'A-Z' 'a-z' < "$entry/vendor")"
    [[ "$vendor" == "$TT_VENDOR_ID" ]] && basename "$entry"
  done | sort
}

CHIPS=""; REASON=""; ONE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --chips)    CHIPS="${2:-}"; shift 2 ;;
    --reason)   REASON="${2:-}"; shift 2 ;;
    --one-chip) ONE=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    --)         shift; break ;;
    *)          die "unknown argument: $1 (the command goes after --)" 2 ;;
  esac
done

[[ "$CHIPS" =~ ^[0-9]+$ ]] && (( CHIPS >= 1 )) || die "--chips N is required" 2
[[ -n "$REASON" ]] || die "--reason is required" 2
(( $# >= 1 )) || die "no command given after --" 2

mapfile -t ALL < <(list_cards)
CARDS=${#ALL[@]}
(( CARDS > 0 )) || die "no Tenstorrent cards found under $PCI_ROOT" 2
(( CHIPS <= CARDS )) || die "asked for $CHIPS chips, this box has $CARDS" 2

# The guard. Refuses, when the lease or pin covers fewer chips than the box has,
# every command shape that opens EVERY card:
#   - a test.sh run with --hw (or the env switch that turns the same thing on);
#   - pytest over the whole tests/integration directory (any path ending in
#     tests/integration, with or without a trailing slash);
#   - anything naming four_workers, the pool test that opens every card
#     (tests/integration/conftest.py's tt_cards_present does not narrow visibility).
# A pytest command naming one other test file passes.
joined=" $* "
opens_every_card=""
if [[ "$joined" == *"test.sh"* ]] && { [[ "$joined" == *" --hw "* ]] || [[ "${TT_BIO_DEMO_HW_TESTS:-0}" == "1" ]]; }; then
  opens_every_card="test.sh --hw"
fi
for arg in "$@"; do
  case "$arg" in
    tests/integration|tests/integration/|*/tests/integration|*/tests/integration/)
      opens_every_card="pytest over the whole tests/integration directory (it includes test_four_workers.py)" ;;
    *four_workers*)
      opens_every_card="test_four_workers.py" ;;
  esac
done
if [[ -n "$opens_every_card" ]] && (( CHIPS < CARDS )); then
  die "refusing: $opens_every_card opens EVERY card ($CARDS on this box), and extra file arguments do not narrow it. Ask for --chips $CARDS, or run pytest on single named test files." 12
fi

if command -v gozer >/dev/null 2>&1; then
  # --one-chip narrows only when gozer actually set TT_VISIBLE_DEVICES. Exporting
  # an empty value would tell the command it can see no chips at all.
  inner='if [[ "${HW_RUN_ONE:-0}" == 1 && -n "${TT_VISIBLE_DEVICES:-}" ]]; then export TT_VISIBLE_DEVICES="${TT_VISIBLE_DEVICES%%,*}"; fi; exec "$@"'
  HW_RUN_ONE="$ONE" exec gozer run --chips "$CHIPS" --who "${HW_RUN_WHO:-claude:tt-bio-demo}" \
    --reason "$REASON" -- bash -c "$inner" hw-run "$@"
fi

# No gozer. Check every card (not just the N pinned ones: see the header on
# board expansion), pin, and be plain about what is not protected.
busy=0
for (( i = 0; i < CARDS; i++ )); do
  node="$DEV_ROOT/$i"
  [[ -e "$node" ]] || continue
  if command -v fuser >/dev/null 2>&1; then
    if fuser -s "$node" 2>/dev/null; then
      echo "hw-run: $node is held by a running process" >&2
      busy=1
    fi
  elif [[ "${HW_RUN_ASSUME_IDLE:-0}" != "1" ]]; then
    die "cannot check whether the chips are idle (no fuser). Set HW_RUN_ASSUME_IDLE=1 only if you know nobody else is using this box." 11
  fi
done
(( busy == 0 )) || die "a chip on this box is held; without gozer the sibling chip of a pinned chip may be opened too. Not starting." 11

visible="$(IFS=,; echo "${ALL[*]:0:CHIPS}")"
(( ONE )) && visible="${ALL[0]}"
echo "hw-run: gozer is not installed here: these chips are NOT leased and nothing fences other users; idleness was checked for processes you own only. Using $visible." >&2
echo "hw-run: if this run wedges a chip, do not reset it from this script; stop and ask the box's owner." >&2
TT_VISIBLE_DEVICES="$visible" exec "$@"
