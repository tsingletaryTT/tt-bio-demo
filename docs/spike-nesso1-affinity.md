# Spike: nesso1 affinity — the real API, timing, and output shape

Task 1 of the affinity-questions plan
(`docs/superpowers/plans/2026-09-15-affinity-questions.md`). Everything below was
run for real on this box's hardware under a gozer lease (chips 0,1 of board
`0000046131924062`, lease `22ff5a`, released cleanly afterward). tt-bio 0.8.0,
`.venvs/venv-runner`.

## 1. What the research pass got right, and what it missed

Right: there is a CLI, `tt-bio affinity --model nesso1 <data>` (`tt_bio.main`'s
`affinity_cmd`, registered as `@cli.command("affinity")`). Confirmed via
`python3 -m tt_bio.main --help` and `python3 -m tt_bio.main affinity --help`
(full output below).

Missed / needs correction:

- **`dir(tt_bio)` exposes nothing named `affin`/`nesso`.** The capability is not
  a top-level symbol; it lives in the `tt_bio.nesso1` and `tt_bio.nesso1_input`
  submodules, imported directly.
- **The CLI is a thin wrapper around one in-process function:
  `tt_bio.nesso1.screen(data, out_dir, ...)`.** `affinity_cmd`'s body is
  `from tt_bio.nesso1 import DEFAULT_SEED, REPORTED_SCALARS, screen`, then a
  single call to `screen(data, out, use_tenstorrent=use_tt, ...)`. This is the
  in-process entry point Task 1's brief asked to find — it exists, and it is
  exactly what the CLI itself calls, so `runner/affinity.py` can call it (or the
  lower-level pieces it's built from — see §4) without shelling out.
- **The docstring undersells the input requirement, and the code is more
  permissive than the docstring.** `affinity_cmd`'s docstring says DATA "needs
  ... a `properties.affinity.binder` naming the ligand to score." The vendored
  `examples/affinity_*.yaml` files in this repo have **no `properties:` block
  at all** (only `sequences:` — see each file's own header comment, which
  explains the block was dropped because the fold runner ignores it). Reading
  `tt_bio/_vendor/nesso/data/yaml_input.py`'s `_validate_affinity_binders`
  confirms the block is optional: it validates the binder **only if the key is
  present** (`if not isinstance(affinity, dict) or "binder" not in affinity:
  continue`). With exactly one ligand chain per input (true of all three
  vendored files), nesso1 resolves the binder without being told. **Confirmed
  empirically**: all three vendored files, with no `properties:` block, scored
  successfully (§3).
- **The return shape is not a bare float.** It is a dict of named scalar
  fields per input (§3 has a real captured example). The two fields worth
  showing a visitor are `affinity_pred_value` (continuous, same
  log10(IC50 µM) scale as tt-bio's Boltz-2 affinity head — see §3.3 for the
  evidence) and `affinity_probability_binary` (a literal probability in
  [0, 1] that the ligand is a binder — no unit conversion, no invented
  verdict tier needed to show this one honestly).

## 2. The exact call

### 2.1 High-level (what the CLI itself uses)

```python
from tt_bio.nesso1 import screen

rows = screen(
    "examples/affinity_dhfr.yaml",   # a single YAML file OR a directory of them
    "/some/out_dir",                  # writes <id>_affinity.json + processed/ here
    use_tenstorrent=True,             # False = CPU torch reference, ~5x slower
    trunk_fp32=False,                 # CLI's own bf16 default; see --trunk help text
    recycling_steps=5,
    tokens_budget=256,
    num_workers=0,                    # 0 = parse inline; see the CLI help for why
    ccd_pkl=None,                      # auto-discovered next to the checkpoint
    cache=None,                       # HF cache dir; None = HF default (~/.cache/huggingface)
    seed=20260820,                    # tt_bio.nesso1.DEFAULT_SEED; pins RDKit's conformer draw
)
# rows: list[dict], one entry per YAML found under `data`.
```

**Weights are not bundled** — this box had to fetch them once via tt-bio's own
registry before any of this worked:

```python
from tt_bio import weights
weights.fetch("nesso1")       # v1.0.0/model.safetensors, ~165 MB
weights.fetch("nesso1-ccd")   # ccd.pkl, ~413 MB (shared with the CCD used elsewhere)
```

Both came from `recursionpharma/nesso` on the HF Hub, resolved via
`weights.artifacts_for("nesso1")` (which also lists a `nesso1-ccd` row) — this
project's `weights.py` wiring (from the 0.7.0 upgrade session) already knows
about both rows; nothing new needed there. `weights.status("nesso1")` reported
`state='missing'` before the fetch; a fresh venue install will need
`tt-bio weights --download nesso1` (and `nesso1-ccd`) added to whatever
provisioning step already handles `protenix-v2` — **flagged for Task 5/6 or a
follow-up; not fixed by this spike**, since fixing it is downstream install
plumbing, not the API question this task answers.

Also downloaded transparently on first use: the ESM-2 encoder
(`facebook/esm2_t33_650M_UR50D`, ~2.6 GB) that nesso1's featurizer runs on CPU
per protein sequence — this is a real, non-optional dependency of `screen()`,
also worth a line in whatever provisioning step handles weights.

### 2.2 Lower-level (what `screen()` is built from, if `runner/affinity.py` wants
finer control than "reload everything from a YAML path every call")

