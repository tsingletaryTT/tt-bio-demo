#!/usr/bin/env bash
# Record a demo-video master of the booth: the 2x2 quad view, four chips, at
# 1920x1080 @ 60 fps, with nothing on screen but the application.
#
#   scripts/record-demo-video.sh [seconds]        # default 130
#   OUT_DIR=/somewhere scripts/record-demo-video.sh 200
#
# Writes <workdir>/quad-master.mp4 and prints the working directory. Recut it
# with the recipes in recordings/README.md.
#
# REQUIRES A DEVICE, and requires OBS's screen-capture portal to have been
# granted interactively once this login session (see that same README).
#
# THE ORDER OF OPERATIONS BELOW IS THE WHOLE POINT. Two earlier takes were
# lost to it:
#
#   * OBS throws a modal "Plugin Load Error" dialog at startup which steals
#     focus, un-raises the fullscreen booth and lets the desktop taskbar
#     through -- for every frame of the recording. It cannot be closed with
#     wmctrl or xdotool: OBS is a Wayland-native Qt app here, so its windows
#     are invisible to XWayland tooling. So OBS starts FIRST, throws its
#     dialog at an empty desktop, and the booth then comes up fullscreen over
#     the top of it. The dirty head is trimmed off by offset afterwards.
#
#   * The booth is started with `--quad` rather than by pressing Q. Driving
#     that key with xdotool silently does not work -- `--window` sends
#     XSendEvent, which GTK ignores, and the global form uses XTEST, which
#     needs real focus that Spectacle steals. Both exit 0 and both record the
#     solo view.
#
# And then it PHOTOGRAPHS THE SCREEN and refuses to continue unless it is
# actually clean, because every failure above reported success.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S="${OUT_DIR:-$(mktemp -d -t tt-bio-rec-XXXXXX)}"; mkdir -p "$S"
echo "working directory: $S"
RECORD_S="${1:-130}"

cd "$REPO"
cleanup() {
  pkill -INT obs 2>/dev/null; sleep 5; pkill obs 2>/dev/null
  pkill -f "ui.app" 2>/dev/null; pkill -f "runner.daemon" 2>/dev/null
  sleep 4; echo "booth stopped"
}
trap cleanup EXIT

# 1. OBS first, alone on the screen, so its dialog lands on the desktop.
BEFORE="$(ls -t "$HOME"/*.mkv 2>/dev/null | head -1 || true)"
systemd-inhibit --what=idle:sleep --why="tt-bio-demo quad recording" \
  obs --startrecording --minimize-to-tray > "$S/obs.log" 2>&1 &
OBS=$!
sleep 15
AFTER="$(ls -t "$HOME"/*.mkv 2>/dev/null | head -1 || true)"
[ "$AFTER" != "$BEFORE" ] || { echo "ERROR: OBS produced no file"; exit 1; }
T0="$(date +%s)"
echo "recording to $(basename "$AFTER"); bringing the booth up over it"

# 2. Now the booth, fullscreen, on top.
setsid ./scripts/run-demo.sh --quad > "$S/demo.log" 2>&1 < /dev/null &
sleep 1

echo "waiting for all four chips..."
for _ in $(seq 1 700); do
  n=$(grep -oE "on chip [0-9]" "$S/demo.log" 2>/dev/null | sort -u | wc -l)
  [ "$n" -ge 4 ] && break; sleep 0.5
done
echo "chips folding: $(grep -oE 'on chip [0-9]' "$S/demo.log" | sort -u | tr '\n' ' ')"
sleep 8

BOOTH="$(xdotool search --name 'tt-bio' 2>/dev/null | tail -1)"
if [ -n "$BOOTH" ]; then
  xdotool windowactivate --sync "$BOOTH" 2>/dev/null
  xdotool windowraise "$BOOTH" 2>/dev/null
fi
sleep 3

# 3. Photograph the screen and refuse unless it is genuinely clean.
spectacle -b -n -f -o "$S/precheck.png" 2>/dev/null
sleep 1
"$REPO/.venvs/venv-ui/bin/python3" - "$S/precheck.png" <<'PY' || exit 1
import sys
from PIL import Image
im = Image.open(sys.argv[1]).convert("RGB")
dialog = sum(1 for p in (im.getpixel((x, 470)) for x in range(830, 1120, 10))
             if all(c > 90 for c in p))
taskbar = sum(1 for p in (im.getpixel((x, 1058)) for x in range(0, 1900, 20))
              if all(c > 60 for c in p))
print(f"  precheck: dialog {dialog}/29, taskbar {taskbar}/95")
if dialog > 3 or taskbar > 6:
    print("  REFUSING: chrome is still on screen")
    sys.exit(1)
print("  screen is clean")
PY
HEAD=$(( $(date +%s) - T0 + 2 ))
echo "clean from ${HEAD}s in; capturing ${RECORD_S}s"

sleep "$RECORD_S"
kill -INT "$OBS" 2>/dev/null; sleep 8; kill "$OBS" 2>/dev/null; sleep 3

echo; echo "=== folds during the recording ==="
grep -c "done in " "$S/demo.log" 2>/dev/null || echo 0
grep -oE "on chip [0-9]" "$S/demo.log" 2>/dev/null | sort | uniq -c

echo; echo "transcoding from ${HEAD}s..."
ffmpeg -hide_banner -loglevel error -ss "$HEAD" -i "$AFTER" \
  -c:v libx264 -crf 20 -pix_fmt yuv420p -movflags +faststart -an -y "$S/quad-master.mp4"
ffprobe -hide_banner -v error -show_entries format=duration:stream=avg_frame_rate,width,height \
  -of default=nw=1 "$S/quad-master.mp4"
