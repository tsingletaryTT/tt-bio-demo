# Putting FKBP12 back, on tt-bio 0.6.4

> **DONE — every step, 2026-08-19, shipped as v0.4.0** (`41263f9`). FKBP12 folds at 9.6 s,
> all seven targets were re-measured on 0.6.4 (42 folds, two chips), the thumbnail was
> re-rendered, and upstream **#11 is closed**. Kept as the record of what the integration
> actually required — including the one thing this checklist did NOT predict: 0.6.4 moved
> `ttnn` into a `tenstorrent` extra, so `setup-venvs.sh` had to become
> `pip install tt-bio[tenstorrent]==...` or the venv ends up with no ttnn at all.
>
> One item deliberately left open: **step 7**, retiring `single_visible_device`. It is dead
> weight on 0.6.4 but wants `./scripts/test.sh --hw` green and its own commit.

Everything here needs the chips. The source-only half is already done and committed: the pin
is `0.6.4`, `INSTALL.md` says so, and `runner/env.py` records that 0.6.4 supersedes the
one-chip-visibility workaround.

**Do not shortcut step 3.** This project's rule is that a gallery card carries a measured
number or says "not yet timed", and every number now on a card was measured on **0.6.3**.
0.6.4 changes the trimul chunk width on a clash, so the timings are not automatically the
same — they have to be re-measured, not assumed.

---

## 1. Rebuild `venv-runner` against 0.6.4

```bash
gozer run --chips 1 --who "claude:integrate-064" --reason "rebuild venv-runner on 0.6.4" -- \
    ./scripts/setup-venvs.sh --force --dev
```

Under a lease because the script ends with a real device open/close probe. **Read the exit
code**: `0` good, `1` hard failure, `2` built-but-non-functional — see `INSTALL.md` step 2.

Confirm what landed:

```bash
.venvs/venv-runner/bin/python3 -c "import importlib.metadata as m; print(m.version('tt_bio'))"
# expect 0.6.4
```

## 2. Confirm the fix on the release (not the branch)

We confirmed `8ad20bd8`; this confirms the shipped wheel.

```bash
gozer run --chips 1 --who "claude:integrate-064" --reason "fkbp12 on the 0.6.4 release" -- \
    env TT_VISIBLE_DEVICES=0000:01:00.0 .venvs/venv-runner/bin/tt-bio predict \
    examples/affinity_fkg.yaml --model protenix-v2 --accelerator tenstorrent \
    --single_sequence --out_dir /tmp/fkbp12-064
```

Expect a clean fold. One `critical`-level clash line may still appear from tt-metal, now
followed by tt-bio saying it is retrying narrower — that is the fix working, not a failure.

## 3. Re-measure EVERY target on 0.6.4

The whole playlist, plus FKBP12. Three warm folds each, model resident, on two chips — the
same shape as the 0.6.3 pass that produced the table in `playlist/manifest.yaml`'s header.

Targets and inputs (note the filenames differ from the ids):

| id | input | residues |
|---|---|---|
| trpcage | `examples/trpcage_no_msa.yaml` | 20 |
| fkbp12 | `examples/affinity_fkg.yaml` | 107 |
| dhfr | `examples/affinity_dhfr.yaml` | 187 |
| trypsin | `examples/affinity_tryp.yaml` | 223 |
| hsa | `examples/hsa_no_msa.yaml` | 585 |
| dna | `examples/dna_dickerson.yaml` | 24 |
| trna | `examples/trna_phe.yaml` | 76 |

**Watch HSA especially.** It is the largest target and has never run on 0.6.4; if the trimul
narrows a width for its shapes, it is the one most likely to move. We promised Moritz we would
report anything that shows up at 585 residues.

Open a device with **one chip visible** or a pair — 0.6.4 accepts both. If you use the pair,
that also re-confirms his mesh fix on the release.

## 4. Put FKBP12 back on the playlist

The full entry — tagline, blurb, all its comments — is in git history:

```bash
git show 72b03bb^:playlist/manifest.yaml   # the version that still had it
```

Restore it between `trpcage` and `dhfr` (its original slot), then:

- set `expected_s` to the measured 0.6.4 warm p50 **(it was 11.7 s on 0.6.2, 9.8 s on the fix
  branch — do not paste either; measure it)**
- rewrite the removal note in the manifest header: it currently explains why FKBP12 is out.
  It comes back with the story of why, which is worth keeping short and true.
- **the tagline must stay under ~100 characters** or it wraps beside the confidence legend and
  costs the render 28px. `test_the_confidence_legend_costs_the_protein_no_height` catches it;
  the original tagline passed, so restoring it verbatim is safe.

## 5. Regenerate the thumbnail

Deleted when the target was removed. It is a real fold, rendered by the booth's own renderer:

```bash
gozer run --chips 1 --who "claude:integrate-064" --reason "fkbp12 thumbnail" -- \
    ./scripts/make-thumbnails.py --only fkbp12
```

Writes **both** `playlist/thumbnails/` and `docs/thumbnails/`;
`test_the_sites_thumbnails_are_the_ones_the_booth_ships` fails if they drift.

## 6. Update every surface that carries a number

All of these were re-measured together last time and must move together:

- `playlist/manifest.yaml` — the header table AND each entry's `expected_s`
- `docs/index.html` — six molecule cards: name, tagline and `N residues · X s`.
  `test_the_docs_site_cards_say_what_the_manifest_says` compares them to the manifest
- `docs/onepager/onepager.html.tmpl` — the "what it folds" table, then
  `./docs/onepager/build.sh`, then regenerate `docs/screenshots/onepager-{front,back}.jpg`
- `README.md` — the warm-times sentence in "The instrument"

## 7. Consider removing the visibility workaround

`runner.env.single_visible_device` is no longer required on 0.6.4. Removing it touches
`tests/integration/conftest.py` and `tests/unit/runner/test_runner_env.py`. Only do it with
the hardware suite green on 0.6.4 (`./scripts/test.sh --hw`), and treat it as its own commit —
it is a cleanup, not part of the release.

## 8. Ship it

`VERSION` → `0.4.0` (a target returns and every number moves; that is minor, not patch),
changelog stanza, `./docs/onepager/build.sh`, full `./scripts/test.sh`, then tag `v0.4.0` and
let CI cut the release.
