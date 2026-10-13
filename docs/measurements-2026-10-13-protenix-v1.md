# Measurements: all seven targets on protenix-v1, with a protenix-v2 reference (2026-10-13)

Every number the manifest, README, site and handout print about timing and confidence comes
from this file. Raw per-run numbers are at the end.

## Method

- **Host stack** (changed since an earlier attempt at this task, which was lost to a host
  freeze at about 11:26 PDT; nothing from that attempt is used): tt-kmd 2.11.0, firmware
  bundle 19.16.0.0 on all four chips, tt-smi 6.7.0, tt-flash 4.1.0, tt-umd 0.9.12,
  pyluwen 0.10.0, ttnn 0.68.0, tt-bio 0.15.2, kernel 7.0.0-38-generic. `tt-qb-lights` was
  left running on purpose.
- **Chip.** One chip, chip 0 (BDF 0000:01:00.0, board 0000046131924062), under a gozer
  lease each time. `TT_VISIBLE_DEVICES=0000:01:00.0` was printed by the script at the
  start of every arm. gozer leases a whole board, so chip 1 was claimed by the same lease
  and idle. All commands went through `scripts/hw-run.sh --chips 1 --one-chip`, in the
  background, one at a time, with the log polled every 20-30 s.
- **Arms and order** (all on chip 0; the lease was released, which resets the chips,
  between arms):

  | arm | model | passes | start | end | gozer lease |
  |---|---|---|---|---|---|
  | v1A | protenix-v1 | 4 | 12:15:07 | 12:22:47 | d3ab95 |
  | v2 | protenix-v2 (reference) | 4 | 12:23:18 | 12:32:57 | 466def |
  | v1B | protenix-v1 | 3 | 12:33:23 | 12:36:16 | 3554d2 |
  | thumbnails | protenix-v1 | 1 | 12:36:30 | 12:38:02 | a382fb |

  `gozer status` showed all four chips FREE before and after every arm (saved copies in
  the report). During each arm chip 0 showed HELD and chip 1 CLAIMED, both under our pid.
  No stall, no 0xFFFF board serial, no BUSY-UNTRACKED, no ethernet-core timeout.
- **One observation about the host-stack change, not an A/B result.** The first attempt at
  arm v1A, on the earlier host stack, wedged chip 0 and froze the box (the 11:26 freeze, on
  tt-kmd 2.10.0). The identical command completed on the later stack in 7 min 40 s, longest
  silence 17.4 s. That is one failure and one success. The tools changed together, so it
  cannot say which change mattered, and one run on each side cannot rule out that the wedge
  was a chance event. The kmd history matters here: the earlier freeze (10:10) ran on
  tt-kmd 2.11.0, the same kmd as the clean run.
- **Host stack before and after** (before = the stack in place when the 11:26 wedge
  happened). tt-kmd history from the kernel log and `/var/log/dpkg.log`: 2.11.0 at the 10:10
  freeze; dpkg moved it to 2.10.0 at 10:45 (who ran that is unknown to me); 2.10.0 at the
  11:26 freeze; dpkg moved it back to 2.11.0 at 12:11, followed by a reboot. So the kmd is
  2.11.0 on the later stack and was 2.10.0 on the earlier one, and it was also 2.11.0 at the
  first freeze. The other changes, installed about 12:10: firmware bundle 19.16.0.0 at both times as far as known; tt-smi
  6.1.0 -> 6.7.0; tt-flash 3.10.0 -> 4.1.0; tt-umd 0.9.8 -> 0.9.12; pyluwen 0.8.5 -> 0.10.0.
  ttnn 0.68.0 and tt-bio 0.15.2 were the same. Which of these were already in place when the
  0.12.0-era numbers in the manifest were taken is unknown to me.
