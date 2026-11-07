// patches/ovis/deepseek-balance.ts, run by the Ovis bundle's bun (tests/test_ovis.py, DeepSeekBalance).
import { describe, expect, test } from "bun:test";
import { join } from "node:path";

import {
  BALANCE_PATH,
  createBalanceSource,
  deepseekBalanceRoute,
  MEMO_MS,
  SHARED_FRESH_MS,
  STALE_AFTER_MS,
  validBalance,
} from "../patches/ovis/deepseek-balance.ts";

const LIB = join(import.meta.dir, "..", "plugins", "deepseek-balance", "lib", "deepseek-balance.mjs");
const KEY = "sk-" + "x".repeat(32);
const T0 = Date.parse("2026-11-02T12:00:00Z");
const BALANCE = { currency: "USD", total_usd: "21.45", granted_usd: "0.00", topped_up_usd: "21.45", available: true } as const;

function deepseekReply(total = "21.45") {
  return {
    is_available: true,
    balance_infos: [
      { currency: "CNY", total_balance: "99.00", granted_balance: "0.00", topped_up_balance: "99.00" },
      { currency: "USD", total_balance: total, granted_balance: "0.00", topped_up_balance: total },
    ],
  };
}

function rig(options: { cache?: unknown; key?: string; status?: number; fail?: boolean } = {}) {
  let clock = T0;
  const calls: { url: string; auth: string | null }[] = [];
  let cache = options.cache;
  let reply: unknown = deepseekReply();
  let status = options.status ?? 200;
  let fail = options.fail ?? false;
  const source = createBalanceSource({
    env: {
      DEEPSEEK_API_KEY: options.key ?? KEY,
      DEEPSEEK_BALANCE_CACHE_FILE: "/cache.json",
      DEEPSEEK_BALANCE_LIB: LIB,
    },
    now: () => clock,
    readText: async () => {
      if (cache === undefined) throw new Error("ENOENT");
      return typeof cache === "string" ? cache : JSON.stringify(cache);
    },
    fetchImpl: (async (url: string, init: RequestInit) => {
      calls.push({ url: String(url), auth: new Headers(init.headers).get("authorization") });
      if (fail) throw new TypeError("network down");
      return new Response(JSON.stringify(reply), { status });
    }) as unknown as typeof fetch,
  });
  return {
    source,
    calls,
    advance: (ms: number) => { clock += ms; },
    setCache: (value: unknown) => { cache = value; },
    setReply: (value: unknown) => { reply = value; },
    setStatus: (value: number) => { status = value; },
    setFail: (value: boolean) => { fail = value; },
  };
}

function footerCache(ageMs: number, stale = false, balance: unknown = BALANCE) {
  return { version: 1, fetched_at: new Date(T0 - ageMs).toISOString(), stale, balance };
}

describe("validBalance", () => {
  test("accepts the plugin's normalized shape only", () => {
    expect(validBalance(BALANCE)).toBe(true);
    expect(validBalance({ ...BALANCE, total_usd: "<img src=x>" })).toBe(false);
    expect(validBalance({ ...BALANCE, total_usd: 21.45 })).toBe(false);
    expect(validBalance({ ...BALANCE, currency: "CNY" })).toBe(false);
    expect(validBalance({ ...BALANCE, available: "yes" })).toBe(false);
    expect(validBalance(null)).toBe(false);
  });
});

