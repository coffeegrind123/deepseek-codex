import assert from "node:assert/strict";
import test from "node:test";

import {
  DeepSeekBalanceError,
  DEEPSEEK_BALANCE_URL,
  formatDeepSeekBalance,
  formatDeepSeekBalanceTitle,
  queryDeepSeekBalance,
  selectUsdBalance,
} from "../lib/deepseek-balance.mjs";

const API_KEY = "ds-secret-key-that-must-not-leak";

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

function assertSafe(error) {
  const serialized = JSON.stringify({
    name: error.name,
    message: error.message,
    code: error.code,
    status: error.status,
  });

  assert.equal(serialized.includes(API_KEY), false);
}

test("selects the USD entry and returns all requested fields", async () => {
  let request;
  const balance = await queryDeepSeekBalance({
    apiKey: API_KEY,
    fetchImpl: async (url, init) => {
      request = { url, init };
      return jsonResponse({
        is_available: false,
        balance_infos: [
          {
            currency: "CNY",
            total_balance: "110.00",
            granted_balance: "10.00",
            topped_up_balance: "100.00",
          },
          {
            currency: "USD",
            total_balance: "12.34",
            granted_balance: "2.00",
            topped_up_balance: "10.34",
          },
        ],
      });
    },
  });

  assert.deepEqual(balance, {
    currency: "USD",
    total_usd: "12.34",
    granted_usd: "2.00",
    topped_up_usd: "10.34",
    available: false,
  });
  assert.equal(request.url, DEEPSEEK_BALANCE_URL);
  assert.equal(request.init.method, "GET");
  assert.equal(request.init.headers.Authorization, `Bearer ${API_KEY}`);
  assert.match(formatDeepSeekBalance(balance), /12\.34 total/);
  assert.equal(formatDeepSeekBalanceTitle(balance), "DeepSeek $12.34 USD (unavailable)");
});

test("fails explicitly when the response contains only CNY", async () => {
  await assert.rejects(
    queryDeepSeekBalance({
      apiKey: API_KEY,
      fetchImpl: async () =>
        jsonResponse({
          is_available: true,
          balance_infos: [
            {
              currency: "CNY",
              total_balance: "110.00",
              granted_balance: "10.00",
              topped_up_balance: "100.00",
            },
          ],
        }),
    }),
    (error) => {
      assert.ok(error instanceof DeepSeekBalanceError);
      assert.equal(error.code, "USD_BALANCE_NOT_FOUND");
      assertSafe(error);
      return true;
    },
  );
});

test("reports HTTP failures without reading or exposing the response body", async () => {
  let jsonCalled = false;

  await assert.rejects(
    queryDeepSeekBalance({
      apiKey: API_KEY,
      fetchImpl: async () => ({
        ok: false,
        status: 401,
        json: async () => {
          jsonCalled = true;
          return { error: API_KEY };
        },
      }),
    }),
    (error) => {
      assert.ok(error instanceof DeepSeekBalanceError);
      assert.equal(error.code, "HTTP_ERROR");
      assert.equal(error.status, 401);
      assert.match(error.message, /HTTP 401/);
      assertSafe(error);
      return true;
    },
  );

  assert.equal(jsonCalled, false);
});

test("reports malformed JSON without exposing parser details or the key", async () => {
  await assert.rejects(
    queryDeepSeekBalance({
      apiKey: API_KEY,
      fetchImpl: async () => ({
        ok: true,
        status: 200,
        json: async () => {
          throw new SyntaxError(`malformed payload containing ${API_KEY}`);
        },
      }),
    }),
    (error) => {
      assert.ok(error instanceof DeepSeekBalanceError);
      assert.equal(error.code, "INVALID_JSON");
      assert.equal(error.message, "DeepSeek balance response was not valid JSON.");
      assertSafe(error);
      return true;
    },
  );
});

test("never returns or includes the API key in successful or failed values", async () => {
  const balance = await queryDeepSeekBalance({
    apiKey: API_KEY,
    fetchImpl: async () =>
      jsonResponse({
        is_available: true,
        balance_infos: [
          {
            currency: "USD",
            total_balance: "1.00",
            granted_balance: "0.25",
            topped_up_balance: "0.75",
          },
        ],
      }),
  });

  const publicValues = JSON.stringify({
    balance,
    summary: formatDeepSeekBalance(balance),
    title: formatDeepSeekBalanceTitle(balance),
  });
  assert.equal(publicValues.includes(API_KEY), false);

  assert.throws(
    () =>
      selectUsdBalance({
        is_available: true,
        balance_infos: [],
      }),
    (error) => {
      assertSafe(error);
      return true;
    },
  );
});
