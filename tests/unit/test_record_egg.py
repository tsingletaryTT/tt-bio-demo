"""What `scripts/record-egg.py` decides, tested without a chip or a display.

The script's two stages need hardware and GTK respectively and are not
tested here. What IS testable is everything that decides what the video
contains -- which frames, in which order, encoded how, and whether a bad
capture is refused -- and that is where its failure modes have historically
been (see the script's own docstring: every capture failure this project has
had reported success).

Each test names the mutation it must catch, and each was confirmed red
against it.

Loaded by path because the script is a hyphenated executable rather than an
importable module name, which is the same reason tests/unit/test_packaging.py
reads its scripts as text.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "record_egg", REPO / "scripts" / "record-egg.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


record_egg = _load()


# ── the frame plan ──────────────────────────────────────────────────────────

def test_the_descent_is_played_once_in_order():
    """Mutation: shuffling, reversing or dropping a step in the plan."""
    plan = record_egg.frame_plan(181, fps=30, hold_s=0.0)
    assert plan == list(range(181))


def test_the_hold_holds_the_finished_mark_not_the_noise():
    """THE ONE THAT MATTERS. The hold exists so the video does not cut the
    instant the last point lands -- so it must repeat the LAST frame.

    Mutations, each red: holding `0` (the video ends on the noise it started
    from), holding `total` (IndexError against a real trajectory), dropping
    the hold entirely (caught by the length assertion below).
    """
    plan = record_egg.frame_plan(181, fps=30, hold_s=2.0)
    assert len(plan) == 181 + 60
    assert plan[:181] == list(range(181))
    assert set(plan[181:]) == {180}
    # And it is a real index into a real trajectory, not one past the end.
    coords = np.zeros((181, 4, 3), dtype=np.float32)
    assert coords[plan[-1]].shape == (4, 3)


def test_the_hold_scales_with_the_frame_rate():
    """Two seconds is two seconds at any fps. Mutation: a constant 60-frame
    hold, which is only two seconds at 30 fps."""
    assert len(record_egg.frame_plan(10, fps=60, hold_s=2.0)) == 10 + 120
    assert len(record_egg.frame_plan(10, fps=15, hold_s=2.0)) == 10 + 30


def test_an_empty_trajectory_is_refused_rather_than_encoded_as_nothing():
    with pytest.raises(ValueError):
        record_egg.frame_plan(0)


# ── the refusal ─────────────────────────────────────────────────────────────
#
# These drive `mark.py`'s real field, so they measure the same thing the
# script measures rather than a stand-in for it.

def test_a_settled_cloud_and_a_noise_cloud_land_on_opposite_sides_of_the_floor():
    """The floor is only meaningful if the two cases it separates really are
    separated. Both clouds here are built from mark.py itself: one descended,
    one not.

    Mutation: `settled_fraction` forgetting to divide by `scale` -- the
    settled cloud then reads as 24x too large and scores ~0, and the script
    would refuse every good capture it ever made.
    """
    import mark

    params = mark.run_parameters(seed=20260814, count=2000)
    run = mark.MarkCondensation(params=params)
    noise = run.points()
    while not run.done:
        run.step()
    settled = run.points()

    assert record_egg.settled_fraction(noise, params.scale) < 0.10
    assert record_egg.settled_fraction(settled, params.scale) > 0.90
    # The floor the script ships sits between them, not on either.
    assert (record_egg.settled_fraction(noise, params.scale)
            < record_egg.DEFAULT_MIN_SETTLED
            < record_egg.settled_fraction(settled, params.scale))


# ── the viewer configuration ────────────────────────────────────────────────

class _FakeViewer:
    """Records what was set on it. No GTK, no GL."""

    def __init__(self):
        self.point_color = None
        self.spin_rate = None

    def set_point_color(self, color):
        self.point_color = color

    def set_spin_rate(self, rate):
        self.spin_rate = rate


def test_the_recording_uses_the_booths_own_egg_viewer_settings():
    """A recording made through a differently configured viewer is a picture
    of a booth that does not exist -- which this project shipped once, when
    make-thumbnails.py drew with a renderer the booth had stopped using.

    Mutations, both red: any other point colour; a non-zero spin rate (the
    mark would rotate away from the camera mid-descent).
    """
    import mark

    viewer = record_egg.configure_egg_viewer(_FakeViewer())
    assert viewer.point_color == mark.BRAND_PURPLE
    assert viewer.spin_rate == 0.0


def test_the_point_colour_is_the_marks_own_constant_not_a_copied_hex():
    """`ui/app.py` sets the egg viewer's colour from `mark.BRAND_PURPLE`. A
    hex copied into the script would drift from the brand the moment that
    constant changed."""
    import mark

    source = (REPO / "scripts" / "record-egg.py").read_text()
    assert "BRAND_PURPLE" in source
    assert mark.BRAND_PURPLE_HEX.lower() not in source.lower(), \
        "the recording script hardcodes the brand hex instead of importing it"


# ── the encode ──────────────────────────────────────────────────────────────

def test_the_frame_rate_is_an_input_option():
    """Mutation: moving `-framerate` after `-i`, where ffmpeg ignores it and
    assembles the stills at its 25 fps default -- a six-second descent
    stretched to seven, silently, with a zero exit code."""
    command = record_egg.encode_command("/frames", "/out.mp4", fps=30)
    assert "-framerate" in command
    assert command.index("-framerate") < command.index("-i")
    assert command[command.index("-framerate") + 1] == "30"


def test_the_encode_is_playable_outside_ffplay():
    """Mutation: dropping `-pix_fmt yuv420p`. libx264 then picks yuv444p for
    RGB input, which Safari, QuickTime and most browsers refuse -- and the
    file looks perfect in ffplay."""
    command = record_egg.encode_command("/frames", "/out.mp4")
    assert "-pix_fmt" in command
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"


def test_the_encode_reads_the_frames_the_render_stage_actually_wrote():
    """Mutation: either half of the filename contract drifting. The render
    stage writes `f0000.png`; a pattern of `%d` or `frame%04d` matches
    nothing and ffmpeg fails with an unhelpful message."""
    command = record_egg.encode_command("/frames", "/out.mp4")
    pattern = command[command.index("-i") + 1]
    assert pattern == "/frames/" + record_egg.FRAME_GLOB
    assert record_egg.FRAME_GLOB % 7 == "f0007.png"


def test_the_gif_is_two_passes_with_its_own_palette():
    """Mutation: a single-pass GIF encode, which quantises to the fixed web
    palette and bands a purple cloud on a dark ground."""
    palette_cmd, gif_cmd = record_egg.gif_commands(
        "/in.mp4", "/out.gif", "/pal.png")
    assert "palettegen=stats_mode=diff" in " ".join(palette_cmd)
    assert "/pal.png" in palette_cmd
    assert "paletteuse" in " ".join(gif_cmd)
    assert gif_cmd.count("-i") == 2, "the gif pass must read the palette too"


def test_the_gif_shrinks_by_scale_and_never_by_frame_rate_below_the_video():
    """recordings/README.md's measured lesson: a GIF that must shrink loses
    duration or pixels, never frame rate -- dropping 12.5 to 6.25 fps is what
    made an earlier loop read as laggy."""
    _palette, gif_cmd = record_egg.gif_commands(
        "/in.mp4", "/out.gif", "/pal.png", fps=20, width=640)
    assert "fps=20" in " ".join(gif_cmd)
    assert "scale=640:-1" in " ".join(gif_cmd)
    assert record_egg.DEFAULT_GIF_FPS >= 20


# ── the contact sheet ───────────────────────────────────────────────────────

def test_the_contact_sheet_shows_both_ends():
    """The first frame is what proves the capture started on noise and the
    last is what proves it finished on the mark. A sheet missing either
    cannot answer the question it exists to answer.

    Mutation: `range(0, total, total // tiles)`, which never includes the
    last frame.
    """
    picked = record_egg.contact_sheet_indices(241, tiles=8)
    assert picked[0] == 0
    assert picked[-1] == 240
    assert len(picked) == 8
    assert picked == sorted(picked)


def test_the_contact_sheet_survives_a_trajectory_shorter_than_its_tiles():
    assert record_egg.contact_sheet_indices(3, tiles=8) == [0, 1, 2]
    assert record_egg.contact_sheet_indices(1, tiles=8) == [0]


# ── the standing rules this script must not break ───────────────────────────

def test_the_fold_stage_always_closes_the_device():
    """Nothing in this repo may leave a chip held. Mutation: moving
    `cleanup()` out of the `finally`, which leaks the card whenever a descent
    raises -- and takes the booth down with it."""
    source = (REPO / "scripts" / "record-egg.py").read_text()
    body = source[source.index("def stage_fold"):source.index("def stage_render")]
    assert "finally:" in body
    finally_block = body[body.index("finally:"):]
    assert "cleanup()" in finally_block.split("\n\n")[0], \
        "cleanup() must be in the finally, not on the happy path"


def test_the_script_never_downscales_the_video():
    """recordings/README.md measured this: the diffusion cloud is 1-2px dots,
    a 1920->1280 downscale destroys them, and h.264 smears the remains in a
    way that reads as stutter. The mp4 is encoded at capture resolution."""
    command = record_egg.encode_command("/frames", "/out.mp4")
    assert not any(arg.startswith("scale=") for arg in command)
    assert "-vf" not in command
