"""Task 11: the Tensix activity panel (ui/chipviz.py).

Two halves, deliberately:

- the PURE half (mode policy, AICLK normalisation, flow shaping, layout
  arithmetic, the generated page) needs no display, no WebKit and no
  hardware, and is where every decision this panel makes actually lives;
- the WIDGET half builds the real `ChipVizPanel` and checks the things that
  can only go wrong in a widget -- that it hides rather than errors when it
  cannot run, that its labels are legible, and above all that its poll source
  is added once and REMOVED, because the booth runs unattended all day.

No test here opens a Tenstorrent device. `chip_dirs`/`read_chip_clocks` read
`/sys/class/tenstorrent/*/tt_aiclk`, which is a passive attribute read, and
every test that cares about a specific chip count monkeypatches `SYSFS_ROOT`
to a temporary directory instead of depending on this machine's hardware.
"""

import json

import pytest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import _legibility  # noqa: E402
from ui import chipviz as chipviz_module  # noqa: E402
from ui.chipviz import (  # noqa: E402
    AICLK_BOOST_MHZ,
    AICLK_IDLE_MHZ,
    MAX_CHIPS,
    POLL_INTERVAL_MS,
    STAGE_STALE_AFTER_S,
    WORKING_ANIMATION_FPS,
    ChipVizPanel,
    animation_fps,
    animation_fps_table,
    build_page_html,
    chip_dirs,
    clock_activity,
    flow_params,
    grid_layout,
    grid_width_px,
    mode_caption,
    read_assets,
    read_chip_clocks,
    readout_text,
    viz_mode,
)
from ui.panels import MIN_CONTRAST_RATIO, contrast_ratio  # noqa: E402


