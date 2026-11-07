// voiceapi client: local files in, voices and audio out.
//
// The MCP server runs next to Codex, inside the cage, so it can read files Ovis drops into
// `<pane cwd>/.ovis/`. The voiceapi gateway runs in another container and cannot. This module
// streams such a file to the gateway (POST /v1/files), then drives the gateway's own API.
// Audio bytes never pass through the model's context.

import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";

export const DEFAULT_URL = "http://host.docker.internal:8080";
export const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;
export const BUILD_WAIT_SECONDS = 55;
export const BUILD_TIMEOUT_SECONDS = 900;

// What the gateway's ffmpeg decodes. A path outside this list is refused before anything is
// read, so a confused or injected prompt cannot ship an arbitrary local file to the gateway.
export const AUDIO_EXTENSIONS = new Set([
  "wav", "mp3", "flac", "ogg", "oga", "opus", "m4a", "mp4", "aac", "webm", "mka", "mkv", "mov", "wma", "aiff",
  "aif", "caf",
]);

const OUTPUT_FORMATS = new Set(["mp3", "opus", "aac", "flac", "wav", "pcm"]);
const FORMAT_EXTENSIONS = { opus: "ogg" };
const SECRET_PATH = /(^|\/)(\.ssh|\.gnupg|\.aws|\.config\/gh)(\/|$)|(^|\/)\.?secrets?[^/]*$|\.env$|(^|\/)\.env\./i;

export class VoiceApiError extends Error {
  constructor(code, message, hint) {
    super(message);
    this.name = "VoiceApiError";
    this.code = code;
    this.hint = hint;
  }
}

export function settings(env = process.env) {
  const baseUrl = (env.VOICEAPI_URL || DEFAULT_URL).replace(/\/+$/, "");
  if (!/^https?:\/\//.test(baseUrl)) {
    throw new VoiceApiError("CONFIG", `VOICEAPI_URL must be an http(s) URL, got ${JSON.stringify(baseUrl)}`);
  }
  const apiKey = env.VOICEAPI_KEY || "";
  if (!apiKey) {
    throw new VoiceApiError(
      "CONFIG",
      "VOICEAPI_KEY is not set.",
      "Add VOICEAPI_KEY (and VOICEAPI_URL if the gateway is not at the default) to .codex/secrets.env, then restart Codex.",
    );
  }
  return { baseUrl, apiKey };
}

/** Resolve and vet a local audio file the agent wants to send. */
export async function resolveAudioFile(rawPath, { maxBytes = MAX_UPLOAD_BYTES } = {}) {
  if (typeof rawPath !== "string" || rawPath.trim() === "") {
    throw new VoiceApiError("INVALID_ARGUMENTS", "path must be a non-empty string.");
  }
  if (!path.isAbsolute(rawPath)) {
    throw new VoiceApiError(
      "INVALID_ARGUMENTS",
      `path must be absolute, got ${JSON.stringify(rawPath)}.`,
      "Use the full path Ovis put in the message (it ends in /.ovis/<file>).",
    );
  }
  let real;
  try {
    real = await fsp.realpath(rawPath);
  } catch {
    throw new VoiceApiError("FILE_NOT_FOUND", `no such file: ${rawPath}`);
  }
  if (SECRET_PATH.test(real)) {
    throw new VoiceApiError("REFUSED", `refusing to upload ${real}: it looks like a credentials file.`);
  }
  const extension = path.extname(real).slice(1).toLowerCase();
  if (!AUDIO_EXTENSIONS.has(extension)) {
    throw new VoiceApiError(
      "UNSUPPORTED_FILE",
      `${path.basename(real)} is not an audio or video file (.${extension || "?"}).`,
      `Supported: ${[...AUDIO_EXTENSIONS].join(", ")}.`,
    );
  }
  const stat = await fsp.stat(real);
  if (!stat.isFile()) {
    throw new VoiceApiError("FILE_NOT_FOUND", `${real} is not a regular file.`);
  }
  if (stat.size === 0) {
    throw new VoiceApiError("UNSUPPORTED_FILE", `${real} is empty.`);
  }
  if (stat.size > maxBytes) {
    throw new VoiceApiError("FILE_TOO_LARGE", `${real} is ${stat.size} bytes; the limit is ${maxBytes}.`);
  }
  return { path: real, name: path.basename(real), size: stat.size };
}

