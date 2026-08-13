# Known follow-ups

Findings from the Phase 1–2 and 3a builds that were deliberately not fixed at
the time. Recorded here because the review scratch space they came from is not
version controlled. Each names why it was deferred, so a future reader can judge
whether the reasoning still holds.

## Fixed in Phase 3b — kept here so a reader knows they are closed

This file is read first (CLAUDE.md says so), so an item that has been fixed and
left sitting under a "blocks" heading is worse than no entry at all. These three
were closed by the Phase 3b branch:

- **The UI's `not_ready` branch** (was "blocks Phase 3b"). `ui/app.py` renders
  the "preparing" overlay, with operator-neutral copy and the `missing` detail
  going to the log only. Task 1; see `_PREPARING_MESSAGE`.
- **`ribbon_from_cif` on the GTK main loop** (was "blocks Phase 3"). It now runs
  on a background worker with a generation stamp, applied to the viewer from the
  main loop via `_drain_pending_ribbon`. Task 2; see `ui/app.py`'s "ribbon
  construction off the main loop" section and `tests/unit/test_ribbon_async.py`.
- **Multi-chain structures splined as one continuous tube** (was "blocks Phase
  3"). `ribbon_from_cif` splines per chain via `_chain_runs`, so a complex no
  longer gets a spurious leg joining one chain's C-terminus to the next chain's
  N-terminus. Task 2; see `ui/geometry.py`.

## Closed in Phase 5 (multi-chip folding) — kept so a reader knows they are closed

- **`--device` was removed rather than plumbed** (was under "From Phase 3a",
  below). The entry said `get_device()` cannot select a card, that threading
  `TT_VISIBLE_DEVICES` through was unverified hardware risk, and that the
  daemon was card-0 only. All three have stopped being true, so this moved
  here rather than being left where a reader would act on it. **Closed by
  Task 8** (`runner/daemon.py`): the daemon holds no device at all now — each
  chip gets its own worker subprocess handed a complete environment, including
  its own `TT_VISIBLE_DEVICES`, via `Popen(env=...)` before the child
  interpreter starts. The flag is back, spelled `--devices 0,1,2,3` the way
  tt-bio's own CLI spells it, and it now selects real hardware rather than
  moving a thermal counter around. See `DaemonConfig.device_ids`'s comment for
  the full before/after, `runner/workers.py` for the pinning, and the hardware
  spike (chip 1 at 33.0 W mid-fold against 13–17 W idle on 0/2/3) for the
  evidence that the pinning is real.

## From Phase 3a — worth doing, not urgent

- **A permanent device-scan failure writes a full traceback per retry.** At
  `DEVICE_SCAN_RETRY_S` (5 s) that is roughly 25 MB/day into `daemon.log`,
  which `--log-budget-gb` does **not** cover (that governs the tt-metal log
  root only). Bounded by nothing today. Was written against `Folder.load()`,
  whose retry loop this replaced in Task 8 (`Daemon._build_pool`); the
  arithmetic and the gap are unchanged.
- **A client connecting during the not-ready window never runs the
  protocol-version check** for that connection's lifetime, because it receives
  `not_ready` instead of `hello`. Later connections are fine — `EventServer`
  calls the hello factory per accept.
- **`Folder.fold()`'s tt-bio contact surface is five symbols**, four of them
  underscore-private in `tt_bio/main.py`. An upgrade has five places to check
  rather than one. There is no public entry point that fits a resident
  device-and-model, so this is inherent rather than an oversight.
- **`prune_log_root` gives no signal when the protected floor exceeds the
  budget.** It returns `removed=[]`, and the daemon's `if removed:` gate means
  an operator who sets `--structures-budget-gb` below three files' worth sees
  the same log output as a healthy run. Growth is bounded, but the metric cannot
  distinguish healthy from misconfigured — the same shape as the Inspector
  finding below, at far lower severity.
- **`cleanup()` raising during `load()`'s rollback is logged and swallowed**
  while the state is zeroed anyway, so `Folder` can report clean while the ttnn
  device and its lease are still held. Bounded by tt-bio's own `atexit` handler
  and the flock lease dying with the process.

## From Phase 1–2 — measurements worth keeping

**`ribbon_from_cif`'s cost, measured on the QB2 dev box: 78 ms at 150 residues,
163 ms at 400, 407 ms at 1000, 1221 ms at 3000.** `catmull_rom` and `tube_mesh`
are Python double loops. This is why the build was moved off the GTK main loop
(see "Fixed in Phase 3b" above) rather than left inline in the `job_done`
handler, and it is the number the daemon's `PROTECTED_STRUCTURE_COUNT` is sized
against — the UI can be more than a second behind the socket in reading a
`.cif` it has been told about.

## Worth doing, not urgent

- **No guard against GApplication double-activation.** Relaunching while running
  re-activates via D-Bus: a second window, a second socket client, a second 33 ms
  frame timer, `self.viewer` rebound and the first window orphaned. Plausible at a
  booth if staff relaunch without realizing it is already running. ~3 lines.
- **`LatestFrame.dropped` is never surfaced.** For an all-day unattended run this
  is the cheapest available signal that the renderer is falling behind.
- **Connection-state vocabulary is duplicated across a module boundary.**
  `ui/viewer.py` hardcodes `_CONNECTION_STATES`, which `ui/client.py` actually
  owns. This is why `_on_state` needs a guard whose only job is stopping the
  viewer's validator from bricking the state channel. Hoist the constant into
  `client.py` and the guard becomes a formality.
- **`connection_state` is write-only.** Nothing renders it yet; Phase 3 owns the
  UI that consumes it. It also arguably belongs on the app rather than on a
  rendering widget.
- **`MockRunner.stop()` joins only the accept-loop thread**, not the
  per-connection `_serve` threads, so it promises more teardown than it delivers.
  Harmless in tests (which replay at `speed=100`), potentially a lingering daemon
  thread at `speed=1.0`.
- **Untested paths:** two simultaneous mock-runner clients (the module docstring
  claims each gets the full stream); a client disconnecting mid-replay, which
  runs on every app shutdown; anything in `ui/app.py` beyond `_handle_event`.
- **`tube_mesh` self-intersects on tight curvature.** When the tube radius exceeds
  the local radius of curvature, some faces flip (measured: 77% correctly oriented
  at radius 1.0 on a synthetic tight curve; 100% at radius 0.05). Not reachable at
  the 1.6 default on real backbones with 3.8 Å C-alpha spacing — but backface
  culling is now on globally, so if it ever is reached it shows as holes rather
  than as shading artifacts.
- **The join/socket timeout relationship is prose, not code.** `ui/client.py`
  needs its 6.0 s join to exceed the 5.0 s socket read timeout; an isolated edit
  to either silently reintroduces the bug that pairing was written to fix.
- **Point size is an absolute pixel value**, so sprites look proportionally
  smaller on a display much denser than the 1280×800 dev default. Scale by
  `get_scale_factor()` in Phase 3, when the booth display is known.

## Deliberately not doing

- **`plddt_colors` uses `>=` at every stop**, so exactly 90.0 lands in the
  very-high band rather than the confident one. This matches the AlphaFold
  convention and boundary values are measure-zero on real pLDDT. If anything is
  wrong here it is the spec's prose (">90 / 70–90 / …"), not the code.
- **`unpack_coords` catches broad `Exception` around `b64decode`**, which raises
  several types depending on input. The broad catch is correct.

## Gotchas worth knowing before touching this code

**Use the project's `venv-ui`, never bare `python3`.** A personal Tenstorrent
virtualenv is active on `$PATH` on the QB2 dev box (a different CPython *build*
from the system interpreter — uv-managed 3.12.12 vs. apt's 3.12.3 — and it has
numpy but not gemmi, PyGObject or PyOpenGL, because distro packages install only
for the system interpreter). Tests run under it pass by accident on the
numpy-only modules and fail the moment gemmi or GTK is involved. This cost a
task to discover, and for a while the fix was "remember to type
`/usr/bin/python3` explicitly." `scripts/setup-venvs.sh` retired that: it builds
`.venvs/venv-ui` from `/usr/bin/python3` with `--system-site-packages`, so it
inherits the apt bindings and there is nothing left to remember — run
`scripts/test.sh` (or `.venvs/venv-ui/bin/python3 -m ui.app` directly) instead of
either bare `python3` or a hand-typed `/usr/bin/python3`. The trap the venv
exists to route around is still real, just no longer the thing you have to hold
in your head: see [`docs/venv-bootstrap-notes.md`](venv-bootstrap-notes.md).

**GDK may negotiate a GLES context by default.** Observed on KWin/Wayland with
radeonsi: GDK hands back GLES 3.2, which rejects the desktop `#version 330 core`
shaders outright even though the driver exposes GL 4.6 core. `StructureViewer`
pins desktop GL via `set_allowed_apis`, guarded because that API is a newer GTK4
addition. This is compositor- and driver-dependent and should be re-checked on
real QB2 hardware.

**An unhandled exception inside a GLib callback does not crash — it silently
freezes that source forever.** Verified on this stack. That is worse than a crash
for an unattended demo, because nothing signals it: the display simply stops
updating. Every GLib-invoked callback in `ui/app.py` is guarded with the `return`
outside the `try` for this reason. Preserve that shape.

**Write tests that can fail.** Four of the nine bugs found during this build
survived a passing suite because the tests could not distinguish the right answer
from the wrong one: an axis-aligned `look_at` case (the rotation block reduces to
the identity, hiding a row/column transposition), an orthonormality assertion
(invariant under the row permutation it was meant to catch), a mesh suite blind to
triangle winding, and a ribbon test that passed whether or not colors aligned to
the correct residues. When a test's input is symmetric, axis-aligned, or otherwise
degenerate, ask what wrong answer it would still accept.

Phase 3a made this concrete. Its whole-branch review mutation-tested the suite:
**13 of 15 mutations aimed at supposedly-covered behavior left it fully green.**
Deleting `folder.load()`, deleting `server.start()`, replacing the fold call with
`pass`, changing the protocol version to 999, swapping the log-containment
constant back to a name that does not exist on this build — all green. The code
was largely right; the evidence that it was right was far thinner than 210 passing
tests suggested.

**If a fix matters, mutate it and watch the test go red.** That is now the
standard here. The wave that fixed the above still introduced one new test that
could not fail, caught only because a reviewer invented a mutation nobody had
asked for. This is a bias to keep checking for, not a bug you fix once.

Phase 3b's whole-branch review ran 57 mutations; 38 of the 47 in the main
battery went red, which is a real improvement on 3a's 2-of-15 — and the three
that survived were all the same shape as before:

- a **constant** delay in the ribbon-worker fixture, which made "newest
  generation wins" indistinguishable from "last writer wins" (deleting the
  generation counter entirely left 459 tests green);
- `thread_alive` reading `self._thread is not None and ...`, so the assertion
  was satisfied by the `self._thread = None` line alone and deleting
  `stop()`'s join changed nothing;
- a legibility guard that checked a hardcoded list of class NAMES against a
  stylesheet, which no widget edit can affect — building a label with no CSS
  class at all stayed green.

Note what those three have in common: each asserted on something ADJACENT to
the behaviour (a fixture's symmetry, a field's nullity, a stylesheet's text)
rather than on the behaviour itself. That is the pattern to look for.

**Short runs cannot see unbounded growth.** Two separate tasks "verified log
containment" with two-fold sessions. A 28-fold run found tt-metal's Inspector
holds its log file open and keeps writing ~13–14 MB/s *after* the file is
unlinked — invisible to a `rglob`-based size walk, which reported the budget
healthy while space was still consumed. The default log root is tmpfs, so an
unattended booth would have exhausted 24.9 GB of RAM in roughly half an hour.
Inspector is now off by default (`TT_METAL_INSPECTOR=1` re-enables it, at the
cost of `tt-triage` functionality). When a property is about *bounds*, verify it
over a duration long enough to distinguish bounded from unbounded — and check
`lsof`, not just the directory.
