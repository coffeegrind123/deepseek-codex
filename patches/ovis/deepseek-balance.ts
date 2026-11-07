// codex-workspace: the DeepSeek balance beside the composer's model pill (README "DeepSeek balance").
//
//   browser (dist/deepseek-balance.js) ──► GET /api/deepseek/balance ──► this module
//                                                                         ├─ .codex/cache/deepseek-balance.json, when fresh
//                                                                         └─ else DeepSeek GET /user/balance itself
//
// Every Codex pane already runs scripts/deepseek-balance.mjs, which refreshes the shared cache
// once a minute for the TUI footer. That file is read first so Ovis adds no requests of its own
// while a pane is open. Ovis runs outside the Landlock cage, so with no fresh cache (no pane, or
// the daemon failing) it queries DeepSeek directly through the same plugin library, at most
// once per MEMO_MS. The API key stays in this process: the browser sees the USD amounts, the
// availability flag and freshness, never the key or the raw response.

import { readFile } from "node:fs/promises";

export const BALANCE_PATH = "/api/deepseek/balance";
/** One request per minute, the cadence of the Codex footer daemon. */
export const MEMO_MS = 60_000;
/** The footer daemon writes every 60 s; a little slack before it counts as not fresh. */
export const SHARED_FRESH_MS = 90_000;
/** A value older than this is still shown, marked stale (the footer's own limit). */
export const STALE_AFTER_MS = 5 * 60_000;
const CACHE_VERSION = 1;
const DECIMAL = /^(?:\d+(?:\.\d*)?|\.\d+)$/;

export interface Balance {
  currency: "USD";
  total_usd: string;
  granted_usd: string;
  topped_up_usd: string;
  available: boolean;
}

export interface Reading {
  configured: boolean;
  balance: Balance | null;
  fetched_at: string | null;
  stale: boolean;
  source: "codex-cache" | "deepseek" | null;
  error: string | null;
}

interface BalanceLib {
  queryDeepSeekBalance(options: { apiKey?: string; endpoint?: string; fetchImpl?: typeof fetch }): Promise<Balance>;
}

export interface SourceOptions {
  env?: Record<string, string | undefined>;
  now?: () => number;
  readText?: (path: string) => Promise<string>;
  loadLib?: (path: string) => Promise<BalanceLib>;
  fetchImpl?: typeof fetch;
}

/** Exactly the normalized shape the plugin library returns; anything else is not shown. */
export function validBalance(value: unknown): value is Balance {
  if (value === null || typeof value !== "object") return false;
  const b = value as Record<string, unknown>;
  const amount = (v: unknown) => typeof v === "string" && DECIMAL.test(v) && Number.isFinite(Number(v));
  return b.currency === "USD" && amount(b.total_usd) && amount(b.granted_usd) && amount(b.topped_up_usd) && typeof b.available === "boolean";
}

interface Held {
  balance: Balance;
  fetchedAt: number;
  stale: boolean;
  source: "codex-cache" | "deepseek";
}

export function createBalanceSource(options: SourceOptions = {}) {
  const env = options.env ?? process.env;
  const now = options.now ?? Date.now;
  const readText = options.readText ?? ((path: string) => readFile(path, "utf8"));
  const loadLib = options.loadLib ?? ((path: string) => import(path) as Promise<BalanceLib>);
  const key = env["DEEPSEEK_API_KEY"]?.trim() ?? "";
  const cacheFile = env["DEEPSEEK_BALANCE_CACHE_FILE"]?.trim() ?? "";
  const libPath = env["DEEPSEEK_BALANCE_LIB"]?.trim() ?? "";
  const endpoint = env["DEEPSEEK_BALANCE_URL"]?.trim() || undefined;

  let held: Held | null = null;
  let lastQuery = -Infinity;
  let lastError: string | null = null;
  let inflight: Promise<void> | null = null;

  async function shared(): Promise<Held | null> {
    if (!cacheFile) return null;
    let payload: { version?: unknown; fetched_at?: unknown; stale?: unknown; balance?: unknown };
    try {
      payload = JSON.parse(await readText(cacheFile));
    } catch {
      return null;
    }
    if (payload?.version !== CACHE_VERSION || typeof payload.stale !== "boolean" || !validBalance(payload.balance)) return null;
    const fetchedAt = Date.parse(String(payload.fetched_at));
    if (!Number.isFinite(fetchedAt)) return null;
    return { balance: payload.balance, fetchedAt, stale: payload.stale, source: "codex-cache" };
  }

  async function query(): Promise<void> {
    lastQuery = now();
    try {
      if (!libPath) throw Object.assign(new Error("DEEPSEEK_BALANCE_LIB is not set"), { code: "LIB_UNSET" });
      const lib = await loadLib(libPath);
      const balance = await lib.queryDeepSeekBalance({ apiKey: key, endpoint, fetchImpl: options.fetchImpl });
      if (!validBalance(balance)) throw Object.assign(new Error("invalid balance"), { code: "INVALID_BALANCE" });
      held = { balance, fetchedAt: now(), stale: false, source: "deepseek" };
      lastError = null;
    } catch (error) {
      // the library's codes (HTTP_ERROR, NETWORK_ERROR, ...) carry no secret; its messages neither
      lastError = String((error as { code?: unknown }).code ?? "QUERY_FAILED");
      if (held) held = { ...held, stale: true };
    }
  }

  function reading(): Reading {
    if (!held) {
      return { configured: key !== "" || cacheFile !== "", balance: null, fetched_at: null, stale: false, source: null, error: lastError };
    }
    const old = now() - held.fetchedAt > STALE_AFTER_MS;
    return {
      configured: true,
      balance: held.balance,
      fetched_at: new Date(held.fetchedAt).toISOString(),
      stale: held.stale || old,
      source: held.source,
      error: lastError,
    };
  }

  async function get(): Promise<Reading> {
    const fromCodex = await shared();
    if (fromCodex && !fromCodex.stale && now() - fromCodex.fetchedAt <= SHARED_FRESH_MS) {
      held = fromCodex;
      lastError = null;
      return reading();
    }
    // the footer cache is newer than what this process holds (a stale one still beats nothing)
    if (fromCodex && (!held || fromCodex.fetchedAt > held.fetchedAt)) held = fromCodex;

    if (key && now() - lastQuery >= MEMO_MS) {
      inflight ??= query().finally(() => { inflight = null; });
    }
    if (inflight) await inflight;
    return reading();
  }

  return { get };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

let defaultSource: ReturnType<typeof createBalanceSource> | null = null;

/** `/api/deepseek/balance`; null for any other path so pane-routes.ts carries on. */
export async function deepseekBalanceRoute(
  pathname: string,
  request: Request,
  source?: ReturnType<typeof createBalanceSource>,
): Promise<Response | null> {
  if (pathname !== BALANCE_PATH) return null;
  if (request.method !== "GET") return json({ error: { code: "method_not_allowed", message: "use GET" } }, 405);
  const active = source ?? (defaultSource ??= createBalanceSource());
  return json(await active.get());
}