class FakeClock:
    """A hand-driven monotonic clock, so staleness is tested by arithmetic
    rather than by sleeping through 15 real seconds."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def _fake_sysfs(tmp_path, clocks):
    """Build a fake `/sys/class/tenstorrent` with one chip dir per entry of
    `clocks` (an entry of None writes junk, standing in for a chip whose
    clock cannot be read). Returns the root."""
    root = tmp_path / "tenstorrent"
    root.mkdir()
    for index, mhz in enumerate(clocks):
        chip = root / f"tenstorrent!{index}"
        chip.mkdir()
        (chip / "tt_aiclk").write_text("n/a\n" if mhz is None else f"{mhz}\n")
    return root


# ---------------------------------------------------------------------------
# The mode policy. This is the whole "say why" of the task: which animation
# claims what about the silicon.
# ---------------------------------------------------------------------------

def test_diffusion_stage_animates_the_diffusion_mode():
    """The booth's headline claim: the model is denoising atom coordinates,
    and tensix-viz has a mode that literally is a denoising ring."""
    assert viz_mode("folding", "diffusion") == "diffusion"


def test_trunk_animates_sustained_work_not_a_denoising_ring():
    """The trunk is 10 refinement cycles of sustained whole-grid matmul, not
    a denoising timestep. Claiming `diffusion` through the trunk would make
    the animation decoration that happens to be on."""
    assert viz_mode("folding", "trunk") == "thinking"


@pytest.mark.parametrize("stage", ["msa", "prep", "saving"])
def test_host_side_stages_animate_idle(stage):
    """MSA search, tensor prep and mmCIF writing are host-side work (see
    ui/diagnostics.py's STAGE_TEACHING, written from runner/folder.py). The
    chips genuinely are not the thing doing them."""
    assert viz_mode("folding", stage) == "idle"


def test_confidence_animates_a_forward_pass():
    assert viz_mode("folding", "confidence") == "inference"


@pytest.mark.parametrize("state", ["attract", "gallery", "folding", "showcase"])
def test_the_booth_screen_does_not_decide_what_the_chips_are_doing(state):
    """The one that would be easiest to get wrong, and the most misleading if
    it were.

    The daemon folds CONTINUOUSLY -- it starts fold N+1 the instant fold N
    finishes, which is the entire reason ui/app.py has a showcase dwell. So
    "the booth is showing a finished structure" and "the booth is showing a
    gallery" both routinely mean "the chips are mid-diffusion". Animating
    idle for those, or animating a different mode per screen, would be a
    straightforward lie about the hardware.

    Mutation this catches: keying `_MODE_BY_STAGE` off `state` instead of
    `stage`.
    """
    assert viz_mode(state, "diffusion") == "diffusion"


def test_preparing_is_the_one_state_that_overrides_the_stage():
    """`not_ready` means the daemon has no fold running at all -- the one
    case where the booth genuinely knows the silicon is not working."""
    assert viz_mode("preparing", "diffusion") == "idle"


def test_no_stage_at_all_is_idle_not_a_guess():
    """Before the first fold, or with no socket, the booth knows nothing --
    and must claim nothing."""
    assert viz_mode("attract", None) == "idle"


def test_an_unknown_future_stage_never_claims_idle():
    """A protocol addition this build has never heard of means "something is
    running that we cannot name", which is not the same as "nothing is
    running". Reading it as idle would be the wrong error."""
    assert viz_mode("folding", "quantum_refolding") == "inference"


def test_mode_captions_are_the_booths_vocabulary_not_the_librarys():
    """The header is read by a visitor who has just been told on the help
    card that the model works by denoising -- not by someone who knows
    tensix-viz's internal mode names."""
    assert mode_caption("diffusion") == "denoising"
    assert mode_caption("thinking") == "refining"
    assert mode_caption("idle") == "idle"


# ---------------------------------------------------------------------------
# AICLK -> activity. Idle-relative, because Blackhole's clock is near-binary.
# ---------------------------------------------------------------------------

def test_an_idle_clock_reads_as_no_activity():
    assert clock_activity(AICLK_IDLE_MHZ) == pytest.approx(0.0)


def test_a_boosted_clock_reads_as_full_activity():
    assert clock_activity(AICLK_BOOST_MHZ) == pytest.approx(1.0)


def test_the_measured_real_clock_on_this_box_reads_as_full_activity():
    """1350 MHz is what all four chips on this machine actually report under
    load (verified by reading tt_aiclk directly)."""
    assert clock_activity(1350) == pytest.approx(1.0)


def test_activity_is_normalised_idle_relative_not_from_zero():
    """Scaled from zero, a resting chip at 800 MHz would sit at ~0.59 and a
    boosted one at 1.0 -- barely distinguishable, and never reaching the
    quiet end. The midpoint of the real range must read as a real midpoint.

    Mutation this catches: `mhz / AICLK_BOOST_MHZ`.
    """
    midpoint = (AICLK_IDLE_MHZ + AICLK_BOOST_MHZ) / 2.0
    assert clock_activity(midpoint) == pytest.approx(0.5)


@pytest.mark.parametrize("value", [None, "n/a", float("nan"), float("inf"), -5])
def test_no_sensor_value_can_cost_the_booth_an_exception(value):
    """This feeds an animation. Every junk value must land somewhere in
    [0, 1], quietly."""
    assert 0.0 <= clock_activity(value) <= 1.0


# ---------------------------------------------------------------------------
# Flow shaping.
# ---------------------------------------------------------------------------

def test_an_active_mode_guarantees_visible_flow_even_at_rest():
    """A chip whose clock happens to be resting mid-fold must not look
    switched off. Mutation this catches: dropping the floor and scaling
    straight from zero."""
    dram, l1, writeback = flow_params(0.0, active=True)
    assert dram >= 0.3 and l1 >= 0.25 and writeback > 0.0


def test_load_intensifies_the_flow():
    quiet = flow_params(0.0, active=True)
    busy = flow_params(1.0, active=True)
    assert all(b > q for b, q in zip(busy, quiet))


def test_idle_stays_quiet():
    dram, l1, writeback = flow_params(0.0, active=False)
    assert dram < 0.1 and writeback == 0.0


@pytest.mark.parametrize("activity", [-3.0, 0.0, 0.5, 1.0, 7.0])
@pytest.mark.parametrize("active", [True, False])
def test_flow_params_are_always_in_range(activity, active):
    assert all(0.0 <= v <= 1.0 for v in flow_params(activity, active))


# ---------------------------------------------------------------------------
# Reading the driver. Passive sysfs only -- no device is ever opened.
# ---------------------------------------------------------------------------

def test_clocks_are_position_aligned_with_the_chips(tmp_path, monkeypatch):
    """An unreadable chip contributes None, it is NOT dropped. Dropping it
    would silently shift every later chip's animation onto the wrong chip's
    clock -- a wrong answer that still looks perfectly plausible on screen.

    Mutation this catches: `continue` instead of `append(None)`.
    """
    monkeypatch.setattr(chipviz_module, "SYSFS_ROOT",
                        _fake_sysfs(tmp_path, [1350, None, 800, 1350]))
    assert read_chip_clocks() == [1350, None, 800, 1350]


def test_chips_are_enumerated_in_a_stable_order(tmp_path, monkeypatch):
    """The animations must line up with the telemetry panel's cells above
    them, which are in tt-smi's device order."""
    monkeypatch.setattr(chipviz_module, "SYSFS_ROOT",
                        _fake_sysfs(tmp_path, [800] * 4))
    assert [p.name for p in chip_dirs()] == [
        "tenstorrent!0", "tenstorrent!1", "tenstorrent!2", "tenstorrent!3"]


def test_a_machine_with_no_driver_at_all_reads_as_no_chips(tmp_path, monkeypatch):
    monkeypatch.setattr(chipviz_module, "SYSFS_ROOT", tmp_path / "nothing-here")
    assert chip_dirs() == []
    assert read_chip_clocks() == []


def test_this_machines_real_chips_are_readable_and_passive():
    """The one test that touches the real driver -- by READING sysfs, which
    does not open the device and cannot disturb another user's workload on
    the same silicon. Skipped (not failed) on a box with no Tenstorrent
    hardware, since that is a legitimate development machine."""
    dirs = chip_dirs()
    if not dirs:
        pytest.skip("no Tenstorrent chips on this machine")
    clocks = read_chip_clocks()
    assert len(clocks) == len(dirs)
    assert any(c is not None for c in clocks), (
        "every chip's tt_aiclk was unreadable, which would leave the panel's "
        "readout permanently blank on real hardware")


# ---------------------------------------------------------------------------
# The readout: honest about how many chips it is showing.
# ---------------------------------------------------------------------------

def test_the_readout_shows_the_peak_clock():
    assert readout_text([800, 1350, 1350, 800], 4, 4) == "1350 MHz"


def test_a_capped_display_says_so_rather_than_under_reporting():
    """The cap keeps a big machine legible; it must never quietly make the
    booth look smaller than it is."""
    assert readout_text([1350] * 4, 4, 8) == "1350 MHz · 4/8"


def test_nothing_readable_reads_as_unknown_never_zero():
    """Same register as the telemetry panel's tri-state: "we do not know" is
    not "0 MHz", which would look like real silicon sitting still."""
    assert readout_text([None, None], 2, 2) == "—"


# ---------------------------------------------------------------------------
# Layout arithmetic.
# ---------------------------------------------------------------------------

def test_one_canvas_per_chip_in_one_row():
    cols, width, height = grid_layout(4)
    assert cols == 4 and width > 0 and height > 0


def test_the_display_is_capped():
    cols, _w, _h = grid_layout(32)
    assert cols == MAX_CHIPS


def test_the_grid_fits_inside_the_side_rail():
    """The rail is a FIXED 430px column and the protein is the hero. This
    panel must fit inside it with its own padding, or it silently forces the
    rail wider and squeezes the protein -- the exact defect
    `_SIDE_RAIL_WIDTH_PX`'s comment records having already happened once."""
    from ui.app import _SIDE_RAIL_WIDTH_PX
    from ui.chipviz import RAIL_INNER_WIDTH_PX
    rail_margins = 2 * 18      # ui/app.py's rail margins
    panel_padding = 2 * 16     # .chipviz-panel's own horizontal padding
    # ui/chipviz.py cannot import ui/app.py (that would be a cycle), so it
    # hardcodes the rail width. This is the line that stops the copy drifting
    # from the original.
    assert RAIL_INNER_WIDTH_PX == (
        _SIDE_RAIL_WIDTH_PX - rail_margins - panel_padding)
    cols, width, _h = grid_layout(4)
    assert grid_width_px(cols, width) <= RAIL_INNER_WIDTH_PX


# ---------------------------------------------------------------------------
# The bundled assets and the page built from them.
# ---------------------------------------------------------------------------

def test_the_tensix_viz_assets_are_vendored_in_this_repo():
    """The booth is OFFLINE at the venue. A CDN embed is a demo that goes
    blank the moment the conference wifi does."""
    js, css = read_assets()
    assert js is not None and css is not None
    assert "TensixViz" in js
    assert len(js) > 10000, "the bundled tensix-viz looks truncated"


def test_the_page_never_references_a_network_host():
    """Nothing in the page may fetch anything: no CDN, no font host, no
    XHR."""
    js, css = read_assets()
    html = build_page_html(js, css, 4, 84, 88)
    assert "http://" not in html
    assert "https://" not in html.replace("https://github.com", "")


def test_the_page_builds_one_canvas_per_chip_with_per_chip_access():
    """Feeding all four canvases one averaged number would make this
    decoration rather than an instrument, which is why the page exposes
    `setChipStats(i, ...)` and not only a fan-out setter."""
    html = build_page_html("/*js*/", "/*css*/", 3, 84, 88)
    assert "i<3" in html
    assert "setChipStats" in html
    assert "new window.TensixViz" in html


def test_the_page_is_buildable_with_no_assets_at_all():
    """Pure, total, and never raises -- so a build with unreadable assets
    still produces a page rather than an exception on the way to the
    screen."""
    assert build_page_html(None, None, 1, 84, 88).startswith("<!doctype html>")


# ---------------------------------------------------------------------------
# The widget: fail-soft, bounded, legible.
# ---------------------------------------------------------------------------

def _panel(monkeypatch, tmp_path, clocks=(1350, 1350, 1350, 1350), clock=None):
    monkeypatch.setattr(chipviz_module, "SYSFS_ROOT",
                        _fake_sysfs(tmp_path, list(clocks)))
    return ChipVizPanel(clock=clock or FakeClock())


def test_no_chips_means_a_hidden_panel_not_an_error(tmp_path, monkeypatch):
    """A development box with no Tenstorrent hardware gets a booth with one
    fewer thing in the rail, and nothing else."""
    monkeypatch.setattr(chipviz_module, "SYSFS_ROOT", tmp_path / "nothing-here")
    panel = ChipVizPanel()
    assert panel.available is False
    assert panel.get_visible() is False


def test_no_webkit_means_a_hidden_panel_not_an_error(tmp_path, monkeypatch):
    """The booth image at the venue may not carry WebKit even though this
    development box does."""
    monkeypatch.setattr(chipviz_module, "WEBKIT_AVAILABLE", False)
    monkeypatch.setattr(chipviz_module, "SYSFS_ROOT",
                        _fake_sysfs(tmp_path, [1350] * 4))
    panel = ChipVizPanel()
    assert panel.available is False
    assert panel.get_visible() is False


def test_unreadable_assets_mean_a_hidden_panel_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(chipviz_module, "ASSETS_DIR", tmp_path / "gone")
    monkeypatch.setattr(chipviz_module, "SYSFS_ROOT",
                        _fake_sysfs(tmp_path, [1350] * 4))
    panel = ChipVizPanel()
    assert panel.available is False
    assert panel.get_visible() is False


def test_an_unavailable_panel_starts_no_timer(tmp_path, monkeypatch):
    """The fail-soft path must also be the resource-free path."""
    monkeypatch.setattr(chipviz_module, "SYSFS_ROOT", tmp_path / "nothing-here")
    panel = ChipVizPanel()
    panel.set_running(True)
    assert panel._poll_source_id is None


def test_the_poll_source_is_added_once_and_removed(tmp_path, monkeypatch):
    """The bound that matters most: the booth runs unattended all day, and a
    timer that is added twice (or never removed) is exactly the unbounded
    growth this project has already been bitten by once.

    Mutation this catches: `set_running(False)` setting a flag instead of
    calling `GLib.source_remove`.
    """
    panel = _panel(monkeypatch, tmp_path)
    if not panel.available:
        pytest.skip("WebKit unavailable in this environment")
    panel.set_running(True)
    first = panel._poll_source_id
    assert first is not None
    panel.set_running(True)
    assert panel._poll_source_id == first, "a second start must not add a second timer"
    panel.set_running(False)
    assert panel._poll_source_id is None
    assert GLib.MainContext.default().find_source_by_id(first) is None, (
        "the GLib source itself must be gone, not merely forgotten")
    panel.set_running(False)  # idempotent


def test_the_first_sample_is_painted_immediately(tmp_path, monkeypatch):
    """Otherwise the readout shows an em dash for the first whole second the
    booth is open."""
    panel = _panel(monkeypatch, tmp_path, clocks=(1350, 800, 1350, 1350))
    if not panel.available:
        pytest.skip("WebKit unavailable in this environment")
    panel.set_running(True)
    try:
        assert panel._readout_label.get_label() == "1350 MHz"
    finally:
        panel.set_running(False)


def test_a_dead_daemon_stops_claiming_the_chips_are_denoising(tmp_path, monkeypatch):
    """The lie this panel could most easily tell. If the daemon dies
    mid-fold, the last stage it sent would otherwise animate "denoising" in
    front of a visitor forever, with nothing computing at all.

    Mutation this catches: deleting the staleness check in `_tick`.
    """
    clock = FakeClock()
    panel = _panel(monkeypatch, tmp_path, clock=clock)
    if not panel.available:
        pytest.skip("WebKit unavailable in this environment")
    panel.set_mode("folding", "diffusion")
    assert panel._mode == "diffusion"
    clock.now += STAGE_STALE_AFTER_S + 1.0
    panel._tick()
    assert panel._mode == "idle"


def test_a_live_fold_is_not_called_stale(tmp_path, monkeypatch):
    """The other half of the same rule: a fold is ~4.4s warm and emits
    several stage events inside that, so an ordinary fold must never trip
    the staleness fallback."""
    clock = FakeClock()
    panel = _panel(monkeypatch, tmp_path, clock=clock)
    if not panel.available:
        pytest.skip("WebKit unavailable in this environment")
    panel.set_mode("folding", "diffusion")
    clock.now += 5.0
    panel._tick()
    assert panel._mode == "diffusion"


def test_the_poll_interval_is_a_second_not_a_frame_rate():
    """tensix-viz animates from its own rAF loop; this poll only re-aims it.
    Polling at frame rate would spend the booth's whole day on sysfs reads
    for a value that moves twice a minute."""
    assert POLL_INTERVAL_MS >= 500


def test_the_tick_survives_a_broken_poll(tmp_path, monkeypatch):
    """`_tick` is a REPEATING GLib source: an exception escaping it removes
    the source permanently -- a panel frozen for the rest of the day with
    nothing on screen saying so.

    Mutation this catches: dropping `_tick`'s try/except, or moving its
    `return True` inside the try.
    """
    panel = _panel(monkeypatch, tmp_path)
    if not panel.available:
        pytest.skip("WebKit unavailable in this environment")

    def boom():
        raise RuntimeError("sysfs went away")

    monkeypatch.setattr(chipviz_module, "read_chip_clocks", boom)
    assert panel._tick() is True


def test_the_pending_js_backlog_is_bounded(tmp_path, monkeypatch):
    """An unrealized WebView swallows every `evaluate_javascript`, so calls
    are queued -- and a panel left running while never realized must not
    accumulate them for the life of the process."""
    panel = _panel(monkeypatch, tmp_path)
    if not panel.available:
        pytest.skip("WebKit unavailable in this environment")
    for index in range(500):
        panel._eval(f"noop({index})")
    assert len(panel._pending_js) <= chipviz_module._MAX_PENDING_JS + 1


def test_the_panel_never_expands_and_squeezes_the_protein(tmp_path, monkeypatch):
    """The rail is a fixed column; the protein is the hero."""
    panel = _panel(monkeypatch, tmp_path)
    if not panel.available:
        pytest.skip("WebKit unavailable in this environment")
    assert panel._webview.get_hexpand() is False
    assert panel._webview.get_vexpand() is False


def test_every_label_on_the_panel_is_legible(tmp_path, monkeypatch):
    """The project rule, applied here too: an explicitly-set background
    implies an explicitly-set foreground, >= 4.5:1. Measured on #092221 --
    the title #C7D9D8 = 11.36:1, the clock readout #F1F8F8 = 15.46:1."""
    panel = _panel(monkeypatch, tmp_path)
    panel.set_mode("folding", "diffusion")
    _legibility.assert_every_label_is_legible(
        panel, context="Tensix activity panel",
        min_contrast=MIN_CONTRAST_RATIO, contrast_ratio_fn=contrast_ratio,
        css_text_fn=lambda: chipviz_module._CHIPVIZ_CSS,
        background_by_class_fn=lambda: chipviz_module._BACKGROUND_BY_CLASS)


def test_every_label_class_on_the_panel_has_an_explicit_colour_rule(tmp_path,
                                                                    monkeypatch):
    """The structural half: a class with no `color:` behind it inherits the
    desktop theme, which measured ~1.01:1 the last time this happened."""
    panel = _panel(monkeypatch, tmp_path)
    rules = _legibility.color_rules_from_css(chipviz_module._CHIPVIZ_CSS)
    for label in _legibility.iter_labels(panel):
        assert _legibility.label_has_an_explicit_color_rule(label, rules), (
            f"label {label.get_label()!r} carries no colour-bearing class")


def test_the_webkit_sandbox_is_disabled_before_webkit_is_imported():
    """The one setting in this module that is load-bearing for the BOOTH, not
    just for a test.

    WebKitGTK's bubblewrap sandbox needs an unprivileged user namespace, which
    Ubuntu 24.04 restricts by default (`kernel.apparmor_restrict_unprivileged_
    userns = 1` on this machine). Without this variable, `bwrap` fails, WebKit
    raises a `g_error`, and the process takes SIGTRAP -- an ABORT, not a
    Python exception, so nothing in ui/chipviz.py's fail-soft handling can
    catch it. It was reproduced exactly that way while building this panel:
    the whole pytest process died with exit 133 after every test had passed.

    See the module comment for why disabling it is defensible for this
    particular WebView (one static local page, no network, no untrusted
    content).
    """
    import os
    assert os.environ.get("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS") is not None, (
        "ui.chipviz must set this before importing WebKit, or a booth on a "
        "machine with restricted user namespaces aborts at startup")


def test_the_panel_says_chips(tmp_path, monkeypatch):
    """Same vocabulary rule as the rest of this task: one animation is one
    chip."""
    panel = _panel(monkeypatch, tmp_path)
    texts = " ".join(l.get_label() for l in _legibility.iter_labels(panel)).lower()
    assert "card" not in texts


# ---------------------------------------------------------------------------
# One chip folds, so one chip animates the fold.
#
# Whole-branch review, Critical 3: the panel fanned the fold's mode out to
# all four canvases while runner/daemon.py folds on card 0 only, so the booth
# showed four chips working when one was. `job_start` already carries the
# card index; these pin that it is used, and that the three idle chips are
# drawn idle -- including their flow floor, which is the other half of
# "looks like it is working".
# ---------------------------------------------------------------------------

def _recording_panel(monkeypatch, tmp_path, chips=4):
    """A real panel whose JS evaluations are captured instead of run."""
    panel = _panel(monkeypatch, tmp_path, clocks=(1350,) * chips)
    if not panel.available:
        pytest.skip("WebKit unavailable in this environment")
    calls = []
    monkeypatch.setattr(panel, "_eval", calls.append)
    return panel, calls


def test_only_the_folding_chip_animates_the_fold(tmp_path, monkeypatch):
    """Every canvas starts idle (the page's own `activate('idle')`), so the
    three that stay idle are correctly sent nothing at all -- what must never
    appear is the fold's mode aimed at them."""
    panel, calls = _recording_panel(monkeypatch, tmp_path)
    panel.set_folding_chip(0)
    panel.set_mode("folding", "diffusion")
    joined = " ".join(calls)
    assert 'activateChip(0,"diffusion")' in joined
    for idle_chip in (1, 2, 3):
        assert f'activateChip({idle_chip},"diffusion")' not in joined, (
            "a chip that is not folding must not animate the fold")


def test_learning_which_chip_folds_puts_the_others_back_to_idle(tmp_path,
                                                                monkeypatch):
    """The transition that actually needs JS: a fold whose card arrives
    after the mode (or a fold that moves to another chip) must stand the
    previous chips down, not leave them animating work they are not doing."""
    panel, calls = _recording_panel(monkeypatch, tmp_path)
    panel.set_mode("folding", "diffusion")     # unattributed: all four animate
    calls.clear()
    panel.set_folding_chip(0)
    joined = " ".join(calls)
    for idle_chip in (1, 2, 3):
        assert f'activateChip({idle_chip},"idle")' in joined
    assert 'activateChip(0,"idle")' not in joined


def test_the_header_names_the_chip_doing_the_work(tmp_path, monkeypatch):
    panel, _ = _recording_panel(monkeypatch, tmp_path)
    panel.set_folding_chip(2)
    panel.set_mode("folding", "trunk")
    label = panel._title_label.get_label()
    assert "CHIP 2" in label and "REFINING" in label


def test_an_idle_booth_claims_no_chip_at_all(tmp_path, monkeypatch):
    """The header must not keep pointing at a chip once nothing is folding
    -- "CHIP 0 · IDLE" reads as a chip that is specially idle."""
    panel, _ = _recording_panel(monkeypatch, tmp_path)
    panel.set_folding_chip(0)
    panel.set_mode("preparing", None)
    assert "CHIP" not in panel._title_label.get_label()


def test_an_unattributed_fold_animates_everything_and_names_nothing(tmp_path,
                                                                    monkeypatch):
    """The fallback: told a stage but never told a card (no job_start yet, or
    a daemon that stops saying), the panel animates the mode everywhere and
    the header attributes it to no chip in particular. It must not silently
    assume chip 0."""
    panel, calls = _recording_panel(monkeypatch, tmp_path)
    panel.set_mode("folding", "diffusion")
    joined = " ".join(calls)
    for chip in range(4):
        assert f'activateChip({chip},"diffusion")' in joined
    assert "CHIP" not in panel._title_label.get_label()


def test_the_flow_floor_is_only_given_to_the_chip_that_is_working(tmp_path,
                                                                  monkeypatch):
    """`flow_params`' active floor exists so a working chip never looks
    switched off. Applying it to the three chips that are NOT folding would
    make them look busy by another route, at the same boosted clock."""
    panel, calls = _recording_panel(monkeypatch, tmp_path)
    panel.set_folding_chip(0)
    panel.set_mode("folding", "diffusion")
    calls.clear()
    panel._tick()
    stats = [call for call in calls if "setChipStats" in call]
    working = [call for call in stats if call.startswith(
        "window.__viz&&window.__viz.setChipStats(0,")]
    resting = [call for call in stats if call.startswith(
        "window.__viz&&window.__viz.setChipStats(1,")]
    active_dram, idle_dram = flow_params(1.0, True)[0], flow_params(1.0, False)[0]
    assert working and f"dram_bw:{active_dram:.3f}" in working[-1]
    assert resting and f"dram_bw:{idle_dram:.3f}" in resting[-1]


def test_an_unusable_card_index_costs_the_attribution_not_an_exception(tmp_path,
                                                                       monkeypatch):
    """Wire-shaped data: `card` is whatever the daemon put on the socket."""
    panel, _ = _recording_panel(monkeypatch, tmp_path)
    panel.set_folding_chip("not-a-chip")
    panel.set_mode("folding", "diffusion")
    assert "CHIP" not in panel._title_label.get_label()


def test_the_page_exposes_per_chip_activation(tmp_path):
    """The Python above can only aim a mode at one canvas if the page lets
    it -- `activate(mode)` alone (the fan-out) cannot express this."""
    html = build_page_html("/*js*/", "/*css*/", 4, 84, 88)
    assert "activateChip" in html


# ── the frame governor (the flicker fix) ────────────────────────────────────
#
# What these pin, and why each one is a real failure rather than a shape:
# the panel flickered because tensix-viz's `idle` mode randomises every cell
# once per DISPLAY frame, and `_drawHeatmap` renormalises the grid to its own
# per-frame maximum, so each fresh pop lands at full contrast. Measured over
# the four canvases: 4697 pixel-brightenings/second ungoverned against 1458
# with the governor in (see ui/chipviz.py's comment above
# `RESTING_ANIMATION_FPS`, and this task's report).
#
# The live JS was verified by measurement, not here -- a unit suite with no
# main loop cannot watch a requestAnimationFrame chain. What CAN be pinned
# here is that the policy is the one that was measured, and that the page
# actually routes the animation through it, which is where a later edit would
# most plausibly break it.


def test_only_the_random_mode_is_slowed_down():
    """The fold's own modes must keep the display's rate.

    `thinking`, `diffusion` and `inference` are smooth deterministic fields
    -- measured at 10-30x less per-frame churn than `idle` -- and `diffusion`
    is the one animation the booth actually points at ("the same shape as the
    collapse on the left of the screen"). Slowing those would cost the panel
    its one honest rhyme to buy nothing.
    """
    assert 0 < animation_fps("idle") <= 30, (
        "`idle` is the mode that flickers; it must be governed, and to a rate "
        "low enough to matter")
    for mode in ("thinking", "diffusion", "inference"):
        assert animation_fps(mode) == 0, (
            f"{mode} is a smooth field and must run at the display's rate")


def test_a_mode_this_build_has_never_heard_of_is_never_frozen():
    """`"*"` is the page's fallback. A future tensix-viz mode, or a later
    `_MODE_BY_STAGE` row, must inherit "ungoverned" rather than whichever
    number happened to be left in the table -- an unknown mode already means
    "something is running and we do not know what" (see `_UNKNOWN_STAGE_MODE`),
    and drawing it at a resting cadence would be the same claim this panel
    refuses to make with the mode itself."""
    table = animation_fps_table()
    assert table["*"] == WORKING_ANIMATION_FPS == 0


def test_the_table_the_page_gets_is_the_policy_the_tests_check():
    """Every mode the stage table can ask for is in the page's budget, with
    the value `animation_fps` computed -- so the policy above cannot drift
    away from what actually reaches the WebView."""
    table = animation_fps_table()
    for mode in set(chipviz_module._MODE_BY_STAGE.values()) | {
            chipviz_module._UNKNOWN_STAGE_MODE}:
        assert table[mode] == animation_fps(mode)
    html = build_page_html("/*js*/", "/*css*/", 4, 84, 88)
    assert json.dumps(table) in html, (
        "the page must carry the computed budget, not a hand-written copy")


def test_every_activation_runs_under_the_governor():
    """The governor hands a chain its budget by wrapping the `activate` call
    (`window.__vizRun`); tensix-viz then re-arms its own loop from inside its
    callback and inherits it. An `activate` called outside that wrapper starts
    an ungoverned chain that nothing later can slow down -- the panel would go
    back to flickering the moment any chip changed mode."""
    html = build_page_html("/*js*/", "/*css*/", 4, 84, 88)
    assert html.count("v.activate(m)") > 0
    assert html.count("v.activate(m)") == html.count("window.__vizRun(m,"), (
        "every activate must be wrapped in __vizRun")


def test_the_governor_is_installed_before_any_canvas_can_start_a_loop():
    """Order is load-bearing: a chain started before `requestAnimationFrame`
    is wrapped keeps the native function for its whole life, and no later
    install can reach it."""
    html = build_page_html("/*js*/", "/*css*/", 4, 84, 88)
    assert (html.index("window.requestAnimationFrame=function")
            < html.index("new window.TensixViz"))


def test_the_governor_leaves_animations_cancellable():
    """tensix-viz's `reset()` -- which every `activate` calls first -- stops
    the previous loop with `cancelAnimationFrame`. The governor hands out its
    OWN ids, so if it does not also wrap the cancel, a mode change leaves the
    old chain running and two loops fight over one canvas forever."""
    html = build_page_html("/*js*/", "/*css*/", 4, 84, 88)
    assert "window.cancelAnimationFrame=function" in html


def test_the_page_denies_itself_every_network_source(tmp_path):
    """The WebKit sandbox is off in this process by necessity (see the
    SIGTRAP note above), so the containment argument -- "one local page, no
    network" -- should be enforced by the engine rather than left as a
    property of what we happened to inline.

    Both halves are asserted: the deny-all base, AND the two inline
    allowances the page genuinely needs. A bare `default-src 'none'` would
    block this page's own inline <script>/<style> and blank the panel at the
    venue, which is precisely the fail-silent mode this module exists to
    avoid.
    """
    html = build_page_html("/*js*/", "/*css*/", 4, 84, 88)
    assert "Content-Security-Policy" in html
    assert "default-src 'none'" in html
    assert "script-src 'unsafe-inline'" in html
    assert "style-src 'unsafe-inline'" in html
