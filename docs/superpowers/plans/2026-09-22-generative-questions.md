# Generative Questions — Implementation Plan

**Date:** 2026-09-22
**Builds on:** `2026-09-15-affinity-questions.md` (v1, complete) · spec `docs/superpowers/specs/2026-09-15-affinity-qa-design.md`
**Status:** Plan — ready to implement

## 0. Goal

Make the booth's affinity questions **generative**: a larger pool of *real*
questions, asked **adaptively** to what is on screen — instead of three
hand-picked questions cycled by blind round-robin.

Two axes (direction set 2026-09-22):

1. **Scale from real data** — grow from 3 to a curated catalog of *real,
   citable* protein–ligand pairs.
2. **Adaptive / contextual selection** — choose *which* question to ask from
   runtime context (what is on screen), not blind round-robin.

**Out of scope (content-honesty, spec §7):** no invented pairs, no
LLM-hallucinated science, no invented "binds tightly" verdicts. Every question
maps to a real, citable pair; every displayed number is what nesso1 actually
returned.

## 1. Current state (verified in code)

- `playlist/questions.yaml` — 3 static questions (`dhfr_mtx`, `trypsin_bam`,
  `fkbp12_sb3`).
- `ui/playlist.py::load_questions()` loads + validates them (each `target_id`
  must be a real manifest target).
- `ui/app.py::_ask_next_question()` round-robins `self.questions` via a plain
  int index (`_ask_question_index`), fired by the attract loop's
  `ASK_QUESTION` cue (`ui/attract.py`, offset 82 s).
- Selection is **blind round-robin** — it ignores what is on screen.
- The app already tracks the on-screen target per slot
  (`view.current_target_id` / `shown_target_id`) and the visitor's pick
  (`router.selected_target`) — the signal an adaptive selector needs already
  exists.

## 2. Design

### 2.1 Data — the question catalog (Axis 1: scale)

- Keep `playlist/questions.yaml` as the active set the booth asks.
- Grow it from 3 to a curated catalog of **real, citable** pairs. Each entry
  keeps the existing schema (`id`, `target_id`, `question`, `ligand_name`,
  `expected_s`) and gains an optional `source` (citation) for traceability.
- **Content-honesty gate:** every entry is a real, citable protein–ligand
  pair. No invented pairs. `expected_s` stays `null` until measured on this
  hardware.

### 2.2 Selection — the adaptive selector (Axis 2: contextual)

New **pure** module `ui/questioning.py` (mirrors `ui/slots.py` /
`ui/states.py`): a decision with no I/O, no clock of its own, fully
unit-testable.

```
select_question(questions, *, on_screen_target_id=None,
                recently_asked=()) -> Question | None
```

Policy (deterministic, in priority order):

1. **Relevance** — prefer questions whose `target_id == on_screen_target_id`
   (ask about what the visitor is looking at).
2. **Freshness** — within the preferred set, pick the least-recently-asked.
3. **Fallback** — if nothing matches the on-screen target, use the whole pool.
4. **No immediate repeat** — never repeat the immediately-previous question
   when an alternative exists.
5. **Deterministic tie-break** by `id`, so the choice is stable and testable.

### 2.3 Integration

- `ui/app.py::_ask_next_question()` calls `select_question(...)` instead of
  the blind index, feeding the current on-screen target and a bounded recency
  history (a bounded deque of recently-asked ids).
- The attract-loop cue (`ASK_QUESTION`) and its cadence are unchanged.

## 3. Tasks (TDD, test-first)

### Task 1 — The adaptive selector (pure)  ← START HERE
- New `ui/questioning.py` with `select_question(...)`.
- Tests: relevance preference, freshness ordering, fallback when no on-screen
  match, no-immediate-repeat, deterministic tie-break, empty/edge cases.
- Self-contained; no data or app changes needed.

### Task 2 — Wire the selector into the app
- Replace the `_ask_question_index` round-robin in `_ask_next_question()` with
  a call to `select_question(...)`.
- Maintain a bounded recency deque; source `on_screen_target_id` from the
  active slot's `current_target_id` (fall back to `router.selected_target`).
- App-level tests using the existing fakes (`tests/unit/_appfakes.py`).

### Task 3 — Grow the catalog (scale from real data)
- Curate additional **real, citable** pairs into `playlist/questions.yaml`
  (keep the existing 3).
- Add optional `source` field; extend `load_questions()` to accept/validate it
  (non-breaking).
- Every new entry: real pair, citable source, `expected_s: null` until
  measured.

### Task 4 — Integration + verification
- `--mock` end-to-end: confirm the attract loop asks context-appropriate
  questions.
- Full suite green (both venv halves).

## 4. Content-honesty guardrails (non-negotiable)

- Every question ↔ a real, citable pair. No invented pairs or ligands.
- Displayed score is exactly what nesso1 returned (probability + hedged IC50);
  no invented tiers.
- `expected_s` stays `null` until actually measured on this hardware.

## 5. Open decisions

- Catalog size target (10? 25? 50?) — bounded by curation effort +
  content-honesty review.
- Recency window size N; whether "on-screen" means the focused slot or any
  visible slot.
- Whether to surface a source/citation affordance in the UI (default: no —
  keep the visitor view clean).

## 6. Amendment (2026-09-22, hardware pass) — Task 3's catalog growth reverted

Tasks 1 and 2 (the adaptive selector and its wiring) shipped as designed and
are unchanged by this amendment. Task 3's six curated additions did not
survive the hardware test pass and were reverted; see
`playlist/questions.yaml`'s own header for the measured evidence.

The plan's Task 3 missed a fact about the runtime this section 2.1 did not
account for: `runner/daemon.py::_accept_question` resolves the affinity
scoring input purely from `target_id` — it reuses the SAME fold file
`playlist/manifest.yaml` already points that target at, because that file is
the one place each protein's baked-in ligand (one CCD code per target) lives.
There is no per-*question* input file, only a per-*target* one. So a second
question naming a second ligand for a target that already has one (`dhfr_tm`
alongside `dhfr_mtx`, etc.) does not score the named ligand at all — it
silently scores the SAME ligand the first question does, and the UI shows
that score captioned with the wrong ligand's name. Confirmed on real
hardware: `dhfr_mtx` and `dhfr_tm` scored 0.9764 and 0.9770 respectively —
noise around the same number, not two different ligands, because they ran
the identical structure. `hsa_warfarin`/`hsa_ibuprofen` failed differently:
`hsa`'s fold input has no ligand entity at all, so both always end in
`answer_error` (`ValueError: No protein or ligand tokens found in the
batch`).

Reverted to the original three pairs, which are the only ones where the
question's named ligand and the file actually scored agree. Their
`expected_s` was measured for the first time in the same pass (mean of 3
warm runs, model resident, chip 0): dhfr_mtx 10.1s, trypsin_bam 10.7s,
fkbp12_sb3 8.3s.

Growing the catalog past one question per target for real is possible, but
needs two things Task 3 didn't do: a per-question input file (a new vendored
example with the correct CCD ligand code for the *named* ligand) and a
daemon change to route the affinity score off `question_id`, not `target_id`.
Worth its own spec if picked back up — it is real engineering, not a data
addition.
