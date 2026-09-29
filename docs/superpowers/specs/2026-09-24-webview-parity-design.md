# Webview Parity — Design Spec

## 1. Why

`webview/` (added 2026-09-21) is a thin browser companion to the GTK4 booth: a bridge
process relays the daemon's own socket protocol to any browser tab over SSE. It works, and
its own README is honest about five deliberate scope cuts made to ship it fast: no ribbon
reveal (points only), a synthetic gallery ("targets seen so far," not the real playlist), no
real question text (target_id + score only), the easter egg silently dropped, and no
frame-level reconnect backfill.

The ask now is to close that gap: **get the webview as close to the native GTK app's
verisimilitude as the medium allows**, so someone can open this over a tunnel with no
physical QuietBox2 in front of them and still see everything the in-person demo shows —
the actual fold happening, the actual Tensix silicon working, the actual telemetry, the
actual questions being asked and answered. The daemon is unmodified throughout: every
addition here is the bridge doing more with events the daemon already emits, or the bridge
independently sampling the same hardware the native UI already samples independently (its
own module docstring: telemetry sampling is deliberately daemon-independent, "so a wedged
or dead daemon still leaves the silicon visibly breathing on screen").

The egg and full reconnect-backfill remain explicitly out of scope (see §8) — named, not
silently dropped, the same shape this project has handled every prior deferred item in.

## 2. Scope

In scope, five pieces, ordered by how much they depend on each other:

1. **Real telemetry** (temp/power/clock per chip) — no daemon change, no new dependency,
   the easiest and lowest-risk piece; do this first to prove the "bridge samples its own
   hardware" pattern before anything else builds on it.
2. **Real Tensix activity panel** — reuses the exact vendored `tensix-viz.js`/`.css` files
   the GTK app already ships, fed by the telemetry piece above plus `stage.frac` (already on
   the wire, already unused by the browser today).
3. **Real Q&A queue + real gallery** — the bridge reads `playlist/questions.yaml` and
   `playlist/manifest.yaml` server-side (via `ui.playlist`'s existing, already-tested
   loaders) and forwards the real text, closing two of the five README scope-cuts outright.
4. **Help screen** — ports `ui/app.py`'s plain-data help content (no widget-building
   involved) to a bridge-served endpoint.
5. **Ribbon rendering** — the big one. Server-side precompute via `ui.cartoon.cartoon_from_cif`
   (the exact function the GTK app trusts), a hand-rolled WebGL renderer porting
   `ui/shaders.py`'s GLSL and `ui/mathutil.py`'s camera math.

**Transport stays SSE + POST.** No WebSocket. The traffic is one-directional server push
plus rare, small, human-triggered actions; `DaemonLink`'s existing subscribe/broadcast
shape already fits every new event type below without restructuring.

## 3. Architecture: one broadcast hub, more publishers

Today, `DaemonLink._broadcast` is called from exactly one place: `_dispatch`, relaying a
raw daemon event. Every new capability below is a **second kind of publisher** onto the
same subscriber fan-out — a `TelemetrySampler`'s tick, a finished ribbon computation — none
of them daemon-relayed, all of them delivered over the identical `/events` SSE stream a
browser tab already listens to.

