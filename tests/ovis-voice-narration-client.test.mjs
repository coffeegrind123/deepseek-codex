// patches/ovis/voice-narration.js: the browser narrator's queue logic, without a browser.
import assert from "node:assert/strict";
import test from "node:test";

await import("../patches/ovis/voice-narration.js");
const { createNarrator, MODE_KEY } = globalThis.__codexWorkspaceNarration;

class FakeAudio {
  constructor() { this.listeners = {}; this.played = []; this.src = ""; this.paused = 0; }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  play() { this.played.push(this.src); return Promise.resolve(); }
  pause() { this.paused += 1; }
  removeAttribute(name) { if (name === "src") this.src = ""; }
  end() { for (const fn of this.listeners.ended ?? []) fn(); }
}

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return { getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); } };
}

function setup({ segments = [], storage = memoryStorage() } = {}) {
  const panes = { p1: segments };
  const audio = new FakeAudio();
  const narrator = createNarrator({
    fetchJson: async (path) => {
      const pane = new URL(path, "http://x").searchParams.get("pane_id");
      return { configured: true, segments: panes[pane] ?? [] };
    },
    createAudio: () => audio,
    storage,
  });
  return { narrator, audio, panes };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const speechOf = (src) => new URL(src, "http://x").searchParams.get("segment");

test("history present when a chat opens is never read", async () => {
  const { narrator, audio, panes } = setup({ segments: [{ id: "old1", phase: "final_answer" }] });
  narrator.outgoing({ type: "conversation-watch", pane_id: "p1", enabled: true });
  await settle();
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  await settle();
  assert.deepEqual(audio.played, []);
  panes.p1 = [...panes.p1, { id: "new1", phase: "commentary" }, { id: "new2", phase: "final_answer" }];
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  await settle();
  assert.deepEqual(audio.played.map(speechOf), ["new1"], "one at a time");
  audio.end();
  assert.deepEqual(audio.played.map(speechOf), ["new1", "new2"], "then the next, in order");
});

test("panes this browser never viewed are not narrated", async () => {
  const { narrator, audio, panes } = setup({ segments: [{ id: "a", phase: "final_answer" }] });
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  narrator.incoming({ type: "pane-status", pane_id: "p1", agent_status: "idle" });
  await settle();
  panes.p1 = [...panes.p1, { id: "b", phase: "final_answer" }];
  narrator.incoming({ type: "pane-status", pane_id: "p1", agent_status: "idle" });
  await settle();
  assert.deepEqual(audio.played, []);
});

test("a watching browser (no conversation-changed) narrates from pane-status", async () => {
  const { narrator, audio, panes } = setup({ segments: [{ id: "old", phase: "final_answer" }] });
  narrator.outgoing({ type: "watch", pane_id: "p1", cols: 80, rows: 24 });
  await settle();
  panes.p1 = [...panes.p1, { id: "reply", phase: "final_answer" }];
  narrator.incoming({ type: "pane-status", pane_id: "p1", agent_status: "done" });
  await settle();
  assert.deepEqual(audio.played.map(speechOf), ["reply"]);
});

test("while the agent works the pane is polled, and polling stops when it is done", async () => {
  const timers = new Map();
  let nextId = 1;
  const panes = { p1: [] };
  const audio = new FakeAudio();
  const narrator = createNarrator({
    fetchJson: async (path) => ({ configured: true, segments: panes[new URL(path, "http://x").searchParams.get("pane_id")] }),
    createAudio: () => audio,
    storage: memoryStorage(),
    setInterval: (fn) => { const id = nextId++; timers.set(id, fn); return id; },
    clearInterval: (id) => timers.delete(id),
  });
  narrator.outgoing({ type: "attach", pane_id: "p1", cols: 80, rows: 24 });
  await settle();
  narrator.incoming({ type: "pane-status", pane_id: "p1", agent_status: "working" });
  narrator.incoming({ type: "pane-status", pane_id: "p1", agent_status: "working" });
  await settle();
  assert.equal(timers.size, 1, "one poller per pane");
  panes.p1 = [{ id: "interim", phase: "commentary" }];
  for (const fn of timers.values()) fn();
  await settle();
  assert.deepEqual(audio.played.map(speechOf), ["interim"], "interim update spoken mid-turn");
  narrator.incoming({ type: "pane-status", pane_id: "p1", agent_status: "idle" });
  await settle();
  assert.equal(timers.size, 0);
});

test("a status frame racing the first snapshot never reads history", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const audio = new FakeAudio();
  const narrator = createNarrator({
    // the first read (the snapshot) is slow; the racing refresh's read answers at once
    fetchJson: (() => {
      let calls = 0;
      return async () => {
        calls += 1;
        if (calls === 1) await gate;
        return { configured: true, segments: [{ id: "history", phase: "final_answer" }] };
      };
    })(),
    createAudio: () => audio,
    storage: memoryStorage(),
  });
  narrator.outgoing({ type: "watch", pane_id: "p1" });
  narrator.incoming({ type: "pane-status", pane_id: "p1", agent_status: "idle" });
  await settle();
  await settle();
  release();
  await settle();
  await settle();
  assert.deepEqual(audio.played, []);
});

