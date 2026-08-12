"""Task 10: the diagnostics panel -- the booth's protocol tap.

Two things are pinned here above all others, because they are the two that
would hurt a real booth:

1. **Boundedness.** The booth runs unattended all day. This project has
   already been bitten once by unbounded growth (a log writing 13-14 MB/s
   into an unlinked file, ~31 minutes from OOM), so "the ring buffer really
   is a ring" is a test, not a comment.
2. **No raw error text, ever.** A log-shaped widget is the most tempting
   place in the whole app to pipe `str(exc)` onto a screen. The tests below
   feed the log a `job_error` carrying a full traceback and a `not_ready`
   carrying real filesystem paths, and assert that not one fragment of
   either reaches a line.

Everything in the first half runs with no display at all (`DiagnosticsLog`
imports no GTK). The widget half needs a live display for the legibility
check and says so loudly rather than skipping, per this project's rule
about silently-empty test halves.
"""

import numpy as np
import pytest

import _legibility
from ui import diagnostics
from ui.diagnostics import (
    KIND_MARK, KIND_TEACH, MAX_LINES, MAX_LINE_CHARS, DiagnosticsLog,
    DiagnosticsPanel, radius_of_gyration,
)
from ui.panels import MIN_CONTRAST_RATIO, contrast_ratio


def _log(**kw):
    """A log with a frozen clock and no banner, so a test asserting on
    "the lines this event produced" sees exactly those."""
    kw.setdefault("clock", lambda: 1_754_000_000.25)
    kw.setdefault("banner", False)
    return DiagnosticsLog(**kw)


def _texts(diag_log):
    return [text for _stamp, _kind, text in diag_log.tail(MAX_LINES)]


def _blob(diag_log):
    return "\n".join(_texts(diag_log))


# ---------------------------------------------------------------------------
# Bounded, because the booth runs all day.
# ---------------------------------------------------------------------------

def test_the_ring_buffer_never_grows_past_its_bound():
    """Mutation this catches: a plain `list` (or a deque with no `maxlen`)
    behind `add` -- which is precisely the shape of the unbounded-growth
    defect this project has already paid for once."""
    diag_log = _log()
    for step in range(MAX_LINES * 25):
        diag_log.note_event({"type": "stage", "stage": "diffusion",
                             "frac": 0.15 + (step % 100) / 1000.0})
    assert len(diag_log) == MAX_LINES


def test_the_oldest_lines_are_the_ones_dropped():
    """A ring that dropped the NEWEST lines would stay bounded and be
    useless -- the panel would freeze on the first twenty events of the
    day. Mutation this catches: appending on the left instead of the
    right (or trimming the wrong end)."""
    diag_log = _log()
    for index in range(MAX_LINES + 50):
        diag_log.note(f"line {index}")
    texts = _texts(diag_log)
    assert texts[-1] == f"line {MAX_LINES + 49}"
    assert texts[0] == f"line {50}"


def test_one_absurd_line_cannot_blow_the_memory_bound():
    """The buffer's memory bound is lines x line length, so the second
    factor has to be enforced too.

    Deliberately NOT tested through `note_event`: every wire-sourced
    fragment there is already clipped by `_safe`, so a million-character
    `target_id` would produce a short line even with `add`'s own truncation
    deleted -- a test written that way passes the mutation and proves
    nothing (this project's own lesson: "ask what wrong answer a test would
    still accept"). `note` is the path that is NOT pre-clipped -- ui/app.py
    composes a line from a playlist-supplied target id through it -- so it
    is the one that pins `add`'s cap.

    Mutation this catches: dropping the `[:MAX_LINE_CHARS]` truncation.
    """
    diag_log = _log()
    diag_log.note("visitor picked " + "x" * 50_000)
    diag_log.note_event({"type": "job_start", "target_id": "x" * 1_000_000,
                         "n_residues": 20, "card": 0, "model": "m",
                         "job_id": "j"})
    assert all(len(text) <= MAX_LINE_CHARS for text in _texts(diag_log))


