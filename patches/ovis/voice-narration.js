// codex-workspace: speak agent replies aloud in the voiceapi default voice (README "Voice API").
// Loaded from dist/index.html before the app (scripts/ovis_patch.py); a file of its own because
// Ovis's CSP is script-src 'self'.
//
// It follows the app's own socket:
//   out  attach / watch / conversation-watch    a pane is being viewed: remember what is already there
//   out  detach / unwatch                       no longer viewed: stop narrating it
//   out  submit {pane_id}                       the user sent something: stop talking
//   in   conversation-changed {pane_id}         the transcript grew (attached browsers): speak new replies
//   in   pane-status {pane_id, agent_status}    every browser, watching ones included, which never get
//                                               conversation-changed: refresh, and poll while working
//   in   watch-data / pty-data {pane_id}        the screen redrew: refresh, throttled
// A status turns "done" a moment before Ovis's transcript read holds the final reply, so a finished
// turn is looked at again a few times (SETTLE_LOOKS_MS) rather than once.
// The server (server/voice-narration.ts) lists reply segments (assistant text only, never tool
// calls or reasoning) and streams each one's audio, keeping the voiceapi key to itself.
(function () {
  "use strict";

  const MODE_KEY = "codex-workspace:narration";
  // localStorage "codex-workspace:narration-debug" = "1" logs every socket message type seen.
  const DEBUG_KEY = "codex-workspace:narration-debug";
  const MODES = ["all", "final", "off"];
  const LABELS = { all: "Narrating replies", final: "Narrating final answers", off: "Narration off" };

  const WORKING_POLL_MS = 3000;
  const REDRAW_THROTTLE_MS = 1500;
  const SETTLE_LOOKS_MS = [1200, 3500, 8000];

  function createNarrator(deps) {
    const fetchJson = deps.fetchJson;
    const createAudio = deps.createAudio;
    const storage = deps.storage;
    const onState = deps.onState || function () {};
    const setTimer = deps.setInterval || function (fn, ms) { return setInterval(fn, ms); };
    const clearTimer = deps.clearInterval || function (id) { clearInterval(id); };
    const later = deps.setTimeout || function (fn, ms) { return setTimeout(fn, ms); };
    const lastRedraw = new Map(); // pane_id -> time of the last redraw-driven refresh
    const trailing = new Set(); // panes with a trailing redraw look scheduled
    const now = deps.now || function () { return Date.now(); };
    const pollers = new Map(); // pane_id -> interval id while its agent works
    const spoken = new Map(); // pane_id -> Set of segment ids already spoken or skipped
    const snapshots = new Map(); // pane_id -> the in-flight "what is already here" read
    const queue = [];
    let audio = null;
    let current = null;
    let unlocked = false;

    function mode() {
      let value = null;
      try { value = storage.getItem(MODE_KEY); } catch (_) { /* storage refused */ }
      return MODES.includes(value) ? value : "all";
    }

    function setMode(value) {
      try { storage.setItem(MODE_KEY, value); } catch (_) { /* storage refused */ }
      if (value === "off") stop();
      onState(state());
    }

    function cycleMode() {
      setMode(MODES[(MODES.indexOf(mode()) + 1) % MODES.length]);
    }

    function state() {
      return { mode: mode(), label: LABELS[mode()], speaking: current !== null, queued: queue.length };
    }

    function wanted(segment) {
      const m = mode();
      return m === "all" || (m === "final" && segment.phase !== "commentary");
    }

    async function list(paneId) {
      const body = await fetchJson("/api/narration/narration?pane_id=" + encodeURIComponent(paneId));
      return body && body.configured ? body.segments : [];
    }

    // A pane is being viewed: everything already in it counts as heard. Refreshes wait for this
    // read, or a status frame racing it would find an empty set and read the history aloud.
    function remember(paneId) {
      const known = spoken.get(paneId) || new Set();
      spoken.set(paneId, known);
      const snapshot = list(paneId)
        .then(function (segments) { for (const segment of segments) known.add(segment.id); })
        .catch(function () { /* not narratable now */ });
      snapshots.set(paneId, snapshot);
      return snapshot;
    }

    function forget(paneId) {
      spoken.delete(paneId);
      snapshots.delete(paneId);
      stopPolling(paneId);
    }

    function stopPolling(paneId) {
      if (pollers.has(paneId)) {
        clearTimer(pollers.get(paneId));
        pollers.delete(paneId);
      }
    }

    // A viewed pane's agent changed state: poll while it works so interim replies are spoken as
    // they appear; when it stops, take one last look and stop polling.
    function status(paneId, agentStatus) {
      if (!spoken.has(paneId)) return;
      if (agentStatus === "working") {
        if (!pollers.has(paneId)) pollers.set(paneId, setTimer(function () { refresh(paneId); }, WORKING_POLL_MS));
      } else {
        stopPolling(paneId);
        for (const delay of SETTLE_LOOKS_MS) later(function () { refresh(paneId); }, delay);
      }
      refresh(paneId);
    }

    // The pane's screen redrew (its reply rendering, a status line ticking): look, at most every
    // REDRAW_THROTTLE_MS, with one trailing look so the last redraw is never missed.
    function redraw(paneId) {
      if (!spoken.has(paneId)) return;
      const since = now() - (lastRedraw.get(paneId) || 0);
      if (since >= REDRAW_THROTTLE_MS) {
        lastRedraw.set(paneId, now());
        refresh(paneId);
      } else if (!trailing.has(paneId)) {
        trailing.add(paneId);
        later(function () {
          trailing.delete(paneId);
          lastRedraw.set(paneId, now());
          refresh(paneId);
        }, REDRAW_THROTTLE_MS - since);
      }
    }

    // The transcript may have grown: queue whatever reply text is new, oldest first. Only panes
    // this browser views are narrated.
    async function refresh(paneId) {
      if (mode() === "off" || !spoken.has(paneId)) return;
      await snapshots.get(paneId);
      const known = spoken.get(paneId);
      if (!known) return;
      let segments;
      try { segments = await list(paneId); } catch (_) { return; }
      for (const segment of segments) {
        if (known.has(segment.id)) continue;
        known.add(segment.id);
        if (wanted(segment)) queue.push({ paneId: paneId, id: segment.id });
      }
      onState(state());
      next();
    }

    function ensureAudio() {
      if (audio === null) {
        audio = createAudio();
        audio.addEventListener("ended", finished);
        audio.addEventListener("error", finished);
      }
      return audio;
    }

    function next() {
      if (current !== null || queue.length === 0 || mode() === "off") return;
      current = queue.shift();
      const player = ensureAudio();
      player.src = "/api/narration/speech?pane_id=" + encodeURIComponent(current.paneId) +
        "&segment=" + encodeURIComponent(current.id);
      const played = player.play();
      if (played && typeof played.catch === "function") played.catch(finished);
      onState(state());
    }

    function finished() {
      current = null;
      onState(state());
      next();
    }

    function stop() {
      queue.length = 0;
      if (audio !== null) {
        try { audio.pause(); } catch (_) { /* not playing */ }
        audio.removeAttribute ? audio.removeAttribute("src") : (audio.src = "");
      }
      current = null;
      onState(state());
    }

    function skip() {
      if (audio !== null) {
        try { audio.pause(); } catch (_) { /* not playing */ }
      }
      finished();
    }

    // Browsers allow audio only after a gesture; play a silent clip inside the first one.
    function unlock() {
      if (unlocked) return;
      unlocked = true;
      const player = ensureAudio();
      if (current !== null) return;
      player.src = "/api/narration/silence";
      const played = player.play();
      if (played && typeof played.catch === "function") played.catch(function () { unlocked = false; });
    }

    function outgoing(message) {
      if (!message || typeof message.pane_id !== "string") return;
      const pane = message.pane_id;
      const viewing = message.type === "attach" || message.type === "watch" ||
        (message.type === "conversation-watch" && message.enabled);
      if (viewing && !spoken.has(pane)) remember(pane);
      if (message.type === "detach" || message.type === "unwatch") forget(pane);
      if (message.type === "submit") stop();
    }

    function incoming(message) {
      if (!message || typeof message.pane_id !== "string") return;
      if (message.type === "conversation-changed") refresh(message.pane_id);
      if (message.type === "pane-status") status(message.pane_id, message.agent_status);
      if (message.type === "watch-data" || message.type === "pty-data") redraw(message.pane_id);
    }

    return { outgoing, incoming, refresh, remember, forget, stop, skip, unlock, cycleMode, setMode, state };
  }

  globalThis.__codexWorkspaceNarration = { createNarrator, MODE_KEY };
  if (typeof window === "undefined" || typeof document === "undefined") return;

  // ---- browser wiring -------------------------------------------------------------------

  let button = null;
  const narrator = createNarrator({
    fetchJson: async function (path) {
      const response = await fetch(path, { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    },
    createAudio: function () {
      const element = new Audio();
      element.preload = "auto";
      return element;
    },
    storage: window.localStorage,
    onState: function (state) { paint(state); },
  });

  let debugging = false;
  try { debugging = window.localStorage.getItem(DEBUG_KEY) === "1"; } catch (_) { /* storage refused */ }
  function trace(direction, message, raw) {
    if (!debugging) return;
    const kind = message ? message.type + (message.pane_id ? " " + message.pane_id : "") : "unparsed " + typeof raw +
      (typeof raw === "string" ? " " + JSON.stringify(raw.slice(0, 40)) : "");
    console.debug("[narration] " + direction + " " + kind);
  }

  function parse(data) {
    if (typeof data !== "string" || data.charCodeAt(0) !== 123) return null;
    try { return JSON.parse(data); } catch (_) { return null; }
  }

  const NativeSocket = window.WebSocket;
  class NarratedSocket extends NativeSocket {
    constructor(...args) {
      super(...args);
      this.addEventListener("message", function (event) {
        const message = parse(event.data);
        if (!message || (message.type !== "pty-data" && message.type !== "pong")) trace("in", message, event.data);
        narrator.incoming(message);
      });
    }
    send(data) {
      const message = parse(data);
      if (!message || message.type !== "pty-ack") trace("out", message, data);
      narrator.outgoing(message);
      return super.send(data);
    }
  }
  window.WebSocket = NarratedSocket;

  for (const type of ["pointerdown", "keydown", "touchstart"]) {
    window.addEventListener(type, function () { narrator.unlock(); }, { capture: true, passive: true });
  }

  // ---- the header chip -------------------------------------------------------------------
  // A chip in Ovis's header meta group (beside the connection pill and the More menu), drawn with
  // Ovis's own tokens so it follows its themes and palettes. Icons are Lucide's, as Ovis's are.

  const SVG_NS = "http://www.w3.org/2000/svg";
  const SPEAKER = "M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z";
  const ICONS = {
    all: [SPEAKER, "M16 9a5 5 0 0 1 0 6", "M19.364 18.364a9 9 0 0 0 0-12.728"],
    final: [SPEAKER, "M16 9a5 5 0 0 1 0 6"],
    off: [SPEAKER, "M22 9l-6 6", "M16 9l6 6"],
    speaking: ["M21 4v16", "M6.029 4.285A2 2 0 0 0 3 6v12a2 2 0 0 0 3.029 1.715l9.997-5.998a2 2 0 0 0 .003-3.432z"],
  };
  const SHORT = { all: "Replies", final: "Final answers", off: "Muted" };
  const CHIP = "cw-narration";
  const STYLE = [
    "." + CHIP + "{display:inline-flex;flex:none;align-items:center;gap:var(--space-1);height:var(--chip-h);" +
      "padding:0 var(--space-2);border:var(--hairline) solid transparent;border-radius:var(--radius-pill);" +
      "background:var(--bg-elevated);color:var(--text-dim);font:inherit;font-size:var(--fs-xs);white-space:nowrap;" +
      "cursor:pointer;transition:background-color var(--dur-fast) var(--ease-out),border-color var(--dur-fast) var(--ease-out),color var(--dur-fast) var(--ease-out)}",
    "." + CHIP + " svg{width:14px;height:14px;flex:none}",
    "." + CHIP + "[data-mode=off]{background:transparent;border-color:var(--border)}",
    "." + CHIP + "[data-speaking]{color:var(--accent);background:var(--accent-tint);border-color:var(--accent)}",
    "." + CHIP + ":focus-visible{outline:2px solid var(--accent);outline-offset:1px}",
    "@media (hover:hover){." + CHIP + ":hover{background:var(--bg-hover);color:var(--text)}" +
      "." + CHIP + "[data-speaking]:hover{background:var(--accent-tint);color:var(--accent)}}",
    "@media (pointer:coarse){." + CHIP + "{height:var(--touch-target-header);padding:0 var(--space-3);background:transparent}}",
    "@media (max-width:640px){." + CHIP + "-label{display:none}}",
    "@media (prefers-reduced-motion:reduce){." + CHIP + "{transition:none}}",
  ].join("");

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    for (const [key, value] of [["viewBox", "0 0 24 24"], ["fill", "none"], ["stroke", "currentColor"], ["stroke-width", "1.75"],
      ["stroke-linecap", "round"], ["stroke-linejoin", "round"], ["aria-hidden", "true"]]) svg.setAttribute(key, value);
    for (const d of ICONS[name]) {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      svg.appendChild(path);
    }
    return svg;
  }

  let painted = "";
  function paint(state) {
    if (button === null) return;
    const look = state.speaking ? "speaking" : state.mode;
    if (look !== painted) {
      painted = look;
      button.replaceChildren(icon(look), label);
    }
    label.textContent = state.speaking ? "Skip" : SHORT[state.mode];
    button.dataset.mode = state.mode;
    if (state.speaking) button.dataset.speaking = ""; else delete button.dataset.speaking;
    const help = state.speaking
      ? "Speaking · click to skip this reply · right-click to change mode"
      : state.label + " · click for: all replies → final answers → off";
    button.title = help;
    button.setAttribute("aria-label", help);
  }

  const label = document.createElement("span");
  label.className = CHIP + "-label";

  // React owns the header and replaces it on some view changes: put the chip back first in the
  // meta group whenever it is not there.
  function place() {
    if (button === null) return;
    const meta = document.querySelector(".app-header .header-meta");
    if (meta === null || (button.parentNode === meta && meta.firstChild === button)) return;
    meta.insertBefore(button, meta.firstChild);
  }

  function mount() {
    const style = document.createElement("style");
    style.id = "codex-workspace-narration-style";
    style.textContent = STYLE;
    document.head.appendChild(style);

    button = document.createElement("button");
    button.type = "button";
    button.className = CHIP;
    button.addEventListener("click", function () {
      if (narrator.state().speaking) narrator.skip(); else narrator.cycleMode();
    });
    button.addEventListener("contextmenu", function (event) { event.preventDefault(); narrator.cycleMode(); });
    narrator.setMode(narrator.state().mode);

    let queued = false;
    new MutationObserver(function () {
      if (queued) return;
      queued = true;
      requestAnimationFrame(function () { queued = false; place(); });
    }).observe(document.body, { childList: true, subtree: true });
    place();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
