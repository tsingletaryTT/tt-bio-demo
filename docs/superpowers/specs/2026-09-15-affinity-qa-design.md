# Affinity questions — design

**Date:** 2026-09-15
**Status:** approved (design); implementation plan not yet written
**Target hardware:** Tenstorrent QB2 (4× p300c Blackhole), same booth as the base design

---

## 1. What this is

tt-bio's value is not only "predict the shape of this molecule" — it can also answer a
question about two molecules together: does this drug bind this protein, and how well.
The booth today only ever demonstrates the first kind of capability (fold → reveal). This
adds the second: a visible **queue of questions**, each answered by a real tt-bio
computation, with a **visual result** — never a number alone.

The concrete question type for this phase is **affinity**: "Does `<ligand>` bind
`<protein>`?", answered by tt-bio's nesso1 scalar affinity model and shown as the finished
protein's ribbon with the binding-pocket residues highlighted, next to the model's own
score.

### Why affinity, and why nesso1

Three targets already on the playlist — DHFR, trypsin, FKBP12 — are protein+ligand
complexes in their *upstream* form. Each input file (`examples/affinity_*.yaml`) already
carries a `properties: affinity:` block naming the ligand (methotrexate, benzamidine, SB3)
that `runner/folder.py`'s `Folder.fold()` has read past and silently ignored since these
files were vetted — confirmed in each file's own header comment. **This phase turns that
ignored block into the question's input.** No new molecules, no new vetting, no new fold
inputs: the affinity worker reads the exact same file the fold worker already folds.

nesso1 is the right model for this, not Boltz-2's affinity head, on pacing grounds alone:
nesso1 is a separate, fast, scalar-only, no-coordinates path (CLAUDE.md's own prior research
put it at ~33s for a 512-residue complex, against ~386s for the equivalent Boltz-2 affinity
path on the same complex). A booth whose slowest existing target is ~96s cannot add a
6-minute step. **nesso1 needs no 3D structure as input** — it scores from sequence + ligand
alone — which is what makes the architecture below possible.

### Non-goals (this phase)

- **Generative design ("can tt-bio design a binder for this pocket?") is explicitly
  deferred.** See §8. No design-model weights are downloaded, no hardware time is spent on
  it, and no UI surfaces it, in this phase.
- Not a new fold model integration. The three complexes already fold today; this phase adds
  a second, independent computation over the same inputs.
- Not a change to the existing fold/showcase state machine's behavior for a visitor who
  never asks a question. `ui/states.py`'s `BoothState`/`StateMachine` are untouched — see §3
  for why that is a design goal, not an accident.

---

## 2. The reuse: nesso1 needs no structure, so it never blocks a fold

The key simplification, worth stating plainly because it decides the whole architecture: **a
question's affinity score does not depend on that target's fold having happened at all.**
nesso1 takes the protein sequence and ligand from the input YAML directly. The **pocket
highlight** — which residues sit near the ligand — is a purely geometric fact computed from
whichever `.cif` the booth already has on disk for that target (gemmi, same library
`ui/geometry.py`/`ui/ligand.py` already use), needing no new runner capability at all.

So a "question" decomposes into two independently-satisfiable things:

1. **The score** (new): nesso1 run on the dedicated Q&A worker, from the input file alone.
2. **The pocket** (already have the data): protein residues within a cutoff of any ligand
   atom, computed **client-side in the UI** from a `.cif` it has already parsed for
   cartoon rendering. No protocol change needed for this half at all.

This means the UI can compute and cache the pocket for a target the very first time it ever
renders that target's cartoon (fold or showcase, unrelated to any question being asked), and
the runner's only new job is the score. The two are stitched together by `target_id` when
both are available — order doesn't matter, and neither blocks the other.

---

## 3. Why this does not touch `ui/states.py`

`BoothState`/`StateMachine` govern the **hero slot**: attract / gallery / folding /
showcase / preparing. That machine is deliberately pure, heavily mutation-tested, and has
already absorbed several rounds of hard-won dwell/timing correctness (CLAUDE.md's dwell and
"empty viewer" sections). Adding a sixth state for "answering a question" would put a new,
untested seam through code this project has repeatedly found subtle bugs in by touching it.

