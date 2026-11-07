import assert from "node:assert/strict";
import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { resolveAudioFile } from "../lib/voiceapi.mjs";
import { createMcpMessageHandler, TOOLS } from "../mcp/server.mjs";

const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const API_KEY = "voiceapi-test-key-that-must-not-leak";

/**
 * A stand-in for the voiceapi gateway with the same routes and response shapes:
 * multipart upload, voice create + job polling, speech, transcription, enhance.
 */
async function fakeGateway({ buildOutcome = "succeeded" } = {}) {
  const calls = [];
  const jobs = new Map();
  const state = { defaultVoice: null };
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    const url = new URL(req.url, "http://x");
    calls.push({ method: req.method, path: url.pathname, query: url.search, auth: req.headers.authorization,
      type: req.headers["content-type"] ?? "", body });
    const send = (status, payload, type = "application/json") => {
      res.writeHead(status, { "Content-Type": type });
      res.end(type === "application/json" ? JSON.stringify(payload) : payload);
    };
    if (req.headers.authorization !== `Bearer ${API_KEY}`) {
      return send(401, { code: "unauthorized", detail: "invalid API key" });
    }
    if (req.method === "POST" && url.pathname === "/v1/files") {
      const name = /filename="([^"]+)"/.exec(body.toString("latin1"))?.[1];
      return send(201, { id: `file_${calls.length}`, filename: name, bytes: body.length });
    }
    if (req.method === "POST" && url.pathname === "/v1/voices") {
      const input = JSON.parse(body.toString());
      if (input.name === "Taken") {
        return send(409, { code: "voice_name_taken", detail: "a voice named 'Taken' exists", hint: "Pick another name." });
      }
      const voice = { id: "voice_01", name: input.name, status: "building", supports_icl: false, reference: null };
      const job = { id: "job_01", object: "job", status: "running", resource_id: voice.id };
      jobs.set(job.id, { polls: 0, voice, input });
      return send(202, { voice, job });
    }
    if (req.method === "GET" && url.pathname.startsWith("/v1/jobs/")) {
      const entry = jobs.get(url.pathname.split("/").pop());
      entry.polls += 1;
      if (entry.polls < 2) {
        return send(202, { id: "job_01", object: "job", status: "running" });
      }
      if (buildOutcome === "failed") {
        return send(200, { id: "job_01", object: "job", status: "failed",
          error: { code: "audio_too_short", message: "no clip has 3 s of speech", hint: "Upload a longer clip." } });
      }
      if (entry.input.make_default) state.defaultVoice = "voice_01";
      const ready = { ...entry.voice, status: "ready", supports_icl: true, is_default: state.defaultVoice === "voice_01",
        reference: { duration_s: 9.4, transcript: "hello this is me", file_id: "file_ref" },
        quality: { snr_before_db: 12.1, snr_after_db: 31.7 } };
      return send(200, { id: "job_01", object: "job", status: "succeeded", result: { voice: ready } });
    }
    if (req.method === "POST" && url.pathname === "/v1/audio/speech") {
      return send(200, Buffer.from("ID3fakeaudio"), "audio/mpeg");
    }
    if (req.method === "POST" && url.pathname === "/v1/audio/transcriptions") {
      return send(200, { text: "hello world", language: "English", duration: 1.5, segments: [] });
    }
    if (req.method === "POST" && url.pathname === "/v1/audio/enhance") {
      return send(200, { file: { id: "file_clean" }, metrics: { snr_before_db: 8, snr_after_db: 30 } });
    }
    if (req.method === "GET" && url.pathname === "/v1/files/file_clean/content") {
      return send(200, Buffer.from("RIFFclean"), "audio/wav");
    }
    if (url.pathname === "/v1/settings") {
      if (req.method === "PATCH") {
        const wanted = JSON.parse(body.toString()).default_voice;
        if (wanted !== null && wanted !== "Narrator" && wanted !== "voice_01") {
          return send(404, { code: "voice_not_found", detail: `voice '${wanted}' not found`, hint: "Use list_voices." });
        }
        state.defaultVoice = wanted === null ? null : "voice_01";
      }
      return send(200, { default_voice: state.defaultVoice ? { id: "voice_01", name: "Narrator", is_default: true } : null });
    }
    if (req.method === "GET" && url.pathname === "/v1/voices") {
      return send(200, { object: "list", data: [{ id: "voice_01", name: "Narrator", status: "ready" }] });
    }
    return send(404, { code: "not_found", detail: `no route ${req.method} ${url.pathname}` });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { url: `http://127.0.0.1:${server.address().port}`, calls, state, close: () => server.close() };
}

async function audioFile(name = "memo.m4a", bytes = 2048) {
  const dir = await mkdtemp(path.join(os.tmpdir(), "voiceapi-test-"));
  const file = path.join(dir, ".ovis", name);
  await writeFile(path.join(dir, "placeholder"), "");
  await import("node:fs/promises").then((fs) => fs.mkdir(path.dirname(file), { recursive: true }));
  await writeFile(file, Buffer.alloc(bytes, 7));
  return { dir, file };
}