# ---------------------------------------------------------------------------
# Never a stack trace, never raw error text. The rule, tested twice.
# ---------------------------------------------------------------------------

_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/home/ttuser/code/tt-bio/runner/folder.py", line 88, in fold\n'
    "    out = model(feats)\n"
    "RuntimeError: TT_FATAL @ tt_metal/impl/device.cpp:212: chip 0 hung"
)


def test_a_job_error_never_shows_the_daemons_error_text():
    """The single most tempting rule violation in the whole app. The log
    reports THAT a fold failed; the traceback stays in the operator's log.

    Mutation this catches: `self.add(f"... {event.get('message')}")` in the
    job_error branch -- the one-line "improvement" a future editor is most
    likely to make.
    """
    diag_log = _log()
    diag_log.note_event({"type": "job_error", "job_id": "j7",
                         "target_id": "trpcage", "message": _TRACEBACK})
    blob = _blob(diag_log)
    assert blob, "a failed fold must still produce a line -- silence is not the fix"
    for fragment in ("Traceback", "RuntimeError", "TT_FATAL", "device.cpp",
                     "folder.py", "model(feats)"):
        assert fragment not in blob, f"{fragment!r} reached the screen"


def test_not_ready_never_shows_the_filesystem_paths_it_carries():
    """`missing` names real paths (ui/app.py's `_PREPARING_MESSAGE` comment
    makes the same point about the preparing overlay). The COUNT is
    information a visitor can have; the paths are not."""
    diag_log = _log()
    diag_log.note_event({"type": "not_ready", "missing": [
        "/home/ttuser/.cache/protenix/model_v2.pt", "/opt/tt-metal/build"]})
    blob = _blob(diag_log)
    assert "/home/ttuser" not in blob and "tt-metal" not in blob
    assert "2" in blob, "the count of outstanding items is the useful part"


def test_a_multiline_wire_string_can_never_paint_a_multiline_entry():
    """Structural, not case-by-case: every wire-sourced fragment goes
    through `_safe`, so even a daemon that smuggled a traceback into a
    field this panel DOES render could not draw one.

    Mutation this catches: interpolating a wire string directly instead of
    through `_safe`.
    """
    diag_log = _log()
    diag_log.note_event({"type": "job_start", "target_id": _TRACEBACK,
                         "n_residues": 20, "card": 0, "model": "m", "job_id": "j"})
    for _stamp, _kind, text in diag_log.tail():
        assert "\n" not in text and "\t" not in text
    blob = _blob(diag_log)
    # The first few characters of the field survive, as a target NAME would
    # -- what cannot survive is the shape of a traceback: the frames, the
    # paths, the exception line.
    for fragment in ("RuntimeError", "TT_FATAL", "folder.py", "line 88"):
        assert fragment not in blob


# ---------------------------------------------------------------------------
# It reads like a log of REAL traffic.
# ---------------------------------------------------------------------------

def test_every_line_carries_a_timestamp_except_the_teaching_comments():
    """Timestamps are what make this read as a tap on something real. The
    `#` commentary deliberately has none -- it is not an event, and stamping
    it would claim it was."""
    diag_log = _log()
    diag_log.note_event({"type": "stage", "stage": "trunk", "frac": 0.12})
    stamped = {kind: stamp for stamp, kind, _text in diag_log.tail()}
    assert stamped["event"] == "15:13:20.250"
    assert stamped[KIND_TEACH] == ""


