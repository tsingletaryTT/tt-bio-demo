# Affinity Questions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a visible queue of affinity questions ("does this ligand bind this protein?") that the booth answers with a real tt-bio nesso1 computation plus a visual binding-pocket highlight on the existing ribbon — without touching the existing fold/showcase state machine.

**Architecture:** A question rides the existing visitor-pick pathway (fold, stage, frame, showcase — all unchanged) with a side-channel `question_id` attached. A dedicated fifth-worker-equivalent (one chip, reserved) holds nesso1 resident and answers independently of any fold, joined back to the UI by `target_id`. The pocket highlight is pure client-side geometry over a `.cif` the UI has already parsed for cartoon rendering — no new runner capability for that half at all.

**Tech Stack:** Python 3.12, `.venvs/venv-runner` (tt-bio 0.8.0+, whichever version is pinned when this plan executes — check `scripts/setup-venvs.sh`'s `TT_BIO_VERSION` first), `.venvs/venv-ui` (GTK4, gemmi), `protocol/events.py` (shared, stdlib+numpy only).

**Spec:** [`../specs/2026-09-15-affinity-qa-design.md`](../specs/2026-09-15-affinity-qa-design.md) — read it in full before Task 1. §2 (why nesso1 needs no structure), §3 (why `ui/states.py` is untouched), §4 (protocol), §5 (chip dedication), §7 (content honesty) are load-bearing for later tasks.

## Global Constraints

- **Run everything with `.venvs/venv-runner/bin/python3` or `.venvs/venv-ui/bin/python3`, never bare `python3`.** See CLAUDE.md / docs/followups.md.
- **Take a gozer lease before anything that opens `/dev/tenstorrent/*`** — including a bare `import tt_bio` that transitively imports ttnn. `gozer run --chips 1 --who "claude:affinity-qa" --reason "<task>" -- <command>`. Use `importlib.util.find_spec("tt_bio")` to check installation without opening hardware.
- **`ui/states.py`'s `BoothState`/`StateMachine` are not modified by this plan.** If a task seems to need a change there, stop and re-read spec §3 — the design is additive by construction; a felt need to touch it means a task is scoped wrong.
- **`protocol/events.py` stays stdlib+numpy only.** It is imported by both venvs.
- **Nothing the runner emits may be shown to a visitor verbatim.** `answer_error.message` goes to logs only, same rule as `job_error.message`.
- **The score's copy states only what tt-bio's own docs say the number is** — never an invented "binds tightly/weakly" verdict tier unless tt-bio's own API defines thresholds. See spec §7.
- **`expected_s` for every question starts `null`/absent and is rendered as "not yet timed,"** exactly like `ui/gallery.py` already handles an unmeasured fold target. Do not invent a number.
- **One chip is permanently reserved for Q&A when >1 chip is detected; on a 1-chip box, `qa_capable: false` and the question UI hides itself entirely.** No time-sharing fallback — see spec §5 for why that was rejected.
- **Never run `tt-smi -r` by hand or touch the system SFPI.** Card reset stays a documented manual step; `gozer release` handles cleanup.
- Commit after every task with conventional-commit prefixes (`feat:`, `fix:`, `test:`, `chore:`, `docs:`).

## File Structure

| File | Responsibility |
|---|---|
| `docs/spike-nesso1-affinity.md` | Task 1's findings: real API call, timing, output shape. Every later task's code is built on this. |
| `protocol/events.py` | Modify: add `question`/`answer_start`/`answer_done`/`answer_error`, bump `PROTOCOL_VERSION`. |
| `playlist/questions.yaml` | New content file: question id, target_id, ligand name, question text, `expected_s: null`. |
| `ui/playlist.py` | Modify: add `load_questions()`, validating each `target_id` exists in `manifest.yaml`. |
| `runner/affinity.py` | New: `AffinityScorer` — owns the Q&A chip's device + resident nesso1 model, mirrors `runner/folder.py`'s `Folder`. |
| `runner/daemon.py` | Modify: reserve one `WorkerSpec` for Q&A, `_accept_question`, dispatch to the affinity worker, `qa_capable` in `hello`. |
| `runner/workers.py` | Modify: `worker_specs` gains a `reserve_for_qa` split helper. |
| `ui/pocket.py` | New: pure geometry — protein residues near ligand atoms, from an already-parsed structure. |
| `ui/client.py` | Modify: decode `answer_start`/`answer_done`/`answer_error`, expose callbacks. |
| `ui/questions.py` | New: `QuestionQueuePanel(Gtk.Box)` — pending/in-flight/answered, mirrors `ui/panels.py`'s `PipelinePanel`/`TelemetryPanel`. |
| `ui/structure_view.py` (or wherever ribbon coloring lives — confirm in Task 9) | Modify: additive highlight-residue-set on top of the existing pLDDT ramp. |
| `ui/app.py` | Modify: mount `QuestionQueuePanel`, join `answer_done` to the on-screen structure by `target_id`, attract-loop cadence, gallery "ask" affordance. |
| `runner/mock.py` (or wherever `--mock` replay lives) | Modify: replay `question`/`answer_*` events for UI-only development. |
| `tests/unit/`, `tests/integration/` | Unit tests with no device; hardware-gated integration tests. |

---

### Task 1: Hardware spike — confirm the real nesso1 API, timing, and output shape

**Files:**
- Create: `docs/spike-nesso1-affinity.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the concrete facts every later task's code is built on. This task's own findings MAY require correcting the draft code in Tasks 4 and 5 below — if so, edit those tasks' code blocks in this plan file before starting them, the same way this project's plans have always been corrected against what a spike actually finds (see `docs/superpowers/plans/2026-08-11-runner-daemon.md`'s own precedent).

This is first, unconditionally, per this project's own established practice: the Phase 3a plan was built on a spike (`spike-real-fold.md`) because the original design assumed things about `dump_fn` nobody had watched run, and three of those assumptions were wrong. The same risk exists here: this plan's draft code below assumes a CLI-shaped `tt-bio affinity --model nesso1` call because that's what a research pass into tt-bio's public surface found — but nobody in this project has actually called it.

- [ ] **Step 1: Take a gozer lease**

```bash
gozer run --chips 1 --who "claude:affinity-qa-spike" --reason "confirm nesso1 API surface/timing before writing runner/affinity.py" -- sleep 3600
```

Or use `gozer acquire`/`release` for a session spanning the rest of this task. If gozer reports contention, use the gozer-keymaster skill before proceeding.

- [ ] **Step 2: Find the real API surface**

Inside `.venvs/venv-runner`, inspect what's actually importable/callable:

```bash
.venvs/venv-runner/bin/python3 -c "
import tt_bio
print([n for n in dir(tt_bio) if 'affin' in n.lower() or 'nesso' in n.lower()])
"
.venvs/venv-runner/bin/python3 -m tt_bio.main --help 2>&1 | grep -i -A3 affinity
```

If it's a CLI-only surface (`tt-bio affinity --model nesso1 <input.yaml>`), find whether there's an in-process Python entry point underneath it (grep `tt_bio`'s installed package for the CLI's own implementation — `python3 -c "import tt_bio.main, inspect; print(inspect.getsourcefile(tt_bio.main))"` then read the `affinity` subcommand's source) so `runner/affinity.py` can call it in-process rather than shelling out per-question, the same residency-preserving reasoning `Folder` uses for protenix-v2.

- [ ] **Step 3: Run it for real against one of the three existing complexes**

```bash
.venvs/venv-runner/bin/python3 -c "
import time
# Adjust this call to whatever Step 2 found. Time it twice: cold (first call,
# pays model load) and warm (second call, same process) -- the same
# cold/warm distinction this project applies to every fold measurement.
t0 = time.time()
# result = <the real call>('examples/affinity_dhfr.yaml')
print('cold:', time.time() - t0)
t0 = time.time()
# result = <the real call>('examples/affinity_dhfr.yaml')
print('warm:', time.time() - t0)
print(repr(result))
"
```

Record: the exact call signature, the exact return type/shape (a bare float? a dict with named fields? what units/scale does tt-bio's own docstring or `docs/model-capabilities.md` — shipped in 0.8.0 — say the number means?), cold and warm wall-clock on this hardware, and whether it needs the protein+ligand file's existing `sequences:`/`properties: affinity:` block verbatim or a reshaped input.

- [ ] **Step 4: Confirm it does NOT require the fold to have run first**

Call it on `examples/affinity_fkg.yaml` (FKBP12+SB3) in a fresh process with no prior fold — spec §2's whole architecture depends on this being true. If it turns out to require coordinates, STOP and flag this to the user before continuing any further task: it would invalidate spec §2/§3's decoupling and needs a design revision, not a workaround improvised mid-plan.

- [ ] **Step 5: Release the lease and write the report**

```bash
gozer release <lease-id>   # or let `gozer run`'s wrapped command exit
```

Write `docs/spike-nesso1-affinity.md` with: the exact call (module, function/CLI, arguments), a table of cold/warm timings for at least two of the three complexes, the exact return shape with a real captured example, and an explicit yes/no on Step 4. Then go back and correct Tasks 4 and 5's draft code below to match reality — cross out and replace, don't leave the wrong version standing.

- [ ] **Step 6: Commit**

```bash
git add docs/spike-nesso1-affinity.md
git commit -m "docs: nesso1 affinity spike -- real API, timing, output shape"
```

---

### Task 2: Protocol — `question`/`answer_*` events

**Files:**
- Modify: `protocol/events.py`
- Test: `tests/unit/test_protocol_events.py` (extend the existing file if present — check first)

**Interfaces:**
- Consumes: nothing new.
- Produces: `PROTOCOL_VERSION = 4`; `question_message(question_id, target_id) -> dict`; new members of `EVENT_TYPES`: `"answer_start"`, `"answer_done"`, `"answer_error"`; `encode`/`decode` handle all three like every other event.

- [ ] **Step 1: Write the failing tests**

```python
from protocol.events import (EVENT_TYPES, PROTOCOL_VERSION, decode, encode,
                              decode_client_message, question_message)


def test_protocol_version_bumped_for_questions():
    assert PROTOCOL_VERSION == 4


def test_answer_events_are_recognized():
    for etype in ("answer_start", "answer_done", "answer_error"):
        assert etype in EVENT_TYPES


def test_question_message_round_trips():
    msg = question_message("q1", "dhfr")
    assert msg == {"type": "question", "version": PROTOCOL_VERSION,
                   "question_id": "q1", "target_id": "dhfr"}
    line = __import__("protocol.events", fromlist=["encode_client_message"])
    from protocol.events import encode_client_message
    decoded = decode_client_message(encode_client_message(msg))
    assert decoded == msg


def test_answer_done_encodes_and_decodes():
    event = {"type": "answer_done", "question_id": "q1", "target_id": "dhfr",
             "score": 0.42}
    assert decode(encode(event)) == event


def test_answer_error_message_is_present_but_not_special():
    # Same rule as job_error: the runner may put detail in `message`; it is
    # this event's job only to carry it, never to sanitize it -- sanitizing
    # is the UI's job at display time, not the protocol's.
    event = {"type": "answer_error", "question_id": "q1", "target_id": "dhfr",
             "message": "nesso1 raised ValueError: ..."}
    assert decode(encode(event)) == event
```

- [ ] **Step 2: Run to verify failure**

```bash
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_protocol_events.py -v -k "question or answer or version_bumped"
```
Expected: FAIL (`ImportError: cannot import name 'question_message'`, and the version/EVENT_TYPES assertions failing).

- [ ] **Step 3: Implement**

In `protocol/events.py`, bump the version comment block (append, don't delete the existing history) and the constant:

```python
# Bumped 3 -> 4 for affinity questions. Adds one client->server message
# (`question`) and three events (`answer_start`, `answer_done`,
# `answer_error`) -- same reasoning as every prior bump: a v4 UI against a
# v3 daemon would send `question` lines the daemon logs and drops (see
# Daemon.on_client_message's "anything not X or Y is logged and dropped"
# guard), silently promising a capability that daemon does not have; a v3
# UI against a v4 daemon would be handed answer_* events it cannot decode.
PROTOCOL_VERSION = 4
```

Add to `EVENT_TYPES`'s frozenset literal: `"answer_start"`, `"answer_done"`, `"answer_error"`.

Add, near `pick_message`/`egg_message`:

```python
def question_message(question_id, target_id):
    """Build a `question` client->server message: "answer this question,
    whose target is already a playlist entry." Mirrors pick_message/
    egg_message exactly -- see their docstrings for the wire-format
    reasoning this repeats."""
    return {"type": "question", "version": PROTOCOL_VERSION,
            "question_id": question_id, "target_id": target_id}
```

Extend `decode_client_message`'s recognized-kind check (wherever `"pick"`/`"egg"` are enumerated) to also accept `"question"`.

- [ ] **Step 4: Run to verify pass**

```bash
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_protocol_events.py -v
```
Expected: PASS, full file.

- [ ] **Step 5: Commit**

```bash
git add protocol/events.py tests/unit/test_protocol_events.py
git commit -m "feat: add question/answer_* protocol events, bump to v4"
```

---

### Task 3: `playlist/questions.yaml` + loader

**Files:**
- Create: `playlist/questions.yaml`
- Modify: `ui/playlist.py`
- Test: `tests/unit/test_playlist.py` (extend existing)

**Interfaces:**
- Consumes: `ui/playlist.py`'s existing manifest-loading machinery (reuse whatever function already validates `manifest.yaml`, e.g. `load_manifest()` — confirm the exact name by reading the file before writing this task's code).
- Produces: `load_questions(path=None) -> list[Question]` where `Question` is a small dataclass/namedtuple with fields `id, target_id, question, ligand_name, expected_s` (the last `None` when unset). Raises on a `target_id` that does not match any entry in the already-loaded fold manifest.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from ui.playlist import load_questions, PlaylistError


def test_loads_three_questions_from_the_shipped_manifest():
    questions = load_questions()
    ids = {q.id for q in questions}
    assert ids == {"dhfr_mtx", "trypsin_bam", "fkbp12_sb3"}


def test_every_question_targets_a_real_playlist_entry():
    questions = load_questions()
    for q in questions:
        assert q.target_id in {"dhfr", "trypsin", "fkbp12"}


def test_unmeasured_expected_s_is_none():
    questions = load_questions()
    assert all(q.expected_s is None for q in questions)


def test_a_question_naming_a_nonexistent_target_is_rejected(tmp_path):
    bad = tmp_path / "questions.yaml"
    bad.write_text("- id: x\n  target_id: not_a_real_target\n  question: 'q?'\n"
                   "  ligand_name: 'L'\n")
    with pytest.raises(PlaylistError, match="not_a_real_target"):
        load_questions(bad)
```

- [ ] **Step 2: Run to verify failure**

```bash
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_playlist.py -v -k question
```
Expected: FAIL — `ImportError: cannot import name 'load_questions'`.

- [ ] **Step 3: Write `playlist/questions.yaml`**

```yaml
# Affinity questions this booth can ask and answer. See
# docs/superpowers/specs/2026-09-15-affinity-qa-design.md sections 2 and 7
# for why these three specifically: each target_id below is an existing
# playlist.yaml entry whose fold input (examples/affinity_*.yaml) already
# carries a `properties: affinity:` block the fold runner has always
# ignored -- this file is what finally reads it.
#
# expected_s stays null until the implementation's hardware spike
# (docs/spike-nesso1-affinity.md) measures a real number. ui/questions.py
# renders a null expected_s as "not yet timed", the same handling
# ui/gallery.py already gives an unmeasured fold target -- never a guess.
- id: dhfr_mtx
  target_id: dhfr
  question: "Does methotrexate block dihydrofolate reductase?"
  ligand_name: "Methotrexate"
  expected_s: null

- id: trypsin_bam
  target_id: trypsin
  question: "Does benzamidine block trypsin?"
  ligand_name: "Benzamidine"
  expected_s: null

- id: fkbp12_sb3
  target_id: fkbp12
  question: "Does SB3 bind FKBP12?"
  ligand_name: "SB3"
  expected_s: null
```

- [ ] **Step 4: Implement `load_questions`**

Read `ui/playlist.py` first to match its existing style (`PlaylistError`, path-resolves-against-this-file's-own-directory convention documented at the top of `manifest.yaml`, dataclass vs namedtuple choice, YAML loading helper already in use). Then add:

```python
@dataclass(frozen=True)
class Question:
    id: str
    target_id: str
    question: str
    ligand_name: str
    expected_s: float | None


def load_questions(path=None):
    """Load playlist/questions.yaml, validating every target_id against the
    fold manifest (load_manifest()) -- a question naming a target this booth
    cannot fold is a config error, not a runtime one, and must fail loudly
    at load time rather than surface as a silent no-op reveal later."""
    path = Path(path) if path is not None else _QUESTIONS_PATH  # mirror
    # whatever constant/pattern load_manifest() uses for manifest.yaml's own
    # path resolution -- same "resolves against this file's own directory"
    # rule from manifest.yaml's header comment applies here.
    raw = yaml.safe_load(path.read_text()) or []
    manifest_ids = {t.id for t in load_manifest()}
    questions = []
    for entry in raw:
        target_id = entry["target_id"]
        if target_id not in manifest_ids:
            raise PlaylistError(
                f"question {entry.get('id')!r} targets {target_id!r}, "
                f"which is not in the fold manifest")
        questions.append(Question(
            id=entry["id"], target_id=target_id, question=entry["question"],
            ligand_name=entry["ligand_name"],
            expected_s=entry.get("expected_s")))
    return questions
```

- [ ] **Step 5: Run to verify pass**

```bash
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_playlist.py -v
```
Expected: PASS, full file (confirm no regression on existing manifest tests).

- [ ] **Step 6: Commit**

```bash
git add playlist/questions.yaml ui/playlist.py tests/unit/test_playlist.py
git commit -m "feat: add playlist/questions.yaml and its loader"
```

---

### Task 4: `runner/affinity.py` — `AffinityScorer`

**Files:**
- Create: `runner/affinity.py`
- Test: `tests/unit/test_affinity.py`

**Interfaces:**
- Consumes: Task 1's confirmed real API call. Task 3's `Question`/`load_questions` only for the input-file path resolution convention — this module itself takes a raw path, same as `Folder.fold()` does.
- Produces: `class AffinityScorer` with `.load(device_id)` (opens device, loads nesso1, mirrors `Folder.load()`) and `.score(question_id, target_id, input_path, emit)` which calls `emit({"type": "answer_start", ...})` then the real nesso1 call then `emit({"type": "answer_done"/"answer_error", ...})` — same emit-callback shape `Folder.fold()` already uses, so `runner/daemon.py` can wire it identically.

**CORRECTED by Task 1's spike (`docs/spike-nesso1-affinity.md`) — read it in full
before touching this task.** The code below in this plan file is the ORIGINAL
draft, built on the research pass's guess (a CLI-style
`tt-bio affinity --model nesso1`), struck through and replaced with what the
spike actually found. Do not implement the struck-through version.

~~The code below is a draft built on the *research pass's* best guess at the
API shape (a CLI-style `tt-bio affinity --model nesso1`). Task 1 may have
corrected this — reread Task 1's final note and `docs/spike-nesso1-affinity.md`
before writing a single line here, and adjust the call inside `_score_real` to
match what was actually found, the same way `Folder`'s own module docstring
records that its design was corrected against `spike-real-fold.md`.~~

**What the spike actually found (§1–§2 of the spike doc):**

- There IS a CLI (`tt-bio affinity --model nesso1 <data>`), confirmed by
  `--help` output, but it is a thin wrapper around one in-process function:
  **`tt_bio.nesso1.screen(data, out_dir, use_tenstorrent=True, ...)`**. This is
  the entry point to call — no shelling out needed.
- `screen()` takes a **file path** (not a directory) fine for one question at
  a time; it also accepts a directory for a multi-target screen, which this
  booth does not need.
- Weights are not bundled and were `state='missing'` on this box before the
  spike: `tt_bio.weights.fetch("nesso1")` and `.fetch("nesso1-ccd")` had to run
  once. `AffinityScorer.load()` should not assume the weights are already on
  disk — see the spike doc §2.1 for the exact calls, and flag the
  provisioning gap (postinst / `doctor.sh` need this too, same as
  `protenix-v2`) as a follow-up rather than solving it inside this task.
- Return shape is `list[dict]` of **named scalar fields**, not a bare float —
  `affinity_pred_value` (continuous, same log10(IC50 µM) scale as tt-bio's
  Boltz-2 affinity head — see spike doc §3.3 for the evidence) and
  `affinity_probability_binary` (a literal `[0, 1]` probability of being a
  binder — the safer field to show a visitor verbatim, per spec §7's
  no-invented-verdict rule). `AffinityScorer.score()`'s `answer_done` event
  should carry both, e.g. `{"score": row["affinity_probability_binary"],
  "affinity_pred_value": row["affinity_pred_value"]}` — exact key names for
  the event are this task's own call, but do not collapse to a single number
  without keeping the probability one, since that is the one spec §7's
  content-honesty rule is easiest to satisfy with.
- **Confirmed does NOT require a prior fold**: run in a fresh process with
  `tt_bio.protenix`/`tt_bio.opendde` never imported, scored fine. Spec §2/§3's
  architecture stands.
- **Realistic per-question latency is ~8–12 s, not just the on-device
  forward pass**, even with the `Nesso1` model held resident across calls —
  `prepare()`'s host-side YAML/RDKit/feature-tensor cost (~6–8 s) is paid on
  **every** call, repeat target or not (only the ESM-2 embedding sub-step
  caches on a repeat). See spike doc §4. Size `AffinityScorer.score()`'s
  docstring and the UI's in-flight state (Task 9) around this, not around an
  assumption that a resident model makes every question near-instant.

- [ ] **Step 1: Write the failing test with a fake scorer call**

```python
import pytest
from runner.affinity import AffinityScorer


def test_score_emits_start_then_done(monkeypatch, tmp_path):
    scorer = AffinityScorer(device_id=0)
    monkeypatch.setattr(scorer, "_score_real", lambda input_path: {"score": 0.73})
    events = []
    scorer.score("q1", "dhfr", str(tmp_path / "in.yaml"), events.append)
    assert events[0] == {"type": "answer_start", "question_id": "q1",
                          "target_id": "dhfr"}
    assert events[1] == {"type": "answer_done", "question_id": "q1",
                          "target_id": "dhfr", "score": 0.73}


def test_score_emits_error_on_exception_and_never_raises(monkeypatch, tmp_path):
    scorer = AffinityScorer(device_id=0)
    def boom(_input_path):
        raise ValueError("bad ligand")
    monkeypatch.setattr(scorer, "_score_real", boom)
    events = []
    scorer.score("q1", "dhfr", str(tmp_path / "in.yaml"), events.append)
    assert events[-1]["type"] == "answer_error"
    assert events[-1]["question_id"] == "q1"
    assert "bad ligand" in events[-1]["message"]  # detail is fine here --
    # this is the RUNNER's event, not what the UI shows; the UI is what
    # must never display it verbatim (see Global Constraints).


def test_score_never_raises_out_of_the_call(monkeypatch, tmp_path):
    scorer = AffinityScorer(device_id=0)
    monkeypatch.setattr(scorer, "_score_real",
                         lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
    # Must not raise -- same "the runner must never crash the UI" rule
    # Folder.fold() follows. A crashed Q&A worker thread must not take the
    # daemon down.
    scorer.score("q1", "dhfr", str(tmp_path / "in.yaml"), lambda e: None)
```

- [ ] **Step 2: Run to verify failure**

```bash
.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_affinity.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.affinity'`.

- [ ] **Step 3: Implement**

**CORRECTED by Task 1's spike.** The original draft here had two
`NotImplementedError` stubs marking "fill in once Task 1 reports back." Task 1
has reported back (`docs/spike-nesso1-affinity.md`); the real calls are below,
not stubs.

```python
"""Answers affinity questions on a dedicated chip, resident, the same
model-residency pattern runner/folder.py's Folder uses for protenix-v2 --
see docs/superpowers/specs/2026-09-15-affinity-qa-design.md section 5 for
why this chip is never shared with folding.

Deliberately does NOT depend on any fold having happened: nesso1 scores
from the input file's sequence + ligand alone (confirmed in
docs/spike-nesso1-affinity.md section 3.4 -- run in a fresh process with
tt_bio.protenix/tt_bio.opendde never imported) -- the pocket visualization
that pairs with this score is computed entirely client-side, in
ui/pocket.py, from a .cif this module never sees.

Realistic per-question latency is ~8-12s even with the model held resident
across calls -- tt_bio.nesso1_input.prepare()'s host-side YAML/RDKit/
feature-tensor cost (~6-8s) is paid on EVERY call, repeat target or not (see
docs/spike-nesso1-affinity.md section 4). Do not size timeouts or UI
"in-flight" copy around an assumption that residency makes this near-instant
-- it makes the on-device forward pass warm (~2-4s), not the host prep.
"""

import logging

from tt_bio.nesso1 import Nesso1, DEFAULT_SEED
from tt_bio.nesso1_input import CLI_PREDICT_ARGS, prepare, collate
import torch

log = logging.getLogger(__name__)


class AffinityScorer:
    def __init__(self, device_id):
        self.device_id = device_id
        self._model = None  # set by load(); a tt_bio.nesso1.Nesso1 instance.

    def load(self):
        """Load nesso1, once, for this worker's lifetime. Opening the device
        is Nesso1.from_pretrained's own job (through tt_bio.tenstorrent.
        get_device(), which -- per this project's own 0.6.3 upgrade notes --
        already calls ensure_p300_mesh_descriptor() internally); no separate
        device-open step is needed here, mirroring how Folder.load() relies
        on get_device() alone. self.device_id selects the chip via
        TT_VISIBLE_DEVICES in this worker's own process environment (set by
        whatever spawns it -- see runner/workers.py's split_for_qa), not a
        constructor argument threaded into this call.

        Weights are NOT bundled with tt-bio: confirmed missing on a fresh
        cache in the spike (docs/spike-nesso1-affinity.md section 2.1). This
        call does not fetch them -- provisioning (postinst / doctor.sh) is
        where `tt_bio.weights.fetch("nesso1")` and `.fetch("nesso1-ccd")`
        belong, the same place protenix-v2's weights are fetched. Calling
        this before those exist raises whatever tt-bio itself raises for a
        missing checkpoint; that is deliberately not swallowed here.
        """
        torch.set_grad_enabled(False)
        self._model = Nesso1.from_pretrained(use_tenstorrent=True)
        # screen()'s own override (see its module comment): routes triangle
        # ops through tt-bio's fused kernels rather than the CPU-only
        # cuEquivariance path the checkpoint's use_kernels: true would select.
        self._model.use_kernels = False
        self._model.predict_args.update(CLI_PREDICT_ARGS)

    def score(self, question_id, target_id, input_path, emit):
        """Score one question. Never raises -- same rule as Folder.fold():
        a wedged or erroring Q&A worker must not crash the daemon thread
        driving it."""
        emit({"type": "answer_start", "question_id": question_id,
              "target_id": target_id})
        try:
            result = self._score_real(input_path)
        except Exception as exc:
            log.exception("question %s (target %s) failed", question_id,
                           target_id)
            emit({"type": "answer_error", "question_id": question_id,
                  "target_id": target_id, "message": str(exc)})
            return
        emit({"type": "answer_done", "question_id": question_id,
              "target_id": target_id, **result})

    def _score_real(self, input_path):
        """The actual nesso1 call, confirmed against real hardware in
        docs/spike-nesso1-affinity.md sections 2.2 and 3.2. Uses the
        lower-level prepare()/collate()/model.predict() path rather than
        screen() so the model loaded in load() stays resident across
        questions instead of being reloaded from the checkpoint every call
        (screen() itself calls Nesso1.from_pretrained() internally, which
        this module deliberately avoids repeating).

        Returns the two fields spec section 7's content-honesty rule cares
        about: affinity_probability_binary (a literal [0, 1] probability of
        being a binder -- safe to show a visitor verbatim, no unit claim
        needed) and affinity_pred_value (continuous; same log10(IC50 uM)
        scale as tt-bio's Boltz-2 affinity head per the spike's evidence,
        but NOT confirmed by an explicit tt-bio doc string -- so this is
        secondary/supporting detail in the UI, never the headline number,
        and its copy must hedge accordingly).
        """
        out_dir = _affinity_out_dir(input_path)  # a scratch dir this module
        # owns -- see the real implementation for exactly where (mirrors
        # wherever Folder already keeps its own scratch/output paths).
        dataset, _manifest, failed = prepare(input_path, out_dir)
        if failed:
            raise ValueError(f"nesso1 could not parse {input_path!r}")
        feats = collate(dataset[0])
        torch.manual_seed(DEFAULT_SEED)  # pins RDKit's conformer draw --
        # screen()'s own docstring: without this, upstream repeats differ by
        # up to 0.058 in reported affinity.
        with torch.no_grad():
            pred = self._model.predict(feats)
        return {
            "score": float(pred["affinity_probability_binary"].reshape(-1)[0]),
            "affinity_pred_value": float(pred["affinity_pred_value"].reshape(-1)[0]),
        }
```

`_affinity_out_dir` is a placeholder name for wherever this task decides to
put nesso1's scratch output (parsed structures, conformers, ESM-2 embedding
cache) — read `runner/folder.py` for the existing convention on scratch-space
placement before inventing a new one; it does not need per-question
uniqueness (`prepare()` is idempotent per target, and a shared scratch dir is
what makes the ESM-2 embedding cache in section 4 of the spike doc actually
help on a repeat question for the same target).