```python
import torch
from tt_bio.nesso1 import Nesso1, DEFAULT_SEED
from tt_bio.nesso1_input import prepare, collate, CLI_PREDICT_ARGS

torch.set_grad_enabled(False)

model = Nesso1.from_pretrained(use_tenstorrent=True)   # loads weights, opens the device
model.use_kernels = False                                # screen()'s own override — see
                                                           # its comment: routes triangle ops
                                                           # through tt-bio's fused kernels,
                                                           # not the CPU-only cuEquivariance path
model.predict_args.update(CLI_PREDICT_ARGS)

# Per question:
dataset, manifest, failed = prepare(Path("examples/affinity_dhfr.yaml"), out_dir)
feats = collate(dataset[0])
torch.manual_seed(DEFAULT_SEED)          # pins RDKit's random conformer draw — see screen()'s
                                          # own docstring: without this, upstream repeats
                                          # differ by up to 0.058 in reported affinity
with torch.no_grad():
    pred = model.predict(feats)          # dict of tensors; see §3.2 for the keys
score = float(pred["affinity_pred_value"].reshape(-1)[0])
```

This is the shape `screen()` itself uses internally (load once, loop
`model.predict(feats)` per record) — it is the "residency-preserving" pattern
the brief asked to look for, and it is real, not a guess: `screen()`'s own
docstring says "The model loads once and stays resident, which is the whole
point for a screen." **Important finding for Task 4** (see §5): the *host-side*
half of `prepare()` does **not** get materially cheaper on a repeat call to the
same target, even with `model` held resident and the ESM-2 embedding disk-cached
— see §4.

### 2.3 Device wiring — no extra step needed

`Nesso1.from_pretrained(use_tenstorrent=True)` opens the device on its own path
(through `tt_bio.tenstorrent.get_device()`, which — per this project's own
0.6.3 upgrade notes in CLAUDE.md — already calls `ensure_p300_mesh_descriptor()`
internally). Confirmed by using it directly with no manual device setup and no
`ensure_p300_mesh_descriptor()`/`_require_ttnn()` call of our own: it worked.
`runner/affinity.py`'s `AffinityScorer.load()` can mirror `Folder.load()`'s
existing pattern (rely on `TT_VISIBLE_DEVICES` in the worker's environment,
call the loader, done) rather than reimplementing device selection.

## 3. What we ran, and what came back

### 3.1 Timing table

All calls used `use_tenstorrent=True`, same process per row-group as noted.
"cold" = first call to `screen()` in a fresh Python process (pays model
weights load + device open + first-ever kernel compile for that token count).
"warm" = second call to `screen()` in the **same** process immediately after
(re-loads the model object again, but the ttnn kernel cache for that padded
token shape is already compiled).

