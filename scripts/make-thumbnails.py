#!/usr/bin/env python3
"""Render `playlist/thumbnails/<id>.png` for every target in the manifest.

Run it from the repo root with no arguments:

    ./scripts/make-thumbnails.py

Every thumbnail is a picture of a protein this booth ACTUALLY FOLDED. There
is no stock art here and no hand-drawn approximation: the script folds each
manifest target on the Tenstorrent card, then draws the resulting structure
with the booth's own renderer -- `ui.structure_view.structure_mesh` for the
ribbon and `ui.viewer.StructureViewer` for the paint, the same two modules
that draw the hero view a visitor watches. A gallery card therefore shows
the same molecule, in the same pLDDT colours, that pressing it produces.

That also means the thumbnails inherit the folds' honesty. Three of the
four targets are `msa: empty` and land at mean pLDDT 40-56, so their ribbons
come out mostly orange and yellow rather than confident blue. That is what
this model predicts for these inputs, and a prettier thumbnail would be a
different (and false) claim.

WHY TWO PROCESSES
-----------------
The two halves cannot share an interpreter. `.venvs/venv-runner` has torch,
ttnn and tt_bio but no gi/PyOpenGL/gemmi; `.venvs/venv-ui` has GTK and the
renderer but must never import torch (the project's standing rule). So this
script re-executes itself:

    stage `fold`    under .venvs/venv-runner  -> writes a JSON id->CIF map
    stage `render`  under .venvs/venv-ui      -> reads it, writes the PNGs

Run with no `--stage` and it drives both in order, which is the intended
entry point. `scripts/run-demo.sh` is the precedent for one script knowing
that both venvs exist.

REPEATABILITY
-------------
Written to be re-run as the playlist grows -- it reads the manifest rather
than a hardcoded list, so a fifth target needs no edit here. `--only ID`
re-does a single target (useful after changing the camera or the ramp);
`--from-cif ID=PATH` skips folding for a target whose structure you already
have, which is also how the render stage can be exercised with no hardware
at all.

HARDWARE
--------
The fold stage opens card 0 and loads protenix-v2 once, then folds every
target warm. Budget roughly a minute for the model load plus the manifest's
own `expected_s` per target (4.4 / 11.7 / 19.7 / 22.3s today). It always
closes the device, including on failure, so it never leaves a card held.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "playlist" / "manifest.yaml"
THUMBNAIL_DIR = REPO_ROOT / "playlist" / "thumbnails"

# Where the two venvs live. `TT_BIO_DEMO_PREFIX` is scripts/test.sh's own
# override, reused here rather than inventing a second spelling -- and it is
# what makes this runnable from a git worktree, where `.venvs/` (gitignored)
# does not exist: point it at the main checkout.
VENV_PREFIX = Path(os.environ.get("TT_BIO_DEMO_PREFIX")
                   or REPO_ROOT / ".venvs")
VENV_RUNNER = VENV_PREFIX / "venv-runner" / "bin" / "python3"
VENV_UI = VENV_PREFIX / "venv-ui" / "bin" / "python3"

# Rendered at 2x the 320x200 the gallery displays (ui/gallery.py's
# `_THUMBNAIL_WIDTH_PX`/`_THUMBNAIL_HEIGHT_PX`) and downsampled, which is
# both a cheap antialias and correct on a HiDPI booth screen. The 1.6 aspect
# ratio is load-bearing: `_load_thumbnail_texture` scales with
# `preserve_aspect_ratio=False`, so anything else arrives stretched.
RENDER_W, RENDER_H = 640, 400
THUMB_W, THUMB_H = 320, 200

# The packaging plan asserts every shipped thumbnail is under this. PNGs of
# a ribbon on a flat ground compress to a small fraction of it.
MAX_THUMBNAIL_BYTES = 400_000


# ---------------------------------------------------------------------------
# Shared: reading the playlist. Safe in both venvs (ui.playlist is pure
# Python + yaml, and imports neither GTK nor torch).
# ---------------------------------------------------------------------------

def load_targets(only=None):
    sys.path.insert(0, str(REPO_ROOT))
    from ui.playlist import load_playlist

    targets = load_playlist(MANIFEST)
    if only:
        wanted = set(only)
        known = {t.id for t in targets}
        missing = wanted - known
        if missing:
            raise SystemExit(
                f"--only names no such target: {', '.join(sorted(missing))} "
                f"(manifest has {', '.join(sorted(known))})")
        targets = [t for t in targets if t.id in wanted]
    return targets


# ---------------------------------------------------------------------------
# Stage 1 -- fold. Runs under .venvs/venv-runner.
# ---------------------------------------------------------------------------

def stage_fold(args):
    """Fold every selected target once and print a JSON {id: cif_path} map.

    One `Folder` for the whole run, so the model loads once and every fold
    after the first is warm -- the same state the booth's attract loop keeps
    the card in.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from runner.env import runner_environ
    from runner.folder import Folder

    # BEFORE the device is opened: without this, tt-metal writes tens of MB
    # of `generated/` into the current directory -- which is the repo root
    # here. Same default and same override as scripts/run-demo.sh, so this
    # lands where every other tt-metal log from this project lands rather
    # than in the working tree.
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    log_root = Path(os.environ.get("TT_BIO_DEMO_LOG_ROOT")
                    or runtime_dir / "tt-bio-demo" / "logs")
    log_root.mkdir(parents=True, exist_ok=True)
    os.environ.update(runner_environ(log_root))

    targets = load_targets(args.only)
    preset = dict(pair.split("=", 1) for pair in args.from_cif)

    result = {}
    to_fold = [t for t in targets if t.id not in preset]
    for target in targets:
        if target.id in preset:
            result[target.id] = str(Path(preset[target.id]).resolve())
            log(f"{target.id}: using supplied CIF {result[target.id]}")

    if not to_fold:
        print(json.dumps(result))
        return 0

    folder = Folder(device_id=0)
    log(f"loading protenix-v2 on card 0 for {len(to_fold)} fold(s)...")
    folder.load()
    try:
        for target in to_fold:
            events = []
            log(f"folding {target.id} ({target.name})...")
            folder.fold(f"thumb-{target.id}", str(target.input_path),
                        events.append, target_id=target.id,
                        n_residues=0, card=0)
            done = events[-1]
            if done.get("type") != "job_done" or not done.get("cif_path"):
                raise SystemExit(
                    f"{target.id}: fold produced no structure "
                    f"(last event {done.get('type')!r})")
            result[target.id] = done["cif_path"]
            log(f"{target.id}: {done['cif_path']} "
                f"(mean pLDDT {done.get('mean_plddt', float('nan')):.1f}, "
                f"{done.get('wall_s', float('nan')):.1f}s)")
    finally:
        # Always, including on a failed fold: a script that leaves a card
        # held takes the booth down with it.
        folder.close()
        log("device closed")

    print(json.dumps(result))
    return 0


