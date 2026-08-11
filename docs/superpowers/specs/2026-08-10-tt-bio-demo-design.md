# tt-bio-demo — design

**Date:** 2026-08-10
**Status:** approved (design); implementation plan not yet written
**Target hardware:** Tenstorrent QB2 (4× p300c Blackhole), Ubuntu 24.04, Wayland

---

## 1. What this is

A turnkey, visually striking demo of [tt-bio](https://github.com/moritztng/tt-bio) —
protein structure prediction on Tenstorrent silicon — designed to run all day at a
conference booth on a freshly imaged QB2.

It is a **native GTK4 desktop application**. No web browser, no embedded WebKit, no
Electron. The 3D protein, the live hardware telemetry, and the fold pipeline all render
as parts of one GTK scene graph, so the demo reads as a single integrated system rather
than a web page bolted onto a terminal.

It is installed with `apt install tt-bio-demo-all`, which brings tt-bio itself, the model
weights (including the OpenFold3 checkpoint), the curated content, and the application.

### Success criteria

1. A fresh QB2 goes from imaged to running demo with one `apt install` and no manual steps
   beyond answering a debconf prompt for the OpenFold3 checkpoint.
2. The demo runs unattended for a full conference day with no crash, no leak, no black
   screen, and no stack trace visible on the display.
3. It functions with **no network connectivity** at the venue.
4. A passer-by with no domain knowledge understands, within about ten seconds, that a
   protein is being computed on the hardware in front of them.

### Non-goals

- Not a general-purpose molecular viewer. It renders what the demo needs, well.
- Not a tt-bio fork. It consumes tt-bio as a pinned released dependency.
- Not a benchmarking tool. Numbers shown are honest observed wall-clock, not gate results.
- Not kiosk-boot infrastructure. It is a desktop application (see §7 for the rationale).

---

## 2. Architecture

Two processes, in two different Python environments, connected by a Unix domain socket.

```
┌── tt-bio-demod ───────────────────┐             ┌── tt-bio-demo (UI) ────────────────┐
│  /opt/tt-bio-demo/venv            │   JSONL     │  system python3 + python3-gi       │
│  (torch, ttnn, tt-metal, tt-bio)  │  over unix  │                                    │
│                                   │   socket    │  GtkApplicationWindow              │
│  • job queue (priority)           │ ──────────▶ │   ├── GtkGLArea  (viewer)          │
│  • one worker per card            │             │   ├── pipeline panel               │
│  • models resident                │             │   ├── telemetry panel              │
│  • emits stage/frame/done/error   │             │   └── gallery / showcase           │
└───────────────────────────────────┘             └────────────────────────────────────┘
         systemd --user service                              │
         Restart=always                            tt-smi sampled independently
```

### Why two processes

**Environment incompatibility.** tt-bio requires a venv with torch, ttnn and the tt-metal
stack. GTK4 requires system `python3-gi`. Installing PyGObject into the tt-metal venv is
fragile and version-coupled to the system GTK. Keeping them apart removes the conflict
entirely rather than managing it.

**Fault isolation.** The compute side is the part that can hang, leak device memory, or
die on a wedged card. At a booth, none of that may take the screen down. The UI process
holds no device handles and imports nothing heavy; it is the thing that must never fail.

**Independent telemetry.** The UI samples `tt-smi` itself rather than receiving telemetry
from the runner. If the runner is wedged, the cards visibly keep breathing on screen and
the last structure keeps rotating, behind a small "reconnecting" indicator. This is a
deliberate resilience property, not an accident of layering.

### Components

| Component | Environment | Responsibility |
|---|---|---|
| `runner/queue.py` | tt-bio venv | Priority job queue; one in-flight job per card |
| `runner/folder.py` | tt-bio venv | Invokes tt-bio, holds models resident, taps `dump_fn` |
| `runner/events.py` | tt-bio venv | Serializes the event protocol to the socket |
| `runner/preflight.py` | tt-bio venv | Verifies offline readiness before Attract is allowed |
| `ui/app.py` | system | GTK application, window, state machine |
| `ui/viewer.py` | system | `GtkGLArea`: point-cloud and ribbon renderers, camera |
| `ui/geometry.py` | system | CIF → C-alpha spline → swept-tube mesh (gemmi) |
| `ui/telemetry.py` | system | `tt-smi` sampler (ported from tt-local-generator) |
| `ui/pipeline.py` | system | Stage progress widget |
| `ui/gallery.py` | system | Visitor pick grid |
| `ui/client.py` | system | Socket client, reconnect logic, frame dropping |
| `playlist/` | data | Curated targets: inputs, blurbs, pre-cached MSAs |
| `debian/` | packaging | Four packages (§7) |

Each unit is independently testable: the geometry module takes a CIF path and returns a
mesh; the telemetry module takes a recorded `tt-smi` JSON and returns card states; the
event codec round-trips. None of them require hardware.

---

## 3. The event protocol

Newline-delimited JSON over a Unix socket at
`$XDG_RUNTIME_DIR/tt-bio-demo/runner.sock`. One object per line, `type`-tagged.

| Type | Payload | Meaning |
|---|---|---|
| `hello` | `{version, cards[], models[]}` | Sent on connect; UI validates protocol version |
| `job_start` | `{job_id, target_id, model, card, n_residues}` | A fold began |
| `stage` | `{job_id, stage, frac}` | Stage progress: `msa`/`prep`/`trunk`/`diffusion`/`confidence`/`saving` |
| `frame` | `{job_id, step, total, coords_b64, n_atoms}` | One subsampled diffusion frame |
| `job_done` | `{job_id, cif_path, wall_s, mean_plddt}` | Fold complete |
| `job_error` | `{job_id, target_id, message}` | Fold failed; UI never displays `message` verbatim |
| `card_state` | `{card, state}` | `idle`/`busy`/`quarantined` |

`coords_b64` is base64 of a little-endian `float32[n_atoms][3]` buffer. A 300-residue
target is roughly 2,400 atoms ≈ 29 KB per frame; subsampled to ~30 frames per fold this is
under a megabyte per job, which the socket handles without ceremony.

**Frame dropping is a UI-side responsibility.** If the UI cannot keep up, it discards
frames rather than queuing them, so the animation stays real-time and lag never
accumulates. Frames are advisory; `job_done` is authoritative.

---

## 4. The fold animation

This is the centerpiece and the reason the demo is worth building.

### Live trajectory from `dump_fn`

tt-bio's diffusion sampler already exposes `dump_fn(sample, step, coords)`, invoked at
every denoising step with the intermediate all-atom coordinate tensor
(`tt_bio/protenix.py`, `tt_bio/opendde.py`). The runner taps this hook and streams
subsampled frames to the UI. What the audience sees is the **actual computation**: a cloud
of atoms condensing out of noise into a folded protein, in real time, on the hardware in
the room. It is not a canned animation and it cannot be faked.

### Model coverage

`dump_fn` exists today only in the Protenix-v2 / OpenDDE family. Those models therefore
carry the live-trajectory experience. Boltz-2, ESMFold2 and OpenFold3 fall back to
reveal-on-complete (§4.2) with no loss of correctness — and adding the same hook to them
upstream is a clean, well-scoped follow-up, deliberately out of scope here so this project
does not depend on landing changes in tt-bio.

### The point-cloud → ribbon handoff

`dump_fn` yields coordinates in the model's **internal atom ordering**. Reconstructing an
atom-to-residue mapping from that, live, to build a ribbon mid-trajectory is fiddly and
would be the riskiest code in the project.

We avoid it entirely:

1. **During diffusion**, render the raw coordinates as an **atom point cloud**. This needs
   no chemistry whatsoever — every atom is a point — and it is precisely the right visual
   for noise resolving into structure.
2. **On `job_done`**, parse the finished `.cif` with **gemmi**, which gives clean residue,
   chain and B-factor (pLDDT) data, build the C-alpha spline tube, and **cross-fade** from
   the point cloud to the polished ribbon.

The riskiest requirement disappears, and the resulting sequence — chaos, resolution, then
a beauty shot — is better theatre than a ribbon throughout would have been.

### Renderer

A single `GtkGLArea` with two draw modes sharing one camera, one trackball, and one
lighting setup:

- **Points:** instanced billboarded sprites, depth-sorted, sized by distance.
- **Ribbon:** Catmull-Rom spline through C-alpha positions, swept with a circular
  cross-section into a triangle mesh, smooth-shaded.

pLDDT drives hue on the ribbon (the conventional blue-to-orange confidence ramp); Tenstorrent
brand colors drive the rest of the interface. Idle rotation is a slow constant-rate spin
about the structure's principal axis.

`glxinfo` on the target reports OpenGL 4.6 via Mesa 25.2 on the QB2's integrated Radeon —
comfortably beyond what this renderer requires.

---

## 5. Interaction model

Four states:

```
    ┌──────────────────────────────────────────────────┐
    │                                                  │
    ▼                                                  │
 ATTRACT ──touch──▶ GALLERY ──pick──▶ FOLDING ──▶ SHOWCASE
    ▲                  │                                │
    └──── 45s idle ────┴────────────────────────────────┘
```

- **Attract** — auto-cycles the curated playlist unattended. Large typography, rotating
  structure, live telemetry, and a persistent "Touch to choose a protein" affordance.
- **Gallery** — a grid of curated targets: thumbnail, name, one-line description, residue
  count, expected runtime.
- **Folding** — the live trajectory, the pipeline panel, and the cards working.
- **Showcase** — the finished structure rotating, with observed wall-clock, residue count,
  model name, cards used, and two or three sentences on what this protein is and why it
  matters.

Any 45 seconds without input returns to Attract, so the booth resets itself after every
visitor with no staff involvement.

**Visitor picks and the attract loop share one queue.** A visitor's pick is enqueued at
high priority and taken by the next card to free up. In-flight jobs are never killed —
tearing down a fold mid-device-op is a needless source of instability, and with four cards
and sub-minute folds the wait is not perceptible.

### Content

`playlist/manifest.yaml` — one entry per target:

```yaml
- id: hemoglobin
  input: inputs/hemoglobin.yaml
  model: protenix-v2
  name: "Hemoglobin"
  blurb: "The oxygen carrier in your blood. Four chains cradle four iron atoms..."
  expected_s: 45
  msa_cache: msa/hemoglobin.a3m
```

Roughly a dozen targets drawn from tt-bio's ~30 bundled examples, chosen for recognizability
and runtime spread. **MSAs are pre-computed and shipped**, which is what makes the demo
independent of venue connectivity.

---

## 6. Failure handling

Unattended operation means every failure must terminate in something presentable. Nothing
below ever puts a stack trace on the display.

| Failure | Behavior |
|---|---|
| Fold fails | Logged with detail; UI shows a brief neutral notice; loop advances to the next target |
| Target fails 3× | Quarantined for the session; logged; silently skipped |
| Runner dies | UI keeps telemetry live and the last structure rotating behind a "reconnecting" chip; systemd restarts it |
| Socket backs up | UI drops frames; never queues them |
| Card overheats | Runner stops scheduling to it; UI dims that card in the telemetry panel |
| All cards unavailable | Calm "warming up" state; no scheduling attempts; loud logging |
| Weights/MSA missing | Preflight refuses to enter Attract and states exactly what is missing |

Card reset (`tt-smi -r`) is **not** attempted automatically. It is disruptive, and a demo
that resets hardware on its own is a demo that can fail in an interesting way in front of
an audience. It stays a documented manual step.

**Preflight** runs at startup and verifies weights, the OpenFold3 checkpoint, MSA caches,
the kernel cache, and card visibility. On a developer machine it reports what's missing and
exits; in demo mode it holds a "preparing" screen rather than entering Attract with content
that will fail. The point is that problems surface at your desk, not at the venue.

---

## 7. Packaging

Four Debian packages, following the structure already proven in `tt-local-generator`
(including debconf `.templates`/`.config` for weight downloads):

| Package | Contents |
|---|---|
| `tt-bio-demo` | GTK UI, runner source, playlist, systemd user unit, `.desktop` entry |
| `tt-bio-demo-runtime` | postinst creates `/opt/tt-bio-demo/venv`, pip-installs `tt-bio` **pinned to a release tag**, runs `tt-bio install-deps` |
| `tt-bio-demo-weights` | postinst downloads model weights and the OpenFold3 checkpoint (debconf prompt), verifies checksums, pre-warms the tt-metal kernel cache |
| `tt-bio-demo-all` | Metapackage depending on the three above |

Declared runtime dependencies: `python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-4.0`,
`python3-gemmi`, `libgl1`, `libglu1-mesa`, `curl`, `ca-certificates`; `tt-installer`
recommended.

**Weights are downloaded in postinst, not shipped in the package.** The alternative — a
multi-gigabyte data `.deb` — is unwieldy to build, host and revise, and the QB2 is
provisioned before the conference where network is available. Offline operation is
required *at the venue*, not at install time.

**tt-bio is pinned to a release tag, not `main`.** Upstream moves hundreds of commits a
month and its own README describes `main` as untested nightly. A demo box must be
reproducible; the pin is bumped deliberately and re-soaked.

**Kernel cache pre-warming matters more than it appears.** Per `docs/cold-start.md`, a
first-ever compile costs ~177 s versus ~12 s warm. Doing this at install time means the
first fold at the booth is already a warm one.

### Desktop application, not kiosk boot

The demo is a normal desktop application launched from a `.desktop` entry. Booting the box
into a locked-down kiosk compositor was considered and rejected for this iteration: it adds
a display-stack dependency to debug at a venue, and the desktop path keeps the same
artifact useful for development and for ad-hoc demos. The runner is a `systemd --user`
service with `Restart=always`, so the fragile half is supervised regardless.

---

## 8. Testing

**A `--mock` runner replays a recorded event stream.** One real fold is captured to JSONL
and replayed with realistic timing, so the entire UI — renderer, state machine, telemetry
fallbacks, reconnect logic — is developable and testable on any laptop with no Tenstorrent
hardware attached. This is the single highest-leverage piece of test infrastructure in the
project and should be built early.

Unit tests:

- Playlist manifest schema validation, including that every referenced input and MSA exists.
- Event protocol codec round-trip, including malformed and truncated lines.
- CIF → ribbon geometry against golden mesh checksums for a few fixed structures.
- `tt-smi` parser against recorded snapshots, including the no-`tt-smi` AICLK fallback.
- State machine transitions, including idle timeout and reconnect.

Integration tests (require hardware):

- End-to-end fold through the runner emitting a well-formed event sequence.
- Preflight correctly detects each class of missing asset.

**Soak test:** a multi-hour headless run cycling the full playlist, asserting no memory
growth, no crashes, no card drop-outs, and no queue stalls. "Leave it on all day" is a
claim that requires evidence, and this is the evidence.

---

## 9. Repository layout

```
tt-bio-demo/
├── ui/                 # system-python GTK4 application
├── runner/             # tt-bio-venv compute daemon
├── playlist/           # manifest.yaml, inputs/, msa/, thumbnails/
├── debian/             # four packages
├── tests/
│   ├── unit/
│   ├── integration/    # hardware-gated
│   └── fixtures/       # recorded event streams, tt-smi snapshots, golden meshes
├── docs/
│   └── superpowers/specs/
├── CLAUDE.md
└── README.md
```

---

## 10. Open questions for implementation

These are deliberately deferred to the implementation plan rather than guessed at now:

1. **Playlist composition.** Which dozen targets, and their exact blurbs, needs a pass with
   someone who can speak to biological interest, not just runtime.
2. **Which model leads.** Protenix-v2 carries the live trajectory today, so it likely
   anchors the loop; whether Boltz-2 affinity or BoltzGen design earn a slot is a content
   decision to make once the loop exists.
3. **Thumbnail generation.** Gallery thumbnails could be pre-rendered at package build time
   using the app's own renderer — pleasing consistency, but it needs a headless render path.
