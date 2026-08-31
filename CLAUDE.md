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
- **FIXED (2026-08-14).** On a chip's **first** fold there is no previous
  structure, so that cell is genuinely blank through `trunk`. The quad's notice
  row carries copy for this ("Atoms appear here when the diffusion stage
  begins") but the notice is booth-wide — one line for four independent folds —
  so it can only ever speak for one cell. The CELL says it now, gated on
  `awaiting_first_frame`. Worth knowing how the gate got there: the first
  version keyed on the stage being a pre-diffusion one, which left the window
  between `job_start` and the first `stage` event uncovered — and every unit
  test was green, because all of them supplied a stage. A photograph of the
  real quad against a replayed four-chip stream is what found it.

### A guard that was not dead, an RNA, and a video of the logo (2026-08-14)

Four carried-over items plus one interruption, and three of them turned into
the same lesson: **the thing that goes stale is never the code, it is what the
code claims about itself.**

**"Is the gallery put-away guard dead code?" No — and three previous passes
had been asking the question in a place where it could not be answered.**
Each mutated the guard, watched the suite stay green, and concluded something
about the *test*. The guard only does anything when the booth has left
`attract`, and the test that covers it never leaves `attract` — so from there
the state machine's own idle timeout restores attract on the same tick and the
mutation is genuinely equivalent. The reachable path has no visitor in it at
all: the **daemon degrades**, `not_ready` moves the booth to `preparing`
without touching the choreography, and the still-owned gallery's hide arrives
seven seconds later and wipes the "getting the booth ready" overlay. Traced
live (gallery t+67, degrade, hide t+74), now pinned by a test that is red
without the guard.

The same trace found a defect next to it. `_note_input`'s comment said
HIDE_GALLERY "is deliberately NOT applied here" — and four lines below, a
branch applied it. Pressing `D`, `T`, `Q` or `?` at a menu the booth was
showing off dropped it to attract, which is the exact snatch the comment
forbids. **It survived because its test pressed an unbound key**, which falls
through to `_on_touch()` and re-opens the gallery in the same call: the bug put
itself back before anything could observe it. A test can be true for a reason
that has nothing to do with the code it points at.

**A sixth target: yeast tRNA-Phe.** The playlist is now a walk from a gene to
a protein. Measured before any copy was written, which is the point: all four
stems come back correctly Watson-Crick paired (C1'-C1' 10.6–10.7 Å) and it is
as compact as the real molecule (radius of gyration 23.3 Å vs 23–24), **but
the corner sits at ~124° where the crystal is ~90°** — a wide open elbow, not
the famous L. So the blurb says "a compact hinged shape" and never promises the
L. Warm 8.6 s, mean pLDDT 88.6 — *identical on all six folds*, which no protein
here is.

**Adding it falsified copy in five places at once**, all of them some version
of "the only thing this booth folds that is not a protein" — the manifest
tagline, the site's molecule card, a section heading, and the five-targets
counts in the site and the README. Two new tests now catch this class: one
keyed on "the only" rather than the exact sentence, and one that parses the
site's molecule cards and compares name, tagline and time against the manifest
they describe.

**`scripts/record-egg.py` — capture without the screen.** Asked for video of
the logo condensing; there was none, only stills. Screen recording needs the
PipeWire portal granted interactively and `Ctrl+G` cannot be driven by
`xdotool` during a capture (both exit 0 — the trap that cost a 169-second
recording). So: run the real descent on a chip via `runner.egg`, draw every
frame with the booth's own `StructureViewer` configured exactly as `ui/app.py`
configures the egg's, read the framebuffer back. No portal, no focus, no
keystroke, deterministic under `--seed`. It **refuses** to encode a capture
whose cloud did not land on the mark, using `mark.py`'s own field as the
oracle (0.4% of points inside at step 0, 98.8% at step 180 — the 90% floor
sits in a wide gap), then writes a contact sheet and says to look at it.

**And the thumbnails had two homes and one writer.** `playlist/thumbnails/`
ships in the .deb; `docs/thumbnails/` is what the site serves, and the site
cannot reference a file outside `docs/`. Keeping them in step was a manual
`cp` nobody had written down — discovered by having to do it. The script
writes both now, and a test compares them byte for byte.

### tt-bio 0.6.3, and a 585-residue candidate (2026-08-17)

Prompted with "there's a new version of tt-bio out. Let's upgrade and see what
we need to do to support it better!". 0.6.3 was cut the same day.

**The scare was a false alarm, and checking it was still the right first move.**
0.6.3's headline fix is that `--trace` silently returned wrong structures for
every target after the first when one process folds several same-size
Protenix-v2 targets. That is this daemon's exact shape -- one long-lived
process, six targets on repeat -- so the first thing done was to check whether
the booth has been showing wrong molecules. It has not: `runner/folder.py`
folds with `n_step`, `n_sample=1` and `progress_fn`, and never passes
`trace=True`. Worth keeping the habit: the release note that describes your own
architecture deserves a source check before anything else gets touched.

**The upgrade itself is one variable and no dependency churn.** 0.6.3 ships
identical metadata to 0.6.2 -- same `ttnn==0.68.0`, same requires-python -- so
the vendored SFPI 7.35.3 machinery needed no thought at all.

**The live trajectory tap survives, and upstream did not take the patch.**
`edm_sample` still accepts `dump_fn` in 0.6.3, so `runner/dump_tap.py` still
works and `runner/preflight.py`'s `check_tap_supported()` is still the guard
that would catch it breaking. But `Protenix.fold` STILL has no public
`dump_fn`, and `docs/upstream/protenix-dump-fn/`'s patch no longer applies --
`fold` moved and its body changed. What 0.6.3 added instead is
`TT_PROTENIX_DUMP`, an env var that `torch.save`s one `.pt` per step to a
directory: a parity-debugging facility, not a callback, and no substitute for
a live stream. That is evidence *for* the patch rather than against it, and
the README now says so. It was NOT rebased, because its reproducer runs a real
fold on a card and an unverified rebase is worth less than an honest "stale".

**The upgrade falsified booth copy nobody would have thought to check.** The
docs site prints a measured claim tied to the version -- "As of v0.6.2 the
repository holds 2,181 commits, 1,744 of them are his" -- so bumping the pin
without re-measuring would have put a fabricated number on the landing page.
Re-measured at v0.6.3: **3,312 and 2,875**. 1,131 commits in ten days is a big
enough jump to be worth verifying rather than pasting, so it was: every
identity in the range is Moritz's, and the 134 merges are his own branches, no
imported history. Two things fell out of it. The spec's documented method
(`git log --author=moritz | wc -l`) never reproduced its own published number
-- it yields 1742, not 1744, because the shipped figure came from the
case-insensitive form; the spec now says `-i`. And one 0.6.2 mention in
`docs/venv-bootstrap-notes.md` was deliberately NOT bumped: it is a record of
one measured 55s build, and rewriting the version there would attach a timing
to a build nobody has done.

**HSA is a candidate, not an entry.** `playlist/manifest.yaml` has carried
human serum albumin (585 residues) as an explicit exclusion since Phase 3b --
"~29x Trp-cage's residue count with no measured fold time ... worth
reconsidering once a hardware pass has real numbers for it". 0.6.3 is what
makes it worth re-asking: it fixed a Protenix-v2 crash on 385-506 residue
targets and row-blocks the pair track so structure models reach ~1095, so
before this release HSA may simply not have folded here. The input is now
vendored at `examples/hsa_no_msa.yaml` (byte-identical sequence, house-style
header) and is deliberately NOT on the playlist in the same commit that
vendors it, because nobody has folded it on these cards.

**The accept criterion is "is there something to show", not a stopwatch** --
which is Taylor's answer to the pacing question, and a better test than the
time limits offered. It changed what the harness measures.
`tests/integration/test_new_targets_timing.py` now timestamps events as they
arrive (the protocol carries no wall clock, so this is unrecoverable
afterwards) and reports two numbers per target: time to the first `frame`
event, and the longest stretch with no event at all. Only `diffusion` emits
frames, so the first number is exactly the window where the hero slot is
holding the PREVIOUS fold dimmed and captioned. The second is the one that can
fail a target: `_SILENCE_BUDGET_S = 30.0`, on the reasoning that a fold of any
length is fine but a progress bar frozen for half a minute reads as a hung
booth whatever else is rotating on screen. The three shipped targets stay in
the run as the baseline -- a pacing number for 585 residues means little
without the 107/187/223 rows folded beside it on the same card.

The specific thing to look at when it runs: on a chip's FIRST fold after
launch there is no previous structure to hold, so that same pre-diffusion
window is genuinely empty. If HSA leads a cold rotation a visitor could meet a
blank hero for a long time -- and the fix for that is ordering or cell-level
copy, not dropping the target.

**0.6.3 broke one shipped target, and made every other one faster.** Measured
on chip 0, model resident, kernel cache warm, reproduced across three separate
runs that agree within ~2%:

| target | 0.6.2 warm | 0.6.3 warm | 1st coords | max silence |
|---|---|---|---|---|
| Trp-cage | 4.4 s | 4.1 s | 2.1 s | 0.9 s |
| **FKBP12** | **11.7 s** | **FAILS** | — | — |
| DHFR | 19.7 s | 15.7 s | 10.3 s | 1.3 s |
| Trypsin | 22.3 s | 17.5 s | 11.6 s | 1.4 s |
| DNA duplex | 4.6 s | 4.5 s | 1.1 s | 0.2 s |
| tRNA | 8.6 s | 7.3 s | 2.7 s | 0.3 s |
| **HSA (new)** | — | **96.8 s** | 82.3 s | 9.5 s |

Every pLDDT the shipped copy makes a claim about held: Trp-cage 95.27 (was
95.3), trypsin 38.5-39.2 (39.5), DNA 95.71 (95.7), tRNA 88.53 (88.6). So the
gallery blurbs are still true; only the times moved, and they moved down.

**The FKBP12 regression is the ligand, and it is precise.** `examples/
affinity_fkg.yaml` (107 residues + CCD ligand SB3, `msa: empty`) dies in the
MSA track -- `_trunk_cond` -> `_msa` -> `_in_proj_matmul` -- with

    Statically allocated circular buffers in program 909 clash with L1 buffers
    on core range [(x=0,y=0) - (x=10,y=9)]. L1 buffer allocated at 1155072 and
    static circular buffer region ends at 1159680

Deterministic, and not order- or state-dependent: it fails as the first fold of
a fresh process just as it does mid-sequence. Three facts narrow it to a
shape-dependent L1 allocation bug rather than "ligands broke": **the same
protein without SB3 folds fine** (pLDDT 43.52), **DHFR with its own MTX ligand
folds fine** at nearly twice the residue count, and 0.6.2 folded this exact
input 2,143 times. Worth the irony: FKBP12+SB3 is the very complex 0.6.3's
release notes headline for the affinity speedup (294 s -> 206 s), so upstream
exercises it through the Boltz-2 affinity path but evidently not through
protenix-v2 structure folding.

**0.6.3 requires one-chip visibility, and that broke the tests, not the booth.**
`get_device()` now calls `ensure_p300_mesh_descriptor()` (new in 0.6.3, absent
from 0.6.2), forcing `TT_MESH_GRAPH_DESC_PATH` to a **1x1** P300 descriptor --
right for a lone chip, wrong for anything else. The booth already complies
(runner/workers.py pins one chip per worker; the daemon opens no device), but
the integration tests build a `Folder` in-process and inherited a gozer lease's
whole board pair, so every fold died at `control_plane.cpp:1262` with "Physical
chip id 0 not found in control plane chip mapping". Fixed at the root:
`runner.env.single_visible_device()` (4 unit tests), a new `tt_cards_present`
gate so the four-worker test still sees every chip, and `tt_device` narrowing
visibility for every in-process opener.

**A third cold/warm layer nobody had written down.** This project already
distinguishes cold vs warm *model residency*. There is a layer under it: the
persistent JIT kernel cache, which a board reset or a tt-bio upgrade
invalidates. The first harness run after the 0.6.3 install measured DHFR at
25.7 s and HSA at 150.4 s; the second, on the same chip, measured 15.7 s and
96.8 s. Both were "warm" by the old definition. At a venue this is real: reset
the boards in the morning and the first pass through the playlist runs up to
64% slower than the numbers printed on the gallery cards.

**Two harness lessons, both paid for.** A target that fails must not abort the
run -- the first 7-target attempt lost every other target's numbers to FKBP12's
failure, which is exactly how "all five others got faster" stayed hidden for an
hour; the fixture now records a per-target error and keeps folding. And a
diagnostic experiment belongs in the background from the start: running one in
the foreground let a 2-minute tool timeout SIGTERM a fold mid-DMA
(`pin_user_pages_longterm failed: -14`), which wedged a whole p300c board --
`tt_serial` unreadable, `tt-smi -s` failing, and gozer unable to enumerate or
even reset, since its lease grain is the board serial. Recovered with
`sudo <full-path>/tt-smi -r 0000:01:00.0 0000:02:00.0` then `gozer reconcile`.
Note the full path: `sudo tt-smi` is `command not found` (it lives in a user
venv) **and still exits 0**, so the first reset silently did nothing.

Suite green at 1,465 tests with hardware skipped.

### tt-bio 0.7.0: the pin was free, the API was not (2026-08-24)

Prompted with "evaluate the latest tt-bio improvements recently released,
identify which we can adopt in this project and the timeline", then "do the
0.7.0 upgrade and re-measure". Five releases had landed in five days on top of
our 0.6.4 pin -- 0.6.5, 0.6.6, 0.6.7, 0.6.8 and 0.7.0, the last of them the
same day.

**The reason to upgrade was 0.6.7, and it turned out to be invisible here.**
0.6.7 fixed two real accuracy bugs in the Protenix-v2 pair trunk this booth
rides on: the mask marking which residue pairs are real reached only one of the
two triangle multiplications, and `OuterProductMean` added its output bias
without the scale the reference applies. Upstream reports every Protenix-v2
structure leg moving inside the reference's own seed-to-seed spread. On our
seven targets **nothing moved** -- every pLDDT landed in the band 0.6.4
recorded. The likely reason is that all seven are `msa: empty` (or nucleic
acids, which have no `msa:` key at all), so the pair trunk here never carries
the MSA depth those bugs distorted most. Recorded in the manifest header
because the absence is the finding: no gallery blurb needed changing.

**The pin cost nothing; the API cost an afternoon.** 0.7.0 ships the same
`ttnn==0.68.0` and the same requires-python as 0.6.4, so the vendored SFPI
7.35.3 machinery needed no thought (pip 41 s, SFPI hash unchanged). What broke
was `tt_bio.main.hf_artifact`, which 0.6.6 moved into a new `tt_bio.weights`
registry behind the `tt-bio weights` CLI and 0.7.0 shipped without. That is
imported in four places, and the first fold of the measurement run died on it:

  - `runner/folder.py` -- now `weights.fetch("protenix-v2", root=_WEIGHTS_CACHE)`.
    `root=` is passed explicitly so this call and `download_mols()` below it
    cannot disagree about which cache they mean; honouring `TT_BIO_CACHE` is a
    deliberate separate change, because `preflight.py` and `doctor.sh` check
    that path too and would have to move with it.
  - `debian/tt-bio-demo-weights.postinst` -- the turnkey install's weight
    download. **This is the one that mattered**, and this project's own
    `test_the_weights_postinst_uses_the_tt_bio_api_that_actually_exists` is
    exactly why it was found in a test run rather than on a venue floor. That
    test was written after a first draft called `hf_artifact` with the wrong
    arity; it has now done its job twice.
  - `docs/upstream/protenix-dump-fn/reproduce.py` and
    `tests/fixtures/streams/capture_real_fold.py`, both ported so no broken
    import is left lying around.

**Rewriting that contract test taught the usual lesson again.** The new version
checks the replacement API *and* the artifact KEY -- `fetch("protenix-v2")`
fails at install time and only there if that registry row is ever renamed, and
a key is a string no type checker can help with. Two mutations were run.
Renaming the key went red. Aliasing a dead name to the module
(`from tt_bio.main import hf_artifact as weights`) **survived**, because every
remaining assertion only read `weights.` textually -- so an assertion on the
import itself was added, and that mutation now fails too.

**Measured on 0.7.0, same method as the 0.6.4 pass** (three consecutive warm
folds per target on each of two chips, model resident, 42 counted folds, plus a
discarded first pass per chip because a version bump empties the JIT kernel
cache):

| target | 0.6.4 chip 0 | 0.7.0 chip 0 | mean pLDDT |
|---|---|---|---|
| Trp-cage | 4.2 s | 4.2 s | 95.24-95.27 |
| DNA duplex | 4.4 s | 4.4 s | 95.69-95.72 |
| tRNA | 7.1 s | 7.1 s | 88.52-88.57 |
| FKBP12 | 9.6 s | 9.8 s | 48.25-51.91 |
| DHFR | 15.4 s | 15.5 s | 51.52-52.39 |
| Trypsin | 17.3 s | 17.4 s | 38.36-39.73 |
| HSA | 97.1 s | 97.5 s | 80.96-81.14 |

**The second chip is why the README lost a claim.** Chip 1 measured 0.5-1.4 s
slower on every target (DNA 5.8 s against 4.3 s -- +35%, which looks like a
regression and is not). Its per-target *minima* match chip 0 exactly, so chip 1
started level and slowed as the run went on: the documented 906 MHz drift about
fifteen minutes into a session, and chip 1 was measured second, roughly
thirteen minutes in. The 0.6.4 run was short enough to stay on the fast side of
it and recorded "both chips agreed to within 0.2 s". **That sentence is now
gone from the README**, because this run is long enough to have crossed the
drift and restating it would be printing a claim the measurement no longer
supports.

**The upgrade falsified site copy again, in a new place.** Last time it was the
commit counts; this time those *and* the model count. tt-bio gained four model
families since 0.6.3 -- RoseTTAFold3 (0.6.6), Nesso-1 (0.6.8), OpenBind-0 and
PXDesign (0.7.0) -- so "Nine model families" was wrong in a heading, a section
title, the timeline card's prose and a nine-row table. All four moved together
to thirteen. Commits re-measured with the spec's own `-i` method: **4,314 total,
3,877 by Moritz** (was 3,312 / 2,875 at 0.6.3), and every author in the range
verified as his before the number went on the page.

**Adopted, and deliberately not adopted.** Taken: the pin, and the weights-API
port it forced. Skipped with reasons: OpenBind-0 co-folds ligands well and
FKBP12+SB3 is literally its headline parity leg, but its checkpoint is not
downloaded (manual `TT_BIO_OPENBIND`), which breaks the offline-at-the-venue
install principle; RoseTTAFold3, PXDesign and the RFD3 speedups are models a
folding booth does not use. `fold_many` (new in 0.7.0, B targets through ONE
batched diffusion trajectory) is actively wrong for us -- it does not thread
`dump_fn`, so batching would kill the live trajectory the whole demo rests on.
Two things left as scoped follow-ups: `tt-bio weights` / `TT_BIO_CACHE` in the
postinst (needs `preflight.py`, `doctor.sh` and a Debian retest moving
together), and **Nesso-1 affinity**, which at 33 s for a 512 aa complex against
386 s for the Boltz-2 path finally makes "what does the drug do" affordable for
the three protein+ligand targets already on the playlist. That one is a feature
and wants its own spec.

Thumbnails were NOT regenerated: the playlist did not change and every pLDDT
sits in its recorded band, so each thumbnail is still a picture of a real fold
of that target. Worth revisiting if a future release moves coordinates enough
to see at that size.

Verified with the **full hardware suite on all four chips**: 1,122 UI + 405
runner (unit + integration) + the four-worker pool test, **1,528 with hardware
included**, after resetting all four chips through `gozer acquire`/`release`
rather than a hand-run `tt-smi -r`.

One instrument lesson, paid for twice in one session. A first integration run
logged nothing but nanobind leak spam: the script piped pytest through
`tail -25`, and tt-metal's teardown prints thousands of leak lines at exit, so
position-based truncation threw the summary away. **Filter that stream by
content, never by position.** And a `grep -E "^  [a-z]+ +[0-9]+ res"` over the
results quietly dropped FKBP12 from a summary table -- `[a-z]+` does not match a
target id with digits in it -- which briefly read as "FKBP12 did not fold".

### The dwell stopped being one number (2026-08-24)

Prompted, while the booth was running, with "we should linger a little longer
on completed renderings. like 5s at least longer."

**The one-line version of that change is measurably wrong, and the repo said
so.** Raising `_SHOWCASE_DWELL_S` from 2.0 to 7.0 does exactly what was asked
on the four long targets and costs them nothing. On the three short ones it is
fatal, because holding fold N on screen is PAID FOR by suppressing fold N+1's
opening frames -- the daemon starts N+1 the instant N ends, and its first frame
is what supersedes the structure. `test_live_diffusion_is_visible_for_a_
substantial_share_of_each_cycle` replays the recorded real Trp-cage fold and
measured the visible collapse at **0 of 30 frames**, against the 40% floor it
holds. Four other tests went red on fixed clock advances that assumed 2.0s.

That is the same mistake `ui/app.py` already records once -- a fixed budget
tuned to a 4.4s cycle applied to a 22.3s one -- so the fix was not a different
constant. **The dwell is now a maximum, capped per target.**

  * `Target.first_frame_s` (new, optional, measured like `expected_s`) is
    seconds from `job_start` to that target's FIRST `frame` event. Measured on
    0.7.0, one fold per target on chip 0: dna 1.1, trpcage 1.9, trna 2.8,
    fkbp12 5.3, dhfr 10.4, trypsin 11.7, hsa 82.7. It is NOT a fraction of
    `expected_s` and the ratio is not constant -- DNA reaches coordinates in a
    quarter of its fold, albumin in 85% of its -- which is why it is measured
    rather than derived.
  * The cap is keyed on the **incoming** target, not the finished one, because
    what a hold can afford is a property of what is coming next.
  * Clamped into [2.0, 7.0]. Resulting dwells: dna and trpcage 2.0 (the floor,
    i.e. exactly today's behaviour), trna 2.8, fkbp12 5.3, dhfr/trypsin/hsa
    7.0. The requested linger lands in full on the four targets that can pay
    for it and no guard is weakened.

**Three decisions worth keeping.** A dwell is narrowed, never widened -- a hold
already being served must not grow under a visitor looking at it. A new
showcase resets to the maximum, or one Trp-cage in the rotation would pin every
later hold on that chip for the session. And an **unmeasured** incoming target
gets the FLOOR, not the maximum: a long hold is a bet that the incoming fold
can afford to lose its opening frames, and only a measurement settles that.
That default is also what makes the whole mechanism a no-op on a playlist that
measures nothing, which is how it was verified against the existing suite --
the first draft defaulted to the maximum and turned five unrelated tests red.

`effective_dwell_s` is now public on both `StateMachine` and `SlotState`:
`showcase_dwell_s` is the maximum any showcase MAY take, and two tests were
reading it as the number the clock is compared against. Both now read the
effective one, so neither silently stops testing expiry the next time the
maximum moves.

Four mutations, four red, control green: cap removed (5 tests, including the
visibility guard), the per-showcase reset removed, `min` flipped to `max` in
the narrowing, and the unmeasured default flipped to the maximum.

Suite green at 1,467. Confirmed on the running booth: DNA was photographed
mid-diffusion with its noise cloud on screen -- the exact thing a flat 7.0s
dwell suppressed entirely.

### The quad by default, and a cartoon that draws DNA (2026-08-24)

Two things noticed while the booth was actually running, which is where both
of them could only have been noticed.

**"Is the solo view by default? I like 4 chip by default when available."** It
was, and `--quad` was the only way out, so a four-chip booth showed one protein
unless somebody remembered a flag. `quad` is tri-state now: None (auto), True
(`--quad`), False (`--solo`, new). The interesting part is WHERE auto resolves.
The chip count is not knowable at construction -- chips register one at a time
as the daemon names them (`_note_card`; the very first live run of the quad
drew one cell because the card list never arrived at all) -- so it resolves as
the card list GROWS, and two things it must not do are pinned: it must not
decide on one known chip (more may be coming, and a booth that registers
gradually would stay solo forever), and it must not overrule a person, so
pressing `Q` marks the question settled. The constructor default was
`quad=False`, which would have made the whole mechanism dead code; the new
tests caught that on their first run.

**The cartoon was dropping nucleic chains, and its own docstring said not to.**
`ui/cartoon.py` promises that a chain with no C-alphas "is swept as plain round
tube using the anchors `ui.geometry` already chooses for it". The code said
`continue`. Asked whether this needed an upstream fix -- it did not; tt-bio
writes the CIF and this is entirely ours.

Measured, not reasoned about, on a structure assembled from two real folds off
these cards (Trp-cage + a 24-nt duplex, nucleic chains moved +200 A so the
answer is geometry rather than coincidence):

    cartoon built: 1380 verts, x-range -9.5 -> 7.2
    DNA represented in the cartoon?  False

So a **protein+nucleic complex drew the protein and silently omitted the
nucleic acid** -- the worse half, and invisible on today's playlist because
nothing on it mixes the two. The visible half: a pure DNA/tRNA fold had every
chain dropped, so `cartoon_from_cif` raised, and the viewer answered with the
old tube renderer -- the right picture arrived at by exception, logged ERROR
with a traceback on **6 of 17 folds** of a live run.

CA-less chains are now swept as a tube at `NUCLEIC_RADIUS`, chosen with
`_best_anchor_atom` so it is the same per-residue choice `ui.geometry` makes
(CA, then P, then C1'): a nucleic chain anchors on its phosphate, and a ligand
-- which has none of the three -- still yields nothing and is still skipped,
because a ligand swept through its own atoms would be a scribble. 1.6 A is not
a new number: it is `ribbon_from_cif`'s own default, which has drawn DNA and
tRNA here since the tube renderer shipped, so this changes WHICH code draws
them and not how they look. After: **40 real structures build a cartoon, 0
failures**, and a live 22-fold run logged zero.

**Two mutations survived their first pass and are worth remembering**, because
both were the same shape -- a guard whose absence no fixture could see. The
`len(anchors) < 2` check (a one-residue chain cannot be a tube; without it
`tube_mesh` raises and takes the whole cartoon down, reintroducing the original
bug for any structure carrying a stray short chain) and `NUCLEIC_RADIUS`
itself, which no assertion pinned until one measured the drawn surface against
`DIMS[COIL]`. Both now fail.

Also fixed here: four existing quad tests assumed solo-at-startup. The one that
asserted it now asserts it for a ONE-CHIP booth and records why it narrowed;
the three about `Q`, `Ctrl+Q` and `Escape` read the START STATE rather than a
literal `False`, so they keep testing the toggle the next time the default
moves -- which it has now done once.

Suite green at 1,479. Both changes confirmed in one photograph of the running
booth: four cells with no flag, DNA drawn as a tube by the cartoon, DHFR as
ribbons and arrows, FKBP12 with its SB3 ligand as sticks, albumin mid-collapse.

### The weights were never a checked box (2026-08-31)

Reported by a user who got the booth running: *"The docs were not quite right
- I had to discover a model downloading command, the doctor.sh didn't know
about -- but once I found that it works great!"* The ask that followed was
that installing **from .deb or source** should leave every box ticked.

Three separate defects were behind one report, and only the first was the one
being complained about.

**1. The source install never fetched weights at all.** `scripts/setup-venvs.sh`
downloads torch, ttnn and a vendored SFPI toolchain and then stops; a grep for
`weight|boltz|mols|protenix` across it returned nothing. The README's Quick
start was `setup-venvs.sh` then `run-demo.sh`. So a checkout ended at a box
that looked finished and could not fold -- the .deb had covered this since
Phase 3b behind a debconf question, and a git clone had nothing. It fetches
by default now (`--skip-weights` opts out, implied by `--skip-runner`), and a
failed download is deliberately **not** fatal: the venvs are the expensive
half and they are fine, so it warns and prints the command to resume with.

**2. `doctor.sh` named no command.** In source mode the entire hint was "they
download on the first fold, or fetch them ahead of time". That is the sentence
that cost the user their afternoon. Both modes now print
`tt-bio weights --download protenix-v2`; the packaged install still leads with
`dpkg-reconfigure`, because that is where a .deb's consent lives.

**3. The doctor FAILED a booth that could fold.** It required `mols.tar` at
>= 1 GB. tt-bio discards that archive once unpacked -- its own `status()` calls
the archive being gone "harmless for mols once the library is unpacked" -- and
`tt-bio weights --prune` deletes it outright. A fold loads `<cache>/mols`. The
check is layered now: filesystem presence and a size floor always (the only
thing available before venv-runner exists, which is when the doctor matters
most), plus tt-bio's own verifier when it can be asked, which is the only
thing that can tell a complete 1.8 GB file from a truncated one.

**A fourth, found while fixing them: `preflight` never checked `mols` at all.**
`REQUIRED_WEIGHTS = ("protenix-v2.pt",)` -- the checkpoint only -- so a cache
with the checkpoint and no molecules printed `preflight: ok` and then died on
the first fold. It is derived from tt-bio's registry now
(`weights.artifacts_for("protenix-v2")`), so a release adding a third artifact
is picked up instead of surfacing as a fold on a machine preflight called
ready.

**Four callers, four different answers, and none of them right.** The cleanup
Taylor asked for on top: `runner/folder.py` hardcoded `Path.home()/".boltz"`,
`run-demo.sh` read `$TT_BIO_DEMO_WEIGHTS`, `doctor.sh` and the postinst read
`$BOLTZ_CACHE` -- and **none read `$TT_BIO_CACHE`**, the variable tt-bio
prefers and documents as relocating the whole cache. The postinst re-derived
it a second time in its python half, so a host using `$TT_BIO_CACHE` would
have downloaded 3.7 GB into a directory neither a fold nor the doctor would
look in. There are two derivations now, one per language
(`runner/env.py:weights_cache`, `scripts/weights-cache.sh`), pinned to each
other behaviourally and both pinned to tt-bio's own `cache_root()`.
`folder.py`'s own comment had said honouring `TT_BIO_CACHE` was "a deliberate,
separate change" needing preflight and the doctor to move with it; they moved.

**What actually prevents a recurrence** is not the fix, it is that printed
commands are now checked against the tt-bio they are printed for.
`test_the_weights_postinst_uses_the_tt_bio_api_that_actually_exists` has caught
a real break twice by parsing the pinned tt-bio's own source; the same idea now
covers every `tt-bio weights` invocation in the README, INSTALL.md, doctor.sh,
setup-venvs.sh and the postinst -- real subcommand, real flags, real artifact
key, and the same model the booth actually folds. Mutations confirm all three:
a fake model, a fake flag, and a REAL model that is the wrong one (that last is
caught only by the booth-agreement test). Plus a textual drift guard that lets
exactly two files in the repo spell `.boltz` or read the cache variables.

**Three instrument lessons, all paid for in this session.**

- **Sourcing a script runs it.** The first draft of
  `tests/unit/test_setup_venvs_weights.py` sourced `setup-venvs.sh` before the
  `SETUP_VENVS_LIB_ONLY` guard existed, which pip-installed torch into five
  pytest tmp directories -- **30 GB, on a box already at 100% disk**. The guard
  is not a nicety and its comment says so.
- **A fixture that needs a gigabyte is measuring the wrong thing.** The doctor
  tests wrote real 1.1 GB and 1.9 GB files to clear the size floor: 15 GB of
  /tmp across three runs, and 1.9 s per run. The check reads `stat -c %s`, so a
  **sparse** file exercises it exactly -- 0.09 s and 376 K.
- **`tail -3` on a mutation run hid a kill.** Reading a truncated mutation
  summary said one mutation had survived when three tests had actually gone
  red. This repo's own rule ("never conclude from a truncated view") applied to
  the instrument checking the tests.

One mutation genuinely survived and is worth keeping. Deleting the
`--skip-weights` case from the argument parser left the whole file green: the
flag then fell through to the `*)` catch-all, which exits 1, so the sourced
shell died **before `fetch_weights` was defined** -- an empty download log for
a reason with nothing to do with skipping. The test asserts the flag is
*accepted* now, separately from what it does.

Suite green at **1,522** with hardware skipped (1,165 UI + 357 runner), plus
the 60 packaging tests including the real Docker container installs, and one
real Trp-cage fold on chip 0 under a gozer lease -- `folder.py`'s `load()`
path has no unit coverage, so that fold is the only real proof the cache
change works.

**Cut as 0.5.5, and the bump found one more instance of the same bug.**
`test_the_handout_pdf_was_rebuilt_for_this_version` couples a version bump to
re-rendering the printed booth handout, so `docs/onepager/build.sh` ran -- and
looking at the result (the build script says to, and this project's own rule
is that an artifact nobody looked at is not verified) turned up page 2's
BEFORE DOORS OPEN checklist:

    Weights on disk. The venue is offline -- nothing downloads at the
    booth.  dpkg -l tt-bio-demo-weights

`dpkg -l` says a PACKAGE is installed. It does not say the weights are on
disk, unpacked, or intact -- which is the exact distinction this whole change
is about -- and it means nothing at all on a source install. On the one
artefact that gets printed and handed to a stranger. It now points at
`doctor.sh`, which answers the question actually being asked.

**And the drift guard caught its own author.** The 0.5.5 changelog stanza
describes the fix, so it contains the string `$TT_BIO_CACHE` -- and
`test_nothing_outside_the_resolvers_reads_the_cache_variables` failed on it.
Correct behaviour from a guard whose scope was wrong: `debian/changelog` has
no file extension, so the prose filter never saw it. Debian metadata is
excluded by EXACT PATH rather than by a `debian/` prefix, because the
maintainer scripts in that directory are real code and one of them is a
caller this test exists for -- with a further test pinning that the exclusion
list can never grow to cover a maintainer script. Verified: adding the
weights postinst to it turns that test red.

**There is no Docker install path, and INSTALL.md now says so.**
`scripts/deb-container.sh` is package-install testing only and refuses to pass
`--device` under any circumstance, so nothing in a container can reach a chip.
Better to state that than to let the question keep being asked.


### What the review found, and the shape it kept finding it in (2026-08-31)

`/code-review` on the branch above returned nine findings. All nine were real
-- verified individually before anything was changed, which is the right order
and also how the one piece of mis-framing surfaced.

**Seven of them are one mistake wearing different clothes: a check that knows
LESS than the thing it is checking.** That is the same mistake the branch was
written to fix, reintroduced by the fix.

* `preflight` and `doctor.sh` built paths from `Artifact.dest()`. tt-bio lets
  an operator relocate ONE artifact (`$PROTENIX_CKPT` / `$TT_BIO_PROTENIX_V2`,
  `$TT_BIO_MOLS`) and only `weights.resolve()` honours that. So a host where
  every fold works was told its checkpoint was missing -- and since preflight
  runs ONCE and the daemon never retries it, the booth sits on "preparing"
  forever. Exactly the false-alarm class the branch removed, one layer down.
* The doctor's layer 2 could only ESCALATE -- it set failures and cleared
  none -- so the comment saying "its verdict wins where it has one" was false
  in the only direction that matters. tt-bio's verdict is now authoritative
  and the filesystem layer is the fallback, not a co-signer.
* **A guard I added blocked a state the fold used to repair itself.**
  Requiring the EXTRACTED `mols/` looked obviously right; `Folder.load()`
  calls `download_mols(cache)`, which unpacks the archive on the spot. So a
  cache holding `mols.tar` and no directory used to start and fix itself in
  twenty seconds, and now the daemon never started. One of my own tests
  asserted the wrong behaviour outright; it is replaced by the corrected
  expectation with the reasoning left in place, because the wrong version was
  plausible enough to write once. The doctor still reports that state -- a
  pre-venue check and a start-or-refuse gate should not answer this the same
  way.
* `setup-venvs.sh` printed one cache and fetched into another: the summary
  used the resolver, the fetch passed no `--cache` and let tt-bio re-derive.
  They agree only while `$HOME` does, and the documented invocation is `sudo
  scripts/setup-venvs.sh`. Two derivations again, in the change whose whole
  point was to have one.
* The shell resolver's `${HOME:-/root}` did not match `Path.home()`, which
  consults passwd. **My own cross-language test could not see it, because
  every row in its matrix set HOME.** A row that does not, now exists.
* `debian/helpers.sh` recursed 1000 frames deep whenever the sourced resolver
  was readable but defined nothing -- an empty file from a partially unpacked
  package, or an upstream rename. Inside a `configure` script that breaks a
  whole dpkg run. The sourced file defines a DIFFERENT name now
  (`..._impl`), and its presence is what the wrapper checks before trusting
  the file. My comment had called the self-redefinition "deliberate", which
  it was; what it was not was safe.
* And the new function was inserted between an existing comment and the
  function that comment documents.

**One finding was right about the defect and wrong about the cause, which is
worth separating.** `--weights` reaches preflight and not the fold -- but
`folder.py` hardcoded `Path.home()/".boltz"` BEFORE this branch too, so the
flag never reached a fold in the first place. Not a regression; a pre-existing
hole the branch made newly worth fixing, since there is now one place to fix
it. What WAS new was my comment claiming the agreement outright. The daemon
now pins `--weights` into the worker environment (`runner_environ(...,
weights_dir=)`), using `BOLTZ_CACHE` rather than `TT_BIO_CACHE` because the
flag means the flat artifact cache and does not claim to move the Hugging
Face hub cache.

**A mutation survived on the fix for that one, and the reason is the lesson.**
`test_an_operators_own_cache_variable_is_not_overwritten` put `TT_BIO_CACHE`
in the base and compared RESOLVED paths -- but the pin writes `BOLTZ_CACHE`,
which `TT_BIO_CACHE` outranks, so making the pin overwrite unconditionally
changed nothing the test looked at. **A test for "this value is preserved"
has to read the variable that gets written, not the one that wins.** It
asserts on the raw `BOLTZ_CACHE` now, plus that no lower-priority answer is
smuggled in beside an operator's own.

Six other mutations, six red, control green. Suite **1,530** with hardware
skipped, 64 packaging tests including the real Docker installs, and the real
Trp-cage fold re-run on chip 0 under a lease after `folder.py` and
`daemon.py` changed -- the load path still has no unit coverage, so that fold
remains the only proof.


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
