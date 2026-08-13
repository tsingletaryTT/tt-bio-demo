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

## Conventions

- **Keep the README's screenshots current.** The README claims every image on it is the
  live application on real silicon; `scripts/refresh-screenshots.sh` is what keeps that
  true. Run it whenever the UI changes visibly, look at the output, and commit the images
  with the change that caused them. A screenshot that no longer matches the app is worse
  than no screenshot — it is a confident lie on the landing page.
  - Capture on this box is **Spectacle only**. `ffmpeg x11grab` records pure black on
    KWin/Wayland (verified: one unique colour in the output frame) and `wf-recorder`
    refuses outright ("compositor doesn't support wlr-screencopy-unstable-v1"). `grim`
    does not work either. Keys are driven with `xdotool`, which needs the window
    activated first or they land in whatever terminal has focus.
  - Spectacle sustains ~5.9 fps, which is also why the demo video is assembled from
    stills rather than recorded.

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
