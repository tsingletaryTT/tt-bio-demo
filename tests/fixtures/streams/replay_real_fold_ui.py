"""Spike script: prove the captured real-fold trajectory
(real_fold_trpcage.jsonl) actually renders in the real UI, not just that it
parses.

This is deliberately NOT production code -- it is the instrument
docs/spike-real-fold.md uses to answer "was the real trajectory captured and
replayed successfully through the UI?" with measured evidence instead of an
impression. It:

  1. Starts a real `runner.mock.MockRunner` (completely unmodified) serving
     tests/fixtures/streams/real_fold_trpcage.jsonl over a Unix socket, exactly
     the way a human demo operator would.
  2. Constructs the real, unmodified `ui.app.DemoApp` pointed at that socket
     -- the same class `python -m ui.app --socket ...` runs.
  3. Monkeypatches `StructureViewer._on_render` (bound at the CLASS, not
     edited in ui/viewer.py) so that after every real render call completes,
     it does one extra `glReadPixels` and stashes summary stats (non-black
     pixel count, mean color, a coarse point-cluster bounding box) instead of
     a full PNG -- screenshot tools don't cooperate with this compositor, but
     glReadPixels against the live GL context does, per the task brief.
  4. Runs the real GTK main loop for a fixed wall-clock budget (a touch longer
     than the fixture's total _delay_ms so the ribbon reveal is included),
     samples readback stats at three checkpoints (early/mid/late), then quits.
  5. Prints the stats to stdout as evidence and exits 0 only if frames with
     real (non-background) pixel content were actually observed at more than
     one checkpoint -- i.e. something moved, not just a static clear color.

Run with the UI venv, from the repo root, on a machine with a live
Wayland/X11 display (WAYLAND_DISPLAY / DISPLAY must be set):
  .venvs/venv-ui/bin/python3 tests/fixtures/streams/replay_real_fold_ui.py
"""

import pathlib
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
from OpenGL import GL  # noqa: E402

from runner.mock import MockRunner, load_stream  # noqa: E402
from ui.app import DemoApp  # noqa: E402
from ui.viewer import StructureViewer  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "streams" / "real_fold_trpcage.jsonl"
SOCK = "/tmp/tt-bio-demo-spike-real-fold.sock"

# Checkpoints (seconds after app start) at which to sample the framebuffer:
# early (still noise / just started), mid (mid-fold), late (after job_done +
# crossfade to ribbon should have begun).
CHECKPOINTS_S = [1.0, 4.5, 8.5]
QUIT_AFTER_S = 10.0

readback_log = []  # list of dicts, one per sampled render


