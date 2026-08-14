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
  the full before/after and `runner/workers.py` for the pinning. **The evidence
  cited here used to be the spike's "chip 1 at 33.0 W mid-fold against 13–17 W
  idle on 0/2/3"; that has been retracted — see "The spike's power evidence for
  chip pinning does not hold" below.** The pinning is real, and what proves it is
  `tests/integration/test_four_workers.py` plus `tt_bio.device_lease`'s flock.

## From Phase 5 Task 18 — the first hardware run of the four-chip booth

Everything below is measured on this QB2 (4× p300c), not reasoned about. The
raw numbers live in `.superpowers/sdd/2026-08-13-multi-chip-folding/task-18-report.md`.

- **A process that has opened a device cannot spawn a child that opens one.**
  The sharpest finding of the task, and the one with teeth outside the test
  suite. Once a process calls `tt_bio.tenstorrent.get_device()`, any child it
  spawns afterwards deadlocks inside `ttnn.open_device` — parked in
  `futex_do_wait` in UMD's cross-process bring-up path, which is exactly the
  hang `tt_bio.tenstorrent._device_init_lock`'s own docstring was written to
  prevent — and never returns, *while holding that host-wide init flock*, so
  every other worker queues behind it too. It happens even though the parent's
  `cleanup()` returned cleanly and the parent holds no `/dev/tenstorrent` fd.
  Measured, deterministic, both directions:

  | parent | child `runner.worker` becomes ready |
  |---|---|
  | never opened a device | **3.5 s** |
  | opened + `cleanup()`d one first | **never — still not ready at 120 s** |

  **Production is already safe, by accident of a decision made for another
  reason.** `runner/daemon.py` holds no device at all and deliberately does not
  import `Folder` (see its import comment) — so the process that spawns workers
  has never opened a chip. That decision is now load-bearing for a second
  reason nobody knew about: adding an in-process device open to the daemon
  would not merely waste a lease, it would make **every worker respawn deadlock
  for the rest of the session**, and Task 7's whole recovery story with it.
  Anything that ever wants to open a device in the daemon has to fork the
  worker pool first, or not open it at all.
  What it cost here: `pytest tests/integration` as one process wedged four
  workers on all four cards, because `test_egg_on_device.py` and
  `test_real_fold.py` open a device in the pytest process and
  `test_four_workers.py` spawns children. `scripts/test.sh` now runs the
  child-spawning test as its own pytest invocation, and
  `_forbid_a_poisoned_process` in that file fails in two seconds with the
  explanation if anyone ever runs it the other way.
  **Recovery, since `tt-smi -r` is off the table:** SIGKILL the wedged workers.
  Verified — the kernel drops the init flock and the per-card lease on process
  death, all four chips returned to 800 MHz, and no reset was needed.

- **The spike's power evidence for chip pinning does not hold. Use `aiclk`, not
  watts.** The Phase 5 spike concluded that a worker pinned to chip 1 really ran
  on chip 1 because `tt-smi` showed **33.0 W on chip 1 against 13–17 W idle on
  0/2/3**. Task 18 sampled this box's *idle* telemetry 80 chip-samples deep with
  nothing running at all: the idle band is **12–33 W**, and 4 of those 80 samples
  read over 30 W — including two at 33.0 W and one at 35.0 W, on chips doing
  nothing. A single 33 W reading is therefore indistinguishable from idle, and the
  spike's inference was luck, not evidence.
  What *does* separate the two, with no overlap at all:

  | | idle (80 samples) | four-way folding (408 chip-samples) |
  |---|---|---|
  | power | 12–33 W | 34–199 W (per-chip medians 79–89 W) |
  | **aiclk** | **800 MHz, every sample** | **1281–1350 MHz, every sample** |
  | temperature | 45–49 °C | 54–77 °C |

  `aiclk` is the honest discriminator: idle is pinned at 800 MHz and a chip with
  work on it never once dropped below 1281 MHz. Any future "is this chip actually
  working" check should read the clock, and should read a *series* rather than one
  sample. (Power is still the right thing to show a visitor — it is the number
  that means something to them — but it must not be load-bearing for a claim.)

- **A visitor's tap most often lands on a target that is already folding.** With
  the shipped five-target playlist on four chips, four of the five are in flight
  at any instant, so most picks hit a target already folding: the daemon logs
  `pick '<id>' is already folding; queueing nothing` and the cell folding it takes
  the focus. Measured across two live sessions: **15 of 24 picks**. This is correct
  behaviour and it is what Task 9 and Task 17 designed for — recorded because it
  means the *queued*-pick path is the minority case at a real booth, and anyone
  reading only the queue code will have the frequencies backwards.