| complex | tokens | cold wall (whole `screen()` call) | cold `model.predict()` only | warm wall (2nd call, same process) | warm `model.predict()` only |
|---|---|---|---|---|---|
| DHFR + methotrexate (`affinity_dhfr.yaml`) | 220 | 78.2 s | 16.7 s | 11.5 s | 3.77 s |
| FKBP12 + SB3 (`affinity_fkg.yaml`, **fresh process, no prior fold** — see §3.4) | 140 | 21.6 s | 13.0 s | 9.2 s | 1.97 s |
| Trypsin + benzamidine (`affinity_tryp.yaml`) | 232 | 18.3 s | 9.5 s | 12.3 s | 4.44 s |

Notes on reading this table honestly:

- The DHFR row's 78.2 s "cold" figure is inflated relative to FKBP12/trypsin's
  cold figures because it was the **first nesso1 call in this session**, so it
  alone paid the one-time HF weights-index/cache-lookup overhead the other two
  rows (run afterward, same session) did not. This is analogous to this
  project's already-documented "JIT kernel cache" cold layer for protenix-v2 —
  a version/first-run cost, not a steady-state one.
- **A resident model does not make repeat host-side prep free.** See §4 — this
  is the finding worth Task 4/5 knowing about, more than the table above.
- All three numbers are well inside "the same booth that already tolerates a
  4–97 s fold wait" territory. Nothing here is disqualifying for a Q&A worker.

### 3.2 Exact return shape (real captured example, DHFR)

```python
>>> screen("examples/affinity_dhfr.yaml", out_dir, use_tenstorrent=True)
[
  {
    "id": "affinity_dhfr",
    "n_tokens": 220,
    "seconds": 3.7697874940022302,
    "affinity_pred_value": -1.032707691192627,
    "affinity_pred_value1": -1.2323474884033203,
    "affinity_pred_value2": -0.8330679535865784,
    "affinity_logits_binary": 3.747560977935791,
    "affinity_probability_binary": 0.9769678115844727,
    "entropy_pp": 0.17845961451530457,
    "entropy_pl": 0.25020262598991394,
    "entropy_ll": 0.2534964084625244,
    "entropy_crop_pp": 0.2093200981616974,
    "entropy_crop_pl": 0.3178222179412842,
    "entropy_crop_ll": 0.2534964084625244
  }
]
```

`screen()`'s return is `list[dict]` — one dict per input YAML, every value a
Python `int`/`float`/`str` (already scalarized: `screen()` does
`float(pred[k].reshape(-1)[0])` for each of `nesso1.REPORTED_SCALARS`). A
failed record instead has `{"id": ..., "error": "..."}`.

The lower-level `model.predict(feats)` (§2.2) returns a **richer** dict of
**tensors** (batch dim included, not yet scalarized): `pdistogram`,
`token_pad_mask`, `pocket_mask`, `token_mask`, the six `entropy_*` fields, and
— only because the shipped checkpoint has `affinity_prediction=True` — the five
`affinity_*` fields above. Worth flagging for Task 7 (`ui/pocket.py`): nesso1
*itself* already computes a token-level `pocket_mask` (tokens within a cutoff
of the ligand, same idea Task 7 wants) — but it is a **token-index** mask
against nesso1's own cropped/padded token axis, not `(chain_id, residue_seqid)`
pairs against a `.cif`'s residue numbering, so it cannot be reused directly by
`ui/pocket.py` without a token→residue remap this spike did not attempt.
Task 7's plan of computing the pocket independently, client-side, from the
`.cif` (spec §2) remains the right call — simpler, no remap risk, no new
coupling to nesso1's internal token layout — this is a "worth knowing," not a
"must change."

### 3.3 What the numbers mean

- **`affinity_probability_binary`** — a probability in `[0, 1]` that the
  ligand is a binder (it is literally `sigmoid` of a learned logit, averaged
  over a 2-member ensemble — see `Nesso1._affinity` in the installed source).
  This is the safest field for the booth to show verbatim: it needs no unit
  conversion and its name already states what it is, satisfying spec §7's "no
  invented verdict tier" rule for free.