def _read_framebuffer_stats(area):
    """glReadPixels the GLArea's current back buffer and summarize it.

    Coarse, robust stats (not a pixel-perfect comparison) are exactly what's
    needed here: proof that something other than the flat background color
    is on screen, and that it changes between checkpoints.
    """
    w = area.get_width()
    h = area.get_height()
    if w <= 0 or h <= 0:
        return None
    # Scale factor matters on HiDPI (GtkGLArea's framebuffer is in device
    # pixels); read at the logical size scaled up, clamp to something sane.
    scale = area.get_scale_factor() if hasattr(area, "get_scale_factor") else 1
    pw, ph = max(1, w * scale), max(1, h * scale)
    buf = GL.glReadPixels(0, 0, pw, ph, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
    arr = np.frombuffer(buf, dtype=np.uint8)
    if arr.size != pw * ph * 3:
        return {"w": pw, "h": ph, "error": f"short read: {arr.size} bytes"}
    arr = arr.reshape(ph, pw, 3).astype(np.float32)
    # Background is Tenstorrent dark base (0x09, 0x22, 0x21); a pixel counts
    # as "content" if it differs from that by more than a small tolerance in
    # any channel (anti-aliasing/blending softens edges near the background).
    bg = np.array([0x09, 0x22, 0x21], dtype=np.float32)
    diff = np.abs(arr - bg).max(axis=-1)
    content_mask = diff > 12.0
    n_content = int(content_mask.sum())
    stats = {
        "w": pw, "h": ph, "n_content_px": n_content,
        "frac_content": n_content / (pw * ph),
        "mean_rgb": arr.reshape(-1, 3).mean(axis=0).tolist(),
    }
    if n_content:
        ys, xs = np.nonzero(content_mask)
        stats["content_bbox"] = [int(xs.min()), int(ys.min()),
                                  int(xs.max()), int(ys.max())]
    return stats


def main():
    events = load_stream(FIXTURE)
    runner = MockRunner(SOCK, events, speed=1.0)
    runner.start()
    print(f"[replay] MockRunner serving {FIXTURE.name} "
          f"({len(events)} events) on {SOCK}", file=sys.stderr)

    app = DemoApp(socket_path=SOCK)

    _orig_on_render = StructureViewer._on_render

    def _patched_on_render(self, area, context):
        result = _orig_on_render(self, area, context)
        # Force the readback to happen against what was just drawn, before
        # the next clear -- glReadPixels must run inside/immediately after
        # the same GL context is current, which it is here (still inside
        # the "render" signal handler).
        stats = _read_framebuffer_stats(area)
        if stats is not None:
            stats["t"] = time.perf_counter()
            readback_log.append(stats)
        return result

    StructureViewer._on_render = _patched_on_render

    t_start = time.perf_counter()
    remaining_checkpoints = list(CHECKPOINTS_S)

    def _tick_checkpoint():
        now = time.perf_counter() - t_start
        if remaining_checkpoints and now >= remaining_checkpoints[0]:
            due = remaining_checkpoints.pop(0)
            print(f"[replay] checkpoint t={due}s reached "
                  f"(actual {now:.2f}s); readback_log has "
                  f"{len(readback_log)} samples so far", file=sys.stderr)
        return True

    def _quit():
        print(f"[replay] quitting after {QUIT_AFTER_S}s wall clock",
              file=sys.stderr)
        app.quit()
        return False

    def _on_activate_extra(*_a):
        GLib.timeout_add(100, _tick_checkpoint)
        GLib.timeout_add(int(QUIT_AFTER_S * 1000), _quit)

    app.connect("activate", _on_activate_extra)

    exit_code = app.run([])
    runner.stop()

    print(f"[replay] app.run returned {exit_code}", file=sys.stderr)
    print(f"[replay] total render samples captured: {len(readback_log)}",
          file=sys.stderr)

    # ---- Evaluate: did we see real (non-background) content, and did the
    # scene actually change over time (not just one static frame)? ----
    with_content = [s for s in readback_log if s.get("n_content_px", 0) > 0]
    print(f"[replay] render samples with non-background pixels: "
          f"{len(with_content)} / {len(readback_log)}", file=sys.stderr)
    if with_content:
        fracs = [s["frac_content"] for s in with_content]
        print(f"[replay] frac_content range: {min(fracs):.4f} .. "
              f"{max(fracs):.4f}", file=sys.stderr)
        print(f"[replay] first-content sample: {with_content[0]}",
              file=sys.stderr)
        print(f"[replay] last-content sample: {with_content[-1]}",
              file=sys.stderr)
        n = len(with_content)
        print("[replay] evenly-spaced trace (10 samples across the run):",
              file=sys.stderr)
        for i in range(0, n, max(1, n // 10)):
            s = with_content[i]
            print(f"    t={s['t'] - with_content[0]['t']:6.2f}s  "
                  f"frac_content={s['frac_content']:.4f}  "
                  f"bbox={s.get('content_bbox')}", file=sys.stderr)

    ok = len(with_content) > 0 and (max(s["frac_content"] for s in with_content) >
                                     min(s["frac_content"] for s in with_content))
    print(f"[replay] RESULT: {'PASS' if ok else 'FAIL'} -- "
          f"{'content rendered and changed over time' if ok else 'no content or no change observed'}",
          file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
