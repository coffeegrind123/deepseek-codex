// codex-workspace: narrate agent replies through a self-hosted voiceapi (README "Voice API").
//
//   browser (dist/voice-narration.js)            this module                      voiceapi gateway
//   conversation-changed ──► GET /api/narration/narration ──► reply segments (ids)
//   new id               ──► GET /api/narration/speech?segment ──► POST /v1/audio/speech ──► mp3 stream
//
// Segments are the assistant's `text` parts from Ovis's own normalized transcript, so tool
// calls, reasoning and skill activity are never read, for every agent Ovis understands. The
// gateway key stays in this process (VOICEAPI_KEY, passed by scripts/ovis.sh); the browser only
// ever sees segment ids and audio. No voice is named, so voiceapi's persistent default voice
// speaks. Nothing here imports the bundle: pane-routes.ts hands in the conversation reader.

import { createHash } from "node:crypto";

export const DEFAULT_VOICEAPI_URL = "http://host.docker.internal:8080";
/** Replies longer than this are cut (with a spoken note) so one answer cannot occupy the voice for minutes. */
export const MAX_SPOKEN_CHARS = 6000;
/** How many of the newest segments a narration listing returns; the client only needs the recent ones. */
export const LISTED_SEGMENTS = 60;
/** 0.2 s of silent MP3 (ffmpeg anullsrc, 24 kHz mono, 32 kbps): played inside a click to unlock autoplay. */
const SILENT_FRAME = Buffer.from(
  "//NExAAAAANIAAAAAExBTUU0LjEgKGFscGhhIDApVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQu//NExFMAAANIAAAAADEgKGFscGhhIDApVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQu//NExKYAAANIAAAAADEgKGFscGhhIDApVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQu//NExKwAAANIAAAAADEgKGFscGhhIDApVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQu//NExKwAAANIAAAAADEgKGFscGhhIDApVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQu//NExKwAAANIAAAAADEgKGFscGhhIDApVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQu//NExKwAAANIAAAAADEgKGFscGhhIDApVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQu//NExKwAAANIAAAAADEgKGFscGhhIDApVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQu//NExKwAAANIAAAAADEgKGFscGhhIDApVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NExKwAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NExKwAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV",
  "base64",
);

export type Phase = "commentary" | "final_answer" | null;

interface TextPart { kind: string; text?: string; phase?: "commentary" | "final_answer" }
interface Turn { role: "user" | "assistant"; ts: string | null; parts: TextPart[] }
export interface Conversation { source?: string; turns?: Turn[] }

export interface Segment {
  id: string;
  phase: Phase;
  text: string;
}

export interface NarrationConfig {
  url: string;
  key: string;
}

export function narrationConfig(env: Record<string, string | undefined> = process.env): NarrationConfig | null {
  const key = env["VOICEAPI_KEY"]?.trim() ?? "";
  if (!key) return null;
  const url = (env["VOICEAPI_URL"]?.trim() || DEFAULT_VOICEAPI_URL).replace(/\/+$/, "");
  return /^https?:\/\//.test(url) ? { url, key } : null;
}

/**
 * Turn markdown into something worth hearing: code is announced, not read; links keep their
 * label; formatting characters, tables and raw URLs are dropped.
 */
export function speakable(markdown: string): string {
  let text = markdown.replace(/\r\n/g, "\n");
  text = text.replace(/```[\s\S]*?(```|$)/g, " (code omitted) ");
  text = text.replace(/`([^`\n]{1,40})`/g, "$1").replace(/`[^`\n]*`/g, " (code) ");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  text = text.replace(/<https?:\/\/[^>]+>/g, " link ").replace(/https?:\/\/\S+/g, " link ");
  text = text.replace(/^\s*\|?\s*:?-{3,}.*$/gm, "");
  text = text.replace(/\|/g, ", ");
  text = text.replace(/^\s{0,3}#{1,6}\s+/gm, "").replace(/^\s*>\s?/gm, "");
  text = text.replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, "");
  text = text.replace(/(\*\*|__)(.+?)\1/g, "$2").replace(/(^|[\s(])[*_]([^*_\n]+)[*_](?=[\s).,!?:;]|$)/g, "$1$2");
  text = text.replace(/~~(.+?)~~/g, "$1");
  text = text.replace(/[ \t]+/g, " ").replace(/\s*\n\s*/g, "\n").replace(/\n{2,}/g, "\n").trim();
  text = text.replace(/(\(code omitted\)\s*){2,}/g, "(code omitted) ");
  if (text.length > MAX_SPOKEN_CHARS) {
    const cut = text.lastIndexOf(". ", MAX_SPOKEN_CHARS);
    text = `${text.slice(0, cut > MAX_SPOKEN_CHARS / 2 ? cut + 1 : MAX_SPOKEN_CHARS)} The rest is in the chat.`;
  }
  return /[\p{L}\p{N}]/u.test(text) ? text : "";
}

