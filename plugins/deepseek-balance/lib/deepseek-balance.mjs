export const DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance";

const DEFAULT_TIMEOUT_MS = 10_000;
const DECIMAL_PATTERN = /^(?:\d+(?:\.\d*)?|\.\d+)$/;

export class DeepSeekBalanceError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = "DeepSeekBalanceError";
    this.code = code;

    if (options.status !== undefined) {
      this.status = options.status;
    }
  }
}

export async function queryDeepSeekBalance(options = {}) {
  const apiKey = options.apiKey ?? process.env.DEEPSEEK_API_KEY;
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const endpoint = options.endpoint ?? DEEPSEEK_BALANCE_URL;

  if (typeof apiKey !== "string" || apiKey.trim() === "") {
    throw new DeepSeekBalanceError(
      "MISSING_API_KEY",
      "DEEPSEEK_API_KEY is not set.",
    );
  }

  if (typeof fetchImpl !== "function") {
    throw new DeepSeekBalanceError(
      "FETCH_UNAVAILABLE",
      "No fetch implementation is available.",
    );
  }

  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new DeepSeekBalanceError(
      "INVALID_TIMEOUT",
      "timeoutMs must be a positive number.",
    );
  }

  if (typeof endpoint !== "string" || endpoint.trim() === "") {
    throw new DeepSeekBalanceError(
      "INVALID_ENDPOINT",
      "DeepSeek balance endpoint must be a non-empty string.",
    );
  }

  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  const signal = options.signal
    ? AbortSignal.any([options.signal, timeoutSignal])
    : timeoutSignal;

  let response;
  try {
    response = await fetchImpl(endpoint, {
      method: "GET",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${apiKey.trim()}`,
      },
      signal,
    });
  } catch (error) {
    if (error?.name === "TimeoutError") {
      throw new DeepSeekBalanceError(
        "REQUEST_TIMEOUT",
        "DeepSeek balance request timed out.",
      );
    }

    if (options.signal?.aborted) {
      throw new DeepSeekBalanceError(
        "REQUEST_ABORTED",
        "DeepSeek balance request was aborted.",
      );
    }

    throw new DeepSeekBalanceError(
      "NETWORK_ERROR",
      "DeepSeek balance request failed before receiving a response.",
    );
  }

  if (!response || typeof response.ok !== "boolean") {
    throw new DeepSeekBalanceError(
      "INVALID_RESPONSE",
      "DeepSeek returned an invalid HTTP response.",
    );
  }

  if (!response.ok) {
    throw new DeepSeekBalanceError(
      "HTTP_ERROR",
      `DeepSeek balance request failed with HTTP ${response.status}.`,
      { status: response.status },
    );
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new DeepSeekBalanceError(
      "INVALID_JSON",
      "DeepSeek balance response was not valid JSON.",
    );
  }

  return selectUsdBalance(payload);
}

export function selectUsdBalance(payload) {
  if (
    payload === null ||
    typeof payload !== "object" ||
    Array.isArray(payload) ||
    typeof payload.is_available !== "boolean" ||
    !Array.isArray(payload.balance_infos)
  ) {
    throw new DeepSeekBalanceError(
      "INVALID_RESPONSE",
      "DeepSeek balance response has an invalid shape.",
    );
  }

  const usdEntry = payload.balance_infos.find(
    (entry) =>
      entry !== null &&
      typeof entry === "object" &&
      typeof entry.currency === "string" &&
      entry.currency.toUpperCase() === "USD",
  );

  if (!usdEntry) {
    throw new DeepSeekBalanceError(
      "USD_BALANCE_NOT_FOUND",
      "DeepSeek balance response did not contain a USD entry.",
    );
  }

  return {
    currency: "USD",
    total_usd: readAmount(usdEntry, "total_balance"),
    granted_usd: readAmount(usdEntry, "granted_balance"),
    topped_up_usd: readAmount(usdEntry, "topped_up_balance"),
    available: payload.is_available,
  };
}

export function formatDeepSeekBalance(balance) {
  assertBalance(balance);
  const availability = balance.available ? "available" : "unavailable";

  return [
    `DeepSeek USD balance: $${balance.total_usd} total`,
    `(granted $${balance.granted_usd}, topped up $${balance.topped_up_usd});`,
    `API access ${availability}.`,
  ].join(" ");
}

export function formatDeepSeekBalanceTitle(balance) {
  assertBalance(balance);
  const unavailable = balance.available ? "" : " (unavailable)";

  return `DeepSeek $${balance.total_usd} USD${unavailable}`;
}

function readAmount(entry, field) {
  const value = entry[field];

  if (
    typeof value !== "string" ||
    !DECIMAL_PATTERN.test(value.trim()) ||
    !Number.isFinite(Number(value))
  ) {
    throw new DeepSeekBalanceError(
      "INVALID_RESPONSE",
      `DeepSeek USD balance has an invalid ${field} value.`,
    );
  }

  return value.trim();
}

function assertBalance(balance) {
  if (
    balance === null ||
    typeof balance !== "object" ||
    balance.currency !== "USD" ||
    typeof balance.total_usd !== "string" ||
    typeof balance.granted_usd !== "string" ||
    typeof balance.topped_up_usd !== "string" ||
    typeof balance.available !== "boolean"
  ) {
    throw new DeepSeekBalanceError(
      "INVALID_BALANCE",
      "A normalized USD balance object is required.",
    );
  }
}
