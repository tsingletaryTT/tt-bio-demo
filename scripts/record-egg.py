#!/usr/bin/env python3
"""Record the easter egg -- the Tenstorrent mark condensing out of noise.

Run it from the repo root with no arguments:

    ./scripts/record-egg.py

Writes `recordings/tenstorrent-mark-on-chip-<N>s.mp4` (and, with `--gif`, a
GIF beside it) plus a contact sheet you are expected to LOOK AT.

What this records is not a screen capture of the booth. It is the same
arithmetic, on the same silicon, through the same renderer:

    `runner.egg.run_egg` runs the descent ON A TENSTORRENT CHIP -- the ttnn
    implementation of `mark.py`, the one `Ctrl+G` at the booth asks a worker
    for -- and every frame it emits is drawn by `ui.viewer.StructureViewer`,
    configured exactly as `ui/app.py` configures the egg's own viewer
    (BRAND_PURPLE points, no spin). What is missing relative to a booth
    screen recording is the overlay card and the side rail, nothing else.

Recording it this way rather than with OBS is deliberate. Screen capture on
this box needs the PipeWire portal granted interactively once per login
session, and the booth's `Ctrl+G` cannot be driven by `xdotool` during a
capture at all (`--window` sends XSendEvent, which GTK ignores; the global
form needs real focus). Both failure modes exit 0 -- see the project's
CLAUDE.md, which cost a 169-second recording of the wrong view to learn.
This path needs no portal, no focus and no keystroke, and it is
deterministic: `--seed` reproduces a run frame for frame.

WHY TWO PROCESSES
-----------------
The same split, for the same reason, as `scripts/make-thumbnails.py`:
`.venvs/venv-runner` has torch, ttnn and tt_bio but no gi/PyOpenGL;
`.venvs/venv-ui` has GTK and the renderer and must never import torch. So
this script re-executes itself:

    stage `fold`    under .venvs/venv-runner  -> writes an .npz of frames
    stage `render`  under .venvs/venv-ui      -> reads it, writes PNGs

Run with no `--stage` and it drives both, then encodes. `--from-npz PATH`
skips the chip entirely and re-renders a trajectory already captured, which
is how the render stage and the encode can be worked on with no hardware.

IT REFUSES RATHER THAN SHIPPING A BAD CAPTURE
---------------------------------------------
This project's standing rule is that a capture you have not looked at is not
verified, because every capture failure it has ever had reported success.
A script cannot look, so this one measures instead: the fold stage checks
the cloud the chip returned actually landed ON the mark, using `mark.py`'s
own signed distance field as the oracle -- the same field
`tests/unit/test_mark.py` rasterises against the shipped artwork. Measured
on this box (seed 20260814, 6000 points): 0.4% of points are inside the mark
at step 0 and 98.8% at step 180, so `--min-settled 0.90` sits in a wide gap
rather than on a threshold. A truncated run, a wrong seed plumbed through,
or a chip that returned noise all fail it.

It then writes a contact sheet of evenly spaced frames next to the video and
tells you to look at it, because the measurement above proves the CHIP was
right and says nothing about whether the RENDER is.

HARDWARE
--------
The fold stage opens one chip (`--device`, default 0) and holds it for about
a second and a half -- the whole trajectory is computed up front, not in real
time. The first egg on a cold ttnn kernel cache costs ~10 s more. It always
closes the device, including on failure, so it never leaves a card held.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RECORDINGS = REPO_ROOT / "recordings"

# Both venvs, found the way scripts/make-thumbnails.py finds them --
# `TT_BIO_DEMO_PREFIX` is scripts/test.sh's own override, reused rather than
# spelled a second way, and it is what makes this runnable from a git
# worktree where `.venvs/` (gitignored) does not exist.
VENV_PREFIX = Path(os.environ.get("TT_BIO_DEMO_PREFIX")
                   or REPO_ROOT / ".venvs")
VENV_RUNNER = VENV_PREFIX / "venv-runner" / "bin" / "python3"
VENV_UI = VENV_PREFIX / "venv-ui" / "bin" / "python3"

# 1280x720 at 30 fps: the descent is 181 frames (mark.STEPS + the untouched
# noise draw at step 0), so it plays in almost exactly six seconds, which is
# the pace the booth plays it at (`_EGG_STEP_MS`, 33 ms).
DEFAULT_W, DEFAULT_H = 1280, 720
DEFAULT_FPS = 30
# Seconds to hold on the finished mark before the video ends, so it does not
# cut the instant the last point lands.
DEFAULT_HOLD_S = 2.0
# crf 20 at native resolution. The lesson recordings/README.md paid for is
# to spend bytes on pixels rather than on bitrate for pixels already thrown
# away -- so this never downscales, and a GIF that must shrink loses
# DURATION, never frame rate.
DEFAULT_CRF = 20
DEFAULT_GIF_FPS = 20
DEFAULT_GIF_WIDTH = 640

# The fraction of points that must have landed inside the mark for the fold
# stage to accept what the chip returned. See "IT REFUSES" above for the
# measurement this floor sits in the middle of.
DEFAULT_MIN_SETTLED = 0.90

FRAME_GLOB = "f%04d.png"


# ---------------------------------------------------------------------------
# Pure helpers. No GTK, no ttnn, no filesystem -- so tests/unit/test_record_egg.py
# can drive the parts that decide what the video contains without hardware.
# ---------------------------------------------------------------------------

def frame_plan(total, fps=DEFAULT_FPS, hold_s=DEFAULT_HOLD_S):
    """Which source frame each output frame draws, as a list of indices.

    The descent once, in order, then the LAST frame repeated for `hold_s`.
    Holding the last one is the whole point of the hold -- an off-by-one that
    held `total` would run off the end and one that held frame 0 would end
    the video on the noise it started from.
    """
    if total < 1:
        raise ValueError("a trajectory needs at least one frame")
    held = max(0, int(round(fps * hold_s)))
    return list(range(total)) + [total - 1] * held


def settled_fraction(points, scale, half_thickness=None):
    """What fraction of `points` landed inside the mark.

    `points` are in the render's own units (what `runner.egg` emits), so they
    are divided by `scale` to reach the field's units -- the same conversion
    tests/integration/test_egg_on_device.py makes before comparing chip
    against host. Kept here rather than inlined in the fold stage so the
    refusal threshold is testable against a cloud that never descended.
    """
    import numpy as np
    import mark

    if half_thickness is None:
        half_thickness = mark.HALF_THICKNESS
    distance, _ = mark.slab_sdf_gradient(
        np.asarray(points, dtype=float) / float(scale), half_thickness)
    return float((distance <= 1e-3).mean())


def configure_egg_viewer(viewer):
    """Give a `StructureViewer` the configuration the booth's egg viewer has.

    `ui/app.py` builds the egg its own viewer and sets exactly these two
    things on it. A recording made through a differently configured viewer
    would be a picture of a booth that does not exist -- the same defect
    `scripts/make-thumbnails.py` shipped once when it drew thumbnails with a
    renderer the booth had stopped using.
    """
    import mark

    viewer.set_point_color(mark.BRAND_PURPLE)
    viewer.set_spin_rate(0.0)
    return viewer


def encode_command(frames_dir, out_path, fps=DEFAULT_FPS, crf=DEFAULT_CRF):
    """The ffmpeg call that turns the captured PNGs into an mp4.

    `-framerate` is an INPUT option and has to precede `-i`; after it, it is
    silently ignored and ffmpeg assembles the stills at its 25 fps default,
    which would stretch a six-second descent to seven and desynchronise it
    from the booth's own pacing. `-pix_fmt yuv420p` is what makes the result
    play in browsers and QuickTime rather than only in ffplay.
    """
    return [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", str(Path(frames_dir) / FRAME_GLOB),
        "-c:v", "libx264", "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out_path),
    ]


def gif_commands(mp4_path, gif_path, palette_path,
                 fps=DEFAULT_GIF_FPS, width=DEFAULT_GIF_WIDTH):
    """The two ffmpeg calls that make a GIF: build a palette, then use it.

    One-pass GIF encoding quantises to a fixed 216-colour web palette and
    bands a purple cloud on a dark ground badly. Two passes cost a second.
    """
    scale = f"fps={fps},scale={width}:-1:flags=lanczos"
    return (
        ["ffmpeg", "-y", "-i", str(mp4_path),
         "-vf", f"{scale},palettegen=stats_mode=diff", str(palette_path)],
        ["ffmpeg", "-y", "-i", str(mp4_path), "-i", str(palette_path),
         "-lavfi", f"{scale}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",
         str(gif_path)],
    )


def contact_sheet_indices(total, tiles=8):
    """Evenly spaced frame numbers for the contact sheet, ends included.

    Both ends matter: the first frame is what proves the capture started on
    noise and the last is what proves it finished on the mark.
    """
    if total < 1:
        raise ValueError("no frames to sheet")
    if total == 1 or tiles < 2:
        return [0]
    tiles = min(tiles, total)
    step = (total - 1) / (tiles - 1)
    return sorted({int(round(i * step)) for i in range(tiles)})


# ---------------------------------------------------------------------------
# Stage 1 -- fold. Runs under .venvs/venv-runner.
# ---------------------------------------------------------------------------

def stage_fold(args):
    """Run one descent on a chip and write the trajectory to `--npz`."""
    sys.path.insert(0, str(REPO_ROOT))

    import numpy as np

    import mark
    from protocol.events import unpack_coords
    from runner.env import runner_environ

    # BEFORE the device is opened, exactly as make-thumbnails.py does it:
    # without this tt-metal writes tens of MB of `generated/` into the
    # working directory, which is the repo root here.
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    log_root = Path(os.environ.get("TT_BIO_DEMO_LOG_ROOT")
                    or runtime_dir / "tt-bio-demo" / "logs")
    log_root.mkdir(parents=True, exist_ok=True)
    os.environ.update(runner_environ(log_root))

    from runner.egg import run_egg
    from tt_bio.tenstorrent import cleanup, get_device

    frames, steps = [], []

    def emit(event):
        # `run_egg` emits nothing else today, but it is the worker's job and
        # not this script's to know that.
        if event.get("type") != "egg_frame":
            return
        frames.append(unpack_coords(event["coords_b64"]))
        steps.append(event["step"])

    log(f"opening card {args.device}...")
    device = get_device()
    try:
        seed = run_egg(device, emit, egg_id="record-egg", card=args.device,
                       seed=args.seed)
    finally:
        # Always, including on failure: a script that leaves a card held
        # takes the booth down with it.
        cleanup()
        log("device closed")

    if not frames:
        raise SystemExit("the chip emitted no frames at all")

    params = mark.run_parameters(seed)
    coords = np.stack(frames).astype(np.float32)
    settled = settled_fraction(coords[-1], params.scale)
    log(f"{len(frames)} frames, seed {seed}, "
        f"{settled * 100:.1f}% of points settled inside the mark")
    if settled < args.min_settled:
        raise SystemExit(
            f"REFUSING: the cloud the chip returned is not the mark -- "
            f"{settled * 100:.1f}% of points are inside it, under the "
            f"{args.min_settled * 100:.0f}% floor. Nothing was encoded.")

    out = Path(args.npz)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, coords=coords, steps=np.array(steps),
                        seed=seed, scale=params.scale)
    log(f"wrote {out} {coords.shape}")
    print(json.dumps({"npz": str(out), "seed": int(seed),
                      "frames": len(frames), "settled": settled}))
    return 0


# ---------------------------------------------------------------------------
# Stage 2 -- render. Runs under .venvs/venv-ui.
# ---------------------------------------------------------------------------

def stage_render(args):
    """Draw every frame with the booth's own viewer, one PNG each.

    There is no offscreen path to reuse -- `Gtk.GLArea` compiles its shaders
    in `realize`, which needs a real surface, and GTK4 dropped
    `Gtk.OffscreenWindow` -- so this does what make-thumbnails.py already
    does against this compositor: a real window, and `glReadPixels` from
    inside the render handler where the GLArea's context is current and its
    FBO is bound.

    One capture per source frame, strictly in order. `pending` is what
    enforces that: the pump refuses to advance while a capture is
    outstanding, so a slow frame delays the video rather than dropping a
    step out of the middle of the descent.
    """
    sys.path.insert(0, str(REPO_ROOT))

    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    import numpy as np
    from OpenGL import GL
    from PIL import Image

    from ui.viewer import StructureViewer

    data = np.load(args.npz)
    coords = data["coords"]
    plan = frame_plan(len(coords), fps=args.fps, hold_s=args.hold_s)
    frames_dir = Path(args.frames_dir)
    if frames_dir.exists():
        # A shorter run must not leave a longer run's tail behind for ffmpeg
        # to pick up -- the glob would silently splice two takes together.
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    log(f"{len(coords)} source frames -> {len(plan)} output frames "
        f"at {args.fps} fps ({len(plan) / args.fps:.1f}s)")

    pending = {"index": None}
    written = {"n": 0}
    original_on_render = StructureViewer._on_render

    def patched_on_render(self, area, context):
        result = original_on_render(self, area, context)
        index = pending["index"]
        if index is None or not getattr(self, "_ready", False):
            return result
        scale = area.get_scale_factor() or 1
        pw = max(1, area.get_width() * scale)
        ph = max(1, area.get_height() * scale)
        raw = GL.glReadPixels(0, 0, pw, ph, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
        arr = np.frombuffer(raw, dtype=np.uint8)
        if arr.size != pw * ph * 3:
            return result
        # glReadPixels' origin is bottom-left; a PNG's is top-left.
        pixels = np.flipud(arr.reshape(ph, pw, 3))
        Image.fromarray(pixels, mode="RGB").save(
            frames_dir / (FRAME_GLOB % index))
        written["n"] += 1
        pending["index"] = None
        return result

    state = {"next": 0, "stalled": 0, "error": None}
    app = Gtk.Application(application_id="com.tenstorrent.ttbiodemo.recordegg")

    def on_activate(_app):
        window = Gtk.ApplicationWindow(application=_app)
        window.set_default_size(args.width, args.height)
        viewer = configure_egg_viewer(StructureViewer())
        viewer.set_hexpand(True)
        viewer.set_vexpand(True)
        window.set_child(viewer)
        window.present()

        def pump():
            if pending["index"] is not None:
                state["stalled"] += 1
                if state["stalled"] > 300:      # ~2.4s on one frame
                    state["error"] = (
                        f"the viewer stopped rendering at frame "
                        f"{state['next'] - 1}")
                    _app.quit()
                    return False
                viewer.queue_draw()
                return True
            state["stalled"] = 0
            i = state["next"]
            if i >= len(plan):
                _app.quit()
                return False
            viewer.set_points(coords[plan[i]])
            pending["index"] = i
            state["next"] = i + 1
            viewer.queue_draw()
            if i % (args.fps * 2) == 0:
                log(f"frame {i}/{len(plan)}")
            return True

        GLib.timeout_add(8, pump)

    StructureViewer._on_render = patched_on_render
    try:
        app.connect("activate", on_activate)
        app.run([])
    finally:
        StructureViewer._on_render = original_on_render

    if state["error"]:
        raise SystemExit(state["error"])
    if written["n"] != len(plan):
        raise SystemExit(
            f"captured {written['n']} frames of {len(plan)}; nothing encoded")
    log(f"wrote {written['n']} PNG(s) to {frames_dir}")
    print(json.dumps({"frames_dir": str(frames_dir), "frames": written["n"]}))
    return 0


# ---------------------------------------------------------------------------
# Encoding and the contact sheet.
# ---------------------------------------------------------------------------

def write_contact_sheet(frames_dir, out_path, tiles=8, width=320):
    """Tile evenly spaced captured frames into one PNG, for a human to read.

    Runs under the UI venv (it needs PIL) and is the last thing this script
    does, because it is the thing the operator is supposed to act on.
    """
    from PIL import Image

    paths = sorted(Path(frames_dir).glob("f*.png"))
    if not paths:
        raise SystemExit(f"no frames in {frames_dir} to sheet")
    picked = [paths[i] for i in contact_sheet_indices(len(paths), tiles)]
    thumbs = []
    for path in picked:
        image = Image.open(path)
        height = max(1, round(width * image.height / image.width))
        thumbs.append(image.resize((width, height), Image.LANCZOS))
    columns = min(4, len(thumbs))
    rows = (len(thumbs) + columns - 1) // columns
    tw, th = thumbs[0].size
    sheet = Image.new("RGB", (columns * tw, rows * th))
    for index, thumb in enumerate(thumbs):
        sheet.paste(thumb, ((index % columns) * tw, (index // columns) * th))
    sheet.save(out_path)
    return out_path, [p.name for p in picked]


def run(command, **kwargs):
    """Run a subprocess and raise SystemExit with its own message on failure."""
    result = subprocess.run(command, cwd=REPO_ROOT, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"failed ({result.returncode}): {' '.join(command)}")
    return result


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def log(message):
    print(f"[record-egg] {message}", file=sys.stderr, flush=True)


def stage_all(args):
    """Fold (unless given a trajectory), render, encode, then sheet."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not on PATH; nothing here can encode")
    needed = [("ui", VENV_UI)] + ([] if args.from_npz else
                                  [("runner", VENV_RUNNER)])
    for label, python in needed:
        if not python.exists():
            raise SystemExit(
                f"missing the {label} venv at {python}; run "
                "./scripts/setup-venvs.sh first, or set TT_BIO_DEMO_PREFIX "
                "to a checkout that already has them")

    npz = Path(args.from_npz) if args.from_npz else Path(args.npz)
    if args.from_npz:
        log(f"skipping the chip; re-rendering {npz}")
    else:
        log("stage 1/2: running the descent on the chip")
        run([str(VENV_RUNNER), str(Path(__file__).resolve()),
             "--stage", "fold", "--npz", str(npz),
             "--device", str(args.device),
             "--min-settled", str(args.min_settled)]
            + (["--seed", str(args.seed)] if args.seed is not None else []))

    log("stage 2/2: drawing it with the booth's own viewer")
    run([str(VENV_UI), str(Path(__file__).resolve()),
         "--stage", "render", "--npz", str(npz),
         "--frames-dir", str(args.frames_dir),
         "--fps", str(args.fps), "--hold-s", str(args.hold_s),
         "--width", str(args.width), "--height", str(args.height)])

    out = Path(args.out) if args.out else None
    if out is None:
        seconds = round(len(sorted(Path(args.frames_dir).glob("f*.png")))
                        / args.fps)
        RECORDINGS.mkdir(parents=True, exist_ok=True)
        out = RECORDINGS / f"tenstorrent-mark-on-chip-{seconds}s.mp4"
    log(f"encoding {out}")
    run(encode_command(args.frames_dir, out, fps=args.fps, crf=args.crf),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if args.gif:
        gif = out.with_suffix(".gif")
        palette = Path(args.frames_dir) / "palette.png"
        for command in gif_commands(out, gif, palette,
                                    fps=args.gif_fps, width=args.gif_width):
            run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"wrote {gif} ({gif.stat().st_size / 1e6:.1f} MB)")

    sheet = out.with_name(out.stem + "-contact.png")
    # Its own stage under the UI venv, because PIL lives there and this
    # driver may be running under either interpreter.
    run([str(VENV_UI), str(Path(__file__).resolve()),
         "--stage", "sheet", "--frames-dir", str(args.frames_dir),
         "--sheet", str(sheet)])

    log(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    log(f"NOW LOOK AT {sheet} -- a capture nobody has looked at is not "
        f"verified (CLAUDE.md), and this script cannot look for you.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--stage", choices=("fold", "render", "sheet"),
        help="run only one stage (the driver sets this when it re-executes "
             "itself under each venv; you rarely want it by hand)")
    parser.add_argument("--out", help="output mp4 (default: recordings/"
                                      "tenstorrent-mark-on-chip-<N>s.mp4)")
    parser.add_argument("--seed", type=int, default=None,
                        help="reproduce an exact run; default is a fresh draw")
    parser.add_argument("--device", type=int, default=0,
                        help="which chip to run the descent on (default 0)")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--hold-s", type=float, default=DEFAULT_HOLD_S,
                        help="seconds to hold on the finished mark")
    parser.add_argument("--width", type=int, default=DEFAULT_W)
    parser.add_argument("--height", type=int, default=DEFAULT_H)
    parser.add_argument("--crf", type=int, default=DEFAULT_CRF)
    parser.add_argument("--gif", action="store_true",
                        help="also write a GIF beside the mp4")
    parser.add_argument("--gif-fps", type=int, default=DEFAULT_GIF_FPS)
    parser.add_argument("--gif-width", type=int, default=DEFAULT_GIF_WIDTH)
    parser.add_argument("--min-settled", type=float,
                        default=DEFAULT_MIN_SETTLED,
                        help="refuse if less of the cloud than this landed "
                             "inside the mark (default 0.90)")
    parser.add_argument("--npz", default=str(Path(
        os.environ.get("TMPDIR", "/tmp")) / "tt-bio-egg-frames.npz"),
        help="where the trajectory is written between the two stages")
    parser.add_argument("--from-npz", default=None,
                        help="skip the chip and re-render this trajectory; "
                             "runs with no hardware at all")
    parser.add_argument("--frames-dir", default=str(Path(
        os.environ.get("TMPDIR", "/tmp")) / "tt-bio-egg-frames"))
    parser.add_argument("--sheet", default=None,
                        help=argparse.SUPPRESS)   # set by the driver
    args = parser.parse_args(argv)

    if args.stage == "fold":
        return stage_fold(args)
    if args.stage == "render":
        return stage_render(args)
    if args.stage == "sheet":
        path, names = write_contact_sheet(args.frames_dir, args.sheet)
        log(f"contact sheet {path} ({', '.join(names)})")
        return 0
    return stage_all(args)


if __name__ == "__main__":
    sys.exit(main())