- [ ] **Step 4: Add one real-hardware test, gated**

```python
import importlib.util
import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("tt_bio") is None,
    reason="requires tt-bio installed; run under venv-runner with a device")


def test_scores_a_real_question_on_real_hardware():
    # Take a gozer lease before running this test manually; the integration
    # suite's own fixture (see tests/integration/conftest.py precedent from
    # prior phases) should wrap it the same way test_real_fold.py's fixture
    # does.
    scorer = AffinityScorer(device_id=0)
    scorer.load()
    events = []
    scorer.score("q1", "dhfr", "examples/affinity_dhfr.yaml", events.append)
    assert events[-1]["type"] == "answer_done"
    assert isinstance(events[-1]["score"], (int, float))
```

Place this in `tests/integration/test_affinity_real.py` instead if this project's convention is to keep hardware tests out of `tests/unit/` entirely (check `tests/unit/` for any existing skip-gated hardware test before deciding — follow whatever the existing split already does).

- [ ] **Step 5: Run to verify pass (mocked tests) and real (hardware, leased)**

```bash
.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_affinity.py -v
gozer run --chips 1 --who "claude:affinity-qa" --reason "verify AffinityScorer against real hardware" -- \
  .venvs/venv-runner/bin/python3 -m pytest tests/integration/test_affinity_real.py -v
```
Expected: PASS on both.

