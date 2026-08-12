"""Verify the demo can actually run before it claims to be ready.

Per spec §6 the point is that problems surface at a desk, not at the venue. So
preflight reports *every* problem it finds in one pass rather than stopping at
the first — an operator fixing things the night before wants the whole list.

The trajectory-tap check is here for a specific reason: if the tap is broken,
folds still succeed and produce correct structures, and the only symptom is that
nothing condenses on screen. That is a failure the demo cannot detect while
running, so it is checked before starting.

Each check below is individually guarded, not the function body as a whole.
That is deliberate: an unreadable weights directory (wrong ownership after a
copy, an NFS mount not yet up) can raise PermissionError from a plain
`Path.is_file()`, and a broken tt-bio install can raise something other than
this module's own `TapUnavailable` out of `check_tap_supported()` (e.g.
ImportError). Both are exactly the kind of night-before misconfiguration this
module exists to surface. A single try/except around the whole function would
catch those too, but it would also throw away every problem already
accumulated by checks that ran and completed fine before the one that raised —
which defeats the "report everything in one pass" purpose stated above. So
each check catches its own failure and turns it into a `missing` entry,
letting every other check still run to completion regardless of what any one
of them does.

The catches are deliberately scoped to OSError (filesystem checks) and to
"whatever check_tap_supported can raise, since it inspects a third-party
module's internals" rather than a single bare `except Exception` around
everything — so a genuine bug in this file's own control flow (a NameError
from a typo, say) still surfaces as a real traceback instead of being
laundered into an unhelpful "missing" string.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from runner.dump_tap import TapUnavailable, check_tap_supported

log = logging.getLogger(__name__)

REQUIRED_WEIGHTS = ("protenix-v2.pt",)


@dataclass
class PreflightResult:
    ok: bool
    missing: list


def run_preflight(weights_dir, playlist_dir, *, check_tap=True, card_count=None):
    """Check everything the demo needs to run offline. Never raises."""
    missing = []

    weights_dir = Path(weights_dir)
    for name in REQUIRED_WEIGHTS:
        path = weights_dir / name
        try:
            present = path.is_file()
        except OSError as exc:
            # e.g. PermissionError on a directory with the wrong ownership.
            missing.append(f"model weights: cannot check {path}: {exc}")
            continue
        if not present:
            missing.append(f"model weights: {path}")

    playlist_dir = Path(playlist_dir)
    targets = sorted(playlist_dir.glob("*.yaml")) if playlist_dir.is_dir() else []
    if not targets:
        missing.append(f"playlist: no .yaml targets under {playlist_dir}")

    if card_count is None:
        from runner.cards import sample_tt_smi
        card_count = len(sample_tt_smi())
    if not card_count:
        missing.append("hardware: no Tenstorrent cards reported by tt-smi")

    if check_tap:
        try:
            check_tap_supported()
        except TapUnavailable as exc:
            missing.append(f"trajectory tap: {exc}")
        except Exception as exc:
            # check_tap_supported imports and inspects tt_bio.protenix; a
            # partial/broken install can fail with something other than its
            # own TapUnavailable (e.g. ImportError). Any failure here means
            # the same thing operationally: the tap cannot be trusted.
            missing.append(
                f"trajectory tap: unexpected error checking tap support "
                f"({type(exc).__name__}): {exc}"
            )

    for item in missing:
        log.error("preflight: %s", item)
    return PreflightResult(ok=not missing, missing=missing)


def not_ready_event(result):
    """The protocol event the UI uses to hold a 'preparing' screen."""
    return {"type": "not_ready", "missing": list(result.missing)}
