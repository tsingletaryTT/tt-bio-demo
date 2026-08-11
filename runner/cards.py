"""Card health, and which cards may receive work.

Per spec §6, a card that runs too hot stops being scheduled and the UI dims it.
The runner samples temperature for its own scheduling decisions; the UI samples
tt-smi separately for display. That duplication is deliberate — routing the
display's data through the runner would couple the thing that must never fail to
the thing most likely to.

Card reset is never attempted automatically. A demo that resets hardware on its
own is a demo that can fail in an interesting way in front of an audience.
"""

import json
import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CardState:
    index: int
    board_type: str
    temperature_c: float
    power_w: float
    aiclk_mhz: float


def _number(value):
    return float(str(value).strip())


def parse_tt_smi(snapshot):
    """Parse a `tt-smi -s` snapshot dict into CardStates.

    Cards whose telemetry cannot be read are skipped rather than raising: one
    unreadable card must not blind the scheduler to the other three.
    """
    cards = []
    for index, device in enumerate(snapshot.get("device_info", []) or []):
        board = device.get("board_info", {}) or {}
        telemetry = device.get("telemetry", {}) or {}
        try:
            cards.append(CardState(
                index=index,
                board_type=board.get("board_type", "unknown"),
                temperature_c=_number(telemetry.get("asic_temperature")),
                power_w=_number(telemetry.get("power")),
                aiclk_mhz=_number(telemetry.get("aiclk")),
            ))
        except (TypeError, ValueError):
            log.warning("card %d has unreadable telemetry; skipping it", index)
    return cards


def sample_tt_smi(timeout=5.0):
    """Run `tt-smi -s` and parse it. Returns [] if it cannot be read."""
    try:
        out = subprocess.run(["tt-smi", "-s", "--snapshot_no_tty"],
                             capture_output=True, timeout=timeout, check=True)
        return parse_tt_smi(json.loads(out.stdout))
    except Exception:
        log.exception("tt-smi sample failed; treating as no telemetry")
        return []


class CardPool:
    """Tracks which cards are healthy, idle, and eligible for work."""

    def __init__(self, indices, max_temp_c=85.0):
        self.max_temp_c = max_temp_c
        self._states = {i: "idle" for i in indices}

    def update(self, cards):
        """Fold in a telemetry sample. Returns card_state events for changes."""
        events = []
        for card in cards:
            if card.index not in self._states:
                continue
            was = self._states[card.index]
            if card.temperature_c >= self.max_temp_c:
                if was != "quarantined":
                    self._states[card.index] = "quarantined"
                    log.warning("card %d at %.1fC exceeds %.1fC; not scheduling to it",
                                card.index, card.temperature_c, self.max_temp_c)
                    events.append({"type": "card_state", "card": card.index,
                                   "state": "quarantined"})
            elif was == "quarantined":
                self._states[card.index] = "idle"
                log.info("card %d cooled to %.1fC; schedulable again",
                         card.index, card.temperature_c)
                events.append({"type": "card_state", "card": card.index,
                               "state": "idle"})
        return events

    def schedulable(self):
        return sorted(i for i, state in self._states.items() if state == "idle")

    def mark_busy(self, index):
        self._states[index] = "busy"
        return {"type": "card_state", "card": index, "state": "busy"}

    def mark_idle(self, index):
        if self._states.get(index) == "quarantined":
            return None      # a hot card stays out until telemetry clears it
        self._states[index] = "idle"
        return {"type": "card_state", "card": index, "state": "idle"}