Instead, a question rides the **existing** fold pathway unchanged, the same way the easter
egg (`ui/app.py`'s `egg_viewer`) rides alongside the main viewer without a `BoothState` of
its own:

- **Asking a question = picking its target, tagged.** `playlist/questions.yaml` (§5) maps a
  question to an existing fold `target_id` (dhfr / trypsin / fkbp12). Asking the question
  enqueues exactly the pick that already exists (`SlotRouter`/`Daemon._accept_pick`), plus a
  side-channel `question_id` that says "and also answer this."
- **The fold, stage progress, frame streaming, and showcase dwell are 100% today's code,
  unmodified.** A visitor watching a question gets exactly the same fold-and-reveal theatre
  as today; nothing about `BoothState` needs to know a question is attached.
- **The answer is additive chrome**, like the diagnostics/Tensix rail panels: a new
  `QuestionQueue` rail panel (§6) shows pending/in-flight/answered questions regardless of
  what the hero slot is doing, and the **pocket highlight** is applied to the ribbon
  whenever the currently-displayed structure's `target_id` matches an answered question —
  additive coloring on top of the existing pLDDT ramp, not a new render mode.
- **If the answer arrives after the showcase dwell has already ended**, the rail panel still
  shows it (captioned with its target's name, the same pattern the "previous fold, dimmed and
  captioned" mechanism already uses for honesty about what's stale) — it does not try to
  force the hero slot back open. A question is never lost; it just may be answered
  chronologically after its own fold's spotlight already moved on, and the rail panel is
  where a visitor confirms that happened.

---

## 4. Protocol additions

Two new client→daemon message kinds, alongside the existing `pick`/`egg`
(`Daemon._handle_client_message`'s `kind` dispatch):

| Kind | Payload | Behavior |
|---|---|---|
| `question` | `{question_id, target_id}` | Enqueues the underlying fold pick (if the target isn't already in flight/recent) exactly as `pick` does, AND enqueues an affinity job on the dedicated Q&A worker. Two independent completions, joined client-side by `target_id`. |

New daemon→UI events, alongside `job_start`/`stage`/`frame`/`job_done`/`job_error`:

| Type | Payload | Meaning |
|---|---|---|
| `answer_start` | `{question_id, target_id}` | The Q&A worker began scoring. |
| `answer_done` | `{question_id, target_id, score, pocket_hint}` | nesso1 finished. `score` is whatever scalar tt-bio's nesso1 API actually returns — **its meaning and units are confirmed against the pinned tt-bio version during implementation, never guessed**; the UI's copy states only what the number honestly is (see §7's content-honesty rule). `pocket_hint` is optional metadata nesso1 might expose (e.g. per-residue attribution) — if it doesn't exist, the pocket is computed client-side per §2 regardless. |
| `answer_error` | `{question_id, target_id, message}` | Scoring failed. UI never displays `message` verbatim, same rule as `job_error`. |

`hello`'s payload gains a `qa_capable: bool` (or similar), so a booth whose Q&A worker isn't
configured (e.g. a single-chip dev box that opted out — see §5) can omit the question UI
entirely rather than show a queue that can never answer.

---

## 5. The dedicated Q&A worker, and chip allocation

One chip is dedicated to nesso1, resident, for the life of the daemon — the same model-
residency pattern `runner/folder.py` already uses for protenix-v2, in a new parallel module
(`runner/affinity.py`, an `AffinityScorer` alongside `Folder`) and a new worker kind in
`runner/workers.py`.

**Decided: dedicate one chip, don't time-share.** Swapping nesso1 in and out of a fold
worker on demand was considered and rejected: this box's model-swap cost between two
resident models has never been measured, and if it costs more than a few seconds it stalls
a fold pick mid-attract-loop — a visible pacing regression, the same class of defect this
project's dwell-tuning history treats as a real bug. Dedicating a chip has a **known,
bounded** cost instead: 25% less fold throughput on a 4-chip box, always, whether or not a
question is ever asked. That trade is made explicit here rather than discovered later.

**Single-chip fallback.** On a box with only one chip (dev boxes, per the existing
`quad`/`solo` auto-detection precedent), there is no chip to dedicate. The Q&A feature
degrades to `qa_capable: false` in `hello` rather than time-sharing the one chip — the UI
hides the question queue entirely on such a box. This is a scope choice, not a limitation
worth engineering around: a booth this small is a dev box, not a venue configuration.

**Open item for the implementation plan, not this spec:** the exact tt-bio API surface for
nesso1 (CLI subprocess `tt-bio affinity --model nesso1`, vs. a Python entry point analogous
to `tt_bio.protenix.Protenix`) is unconfirmed. Per this project's own established practice
(the Phase 3a hardware spike, done *because* the design assumed things about `dump_fn`
nobody had watched run), **the first implementation task must be a spike that folds nothing
and only confirms**: the real API call, its actual wall-clock time on this hardware, and its
actual output shape/units — before any protocol field names are treated as final.

---

## 6. The UI: `QuestionQueue` rail panel

A third rail panel, `ui/questions.py`, alongside the existing pipeline and telemetry panels
— always visible when `qa_capable`, independent of hero-slot state:

- **Pending**: questions not yet dispatched (attract-loop cadence, §7, or a visitor's pick
  not yet reached the front).
- **In flight**: the one question currently being scored, with a simple "checking whether
  it binds…" indicator (nesso1 gives no stage/frame events — it's a single fast scalar
  call, not a multi-stage pipeline like a fold. No fake progress bar; an indeterminate
  spinner is the honest representation of a call this project cannot subdivide).
- **Answered**: the most recent result — the question text, the model's score, and whether
  its target's structure (and therefore its pocket highlight) is the one currently on
  screen, so a visitor can tell whether looking at the hero slot right now shows this
  answer or a different, since-superseded one.

**Visual answer on the ribbon.** `StructureViewer` (or `ui/cartoon.py`, wherever residue
coloring already lives for pLDDT) gains a highlight-residue-set — additive to, not a
replacement for, the existing confidence ramp: pocket residues get an outline/emphasis
rather than losing their pLDDT color, so a visitor can still read both facts (how confident
is the fold, and where does the ligand sit) at once. Camera behavior: **no automatic
fly-to** — the existing idle rotation already brings every angle around within a few
seconds, and forcing a camera cut has previously been the source of jarring, disorienting
changes elsewhere in this project's camera-tuning history (`_frame_camera`'s easing bugs in
Phase 1-2). The pocket simply becomes visible as the structure rotates through, same as
everything else on it.

---

## 7. Content and honesty

`playlist/questions.yaml` — one entry per question, following `manifest.yaml`'s own
established conventions (optional fields degrade gracefully, every claim is measured before
it is printed, never fabricated or derived from a mismatched run):

```yaml
- id: dhfr_mtx
  target_id: dhfr             # existing playlist entry; reused verbatim
  question: "Does methotrexate block dihydrofolate reductase?"
  ligand_name: "Methotrexate"  # for display; CCD code MTX lives in the fold input already
  expected_s: null             # unmeasured until the implementation spike runs
```

Three entries ship initially: `dhfr_mtx`, `trypsin_bam` (benzamidine), `fkbp12_sb3` (SB3) —
exactly the three complexes already on the playlist, no new vetting needed.

**Content-honesty rules, consistent with this project's established standard (the DNA/tRNA
blurbs, the FKBP12 pLDDT-spread note, the "expected_s means the warm state a visitor
actually meets" rule):**

- The score's copy states only what tt-bio's own documentation says the number *is* (a
  predicted affinity value, in whatever units/scale nesso1 defines) — never reinterpreted
  into an invented "binds tightly / weakly / not at all" verdict tier unless tt-bio's own
  docs define such thresholds. If they don't, the booth shows the number and a one-line
  factual gloss, not a fabricated confidence category.
- `expected_s` for each question stays `null` — and `ui/questions.py` renders that as "not
  yet timed," mirroring `ui/gallery.py`'s existing handling of an unmeasured
  `expected_s` — until the implementation's hardware spike (§5) measures it for real.
- The pocket-residue cutoff distance is a stated, checkable number (not a fudge), and the
  UI's copy about it says only "the residues nearest the ligand," a geometric fact anyone
  could verify from the same `.cif`, not a claim about a "true" binding site the geometry
  cutoff cannot actually establish.

---

## 8. Deferred: generative design questions

Explicitly **not** built this phase, following the same "excluded, not forgotten" pattern
this project already uses for HSA-before-0.6.3 and FKBP12-during-0.6.3: a real capability
(PXDesign/BoltzGen — "can tt-bio design a molecule that binds this pocket?") that this spec
declines to build now because none of its prerequisites exist yet:

- No design-model checkpoint has been downloaded or verified against the offline-at-the-
  venue install principle.
- No hardware timing exists for a design job on this box — pacing risk unknown, unlike
  affinity's ~33s reference point.
- No correctness/plausibility story exists yet for "the booth generated a molecule" the way
  the fold entries each carry a measured pLDDT/geometry sanity check before shipping.
- The generic "question" plumbing this spec builds (§4's protocol, §6's rail panel) is
  intentionally typed loosely enough (`answer_done`'s payload is per-question-kind, not
  affinity-specific in its envelope) that a design question is a plausible SECOND question
  kind riding the same queue later — but that is a future spec's decision to make once
  affinity has shipped and been watched running for real, the same way this project always
  waits to add a model until it has been measured rather than estimated.

---

## 9. Testing

Same shape as every prior phase's test strategy (`--mock` runner extension, unit tests with
no hardware, hardware-gated integration tests):

- `AffinityScorer` unit-testable the same way `Folder` is: a fake nesso1 call injected, so
  the worker/event-emission logic has coverage with no device.
- Pocket-residue computation is pure geometry (`.cif` in, residue-id set out) — testable
  against a hand-built fixture the same way `ui/geometry.py`'s existing tests use one, with
  the same "no two atoms share a distinguishing property" discipline that caught the DNA
  duplex fixture bug (CLAUDE.md, 2026-08-13).
- `question` message handling in `Daemon._handle_client_message` and the two independent
  completions joining by `target_id` get an integration test using the mock runner extended
  with a fake `answer_done`.
- A real hardware test (gated, like `test_real_fold.py`) that asks one real question against
  one real chip pairing and confirms a real score comes back — the same "one real fold is
  the only proof the cache change works" standard this project holds itself to (see the
  2026-08-31 weights section of CLAUDE.md) applies here: at least one integration test must
  exercise the real nesso1 call, not only a mock.
