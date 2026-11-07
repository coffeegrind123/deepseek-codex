#!/usr/bin/env node

import path from "node:path";
import readline from "node:readline";
import { pathToFileURL } from "node:url";

import { createClient, VoiceApiError } from "../lib/voiceapi.mjs";

const SERVER_NAME = "voiceapi";
const SERVER_VERSION = "0.1.0";
const DEFAULT_PROTOCOL_VERSION = "2024-11-05";

const PATH_HINT = "Absolute path to a local audio or video file, e.g. the /.ovis/<file> path Ovis puts in a message.";

export const TOOLS = [
  {
    name: "create_voice_from_file",
    description:
      "Clone a voice from local audio files (a dropped recording, a voice memo, a video). Uploads them to voiceapi, " +
      "cleans them with NVIDIA RE-USE, transcribes them, picks the best 5-15 s reference and builds the voice. " +
      "Waits for the build (usually under a minute) and returns the voice, its reference transcript and quality.",
    inputSchema: {
      type: "object",
      properties: {
        paths: { type: "array", items: { type: "string", description: PATH_HINT }, minItems: 1, maxItems: 20 },
        name: { type: "string", description: "Voice name, 1-63 characters; used later to speak with it." },
        transcript: { type: "string", description: "Exact words spoken; only with a single file. Omit to auto-transcribe." },
        language: { type: "string", description: "Language of the speech, e.g. english or en. Default: auto." },
        clean: { type: "boolean", description: "Run speech enhancement first (default true)." },
        mode: { type: "string", enum: ["auto", "icl", "xvector"], description: "Default cloning mode (default auto)." },
        make_default: {
          type: "boolean",
          description:
            "Make this the default voice for every future session once built (default true). It persists until " +
            "the user asks to change or turn it off.",
        },
        description: { type: "string" },
      },
      required: ["paths", "name"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
  },
  {
    name: "speak",
    description:
      "Synthesize speech and save it to a local file. Omit voice to use the default voice (set when the user " +
      "created one, persisting across sessions). Returns the file path; mention it in your reply. You cannot play " +
      "audio yourself (no speakers, a headless browser plays nowhere): saving the file is the whole job.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Text to speak. Long text is fine." },
        voice: { type: "string", description: "Voice id (voice_...) or name. Omit to use the default voice." },
        out_path: {
          type: "string",
          description: "Where to save, e.g. <project>/.ovis/hello.mp3. Default: a file under $TMPDIR/voiceapi.",
        },
        format: { type: "string", enum: ["mp3", "opus", "aac", "flac", "wav", "pcm"], description: "Default mp3." },
        language: { type: "string" },
        voice_mode: { type: "string", enum: ["auto", "icl", "xvector"] },
        speed: { type: "number", minimum: 0.25, maximum: 4 },
      },
      required: ["text"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
  },
  {
    name: "transcribe_file",
    description: "Transcribe a local audio or video file (Qwen3-ASR). Returns text, language, segments and optional word timings.",
    inputSchema: {
      type: "object",
      properties: {
        path: { type: "string", description: PATH_HINT },
        language: { type: "string" },
        prompt: { type: "string", description: "Names or vocabulary to bias recognition." },
        word_timestamps: { type: "boolean" },
      },
      required: ["path"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "enhance_file",
    description: "Remove noise, reverb and codec damage from speech (NVIDIA RE-USE). Writes a cleaned WAV next to the input unless out_path is given.",
    inputSchema: {
      type: "object",
      properties: {
        path: { type: "string", description: PATH_HINT },
        out_path: { type: "string" },
      },
      required: ["path"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "list_voices",
    description: "List voices (ready ones by default) with ids, names, reference transcripts and quality.",
    inputSchema: {
      type: "object",
      properties: {
        status: { type: "string", enum: ["ready", "building", "failed"] },
        limit: { type: "integer", minimum: 1, maximum: 100 },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "delete_voice",
    description: "Permanently delete a voice by id or name. Only when the user asks for it.",
    inputSchema: {
      type: "object",
      properties: { voice: { type: "string" } },
      required: ["voice"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "get_default_voice",
    description:
      "The voice used whenever none is named. It is stored in voiceapi, so it persists across sessions: check " +
      "it instead of assuming there is no voice.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "set_default_voice",
    description:
      "Change the default voice (id or name) for every future session, or pass voice=null to turn the default " +
      "off. Only when the user asks.",
    inputSchema: {
      type: "object",
      properties: { voice: { type: ["string", "null"], description: "Voice id or name; null turns it off." } },
      required: ["voice"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "get_job",
    description: "Status of a voiceapi background job (a voice build that outlived create_voice_from_file's wait).",
    inputSchema: {
      type: "object",
      properties: { job_id: { type: "string" } },
      required: ["job_id"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
];

const HANDLERS = {
  create_voice_from_file: async (client, args) => {
    const { voice } = await client.createVoiceFromFiles({
      paths: args.paths,
      name: args.name,
      transcript: args.transcript,
      language: args.language,
      clean: args.clean ?? true,
      mode: args.mode ?? "auto",
      description: args.description ?? "",
      makeDefault: args.make_default ?? true,
    });
    const reference = voice.reference ?? {};
    const quality = voice.quality ?? {};
    return {
      text:
        `Voice "${voice.name}" is ready (${voice.id}).\n` +
        `Reference: ${reference.duration_s ?? "?"} s — "${reference.transcript ?? ""}"\n` +
        `Clone mode: ${voice.supports_icl ? "in-context (reference audio + transcript)" : "speaker embedding only"}\n` +
        `Cleaning: SNR ${quality.snr_before_db ?? "?"} dB -> ${quality.snr_after_db ?? "?"} dB` +
        (voice.is_default ? "\nIt is now the default voice for every session." : ""),
      structured: voice,
    };
  },
  speak: async (client, args) => {
    const result = await client.speakToFile({
      text: args.text,
      voice: args.voice,
      format: args.format ?? "mp3",
      outPath: args.out_path,
      language: args.language,
      voiceMode: args.voice_mode,
      speed: args.speed,
    });
    return { text: `Saved ${result.bytes} bytes of ${result.format} to ${result.path}`, structured: result };
  },
  transcribe_file: async (client, args) => {
    const result = await client.transcribeFile({
      path: args.path,
      language: args.language,
      prompt: args.prompt,
      wordTimestamps: args.word_timestamps ?? false,
    });
    return { text: result.text ?? "", structured: result };
  },
  enhance_file: async (client, args) => {
    const result = await client.enhanceFile({ path: args.path, outPath: args.out_path });
    return { text: `Cleaned audio written to ${result.path}`, structured: result };
  },
  list_voices: async (client, args) => {
    const voices = await client.listVoices({ status: args.status ?? "ready", limit: args.limit ?? 50 });
    const lines = voices.map((voice) => `${voice.name} (${voice.id}) — ${voice.status}`);
    return { text: lines.length ? lines.join("\n") : "No voices yet.", structured: { voices } };
  },
  delete_voice: async (client, args) => {
    const result = await client.deleteVoice(args.voice);
    return { text: `Deleted voice ${args.voice}.`, structured: result };
  },
  get_default_voice: async (client) => {
    const voice = await client.getDefaultVoice();
    return {
      text: voice ? `Default voice: ${voice.name} (${voice.id}).` : "No default voice is set.",
      structured: { default_voice: voice },
    };
  },
  set_default_voice: async (client, args) => {
    const voice = await client.setDefaultVoice(args.voice);
    return {
      text: voice ? `Default voice is now ${voice.name} (${voice.id}) for every session.` : "Default voice turned off.",
      structured: { default_voice: voice },
    };
  },
  get_job: async (client, args) => {
    const job = await client.getJob(args.job_id);
    return { text: `Job ${job.id}: ${job.status}`, structured: job };
  },
};

export function createMcpMessageHandler(options = {}) {
  const env = options.env ?? process.env;
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const sleep = options.sleep;

  return async function handleMessage(message) {
    if (message === null || typeof message !== "object" || Array.isArray(message) || message.jsonrpc !== "2.0") {
      return rpcError(null, -32600, "Invalid Request");
    }
    if (!Object.prototype.hasOwnProperty.call(message, "id")) {
      return null;
    }
    try {
      switch (message.method) {
        case "initialize":
          return rpcResult(message.id, {
            protocolVersion:
              typeof message.params?.protocolVersion === "string"
                ? message.params.protocolVersion
                : DEFAULT_PROTOCOL_VERSION,
            capabilities: { tools: { listChanged: false } },
            serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
          });
        case "ping":
          return rpcResult(message.id, {});
        case "tools/list":
          return rpcResult(message.id, { tools: TOOLS });
        case "tools/call":
          return await callTool(message.id, message.params, { env, fetchImpl, sleep });
        default:
          return rpcError(message.id, -32601, `Method not found: ${String(message.method)}`);
      }
    } catch {
      return rpcError(message.id, -32603, "Internal error");
    }
  };
}

async function callTool(id, params, clientOptions) {
  const handler = HANDLERS[params?.name];
  if (!handler) {
    return rpcError(id, -32602, "Unknown tool");
  }
  const args = params.arguments ?? {};
  if (args === null || typeof args !== "object" || Array.isArray(args)) {
    return toolError(id, "INVALID_ARGUMENTS", "Tool arguments must be an object.");
  }
  try {
    const client = createClient(clientOptions);
    const { text, structured } = await handler(client, args);
    return rpcResult(id, {
      content: [{ type: "text", text }],
      structuredContent: structured,
      isError: false,
    });
  } catch (error) {
    if (error instanceof VoiceApiError) {
      return toolError(id, error.code, error.hint ? `${error.message} Hint: ${error.hint}` : error.message);
    }
    return toolError(id, "INTERNAL_ERROR", `voiceapi call failed unexpectedly: ${error?.message ?? error}`);
  }
}

function toolError(id, code, message) {
  return rpcResult(id, { content: [{ type: "text", text: `${code}: ${message}` }], isError: true });
}

function rpcResult(id, result) {
  return { jsonrpc: "2.0", id, result };
}

function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

export async function runStdioServer(options = {}) {
  const input = options.input ?? process.stdin;
  const output = options.output ?? process.stdout;
  const handleMessage = createMcpMessageHandler(options);
  const lines = readline.createInterface({ input, crlfDelay: Infinity, terminal: false });
  for await (const line of lines) {
    if (line.trim() === "") {
      continue;
    }
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      output.write(`${JSON.stringify(rpcError(null, -32700, "Parse error"))}\n`);
      continue;
    }
    const response = await handleMessage(message);
    if (response !== null) {
      output.write(`${JSON.stringify(response)}\n`);
    }
  }
}

function isMainModule() {
  if (!process.argv[1]) {
    return false;
  }
  return import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
}

if (isMainModule()) {
  await runStdioServer();
}
