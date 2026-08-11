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

- **Native GTK4 throughout, no WebKit.** The original viability sketch assumed a browser
  panel running Mol* for the 3D view. Taylor pushed back on that, and the native path turned
  out better: one scene graph, one visual language, and the protein is a real widget
  alongside the telemetry rather than an iframe next to it.
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

## Conventions

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