export function createClient({ env = process.env, fetchImpl = globalThis.fetch, sleep } = {}) {
  const { baseUrl, apiKey } = settings(env);
  const pause = sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));

  async function request(method, route, { json, body, headers = {}, expect = [200] } = {}) {
    let response;
    try {
      response = await fetchImpl(`${baseUrl}${route}`, {
        method,
        headers: {
          Authorization: `Bearer ${apiKey}`,
          ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
          ...headers,
        },
        body: json !== undefined ? JSON.stringify(json) : body,
      });
    } catch (error) {
      throw new VoiceApiError(
        "UNREACHABLE",
        `cannot reach voiceapi at ${baseUrl}: ${error?.cause?.code ?? error?.message ?? error}`,
        "Is the voiceapi stack running (docker compose ps in the voiceapi repo)? Under CODEX_NET=restricted only the router port is reachable.",
      );
    }
    if (expect.includes(response.status)) {
      return response;
    }
    let problem = {};
    try {
      problem = await response.json();
    } catch {
      problem = { detail: await response.text().catch(() => "") };
    }
    throw new VoiceApiError(
      String(problem.code ?? `HTTP_${response.status}`).toUpperCase(),
      problem.detail ?? `voiceapi returned HTTP ${response.status}`,
      problem.hint,
    );
  }

  async function uploadFile(file) {
    const form = new FormData();
    form.append("file", await fs.openAsBlob(file.path), file.name);
    const response = await request("POST", "/v1/files", { body: form, expect: [201] });
    return response.json();
  }

  async function waitForJob(job, { timeoutSeconds = BUILD_TIMEOUT_SECONDS } = {}) {
    const deadline = Date.now() + timeoutSeconds * 1000;
    let current = job;
    while (!["succeeded", "failed", "cancelled"].includes(current.status)) {
      if (Date.now() > deadline) {
        throw new VoiceApiError(
          "TIMEOUT",
          `job ${current.id} still ${current.status} after ${timeoutSeconds}s.`,
          `Check it later with get_job("${current.id}").`,
        );
      }
      const response = await request("GET", `/v1/jobs/${current.id}?wait=${BUILD_WAIT_SECONDS}`, {
        expect: [200, 202],
      });
      current = await response.json();
      if (current.status === "queued") {
        await pause(1000);
      }
    }
    return current;
  }

  return {
    baseUrl,

    async createVoiceFromFiles({ paths, name, transcript, language, clean = true, mode = "auto",
      description = "", makeDefault = true }) {
      const list = Array.isArray(paths) ? paths : [paths];
      if (list.length === 0 || list.length > 20) {
        throw new VoiceApiError("INVALID_ARGUMENTS", "give 1 to 20 audio files.");
      }
      const files = [];
      for (const raw of list) {
        files.push(await resolveAudioFile(raw));
      }
      const uploaded = [];
      for (const file of files) {
        uploaded.push(await uploadFile(file));
      }
      const created = await request("POST", "/v1/voices?wait=5", {
        json: {
          name,
          file_ids: uploaded.map((item) => item.id),
          description,
          clean,
          mode,
          make_default: makeDefault,
          language: language || "auto",
          ...(transcript ? { transcript } : {}),
        },
        expect: [200, 202],
      });
      const { voice, job } = await created.json();
      const finished = await waitForJob(job);
      if (finished.status !== "succeeded") {
        const error = finished.error ?? {};
        throw new VoiceApiError(
          String(error.code ?? "BUILD_FAILED").toUpperCase(),
          `building voice "${name}" failed: ${error.message ?? finished.status}`,
          error.hint,
        );
      }
      return { voice: finished.result?.voice ?? voice, uploads: uploaded.map((item) => item.id) };
    },

    async speakToFile({ text, voice, format = "mp3", outPath, language, voiceMode, speed }) {
      if (!OUTPUT_FORMATS.has(format)) {
        throw new VoiceApiError("INVALID_ARGUMENTS", `format must be one of ${[...OUTPUT_FORMATS].join(", ")}.`);
      }
      const target = await outputPath(outPath, format, env);
      const response = await request("POST", "/v1/audio/speech", {
        json: {
          input: text,
          ...(voice ? { voice } : {}),
          response_format: format,
          ...(language ? { language } : {}),
          ...(voiceMode ? { voice_mode: voiceMode } : {}),
          ...(speed ? { speed } : {}),
        },
      });
      const bytes = Buffer.from(await response.arrayBuffer());
      await fsp.writeFile(target, bytes);
      return { path: target, bytes: bytes.length, format };
    },

    async transcribeFile({ path: rawPath, language, prompt, wordTimestamps = false }) {
      const file = await resolveAudioFile(rawPath);
      const form = new FormData();
      form.append("file", await fs.openAsBlob(file.path), file.name);
      form.append("response_format", "verbose_json");
      if (language) form.append("language", language);
      if (prompt) form.append("prompt", prompt);
      if (wordTimestamps) form.append("timestamp_granularities[]", "word");
      const response = await request("POST", "/v1/audio/transcriptions", { body: form });
      return response.json();
    },

    async enhanceFile({ path: rawPath, outPath }) {
      const file = await resolveAudioFile(rawPath);
      const uploaded = await uploadFile(file);
      const started = await request("POST", "/v1/audio/enhance?wait=55", {
        json: { file_id: uploaded.id },
        expect: [200, 202],
      });
      const body = await started.json();
      let fileId = body.file?.id;
      let metrics = body.metrics;
      if (!fileId && body.object === "job") {
        const finished = await waitForJob(body);
        if (finished.status !== "succeeded") {
          throw new VoiceApiError("ENHANCE_FAILED", finished.error?.message ?? finished.status, finished.error?.hint);
        }
        fileId = finished.result.file_id;
        metrics = finished.result.metrics;
      }
      const target = await outputPath(
        outPath ?? path.join(path.dirname(file.path), `${path.parse(file.name).name}-clean.wav`),
        "wav",
        env,
      );
      const audio = await request("GET", `/v1/files/${fileId}/content`);
      await fsp.writeFile(target, Buffer.from(await audio.arrayBuffer()));
      return { path: target, metrics };
    },

    async listVoices({ status = "ready", limit = 50 } = {}) {
      const query = new URLSearchParams({ limit: String(limit), ...(status ? { status } : {}) });
      const response = await request("GET", `/v1/voices?${query}`);
      return (await response.json()).data;
    },

    async deleteVoice(voice) {
      await request("DELETE", `/v1/voices/${encodeURIComponent(voice)}`, { expect: [204] });
      return { deleted: voice };
    },

    async getDefaultVoice() {
      const response = await request("GET", "/v1/settings");
      return (await response.json()).default_voice;
    },

    async setDefaultVoice(voice) {
      const response = await request("PATCH", "/v1/settings", { json: { default_voice: voice ?? null } });
      return (await response.json()).default_voice;
    },

    async getJob(jobId) {
      const response = await request("GET", `/v1/jobs/${encodeURIComponent(jobId)}`, { expect: [200, 202] });
      return response.json();
    },
  };
}

async function outputPath(outPath, format, env = process.env) {
  const extension = FORMAT_EXTENSIONS[format] ?? format;
  const target = outPath
    ? path.resolve(outPath)
    : path.join(env.VOICEAPI_OUTPUT_DIR || path.join(env.TMPDIR || "/tmp", "voiceapi"),
      `speech-${Date.now()}.${extension}`);
  if (SECRET_PATH.test(target)) {
    throw new VoiceApiError("REFUSED", `refusing to write to ${target}.`);
  }
  await fsp.mkdir(path.dirname(target), { recursive: true });
  return target;
}