def test_a_stage_line_reports_both_the_wire_and_the_within_stage_fraction():
    """The whole-fold fraction is what the wire carries; the within-stage
    one is what the pipeline panel draws. Showing both is what makes the
    panel a teaching tool rather than a number dump -- and it goes through
    the SAME shared converter the panel does, so the two readouts cannot
    disagree.

    Mutation this catches: printing the raw wire frac twice (i.e. skipping
    `within_stage_frac`), which would read `in-stage 55.0%` the instant
    diffusion started instead of 0.0%.
    """
    diag_log = _log()
    # diffusion owns 0.15-0.95 (protocol.events.STAGE_BANDS), so a wire
    # fraction of 0.55 is exactly half way through diffusion itself.
    diag_log.note_event({"type": "stage", "stage": "diffusion", "frac": 0.55})
    line = _texts(diag_log)[0]
    assert "wire  55.0%" in line
    assert "in-stage  50.0%" in line


def test_a_frame_line_reports_the_step_the_atom_count_and_the_collapse():
    """The radius of gyration is the number that makes the collapse legible
    AS a number -- it falls as the cloud condenses, in step with what is on
    screen."""
    diag_log = _log()
    coords = np.array([[3.0, 0.0, 0.0], [-3.0, 0.0, 0.0]], dtype="float32")
    diag_log.note_frame({"type": "frame", "step": 42, "total": 200}, coords)
    line = _texts(diag_log)[0]
    assert "42/200" in line
    assert "2 atoms" in line
    assert "3.00 Å" in line


def test_the_radius_of_gyration_is_the_rms_distance_from_the_centroid():
    """Pinned against a hand-computable case: four points at distance 5
    from their own centroid have Rg exactly 5.

    The centroid is deliberately NOT the origin. A cloud centred on (0,0,0)
    is a degenerate input here -- it makes "distance from the centroid" and
    "distance from the origin" the same number, so the test would pass
    against an implementation that measured the wrong one, and a real
    trajectory (which sits wherever the model put it) would then report an
    Rg dominated by how far the structure is from the origin rather than by
    how collapsed it is. Mutation this catches: `centre = np.zeros(3)`.
    """
    offset = np.array([100.0, -50.0, 7.0])
    coords = np.array([[5.0, 0.0, 0.0], [-5.0, 0.0, 0.0],
                       [0.0, 5.0, 0.0], [0.0, -5.0, 0.0]]) + offset
    assert radius_of_gyration(coords) == pytest.approx(5.0)


def test_the_radius_of_gyration_of_an_empty_frame_is_not_a_number():
    """Mutation this catches: returning 0.0 for an empty frame -- "no
    atoms" would then render as a perfectly collapsed structure."""
    assert np.isnan(radius_of_gyration(np.zeros((0, 3))))


def test_the_collapse_really_does_read_as_a_collapse_on_the_recorded_fold():
    """End-to-end against the real recorded trajectory: the Rg the panel
    reports must fall by orders of magnitude across a fold. This is the test
    that would catch a "correct-looking" Rg that was actually computed about
    the origin instead of the centroid, or per-axis instead of in 3D."""
    import json
    import pathlib

    from protocol.events import unpack_coords

    stream = (pathlib.Path(__file__).resolve().parents[2]
              / "tests/fixtures/streams/real_fold_trpcage.jsonl")
    values = []
    for line in stream.read_text().splitlines():
        event = json.loads(line)
        if event.get("type") == "frame":
            values.append(radius_of_gyration(unpack_coords(event["coords_b64"])))
    assert len(values) == 30
    assert values[0] > 1000.0            # opening noise: a huge ball
    assert values[-1] < 10.0             # a folded 20-residue protein
    assert values[-1] < values[0] / 100.0


# ---------------------------------------------------------------------------
# Teaching, interleaved.
# ---------------------------------------------------------------------------

def test_a_stage_teaches_itself_once_per_fold_not_once_per_event():
    """tt-bio's progress_fn fires many `stage` events per stage. Repeating
    the explanation on each one would bury the traffic it is supposed to
    annotate.

    Mutation this catches: dropping the `_taught` bookkeeping.
    """
    diag_log = _log()
    for frac in (0.15, 0.35, 0.55, 0.75, 0.95):
        diag_log.note_event({"type": "stage", "stage": "diffusion", "frac": frac})
    teach_lines = [t for _s, k, t in diag_log.tail(MAX_LINES) if k == KIND_TEACH]
    assert teach_lines == list(diagnostics.STAGE_TEACHING["diffusion"])