- [ ] **Step 6: Commit**

```bash
git add runner/affinity.py tests/unit/test_affinity.py tests/integration/test_affinity_real.py
git commit -m "feat: add AffinityScorer, resident nesso1 on a dedicated chip"
```

---

### Task 5: `runner/workers.py` — reserve one chip for Q&A

**Files:**
- Modify: `runner/workers.py`
- Test: `tests/unit/test_worker_specs.py` (extend existing)

**Interfaces:**
- Consumes: `worker_specs(device_ids=None, max_workers=MAX_WORKERS) -> list[WorkerSpec]` (existing, unchanged signature).
- Produces: `split_for_qa(specs: list[WorkerSpec]) -> tuple[list[WorkerSpec], WorkerSpec | None]` — `(fold_specs, qa_spec)`. `qa_spec` is `None` when `len(specs) < 2` (the single-chip-box case — spec §5's documented degrade, not an error).

**Checked against Task 1's spike (`docs/spike-nesso1-affinity.md`) — no
correction needed here.** This task's code is a pure `WorkerSpec`-splitting
function with no dependency on nesso1's call shape, return type, or timing;
none of the spike's findings touch it. The one spike finding that *does* bear
on this task is non-code: `AffinityScorer.load()` (Task 4) needs
`tt_bio.weights.fetch("nesso1")`/`.fetch("nesso1-ccd")` to have already run
for whichever chip `split_for_qa` reserves — a provisioning concern for
Task 6/postinst, not a reason to change `split_for_qa` itself.

- [ ] **Step 1: Write the failing test**

```python
from runner.workers import WorkerSpec, split_for_qa


def _spec(card):
    return WorkerSpec(card=card, label=f"chip{card}", visible_devices=str(card),
                       logical_device_id=0, mesh_graph_descriptor=None)


def test_four_chips_reserve_exactly_one_for_qa():
    specs = [_spec(0), _spec(1), _spec(2), _spec(3)]
    fold_specs, qa_spec = split_for_qa(specs)
    assert len(fold_specs) == 3
    assert qa_spec is not None
    assert qa_spec not in fold_specs
    assert {s.card for s in fold_specs} | {qa_spec.card} == {0, 1, 2, 3}


def test_one_chip_reserves_none_for_qa():
    fold_specs, qa_spec = split_for_qa([_spec(0)])
    assert len(fold_specs) == 1
    assert qa_spec is None


def test_reservation_is_deterministic():
    # Same input, same split, every call -- a daemon restart must not
    # silently move which physical chip answers questions.
    specs = [_spec(0), _spec(1)]
    first = split_for_qa(specs)
    second = split_for_qa(specs)
    assert first[1].card == second[1].card
```

- [ ] **Step 2: Run to verify failure**

```bash
.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_worker_specs.py -v -k qa
```
Expected: FAIL — `ImportError: cannot import name 'split_for_qa'`.

- [ ] **Step 3: Implement**

```python
def split_for_qa(specs):
    """Split worker_specs()'s output into (fold_specs, qa_spec).

    Reserves the HIGHEST-numbered card deterministically -- not because
    that chip is special, but because a fixed, order-independent rule
    means a daemon restart never silently reassigns which physical chip
    answers questions, which would be confusing to debug at a venue ("why
    is chip 3 slow now"). See spec section 5: this is a permanent
    reservation, not a fallback -- a 1-chip box gets qa_spec=None and the
    UI hides the question feature entirely rather than time-sharing.
    """
    if len(specs) < 2:
        return list(specs), None
    ordered = sorted(specs, key=lambda s: s.card)
    return ordered[:-1], ordered[-1]
```

- [ ] **Step 4: Run to verify pass**

```bash
.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_worker_specs.py -v
```
Expected: PASS, full file.

- [ ] **Step 5: Commit**

```bash
git add runner/workers.py tests/unit/test_worker_specs.py
git commit -m "feat: reserve one worker chip for Q&A when more than one is available"
```

---

### Task 6: `runner/daemon.py` — wire in questions

**Files:**
- Modify: `runner/daemon.py`
- Test: `tests/unit/test_daemon.py` (extend existing)

**Interfaces:**
- Consumes: `runner.workers.split_for_qa`, `runner.affinity.AffinityScorer`, `ui.playlist` — wait, the daemon runs in `venv-runner`; confirm `playlist/questions.yaml` loading happens through a runner-local, dependency-light loader rather than importing `ui.playlist` (which may pull in GTK-adjacent dependencies not installed in venv-runner) — mirror however `runner/daemon.py` already resolves `manifest.yaml` targets today (see `_playlist_target` in `_accept_pick`) rather than reaching into `ui/`.
- Produces: `Daemon._accept_question(message)`; `hello`'s payload gains `"qa_capable": bool`; a background thread (or reuse of the existing worker-pool reader-thread pattern) that drains a small question queue onto the reserved `AffinityScorer`.

- [ ] **Step 1: Write the failing tests**

```python
def test_hello_reports_qa_capable_when_two_or_more_chips(daemon_with_n_chips):
    d = daemon_with_n_chips(2)
    assert d._hello_payload()["qa_capable"] is True


def test_hello_reports_not_qa_capable_on_one_chip(daemon_with_n_chips):
    d = daemon_with_n_chips(1)
    assert d._hello_payload()["qa_capable"] is False


def test_accept_question_rejects_unknown_target(daemon_with_n_chips, caplog):
    d = daemon_with_n_chips(2)
    d._accept_question({"type": "question", "question_id": "q1",
                         "target_id": "not_a_real_target"})
    assert "no such target" in caplog.text


def test_accept_question_queues_a_real_target(daemon_with_n_chips):
    d = daemon_with_n_chips(2)
    d._accept_question({"type": "question", "question_id": "q1",
                         "target_id": "dhfr"})
    assert len(d._qa_queue) == 1
```

(Adjust fixture names to whatever `tests/unit/test_daemon.py` already provides for constructing a `Daemon` with a fake pool — read the existing fixtures before inventing new ones; this project's convention names them explicitly, e.g. `daemon_with_n_chips` may already exist under a different name for the multi-chip tests.)

- [ ] **Step 2: Run to verify failure**

```bash
.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_daemon.py -v -k question
```
Expected: FAIL — `AttributeError: 'Daemon' object has no attribute '_accept_question'`.

- [ ] **Step 3: Implement**

In `Daemon.__init__`, alongside the existing `_egg_lock`/`_egg_request` setup:

```python
        self._qa_queue = []            # list of (question_id, target_id)
        self._qa_spec = None           # set in _build_pool once workers exist
        self._qa_scorer = None
```

In whatever method builds the worker pool from `worker_specs()` (search for where that's currently called — likely `_build_pool` per the file structure comment elsewhere in this codebase), split off the Q&A spec:

```python
        fold_specs, self._qa_spec = split_for_qa(specs)
        # fold_specs replaces `specs` in whatever follows this line today.
```

In `on_client_message`'s dispatch:

```python
            elif kind == "question":
                self._accept_question(message)
```

```python
    def _accept_question(self, message):
        """A visitor (or the attract loop) asked a question: validate its
        target against the fold manifest, then queue it for the reserved
        Q&A chip. Mirrors _accept_pick's validation shape but does NOT
        touch self.queue (the fold JobQueue) at all -- see spec section 3:
        a question's score never competes with a fold for a chip."""
        question_id = message.get("question_id")
        target_id = message.get("target_id")
        target = self._playlist_target(target_id)  # reuse existing helper
        if target is None:
            log.info("ignoring question %r: no such target %r in the "
                      "playlist", question_id, target_id)
            return
        if self._qa_spec is None:
            log.info("ignoring question %r: this booth has no chip "
                      "reserved for Q&A", question_id)
            return
        self._qa_queue.append((question_id, target_id, str(target)))
        self._wake.set()
        log.info("question %s (target %s) queued", question_id, target_id)
```

Add `"qa_capable": self._qa_spec is not None` to whatever builds the `hello` dict (the method returning `{"type": "hello", ...}` seen earlier).

Wire the drain: in the daemon's own dispatch loop (`run()`), after handling fold dispatch, drain `self._qa_queue` onto `self._qa_scorer.score(...)` on a dedicated worker thread — follow whichever threading pattern the existing worker-pool reader threads already use (one thread per worker, reading its stdout) rather than inventing a second concurrency model; if the Q&A worker is a full subprocess like a fold worker (recommended, for the same fault-isolation reason folds run in subprocesses rather than in-process), give it its own `Popen` and control-line protocol exactly like `runner/pool.py`'s existing workers, just running a different entry point (`runner/affinity_worker.py`, analogous to whatever `runner/worker.py` does for folds) instead of `AffinityScorer` in-process in the daemon.

**This step is the one most likely to need adjusting once you're looking at the real `_build_pool`/`run()` code** — read both in full before writing this task's actual diff, and prefer matching the existing subprocess-per-worker pattern over an in-process shortcut, for the same "the daemon must never open a device itself" rule `docs/followups.md`'s Phase 5 Task 18 entry documents (a process that has opened a device cannot spawn a child that opens one — so if the daemon's own process ever calls `AffinityScorer.load()` in-process, it must never also spawn fold-worker subprocesses afterward; the subprocess-per-worker pattern avoids this trap entirely, which is why it's recommended over the in-process shortcut above).

- [ ] **Step 4: Run to verify pass**

```bash
.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_daemon.py -v
```
Expected: PASS, full file, no regression on existing pick/egg tests.

- [ ] **Step 5: Commit**

```bash
git add runner/daemon.py tests/unit/test_daemon.py
git commit -m "feat: daemon accepts and dispatches affinity questions"
```

---

### Task 7: `ui/pocket.py` — pocket-residue geometry

**Files:**
- Create: `ui/pocket.py`
- Test: `tests/unit/test_pocket.py`

**Interfaces:**
- Consumes: a `gemmi.Structure` (or whatever type `ui/geometry.py`/`ui/ligand.py` already produce from a parsed `.cif` — confirm the exact type by reading `ui/ligand.py` first, since this module should take the SAME already-parsed object those modules use, not reparse the file itself).
- Produces: `pocket_residues(structure, cutoff_angstrom=5.0) -> set[tuple[str, int]]` — a set of `(chain_id, residue_seqid)` pairs for protein residues with at least one atom within `cutoff_angstrom` of any ligand atom.

- [ ] **Step 1: Write the failing test against a hand-built fixture**

Follow this project's own hard-won fixture discipline (CLAUDE.md, 2026-08-13: "no two candidate anchors in a residue share a position or a B-factor" — the DNA duplex fixture bug was caught only because a real fixture couldn't hide a wrong-atom bug). Build a fixture where the "pocket" and "non-pocket" residues are unambiguously distinguishable by position, not by construction symmetry:

```python
import gemmi
from ui.pocket import pocket_residues


def _fixture():
    doc = gemmi.cif.Document()
    block = doc.add_new_block("fixture")
    # One protein chain, three residues at x=0, x=10, x=100 (CA only, enough
    # for gemmi to build a Structure); one ligand atom at x=1 -- close to
    # residue 1 (x=0, distance 1A), far from residue 2 (x=10, distance 9A)
    # and residue 3 (x=100, distance 99A). Cutoff 5A must catch exactly
    # residue 1.
    # ... construct via gemmi's cif writer or gemmi.Structure directly,
    # matching whatever helper tests/fixtures/ already uses to build a
    # minimal structure (check tests/unit/test_geometry.py for the existing
    # pattern before inventing a new one here).
    ...


def test_only_the_close_residue_is_in_the_pocket():
    structure = _fixture()
    pocket = pocket_residues(structure, cutoff_angstrom=5.0)
    assert pocket == {("A", 1)}


def test_widening_the_cutoff_catches_more():
    structure = _fixture()
    pocket = pocket_residues(structure, cutoff_angstrom=15.0)
    assert pocket == {("A", 1), ("A", 2)}


def test_a_structure_with_no_ligand_has_an_empty_pocket():
    structure = _fixture_no_ligand()
    assert pocket_residues(structure) == set()
```

- [ ] **Step 2: Run to verify failure**

```bash
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_pocket.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'ui.pocket'`.

- [ ] **Step 3: Implement**

```python
"""Which protein residues sit near a ligand -- pure geometry, no model
output involved. See docs/superpowers/specs/2026-09-15-affinity-qa-design.md
section 2: this is the "pocket" half of an affinity question's visual
answer, computed entirely from a .cif the booth already has on disk,
independent of and never blocking on the nesso1 score itself.

The cutoff is a stated, checkable number, not a claim about a "true"
binding site -- see spec section 7's content-honesty rule. The UI's copy
must describe this as "residues nearest the ligand," never as "the
binding site," since a distance cutoff cannot establish the latter.
"""


def pocket_residues(structure, cutoff_angstrom=5.0):
    ligand_atoms = []
    protein_atoms = []  # (chain_id, seqid, position)
    for model in structure:
        for chain in model:
            for residue in chain:
                is_ligand = residue.het_flag == 'H'  # gemmi convention for
                                                       # a HETATM residue
                for atom in residue:
                    if is_ligand:
                        ligand_atoms.append(atom.pos)
                    else:
                        protein_atoms.append((chain.name, residue.seqid.num,
                                               atom.pos))
    pocket = set()
    for chain_id, seqid, pos in protein_atoms:
        for lig_pos in ligand_atoms:
            if pos.dist(lig_pos) <= cutoff_angstrom:
                pocket.add((chain_id, seqid))
                break
    return pocket
```

(Confirm `residue.het_flag`/`atom.pos`/`.dist()` against the actual gemmi API version pinned in `venv-ui` — `python3 -c "import gemmi; help(gemmi.Residue)"` — before treating this as final; adjust names if this gemmi version differs.)

- [ ] **Step 4: Run to verify pass**

```bash
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_pocket.py -v
```
Expected: PASS, full file.

- [ ] **Step 5: Commit**

```bash
git add ui/pocket.py tests/unit/test_pocket.py
git commit -m "feat: compute binding-pocket residues from a parsed structure"
```

---

### Task 8: `ui/client.py` — decode `answer_*` events

**Files:**
- Modify: `ui/client.py`
- Test: `tests/unit/test_client.py` (extend existing)

**Interfaces:**
- Consumes: Task 2's new `EVENT_TYPES` members.
- Produces: whatever callback-registration mechanism `ui/client.py` already uses for `job_start`/`job_done`/etc. (confirm the exact pattern — a dict of `event_type -> list[callback]`, or a single `on_event` dispatched by the app — before adding new branches) gains `answer_start`/`answer_done`/`answer_error` handling, forwarding the decoded event to the app the same way every other event type already does.

- [ ] **Step 1: Write the failing test**

```python
def test_answer_done_is_forwarded_to_the_registered_handler(client_with_fake_socket):
    # Reuse whatever fixture the existing job_done test uses to inject a
    # line and assert a callback fired -- match that test's shape exactly,
    # substituting answer_done for job_done.
    received = []
    client = client_with_fake_socket(on_event=received.append)
    client._feed_line('{"type": "answer_done", "question_id": "q1", '
                       '"target_id": "dhfr", "score": 0.5}')
    assert received[-1]["type"] == "answer_done"
    assert received[-1]["score"] == 0.5
```

- [ ] **Step 2: Run to verify failure**

Run whatever the existing test file's invocation is; expected failure mode depends on how `ui/client.py` currently handles an unrecognized-but-valid event type — it may already forward everything generically, in which case this step reveals there's nothing to implement in Step 3 beyond confirming that, and the test should instead assert on a currently-MISSING piece (e.g., a `question_id`-keyed lookup helper the app needs — see Task 10). **Read `ui/client.py` in full before writing this task's test**, since the two plausible shapes ("client forwards everything, `ui/app.py` filters" vs. "client dispatches per-type") lead to different, non-interchangeable Step 3 implementations.

- [ ] **Step 3: Implement (shape depends on Step 2's finding)**

If `ui/client.py` already forwards every decoded event generically (the more likely shape, given `protocol.events.EVENT_TYPES` is the single source of truth for what's valid and the egg events required no client.py change when they were added — check git history for the egg-event-adding commit to confirm this precedent before assuming it), this task reduces to: no code change in `ui/client.py` at all, and Step 1's test should be rewritten to prove that (a regression guard, not a feature). Do not invent a per-type branch that isn't needed — see this project's own "don't add abstractions beyond what the task requires" convention.

- [ ] **Step 4: Run to verify pass**

```bash
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_client.py -v
```

- [ ] **Step 5: Commit**

```bash
git add ui/client.py tests/unit/test_client.py
git commit -m "test: confirm answer_* events forward through the existing client dispatch"
```

---

### Task 9: `ui/questions.py` — `QuestionQueuePanel`

**Files:**
- Create: `ui/questions.py`
- Test: `tests/unit/test_questions_panel.py`

**Interfaces:**
- Consumes: `ui.playlist.load_questions()`.
- Produces: `class QuestionQueuePanel(Gtk.Box)` with `.on_answer_start(question_id, target_id)`, `.on_answer_done(question_id, target_id, score)`, `.on_answer_error(question_id, target_id)`, `.set_qa_capable(bool)` (hides the whole panel when `False` — see Global Constraints). Mirrors `ui/panels.py`'s `PipelinePanel`'s construction pattern (read it first) so the rail's three panels share one visual language.

- [ ] **Step 1: Write the failing test**

```python
import gi
gi.require_version("Gtk", "4.0")
from ui.questions import QuestionQueuePanel


def test_hides_entirely_when_not_qa_capable():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(False)
    assert panel.get_visible() is False


def test_shows_when_qa_capable():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    assert panel.get_visible() is True


def test_in_flight_question_shows_its_text():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    panel.on_answer_start("q1", "dhfr")
    # Assert on whatever label/child the panel exposes for testing -- follow
    # PipelinePanel's own existing test pattern for how it asserts on a
    # GTK label's text (read tests/unit/test_panels.py first).


def test_answered_question_shows_the_score_not_a_fabricated_verdict():
    panel = QuestionQueuePanel()
    panel.set_qa_capable(True)
    panel.on_answer_done("q1", "dhfr", score=0.5)
    # Assert the rendered text contains the raw score and does NOT contain
    # any of the forbidden invented-verdict words -- this is the content-
    # honesty rule from spec section 7, enforced as a test, not just prose.
    for word in ("tightly", "weakly", "strongly"):
        assert word not in panel.get_display_text().lower()
```

(`get_display_text()` is a helper this task should add for testability — mirror however `PipelinePanel`/`TelemetryPanel` already expose their rendered text for their own tests, if they do; if they don't, check with the same pattern those files' tests use to assert on GTK widget state directly.)

- [ ] **Step 2: Run to verify failure**

```bash
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_questions_panel.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'ui.questions'`.

- [ ] **Step 3: Implement**

Build `QuestionQueuePanel(Gtk.Box)` following `ui/panels.py`'s `PipelinePanel` construction exactly (same CSS class conventions, same `Gtk.Box` orientation/spacing idioms — copy its constructor's shape, don't reinvent). Three rows: pending (a `Gtk.Label` listing queued question text), in-flight (a `Gtk.Spinner` + the question text — no fake progress bar, see spec §6), answered (question text + `f"score: {score}"`, literally that plain, per the content-honesty rule — no adjective the model didn't supply). `set_qa_capable(False)` calls `self.set_visible(False)`.

- [ ] **Step 4: Run to verify pass**

```bash
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_questions_panel.py -v
```

- [ ] **Step 5: Commit**

```bash
git add ui/questions.py tests/unit/test_questions_panel.py
git commit -m "feat: add QuestionQueuePanel rail widget"
```

---

### Task 10: Ribbon highlight — additive pocket coloring

**Files:**
- Modify: wherever pLDDT ribbon coloring actually lives — **confirm the exact file first** (`ui/viewer.py`, `ui/structure_view.py`, or `ui/cartoon.py` — the base design spec's §4 says the renderer is `ui/viewer.py`'s `GtkGLArea`, but CLAUDE.md's cartoon-renderer entries suggest per-residue coloring now lives in `ui/cartoon.py`; grep for wherever `plddt_colors`/`PLDDT_STOPS` is actually applied per-vertex before writing this task).
- Test: extend whichever existing test file covers that module's coloring.

**Interfaces:**
- Consumes: `ui.pocket.pocket_residues()`'s output shape (`set[tuple[chain_id, seqid]]`).
- Produces: a new parameter/method (e.g. `set_highlight_residues(residues: set)`) on whatever class builds the ribbon's per-vertex color array, applied ADDITIVELY (an outline/emphasis, per spec §6 — the existing pLDDT color must remain visible, never be replaced).

- [ ] **Step 1: Find the real target and write the failing test**

Read the module first; then write a test in its existing style, e.g.:

```python
def test_highlighted_residues_keep_their_plddt_color_but_gain_emphasis():
    mesh = build_cartoon(fixture_structure, highlight_residues={("A", 5)})
    # Assert on whatever per-vertex attribute this module already exposes
    # for pLDDT color (do not replace that assertion -- ADD one for the new
    # emphasis attribute existing alongside it) -- exact assertion depends
    # on the real function signature found above.
```

- [ ] **Step 2: Run to verify failure**

Run the relevant existing test file with `-k highlight`; expected failure is a `TypeError` for the new unexpected keyword argument.

- [ ] **Step 3: Implement**

Add the parameter, threading it through to whatever per-vertex color computation already runs, applying an additive change (e.g., a brightness boost or an outline flag consumed by the existing shader — check `ui/shaders.py` for what's already available before adding a new shader uniform) rather than overwriting the pLDDT-derived color.

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: additive pocket-residue highlight on the ribbon"
```

---

### Task 11: `ui/app.py` — wire the panel, the join, and the two trigger paths

**Files:**
- Modify: `ui/app.py`

**Interfaces:**
- Consumes: everything from Tasks 2, 3, 7, 9, 10.
- Produces: a mounted `QuestionQueuePanel` in the rail; `answer_done` events routed to it AND, when the answered `target_id` matches whatever structure is currently on screen, the highlight applied via Task 10's new setter; a "?" gallery affordance (or a new `Gtk.Button` per question, following whatever `ui/gallery.py` already does for a fold-pick tile) that sends `question_message(...)` through the existing socket client on a visitor tap; an attract-loop cadence that periodically sends a `question` message for a round-robined `load_questions()` entry, gated by `qa_capable` from `hello` — model this cadence on `ui/attract.py`'s existing cue timing (read it fully first; do not invent a second choreography engine when one already exists and is pure/testable).

- [ ] **Step 1: Extend `ui/attract.py`'s pure cue module with a testable question cadence**

```python
def test_a_question_cue_fires_after_the_existing_cues_rest_period(idle_clock):
    # Follow the exact test pattern this file's own existing cue tests use
    # (SHOW_GALLERY/OPEN_TENSIX timing tests) -- read them first, match the
    # style, add ASK_QUESTION/no-corresponding-close-cue (a question is
    # fire-and-forget, unlike the open/close pairs) to the same score.
```

Add `ASK_QUESTION = "ask_question"` to the cue vocabulary and extend the pure choreography function with one more beat, following this file's documented rule 2 ("it never leaves the booth changed") — a question cue has no "close" to undo, so this rule doesn't constrain it the way it constrains the diagnostics/Tensix opens, but rule 1 ("never fights a visitor") still fully applies.

- [ ] **Step 2: Run to verify failure, then implement, then verify pass**

Standard cycle against `tests/unit/test_attract.py` (or wherever the existing cue tests live).

- [ ] **Step 3: Wire everything in `ui/app.py`**

- Construct `QuestionQueuePanel()` alongside the existing `PipelinePanel`/`TelemetryPanel` construction; call `.set_qa_capable(hello["qa_capable"])` when `hello` arrives.
- Register `answer_start`/`answer_done`/`answer_error` handlers that forward to the panel, and on `answer_done`, if `target_id` matches the currently-displayed structure's own target (check whatever field the app already tracks for "what's on screen" — likely on `ui/slots.py`'s `SlotState`), call Task 10's highlight setter with `pocket_residues(...)` computed from the already-parsed structure.
- On the `ASK_QUESTION` cue firing (from Step 1's extended choreography, driven the same way `OPEN_TENSIX` etc. already are), round-robin through `load_questions()` and call `self.router.send(question_message(q.id, q.target_id))` (or whatever the existing send path for a pick is named — mirror it exactly).
- Add one tappable affordance per question to the gallery (or a dedicated small strip — whichever is the smaller diff against `ui/gallery.py`'s existing grid-tile construction) that does the same send on a visitor's tap.
- **Do not add a camera fly-to/cut when a pocket highlight arrives** (spec §6). The existing idle rotation already brings every angle around within a few seconds; a forced camera cut has been the source of real bugs in this project's camera-tuning history (`_frame_camera`'s Phase 1-2 easing defects). The highlight simply becomes visible as the structure rotates through.

- [ ] **Step 4: Manual verification against `--mock`**

Extend the mock runner (Task 12) first if it doesn't yet replay `answer_*`, then run:

```bash
.venvs/venv-ui/bin/python3 -m ui.app --mock <recorded-stream-with-a-question>
```

Confirm the rail panel appears, an in-flight spinner shows, and a score appears with the ribbon gaining a highlight when that target is on screen — this is a UI change, and per this project's own standing rule, a UI change is not verified until someone has looked at it running, not just at its test suite passing.

- [ ] **Step 5: Commit**

```bash
git add ui/app.py ui/attract.py ui/gallery.py tests/unit/test_attract.py
git commit -m "feat: wire the question queue panel, pocket highlight, and attract-loop cadence"
```

---

### Task 12: `--mock` runner replay + end-to-end integration test

**Files:**
- Modify: wherever `--mock` replay lives (`runner/mock.py` or similar — confirm by reading how the base design's Phase 1 mock runner is structured).
- Create: `tests/fixtures/streams/` addition — a recorded event stream including a `question`/`answer_done` pair (hand-authored is fine here, unlike a real fold capture, since Task 4's hardware test already proves the real call works).
- Test: `tests/integration/test_mock_questions.py`

**Interfaces:**
- Consumes: Task 2's new event types.
- Produces: the `--mock` runner can replay a stream containing `answer_start`/`answer_done`/`answer_error`, so UI development and CI both work with no hardware.

- [ ] **Step 1: Write the failing test**

```python
def test_mock_runner_replays_a_question_answer_pair(mock_runner_with_stream):
    runner = mock_runner_with_stream("tests/fixtures/streams/with_question.jsonl")
    events = list(runner.replay())
    types = [e["type"] for e in events]
    assert "answer_start" in types
    assert "answer_done" in types
```

- [ ] **Step 2: Run to verify failure**

Expected failure depends on the mock runner's actual replay mechanism — if it already replays any well-formed line from `protocol.events.EVENT_TYPES` generically (likely, given the same reasoning as Task 8), the failure will be a missing fixture file, not a code gap.

- [ ] **Step 3: Add the fixture, and implement only if the generic-replay assumption from Step 2 turns out false**

```jsonl
{"type": "hello", "version": 4, "cards": [0], "models": ["protenix-v2"], "preflight": "ok", "qa_capable": true}
{"type": "job_start", "job_id": "j1", "target_id": "dhfr", "model": "protenix-v2", "card": 0, "n_residues": 187}
{"type": "answer_start", "question_id": "q1", "target_id": "dhfr"}
{"type": "answer_done", "question_id": "q1", "target_id": "dhfr", "score": 0.5}
{"type": "job_done", "job_id": "j1", "cif_path": "dhfr.cif", "wall_s": 14.5, "mean_plddt": 53.5}
```

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/streams/with_question.jsonl tests/integration/test_mock_questions.py
git commit -m "test: mock runner replays a question/answer pair end to end"
```

---

## After all tasks: whole-branch review

Follow this project's own established practice (every phase in CLAUDE.md ends this way): after Task 12, run a whole-branch review with mutation testing on the new code (`runner/affinity.py`, `runner/workers.py`'s `split_for_qa`, `ui/pocket.py`, the `Daemon._accept_question` path, the attract-loop cadence). This project's own history (Phase 3a: 13/15 mutations survived; Phase 3b: 38/47 died) shows a fresh implementer's tests reliably miss real gaps the first time — budget for a fix wave, not just a review.