# ---------------------------------------------------------------------------
# Stage 2 -- render. Runs under .venvs/venv-ui.
# ---------------------------------------------------------------------------

def stage_render(args):
    """Draw each CIF with the booth's own renderer and write the PNGs.

    Reads the {id: cif_path} map on stdin.

    There is no offscreen path to reuse: `Gtk.GLArea` compiles its shaders
    in `realize`, which needs a real surface, and GTK4 dropped
    `Gtk.OffscreenWindow`. So this does what
    tests/fixtures/streams/replay_real_fold_ui.py already does against this
    same compositor -- puts the viewer in a real window and reads the
    framebuffer back from inside the `render` handler, where the GLArea's
    context is current and its FBO is bound.
    """
    sys.path.insert(0, str(REPO_ROOT))

    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    import numpy as np
    from OpenGL import GL
    from PIL import Image

    # The booth's own renderer, which since the cartoon landed means
    # ui.structure_view -- cartoon plus any bound ligand. A thumbnail
    # built from a different renderer than the booth uses is a picture
    # of a booth that does not exist.
    from ui.structure_view import structure_mesh as ribbon_from_cif
    from ui.viewer import StructureViewer

    cif_by_id = json.loads(sys.stdin.read())
    targets = load_targets(args.only)
    THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)

    # Each target gets a window, a capture, and a teardown, one at a time.
    # `captured` is written from inside the render handler.
    written = []
    failures = []

    for target in targets:
        cif = cif_by_id.get(target.id)
        if not cif:
            failures.append(f"{target.id}: no CIF in the fold stage's output")
            continue
        out = THUMBNAIL_DIR / f"{target.id}.png"
        try:
            _render_one(target, Path(cif), out, gi=gi, Gtk=Gtk, GLib=GLib,
                        np=np, GL=GL, Image=Image,
                        ribbon_from_cif=ribbon_from_cif,
                        StructureViewer=StructureViewer)
        except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
            failures.append(f"{target.id}: {exc}")
            continue
        size = out.stat().st_size
        if size > MAX_THUMBNAIL_BYTES:
            failures.append(
                f"{target.id}: {out.name} is {size} bytes, over the "
                f"{MAX_THUMBNAIL_BYTES}-byte ship limit")
            continue
        written.append((target.id, out, size))
        log(f"{target.id}: wrote {out.relative_to(REPO_ROOT)} ({size} bytes)")

    for line in failures:
        log(f"FAILED {line}")
    if failures:
        return 1
    log(f"{len(written)} thumbnail(s) written to "
        f"{THUMBNAIL_DIR.relative_to(REPO_ROOT)}")
    return 0