`DaemonLink` gains one public method, `publish(event)`, doing exactly what `_dispatch`'s
tail end does today (broadcast to every subscriber's queue). `_dispatch` calls it for
relayed daemon events; the new `TelemetryBroadcaster` and `RibbonCache` (both below) call
it directly for their own synthesized events. No second pub-sub mechanism, no new
subscriber-management code — one hub, more feeds into it.

**These new event types are bridge-internal.** They are never added to
`protocol/events.py`'s `EVENT_TYPES` — that set specifically validates what the *daemon*
emits, via `decode_event`'s round-trip contract that `ui/client.py` and `DaemonLink` both
rely on, and the daemon will never emit a `telemetry` or `ribbon_ready` event. They are
validated and documented in `webview/bridge.py` itself, as a small bridge-owned superset
layered on top of the daemon's real vocabulary. A browser client that only understands the
original five REST/SSE surface (a stale cached tab, say) still works unmodified — every new
event type is additive, and `app.js`'s existing `default: console.debug(...)` fallthrough
already handles an unrecognized type gracefully.

## 4. Telemetry (§2 item 1)

`ui/telemetry.py`'s `TelemetrySampler`/`ChipReading`/`parse_snapshot` are already
GTK-free, already tested, already the exact code the native UI runs. The bridge
instantiates one `TelemetrySampler(period_s=2.0)` at startup (same interval the native
panel uses), alongside a `TelemetryBroadcaster` that polls `.latest()` every tick and
`publish()`s a new event when the reading changes (never on every tick if nothing moved —
avoids a needless SSE write every 2s for a static number):

```json
{"type": "telemetry", "chips": [
  {"index": 0, "board_type": "p300c", "temperature_c": 47.8,
   "power_w": 19.0, "aiclk_mhz": 800, "board_id": "0000046131924062"}
]}
```

A chip whose reading is currently unreadable (matches `ChipReading`'s own optional fields)
sends `null` for that field — never a fabricated number, matching this project's own
content-honesty standard everywhere else. `.latest() is None` (no sample has ever
succeeded) means no `telemetry` event fires at all yet; the browser's own per-cell display
shows an em dash exactly like a not-yet-connected native panel would, driven by simply
never having received one.

**Failure mode:** `TelemetrySampler` already returns `[]`/`None` rather than raising on any
`tt-smi` failure (missing binary, timeout, bad JSON) — verified behavior, not an assumption,
since this is the identical class the native UI has run in production. The bridge adds
nothing on top; it inherits that contract for free.

## 5. Real Tensix activity panel (§2 item 2)

`ui/chipviz.py`'s `read_assets()` reads `ui/assets/tensix-viz/tensix-viz.{js,css}` off disk.
The bridge's static-file handler (already path-traversal-guarded) serves these same two
files verbatim at e.g. `/tensix-viz.js` / `/tensix-viz.css` — confirmed by direct reading of
the generated page wrapper that the library itself has zero WebKit-specific glue (every
call is `document.*`/`requestAnimationFrame`/plain DOM).

`app.js` gains a small facade mirroring `build_page_html`'s generated wrapper exactly:
construct one `new TensixViz(canvas, {arch: "blackhole", showMemory: true})` per chip
canvas, call `.activate(mode)` on stage change (mode-from-stage mapping is `ui/chipviz.py`'s
pure `viz_mode`/`_MODE_BY_STAGE` — small enough, and stable enough, to port to JS directly;
a cross-language drift test — the same "one check that reads what actually executes"
pattern `test_weights_cache_is_derived_once.py` and `test_run_webview_sh.py` already use in
this repo — asserts the JS mode table's literal values match `ui.chipviz`'s Python source),
`.setActivity(a)` fed from the telemetry event's `power_w` through `power_activity` (ported
the same way, floor=15.0/ceiling=90.0/curve=0.6, same drift test covers it), and
`.setProgress(p)` fed from `stage.frac` via a small `withinStageFrac(stage, frac)` JS port of
`protocol/events.py`'s `within_stage_frac` (the STAGE_BANDS table, also drift-tested).

No AICLK fallback path is needed client-side the way `ui/chipviz.py` needs one: the bridge
always has a real telemetry sample within 2s of startup (or none at all, which reads as
resting, same "idle relative" honesty the native panel already holds itself to) — there is
no sysfs-vs-tt-smi distinction in a browser, since the browser has neither.

## 6. Real Q&A queue + real gallery (§2 item 3)

The bridge loads `playlist/questions.yaml` via `ui.playlist.load_questions()` and
`playlist/manifest.yaml` via `ui.playlist.load_playlist()` once at startup (both already
validate at load time and raise `PlaylistError` on anything malformed — the bridge fails
loudly at startup on a bad file, the same "config error, not runtime" standard
`ui.playlist`'s own docstring already holds itself to), and sends both as one-time catalog
events right after `hello`:

```json
{"type": "questions_catalog", "questions": [
  {"id": "dhfr_mtx", "target_id": "dhfr",
   "question": "Does methotrexate block dihydrofolate reductase?",
   "ligand_name": "Methotrexate", "expected_s": null}
]}
{"type": "playlist_catalog", "targets": [
  {"id": "trpcage", "name": "Trp-cage", "tagline": "...", "expected_s": 4.6}
]}
```

`app.js`'s single global `#qa-panel` becomes a **queue** matching `QuestionQueuePanel`'s
own three-row shape (pending / in-flight / answered), using `ui.questions`'s own pure text
functions **at the point the bridge builds these catalog payloads** — `question_label`,
`pending_text`, `in_flight_text`, `answered_text`, `error_text`, `SCORE_GLOSS` all run
server-side, so the text the browser displays is rendered by the exact same functions the
GTK app calls, not a second hand-typed copy in JS. The browser's job is arranging already-
correct strings into pending/in-flight/answered rows, mirroring `QuestionQueuePanel._render`'s
own three-bucket logic (exclude whichever question is in-flight or most-recently-answered
from the pending list) — a small, pure port, covered by a JS-side unit test mirroring
`test_questions_panel.py`'s own coverage.

The gallery becomes the real target list with real names/taglines/timings instead of "seen
so far," with `/pick` unchanged (still just a `target_id` string).

Thumbnails are **out of scope for this pass** — `playlist_catalog` carries text only. Named
here so it reads as a scoped cut, not an oversight: they're static files `ui.playlist`
already knows the paths to, and adding them is a small, separate follow-up once the text
parity above is proven.

## 7. Help screen (§2 item 4)

`ui/app.py`'s `_help_intro(n_chips)` / `_key_help(n_chips)` / `_help_panels(n_chips)` /
`_PLDDT_LEGEND` are plain functions/data — confirmed by direct reading, no widget
construction inside them. The bridge exposes `GET /help.json`, computed fresh per request
(cheap, pure, and `n_chips` can genuinely change if the card list changes mid-session):

```json
{"intro": ["...", "..."], "keys": [["? or F1", "this card, any time"]],
 "panels": ["..."], "plddt_legend": [["plddt-high", "90+", "very high, trust it"]]}
```

`app.js` renders this into a card matching the native `?` overlay's content, opened by a
`?` key binding and a visible on-screen affordance (mirroring the native booth's own
"always-visible hint" rule) — never silently keyboard-only, since a remote tunnel viewer
has no reason to already know the native app's key bindings.

## 8. Ribbon rendering (§2 item 5)

**Trigger and cache.** On a relayed `job_done` event carrying a `cif_path` (confirmed field,
`runner/folder.py:352`), the bridge's new `RibbonCache` submits `ui.cartoon.cartoon_from_cif
(cif_path)` to a small `ThreadPoolExecutor` (2 workers — this is real CPU work: gemmi
parsing, spline sweeps; it must never block `DaemonLink`'s own dispatch thread, and two
concurrent finishes is plenty of headroom for four folding chips). On completion it caches
the four arrays keyed by `job_id` (bounded LRU, same `SUBSCRIBER_QUEUE_MAX`-style explicit
cap this file already applies elsewhere — old entries evicted, never unbounded growth on a
booth that runs all day) and `publish()`s:

```json
{"type": "ribbon_ready", "job_id": "...",
 "vertices_b64": "...", "normals_b64": "...", "colors_b64": "...",
 "indices_b64": "...", "vertex_count": N, "index_count": M}
```

Float32 arrays packed exactly like `pack_coords` (base64 little-endian), `indices_b64` as
uint32 the same way. A late-joining browser tab that missed the original `job_done` can
`GET /ribbon/<job_id>` for the same cached buffers on demand — the cache is the source of
truth either way, SSE push and on-demand GET both read it.

`app.js` already tracks `jobToCard` from `job_start`; `ribbon_ready` uses the same map to
find its cell, exactly as `frame`/`job_done` already do. No new job-tracking state needed.

**Rendering: hand-rolled WebGL, not three.js.** `ui/shaders.py`'s `RIBBON_VERT`/`RIBBON_FRAG`
(desktop GLSL 330 core) and `ui/mathutil.py`'s camera math (`look_at`, perspective, rotation
— including the column-major fix this project has *already paid to find once*, see
CLAUDE.md's own history) port mechanically to WebGL1-compatible GLSL and JS respectively.
This keeps the page dependency-free (matching the project's existing "vendor or hand-build,
never a CDN fetch for something this size" ethos already applied to tensix-viz) and reuses
math this project has already debugged rather than re-discovering the same bug class in a
second language. The point cloud keeps its existing 2D canvas rendering unchanged during
diffusion; only the finished ribbon gets the new WebGL path, cross-fading in exactly the
way the native `StructureViewer` already does (alpha ramp, not a hard cut) — the same
"never a jarring swap" rule, ported rather than reinvented.

**Verified, not assumed, before committing to this path:** `ui/geometry.py` imports `gemmi`
at module scope — a compiled C++ extension with no realistic Pyodide/WASM story — which is
why server-side precompute is the only path considered; running `ui.cartoon` unmodified
in-browser was this project's own webview/README's OTHER named option and is ruled out here
on that concrete evidence, not by preference.

## 9. Testing strategy

- Every new pure Python function (`TelemetryBroadcaster`'s change-detection, `RibbonCache`'s
  eviction) gets a direct unit test, GTK-free, matching this file's existing test
  conventions (`tests/unit/test_webview_bridge.py`).
- Every ported JS pure function (`viz_mode`, `power_activity`, `withinStageFrac`, the
  pending/in-flight/answered bucketing) gets a JS-side test AND a cross-language drift test
  reading the real Python source's literal constants, the same shape this repo already uses
  for the weights-cache variables and the Ctrl+A exit code.
- End-to-end: `runner.mock.MockRunner` replaying a real fixture over a real socket, bridge
  and a real (or headless-browser-driven) client on top, mirroring how `webview/`'s original
  build was verified with no hardware — extended fixtures need a `job_done` with a real
  `cif_path` pointing at a checked-in test CIF (already exists somewhere under
  `tests/fixtures/` for `ui/cartoon.py`'s own tests — reuse it) so the ribbon path is
  exercised without needing live hardware either.
- Real hardware verification (real `tt-smi`, a real fold, a real finished ribbon rendered
  in a real browser) happens once, at the end, the same "one real fold is the only proof
  the load path works" standard this project applies to every daemon change.

## 10. Rollout

Built in the dependency order §2 lists (telemetry → Tensix reuse → Q&A/gallery text → help
→ ribbon), each piece independently shippable and independently testable, since none of the
later pieces are needed for the earlier ones to work. The egg and full frame-level
reconnect backfill remain named, deferred follow-ups (README already documents both;
updated to reflect what closed and what didn't once this lands).
