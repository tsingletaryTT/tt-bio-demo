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
  focusedCard: null,
  seenTargets: [],      // target_ids observed in job_start, most recent first
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
  }

  onJobStart(targetId) {
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

function rebuildCellsIfNeeded(cards) {
  const changed = cards.length !== state.cards.length
    || cards.some((c, i) => c !== state.cards[i]);
  if (!changed) return;
  state.cards = cards.slice();
  stageEl.innerHTML = "";
  state.cells.clear();
  for (const card of cards) {
    const cell = new Cell(card);
    state.cells.set(card, cell);
    stageEl.appendChild(cell.el);
  }
  if (state.focusedCard === null || !cards.includes(state.focusedCard)) {
    state.focusedCard = cards[0] ?? null;
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
      if (cell) cell.onJobStart(event.target_id);
      addSeenTarget(event.target_id);
      break;
    }
    case "stage": {
      const cell = cellForJob(event.job_id);
      if (cell) cell.onStage(event.stage);
      break;
    }
    case "frame": {
      const cell = cellForJob(event.job_id);
      if (cell) cell.onFrame(unpackCoords(event.coords_b64));
      break;
    }
    case "job_done": {
      const cell = cellForJob(event.job_id);
      if (cell) cell.onJobDone(event.mean_plddt);
      state.jobToCard.delete(event.job_id);
      break;
    }
    case "job_error": {
      const cell = cellForJob(event.job_id);
      if (cell) cell.onJobError();
      state.jobToCard.delete(event.job_id);
      break;
    }
    case "card_state": {
      const cell = state.cells.get(event.card);
      if (cell) cell.onCardState(event.state);
      break;
    }
    case "answer_start": {
      qaPanel.hidden = false;
      qaQuestion.textContent = `Scoring affinity for ${event.target_id}…`;
      qaSpinner.hidden = false;
      qaScore.hidden = true;
      break;
    }
    case "answer_done": {
      qaPanel.hidden = false;
      qaQuestion.textContent = `Affinity result for ${event.target_id}`;
      qaSpinner.hidden = true;
      qaScore.hidden = false;
      const pct = Math.round(event.score * 100);
      qaScore.textContent = `${pct}% predicted probability of binding`;
      break;
    }
    case "answer_error": {
      qaPanel.hidden = false;
      qaQuestion.textContent = `Scoring failed for ${event.target_id}.`;
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
  const source = new EventSource("/events");
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
    const resp = await fetch("/pick", {
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
  const solo = stageEl.dataset.mode === "solo";
  stageEl.dataset.mode = solo ? "quad" : "solo";
  viewToggle.textContent = solo ? "Solo view" : "Quad view";
});