describe("createBalanceSource", () => {
  test("a fresh footer cache is served without asking DeepSeek", async () => {
    const r = rig({ cache: footerCache(30_000) });
    const reading = await r.source.get();
    expect(reading).toMatchObject({ configured: true, balance: BALANCE, stale: false, source: "codex-cache", error: null });
    expect(r.calls).toHaveLength(0);
  });

  test("no fresh cache: queries DeepSeek once per minute and picks the USD entry", async () => {
    const r = rig();
    const first = await r.source.get();
    expect(first).toMatchObject({ balance: { total_usd: "21.45" }, stale: false, source: "deepseek" });
    expect(r.calls).toEqual([{ url: "https://api.deepseek.com/user/balance", auth: `Bearer ${KEY}` }]);

    r.advance(MEMO_MS - 1);
    await r.source.get();
    expect(r.calls).toHaveLength(1);

    r.setReply(deepseekReply("20.00"));
    r.advance(1);
    expect((await r.source.get()).balance?.total_usd).toBe("20.00");
    expect(r.calls).toHaveLength(2);
  });

  test("concurrent reads share one request", async () => {
    const r = rig();
    await Promise.all([r.source.get(), r.source.get(), r.source.get()]);
    expect(r.calls).toHaveLength(1);
  });

  test("a footer cache older than SHARED_FRESH_MS is shown until DeepSeek answers, and replaced by it", async () => {
    const r = rig({ cache: footerCache(SHARED_FRESH_MS + 1_000), fail: true });
    const reading = await r.source.get();
    expect(reading).toMatchObject({ balance: BALANCE, source: "codex-cache", error: "NETWORK_ERROR" });
    expect(reading.stale).toBe(true); // the failed query marks what is held as stale

    r.setFail(false);
    r.setReply(deepseekReply("5.00"));
    r.advance(MEMO_MS);
    expect(await r.source.get()).toMatchObject({ balance: { total_usd: "5.00" }, source: "deepseek", stale: false, error: null });
  });

  test("a failure after a success keeps the last value, stale, with the library's error code", async () => {
    const r = rig();
    await r.source.get();
    r.setStatus(401);
    r.advance(MEMO_MS);
    expect(await r.source.get()).toMatchObject({ balance: { total_usd: "21.45" }, stale: true, error: "HTTP_ERROR" });
  });

  test("a value older than STALE_AFTER_MS is stale even without a failure", async () => {
    const r = rig({ key: "", cache: footerCache(STALE_AFTER_MS + 1_000) });
    expect(await r.source.get()).toMatchObject({ balance: BALANCE, stale: true, source: "codex-cache" });
    expect(r.calls).toHaveLength(0);
  });

  test("a stale-flagged footer cache is not trusted as fresh", async () => {
    const r = rig({ cache: footerCache(10_000, true) });
    expect(await r.source.get()).toMatchObject({ source: "deepseek", stale: false });
    expect(r.calls).toHaveLength(1);
  });

  test("a malformed cache is ignored", async () => {
    for (const cache of ["{not json", { version: 2, fetched_at: new Date(T0).toISOString(), stale: false, balance: BALANCE },
      footerCache(0, false, { ...BALANCE, total_usd: "1e9" })]) {
      const r = rig({ key: "", cache });
      expect(await r.source.get()).toMatchObject({ balance: null });
    }
  });

  test("no key and no cache file: not configured, nothing fetched", async () => {
    const source = createBalanceSource({ env: {}, fetchImpl: (() => { throw new Error("must not fetch"); }) as unknown as typeof fetch });
    expect(await source.get()).toMatchObject({ configured: false, balance: null });
  });
});

describe("deepseekBalanceRoute", () => {
  test("other paths fall through", async () => {
    expect(await deepseekBalanceRoute("/api/pane/conversation", new Request("http://ovis/api/pane/conversation"))).toBeNull();
  });

  test("GET only", async () => {
    const response = await deepseekBalanceRoute(BALANCE_PATH, new Request(`http://ovis${BALANCE_PATH}`, { method: "POST" }));
    expect(response?.status).toBe(405);
  });

  test("answers JSON that never carries the key", async () => {
    const r = rig();
    const response = await deepseekBalanceRoute(BALANCE_PATH, new Request(`http://ovis${BALANCE_PATH}`), r.source);
    expect(response?.status).toBe(200);
    expect(response?.headers.get("cache-control")).toBe("no-store");
    const body = await response!.text();
    expect(JSON.parse(body)).toMatchObject({ configured: true, balance: { total_usd: "21.45" } });
    expect(body).not.toContain(KEY);
    expect(body).not.toContain("CNY");
  });
});
