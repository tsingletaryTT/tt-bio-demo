"""Verify the demo can actually run before it claims to be ready.

Per spec §6 the point is that problems surface at a desk, not at the venue. So
preflight reports *every* problem it finds in one pass rather than stopping at
the first — an operator fixing things the night before wants the whole list.

The trajectory-tap check is here for a specific reason: if the tap is broken,
folds still succeed and produce correct structures, and the only symptom is that
nothing condenses on screen. That is a failure the demo cannot detect while
running, so it is checked before starting.
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
        if not (weights_dir / name).is_file():
            missing.append(f"model weights: {weights_dir / name}")

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

    for item in missing:
        log.error("preflight: %s", item)
    return PreflightResult(ok=not missing, missing=missing)


def not_ready_event(result):
    """The protocol event the UI uses to hold a 'preparing' screen."""
    return {"type": "not_ready", "missing": list(result.missing)}