- **`affinity_pred_value`** — continuous, and **not documented in the
  installed package** as a specific unit (no `docs/nesso1.md` or
  `docs/model-capabilities.md` ships inside the pip wheel — the plan's
  reference to those docs assumes an upstream GitHub docs tree, not the
  installed artifact; not fetched for this spike since the task is about the
  *API*, not the docs site). Indirect but strong evidence it is on the same
  scale as tt-bio's own Boltz-2 affinity head:  `Nesso1`'s `AffinityModule` is
  built from the **same, directly-imported** classes
  (`AffinityHeadsTransformer` from `tt_bio.boltz2`), and `tt_bio/boltz2.py`'s
  own code comment (line ~5138) states outright: *"the affinity scalar's
  mean-over-pooled-pair reduction turns bf16 storage rounding in z into a
  ~0.19 log10(IC50) offset"* — i.e., Boltz-2's own `affinity_pred_value` is
  `log10(IC50 in µM)`. Sanity-checking our three real numbers against known
  chemistry supports the same reading for nesso1: DHFR+methotrexate → -1.03
  (≈0.09 µM = 93 nM IC50 — methotrexate is a famously nanomolar DHFR
  inhibitor); trypsin+benzamidine → 1.42 (≈26 µM — benzamidine's real affinity
  for trypsin is documented in the tens-of-µM range); FKBP12+SB3 → 0.46
  (≈2.9 µM). All three land in a plausible, differentiated range under this
  reading and none contradicts it. **Not a certainty** — this spike did not
  find an explicit "nesso1's `affinity_pred_value` is log10(IC50 µM)" sentence
  in the installed source — so Task 9/11's copy should hedge exactly this much
  and no more (e.g., "a predicted binding-affinity score, tt-bio's own scale"
  rather than asserting µM units as fact), and `affinity_probability_binary`
  should be the primary number shown, with `affinity_pred_value` as
  secondary/supporting detail, not the headline.

### 3.4 Step 4: does it need a prior fold? **No.**

