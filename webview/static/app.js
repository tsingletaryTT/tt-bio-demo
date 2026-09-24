"use strict";

// The thin web viewer's browser half. Everything it draws comes from real
// protocol/events.py events relayed unmodified by webview/bridge.py -- there
// is no fabricated animation here, only a v1 SCOPE CUT from the native app,
// called out at each point below:
//
//   * Fold points render as a plain point cloud (POINT_COLOR teal, same as
//     ui/viewer.py before confidence data exists). There is no ribbon/cartoon
//     reveal on job_done -- that needs the .cif geometry pipeline
//     (ui/cartoon.py, ui/secstruct.py), which is pure numpy/gemmi and a
//     genuine follow-up (server-side precompute, or Pyodide running that
//     same code client-side -- see the conversation this was built from).
//   * The gallery is "targets seen so far", not the real manifest.yaml
//     playlist (thumbnails, descriptions, expected_s) -- reading that would
//     need a YAML dependency this bridge deliberately does not take on.
//   * Affinity questions show target_id and the model's own score, not the
//     human-written question text from playlist/questions.yaml -- that text
//     never travels on the wire (see the design spec, section 4); only the
//     booth's own rail panel has it loaded locally.

const state = {
  cards: [],           // card ids from the last `hello`
  cells: new Map(),    // card id -> Cell
  jobToCard: new Map(),// job_id -> card id, for stage/frame/job_done/job_error
  qaCapable: false,
  qaCard: null,        // the physical chip reserved for Q&A, or null (hello.qa_card)
  focusedCard: null,
  seenTargets: [],      // target_ids observed in job_start, most recent first
  viewModeUserSet: false, // true once the visitor has pressed the toggle --
                          // an explicit choice always outranks the auto-pick
                          // below, same rule the native GTK booth's own
                          // tri-state `quad` follows (ui/app.py).
};

const POINT_COLOR = "#74c5df";
const BACKGROUND = "#092221";

class Cell {
  constructor(card) {
    this.card = card;
    this.el = document.createElement("div");
    this.el.className = "cell";
    this.el.dataset.card = String(card);

    this.canvas = document.createElement("canvas");
    this.canvas.width = 480;
    this.canvas.height = 360;
    this.el.appendChild(this.canvas);
    this.ctx = this.canvas.getContext("2d");

    this.labelEl = document.createElement("div");
    this.labelEl.className = "chip-label";
    this.labelEl.textContent = `CHIP ${card}`;
    this.el.appendChild(this.labelEl);

    this.captionEl = document.createElement("div");
    this.captionEl.className = "caption";
    this.el.appendChild(this.captionEl);

    this.el.addEventListener("click", () => setFocus(card));

    this.points = null;       // the live frame, or null between frames
    this.heldPoints = null;   // last real points, held+dimmed ("never blank")
    this.targetId = null;
    this.stage = null;
    this.meanPlddt = null;
    this.cardState = "idle";
    this.angle = Math.random() * Math.PI * 2;
    // The job_id this cell is CURRENTLY showing. stage/frame/job_done/
    // job_error events are only applied when they carry this exact id --
    // see the module-level handleEvent switch. Without this, a stale event
    // from a superseded fold (the daemon starts fold N+1 the instant fold N
    // ends, and nothing here orders their arrival) could still resolve to
    // this cell via state.jobToCard and overwrite what the CURRENT fold is
    // showing -- the same class of bug ui/app.py's own history already
    // paid to find and fix once on the native side.
    this.activeJobId = null;
  }

  onJobStart(jobId, targetId) {
    this.activeJobId = jobId;
    this.targetId = targetId;
    this.stage = "starting";
    this.points = null;
    this.meanPlddt = null;
  }

  onStage(stage) {
    this.stage = stage;
  }

  onFrame(points) {
    this.points = points;
    this.heldPoints = points;
  }

  onJobDone(meanPlddt) {
    this.stage = "done";
    this.meanPlddt = meanPlddt;
    this.points = null;
  }

