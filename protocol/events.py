"""Wire protocol shared by the runner and the UI.

Imports must stay limited to the standard library and numpy: this module is
imported both by the UI on system python3 and by the runner inside the tt-bio
venv, and any richer dependency would have to exist in both.
"""

import base64
import json
import logging

import numpy as np

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

EVENT_TYPES = frozenset(
    {"hello", "not_ready", "job_start", "stage", "frame",
     "job_done", "job_error", "card_state"}
)

# ---------------------------------------------------------------------------
# The stage contract: the vocabulary of `stage` events, and how a `frac` on
# the wire maps onto that vocabulary.
#
# This lives here, not in runner/shaping.py where it started, because it is
# consumed on BOTH sides of the wire: runner/folder.py emits `stage` events
# shaped by it, and ui/panels.py's pipeline panel renders them. Before this
# move, the UI venv (system python3, no torch/tt-bio) could not import
# runner.shaping at all, so the brief for the panel work had to hardcode its
# own copy of STAGE_ORDER as a bare tuple literal in its test file -- which
# means a stage added to or removed from the real contract would be caught
# by runner/folder.py's own import-time assert (see below) on the runner
# side, and by *nothing* on the UI side: the panel would just silently gain
# or lose a row, out of step with what the daemon actually sends. Since this
# module already declares itself the one both venvs can reach (stdlib and
# numpy only -- see the module docstring above), it is the correct home.
# runner/shaping.py re-exports STAGE_ORDER (a bare `import`, not a copy) so
# every existing runner-side import keeps working unchanged.

# The full vocabulary a `stage` event's `stage` field may hold. tt-bio's own
# progress_fn only ever reports `trunk` and `diffusion`; the other four are
# emitted by the daemon (runner/folder.py) bracketing the work it does
# around the fold itself. Order matters: it is the pipeline panel's display
# order top-to-bottom, and it is the order `stage_rows` (ui/panels.py) walks
# to decide which stages read as done/active/pending.
STAGE_ORDER = ("msa", "prep", "trunk", "diffusion", "confidence", "saving")

# The fraction-of-progress-bar band each stage owns on the WHOLE-FOLD wire
# fraction, in STAGE_ORDER. Bands are contiguous -- each starts exactly
# where the previous one ends -- so the fraction reported to the UI is
# monotonically non-decreasing across an entire fold. That matters because a
# naive per-stage fraction (each stage restarting its own 0.0 -> 1.0) makes
# the bar visibly jump backward at every stage transition: trunk climbing to
# "40%" and then diffusion's first callback reporting under 1%, in front of
# a live audience, on every single fold (see runner/folder.py's own history
# of this exact incident).
#
# tt-bio's progress_fn reports real (step, total) counts for exactly two
# stages: trunk (10 refinement cycles) and diffusion (200 denoising steps).
# The other four are synthetic brackets runner/folder.py owns and fires
# once, at the end of their own band, the instant it reaches that point --
# there is no partial progress to report for work that module doesn't
# itself perform. diffusion gets the bulk of the bar deliberately: it is
# 20x trunk's step count, so most of a fold's wall-clock time is spent
# there.
STAGE_BANDS = {
    "msa": (0.00, 0.05),
    "prep": (0.05, 0.10),
    "trunk": (0.10, 0.15),
    "diffusion": (0.15, 0.95),
    "confidence": (0.95, 0.98),
    "saving": (0.98, 1.00),
}
# Enforced at import time, not just by convention: if a stage is ever added
# to or removed from STAGE_ORDER without updating this table (or vice
# versa) in this same edit, this fails loudly here, at the actual source of
# both values, rather than only downstream. runner/folder.py keeps its OWN
# copy of this same assert against the names it imports from here -- that
# one guards against the two names disagreeing by the time folder.py binds
# them (e.g. a bad merge), which this assert, sitting between the two
# literals above, cannot see for itself.
assert tuple(STAGE_BANDS) == STAGE_ORDER, (
    "STAGE_BANDS must list exactly the stages in STAGE_ORDER, in the same order"
)


