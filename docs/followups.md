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

## Closed on 2026-08-14 — kept so a reader knows they are closed

Eleven entries below were fixed in one pass. Each is struck from the list it
used to sit in; what follows is what closed it, so a reader who remembers the
open version can check the reasoning still holds.

- **The UI logged one line per `stage` event** (was under Task 19). One line
  per stage TRANSITION now, per slot, reset on `job_start`. 8.0 MB/h → bounded
  by folds × stages. `ui/app.py`'s `stage` branch.
- **A permanent device-scan failure wrote a traceback per retry** (was under
  Phase 3a). First failure logs in full; repeats are one line with a counter;
  a *different* failure prints in full again. `runner/daemon.py:_build_pool`.
- **`prune_log_root` gave no signal when the protected floor exceeded the
  budget** (was under Phase 3a). It warns now — and the guard first written on
  that warning turned out to restate a condition that is always true where it
  sits, which `elif True:` proved by leaving the suite green. It is an
  unconditional `else` with the reasoning written down.
- **Stopping by process group logged one ERROR and a traceback** (was under
  Task 19). A negative return code is "killed by signal", not a failure
  `tt-smi` reported; one INFO line. A sample that fails while the booth is
  RUNNING still gets its traceback. `runner/cards.py:sample_tt_smi`.
- **tt-metal's two watcher files were not in `_prune_logs`'s protect set**
  (was under Task 19, recorded as unreachable rather than fixed). The sweep
  knows about them now — a better guarantee than "we measured 0.00 MB/h once".
  `runner/env.py:_is_held_open_by_tt_metal`.
- **The version guard stopped the send but not the promise** (was under Task
  18). `_on_pick` returns before touching the router when the connection is
  `incompatible`, so a refused booth no longer raises "NEXT UP: …" over its
  own "getting the booth ready" overlay.
- **`kill -INT` on `run-demo.sh`'s pid alone does nothing** (was under Task
  18). Now a README troubleshooting entry, with the `-$(ps -o pgid=)` form.
