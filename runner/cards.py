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
    """Tracks which cards are healthy, idle, and eligible for work.

    Two independent facts are tracked per card:

    - `busy`  — a job is in flight on it (set by mark_busy/mark_idle).
    - `hot`   — its last-sampled temperature was at or above max_temp_c
                (set by update()).

    These are kept as two separate booleans rather than one status string
    because they really are independent: a fold keeps running on a card that
    overheats mid-fold, so "a job is in flight" must survive the card getting
    hot, and must survive the card cooling back down before that job finishes.
    An earlier version of this class folded both facts into a single
    `"idle"/"busy"/"quarantined"` state, which meant overheating a busy card
    clobbered the fact that it was busy — and cooling it back down then
    silently un-quarantined it and made it schedulable while the original job
    was still running.

    Wire precedence: the `card_state` event and the UI's dimming both take a
    single `state` string, so a card that is both busy and hot has to pick
    one. Quarantined wins: heat is the fact the scheduler and the UI most
    need to see, and "there's a job running on it" is comparatively minor
    bookkeeping once you already know not to send it more work. See
    `_reported_state`.
    """

    def __init__(self, indices, max_temp_c=85.0):
        self.max_temp_c = max_temp_c
        self._indices = list(indices)
        self._busy = {i: False for i in self._indices}
        self._hot = {i: False for i in self._indices}

    def _reported_state(self, index):
        """The single state string this card would be reported as right now.

        Precedence: quarantined beats busy beats idle (see class docstring).
        """
        if self._hot.get(index, False):
            return "quarantined"
        if self._busy.get(index, False):
            return "busy"
        return "idle"

    def update(self, cards):
        """Fold in a telemetry sample. Returns card_state events for changes.

        Only updates the `hot` fact — `busy` is untouched here and can only
        change via mark_busy/mark_idle. A card that is busy when it crosses
        max_temp_c is reported as quarantined (per the precedence rule) but
        stays busy underneath; if it cools before mark_idle is called, it is
        reported as busy again, not idle — it never became schedulable
        through temperature recovery alone.
        """
        events = []
        for card in cards:
            if card.index not in self._hot:
                continue
            before = self._reported_state(card.index)
            self._hot[card.index] = card.temperature_c >= self.max_temp_c
            after = self._reported_state(card.index)
            if after == before:
                continue
            if after == "quarantined":
                log.warning("card %d at %.1fC exceeds %.1fC; not scheduling to it",
                            card.index, card.temperature_c, self.max_temp_c)
            elif before == "quarantined":
                log.info("card %d cooled to %.1fC", card.index, card.temperature_c)
            events.append({"type": "card_state", "card": card.index, "state": after})
        return events

    def schedulable(self):
        """Indices that are neither busy nor hot — safe to hand a job to."""
        return sorted(i for i in self._indices if not self._busy[i] and not self._hot[i])

    def all_indices(self):
        """Every card this pool tracks, regardless of busy/hot state.

        Distinct from schedulable() on purpose: the daemon's `hello` greeting
        (runner/daemon.py's Daemon._hello) needs to describe what hardware
        exists, not what happens to be free at the instant a UI connects — a
        card that is mid-fold has not stopped existing, and schedulable()
        exists for the dispatch decision, not for describing inventory. Used
        to be the one call site (_hello) that reported schedulable() instead,
        which meant a card busy mid-fold silently vanished from every UI that
        connected while it was working.
        """
        return sorted(self._indices)

    def mark_busy(self, index):
        """Reserve a card for a job.

        Raises ValueError if the card is quarantined. schedulable() already
        excludes hot cards, so a correct caller never reaches this branch —
        it exists to fail loudly on a scheduler bug (dispatching to a card it
        should never have picked), not as a control path callers branch on.
        """
        if self._hot.get(index, False):
            raise ValueError(f"card {index} is quarantined; refusing to mark it busy")
        self._busy[index] = True
        return {"type": "card_state", "card": index, "state": "busy"}

    def mark_idle(self, index):
        """Release a card after its job finishes.

        Returns a card_state event dict, or None if the card is still hot:
        in that case it remains quarantined rather than idle, and stays out
        of schedulable() until a later update() sees it cool down.
        """
        self._busy[index] = False
        if self._hot.get(index, False):
            return None      # a hot card stays out until telemetry clears it
        return {"type": "card_state", "card": index, "state": "idle"}