def within_stage_frac(stage, wire_frac):
    """Convert the wire's WHOLE-FOLD `frac` into the WITHIN-STAGE fraction a
    per-row progress bar wants.

    The wire's `frac` (as sent in a `stage` event, and as STAGE_BANDS
    documents) is a whole-fold fraction with contiguous, non-decreasing
    bands -- e.g. diffusion owns 0.15-0.95, so a `stage` event reporting
    diffusion at 0.55 whole-fold means diffusion itself is halfway done:
    (0.55 - 0.15) / (0.95 - 0.15) = 0.5. `ui.panels.stage_rows` takes
    exactly that within-stage number for its `current` stage's row, NOT the
    raw wire value -- passing the wire fraction straight through would show
    diffusion's row sitting at "40%" the instant diffusion starts, instead
    of 0%, and it would never show 100% (diffusion's row would read 100%
    only when the wire value reaches 0.95, at which point the wire has
    already moved on to confidence). This is the one tested place that
    conversion happens, so a caller (see ui/app.py / the daemon-wiring task)
    never has to -- and never has a chance to get it subtly wrong -- inline
    at the call site.

    Clamped to [0.0, 1.0]: a `frac` that lands exactly on a band edge (or a
    hair outside it, from float rounding) must not produce a negative or
    >1.0 within-stage value.

    An unrecognized `stage` (a future protocol stage this build's copy of
    STAGE_BANDS doesn't know about) has no band to divide by, so `wire_frac`
    is passed through unchanged (still clamped to [0, 1]) rather than
    raising -- symmetric with `stage_rows`' own "an unknown stage must not
    raise" contract.

    A `wire_frac` that falls OUTSIDE the named stage's own band (e.g. a
    `trunk` event reporting 0.5, when trunk's band is 0.10-0.15) is not
    something this function can refuse -- clamping still has to produce
    *something* renderable -- but it is never supposed to happen in a
    correctly-behaving daemon (bands are contiguous and each stage event
    should only ever report a frac inside its own band), so it is logged at
    warning level here: the one place both the runner and UI import, so
    catching it here catches it regardless of which side's bug produced it.
    """
    band = STAGE_BANDS.get(stage)
    if band is None:
        return max(0.0, min(1.0, float(wire_frac)))
    start, end = band
    wire_frac = float(wire_frac)
    if wire_frac < start or wire_frac > end:
        log.warning(
            "wire frac %.4f for stage %r falls outside its own band "
            "(%.4f, %.4f) -- clamping, but this should not happen from a "
            "correctly-behaving daemon", wire_frac, stage, start, end)
    return max(0.0, min(1.0, (wire_frac - start) / (end - start)))


class ProtocolError(Exception):
    """A message could not be encoded or decoded."""


def encode(event):
    """Serialize one event to a newline-terminated JSON line."""
    kind = event.get("type")
    if kind not in EVENT_TYPES:
        raise ProtocolError(f"unknown event type: {kind!r}")
    return (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line):
    """Parse one newline-terminated JSON line into an event dict."""
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"malformed JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise ProtocolError("not a JSON object")
    if "type" not in event:
        raise ProtocolError("missing 'type'")
    if event["type"] not in EVENT_TYPES:
        raise ProtocolError(f"unknown event type: {event['type']!r}")
    return event


def pack_coords(a):
    """Pack an (N, 3) coordinate array as base64 of little-endian float32."""
    arr = np.asarray(a)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ProtocolError(f"coordinates must have shape (N, 3), got {arr.shape}")
    buf = np.ascontiguousarray(arr, dtype="<f4").tobytes()
    return base64.b64encode(buf).decode("ascii")


def unpack_coords(s):
    """Inverse of pack_coords. Returns an (N, 3) float32 array."""
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception as exc:
        raise ProtocolError(f"invalid base64: {exc}") from exc
    if len(raw) % 12 != 0:
        raise ProtocolError(
            f"buffer of {len(raw)} bytes is not a whole number of 3-vectors"
        )
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 3)