function handler(gateway, extraEnv = {}) {
  return createMcpMessageHandler({
    env: { VOICEAPI_KEY: API_KEY, VOICEAPI_URL: gateway.url, ...extraEnv },
    sleep: async () => {},
  });
}

async function call(handle, name, args) {
  const response = await handle({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } });
  return response.result;
}

test("manifest and MCP wiring pass the key through env_vars", async () => {
  const manifest = JSON.parse(await readFile(path.join(PLUGIN_ROOT, ".codex-plugin/plugin.json"), "utf8"));
  const mcp = JSON.parse(await readFile(path.join(PLUGIN_ROOT, ".mcp.json"), "utf8"));
  const server = mcp.mcpServers.voiceapi;
  assert.equal(manifest.name, "voiceapi");
  assert.equal(manifest.mcpServers, "./.mcp.json");
  assert.deepEqual(server.args, ["./mcp/server.mjs"]);
  assert.ok(server.env_vars.includes("VOICEAPI_KEY"));
  assert.ok(server.tool_timeout_sec >= 900, "a voice build can take minutes");
});

test("tools/list exposes the voice tools with closed schemas", async () => {
  const response = await handler({ url: "http://127.0.0.1:1" })({ jsonrpc: "2.0", id: 1, method: "tools/list" });
  const names = response.result.tools.map((tool) => tool.name);
  assert.deepEqual(names, TOOLS.map((tool) => tool.name));
  assert.ok(names.includes("create_voice_from_file") && names.includes("speak"));
  for (const tool of response.result.tools) {
    assert.equal(tool.inputSchema.additionalProperties, false, tool.name);
  }
});

test("create_voice_from_file uploads the dropped file, waits for the build, reports the voice", async () => {
  const gateway = await fakeGateway();
  try {
    const { file } = await audioFile();
    const result = await call(handler(gateway), "create_voice_from_file",
      { paths: [file], name: "Narrator" });
    assert.equal(result.isError, false, JSON.stringify(result));
    assert.equal(result.structuredContent.status, "ready");
    assert.match(result.content[0].text, /Voice "Narrator" is ready/);
    assert.match(result.content[0].text, /12.1 dB -> 31.7 dB/);

    const upload = gateway.calls.find((c) => c.path === "/v1/files");
    assert.match(upload.type, /^multipart\/form-data/);
    assert.match(upload.body.toString("latin1"), /filename="memo\.m4a"/);
    const create = JSON.parse(gateway.calls.find((c) => c.path === "/v1/voices").body.toString());
    assert.equal("consent" in create, false);
    assert.equal(create.make_default, true, "a chat-created voice becomes the persistent default");
    assert.match(result.content[0].text, /now the default voice for every session/);
    assert.equal(create.clean, true);
    assert.equal(create.file_ids.length, 1);
    assert.match(create.file_ids[0], /^file_\d+$/);
    assert.ok(gateway.calls.filter((c) => c.path === "/v1/jobs/job_01").length >= 2, "polled until done");
    assert.ok(gateway.calls.every((c) => c.auth === `Bearer ${API_KEY}`));
    assert.ok(!JSON.stringify(result).includes(API_KEY), "the key never reaches the model");
  } finally {
    gateway.close();
  }
});

test("a failed build surfaces the gateway's code and hint", async () => {
  const gateway = await fakeGateway({ buildOutcome: "failed" });
  try {
    const { file } = await audioFile();
    const result = await call(handler(gateway), "create_voice_from_file", { paths: [file], name: "Short" });
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /^AUDIO_TOO_SHORT: .*Hint: Upload a longer clip\./);
  } finally {
    gateway.close();
  }
});

test("gateway problem documents become actionable tool errors", async () => {
  const gateway = await fakeGateway();
  try {
    const { file } = await audioFile();
    const result = await call(handler(gateway), "create_voice_from_file", { paths: [file], name: "Taken" });
    assert.match(result.content[0].text, /^VOICE_NAME_TAKEN: .*Hint: Pick another name\./);
  } finally {
    gateway.close();
  }
});

test("non-audio and credential files are refused locally, before any upload", async () => {
  const gateway = await fakeGateway();
  try {
    const { dir } = await audioFile();
    const notes = path.join(dir, "notes.txt");
    const secrets = path.join(dir, "secrets.env");
    await writeFile(notes, "hi");
    await writeFile(secrets, "VOICEAPI_KEY=x");
    for (const [target, code] of [[notes, "UNSUPPORTED_FILE"], [secrets, "REFUSED"], ["relative.wav", "INVALID_ARGUMENTS"],
      [path.join(dir, "missing.wav"), "FILE_NOT_FOUND"]]) {
      const result = await call(handler(gateway), "create_voice_from_file", { paths: [target], name: "N" });
      assert.match(result.content[0].text, new RegExp(`^${code}:`), target);
    }
    assert.equal(gateway.calls.length, 0);
  } finally {
    gateway.close();
  }
});

