# Multi-chip folding — design spec

**Status:** proposed, 2026-08-13
**Motivation:** the booth has four Blackhole chips and folds on one.

## The problem

`runner/daemon.py:117` builds `CardPool([config.device_id])` with `device_id: int = 0`. One chip folds; the other three idle at 800 MHz. Two consequences, one of them a correctness problem:

- **The booth undersells the hardware.** A visitor sees "4 chips on 2 boards" in the telemetry panel and one protein appearing. The most impressive thing in the room — four accelerators — is decoration.
- **The Tensix panel had to be walked back to stay honest.** It once claimed "four chips and they are all working"; that was a Critical finding in the Phase 3b whole-branch review, and the fix was to show only the folding chip. The panel is now truthful *because* it says less. Making four chips genuinely work is what earns the claim back.

## The constraint that shapes everything

**tt-bio's device model is one worker process per chip.** `tt_bio/tenstorrent.py:451` — `get_device()` always opens *logical* device 0, and its docstring is explicit: "Worker processes set `TT_VISIBLE_DEVICES` before importing ttnn, so the assigned physical chip appears as logical device 0."

That environment variable must be set **before ttnn is imported**, which in our daemon happens during preflight — long before any per-job device choice could be made. This is exactly why `--device` was deleted from the daemon rather than fixed (`runner/daemon.py:80-100`): the flag was accepted but inert, so `CardPool` tracked one index while the fold ran on whatever `get_device()` actually opened, silently decoupling the thermal guard from the hardware doing the work.

**So multi-chip cannot be threads in one process. It is one subprocess per chip.** tt-bio does this itself (`tt_bio/esmc.py:1208`, `tt_bio/saprot.py:561` spawn fresh interpreters with `TT_VISIBLE_DEVICES=<device>`), and `tt_bio/main.py:958` `_build_worker_device_assignments` is the reference implementation.

Two details from that function apply directly to this box:

- **Our chips are p300c, and a lone P300 is a custom topology.** `_detect_p300_devices()` exists because P300 chips are exposed one at a time, and each such worker also needs a 1×1 Blackhole mesh graph descriptor — `p150_mesh_graph_descriptor.textproto`, via `TT_MESH_GRAPH_DESC_PATH`. Confirmed present in our runner venv at `ttnn/tt_metal/fabric/mesh_graph_descriptors/`. Getting this wrong is the most likely cause of a worker that opens a device and then behaves strangely.
- `tt_bio/device_lease.py` provides `DeviceLease` and `DeviceInUseError` — a file-based lease so two processes cannot claim the same chip. Prefer it over inventing our own mutual exclusion.

## Feasibility

Measured on this box: one resident folding process is **7.2 GB RSS**. Four would be ~29 GB against 249 GB total (111 GB in use, 137 GB available). Comfortable.

Cold start is the real cost: 5.7 s cold versus 4.35–4.45 s warm for Trp-cage, so each worker pays a one-time model load. Four workers starting together at boot is a slower first fold, not a slower steady state.

## What the visitor sees — the design decision

Four concurrent folds change the UI's central assumption: `ui/viewer.py` renders **one** structure.

| Option | What it looks like | Verdict |
|---|---|---|
| **A. Quad view** | 2×2 grid, four proteins condensing at once, each labelled with its chip | **Chosen.** It is the honest picture of the machine, and four proteins folding simultaneously is a far stronger booth image than one. It also makes the telemetry panel and the Tensix grid true rather than decorative. |
| B. Hero + previews | One large protein, three small | Keeps the current hero framing but wastes the point; the small ones are unreadable at booth distance. |
| C. Pipelined pre-compute | One visible fold; other chips pre-compute upcoming targets | Better throughput, invisible to a visitor. Optimises the thing nobody can see. |

**Chosen: A.** With the caveat that the gallery's single-pick flow still selects one target — a visitor's pick becomes the *hero* of the quad, and the other three chips continue the attract playlist.

## Architecture

```
 daemon (parent, no device)
   ├── JobQueue                          existing
   ├── CardPool([0,1,2,3])               existing, already multi-index
   ├── worker[chip 0..3]  subprocess     NEW: TT_VISIBLE_DEVICES=<n>,
   │     └── Folder + resident model          TT_MESH_GRAPH_DESC_PATH=<p150 MGD>
   └── event multiplexer                 NEW: tag every event with its chip,
         └── existing socket server            forward to the UI unchanged
```

The wire protocol **does not change**: `job_start` already carries `card`, and every downstream event carries `job_id`. The UI already knows which chip a job is on — `ui/app.py` logs "on chip N" and the Tensix panel already highlights `set_folding_chip`. What changes is that several jobs are now in flight at once, so the UI must key its viewers by `job_id` rather than assuming one.

## Risks

- **A worker dies mid-fold.** The parent must notice, mark the chip idle, requeue or abandon the job, and keep the other three folding. A dead worker must never take the booth down — the current single-process design fails closed, four workers must fail partially.
- **Thermal.** Four chips folding continuously will run hotter than one. `CardPool`'s 85 °C quarantine already exists and already handles busy-and-hot; this is the first time it will actually fire in anger. Watch it during the soak.
- **Log growth ×4.** tt-metal's output is per-process. The existing `--log-budget-gb` sweep assumes one writer; verify it still bounds four. This project has been bitten once by a log that grew 13–14 MB/s into an unlinked file.
- **Startup time.** Four cold model loads; preflight must not report ready before at least one worker can fold.
- **The quad view is a real renderer change**, not a layout tweak. It is the largest piece of work here.

## Out of scope

- Multi-chip *within* one fold (tt-bio can fan a single prediction across cards). Different problem, different win: it makes one fold faster rather than four folds concurrent. Worth measuring later — for a 22 s trypsin fold it might matter more than concurrency.
- Multi-host.