def test_a_new_fold_teaches_its_stages_again():
    """A visitor who arrives mid-cycle should get the explanation on the
    next fold. Mutation this catches: never clearing `_taught`."""
    diag_log = _log()
    diag_log.note_event({"type": "stage", "stage": "trunk", "frac": 0.12})
    diag_log.note_event({"type": "job_start", "target_id": "t", "card": 0,
                         "n_residues": 20, "model": "m", "job_id": "j2"})
    diag_log.note_event({"type": "stage", "stage": "trunk", "frac": 0.12})
    teach_lines = [t for _s, k, t in diag_log.tail(MAX_LINES) if k == KIND_TEACH]
    assert teach_lines == list(diagnostics.STAGE_TEACHING["trunk"]) * 2


def test_every_protocol_stage_has_teaching_copy():
    """A stage added to the protocol without a line of explanation would
    silently teach nothing -- the panel would just go quiet at the moment a
    visitor most wants to know what changed."""
    from protocol.events import STAGE_ORDER
    assert set(diagnostics.STAGE_TEACHING) == set(STAGE_ORDER)


def test_the_teaching_copy_fits_the_rail_without_ellipsizing():
    """Rows are single-line and ellipsized (see `DiagnosticsPanel`), so copy
    written too wide would be silently cut off mid-sentence on the booth --
    a defect invisible to every other test here."""
    for stage, lines in diagnostics.STAGE_TEACHING.items():
        for line in lines:
            assert len(line) <= 62, f"{stage}: {line!r} is {len(line)} chars"
    for line in diagnostics.BANNER:
        assert len(line) <= 62


# ---------------------------------------------------------------------------
# Total on wire-shaped data: nothing here may raise into a GLib callback.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event", [
    {"type": "stage", "stage": "diffusion", "frac": "not-a-number"},
    {"type": "stage", "stage": None, "frac": None},
    {"type": "job_done", "wall_s": None, "mean_plddt": float("nan")},
    {"type": "job_start"},
    {"type": "hello", "cards": "four", "version": None},
    {"type": "card_state", "card": None, "temperature_c": "hot"},
    {"type": "not_ready", "missing": "everything"},
    {"type": "future_event_from_a_later_protocol"},
    {"type": None},
])
def test_no_wire_shaped_event_can_raise(event):
    """Every one of these produces a line and no exception. An exception
    escaping here would land in ui/app.py's broad guard and turn a good
    event into "dropping malformed ..." -- the panel costing the booth its
    rendering is exactly backwards."""
    diag_log = _log()
    diag_log.note_event(event)
    assert len(diag_log) >= 0  # the point is that the call above returned


def test_an_unknown_event_type_still_produces_an_honest_line():
    """Silence would be the wrong answer: a future protocol addition should
    be visible in the tap, not invisible."""
    diag_log = _log()
    diag_log.note_event({"type": "quantum_teleport"})
    assert "quantum_teleport" in _blob(diag_log)


def test_a_frame_with_no_coordinates_reads_as_unknown_not_as_zero():
    diag_log = _log()
    diag_log.note_frame({"type": "frame", "step": 1, "total": 200}, None)
    assert "?" in _texts(diag_log)[0]


# ---------------------------------------------------------------------------
# The widget. Fixed shape, repaints only when there is something new.
# ---------------------------------------------------------------------------

def test_the_panel_has_a_fixed_number_of_rows_that_never_grows():
    """The widget-side half of the boundedness story: rows are created once
    and re-labelled, never appended. Mutation this catches: building a fresh
    label per line in `refresh`, which on an all-day booth is the same
    unbounded-growth defect wearing a widget."""
    panel = DiagnosticsPanel(visible_lines=6)
    diag_log = _log()
    before = len(list(_legibility.iter_labels(panel)))
    for index in range(500):
        diag_log.note(f"line {index}")
        panel.refresh(diag_log)
    assert len(list(_legibility.iter_labels(panel))) == before