- **Harness.** The brief names `tests/integration/test_new_targets_timing.py`, which folds
  each target once per invocation. The measurement here used an uncommitted scratchpad
  script that copies that harness's stamping exactly (an `emit` callback records the
  offset of every event, so first-frame time and longest silence are measured the same
  way) and loops it for several passes on one resident `Folder`, in the harness's
  target order (trpcage, fkbp12, dhfr, trypsin, dna, trna, hsa). It uses the booth's own
  `runner.folder.Folder` and `fold()` with `n_step=200`, the same input YAMLs in
  `examples/`, and no explicit seed (the model's default) for both models. The pytest
  harness itself was not run.
- **v2 reference arm.** `runner.folder.BOOTH_MODEL` was set to `protenix-v2` in memory in
  the scratchpad script only, so `Folder.load()` loaded `~/.boltz/protenix-v2.pt`. No repo
  code changed. tt-bio 0.15.2 does not download v2 but still loads a file that is present.
  The v2 numbers are therefore v2 on tt-bio 0.15.2 on this host stack, not the 0.12.0 numbers
  the manifest used to print.
- **Cold and warm.** The first pass of arm v1A is JIT-cold for every target except trpcage
  (its kernels were cached by an earlier, lost run) and is reported separately. Warm v1 =
  arm v1A passes 2-4 plus arm v1B passes 2-3 (5 folds per target). Warm v2 = passes 2-4
  (3 folds). Arm v1B pass 1 is also warm for every target but trpcage (a first fold of a
  new process), and is in the raw table but not in the means.
- **Drift.** Arm v1B ran 18 min after arm v1A started. Warm times agree with arm v1A to
  within 0.2 s on every target (table below), so this run shows no clock drift. This
  does not test the documented 15-minute drift cleanly: the lease release between arms
  resets the chips, so no arm ran for 15 minutes on one reset.
- The first-pass JIT cache was valid after the driver and firmware change for trpcage;
  every other target compiled anew in the v1A and v2 first passes.

## Results

All times are seconds on chip 0. "First frame" is seconds from `job_start` to the first
`frame` event. "Max silence" is the longest gap between any two events in a fold (worst
of the warm folds).

| target | residues | v2 warm | v1 warm | v1 first frame | v1 max silence | v1 mean pLDDT range | v2 mean pLDDT range | v1 cold first fold |
|---|---|---|---|---|---|---|---|---|
| trpcage | 20 | 3.75 | 2.99 | 0.37 | 0.09 | 93.35-93.57 | 95.34-95.37 | 4.9 (cached, not cold) |
| fkbp12 + SB3 | 107 | 7.66 | 4.43 | 0.97 | 0.29 | 39.15-40.43 | 48.88-52.15 | 32.8 (silence 14.9) |
| dhfr + MTX | 187 | 11.28 | 5.28 | 1.67 | 0.49 | 32.85-33.52 | 53.29-53.50 | 53.3 (silence 15.6) |
| trypsin | 223 | 13.97 | 5.88 | 2.26 | 0.64 | 30.89-32.00 | 38.56-38.59 | 54.8 (silence 16.6) |
| dna (24 nt) | 24 | 3.81 | 3.06 | 0.39 | 0.09 | 94.46-94.48 | 95.93-95.94 | 22.8 (silence 10.3) |
| trna | 76 | 4.99 | 3.47 | 0.56 | 0.15 | 81.02-81.76 | 88.80-88.83 | 46.7 (silence 15.0) |
| hsa | 585 | 51.63 | 16.11 | 10.10 | 2.96 | 49.52-50.59 | 74.46-74.48 | 74.7 (silence 17.4) |

v2 first frame, warm: trpcage 1.13, fkbp12 4.19, dhfr 7.60, trypsin 10.28, dna 1.15, trna 2.07,
hsa 45.31. v2 first fold of the process (cold): trpcage 25.8, fkbp12 32.0, dhfr 34.5, trypsin 35.3,
dna 5.7, trna 29.8, hsa 78.2.

Drift check, v1 warm total seconds, arm v1A (passes 2-4) against arm v1B (passes 2-3):
trpcage 2.99 / 2.98, fkbp12 4.44 / 4.40, dhfr 5.27 / 5.24, trypsin 5.87 / 5.85,
dna 3.05 / 3.08, trna 3.46 / 3.47, hsa 16.13 / 16.10.

Headlines:

- v1 is faster than v2 on every target, warm: 1.25x on trpcage and dna, 1.4x on trna, 1.7x on
  fkbp12, 2.1x on dhfr, 2.4x on trypsin and 3.2x on hsa. v1 runs 4 trunk cycles where v2 runs 10
  (docs/spike-protenix-v1.md).
- v1 is less confident than v2 on every target: about 1.5-2 points on trpcage and dna, 7 on
  trna, 7-20 on the three `msa: empty` proteins (trypsin 7, FKBP12 11, DHFR 20), 24 on albumin.
- The fastest warm fold is trpcage at 2.99 s. The slowest is albumin at 16.1 s.
- All seven targets fold. FKBP12 with SB3 did not hit the 0.6.3 L1 clash on v1 or on v2.
  tt-metal logged a handled `TT_THROW` circular-buffer line (and tt-bio's own "retrying
  narrower" line) on the cold passes, twice per arm; every fold completed.
- The v2 reference on this stack is already much faster than the 0.12.0 numbers the manifest
  used to print (hsa 51.6 s here against 84.4 s; trypsin 14.0 against 17.4). So part of the
  speed-up that a reader would see between the old manifest and the new one is the stack
  and tt-bio 0.15.2, not the model. The v1 against v2 columns above are the like-for-like
  comparison.

## Cold start and the booth

First fold of each shape on v1 took 22.8-74.7 s with up to 17.4 s of silence (the progress
bar frozen; the longest in the harness's own 30 s budget). That budget is not breached.
After the first pass every fold was warm. A venue should still warm every target once
after a board reset or a tt-bio upgrade, as docs/cold-start.md says.

## Showcase dwell

`first_frame_s` caps the dwell in front of each target (clamp [2.0, 7.0] s, ui/app.py).
With v1 numbers: trpcage 0.4, dna 0.4, trna 0.6, fkbp12 1.0, dhfr 1.7, trypsin 2.3 and hsa
10.1 give dwells of 2.0, 2.0, 2.0, 2.0, 2.0, 2.3 and 7.0 s. DHFR and trypsin held a
structure for up to 7.0 s before. Only albumin still reaches the maximum. The unit tests
tied to these numbers are listed at the end.

## Structural claims, judged on v1 CIFs

Method: `analyze.py`, an uncommitted scratchpad script run with the UI venv (gemmi, numpy),
on the CIFs from arm v1A pass 3 and pass 4, and from v2 pass 3 for comparison. The script
that produced the earlier 124-degree tRNA corner figure and the earlier C1'-C1' numbers
was not kept anywhere in the repo (grep of CLAUDE.md, docs/ and scripts/ found only the
quoted results), so definitions below are stated and applied identically to v1 and v2.

**DNA duplex (CGCGAATTCGCG, two chains).** Holds. All twelve base pairs (strand A residue
i against strand B residue 13-i) have C1'-C1' distances 10.40-10.75 A, mean 10.55 (pass
3; pass 4 mean 10.51, range 10.33-10.75). The earlier claim was 10.6-10.7. Compact: heavy
atom radius of gyration 13.3 A, helix length along its principal axis 38.4 A (12 base
pairs of B-DNA are 37-40 A). Mean pLDDT 94.5, per-atom B-factors 82.7-96.7, which the
ramp draws blue and pale blue. v2: 10.52 mean, 38.0 A, B 84.7-97.4.

**tRNA-Phe.** Pairing holds. All four stems are Watson-Crick paired: stem means
10.52 (acceptor), 10.69 (D), 10.56 (anticodon), 10.54 (T) A, individual pairs 10.23-10.78 A
(pass 3). Compact holds: radius of gyration 23.06 A over heavy atoms (23.55 on P atoms,
23.09 on C1' atoms) against 23-24 A for real tRNA. Anticodon (C1' centroid of residues
34-36) to the 3' end 76.1 A (pass 3) and 69.2 A (pass 4); v2 69.0 A; crystal about 75-80 A.
**The corner claim does not reproduce.** With the corner defined as the angle at the
centroid of the D and T loops between the centroid of the acceptor stem and the centroid of
the anticodon stem (C1' atoms), v1 gives 91.3 and 89.7 degrees and v2 gives 92.2. With it
defined as the angle between the helix axes (acceptor to T stem, and D stem to anticodon
stem), v1 gives 114.5 and 111.9 and v2 114.8. The earlier "about 124 degrees against a
crystal 90" cannot be rechecked because its definition is not on file, and v1 and v2 agree
with each other under both definitions here, so nothing distinguishes the models. The
manifest's "wide, open elbow rather than the L" sentence is removed. The blurb
now says "a compact shape": "hinged" was never measured and is dropped; "compact" rests on the radius of gyration. Per-residue B-factors on v1 are 57.7-89.7
(mean 81.4; v2 mean 88.8), so the ribbon is pale blue with a yellow stretch, not the
deep blue of v2.

**FKBP12, DHFR, trypsin (low-confidence wording).** The blurbs and the DNA entry say the
`msa: empty` proteins come back "mostly yellow and orange". On v1, per-C-alpha pLDDT
(pass 3): FKBP12 85% below 50 and 15% in 50-70; DHFR 95% below 50 and 5% in 50-70;
trypsin 100% below 50. The ramp draws below 50 orange and 50-70 yellow, so these come back
almost entirely orange. "Mostly yellow and orange" is still true but trypsin and DHFR show
no yellow to speak of. The wording was left alone (it describes the set, and albumin and
FKBP12 do show yellow). No number in a blurb changed. Mean pLDDT fell from 39-53 on v2 to 31-40.

**FKBP12 + SB3 and DHFR + MTX ligands.** Present. In every fold's CIF chain B is one
residue named LIG: 33 heavy atoms for FKBP12 and for DHFR, 9 for trypsin, all coordinates
finite, none within 1 A of a protein heavy atom (closest 1.9-2.75 A across the DHFR
folds). Both models produce them. FKBP12 does not hit the 0.6.3 L1 clash on this stack.

**Trp-cage and albumin.** Trp-cage pLDDT 93.4-93.6 (v2 95.3-95.4): all C-alphas at 90 or
above. Albumin on v1: mean 49.5-50.6, per-C-alpha 42% below 50, 47% in 50-70 and 11% in
70-90 (v2: 74.5 mean, 74% in 70-90). The albumin blurb makes no confidence claim.

## Thumbnails

Regenerated with `scripts/hw-run.sh --chips 1 --one-chip --reason "thumbnails on
protenix-v1" -- .venvs/venv-runner/bin/python scripts/make-thumbnails.py` (lease a382fb).
Its own folds reported pLDDT 93.5 (trpcage) and 50.4 (hsa); all seven PNGs are written to
both `playlist/thumbnails/` and `docs/thumbnails/` and compare byte-identical. Each PNG was
opened and looked at: trpcage is a blue helix with a loop tail; fkbp12, dhfr and trypsin
are orange ribbons (fkbp12 and dhfr with a few yellow strands and the ligand as sticks);
dna is two blue tubes winding together (a loose double helix); trna is a pale
blue compact tangle with a yellow end; hsa is a mostly yellow and orange helical bundle
with a pale blue helix pair. Each is a plausible picture of that fold.

## Tests after the manifest change

Run: UI half `pytest tests/unit --ignore=tests/unit/runner` (venv-ui): 1602 passed, 3 failed.
Runner half `pytest tests/unit/runner` (venv-runner): 461 passed. Every dwell, first_frame,
playlist and manifest test other than the one below passed with the new numbers, including
the visibility guard for live diffusion.

- `tests/unit/test_playlist.py::test_the_docs_site_cards_say_what_the_manifest_says` fails
  because `docs/index.html` still prints the old times on its cards (the first mismatch is
  Trp-cage: site 4.5 s, manifest 3.0 s). The manifest moved ahead of site copy. Task 6
  owns the site, README and handout numbers.
- `tests/unit/test_packaging.py::test_the_pin_is_read_from_setup_venvs_not_duplicated` and
  `::test_the_pin_is_the_one_setup_venvs_actually_declares` fail on this box for the known
  environmental reason: they read the stale 0.5.5 package install under `/opt/tt-bio-demo`.
  With `TT_BIO_DEMO_PREFIX=/nonexistent` both pass.

## Raw per-run numbers

| arm | pass | target | total s | first frame s | max silence s | frames | mean pLDDT | wall clock |
|---|---|---|---|---|---|---|---|---|
| v1A | 1 | trpcage | 4.86 | 1.74 | 0.97 | 30 | 93.449 | 12:15:16 |
| v1A | 1 | fkbp12 | 32.75 | 4.09 | 14.92 | 30 | 38.552 | 12:15:49 |
| v1A | 1 | dhfr | 53.29 | 32.03 | 15.63 | 30 | 33.077 | 12:16:42 |
| v1A | 1 | trypsin | 54.83 | 34.46 | 16.58 | 30 | 31.5 | 12:17:37 |
| v1A | 1 | dna | 22.76 | 11.91 | 10.32 | 30 | 94.479 | 12:17:59 |
| v1A | 1 | trna | 46.66 | 29.36 | 14.97 | 30 | 81.576 | 12:18:46 |
| v1A | 1 | hsa | 74.71 | 44.47 | 17.36 | 30 | 50.15 | 12:20:01 |
| v1A | 2 | trpcage | 2.99 | 0.37 | 0.09 | 30 | 93.348 | 12:20:04 |
| v1A | 2 | fkbp12 | 4.45 | 0.97 | 0.29 | 30 | 40.427 | 12:20:08 |
| v1A | 2 | dhfr | 5.29 | 1.68 | 0.49 | 30 | 32.846 | 12:20:14 |
| v1A | 2 | trypsin | 5.87 | 2.26 | 0.63 | 30 | 31.683 | 12:20:19 |
| v1A | 2 | dna | 3.04 | 0.39 | 0.09 | 30 | 94.484 | 12:20:22 |
| v1A | 2 | trna | 3.47 | 0.56 | 0.15 | 30 | 81.759 | 12:20:26 |
| v1A | 2 | hsa | 16.1 | 10.11 | 2.96 | 30 | 49.515 | 12:20:42 |
| v1A | 3 | trpcage | 2.99 | 0.37 | 0.09 | 30 | 93.512 | 12:20:45 |
| v1A | 3 | fkbp12 | 4.45 | 0.97 | 0.29 | 30 | 39.525 | 12:20:50 |
| v1A | 3 | dhfr | 5.22 | 1.67 | 0.49 | 30 | 33.391 | 12:20:55 |
| v1A | 3 | trypsin | 5.84 | 2.26 | 0.63 | 30 | 30.894 | 12:21:01 |
| v1A | 3 | dna | 3.06 | 0.4 | 0.09 | 30 | 94.47 | 12:21:04 |
| v1A | 3 | trna | 3.44 | 0.56 | 0.15 | 30 | 81.384 | 12:21:07 |
| v1A | 3 | hsa | 16.14 | 10.12 | 2.96 | 30 | 50.587 | 12:21:23 |
| v1A | 4 | trpcage | 2.99 | 0.37 | 0.09 | 30 | 93.418 | 12:21:26 |
| v1A | 4 | fkbp12 | 4.41 | 0.97 | 0.29 | 30 | 39.595 | 12:21:31 |
| v1A | 4 | dhfr | 5.3 | 1.67 | 0.49 | 30 | 32.928 | 12:21:36 |
| v1A | 4 | trypsin | 5.91 | 2.27 | 0.64 | 30 | 31.34 | 12:21:42 |
| v1A | 4 | dna | 3.05 | 0.39 | 0.09 | 30 | 94.463 | 12:21:45 |
| v1A | 4 | trna | 3.46 | 0.56 | 0.15 | 30 | 81.021 | 12:21:48 |
| v1A | 4 | hsa | 16.15 | 10.12 | 2.96 | 30 | 50.225 | 12:22:04 |
| v2 | 1 | trpcage | 25.81 | 19.2 | 13.74 | 30 | 95.358 | 12:23:47 |
| v2 | 1 | fkbp12 | 31.99 | 23.86 | 15.93 | 30 | 51.742 | 12:24:19 |
| v2 | 1 | dhfr | 34.49 | 26.67 | 14.65 | 30 | 53.889 | 12:24:53 |
| v2 | 1 | trypsin | 35.32 | 27.79 | 14.8 | 30 | 38.615 | 12:25:29 |
| v2 | 1 | dna | 5.67 | 2.39 | 1.28 | 30 | 95.934 | 12:25:34 |
| v2 | 1 | trna | 29.8 | 23.02 | 17.29 | 30 | 88.781 | 12:26:04 |
| v2 | 1 | hsa | 78.23 | 67.06 | 21.39 | 30 | 74.552 | 12:27:23 |
| v2 | 2 | trpcage | 3.74 | 1.13 | 0.11 | 30 | 95.358 | 12:27:26 |
| v2 | 2 | fkbp12 | 7.69 | 4.19 | 0.52 | 30 | 52.153 | 12:27:34 |
| v2 | 2 | dhfr | 11.29 | 7.6 | 0.92 | 30 | 53.29 | 12:27:45 |
| v2 | 2 | trypsin | 13.97 | 10.29 | 1.19 | 30 | 38.565 | 12:27:59 |
| v2 | 2 | dna | 3.79 | 1.14 | 0.11 | 30 | 95.939 | 12:28:03 |
| v2 | 2 | trna | 4.97 | 2.07 | 0.25 | 30 | 88.825 | 12:28:08 |
| v2 | 2 | hsa | 51.66 | 45.32 | 5.27 | 30 | 74.483 | 12:29:00 |
| v2 | 3 | trpcage | 3.75 | 1.13 | 0.11 | 30 | 95.368 | 12:29:03 |
| v2 | 3 | fkbp12 | 7.62 | 4.18 | 0.51 | 30 | 50.777 | 12:29:11 |
| v2 | 3 | dhfr | 11.29 | 7.62 | 0.91 | 30 | 53.452 | 12:29:22 |
| v2 | 3 | trypsin | 14.0 | 10.28 | 1.19 | 30 | 38.588 | 12:29:36 |
| v2 | 3 | dna | 3.83 | 1.15 | 0.11 | 30 | 95.928 | 12:29:40 |
| v2 | 3 | trna | 5.0 | 2.07 | 0.25 | 30 | 88.803 | 12:29:45 |
| v2 | 3 | hsa | 51.67 | 45.32 | 5.29 | 30 | 74.46 | 12:30:37 |
| v2 | 4 | trpcage | 3.77 | 1.12 | 0.11 | 30 | 95.341 | 12:30:41 |
| v2 | 4 | fkbp12 | 7.68 | 4.19 | 0.51 | 30 | 48.876 | 12:30:48 |
| v2 | 4 | dhfr | 11.27 | 7.59 | 0.92 | 30 | 53.497 | 12:31:00 |
| v2 | 4 | trypsin | 13.93 | 10.28 | 1.19 | 30 | 38.592 | 12:31:13 |
| v2 | 4 | dna | 3.82 | 1.15 | 0.12 | 30 | 95.939 | 12:31:17 |
| v2 | 4 | trna | 4.99 | 2.08 | 0.25 | 30 | 88.818 | 12:31:22 |
| v2 | 4 | hsa | 51.57 | 45.28 | 5.28 | 30 | 74.457 | 12:32:14 |
| v1B | 1 | trpcage | 4.47 | 1.45 | 0.81 | 30 | 93.452 | 12:33:30 |
| v1B | 1 | fkbp12 | 4.94 | 1.28 | 0.32 | 30 | 40.631 | 12:33:35 |
| v1B | 1 | dhfr | 5.72 | 1.95 | 0.51 | 30 | 32.822 | 12:33:41 |
| v1B | 1 | trypsin | 6.26 | 2.48 | 0.66 | 30 | 31.184 | 12:33:47 |
| v1B | 1 | dna | 3.23 | 0.51 | 0.14 | 30 | 94.474 | 12:33:50 |
| v1B | 1 | trna | 3.8 | 0.79 | 0.22 | 30 | 81.158 | 12:33:54 |
| v1B | 1 | hsa | 16.35 | 10.27 | 2.98 | 30 | 49.91 | 12:34:10 |
| v1B | 2 | trpcage | 2.99 | 0.37 | 0.09 | 30 | 93.573 | 12:34:13 |
| v1B | 2 | fkbp12 | 4.4 | 0.97 | 0.29 | 30 | 40.316 | 12:34:18 |
| v1B | 2 | dhfr | 5.24 | 1.66 | 0.48 | 30 | 33.204 | 12:34:23 |
| v1B | 2 | trypsin | 5.93 | 2.25 | 0.63 | 30 | 31.704 | 12:34:29 |
| v1B | 2 | dna | 3.09 | 0.39 | 0.09 | 30 | 94.469 | 12:34:32 |
| v1B | 2 | trna | 3.47 | 0.56 | 0.15 | 30 | 81.736 | 12:34:35 |
| v1B | 2 | hsa | 16.1 | 10.08 | 2.95 | 30 | 50.456 | 12:34:52 |
| v1B | 3 | trpcage | 2.98 | 0.37 | 0.09 | 30 | 93.509 | 12:34:55 |
| v1B | 3 | fkbp12 | 4.45 | 0.97 | 0.29 | 30 | 39.152 | 12:34:59 |
| v1B | 3 | dhfr | 5.34 | 1.66 | 0.48 | 30 | 33.516 | 12:35:04 |
| v1B | 3 | trypsin | 5.85 | 2.25 | 0.63 | 30 | 31.996 | 12:35:10 |
| v1B | 3 | dna | 3.08 | 0.39 | 0.09 | 30 | 94.469 | 12:35:13 |
| v1B | 3 | trna | 3.51 | 0.56 | 0.15 | 30 | 81.672 | 12:35:17 |
| v1B | 3 | hsa | 16.08 | 10.07 | 2.95 | 30 | 50.45 | 12:35:33 |