test("a symlinked credential file is judged by its real path", async () => {
  const { dir } = await audioFile();
  const secret = path.join(dir, ".env");
  await writeFile(secret, "TOKEN=x");
  const link = path.join(dir, "innocent.wav");
  await import("node:fs/promises").then((fs) => fs.symlink(secret, link));
  await assert.rejects(resolveAudioFile(link), (error) => error.code === "REFUSED" || error.code === "UNSUPPORTED_FILE");
});

test("speak saves the audio where asked and returns the path", async () => {
  const gateway = await fakeGateway();
  try {
    const { dir } = await audioFile();
    const out = path.join(dir, ".ovis", "hello.mp3");
    const result = await call(handler(gateway), "speak", { text: "Hello there.", voice: "Narrator", out_path: out });
    assert.equal(result.isError, false, JSON.stringify(result));
    assert.equal(result.structuredContent.path, out);
    assert.equal((await readFile(out)).toString(), "ID3fakeaudio");
    const sent = JSON.parse(gateway.calls.find((c) => c.path === "/v1/audio/speech").body.toString());
    assert.deepEqual(sent, { input: "Hello there.", voice: "Narrator", response_format: "mp3" });
  } finally {
    gateway.close();
  }
});

test("speak defaults to a file under VOICEAPI_OUTPUT_DIR", async () => {
  const gateway = await fakeGateway();
  try {
    const out = await mkdtemp(path.join(os.tmpdir(), "voiceapi-out-"));
    const result = await call(handler(gateway, { VOICEAPI_OUTPUT_DIR: out }), "speak", { text: "Hi", voice: "N", format: "opus" });
    assert.equal(result.isError, false, JSON.stringify(result));
    assert.equal(path.dirname(result.structuredContent.path), out);
    assert.match(result.structuredContent.path, /speech-\d+\.ogg$/);
    assert.ok((await stat(result.structuredContent.path)).size > 0);
  } finally {
    gateway.close();
  }
});

test("transcribe, enhance and list round-trip through the gateway", async () => {
  const gateway = await fakeGateway();
  try {
    const { file } = await audioFile("talk.wav");
    const transcript = await call(handler(gateway), "transcribe_file", { path: file, language: "en" });
    assert.equal(transcript.content[0].text, "hello world");
    const cleaned = await call(handler(gateway), "enhance_file", { path: file });
    assert.match(cleaned.structuredContent.path, /talk-clean\.wav$/);
    assert.equal((await readFile(cleaned.structuredContent.path)).toString(), "RIFFclean");
    const voices = await call(handler(gateway), "list_voices", {});
    assert.match(voices.content[0].text, /Narrator \(voice_01\)/);
  } finally {
    gateway.close();
  }
});

test("missing key and unreachable gateway explain themselves", async () => {
  const noKey = createMcpMessageHandler({ env: {} });
  const missing = await call(noKey, "list_voices", {});
  assert.match(missing.content[0].text, /^CONFIG: VOICEAPI_KEY is not set\..*secrets\.env/);
  const down = await call(handler({ url: "http://127.0.0.1:1" }), "list_voices", {});
  assert.match(down.content[0].text, /^UNREACHABLE: cannot reach voiceapi/);
});

test("make_default=false leaves the default alone", async () => {
  const gateway = await fakeGateway();
  try {
    const { file } = await audioFile();
    await call(handler(gateway), "create_voice_from_file", { paths: [file], name: "Extra", make_default: false });
    const create = JSON.parse(gateway.calls.find((c) => c.path === "/v1/voices").body.toString());
    assert.equal(create.make_default, false);
    assert.equal(gateway.state.defaultVoice, null);
  } finally {
    gateway.close();
  }
});

test("speak without a voice relies on the gateway default", async () => {
  const gateway = await fakeGateway();
  try {
    const { dir } = await audioFile();
    const result = await call(handler(gateway), "speak", { text: "Default please.", out_path: path.join(dir, "d.mp3") });
    assert.equal(result.isError, false, JSON.stringify(result));
    const sent = JSON.parse(gateway.calls.find((c) => c.path === "/v1/audio/speech").body.toString());
    assert.equal("voice" in sent, false);
  } finally {
    gateway.close();
  }
});

test("a fresh session can read, change and turn off the default", async () => {
  const gateway = await fakeGateway();
  try {
    // A new handler per call mirrors new sessions: no client-side memory, state lives in the gateway.
    const none = await call(handler(gateway), "get_default_voice", {});
    assert.equal(none.content[0].text, "No default voice is set.");
    const set = await call(handler(gateway), "set_default_voice", { voice: "Narrator" });
    assert.match(set.content[0].text, /Default voice is now Narrator \(voice_01\)/);
    const later = await call(handler(gateway), "get_default_voice", {});
    assert.match(later.content[0].text, /Default voice: Narrator/);
    const off = await call(handler(gateway), "set_default_voice", { voice: null });
    assert.equal(off.content[0].text, "Default voice turned off.");
    const unknown = await call(handler(gateway), "set_default_voice", { voice: "nobody" });
    assert.match(unknown.content[0].text, /^VOICE_NOT_FOUND: .*Hint: Use list_voices\./);
  } finally {
    gateway.close();
  }
});