- **No guard against GApplication double-activation** (was under "Worth
  doing"). Re-activation presents the existing window and returns.
- **The join/socket timeout relationship was prose** (was under "Worth
  doing"). `SOCKET_TIMEOUT_S` is the one number; both joins are derived from
  it, and a test pins the relationship *and* the absence of a fresh literal.
- **`MockRunner.stop()` joined only the accept-loop thread** (was under "Worth
  doing"). It joins the per-connection `_serve` threads too. The test for it
  could not fail at first — a 200 ms replay gap let the thread finish on its
  own — and says so.
- **`LatestFrame.dropped` was never surfaced** (was under "Worth doing"), and
  the reason it never was is that it counted two opposite things: ordinary
  latest-wins supersession (thousands per run, meaning nothing) and whole-job
  eviction (meaning the renderer fell behind). Split into `dropped` and
  `evicted`, reported at shutdown, eviction at WARNING. **Still open:** a
  LIVE indicator, which needs a rate and a threshold nobody has measured.

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

## From Phase 5 Task 19 — the two-hour four-chip soak

The booth, hands off, on four chips for **two hours**: `run-demo.sh
--windowed --all-targets`, the real UI attached, sampled every 60 s (121
sweeps), plus a second passive socket client recording every non-`frame`
event. **2143 folds, zero `job_error`s, zero worker deaths, zero respawns,
zero client drops, and exactly one non-INFO line in the daemon's whole log —
which was a truncation this task caused on purpose.** Raw numbers and the
sampler/analysis scripts are in
`.superpowers/sdd/2026-08-13-multi-chip-folding/task-19-report.md`.

**Two hours rather than the one the brief asked for**, because one hour
yields a single delta and cannot tell "bounded" from "growing slowly"; two
lets the second hour be checked against the first. That comparison is the
result, so it is worth stating plainly: hour 2 looked like hour 1 in every
resource measured.

- **Nothing grew. Here is what would have shown it if it had.** The log root
  was **1,830,212 bytes in all 121 sweeps — byte-for-byte identical**, 0.09 %
  of the 2 GB budget, and `du -sb` agreed with a `log_root_size`-style walk
  exactly every time. Every `worker.log` sat at **103 bytes** (0.0002 % of
  `WORKER_LOG_CAP_BYTES`) for the whole run. Total RSS went 19.33 → 19.35 GB
  (per worker 4.28–4.64 GB, drifting in both directions); **open fd counts
  never moved at all** — 20 per worker, 19 for the daemon, 37 for the UI, in
  all 121 sweeps.
  The Phase 3a trap was checked the way that finding demands, from
  `/proc/<pid>/fd` (a `readlink` ending `" (deleted)"`, then `stat` on the fd
  itself) and cross-checked against `lsof` every sweep. **Bytes held in
  deleted-but-open files by the booth: zero.** The only such fds any booth
  process ever held were four `/dev/shm/open_mpi.0000` at 0 bytes and two UI
  `memfd`s totalling 4 KB. tt-metal's Inspector is off, and nothing replaced
  it.
  The reason the log root is flat is worth knowing: tt-metal writes
  `generated/watcher/kernel_names.txt` and `kernel_elf_paths.txt` once at
  device bring-up (563,859 and 1,265,941 bytes here) and never appends again
  for a fixed playlist. **Those two files are held open for write for the
  life of every worker and were NOT in `_prune_logs`'s `protect` set**, so if
  something ever did make the log root exceed its budget, an oldest-first
  sweep would unlink them and free nothing — the Phase 3a failure exactly. At
  a measured 0.00 MB/h that is unreachable, which is why it was recorded here
  rather than fixed. Anything that makes tt-metal chattier (a growing
  playlist, `TT_METAL_WATCHER`, Inspector back on) puts it back in range.
  **CLOSED 2026-08-14:** the sweep skips them by name *and* parent directory
  (`runner/env.py`'s `_is_held_open_by_tt_metal`), because "the sweep knows
  not to bother" is a stronger guarantee than "we measured 0.00 MB/h once" —
  and the measurement above is exactly the kind that stops being true when
  the playlist grows, which it did the same day, twice.

- **The per-card tt-metal log tree had never once existed in the shipped
  daemon.** `worker_environ` set `TT_METAL_LOGS_PATH` with `setdefault`, and
  `runner/daemon.py:main` runs `os.environ.update(runner_environ(args.log_root))`
  before it spawns anything — so the key was always already present and the
  `setdefault` was unconditionally a no-op. Read straight off the live booth:
  all four workers had `TT_METAL_LOGS_PATH=<log_root>` in
  `/proc/<pid>/environ`, and `lsof` showed all four holding **the same two
  inodes** (NODE 1139/1140) open for write under one shared
  `<log_root>/generated/watcher/`.
  **Three tests asserted the per-card split and all three were green**, because
  each removes the ambient variable before looking — `base={}` in
  `test_worker_specs.py` and `test_janitors_four_up.py`, `monkeypatch.delenv`
  in `test_worker_pool.py` and `tests/integration/test_four_workers.py`. Every
  one of those deletions carries a comment explaining that leaving the variable
  in place would collapse the four trees into one. They were describing
  production and calling it a test artifact — the sharpest instance yet of the
  "write tests that can fail" pattern at the bottom of this file, because here
  the tests *named the bug* and then arranged not to meet it.
  Fixed: `TT_METAL_LOGS_PATH` is a plain assignment now, joining
  `TT_VISIBLE_DEVICES` for the same stated reason (a per-worker fact the caller
  has already decided must not lose to an ambient leftover). The integration
  fixture now **sets** an ambient shared root rather than deleting it, and
  asserts on the env each child was really spawned with plus the absence of a
  `generated/` tree in that shared root. Red-then-green on hardware:
  `AssertionError: four workers were launched with 1 tt-metal log root(s), not
  4`. The fix costs about 5.5 MB — four copies of the watcher files instead of
  one.

- **Two of the four chips throttle, and it costs a fifth of their speed on
  long folds.** The largest operational finding of the soak, and it is
  invisible in any short run. Chips **0 and 2 held 1293–1350 MHz across all
  121 sweeps**. Chips **1 and 3 dropped as low as 906 MHz** mid-fold, with
  power *falling* as they did (62–68 W throttled against 88–100 W at full
  clock), starting around 12–15 minutes in and continuing for the rest of the
  run. Those two chips also run 3–4 °C hotter than the other two — at idle as
  well as under load — which points at chassis position, not workload.

  | target | chips 0 / 2 (p50) | chips 1 / 3 (p50) | manifest `expected_s` |
  |---|---|---|---|
  | trpcage | 4.49 / 4.51 s | 4.51 / 4.51 s | 4.4 |
  | dna | 4.74 / 4.75 s | 4.74 / 4.77 s | 4.6 |
  | fkbp12 | 11.71 / 11.69 s | **12.25 / 12.18 s** | 11.7 |
  | dhfr | 19.76 / 19.66 s | **24.98 / 24.28 s** | 19.7 |
  | trypsin | 22.57 / 22.46 s | **27.14 / 27.10 s** | 22.3 |

  **Short targets are untouched** — 4.4 s and 4.7 s folds finish before the
  chip heats — and the long ones lose 4–26 %. 27.2 % and 30.2 % of folds on
  chips 1 and 3 exceeded 1.15× their target's median; **0.0 % and 0.0 %** on
  chips 0 and 2, across 565 and 583 folds. Consequences worth carrying
  forward:
  - **Four chips buy 3.70×, not 4×.** 2155 folds against 2332 (4× the best
    chip's 583) = 92.4 %, and the whole shortfall is these two chips.
  - **Host CPU contention is ruled out**, which the brief asked to check
    first: `OMP_NUM_THREADS`/`MKL_NUM_THREADS`/`OPENBLAS_NUM_THREADS` all read
    `4` in `/proc/<pid>/environ` for every worker, the four workers together
    used ~4.8 of 16 cores, and chips 0 and 2 never lost a megahertz on the same
    host at the same instant. The clock drop is device-side.
  - **`playlist/manifest.yaml`'s `expected_s` are cold-chip numbers.** They
    match chips 0 and 2 to within 0.1 s and are 4–26 % optimistic for the
    other two after twenty minutes of booth time. Not changed here: they are
    honest for half the box, and a gallery card promising a range is a copy
    decision, not a plumbing one. Worth deciding before a venue.

- **The 85 °C quarantine did not fire, and on this box it probably cannot.**
  Peak across 484 chip-samples: **83.6 °C** (chip 1), then 82.8, 79.8, 78.1 —
  1.4 °C of margin, hotter than Task 18's 78.2 °C but still short. The reason
  matters more than the number: **the silicon throttles first**. Chips 1 and 3
  shed clock at ~80 °C and stay there, so the ASIC temperature plateaus below
  the daemon's threshold instead of climbing through it. Temperature was flat
  from about fifteen minutes in — hour 2's per-chip means were only +0.7 to
  +2.0 °C above hour 1's. `CardPool`'s quarantine is therefore still
  unexercised in anger after two hours of maximum sustained load, and the
  honest reading is that it is a backstop against a *cooling failure*, not
  something normal booth operation will reach. Its unit tests remain the only
  evidence it works.

- **The two janitors do work — but the soak could not show it, so it was made
  to.** Neither budget binds inside two hours, and "we ran for two hours and
  the janitor never had to act" is not evidence the janitor acts. Both were
  therefore provoked on the *live* booth after the soak window closed:
  - **`WORKER_LOG_CAP_BYTES`.** 73.4 MB appended to `card-0/worker.log` (which
    the daemon holds open `O_APPEND`). 25 s later: size 0, **`st_blocks` 0**,
    and the tmpfs's used bytes back to **exactly** the pre-pad figure
    (4,767,744 both times) — the blocks really came back, which is the entire
    difference between `truncate` and `unlink`. The daemon logged it, and card 0
    kept folding on the same pid.
  - **`--structures-budget-gb`.** `device-0` padded from 99.0 MB to 220.7 MB.
    26 s later: 99.3 MB, `card 0: structures pruned: 1 file(s), 121.6 MB
    freed`, and all 1424 real `.cif` files still there (the padding was given
    an old mtime, so oldest-first ate it and nothing else).
  The structures budget **will** bind at a venue, unlike the log budget: each
  card accumulated **26–30 MB/h** (52–60 MB per card over the two hours), so a
  card starting empty reaches 200 MB after roughly **7 hours** of continuous
  folding — inside a conference day. That path had never run outside a unit
  test before today.

- **The UI never wobbled, and here is the measurement that would have caught
  it if it had.** Zero disconnects and zero reconnects over two hours, on both
  the real UI and the passive second client; the daemon logged one client drop
  in the whole session (the tap's own restart, before the window opened). The
  tap received 64,328 frames, 536/min. `docs/followups.md` warns that a GLib
  source can freeze silently and that nothing signals it — so the UI's CPU time
  was sampled every minute: **246–295 ticks per minute, mean 272, and not one
  minute at zero** across 121 minutes, with its thread count pinned at 39.
  A frozen main loop is a flat line there, and there is no flat line.
  Caveat worth stating: the desktop session was **locked** for the run
  (`LockedHint=yes`), so this proves the socket, the GLib main loop and the
  event handlers stayed live; it does not prove the compositor kept compositing
  a visible window. Screenshots could not be taken — the KDE screenshot portal
  returned "Did not receive a reply" with the session locked.

- **A booth at full tilt still lets a chip go idle for a moment.** Task 18
  concluded "at a running booth there is never a free chip". At 1 Hz sampling
  over two hours that is true of chips 0 and 2 (**0** sweeps at 800 MHz) but not
  of the throttled pair: chip 1 was caught idle in 12 sweeps and chip 3 in 20.
  With five targets on four chips a finishing chip sometimes finds every
  remaining target already in flight and waits for `DISPATCH_POLL_S`. The
  practical effect is nil — it is sub-second — but "never" was too strong.

- **CLOSED 2026-08-14** (see the closed section above). *The UI logs one line per
  `stage` event, at INFO, into whatever captures its stdout.* `ui/app.py:3709`. Measured over this run: **17.0 MB / 488,048 lines
  in 2h07m = 8.0 MB/h**, or ~64 MB across an eight-hour day, growing with the
  number of chips. `run-demo.sh` leaves the UI's stdout on the terminal so an
  operator never sees this, but a booth run under systemd or `nohup` puts it in
  the journal or a file, and **no budget in this codebase covers it** — the
  same gap as the `daemon.log` entry under "From Phase 3a" below, on the health
  path rather than the failure path. For comparison, `daemon.log` itself grew
  at ~90 KB/h (2278 lines in 2h07m). One line at DEBUG, or one per stage
  *transition* instead of per stage event, would remove it.

- **CLOSED 2026-08-14** (see above). *Stopping the booth by process group logs one
  ERROR and a traceback, every time.* `kill -INT -<pgid>` — the method this file recommends, and what a
  terminal Ctrl-C does — also signals the `tt-smi` child the daemon's telemetry
  thread has in flight, so `runner/cards.py`'s `sample_tt_smi` catches
  `CalledProcessError: ... died with <Signals.SIGINT: 2>` and writes
  `ERROR runner.cards: tt-smi sample failed; treating as no telemetry` plus a
  full traceback. The handling is correct — one unreadable sample must not
  blind the scheduler — but an operator reading the log after a clean shutdown
  finds an ERROR and a traceback at the bottom of a run that went perfectly.
  Cheap to silence: treat a negative return code (killed by signal) during
  shutdown as expected.

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

- **CLOSED 2026-08-14** (see above). *The version guard stops the send but not the
  promise.* Verified by running
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

- **CLOSED 2026-08-14** (now a README troubleshooting entry). *`kill -INT` on
  `run-demo.sh`'s pid alone does nothing.* The launcher's `INT`
  trap cannot run until its foreground command (the UI) returns, and signalling
  only bash leaves the UI untouched — the booth just keeps folding. A terminal
  Ctrl-C works because the tty signals the whole foreground process *group*. This
  is normal job control and not a bug in the script, but it costs an operator (or
  an automation harness) several confused minutes: to stop the booth without a
  terminal, signal the group — `kill -INT -$(ps -o pgid= -p <pid>)`. Worth a line
  in the README's troubleshooting section if the booth is ever run under a
  supervisor that stops it by pid.

## From Phase 3a — worth doing, not urgent

- **CLOSED 2026-08-14** (see above). *A permanent device-scan failure writes a full
  traceback per retry.* At
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
- **CLOSED 2026-08-14** (see above). *`prune_log_root` gives no signal when the
  protected floor exceeds the budget.* It returns `removed=[]`, and the daemon's `if removed:` gate means
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

- **CLOSED 2026-08-14** (see above). *No guard against GApplication double-activation.* Relaunching while running
  re-activates via D-Bus: a second window, a second socket client, a second 33 ms
  frame timer, `self.viewer` rebound and the first window orphaned. Plausible at a
  booth if staff relaunch without realizing it is already running. ~3 lines.
- **CLOSED 2026-08-14, in part** (see above; a live indicator is still open).
  *`LatestFrame.dropped` is never surfaced.* For an all-day unattended run this
  is the cheapest available signal that the renderer is falling behind.
- **Connection-state vocabulary is duplicated across a module boundary.**
  `ui/viewer.py` hardcodes `_CONNECTION_STATES`, which `ui/client.py` actually
  owns. This is why `_on_state` needs a guard whose only job is stopping the
  viewer's validator from bricking the state channel. Hoist the constant into
  `client.py` and the guard becomes a formality.
- **`connection_state` is write-only.** Nothing renders it yet; Phase 3 owns the
  UI that consumes it. It also arguably belongs on the app rather than on a
  rendering widget.
- **CLOSED 2026-08-14** (see above). *`MockRunner.stop()` joins only the accept-loop
  thread*, not the
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
- **CLOSED 2026-08-14** (see above). *The join/socket timeout relationship is prose,
  not code.* `ui/client.py`
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

**Task 19's soak is the live evidence `WORKER_LOG_CAP_BYTES` was chosen
without.** 64 MB/card was a judgement call; two hours of four-chip folding put
every `worker.log` at **103 bytes**, so the cap is roughly six orders of
magnitude above what a healthy worker writes and the "a worker that reaches
64 MB is a worker in a repeating-error loop" reasoning holds with enormous
margin. The mechanism was also exercised for real on the running booth rather
than only in the unit test: 73.4 MB appended to a live `worker.log`, truncated
25 s later, `st_blocks` to 0 and the tmpfs's used-byte count back to exactly
where it started. See the Task 19 section above. And the same soak found the
*third* instance of this trap — two tt-metal watcher files that four workers
hold open and the pruner is not told to protect — which is recorded there as
unreachable at the measured growth rate rather than fixed.
