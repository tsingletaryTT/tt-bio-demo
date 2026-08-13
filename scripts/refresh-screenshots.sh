#!/usr/bin/env bash
# Refresh the README's screenshots from the REAL booth on REAL hardware.
#
# The README claims every image on it is the live application on real silicon.
# This script is what keeps that true: it launches scripts/run-demo.sh, waits
# for an actual fold, photographs a whole cycle, and writes the best frames to
# docs/screenshots/.
#
# Run it whenever the UI changes visibly. A screenshot that no longer matches
# the app is worse than none -- it is a confident lie on the landing page.
#
#   scripts/refresh-screenshots.sh            # capture + pick automatically
#   scripts/refresh-screenshots.sh --keep-all # also leave every burst frame
#
# CAPTURE METHOD, and why it is this one: on a KWin/Wayland box `ffmpeg
# x11grab` records pure black (verified -- one unique colour in the output)
# and `wf-recorder` refuses outright ("compositor doesn't support
# wlr-screencopy-unstable-v1"). Spectacle is the only thing that works here,
# and `grim` does not. Keys are driven with xdotool, which needs the window
# activated first or they land in whatever terminal has focus.
#
# REQUIRES A DEVICE. This folds for real; do not run it while someone else is
# using the chips.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO/docs/screenshots"
WORK="$(mktemp -d -t tt-bio-shots-XXXXXX)"
KEEP_ALL=0
[ "${1:-}" = "--keep-all" ] && KEEP_ALL=1

for tool in spectacle xdotool; do
  command -v "$tool" >/dev/null || { echo "ERROR: $tool is required" >&2; exit 1; }
done

mkdir -p "$OUT" "$WORK/frames"
cd "$REPO"

setsid ./scripts/run-demo.sh > "$WORK/demo.log" 2>&1 < /dev/null &
sleep 1
cleanup() {
  pkill -f "ui.app" 2>/dev/null
  pkill -f "runner.daemon" 2>/dev/null
  sleep 3
  [ "$KEEP_ALL" = "1" ] && echo "burst frames kept in $WORK/frames" || rm -rf "$WORK"
}
trap cleanup EXIT

echo "waiting for a real fold..."
for _ in $(seq 1 400); do
  grep -qE "folding |job_start" "$WORK/demo.log" 2>/dev/null && break
  sleep 0.5
done
grep -qE "folding |job_start" "$WORK/demo.log" 2>/dev/null || {
  echo "ERROR: no fold started -- is a device free? see $WORK/demo.log" >&2
  echo "  (this script needs hardware; it photographs real folds)" >&2
  KEEP_ALL=1; exit 1
}
sleep 6

activate() {
  local w; w="$(xdotool search --name 'tt-bio' 2>/dev/null | tail -1)"
  [ -n "$w" ] && xdotool windowactivate --sync "$w" 2>/dev/null
  sleep 1
}

# Burst the whole cycle rather than trying to time each moment: the showcase
# dwell is ~2s and folds run 4-22s by target, so timing individual frames is
# fragile in a way that fails silently (you get a black viewer, not an error).
echo "capturing the fold cycle..."
activate
for i in $(seq 1 30); do
  spectacle -b -n -f -o "$(printf '%s/frames/f_%02d.png' "$WORK" "$i")" 2>/dev/null
done

echo "capturing with the panels open..."
activate; xdotool key --clearmodifiers t 2>/dev/null; sleep 1.5
xdotool key --clearmodifiers d 2>/dev/null; sleep 2
for i in $(seq 1 12); do
  spectacle -b -n -f -o "$(printf '%s/frames/p_%02d.png' "$WORK" "$i")" 2>/dev/null
done

# Pick the frame with the most ribbon in the viewer area as the hero, and the
# fullest panels frame as the "in flight" shot. Counting pixels beats guessing
# at timing, and it is reproducible.
echo "choosing frames..."
"$REPO/.venvs/venv-ui/bin/python3" - "$WORK/frames" "$OUT" <<'PY'
import glob, os, sys
from PIL import Image

frames_dir, out = sys.argv[1], sys.argv[2]

def score(path, lo, hi):
    im = Image.open(path).convert("RGB").crop((0, 0, 1250, 1080))
    return sum(1 for r, g, b in im.getdata() if lo(r, g, b) and hi(r, g, b))

ribbon = lambda r, g, b: b > 120 and b - r > 40, lambda r, g, b: g < 120
cloud = lambda r, g, b: 150 < b < 235 and 90 < g < 200, lambda r, g, b: r < 160

hero = max(glob.glob(os.path.join(frames_dir, "f_*.png")),
           key=lambda p: score(p, *ribbon), default=None)
flight = max(glob.glob(os.path.join(frames_dir, "p_*.png")),
             key=lambda p: score(p, *cloud), default=None)

if hero:
    Image.open(hero).save(os.path.join(out, "01-folded-structure.png"))
    # The rail is the instrument, and at README width its 10pt type is
    # unreadable in a full-screen frame -- so ship a detail crop too.
    Image.open(hero).crop((1330, 10, 1920, 460)).save(
        os.path.join(out, "04-panels-detail.png"))
    print("  hero:", os.path.basename(hero))
if flight:
    Image.open(flight).save(os.path.join(out, "02-live-diffusion.png"))
    Image.open(flight).crop((1340, 430, 1920, 770)).save(
        os.path.join(out, "03-diagnostics-detail.png"))
    print("  in-flight:", os.path.basename(flight))
PY

echo
echo "=== docs/screenshots ==="
ls -la "$OUT"/*.png | awk '{print "  " $5, $9}'
echo
echo "Look at them before committing. A screenshot that no longer matches the app"
echo "is worse than no screenshot at all."
