"""Adaptive selection of the next affinity question to ask.

Pure by construction, exactly like ``ui/states.py`` and ``ui/slots.py``: it
takes the pool of questions plus a little context (what is on screen, what
was asked recently) and returns the one question to ask next. No GTK, no I/O,
no clock of its own -- which is what makes the selection policy unit-testable
in isolation (see ``tests/unit/test_questioning.py``).

Why this module exists
----------------------
Before this, ``ui/app.py`` cycled through the question list with a blind
round-robin index: it asked the same questions in the same fixed order no
matter what the visitor was looking at, and it happily re-asked the question
it had just asked. This module replaces that with a small, deterministic
policy (see :func:`select_question`):

1. **Relevance** -- prefer questions about the target currently on screen.
2. **Freshness** -- within the relevant set, ask the least-recently-asked.
3. **Fallback** -- if nothing matches the on-screen target, use the whole pool.
4. **No immediate repeat** -- never re-ask the last question when an
   alternative exists.
5. **Deterministic** -- ties break by ``id`` so the choice is stable and
   testable.

This module must never import torch, tt-bio, or GTK (see
docs/venv-bootstrap-notes.md for the venv split it depends on).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ui.playlist import Question


def select_question(
    questions: Sequence[Question],
    *,
    on_screen_target_id: str | None = None,
    recently_asked: Sequence[str] = (),
) -> Question | None:
    """Pick the next affinity question to ask.

    Args:
        questions: The full pool of available questions.
        on_screen_target_id: The ``target_id`` currently on screen, if any.
            Questions about this target are preferred (rule 1 below).
        recently_asked: Question ids asked most recently, most-recent first.
            Drives both the freshness rule (rule 2) and the no-immediate-
            repeat rule (rule 4). An empty sequence means "nothing asked
            yet", in which case every question is equally fresh.

    Selection policy, in priority order:

    1. **Relevance** -- prefer questions whose ``target_id`` matches
       ``on_screen_target_id`` (ask about what the visitor is looking at).
    2. **Freshness** -- within the relevant set, pick the
       least-recently-asked question. A question absent from
       ``recently_asked`` is the freshest of all.
    3. **Fallback** -- if no question matches the on-screen target, fall back
       to the whole pool rather than asking nothing.
    4. **No immediate repeat** -- the most-recently-asked question is ranked
       last, so it is only ever chosen when it is the sole candidate (i.e.
       there is no alternative to repeat it against).
    5. **Deterministic tie-break** -- any remaining tie breaks by ``id``, so
       the choice is stable and reproducible.

    Returns the chosen :class:`~ui.playlist.Question`, or ``None`` if
    ``questions`` is empty.
    """
    pool = list(questions)
    if not pool:
        return None

    # 1. Relevance: prefer the on-screen target's own questions.
    if on_screen_target_id is not None:
        relevant = [q for q in pool if q.target_id == on_screen_target_id]
    else:
        relevant = []
    # 3. Fallback: no question about the on-screen target -> use the whole pool.
    if not relevant:
        relevant = pool

    # 2/4/5. Rank by "least recently asked" (most stale first), tie-break by
    # id. Lower rank == more preferred. A question absent from
    # `recently_asked` is the freshest of all and ranks first.
    n = len(recently_asked)

    def _rank(q: Question) -> tuple[int, str]:
        for i, qid in enumerate(recently_asked):
            if qid == q.id:
                # In the recency window: the older it is (higher i), the more
                # preferred. The most recent (i == 0) ranks worst here, which
                # is exactly what keeps us from immediately repeating it.
                return (n - i, q.id)
        # Never asked within the window: freshest of all, so most preferred.
        return (0, q.id)

    return min(relevant, key=_rank)