def test_the_panel_shows_the_newest_lines_at_the_bottom():
    panel = DiagnosticsPanel(visible_lines=4)
    diag_log = _log()
    for index in range(10):
        diag_log.note(f"line {index}")
    panel.refresh(diag_log)
    rendered = [label.get_label() for label in _legibility.iter_labels(panel)]
    assert rendered[-1].endswith("line 9")
    assert rendered[-2].endswith("line 8")


def test_the_panel_does_not_repaint_when_the_log_has_not_moved():
    """A 30Hz frame stream must not re-label twenty rows thirty times a
    second for a list nothing has appended to. Mutation this catches:
    dropping the revision check."""
    panel = DiagnosticsPanel(visible_lines=4)
    diag_log = _log()
    assert panel.refresh(diag_log) is True      # first paint always happens
    assert panel.refresh(diag_log) is False
    diag_log.note("something happened")
    assert panel.refresh(diag_log) is True


def test_a_short_log_pads_at_the_top_rather_than_the_bottom():
    """So the newest line is always on the same row instead of the whole
    block sliding down the rail as the log fills."""
    panel = DiagnosticsPanel(visible_lines=5)
    diag_log = _log()
    diag_log.note("only line")
    panel.refresh(diag_log)
    rendered = [label.get_label() for label in _legibility.iter_labels(panel)]
    assert rendered[-1].endswith("only line")
    assert rendered[-2] == ""


# ---------------------------------------------------------------------------
# Legibility -- the same shared guard ui/panels.py and ui/gallery.py use,
# pointed at this module's stylesheet. Measured ratios on the dark ground
# (#092221): event rows #C7D9D8 = 11.36:1, mark rows #F1F8F8 = 15.46:1,
# teaching rows #3299B9 = 5.06:1, header #C7D9D8 = 11.36:1. AA floor 4.5:1.
# ---------------------------------------------------------------------------

def _assert_every_label_is_legible(root, *, context):
    return _legibility.assert_every_label_is_legible(
        root, context=context, min_contrast=MIN_CONTRAST_RATIO,
        contrast_ratio_fn=contrast_ratio,
        css_text_fn=lambda: diagnostics._DIAGNOSTICS_CSS,
        background_by_class_fn=lambda: diagnostics._BACKGROUND_BY_CLASS)


def test_every_diagnostics_label_is_legible_in_every_line_kind():
    panel = DiagnosticsPanel(visible_lines=8)
    diag_log = DiagnosticsLog(clock=lambda: 1_754_000_000.25)   # banner: teach
    diag_log.note_event({"type": "job_start", "target_id": "trpcage", "card": 0,
                         "n_residues": 20, "model": "protenix-v2", "job_id": "j"})
    diag_log.note_event({"type": "stage", "stage": "diffusion", "frac": 0.55})
    panel.refresh(diag_log)
    _assert_every_label_is_legible(panel, context="event + mark + teach")


def test_every_line_kind_has_a_colour_rule_of_its_own():
    """The legibility rule's structural half: an explicitly-set background
    implies an explicitly-set foreground, so no row may ever inherit its
    colour from the desktop theme. Mutation this catches: adding a fourth
    line kind (or a fourth CSS class) with no `color:` rule behind it --
    which on a light desktop theme renders near-invisible and which the
    contrast test above cannot see, because it only walks the kinds a test
    happened to produce."""
    rules = _legibility.color_rules_from_css(diagnostics._DIAGNOSTICS_CSS)
    for css_class in list(diagnostics._KIND_CLASSES.values()) + ["diagnostics-header"]:
        assert any(required <= {css_class, "diagnostics-line"} for required in rules), \
            f"{css_class} has no explicit color: rule"