Ran in a **fresh Python process**, importing nothing from `tt_bio.protenix` or
`tt_bio.opendde` (this repo's fold path) at all, against
`examples/affinity_fkg.yaml` (FKBP12+SB3) — the file the brief specified:

```python
import sys
assert 'tt_bio.protenix' not in sys.modules
assert 'tt_bio.opendde' not in sys.modules

from tt_bio.nesso1 import screen
rows = screen("examples/affinity_fkg.yaml", out_dir, use_tenstorrent=True)
# ... scored successfully, see §3.1's table ...

print('protenix imported?', 'tt_bio.protenix' in sys.modules)   # -> False
print('opendde imported?', 'tt_bio.opendde' in sys.modules)     # -> False
```

Real output: `protenix imported? False` / `opendde imported? False`, and the
score came back (§3.1's FKBP12 row). **Spec §2/§3's whole architecture — a
question never depends on a fold having run, and rides the existing pick
pathway only for its fold-and-reveal theatre, never for the score itself — is
confirmed true.** Nothing here is BLOCKED.

## 4. A finding not asked for, but load-bearing for Task 4/5's design

The brief's cold/warm framing (model-load-dominated) is the right question for
whether the Q&A worker needs to keep its **device model** resident (yes,
obviously — reloading a checkpoint and recompiling the device kernel per
question would be absurd). It undersells a second, separate cost this spike
found by instrumenting `prepare()`'s internals directly
(`tt_bio.nesso1_input.preprocess`/`collect_esm`/`run_esm`/`build_dataset`):

With **one `Nesso1` model held resident** across three different targets, then
a **fourth call repeating the first target** (DHFR) — i.e., the actual shape
`AffinityScorer.score()` will run in production — the on-device
`model.predict()` cost drops to warm levels as expected (~2–4 s), but the
**host-side `prepare()` step costs ~6–8 s every single time, including on the
exact-repeat call**:

| call | `prepare()` host cost | `model.predict()` (resident model) |
|---|---|---|
| DHFR (1st ever) | 7.94 s | 3.92 s |
| FKBP12 (1st ever, different target) | 7.23 s | 2.04 s |
| Trypsin (1st ever, different target) | 7.33 s | 4.41 s |
| DHFR (repeat — same target, same ligand, same everything) | 6.19 s | 3.72 s |

Breaking `prepare()` down further (own instrumentation, not part of the public
API) shows where that ~6–8 s goes and which piece **is** cached across
repeats: `run_esm`'s ESM-2 embedding compute correctly drops to ~0 s on a
repeat target (`cached()` checks a disk path under `out_dir`, and it hit).
`preprocess()` (YAML parse + RDKit ligand conformer generation) and
`build_dataset()` (feature-tensor assembly) do **not** benefit from any
resident state or repeat-call caching — each costs roughly 2–3 s **every**
call, repeat or not, because `screen()`/`prepare()` re-parses the YAML and
re-derives features from scratch every time by design (there is no
"give me a cached, already-featurized target" entry point in this version of
tt-bio).

**What this means for Task 4/5's code, concretely:** `AffinityScorer.score()`
cannot be assumed to be "just" the on-device forward pass once the model is
loaded. A realistic per-question latency budget on this hardware is roughly
**8–12 s total** (≈6–8 s host prep + ≈2–4 s on-device predict), not the ≈2–4 s
the draft code's docstrings implied by only distinguishing "load" from
"score." This is not a blocker — it is comfortably inside the booth's existing
tolerance for a multi-second wait — but Task 4's `AffinityScorer.score()`
docstring and Task 9's "in-flight" UI state (a spinner, not a fake progress
bar — spec §6) should be written with an ~10 s answer latency in mind, not an
assumption that a resident model makes every question near-instant.

## 5. Summary of the yes/no findings

| question | answer | evidence |
|---|---|---|
| Is there an in-process entry point under the CLI? | **Yes** — `tt_bio.nesso1.screen(...)`, and the lower-level `Nesso1.from_pretrained()` + `tt_bio.nesso1_input.prepare()`/`collate()` + `model.predict(feats)` it's built from. | §2.1, §2.2; CLI source read directly. |
| Exact return shape? | `list[dict]` of scalar fields (`screen()`) or `dict[str, Tensor]` (`model.predict()`); not a bare float. | §3.2, real captured example. |
| Does it need the input file's `properties: affinity:` block? | **No**, when there is exactly one ligand chain (true of all three vendored examples) — the block is validated only if present. | §1, §3.4; ran successfully with no `properties:` block on all three files. |
| Does it require a prior fold / coordinates? | **No.** Confirmed in a fresh process with `tt_bio.protenix`/`tt_bio.opendde` never imported. | §3.4. **Spec §2/§3's architecture is not invalidated.** |
| Cold/warm timing, at least two complexes? | DHFR: 78.2 s / 11.5 s (whole call); FKBP12: 21.6 s / 9.2 s. Trypsin also measured: 18.3 s / 12.3 s. | §3.1. |

## 6. What changed as a result

Tasks 4 and 5's draft code in
`docs/superpowers/plans/2026-09-15-affinity-questions.md` were corrected to
match this spike — see that file's own diff (git history) for the exact
before/after. Summary of the corrections:

- Task 4's `AffinityScorer._score_real` is no longer a `NotImplementedError`
  stub guessing at a CLI shape — it now calls `tt_bio.nesso1.screen()` (or the
  resident lower-level pieces) for real, with the real return-shape handling
  (`affinity_pred_value`, `affinity_probability_binary`, not a bare float).
- Task 4's `AffinityScorer.load()` now calls `Nesso1.from_pretrained(...)`
  directly (mirroring `Folder.load()`'s `get_device()` pattern — no separate
  device-open step needed).
- Task 4's docstrings/timing assumptions now reflect the ~8–12 s realistic
  per-question latency from §4, not a bare on-device-only estimate.
- A new, explicit call-out that `weights.fetch("nesso1")` /
  `weights.fetch("nesso1-ccd")` (and the ESM-2 encoder's own first-use
  download) need to land in whatever provisioning step already handles
  `protenix-v2`'s weights — flagged as a follow-up, not fixed by this task.