- **At a running booth there is never a free chip, so every queued pick is a
  "busy" pick.** `Daemon.run` re-enqueues the whole playlist the instant the queue
  empties (`if len(self.queue) == 0: self._enqueue_playlist()`), so all four chips
  are permanently occupied from the first second. The only window in which a pick
  meets a free chip is the sub-second one during start-up, before the attract loop
  saturates. Measured there: **0.500 s** from pick to dispatch — and even that was
  bounded by the *next worker finishing its model load*, not by anything queue-
  related. Busy-case picks measured **0.250 s – 5.503 s** (8 samples; median
  ≈ 2.0 s), pick to dispatch, which brackets Task 17's 1.75–3.25 s on both sides
  and stays nowhere near the twenty seconds the Task 18 brief asked us to watch
  for. The tap-to-daemon hop itself is 3–24 ms (measured twice against the
  wall-clock of the `xdotool` click), so "tap to dispatch" and "pick to dispatch"
  are the same number to three decimal places.
  **The ceiling is the shortest fold in flight, not the average.** The shipped
  playlist contains Trp-cage (4.4 s) and the DNA duplex (4.6 s), so something frees
  up constantly. A playlist made only of the 60–75 s targets would push the busy
  case toward those numbers, and the visitor-facing copy ("starting on the next
  free chip") would then be describing a much longer wait. Worth re-measuring
  before shipping a playlist without a short target in it.

- **The version guard stops the send but not the promise.** Verified by running
  the real UI against a daemon whose `PROTOCOL_VERSION` was 999: the UI logs the
  mismatch exactly once, goes `incompatible`, never reconnects, shows the neutral
  "Preparing / Getting the booth ready" overlay, and sends **zero** picks (the
  daemon's log confirms it received none). All of that is right.
  What is wrong is that a tap *also* still raises the quad notice —
  **"NEXT UP: FKBP12 — starting on the next free chip"** — on top of a booth that
  has just decided it cannot talk to this daemon. `ui/app.py`'s `_on_pick` calls
  `self.router.select_target(...)` *before* `_send_pick`, and `_send_pick`'s
  `incompatible` branch returns `False` without ever unwinding that selection, so
  `SlotRouter.pick_status` reports `queued` for a fold that was never requested.
  The visitor reads a promise the booth has already refused to make, next to copy
  saying the booth is not ready. Two contradictory sentences on one screen.
  Not fixed in Task 18: it is a `ui/app.py` change and this was a hardware task
  whose file scope was `run-demo.sh`, the README and the new hardware test. The
  fix is small — have `_on_pick` roll the selection back (or not make it) when
  `_send_pick` returns `False` for `incompatible` — but it needs its own test, and
  the honest version of that test has to drive `_on_pick` with the connection
  state set, not drive the router directly (a `SlotRouter`-only test could not
  fail against this bug, since the router is behaving exactly as asked).
  Screenshot: the mismatch capture in the Task 18 report.

- **`kill -INT` on `run-demo.sh`'s pid alone does nothing.** The launcher's `INT`
  trap cannot run until its foreground command (the UI) returns, and signalling
  only bash leaves the UI untouched — the booth just keeps folding. A terminal
  Ctrl-C works because the tty signals the whole foreground process *group*. This
  is normal job control and not a bug in the script, but it costs an operator (or
  an automation harness) several confused minutes: to stop the booth without a
  terminal, signal the group — `kill -INT -$(ps -o pgid= -p <pid>)`. Worth a line
  in the README's troubleshooting section if the booth is ever run under a
  supervisor that stops it by pid.

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

**And multi-chip folding reintroduced the same trap by a different route.**
The parent process now holds four `worker.log` files open — one per chip, each
handed to a `Popen` as that child's stdout and stderr for the life of the
worker (`runner/pool.py`) — so `prune_log_root`'s oldest-first `unlink` would
once again take a *name* and free nothing. Task 11 closed it by telling the
sweep those paths are not its to delete (`protect=`) and bounding them with
`os.truncate` instead, which is the one operation that returns the blocks while
an fd is open, and which is safe here only because the files were opened
`O_APPEND`. `WORKER_LOG_CAP_BYTES` (64 MB, `runner/pool.py`) is the bound. Note
that the *tests* for this cannot demonstrate unbounded growth either — they are
too short, exactly as the two-fold sessions were — so
`test_truncation_frees_the_blocks_a_held_open_writer_is_using` holds a real fd
across the sweep and asserts on `st_nlink`, `st_ino` and `st_blocks` seen
*through that fd*. That is the `lsof` check above, written as a unit test: it is
the only assertion in the file that can tell `unlink` from `truncate` at all.