def _render_one(target, cif_path, out_path, *, gi, Gtk, GLib, np, GL, Image,
                ribbon_from_cif, StructureViewer):
    """One window, one frame, one PNG."""
    vertices, normals, colors, indices = ribbon_from_cif(cif_path)

    captured = {}

    def read_framebuffer(area):
        scale = area.get_scale_factor() or 1
        pw = max(1, area.get_width() * scale)
        ph = max(1, area.get_height() * scale)
        raw = GL.glReadPixels(0, 0, pw, ph, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
        arr = np.frombuffer(raw, dtype=np.uint8)
        if arr.size != pw * ph * 3:
            return None
        # glReadPixels' origin is bottom-left; a PNG's is top-left.
        return np.flipud(arr.reshape(ph, pw, 3))

    original_on_render = StructureViewer._on_render

    def patched_on_render(self, area, context):
        result = original_on_render(self, area, context)
        if "pixels" not in captured and getattr(self, "_ready", False):
            pixels = read_framebuffer(area)
            if pixels is not None:
                captured["pixels"] = pixels
        return result

    app = Gtk.Application(application_id="com.tenstorrent.ttbiodemo.thumbs")
    state = {"error": None}

    def on_activate(_app):
        window = Gtk.ApplicationWindow(application=_app)
        window.set_default_size(RENDER_W, RENDER_H)
        viewer = StructureViewer()
        viewer.set_hexpand(True)
        viewer.set_vexpand(True)
        window.set_child(viewer)
        window.present()

        viewer.set_ribbon(vertices, normals, colors, indices)
        # The ribbon is invisible at blend 0 and `begin_crossfade` would need
        # ~0.8s of frame ticks to get there; a still just wants it fully on.
        viewer.set_blend(1.0)
        # A still must not depend on WHEN the shutter fired: stop the spin so
        # every target is captured at the same pose and a re-run of this
        # script reproduces the same image.
        viewer.stop_animation()
        viewer.queue_draw()

        deadline = {"ticks": 0}

        def poll():
            deadline["ticks"] += 1
            if "pixels" in captured:
                _app.quit()
                return False
            if deadline["ticks"] > 200:  # ~10s
                state["error"] = "no frame was captured"
                _app.quit()
                return False
            viewer.queue_draw()
            return True

        GLib.timeout_add(50, poll)

    StructureViewer._on_render = patched_on_render
    try:
        app.connect("activate", on_activate)
        app.run([])
    finally:
        StructureViewer._on_render = original_on_render

    if state["error"]:
        raise RuntimeError(state["error"])
    if "pixels" not in captured:
        raise RuntimeError("the viewer never rendered a frame")

    image = Image.fromarray(captured["pixels"], mode="RGB")
    image = image.resize((THUMB_W, THUMB_H), Image.LANCZOS)
    image.save(out_path, format="PNG", optimize=True)


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def log(message):
    print(f"[make-thumbnails] {message}", file=sys.stderr, flush=True)


def stage_all(args):
    """Drive both stages, each in its own venv."""
    for label, python in (("runner", VENV_RUNNER), ("ui", VENV_UI)):
        if not python.exists():
            raise SystemExit(
                f"missing the {label} venv at {python}; run "
                "./scripts/setup-venvs.sh first, or set TT_BIO_DEMO_PREFIX "
                "to a checkout that already has them (which is also how to "
                "run this from a git worktree, where .venvs/ does not exist)")

    passthrough = []
    for value in args.only:
        passthrough += ["--only", value]
    for value in args.from_cif:
        passthrough += ["--from-cif", value]

    log("stage 1/2: folding on the card")
    fold = subprocess.run(
        [str(VENV_RUNNER), str(Path(__file__).resolve()),
         "--stage", "fold"] + passthrough,
        cwd=REPO_ROOT, stdout=subprocess.PIPE, text=True)
    if fold.returncode != 0:
        return fold.returncode
    cif_map = fold.stdout.strip().splitlines()[-1]

    log("stage 2/2: rendering with the booth's own renderer")
    render = subprocess.run(
        [str(VENV_UI), str(Path(__file__).resolve()),
         "--stage", "render"] + passthrough,
        cwd=REPO_ROOT, input=cif_map, text=True)
    return render.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--stage", choices=("fold", "render"),
        help="run only one stage (the driver sets this when it re-executes "
             "itself under each venv; you rarely want it by hand)")
    parser.add_argument(
        "--only", action="append", default=[], metavar="ID",
        help="only this manifest target; repeatable")
    parser.add_argument(
        "--from-cif", action="append", default=[], metavar="ID=PATH",
        help="skip folding this target and draw the given CIF instead; "
             "repeatable. Lets the render stage run with no hardware.")
    args = parser.parse_args(argv)

    if args.stage == "fold":
        return stage_fold(args)
    if args.stage == "render":
        return stage_render(args)
    return stage_all(args)


if __name__ == "__main__":
    sys.exit(main())
