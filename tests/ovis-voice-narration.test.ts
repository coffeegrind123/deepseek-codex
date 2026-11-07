// patches/ovis/voice-narration.ts, run by the Ovis bundle's bun (tests/test_ovis.py, VoiceNarration).
import { describe, expect, test } from "bun:test";

import {
  narrationConfig,
  replySegments,
  speakable,
  voiceNarrationRoute,
  type Conversation,
} from "../patches/ovis/voice-narration.ts";

const ENV = { VOICEAPI_KEY: "k".repeat(32), VOICEAPI_URL: "http://voiceapi.test:8080/" };

const conversation: Conversation = {
  source: "codex-transcript",
  turns: [
    { role: "user", ts: "2026-11-01T10:00:00Z", parts: [{ kind: "text", text: "do the thing" }] },
    {
      role: "assistant",
      ts: "2026-11-01T10:00:01Z",
      parts: [
        { kind: "thinking", text: "secret reasoning" },
        { kind: "text", text: "I'll look at the config first.", phase: "commentary" },
        { kind: "tool", text: undefined },
        { kind: "text", text: "Done. Here is the fix:\n```ts\nconst x = 1;\n```\nSee [the docs](https://x.dev).", phase: "final_answer" },
      ],
    },
  ],
};

function route(path: string, options: Partial<Parameters<typeof voiceNarrationRoute>[3]> = {}) {
  const url = new URL(`http://ovis${path}`);
  return voiceNarrationRoute(url.pathname, url, new Request(url), {
    readConversation: async () => conversation,
    env: ENV,
    ...options,
  });
}

describe("speakable", () => {
  test("announces code instead of reading it and keeps link labels", () => {
    expect(speakable("Use `npm test`.\n```sh\nrm -rf /\n```\nRead [the guide](https://a.b/c) or https://x.y/z")).toBe(
      "Use npm test.\n(code omitted)\nRead the guide or link",
    );
  });

  test("drops markdown structure", () => {
    expect(speakable("# Title\n- **bold** item\n> quote\n1. _first_")).toBe("Title\nbold item\nquote\nfirst");
  });

  test("tables become comma separated and separators vanish", () => {
    expect(speakable("| a | b |\n|---|---|\n| 1 | 2 |")).toBe(", a , b ,\n, 1 , 2 ,");
  });

  test("nothing speakable yields empty", () => {
    expect(speakable("```\nonly code\n```")).toBe("(code omitted)");
    expect(speakable("---\n|  |")).toBe("");
  });

  test("very long replies are cut at a sentence with a note", () => {
    const text = speakable("This is a sentence. ".repeat(600));
    expect(text.length).toBeLessThan(6100);
    expect(text.endsWith("The rest is in the chat.")).toBe(true);
  });
});

describe("replySegments", () => {
  test("only assistant text parts, never thinking or tools, with their phase", () => {
    const segments = replySegments(conversation);
    expect(segments.map((s) => s.phase)).toEqual(["commentary", "final_answer"]);
    expect(segments[0]!.text).toBe("I'll look at the config first.");
    expect(segments[1]!.text).toBe("Done. Here is the fix:\n(code omitted)\nSee the docs.");
    expect(JSON.stringify(segments)).not.toContain("secret reasoning");
  });

  test("ids are stable across refetches and differ per part", () => {
    const first = replySegments(conversation).map((s) => s.id);
    const second = replySegments(structuredClone(conversation)).map((s) => s.id);
    expect(second).toEqual(first);
    expect(new Set(first).size).toBe(first.length);
  });
});

describe("narrationConfig", () => {
  test("needs a key; URL defaults and is normalised", () => {
    expect(narrationConfig({})).toBeNull();
    expect(narrationConfig({ VOICEAPI_KEY: "x" })).toEqual({ url: "http://host.docker.internal:8080", key: "x" });
    expect(narrationConfig(ENV)!.url).toBe("http://voiceapi.test:8080");
    expect(narrationConfig({ VOICEAPI_KEY: "x", VOICEAPI_URL: "ftp://nope" })).toBeNull();
  });
});

describe("voiceNarrationRoute", () => {
  test("other paths fall through", async () => {
    expect(await route("/api/pane/conversation?pane_id=1")).toBeNull();
  });

  test("lists segment ids and phases without the text", async () => {
    const response = (await route("/api/narration/narration?pane_id=p1"))!;
    const body = await response.json();
    expect(body.configured).toBe(true);
    expect(body.segments.map((s: { phase: string }) => s.phase)).toEqual(["commentary", "final_answer"]);
    expect(JSON.stringify(body)).not.toContain("config first");
  });

  test("unconfigured narration says so instead of failing", async () => {
    const body = await (await route("/api/narration/narration?pane_id=p1", { env: {} }))!.json();
    expect(body).toEqual({ configured: false, segments: [] });
    expect((await route("/api/narration/speech?pane_id=p1&segment=x", { env: {} }))!.status).toBe(503);
  });

  test("an unreadable pane narrates nothing", async () => {
    const body = await (await route("/api/narration/narration?pane_id=shell", {
      readConversation: async () => { throw new Error("no transcript"); },
    }))!.json();
    expect(body.segments).toEqual([]);
  });

  test("speech forwards the segment text with the key and no voice, and streams the audio", async () => {
    const [, final] = replySegments(conversation);
    let seen: { url: string; init: RequestInit } | null = null;
    const response = (await route(`/api/narration/speech?pane_id=p1&segment=${final!.id}`, {
      fetchImpl: (async (url: string, init: RequestInit) => {
        seen = { url, init };
        return new Response("ID3audio", { headers: { "content-type": "audio/mpeg" } });
      }) as unknown as typeof fetch,
    }))!;
    expect(response.headers.get("content-type")).toBe("audio/mpeg");
    expect(await response.text()).toBe("ID3audio");
    expect(seen!.url).toBe("http://voiceapi.test:8080/v1/audio/speech");
    expect((seen!.init.headers as Record<string, string>).authorization).toBe(`Bearer ${ENV.VOICEAPI_KEY}`);
    expect(JSON.parse(String(seen!.init.body))).toEqual({ input: final!.text, response_format: "mp3" });
  });

  test("unknown segments and voiceapi errors are explained, never leaking the key", async () => {
    expect((await route("/api/narration/speech?pane_id=p1&segment=nope"))!.status).toBe(404);
    const [first] = replySegments(conversation);
    const failed = (await route(`/api/narration/speech?pane_id=p1&segment=${first!.id}`, {
      fetchImpl: (async () => new Response(JSON.stringify({ code: "voice_not_found", detail: "no default voice", hint: "Set one." }), { status: 422 })) as unknown as typeof fetch,
    }))!;
    expect(failed.status).toBe(502);
    const text = await failed.text();
    expect(text).toContain("voice_not_found: no default voice: Set one.");
    expect(text).not.toContain(ENV.VOICEAPI_KEY);
  });

  test("the unlock clip is real audio", async () => {
    const response = (await route("/api/narration/silence"))!;
    expect(response.headers.get("content-type")).toBe("audio/mpeg");
    const bytes = new Uint8Array(await response.arrayBuffer());
    expect(bytes.length).toBeGreaterThan(200);
    expect(bytes[0]).toBe(0xff);
  });

  test("only GET", async () => {
    const url = new URL("http://ovis/api/narration/narration?pane_id=p1");
    const response = await voiceNarrationRoute(url.pathname, url, new Request(url, { method: "POST" }), {
      readConversation: async () => conversation,
      env: ENV,
    });
    expect(response!.status).toBe(405);
  });
});
