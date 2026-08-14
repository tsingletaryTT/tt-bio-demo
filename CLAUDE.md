# tt-bio-demo — project log

A turnkey GTK4 conference demo for [tt-bio](https://github.com/moritztng/tt-bio) protein
structure prediction on a Tenstorrent QB2.

- Design spec: [`docs/superpowers/specs/2026-08-10-tt-bio-demo-design.md`](docs/superpowers/specs/2026-08-10-tt-bio-demo-design.md)
- Phase 1–2 plan: [`docs/superpowers/plans/2026-08-10-protocol-and-renderer.md`](docs/superpowers/plans/2026-08-10-protocol-and-renderer.md)
- Known follow-ups and gotchas: [`docs/followups.md`](docs/followups.md) — **read this before touching the renderer**

## What happened

### 2026-08-10 — project spun up from a tt-boltz session

Started in `~/code/tt-boltz` with: *"can we make sure this directory / repo is tracking the
recently renamed-to tt-bio project's latest release? I'd like to examine viability for an
automated, visual running demo to leave on in a conference setting."*

That session re-pointed `~/code/tt-boltz` at `moritztng/tt-bio` (the project was renamed
from tt-boltz) and reset it to release **v0.6.2**, keeping the stale local main on branch
`pre-tt-bio-sync-backup`. Viability came back favorable, and the follow-up prompt asked to
spec the demo as its own project here, with debian packaging for turnkey install on a fresh
QB2 — and specifically asked whether we could **avoid a web browser** in favor of an
integrated GTK UI.

### Key decisions

- **Native GTK4 for everything the demo is claiming.** The original viability sketch assumed
  a browser panel running Mol* for the 3D view. Taylor pushed back on that, and the native
  path turned out better: one scene graph, one visual language, and the protein is a real
  widget alongside the telemetry rather than an iframe next to it.
  **Amended 2026-08-12 (Phase 3b):** this was written as "no WebKit", and that is no longer
  literally true. The Tensix activity panel (`ui/chipviz.py`) is a `WebKit.WebView` holding a
  vendored [tensix-viz](ui/assets/tensix-viz/PROVENANCE.md) animation. The decision the
  pushback was actually about — the 3D protein view — is unchanged and stays GTK4 + OpenGL;
  the exception is scoped to one 430 px decorative panel that hides itself if WebKit is
  missing, loads one local `about:blank` page, never navigates and now declares
  `default-src 'none'`. See `ui/chipviz.py`'s "Why there is a WebView" section, and the
  sandbox note below.
- **`WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1` is load-bearing for the booth.** Ubuntu
  24.04 restricts unprivileged user namespaces (`kernel.apparmor_restrict_unprivileged_userns
  = 1`), so WebKitGTK's bubblewrap sandbox cannot start and WebKit responds with a `g_error`
  — a SIGTRAP that kills the whole process and that **no Python `try/except` can catch**.
  Without this variable the kiosk aborts at startup, at the venue, with nothing on screen.
  Its blast radius is bounded by what the WebView loads: one vendored local page, no
  navigation, no network, no remote or user-supplied bytes, plus the CSP above. `setdefault`,
  not assignment, so an operator on a machine where the sandbox works can export `0`.
- **Two processes, two Python environments.** tt-bio needs a torch/ttnn venv; GTK needs
  system `python3-gi`. Splitting them removes the conflict instead of managing it, and gives
  fault isolation for free — the UI holds no device handles and cannot be taken down by a
  wedged card.
- **Live diffusion trajectory via `dump_fn`.** Discovered while assessing feasibility that
  tt-bio's sampler already exposes `dump_fn(sample, step, coords)` per denoising step in
  `protenix.py`/`opendde.py`. This turns "watch it fold" from decorative animation into the
  actual computation, streamed live. Protenix-v2/OpenDDE only for now; adding the hook to
  the other models is a deliberate out-of-scope follow-up so this project never blocks on
  upstream changes.
- **Point cloud during diffusion, ribbon on completion.** `dump_fn` coordinates come in the
  model's internal atom ordering, so building a ribbon mid-trajectory means reconstructing
  atom→residue mapping — the riskiest code in the project. Rendering raw atoms as points
  during diffusion needs no chemistry at all, and cross-fading to a gemmi-parsed ribbon at
  the end deletes the risk while making the sequence better theatre.
- **Kiosk boot rejected for now.** Desktop app instead; the runner is a supervised
  `systemd --user` service. Less display-stack surface to debug at a venue.
- **Weights via postinst download, not a fat .deb.** Offline operation is required at the
  venue, not at install time.

### Notable moments in prompting

- Taylor pointed at `~/code/tt-local-generator` and `~/code/tt-station` as prior art
  mid-turn. Both paid off immediately: tt-local-generator's `app/activity_viz.py` has a
  working `tt-smi` telemetry sampler (with an AICLK fallback) to port directly, and its
  `debian/` tree is the multi-package + debconf-templates pattern this project copies.
- The interactivity question came back as *"3 but with an attract loop too"* — i.e. a kiosk
  visitors can drive that also runs itself when nobody's there. That answer is what produced
  the four-state machine with a 45-second idle reset.

### 2026-08-11 — Phase 1–2 built (protocol, mock runner, renderer)

Executed the Phase 1–2 plan with subagent-driven development: a fresh implementer per
task, a spec-and-quality review after each, and a whole-branch review at the end.
Ten tasks, 24 commits, 83 tests. The deliverable works: a recorded fold plays back
end to end — noise cloud, contraction, cross-fade, rotating pLDDT ribbon — on any
Linux box with no Tenstorrent hardware attached.

**The review loop found nine genuine bugs in the plan's own code**, not in the
implementations of it. The plan was written carefully and reviewed before execution,
and it was still wrong in nine places. Worth remembering the next time a plan looks
finished:

- `look_at` stored its rotation basis as rows where column-major storage needs
  columns — wrong for every off-axis camera.
- `tube_mesh` wound every triangle backwards; with backface culling the ribbon would
  have been invisible.
- The point-size formula cancelled exactly against the camera distance, pinning atoms
  at ~1.3 px at any zoom. The fold rendered, invisibly.
- `EventClient._run` set state to `"disconnected"` before testing for
  `"incompatible"`, clobbering the flag its own guard checked — infinite reconnects.
- `on_event` was called unguarded, and `unpack_coords` too, either of which silently
  freezes a GLib source forever.
- `clear_structure` reset the blend but not its target, so a second fold faded toward
  a discarded ribbon.
- `_frame_camera`'s streaming ease was reused for the one-shot ribbon, which therefore
  never converged and left the hero image permanently mis-scaled.
- A degenerate leading tangent produced an all-NaN mesh, silently.
- Every command in the plan used bare `python3`, which resolves to a Tenstorrent venv
  lacking gemmi and GTK.

Each fix was applied to the shipped code *and* back to the plan document, so the plan
of record now matches what was built rather than quietly disagreeing with it.

**The recurring failure mode was tests that could not fail.** Four of those bugs
survived a green suite because the test could not distinguish the right answer from
the wrong one. See the last section of [`docs/followups.md`](docs/followups.md).

### 2026-08-11 — venv bootstrap: the project owns its own environment now

Prompted from `~/code/tt-boltz` with a request to give tt-bio-demo project-owned
virtualenvs instead of running against `/usr/bin/python3` bare and a personal
`~/.tenstorrent-venv` for the future runner — the venv was a different CPython
*build* (uv 3.12.12 vs. apt's 3.12.3) and had neither `ttnn` nor `tt_bio`, so it
was never going to work for Phase 3 and had to be retired before it caused a
worse surprise than the bare-`python3` trap already on record.

**What got built:** `scripts/setup-venvs.sh` creates `.venvs/venv-ui` (system
python + `--system-site-packages`, inherits apt's `python3-gi`/`gemmi`/`OpenGL`/
`numpy`) and `.venvs/venv-runner` (isolated, `pip install tt-bio==0.6.2`, pinned
in one variable at the top of the script). Idempotent — a second run is a
~0.3–0.5 s no-op once both venvs verify — with `--force` to rebuild and
`--skip-runner` to skip the expensive half while iterating on the UI side.
`scripts/test.sh` retires the last reason to type a bare `python3` command.

**Key decisions:**
- **Two venvs, not the single `/opt/tt-bio-demo/venv` the original design spec
  sketched.** The spec (§2) predates this work and is left as-is since it's a
  historical record, but the two-venv split is a refinement of the same idea,
  not a contradiction of it: the spec's point was "UI environment and tt-bio
  environment must never mix," and splitting the *UI* side into its own venv
  (rather than running it bare against `/usr/bin/python3`) makes that
  reproducible on a fresh box instead of just non-conflicting on this one.
- **`.venvs/` inside the repo, gitignored, as the dev default.** Mirrors the
  production `/opt/tt-bio-demo` layout one level down (`venv-ui`/`venv-runner`
  live directly under the prefix either way), so `--prefix /opt/tt-bio-demo`
  in the later Debian postinst is the same script, same code paths, different
  argument — not a separate dev-vs-prod branch to keep in sync.
- **No `tt-bio install-deps`, ever, from this script.** Installing Tenstorrent
  system packages/kernel modules is a Debian-packaging-phase decision that
  needs explicit consent; a bootstrap script silently doing that on someone's
  box is exactly the kind of surprise this project's `install-deps`-free
  design elsewhere is trying to avoid.

**Notable moment in prompting:** the script's first end-to-end run looked
successful right up until a second, idempotent run silently died — no error,
exit code 120, nothing on stderr. Root cause: `pip show tt-bio | awk
'/^Version:/{print $2; exit}'` inside a `local x=$(...)` assignment. `awk`'s
early `exit` closes its end of the pipe before `pip show` finishes writing the
rest of its output (Summary, Home-page, ...), so `pip` gets `SIGPIPE`,
`pipefail` turns that into a nonzero pipeline status, and `set -e` kills the
whole script on a plain assignment statement with no message at all — the
exact "silent half-built environment" this script's own design principle says
to avoid, sitting in the script meant to enforce that principle. Fixed by
capturing pip's full output into a variable first and parsing it with no live
pipe underneath. Worth remembering: any `cmd | prog_with_early_exit` inside
`set -e -o pipefail` is this bug waiting to happen, not just this one instance
of it.

See [`docs/venv-bootstrap-notes.md`](docs/venv-bootstrap-notes.md) for the
environment split written up for a future reader, including what `tt-bio`
actually pulls in and its measured install size/time on this box.

### 2026-08-12 — Phase 3a built (the compute daemon)

`tt-bio-demod` folds proteins on real Blackhole silicon and streams the live
diffusion trajectory to the GTK app over the existing socket protocol. Verified
across 28 consecutive folds: 5.66 s cold, 4.33–4.42 s with the model resident,
30 frames each, radius of gyration collapsing 4453 Å → 7.0 Å. Kill the daemon
mid-fold and the UI keeps rotating the last structure and reconnects within a
second. 224 tests.

Ten tasks, subagent-driven, each with its own review gate. Preceded by a
hardware spike, because the design assumed things about `dump_fn` nobody had
watched run — and three of those assumptions were wrong.

**The review loop found nineteen defects in the plan's own code**, not in the
implementations of it. Worth internalising rather than reading past: a
log-pinning environment variable that does not exist in this tt-metal build; a
card-state model that could not represent a card that was both busy and hot; a
preflight that crashed instead of reporting on exactly the misconfigurations it
exists to catch; a progress bar that ran backwards on every fold; a daemon that
discarded its injected test double and opened real hardware inside a unit test.

**The whole-branch review then flipped to not-shippable on mutation evidence** —
13 of 15 mutations left the suite green. See the "Write tests that can fail" and
"Short runs cannot see unbounded growth" sections in
[`docs/followups.md`](docs/followups.md); they are the two lessons this phase
actually paid for.

Also from this phase: the project now owns its Python environments
([`scripts/setup-venvs.sh`](scripts/setup-venvs.sh)) including a **vendored SFPI
toolchain**, which avoids downgrading the system-wide one that other Tenstorrent
projects on this box depend on. And a tested patch for upstream tt-bio lives in
[`docs/upstream/protenix-dump-fn/`](docs/upstream/protenix-dump-fn/) — adding the
public `dump_fn` that `OpenDDE.fold` already has and `Protenix.fold` lacks.

### 2026-08-12 — Phase 3b built (the booth: panels, gallery, help, diagnostics)

The UI became a booth rather than a renderer. Ten tasks, subagent-driven with a
review gate each, then a whole-branch review and its fix wave. **634 tests**
(500 UI-side + 134 runner-side; 593 at review time, the rest added by the fix
wave). This file said 224 until now, which is its own small lesson about
documents of record — the same lesson the review found in three other places.

What shipped: the two rail panels (pipeline + per-chip telemetry), the gallery and
its curated playlist, the five-state machine with the showcase dwell, the `?` help
card, the `D` diagnostics log, a live Tensix core-grid animation per chip, and the
"preparing" screen for a daemon that is not ready.

**Key decisions**

- **The showcase dwell, set against a measurement rather than taste.** The daemon
  never pauses — it starts fold N+1 the instant fold N finishes — so a dwell does
  not extend the cycle, it *displaces* live diffusion second for second. Measured
  against the recorded 3.69 s cycle: a 3.0 s dwell leaves 13% of the collapse
  visible, 2.0 s leaves 47%. 2.0 s, and the reasoning is in `ui/app.py` where the
  constant lives.
- **A WebView, for one 430 px panel.** See the amended decision above. Worth
  restating why it was allowed: it is optional, scoped, fail-soft, and already
  proven in `tt-local-generator`. What it is NOT allowed to be is the 3D view.
- **Measured fold times on the cards, or nothing.** Trp-cage **4.4 s** warm;
  FKBP12 **62.6 s**, DHFR **69.1 s**, trypsin **74.9 s** (107/187/223 residues —
  the cost is dominated by the fixed ~200 denoising steps, not by residue count).
  A target nobody has folded here shows "not yet timed", never a guessed number.
- **Unattended for 29 folds, hands-off**, recorded end to end — the thing the
  brief actually asks for.

**Notable moments in prompting**

- The timing pass was blocked by a board-reset fault: every device open failed on
  an ethernet-core error until the boards were reset, and only then did the three
  new targets get real numbers. Until that point the manifest deliberately shipped
  with `expected_s` omitted rather than estimated — the machinery for "not yet
  measured" exists because of exactly this, not as a hypothetical.
- The whole-branch review ran **57 mutations**. 38 of the 47 in the main battery
  went red (3a managed 2 of 15), and the three survivors were each a test asserting
  on something *adjacent* to the behaviour rather than the behaviour — see
  [`docs/followups.md`](docs/followups.md), which now names the pattern.
- The same review's headline finding was not a crash but a **lie**: the turnkey
  launcher advertised four targets over a daemon that could fold one, and every
  visitor-facing string called the gallery a chooser when a pick could not reach
  the daemon at all. Both are fixed by making the copy and the launcher tell the
  truth, not by inventing the capability. That is the standard this project is
  holding itself to, and it is worth re-reading before writing any booth copy.

### 2026-08-13 — the empty viewer: hold the previous structure until superseded

Prompted from a frame-by-frame scan of a 91-second recording of the real booth:
**20 of 45 sampled seconds had an empty 3D view.** A frame at t=44s shows trypsin
at `TRUNK ~60%`, diffusion not started, and nothing on screen but a progress bar.

The cause is a one-line assumption that only ever held for Trp-cage. Only the
`diffusion` stage emits `frame` events — `msa`, `prep` and `trunk` emit progress
and no coordinates — and trunk is ten refinement cycles, ~15s on a 223-residue
target. `job_start` cleared the viewer. So for three of the four shipped targets
(FKBP12 11.7s, DHFR 19.7s, trypsin 22.3s, against Trp-cage's 4.4s) the demo whose
whole premise is *watch it fold* showed a visitor black for most of every fold.
The `_SHOWCASE_DWELL_S = 2.0` hold made it 2 seconds less bad and was itself the
same mistake in miniature: a fixed budget tuned against a 4.4s cycle is under 10%
of a 22.3s one.

**The fix is one sentence: never clear the viewer until there is something to put
in its place.** The clear moved out of `job_start` and into `_drain_frames`, at
the instant the new fold's first real frame is about to be drawn. The hold stops
being a number of seconds and becomes "until superseded", which scales with fold
length by construction. It also collapsed the old `_deferred_clear` machinery —
the deferred clear existed only to postpone a clear that should not have been
happening.

What made it a *booth* change rather than a one-liner was honesty. A rotating
protein with a live stage readout beside it reads as the fold in progress, and
after this change it frequently is not. So:

- Nothing is fabricated. No interpolation, no synthesised motion, no placeholder
  geometry for a stage that produced none — the held thing is a structure that
  was really computed, just an older one.
- It is **dimmed** (`StructureViewer.set_held`, 0.55) and **captioned**
  ("Previous fold: Trp-cage" / "Now folding Trypsin"), so "this one is finished,
  the next is computing" reads at booth distance.
- The caption is an assertion, so it is taken down by anything that makes it
  false: the first real frame, `job_error`, and `not_ready`. A daemon that dies
  mid-fold leaves an honest structure with no claim over it, never a permanent
  "Now folding X".
- Where there is genuinely nothing to hold (the first fold after launch) the
  booth says what it is doing and when the view will fill, rather than showing a
  bare black field.
- An unknown `target_id` degrades to a claims-less caption, never to the raw wire
  id on a conference screen.

Two smaller things fell out of it, both worth remembering:

- **`job_id` on `frame` events is load-bearing now.** The daemon does not pause
  between folds, so a straggler frame from fold N can arrive after fold N+1's
  `job_start`; without the comparison it would retire a finished structure in
  favour of the *older* fold's noise cloud.
- **"Is the cross-fade finished" and "how bright is this" became two different
  questions.** `_draw_ribbon` used to derive its depth-write flag from its alpha,
  which was correct until a fully cross-faded ribbon could be drawn at 0.55 —
  after which the whole 15-second hold would have run with depth writes off,
  resolving the tube's self-overlaps by triangle index order.

Thirteen mutations, thirteen red. Two of them are literally the pre-change code
(the immediate clear and the deferred one); each turns seven tests red, including
all four the brief named.

### DNA renders, and the ramp says what it means (2026-08-13)

Two things, one branch.

**The booth folds DNA now, and you can see it.** The compute side had always
worked — a Dickerson–Drew dodecamer (`CGCGAATTCGCG` paired with itself) folded
in 4.6s warm at mean pLDDT 95.7 — but it drew *nothing*, because
`ui/geometry.py` looked for `CA` atoms and a nucleotide has none. The fix is
`BACKBONE_ANCHORS = ("CA", "P", "C1'")`, tried **per residue**, so a
protein/DNA complex traces its protein chains on C-alpha and its nucleic
chains on phosphorus in one pass. Order is load-bearing in both directions:
CA first, because phosphoserine is protein and contains a `P`; `C1'` last,
because a 5'-terminal nucleotide often has no phosphate and dropping it would
shorten every strand by one. `load_ca_trace`/`CaTrace` were renamed to
`load_backbone_trace`/`BackboneTrace` — the old names had stopped being true.

The lesson worth keeping is about the **fixture**, not the code. A DNA duplex
is two near-identical strands (the sequence is its own reverse complement), so
every count is the same for both and a chain-assignment bug would leave the
obvious assertions green. What actually separates a right duplex from a wrong
one is *where* things are: every vertex of strand A is nearer to strand A's
own phosphates than to strand B's, and nothing at all sits in the 18.8 Å gap
between A's 3' end and B's 5' end (measured: 6.85 Å clearance when the chains
are splined separately, 1.34 Å when they are not). The asymmetric half of the
job went to a hand-written protein+DNA fixture where no two candidate anchors
in a residue share a position *or* a B-factor — so picking the wrong atom
moves the trace **and** recolours it, instead of being invisible.

**A subtle confidence legend**, at the right-hand end of the caption strip
under the render: one line ("Colour: how sure the model is, residue by
residue") over the four ramp bands, low to high, with `less sure` / `more
sure` naming the ends. Generated from `PLDDT_STOPS` like the `?` card's
legend, so neither can drift from the ribbon; the low-to-high order is
`reversed()`, never a second hand-ordered list.

It is placed **beside** the caption rather than under it for a measured
reason: stacked, the strip goes 96px → 124px and the render loses 28px —
the same class of defect, and nearly the same size, as the 32px the rail's
natural width once took out of the hero slot. `test_the_confidence_legend_costs
_the_protein_no_height` measures that A/B at the real hero width against
**every tagline the manifest ships**, because "does the tagline still fit
beside it" is a fact about the copy as much as about the widget. It caught the
first draft of the DNA tagline, which was 135 characters and did not.

### The Tensix panel's flicker, and an easter egg (2026-08-13)

Two things, one branch, both driven by "measure it, do not guess."

**The flicker was the animation, not our loop.** Reported from the booth as
*"when the tensix viz is on there's a lot of flicker in their rendering area."*
The obvious suspect was `ChipVizPanel._tick` — the 1 Hz JS/sysfs poll. It was
not. Rendering the live WebView to a texture on every GTK frame and comparing
pixels (in isolation, then inside the real app, then against a 20-frame
Spectacle burst of the booth itself) showed the panel's pixel statistics are
*identical* with the poll running and with it stopped: no re-layout, no resize,
no blanking, no white flash, whole-panel mean luminance flat to ±0.3%.

What it actually is: tensix-viz's **`idle`** mode randomises every cell once per
*display* frame, and `_drawHeatmap` renormalises the grid to its own per-frame
maximum — so on a 60 Hz panel each 86 px canvas starts ~4 new pops every 17 ms
and each one lands at *full* contrast as a hard 4×7 px near-white dot. Measured
over the four canvases: 4697 pixel-brightenings/second. The three smooth modes
measure 10–30× quieter on the same metric, which is why the panel flickered
**worse at rest than mid-fold** — three of four canvases are `idle` during a
fold and all four are when nothing is folding.

The fix owns the frame budget rather than the vendored library
(`PROVENANCE.md`: do not hand-edit those files): the generated page installs a
`requestAnimationFrame` governor and gets the per-mode budget as data from
Python. `idle` runs at 20 fps; every fold mode stays ungoverned so the diffusion
ring keeps its designed cadence. Verified live — one chip folding renders 60
frames/s while the three resting ones render 16–17. **4697 → 1458
brightenings/s (3.2×).** Stated honestly as a reduction: what is left is
inherent to that mode at a canvas 4× smaller than the library's design size.

**`Ctrl+G` is an easter egg, and it is labelled as one.** `ui/mark.py` defines
the Tenstorrent mark as a signed distance field and runs gradient descent on it,
pulling 6,000 points of Gaussian noise into the logo over six seconds — through
the same `StructureViewer.set_points` the diffusion trajectory uses, because it
is the same noise-becomes-structure motion.

- **The geometry is a construction, not a trace.** The mark is a cube seen
  corner-on, so every vertex lands on an isometric lattice; the shipped vector
  artwork confirms it (five x values 28 apart, eight y values 16.2 apart, ratio
  0.5786 against tan 30° = 0.5774). Rasterising the field against the real
  32×32 artwork scores **IoU 0.996**.
- **The first model was wrong and looked right.** "Three congruent rhombi at
  120°" scores 0.60. The artwork settles it: the three pieces are *not*
  congruent and the mark is *not* three-fold symmetric. Only rasterising caught
  that — which is the same lesson as the DNA duplex fixture, one surface later.
- **Honesty is the hard part, not the maths.** It is a chord (every unbound
  plain key is a visitor touch, so a letter would have stolen one), it is not on
  the `?` card, the heading says "Not a fold" and the body says "not a folded
  structure", the point count in the copy is interpolated from `mark.POINTS` so
  it cannot drift, it closes itself after 60 s idle, and it gets its **own**
  viewer so the fold in flight keeps streaming into the real one — dismissing it
  returns the booth to exactly what it would have been showing. It covers the
  hero slot only, so its own claim ("the rail on the right is still live") is
  something a visitor can check.

Seven mutations for the flicker fix, seven red. Twenty-four for the egg,
twenty-four red, with a deliberate no-op control that correctly survived.

### Recording the quad, and `--quad` (2026-08-14)

Phase 5's headline feature is the 2×2 quad view, and the demo video did not
show it. Three takes to fix, each failing in a way that reported success:

- **Take 0** (169 s, four chips, 55 folds): `xdotool key q` never reached the
  app, so it recorded the solo view. `--window` sends XSendEvent, which GTK
  ignores; the global form uses XTEST, which needs real focus that Spectacle
  steals. A quadrant colour-count then "verified" it *was* the quad — one large
  protein fills all four sampled regions. Only extracting frames and looking at
  them caught it.
- **Take 1** (149 s): genuinely the quad, and unusable — an OBS "Plugin Load
  Error" dialog and the desktop taskbar sat over every frame. OBS's dialog
  steals focus at launch, which un-raises the fullscreen booth.
- **Take 2**: tried to close that dialog with `wmctrl`/`xdotool` and could not.
  OBS is a **Wayland-native Qt app** here, so its windows are invisible to
  XWayland tooling entirely. The new precheck caught the still-dirty screen and
  refused, which is why this take cost three minutes instead of another 149
  seconds of footage.
- **Take 3**: stop fighting the dialog — start **OBS first**, let it throw the
  dialog at an empty desktop, then bring the booth up fullscreen over it and
  trim the dirty head by offset. 150 s, 59 folds across all four chips.

Two things came out of this that outlive the video. `--quad` (and the existing
`--windowed`) make the **start state** a flag rather than a synthetic keypress,
which is both an operator feature — a four-chip booth can run the grid all day —
and the only reliable way to record it. And `scripts/record-demo-video.sh`
photographs the screen after the booth is up and **refuses** unless it is clean,
because every failure above exited 0.

The 30 s loop is cut by measurement rather than by eye: *solidity* — lit pixels
whose neighbours are also lit — separates a diffusion cloud (hundreds of small
discs) from a finished ribbon (a few fat tubes), so ranking 10 s windows by how
much it **rises** finds the moments where dots actually become shapes. It picked
t=43, 72, 104.

**The first cut of that video looked laggy, and the app was not the reason.**
Worth recording because the intuition points the wrong way: measuring exact
duplicate frames (OBS captures at a fixed 60 fps, so a frame the app did not
redraw arrives bit-identical) shows the **quad running at a solid 60 fps, 0%
duplicates**, while the *solo* view sits at 4.5–39.8 fps — up to 92% duplicates,
including one 3.1-second frozen run. Solo spends most of its time holding a
finished structure that is not moving; the quad always has one of four cells
mid-diffusion. Four GL contexts cost nothing visible here.

The lag was entirely in the encode, and both causes were mine: a GIF dropped
from 12.5 to 6.25 fps to hit a size target, and a 1920→1280 downscale. The
downscale was the bigger one — the diffusion cloud is **1–2 px dots**, which do
not survive it, and h.264 smears the remains across moving frames in a way that
reads as stutter rather than softness. Native 1080p at crf 30 measures *better*
than 720p at crf 20 while being 40% smaller. **Spend bytes on pixels, not on
bitrate for pixels already thrown away**; when a GIF must shrink, take it out of
the duration, never the frame rate. Full table in `recordings/README.md`.

Two defects the quad screenshots exposed:

- **FIXED.** A cell in `trunk` shows the **previous** fold's structure (correct,
  and the point of "never a blank screen") but was **labelled with the incoming
  target** — "Dihydrofolate Reductase · TRUNK" under a picture of trypsin. This
  is the mistake `ui/quad.py`'s own module docstring already refuses for the
  notice row, from the other direction: a line that "would label whatever cell 0
  actually IS folding with the wrong protein's name". Solo view has the notice
  row to disambiguate; a quad cell has one line and four cells need four
  different answers, so the line carries both — `Trypsin — now folding
  Dihydrofolate Reductase · TRUNK`, what is DRAWN first because that is what a
  visitor is looking at. Six mutations, six red — but only after the fifth
  (removing the `has_structure` guard) initially **survived**: nothing tested
  that an emptied cell must not claim to be showing something. It is an
  equivalent mutant today (the one clearing path restores `has_structure` in the
  same call, so no observer sees the gap) and is now pinned by a test anyway, so
  the guard cannot be deleted as dead code when a second clearing path appears.
- **OPEN.** On a chip's **first** fold there is no previous structure, so that
  cell is genuinely blank through `trunk`. The quad's notice row carries copy for
  this ("Atoms appear here when the diffusion stage begins") but the notice is
  booth-wide — one line for four independent folds — so it can only ever speak
  for one cell.

## Conventions

- **Keep the README's screenshots current.** The README claims every image on it is the
  live application on real silicon; `scripts/refresh-screenshots.sh` is what keeps that
  true. Run it whenever the UI changes visibly, look at the output, and commit the images
  with the change that caused them. A screenshot that no longer matches the app is worse
  than no screenshot — it is a confident lie on the landing page.
  - **Stills** on this box are **Spectacle only**. `ffmpeg x11grab` records pure black on
    KWin/Wayland (verified: one unique colour in the output frame) and `wf-recorder`
    refuses outright ("compositor doesn't support wlr-screencopy-unstable-v1"). `grim`
    does not work either.
  - **Video** is **OBS + PipeWire** at 1920×1080 @ 60 fps — `scripts/record-demo-video.sh`.
    The portal must be granted interactively once per login session. Spectacle tops out
    at ~5.9 fps, which is why an early demo video looked choppy; it is no longer how the
    video is made.
  - **A capture of one widget does not need the screen at all.** `scripts/record-egg.py`
    records the easter egg by running the real descent on a chip and drawing every frame
    with the booth's own `StructureViewer`, reading the framebuffer back the way
    `make-thumbnails.py` does. No portal, no focus, no keystroke, and `--seed` reproduces
    a run frame for frame — which is why it is the right shape for anything that is *one
    view* rather than the whole booth. It also refuses to encode a capture whose cloud did
    not land on the mark (`mark.py`'s own field is the oracle), because this project's
    capture failures all reported success.
  - **Do not drive the app's keys with `xdotool` in a capture script.** `--window` sends
    XSendEvent, which GTK ignores; the global form uses XTEST, which needs real focus that
    Spectacle steals the moment it runs. Both exit 0 and neither key arrives — this cost a
    169-second recording of the wrong view. Prefer a **flag that sets the start state**
    (`run-demo.sh --quad`, `--windowed`) over a synthetic keypress. `refresh-screenshots.sh`
    still uses xdotool for `T`/`D`, and gets away with it only because it activates the
    window immediately before each press with nothing in between.

- **A capture you have not looked at is not verified.** Every capture failure in this
  project reported success: black x11grab frames (exit 0), a solo recording that a
  quadrant colour-count "confirmed" was the quad (one large protein fills all four sampled
  regions), a 149-second master shot underneath an OBS dialog and the taskbar, a good
  136-second recording declared blank by a verifier that sampled one frame at t=1s. Extract
  frames and look at them. Where a script must decide unattended, make it photograph the
  screen and **refuse** — see `scripts/record-demo-video.sh`.

- Spec-first: brainstormed design → committed spec → implementation plan → code.
- tt-bio is pinned to a **release tag**, never `main` (upstream nightly moves fast) —
  same rule applies to the `TT_BIO_VERSION` pin in `scripts/setup-venvs.sh`.
- Nothing in the UI may ever display a stack trace; every failure path resolves to
  something presentable. See §6 of the spec.
- **Use the project's own venvs, not bare or hand-typed `python3`.** Run
  `scripts/setup-venvs.sh` once to create `.venvs/venv-ui` (GTK/gemmi/OpenGL, via
  `--system-site-packages` off `/usr/bin/python3`) and `.venvs/venv-runner`
  (isolated, `pip install tt-bio`). Run the suite with `scripts/test.sh`, the app
  with `.venvs/venv-ui/bin/python3 -m ui.app`. This retires the earlier
  "always type `/usr/bin/python3` explicitly" rule — see
  [`docs/venv-bootstrap-notes.md`](docs/venv-bootstrap-notes.md) for why bare
  `python3` is still a trap on this box even with the venvs in place.