test("unwatching a pane stops narrating it", async () => {
  const { narrator, audio, panes } = setup();
  narrator.outgoing({ type: "watch", pane_id: "p1" });
  await settle();
  narrator.outgoing({ type: "unwatch", pane_id: "p1" });
  panes.p1 = [{ id: "late", phase: "final_answer" }];
  narrator.incoming({ type: "pane-status", pane_id: "p1", agent_status: "idle" });
  await settle();
  assert.deepEqual(audio.played, []);
});

test("each reply is spoken once even when the transcript changes again", async () => {
  const { narrator, audio, panes } = setup();
  await narrator.remember("p1");
  panes.p1 = [{ id: "x", phase: "final_answer" }];
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  await settle();
  audio.end();
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  await settle();
  assert.deepEqual(audio.played.map(speechOf), ["x"]);
});

test("final-answers mode skips commentary; off speaks nothing", async () => {
  const storage = memoryStorage({ [MODE_KEY]: "final" });
  const { narrator, audio, panes } = setup({ storage });
  await narrator.remember("p1");
  panes.p1 = [{ id: "c", phase: "commentary" }, { id: "f", phase: "final_answer" }];
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  await settle();
  assert.deepEqual(audio.played.map(speechOf), ["f"]);
  narrator.setMode("off");
  panes.p1 = [...panes.p1, { id: "g", phase: "final_answer" }];
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  await settle();
  assert.deepEqual(audio.played.map(speechOf), ["f"]);
});

test("sending a message stops narration and drops the queue", async () => {
  const { narrator, audio, panes } = setup();
  await narrator.remember("p1");
  panes.p1 = [{ id: "1", phase: "final_answer" }, { id: "2", phase: "final_answer" }];
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  await settle();
  narrator.outgoing({ type: "submit", id: 1, pane_id: "p1", text: "next", payload: "next" });
  assert.equal(audio.paused, 1);
  assert.equal(narrator.state().queued, 0);
  audio.end();
  assert.deepEqual(audio.played.map(speechOf), ["1"]);
});

test("skip moves to the next reply", async () => {
  const { narrator, audio, panes } = setup();
  await narrator.remember("p1");
  panes.p1 = [{ id: "1", phase: "final_answer" }, { id: "2", phase: "final_answer" }];
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  await settle();
  narrator.skip();
  assert.deepEqual(audio.played.map(speechOf), ["1", "2"]);
});

test("the first gesture plays the silent unlock clip once", () => {
  const { narrator, audio } = setup();
  narrator.unlock();
  narrator.unlock();
  assert.deepEqual(audio.played, ["/api/narration/silence"]);
});

test("mode defaults to all and cycles all -> final -> off", () => {
  const { narrator } = setup();
  assert.equal(narrator.state().mode, "all");
  narrator.cycleMode();
  assert.equal(narrator.state().mode, "final");
  narrator.cycleMode();
  assert.equal(narrator.state().mode, "off");
  narrator.cycleMode();
  assert.equal(narrator.state().mode, "all");
});

test("unconfigured server narrates nothing", async () => {
  const audio = new FakeAudio();
  const narrator = createNarrator({
    fetchJson: async () => ({ configured: false, segments: [] }),
    createAudio: () => audio,
    storage: memoryStorage(),
  });
  await narrator.remember("p1");
  narrator.incoming({ type: "conversation-changed", pane_id: "p1" });
  await settle();
  assert.deepEqual(audio.played, []);
});

test("a reply that lands after the done status is still spoken (settle looks)", async () => {
  const later = [];
  const panes = { p1: [] };
  const audio = new FakeAudio();
  const narrator = createNarrator({
    fetchJson: async (path) => ({ configured: true, segments: panes[new URL(path, "http://x").searchParams.get("pane_id")] }),
    createAudio: () => audio,
    storage: memoryStorage(),
    setTimeout: (fn, ms) => later.push({ fn, ms }),
  });
  narrator.outgoing({ type: "watch", pane_id: "p1" });
  await settle();
  narrator.incoming({ type: "pane-status", pane_id: "p1", agent_status: "done" });
  await settle();
  assert.deepEqual(audio.played, [], "the transcript does not hold the reply yet");
  panes.p1 = [{ id: "late", phase: "final_answer" }];
  assert.ok(later.length >= 2, "follow-up looks scheduled");
  later[0].fn();
  await settle();
  assert.deepEqual(audio.played.map(speechOf), ["late"]);
});

test("screen redraws refresh, throttled, with a trailing look", async () => {
  let clock = 0;
  const later = [];
  const reads = [];
  const narrator = createNarrator({
    fetchJson: async (path) => { reads.push(path); return { configured: true, segments: [] }; },
    createAudio: () => new FakeAudio(),
    storage: memoryStorage(),
    setTimeout: (fn, ms) => later.push({ fn, ms }),
    now: () => clock,
  });
  narrator.outgoing({ type: "watch", pane_id: "p1" });
  await settle();
  const before = reads.length;
  clock = 5000;
  narrator.incoming({ type: "watch-data", pane_id: "p1", data: "x" });
  narrator.incoming({ type: "watch-data", pane_id: "p1", data: "y" });
  narrator.incoming({ type: "watch-data", pane_id: "p1", data: "z" });
  await settle();
  assert.equal(reads.length - before, 1, "one immediate look");
  assert.equal(later.length, 1, "one trailing look, not one per redraw");
  later[0].fn();
  await settle();
  assert.equal(reads.length - before, 2);
});
