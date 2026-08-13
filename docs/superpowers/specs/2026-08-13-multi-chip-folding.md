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

## Verified by spike, 2026-08-13

Run before writing any plan, because a wrong answer here invalidates the design. Script: one subprocess per chip, `TT_VISIBLE_DEVICES` + the p300 MGD set before import, each folding Trp-cage through our own `runner.folder.Folder`.

**Q1 — does a worker pinned to a non-zero chip fold?** Yes. Chip 1 alone: pLDDT 95.3, 30 frames, load 3.13 s, fold 7.42 s, peak RSS 4.05 GB.

**Q2 — does it use the chip we asked for, or silently chip 0?** It really uses the requested chip. Mid-fold telemetry with only chip 1 working: **chip 1 at 33.0 W, chips 0/2/3 at 13–17 W idle.** This was the failure most worth ruling out — the daemon's own comment describes exactly this silent decoupling.

**Q3 — can four fold concurrently?** Yes. All four succeeded, each pLDDT 95.3 and 30 frames, folds 6.6–7.7 s, peak RSS 4.04 GB each (~16 GB total, not the 29 GB estimated below). Model load stretched from 3.1 s solo to 6.4–9.2 s under four-way contention — the cost is paid at startup, not per fold.

*Caveat on the spike's own evidence:* the four-way run sampled `tt-smi` at a fixed 25 s, by which time load+fold had already completed, so those wattages show idle and prove nothing about concurrency. The proof of concurrency is that four workers each returned a correct structure; the power evidence stands only for Q2.

## Use tt-bio's own worker machinery, do not reinvent it

The user asked whether tt-bio already documents running all four chips on a QB2. **It does**, and the design should follow it rather than parallel it:

> "Prediction uses up to one card per pending target, labelled in the display (`quietbox:tt0`, `quietbox:tt1`, …). Models load once per active card and stay resident."
> "Pass `--devices 0,1,2,3` to pick or limit the available cards. **A single target remains a single-card fold; additional cards increase throughput only when multiple targets are queued.**"

Two consequences:

1. **That last sentence settles the "out of scope" question below.** Fanning one fold across four chips is not merely out of scope for us — `predict` does not offer it. Four chips buy *throughput on queued targets*, which is exactly the quad view. There is no faster-single-fold option to weigh against it.
2. **`tt_bio.runtime` is importable without importing ttnn** — verified — which matters enormously, because device assignment must happen before that import. Reuse:
   - `detect_tenstorrent_devices(device_ids, num_devices, max_workers)` — validates a requested id against `/dev/tenstorrent` and errors clearly on a typo instead of failing later with an opaque device-open error. Returns `[0, 1, 2, 3]` here.
   - `build_local_workers(...)` → `WorkerSlot`s, ids `tsingletaryTT-quietbox:tt:0..3` on this box.
   - `tt_bio.main._build_worker_device_assignments(...)` — the p300 MGD handling.

   **Correction (2026-08-13), found while planning:** this section originally implied all three are reachable without importing ttnn. Only `tt_bio.runtime` is. Verified: importing `tt_bio.runtime` leaves both `ttnn` and `torch` unimported; importing `tt_bio.main` pulls in **both** at module scope. The three functions are themselves ttnn-free — `_detect_p300_devices` reads `/sys/class/tenstorrent`, `_find_ttnn_mesh_graph_descriptor` uses `importlib.util.find_spec` — but you cannot reach them without the import.

   The design survives, for a better reason than the original one: importing ttnn *opens no device*, the parent already imports tt-bio during preflight's tap check, and the parent hands each child a complete environment via `Popen(env=...)` — so the child's variables are set **before its interpreter starts**, which is strictly stronger than "set before the import".

**We still cannot simply shell out to `tt-bio predict`.** It returns finished structures; the booth's entire premise is the live per-denoising-step trajectory, which comes from the `dump_fn` tap on the Python API. So: tt-bio's *worker/device machinery*, our own fold loop and event stream on top.

## Feasibility

Superseded by the spike above, which measured rather than estimated: **4.04 GB peak RSS per worker, ~16 GB for four**, against 249 GB total (111 GB in use, 137 GB available). The earlier 7.2 GB figure was the whole daemon process under a different workload, and estimating from it overstated the cost by nearly 2x.

Cold start is the real cost, and four-way contention makes it worse than a naive multiple: model load went from 3.1 s solo to 6.4–9.2 s with four workers loading together. That is paid once at startup, not per fold — but preflight must not report ready before at least one worker can actually fold.

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

- Multi-chip *within* one fold. **Not available**: tt-bio's own documentation states a single target remains a single-card fold, and extra cards raise throughput only across queued targets. Nothing to measure.
- Multi-host.
