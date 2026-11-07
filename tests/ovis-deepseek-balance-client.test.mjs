// patches/ovis/deepseek-balance.js: what the composer pill shows, without a browser.
import assert from "node:assert/strict";
import test from "node:test";

await import("../patches/ovis/deepseek-balance.js");
const { formatAmount, describe, isDeepSeek, LOW_USD } = globalThis.__codexWorkspaceBalance;

const NOW = Date.parse("2026-11-02T12:00:00Z");
const reading = (balance, extra = {}) => ({
  configured: true,
  balance: { currency: "USD", granted_usd: "0.00", topped_up_usd: balance.total_usd ?? "0", available: true, ...balance },
  fetched_at: new Date(NOW - 120_000).toISOString(),
  stale: false,
  ...extra,
});

test("formatAmount rounds to cents and refuses anything but a plain decimal", () => {
  assert.equal(formatAmount("21.4567"), "$21.46");
  assert.equal(formatAmount("5"), "$5.00");
  assert.equal(formatAmount(".5"), "$0.50");
  assert.equal(formatAmount("<b>1</b>"), null);
  assert.equal(formatAmount("1e3"), null);
  assert.equal(formatAmount(21.45), null);
});

test("isDeepSeek matches the model id, not other models", () => {
  assert.equal(isDeepSeek("deepseek-flash"), true);
  assert.equal(isDeepSeek("DeepSeek-V4"), true);
  assert.equal(isDeepSeek("glm-5.3"), false);
  assert.equal(isDeepSeek(null), false);
});

test("describe: amount, tooltip and age", () => {
  const view = describe(reading({ total_usd: "21.45" }), NOW);
  assert.equal(view.text, "$21.45");
  assert.equal(view.low, false);
  assert.equal(view.stale, false);
  assert.match(view.title, /^DeepSeek balance: \$21\.45 USD \(granted \$0\.00, topped up \$21\.45\)\nUpdated 2 min ago$/);
});

test("describe: low below LOW_USD, and when DeepSeek reports the account unavailable", () => {
  assert.equal(describe(reading({ total_usd: String(LOW_USD - 0.01) }), NOW).low, true);
  const off = describe(reading({ total_usd: "50", available: false }), NOW);
  assert.equal(off.low, true);
  assert.match(off.title, /API access unavailable/);
});

test("describe: stale says so", () => {
  const view = describe(reading({ total_usd: "3" }, { stale: true }), NOW);
  assert.equal(view.stale, true);
  assert.match(view.title, /Stale: last read 2 min ago/);
});

test("describe: nothing to show without a balance", () => {
  assert.equal(describe(null, NOW), null);
  assert.equal(describe({ configured: false, balance: null }, NOW), null);
  assert.equal(describe(reading({ total_usd: "NaN" }), NOW), null);
});