/**
 * Every assistant text part of the conversation, oldest first, with an id stable across
 * refetches: the turn's timestamp, the part's place in its turn and its text.
 */
export function replySegments(conversation: Conversation): Segment[] {
  const segments: Segment[] = [];
  for (const turn of conversation.turns ?? []) {
    if (turn.role !== "assistant") continue;
    turn.parts.forEach((part, index) => {
      if (part.kind !== "text" || typeof part.text !== "string") return;
      const text = speakable(part.text);
      if (!text) return;
      const id = createHash("sha256").update(`${turn.ts ?? ""}\u0000${index}\u0000${part.text}`).digest("hex").slice(0, 20);
      segments.push({ id, phase: part.phase ?? null, text });
    });
  }
  return segments;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

export type ConversationReader = (paneId: string) => Promise<Conversation>;

export interface RouteOptions {
  readConversation: ConversationReader;
  fetchImpl?: typeof fetch;
  env?: Record<string, string | undefined>;
}

/** `/api/narration/*`; null for any other path so pane-routes.ts carries on. */
export async function voiceNarrationRoute(pathname: string, url: URL, request: Request, options: RouteOptions): Promise<Response | null> {
  if (!pathname.startsWith("/api/narration/")) return null;
  if (request.method !== "GET") return json({ error: { code: "method_not_allowed", message: "use GET" } }, 405);
  const config = narrationConfig(options.env);

  if (pathname === "/api/narration/silence") {
    return new Response(SILENT_FRAME, { headers: { "content-type": "audio/mpeg", "cache-control": "private, max-age=86400" } });
  }

  const paneId = url.searchParams.get("pane_id");
  if (!paneId) return json({ error: { code: "missing_pane_id", message: "pane_id is required" } }, 400);

  if (pathname === "/api/narration/narration") {
    if (config === null) return json({ configured: false, segments: [] });
    let conversation: Conversation;
    try {
      conversation = await options.readConversation(paneId);
    } catch {
      // a pane without a readable transcript (a shell, a starting agent) has nothing to narrate yet
      return json({ configured: true, segments: [] });
    }
    const segments = replySegments(conversation).slice(-LISTED_SEGMENTS).map(({ id, phase, text }) => ({ id, phase, chars: text.length }));
    return json({ configured: true, segments });
  }

  if (pathname === "/api/narration/speech") {
    if (config === null) {
      return json({ error: { code: "not_configured", message: "VOICEAPI_KEY is not set for the Ovis server" } }, 503);
    }
    const segmentId = url.searchParams.get("segment");
    if (!segmentId) return json({ error: { code: "missing_segment", message: "segment is required" } }, 400);
    let segment: Segment | undefined;
    try {
      segment = replySegments(await options.readConversation(paneId)).find((item) => item.id === segmentId);
    } catch {
      segment = undefined;
    }
    if (!segment) return json({ error: { code: "segment_not_found", message: "no such reply in this pane" } }, 404);
    const fetchImpl = options.fetchImpl ?? fetch;
    let upstream: Response;
    try {
      upstream = await fetchImpl(`${config.url}/v1/audio/speech`, {
        method: "POST",
        headers: { authorization: `Bearer ${config.key}`, "content-type": "application/json" },
        body: JSON.stringify({ input: segment.text, response_format: "mp3" }),
        signal: request.signal,
      });
    } catch (error) {
      return json({ error: { code: "voiceapi_unreachable", message: `cannot reach voiceapi: ${(error as Error).message}` } }, 502);
    }
    if (!upstream.ok || upstream.body === null) {
      let detail = `voiceapi returned ${upstream.status}`;
      try {
        const problem = (await upstream.json()) as { detail?: string; hint?: string; code?: string };
        detail = [problem.code, problem.detail, problem.hint].filter(Boolean).join(": ");
      } catch {
        // not JSON: keep the status line
      }
      return json({ error: { code: "voiceapi_error", message: detail } }, 502);
    }
    return new Response(upstream.body, {
      headers: { "content-type": "audio/mpeg", "cache-control": "no-store", "x-content-type-options": "nosniff" },
    });
  }

  return json({ error: { code: "not_found", message: `no route ${pathname}` } }, 404);
}