  onJobError() {
    this.stage = "error";
    this.points = null;
  }

  onCardState(cardState) {
    this.cardState = cardState;
  }

  captionText() {
    const target = this.targetId || "—";
    if (this.stage === "done") {
      const plddt = this.meanPlddt != null ? this.meanPlddt.toFixed(1) : "—";
      return `${target} · done · pLDDT ${plddt}`;
    }
    if (this.stage === "error") return `${target} · failed`;
    if (this.cardState === "quarantined") return `${target} · quarantined (cooling)`;
    if (this.targetId) return `${target} · ${this.stage || "starting"}`;
    return "idle";
  }

  draw() {
    const ctx = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;
    ctx.fillStyle = BACKGROUND;
    ctx.fillRect(0, 0, w, h);

    const live = this.points;
    const drawSet = live || this.heldPoints;
    if (drawSet && drawSet.length) {
      this._drawPoints(drawSet, !live);
    }

    this.labelEl.textContent = `CHIP ${this.card}`;
    this.captionEl.textContent = this.captionText();
    this.el.classList.toggle("focused", this.card === state.focusedCard);
    this.angle += 0.004;
  }

  _drawPoints(points, dim) {
    const ctx = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;
    const cos = Math.cos(this.angle);
    const sin = Math.sin(this.angle);
    let maxR = 1e-6;
    const projected = new Array(points.length);
    for (let i = 0; i < points.length; i++) {
      const [x, y, z] = points[i];
      const rx = x * cos - z * sin;
      const rz = x * sin + z * cos;
      maxR = Math.max(maxR, Math.abs(rx), Math.abs(y), Math.abs(rz));
      projected[i] = [rx, y];
    }
    const scale = (Math.min(w, h) * 0.42) / maxR;
    ctx.globalAlpha = dim ? 0.35 : 1.0;
    ctx.fillStyle = POINT_COLOR;
    const r = Math.max(1.4, w * 0.01);
    for (const [rx, y] of projected) {
      ctx.beginPath();
      ctx.arc(w / 2 + rx * scale, h / 2 - y * scale, r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1.0;
  }
}

// --- coordinate decoding: the exact inverse of protocol.events.pack_coords,
// which writes an (N, 3) array as base64 of little-endian float32. Every
// browser this app targets is little-endian, so a plain Float32Array view
// over the decoded bytes reads the same values numpy wrote -- no DataView
// byte-swapping needed (there is no big-endian browser to worry about).
function unpackCoords(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const floats = new Float32Array(bytes.buffer);
  const n = Math.floor(floats.length / 3);
  const points = new Array(n);
  for (let i = 0; i < n; i++) {
    points[i] = [floats[i * 3], floats[i * 3 + 1], floats[i * 3 + 2]];
  }
  return points;
}

// --- DOM plumbing -----------------------------------------------------

const liveDot = document.getElementById("live-dot");
const liveText = document.getElementById("live-text");
const boothTitle = document.getElementById("booth-title");
const boothSub = document.getElementById("booth-sub");
const noticeEl = document.getElementById("notice");
const stageEl = document.getElementById("stage");
const viewToggle = document.getElementById("view-toggle");
const qaPanel = document.getElementById("qa-panel");
const qaQuestion = document.getElementById("qa-question");
const qaSpinner = document.getElementById("qa-spinner");
const qaScore = document.getElementById("qa-score");
const pickForm = document.getElementById("pick-form");
const pickInput = document.getElementById("pick-input");
const pickError = document.getElementById("pick-error");
const seenTargetsEl = document.getElementById("seen-targets");

function setNotice(text) {
  if (!text) {
    noticeEl.hidden = true;
    return;
  }
  noticeEl.hidden = false;
  noticeEl.textContent = text;
}

function setFocus(card) {
  state.focusedCard = card;
}

// A question "on screen" gets a little marching-band bounce while it's
// actively being asked -- an ORIGINAL animation (per-letter bob + a warm
// color cycle over this project's own palette), not a reproduction of any
// particular song, lyric, or character design: nothing here is copied from
// anywhere, just evoking a jaunty, playful energy for the moment nesso1 is
// working. Settles to plain static text once a result exists (`answered`
// false below) -- motion means "pending," stillness means "here's the
// answer," which is its own small piece of honesty: nothing keeps
// performing once there is a real number to read.
function setQuestionText(el, text, { animated }) {
  el.innerHTML = "";
  if (!animated) {
    el.textContent = text;
    el.classList.remove("oompa-bounce");
    return;
  }
  el.classList.add("oompa-bounce");
  [...text].forEach((ch, i) => {
    const span = document.createElement("span");
    span.textContent = ch === " " ? " " : ch;
    span.style.setProperty("--i", i);
    el.appendChild(span);
  });
}

function setStageMode(mode) {
  stageEl.dataset.mode = mode;
  viewToggle.textContent = mode === "solo" ? "Quad view" : "Solo view";
}

function renderQaPlaceholder(card) {
  // A real chip, deliberately never given a fold Cell -- see the "hello"
  // handler's comment on state.qaCard. No canvas, no animation: nothing to
  // draw for a chip that never receives frame/job_start events, and drawing
  // something anyway would be exactly the kind of fabricated activity this
  // project's content-honesty rule forbids.
  const el = document.createElement("div");
  el.className = "cell qa-reserved";
  el.dataset.card = String(card);
  const label = document.createElement("div");
  label.className = "chip-label";
  label.textContent = `CHIP ${card}`;
  el.appendChild(label);
  const caption = document.createElement("div");
  caption.className = "caption";
  caption.textContent = "reserved for Q&A -- see the panel below";
  el.appendChild(caption);
  return el;
}

function rebuildCellsIfNeeded(cards) {
  // `cards` is the full inventory (runner/daemon.py's _hello: "a card
  // legitimately busy, quarantined, or RESERVED has not stopped existing")
  // and includes the Q&A chip when one is reserved -- the "changed" check
  // below is against that full list, so a real hardware change (a chip
  // added or retired) is still detected even though the Q&A chip never gets
  // a fold Cell.
  const changed = cards.length !== state.cards.length
    || cards.some((c, i) => c !== state.cards[i]);
  if (!changed) return;
  state.cards = cards.slice();
  stageEl.innerHTML = "";
  state.cells.clear();
  // `cards` is the FOLD inventory only -- confirmed directly against a real
  // daemon: `hello.cards` is [0,1,2] on a 4-chip booth with Q&A on, never
  // [0,1,2,3]. The reserved chip is real (it is chip 3), it is simply never
  // in this list at all -- so this filter is a no-op today and exists only
  // so a future daemon that DOES include it (matching the `_hello` comment
  // that reads as if it should) doesn't silently grow a duplicate cell.
  const foldCards = cards.filter((c) => c !== state.qaCard);
  for (const card of foldCards) {
    const cell = new Cell(card);
    state.cells.set(card, cell);
    stageEl.appendChild(cell.el);
  }
  // The placeholder is keyed on qaCard being SET, not on it appearing in
  // `cards` -- it deliberately does not, so gating on `cards.includes(...)`
  // would mean this branch never runs. Without it, the reserved chip's grid
  // slot was simply empty: no label, no explanation, indistinguishable at a
  // glance from "nothing is happening here" -- which is the exact
  // "chip 3 idle" confusion this was built to fix in the first place.
  if (state.qaCard !== null) {
    stageEl.appendChild(renderQaPlaceholder(state.qaCard));
  }
  if (state.focusedCard === null || !foldCards.includes(state.focusedCard)) {
    state.focusedCard = foldCards[0] ?? null;
  }
  // Quad by default once more than one FOLD chip is actually available (the
  // reserved Q&A chip doesn't count -- a booth with one fold chip and Q&A on
  // is still a solo booth), mirroring the native booth's own tri-state
  // default (ui/app.py, 2026-08-24: "I like 4 chip by default when
  // available"). Never overrides a visitor's own press of the toggle, and
  // never downgrades back to solo just because the card list happened to
  // arrive gradually and briefly had one entry.
  if (!state.viewModeUserSet && foldCards.length > 1) {
    setStageMode("quad");
  }
}

function addSeenTarget(targetId) {
  if (!targetId || state.seenTargets.includes(targetId)) return;
  state.seenTargets.unshift(targetId);
  state.seenTargets = state.seenTargets.slice(0, 12);
  seenTargetsEl.innerHTML = "";
  for (const t of state.seenTargets) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = t;
    b.addEventListener("click", () => sendPick(t));
    seenTargetsEl.appendChild(b);
  }
}

function cellForJob(jobId) {
  const card = state.jobToCard.get(jobId);
  return card === undefined ? null : state.cells.get(card);
}

// Read once at load, from this PAGE's own URL -- a visitor opens
// http://host:port/?token=SECRET, and every request this page makes
// (the SSE connection and every fetch()) carries the same token back to
// the bridge. If the bridge has no --auth-token/WEBVIEW_AUTH_TOKEN
// configured, it ignores this entirely and nothing here changes behavior.
const AUTH_TOKEN = new URLSearchParams(location.search).get("token");

function withToken(url) {
  if (!AUTH_TOKEN) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(AUTH_TOKEN)}`;
}

// --- protocol event handling -------------------------------------------

function handleEvent(event) {
  switch (event.type) {
    case "hello": {
      if (event.bridge_incompatible) {
        // The bridge itself (webview/bridge.py), not this script, decided
        // the daemon's protocol version doesn't match what it speaks --
        // flagged that way rather than this file hardcoding its own copy
        // of PROTOCOL_VERSION, which would just be a second number to keep
        // in sync with protocol/events.py by hand.
        liveDot.className = "dot dot-off";
        liveText.textContent = "incompatible daemon version";
        setNotice(
          `The bridge refuses to talk to this daemon: it reports protocol `
          + `v${event.version}, which this bridge was not built for. `
          + `Upgrade one side and restart both.`);
        return;
      }
      liveDot.className = "dot dot-on";
      liveText.textContent = "live";
      boothTitle.textContent = `${event.cards.length} chip${event.cards.length === 1 ? "" : "s"} · protocol v${event.version}`;
      boothSub.textContent = event.models.join(", ");
      state.qaCapable = !!event.qa_capable;
      // Which physical chip is reserved for Q&A, if any (runner/daemon.py's
      // `_hello`: "qa_card"). It is still a real, present chip -- `cards`
      // below is the FULL inventory and includes it -- but it is never
      // scheduled a fold (runner/workers.py's split_for_qa), so a normal
      // fold Cell for it would sit "idle" forever: real, but misleading
      // next to the native GTK booth, which excludes this same chip from
      // its own fold quad and shows the Q&A spotlight in its place
      // (ui/quad.py's set_extra_cell). Mirrored here in
      // rebuildCellsIfNeeded/renderQaPlaceholder rather than left to read
      // as a stuck chip.
      state.qaCard = event.qa_card ?? null;
      qaPanel.hidden = !state.qaCapable;
      setNotice("");
      rebuildCellsIfNeeded(event.cards);
      break;
    }
    case "not_ready": {
      liveDot.className = "dot dot-on";
      liveText.textContent = "live";
      setNotice(`booth preparing: ${event.missing.join("; ")}`);
      break;
    }
    case "job_start": {
      state.jobToCard.set(event.job_id, event.card);
      const cell = state.cells.get(event.card);
      if (cell) cell.onJobStart(event.job_id, event.target_id);
      addSeenTarget(event.target_id);
      break;
    }
    case "stage": {
      // Gated on activeJobId, not just cellForJob's card lookup: cellForJob
      // resolves the right CELL (a job_id always maps to the card it
      // started on), but that cell may have already moved on to a newer
      // job by the time a straggler for THIS job_id arrives -- see
      // Cell.activeJobId's own comment.
      const cell = cellForJob(event.job_id);
      if (cell && cell.activeJobId === event.job_id) cell.onStage(event.stage);
      break;
    }
    case "frame": {
      const cell = cellForJob(event.job_id);
      if (cell && cell.activeJobId === event.job_id) cell.onFrame(unpackCoords(event.coords_b64));
      break;
    }
    case "job_done": {
      const cell = cellForJob(event.job_id);
      if (cell && cell.activeJobId === event.job_id) cell.onJobDone(event.mean_plddt);
      state.jobToCard.delete(event.job_id);
      break;
    }
    case "job_error": {
      const cell = cellForJob(event.job_id);
      if (cell && cell.activeJobId === event.job_id) cell.onJobError();
      state.jobToCard.delete(event.job_id);
      break;
    }
    case "card_state": {
      const cell = state.cells.get(event.card);
      if (cell) cell.onCardState(event.state);
      break;
    }
    case "telemetry": {
      Telemetry.render(document.getElementById("telemetry-panel"), event.chips);
      break;
    }
    case "answer_start": {
      qaPanel.hidden = false;
      setQuestionText(qaQuestion, `Scoring affinity for ${event.target_id}…`, { animated: true });
      qaSpinner.hidden = false;
      qaScore.hidden = true;
      break;
    }
    case "answer_done": {
      qaPanel.hidden = false;
      setQuestionText(qaQuestion, `Affinity result for ${event.target_id}`, { animated: false });
      qaSpinner.hidden = true;
      qaScore.hidden = false;
      const pct = Math.round(event.score * 100);
      qaScore.textContent = `${pct}% predicted probability of binding`;
      break;
    }
    case "answer_error": {
      qaPanel.hidden = false;
      setQuestionText(qaQuestion, `Scoring failed for ${event.target_id}.`, { animated: false });
      qaSpinner.hidden = true;
      qaScore.hidden = true;
      break;
    }
    case "egg_frame":
    case "egg_refused":
      // Deliberately unsurfaced -- see the module docstring above.
      break;
    default:
      console.debug("unhandled event type", event.type);
  }
}

function drawLoop() {
  for (const cell of state.cells.values()) cell.draw();
  requestAnimationFrame(drawLoop);
}
requestAnimationFrame(drawLoop);

// --- SSE connection ------------------------------------------------------

function connect() {
  const source = new EventSource(withToken("/events"));
  source.onopen = () => {
    liveDot.className = "dot dot-on";
    liveText.textContent = "live";
  };
  source.onerror = () => {
    liveDot.className = "dot dot-off";
    liveText.textContent = "reconnecting…";
    // EventSource retries on its own; nothing else to do here.
  };
  source.onmessage = (msg) => {
    let event;
    try {
      event = JSON.parse(msg.data);
    } catch (err) {
      console.warn("malformed event from bridge", err);
      return;
    }
    handleEvent(event);
  };
}
connect();

// --- actions: pick + view toggle -----------------------------------------

async function sendPick(targetId) {
  pickError.hidden = true;
  try {
    const resp = await fetch(withToken("/pick"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_id: targetId }),
    });
    if (!resp.ok) {
      const text = await resp.text();
      pickError.hidden = false;
      pickError.textContent = resp.status === 503
        ? "Not connected to a daemon right now."
        : `Pick refused: ${text}`;
    }
  } catch (err) {
    pickError.hidden = false;
    pickError.textContent = "Could not reach the bridge.";
  }
}

pickForm.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const targetId = pickInput.value.trim();
  if (!targetId) {
    pickError.hidden = false;
    pickError.textContent = "Enter a target id first.";
    return;
  }
  sendPick(targetId);
  pickInput.value = "";
});

viewToggle.addEventListener("click", () => {
  state.viewModeUserSet = true;
  const solo = stageEl.dataset.mode === "solo";
  setStageMode(solo ? "quad" : "solo");
});
