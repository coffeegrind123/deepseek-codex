// Run with: npm test  (node --test router/)
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createRouter, loadRoutes, parseUsageLimitEvent } from './router.mjs';

const TOKEN = 'test-router-token';
const listen = (server) => new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => server.close(resolve));

async function request(port, { method = 'POST', path = '/v1/responses', token = TOKEN, body, headers = {} } = {}) {
  const payload = body === undefined ? null : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path, headers: {
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(payload ? { 'content-type': 'application/json', 'content-length': payload.length } : {}),
      ...headers,
    } }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, text: Buffer.concat(chunks).toString() }));
    });
    req.on('error', reject);
    req.end(payload ?? undefined);
  });
}

let upstreamA; let upstreamB; let router; let routerPort;
const seen = [];
let limitHitsRemaining = 0;
let limitResetAt = 0;
let contentRiskHitsRemaining = 0;
let chunkedContentRiskHitsRemaining = 0;
let invalidRequestHitsRemaining = 0;
const ssePolicyHits = new Map();

function policySse(payload) {
  return `event: response.failed\ndata: ${JSON.stringify(payload)}\n\n`;
}

const normalSse = [
  'event: response.created\ndata: {"type":"response.created"}\n\n',
  'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ordinary output"}\n\n',
  'event: response.completed\ndata: {"type":"response.completed"}\n\n',
].join('');

before(async () => {
  // Upstream A mimics z.ai: real SSE on success, HTTP 200 + JSON on a bad key.
  upstreamA = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      const requestBody = JSON.parse(raw);
      seen.push({ name: 'A', url: req.url, auth: req.headers.authorization, body: requestBody });
      if (limitHitsRemaining > 0) {
        limitHitsRemaining -= 1;
        const shanghai = new Date(limitResetAt + 8 * 3600 * 1000).toISOString().replace('T', ' ').slice(0, 19);
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.end(`event: response.failed\ndata: ${JSON.stringify({ response: { error: { code: 'rate_limit_exceeded', message: `Usage limit reached for 5 hour. Your limit will reset at ${shanghai}[abc]` }, status: 'failed' }, type: 'response.failed' })}\n\nevent: error\ndata: {"code":"rate_limit_exceeded","message":"x","type":"error"}\n\ndata: [DONE]\n\n`);
        return;
      }
      if (req.headers.authorization !== 'Bearer key-a') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ code: 401, msg: 'token expired or incorrect', success: false }));
        return;
      }
      const policy = ssePolicyHits.get(requestBody.model);
      if (policy?.remaining > 0) {
        policy.remaining -= 1;
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.end(`${policy.prefix ?? ''}${policySse(policy.payload)}`);
        return;
      }
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(normalSse);
      res.end();
    });
  });
  // Upstream B mimics DeepSeek: echoes path and body for inspection.
  upstreamB = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      const requestBody = JSON.parse(raw);
      seen.push({ name: 'B', url: req.url, auth: req.headers.authorization, body: requestBody });
      if (chunkedContentRiskHitsRemaining > 0) {
        chunkedContentRiskHitsRemaining -= 1;
        const payload = JSON.stringify({
          error: {
            message: 'Content Exists Risk (chunked request_id: test-risk)',
            type: 'invalid_request_error',
            param: null,
            code: 'invalid_request_error',
          },
        });
        res.writeHead(400, { 'content-type': 'application/json' });
        res.end(payload);
        return;
      }
      if (contentRiskHitsRemaining > 0) {
        contentRiskHitsRemaining -= 1;
        const payload = JSON.stringify({
          error: {
            message: 'Content Exists Risk (request_id: test-risk)',
            type: 'invalid_request_error',
            param: null,
            code: 'invalid_request_error',
          },
        });
        res.writeHead(400, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) });
        res.end(payload);
        return;
      }
      if (invalidRequestHitsRemaining > 0) {
        invalidRequestHitsRemaining -= 1;
        const payload = JSON.stringify({
          error: {
            message: 'A different invalid request',
            type: 'invalid_request_error',
            param: null,
            code: 'invalid_request_error',
          },
        });
        res.writeHead(400, { 'content-type': 'application/json' });
        res.end(payload);
        return;
      }
      if (requestBody.stream === true) {
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.end(normalSse);
        return;
      }
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({
        path: req.url,
        auth: req.headers.authorization,
        input: requestBody.input,
        include: requestBody.include,
      }));
    });
  });
  const portA = await listen(upstreamA);
  const portB = await listen(upstreamB);
  process.env.KEY_A = 'key-a';
  process.env.KEY_B = 'key-b';
  const routes = [
    { name: 'zai-flash', match: 'glm-5.3-flash', base: new URL(`http://127.0.0.1:${portA}/api/v1`), keyEnv: 'KEY_A',
      drop: ['store'], set: {}, fallbackModel: 'glm-5-turbo',
      usageLimit: { enabled: true, resetTzOffsetMinutes: 480, maxHoldMs: 10_000, pollMs: 500, heartbeatMs: 100, resetSlackMs: 0, retryBaseMs: 100 } },
    { name: 'zai-turbo', match: 'glm-5-turbo', base: new URL(`http://127.0.0.1:${portA}/api/v1`), keyEnv: 'KEY_A',
      drop: ['top_p'], set: { temperature: 0.2 }, fallbackModel: 'deepseek-flash',
      usageLimit: { enabled: true, resetTzOffsetMinutes: 480, maxHoldMs: 10_000, pollMs: 500, heartbeatMs: 100, resetSlackMs: 0, retryBaseMs: 100 } },
    { name: 'zai-5.3', match: 'glm-5.3', base: new URL(`http://127.0.0.1:${portA}/api/v1`), keyEnv: 'KEY_A',
      drop: [], set: {}, fallbackModel: 'glm-5.3-flash',
      usageLimit: { enabled: true, resetTzOffsetMinutes: 480, maxHoldMs: 10_000, pollMs: 500, heartbeatMs: 100, resetSlackMs: 0, retryBaseMs: 100 } },
    { name: 'zai-other', match: 'glm-', base: new URL(`http://127.0.0.1:${portA}/api/v1`), keyEnv: 'KEY_A',
      drop: [], set: {}, fallbackModel: 'deepseek-flash',
      usageLimit: { enabled: true, resetTzOffsetMinutes: 480, maxHoldMs: 10_000, pollMs: 500, heartbeatMs: 100, resetSlackMs: 0, retryBaseMs: 100 } },
    { name: 'b', match: 'deepseek-', base: new URL(`http://127.0.0.1:${portB}`), keyEnv: 'KEY_B', drop: ['store'], set: {}, fallbackModel: 'glm-5.3', plainTextReasoningHistory: true,
      usageLimit: { enabled: false } },
  ];
  router = createRouter({ routes, token: TOKEN, logger: { line() {} }, idleMs: 5000 });
  routerPort = await listen(router);
});

after(async () => { await Promise.all([close(router), close(upstreamA), close(upstreamB)]); });

test('routes by model prefix, strips /v1, swaps the bearer token', async () => {
  const res = await request(routerPort, { body: { model: 'deepseek-flash', input: 'hi', store: false } });
  assert.equal(res.status, 200);
  const echoed = JSON.parse(res.text);
  assert.equal(echoed.path, '/responses');
  assert.equal(echoed.auth, 'Bearer key-b');
  const last = seen.at(-1);
  assert.equal(last.name, 'B');
  assert.equal('store' in last.body, false, 'dropFields rewrite applied');
});

test('DeepSeek reasoning history is normalized to plain text', async () => {
  const res = await request(routerPort, {
    body: {
      model: 'deepseek-flash',
      input: [
        { type: 'reasoning', id: 'native-deepseek', summary: [], content: [
          { type: 'reasoning_text', text: 'prior reasoning' },
        ], encrypted_content: 'native-deepseek-0' },
        { type: 'reasoning', id: 'glm-fallback', summary: [], content: [
          { type: 'reasoning_text', text: 'fallback reasoning' },
        ], encrypted_content: null },
      ],
      include: ['reasoning.encrypted_content'],
    },
  });
  assert.equal(res.status, 200);
  const echoed = JSON.parse(res.text);
  assert.equal(echoed.include, undefined);
  assert.deepEqual(echoed.input, [
    { type: 'reasoning', id: 'native-deepseek', summary: [], content: [
      { type: 'reasoning_text', text: 'prior reasoning' },
    ] },
    { type: 'reasoning', id: 'glm-fallback', summary: [], content: [
      { type: 'reasoning_text', text: 'fallback reasoning' },
    ] },
  ]);
});

test('DeepSeek gets compatibility reasoning for orphaned fallback tool calls', async () => {
  const res = await request(routerPort, {
    body: {
      model: 'deepseek-flash',
      input: [
        { role: 'user', content: 'work' },
        { type: 'reasoning', id: 'valid', summary: [], content: [
          { type: 'reasoning_text', text: 'valid reasoning' },
        ], encrypted_content: null },
        { type: 'function_call', id: 'valid-call', call_id: 'call_valid', name: 'tool', arguments: '{}' },
        { type: 'function_call_output', call_id: 'call_valid', output: 'ok' },
        { type: 'function_call', id: 'orphan-call', call_id: 'call_orphan', name: 'tool', arguments: '{}' },
        { type: 'function_call_output', call_id: 'call_orphan', output: 'ok' },
      ],
    },
  });
  assert.equal(res.status, 200);
  const echoed = JSON.parse(res.text);
  assert.deepEqual(echoed.input.map((item) => [item.type, item.id ?? item.call_id]), [
    [undefined, undefined],
    ['reasoning', 'valid'],
    ['function_call', 'valid-call'],
    ['function_call_output', 'call_valid'],
    ['reasoning', 'router-reasoning-call_orphan'],
    ['function_call', 'orphan-call'],
    ['function_call_output', 'call_orphan'],
  ]);
  const injected = echoed.input.find((item) => item.id === 'router-reasoning-call_orphan');
  assert.deepEqual(injected.content, [{
    type: 'reasoning_text',
    text: 'The upstream provider returned this tool call without reasoning. Preserve the tool-call sequence for DeepSeek thinking-mode compatibility.',
  }]);
});

// DeepSeek rejects (HTTP 400 "The `reasoning_text` in the thinking mode must be passed back
// to the API") a tool-calling assistant turn whose reasoning comes after its assistant
// message, and one whose tool call is a freeform custom_tool_call (apply_patch). Measured
// against api.deepseek.com on 2026-11-01: [user, reasoning, message, call] -> 200;
// [user, message, reasoning, call] -> 400; [user, custom_tool_call] -> 400.
test('compatibility reasoning goes before the assistant message of a tool-calling turn', async () => {
  const res = await request(routerPort, {
    body: {
      model: 'deepseek-flash',
      input: [
        { role: 'user', content: 'work' },
        { type: 'message', role: 'assistant', id: 'say', content: [{ type: 'output_text', text: 'Reading.' }] },
        { type: 'function_call', id: 'c1', call_id: 'call_1', name: 'tool', arguments: '{}' },
        { type: 'function_call', id: 'c2', call_id: 'call_2', name: 'tool', arguments: '{}' },
        { type: 'function_call_output', call_id: 'call_1', output: 'ok' },
        { type: 'function_call_output', call_id: 'call_2', output: 'ok' },
        { type: 'message', role: 'assistant', id: 'final', content: [{ type: 'output_text', text: 'Done.' }] },
      ],
    },
  });
  assert.equal(res.status, 200);
  const echoed = JSON.parse(res.text);
  assert.deepEqual(echoed.input.map((item) => [item.type, item.id ?? item.call_id]), [
    [undefined, undefined],
    ['reasoning', 'router-reasoning-say'],
    ['message', 'say'],
    ['function_call', 'c1'],
    ['function_call', 'c2'],
    ['function_call_output', 'call_1'],
    ['function_call_output', 'call_2'],
    ['message', 'final'],
  ]);
});

test('custom tool calls get compatibility reasoning too', async () => {
  const res = await request(routerPort, {
    body: {
      model: 'deepseek-flash',
      input: [
        { role: 'user', content: 'patch it' },
        { type: 'custom_tool_call', id: 'p1', call_id: 'call_p', name: 'apply_patch', input: '*** Begin Patch\n*** End Patch' },
        { type: 'custom_tool_call_output', call_id: 'call_p', output: 'ok' },
        { type: 'reasoning', id: 'r2', summary: [], content: [{ type: 'reasoning_text', text: 'next' }] },
        { type: 'message', role: 'assistant', id: 'm2', content: [{ type: 'output_text', text: 'More.' }] },
        { type: 'custom_tool_call', id: 'p2', call_id: 'call_p2', name: 'apply_patch', input: '*** Begin Patch\n*** End Patch' },
        { type: 'custom_tool_call_output', call_id: 'call_p2', output: 'ok' },
      ],
    },
  });
  assert.equal(res.status, 200);
  const echoed = JSON.parse(res.text);
  assert.deepEqual(echoed.input.map((item) => [item.type, item.id ?? item.call_id]), [
    [undefined, undefined],
    ['reasoning', 'router-reasoning-call_p'],
    ['custom_tool_call', 'p1'],
    ['custom_tool_call_output', 'call_p'],
    ['reasoning', 'r2'],
    ['message', 'm2'],
    ['custom_tool_call', 'p2'],
    ['custom_tool_call_output', 'call_p2'],
  ]);
});

test('prefixes the upstream base path', async () => {
  const res = await request(routerPort, { body: { model: 'glm-5.3', input: 'hi', stream: true } });
  assert.equal(res.status, 200);
  assert.match(res.headers['content-type'], /text\/event-stream/);
  assert.match(res.text, /response\.completed/);
  assert.equal(seen.at(-1).url, '/api/v1/responses');
});

test('falls back to the configured model when the upstream reports Content Exists Risk', async () => {
  contentRiskHitsRemaining = 1;
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'deepseek-flash', input: 'authorized research', store: false, stream: true },
  });
  assert.equal(res.status, 200);
  assert.match(res.text, /response\.completed/);
  assert.equal(seen.length - before, 2);
  assert.equal(seen.at(-2).name, 'B');
  assert.equal(seen.at(-1).name, 'A');
  assert.equal(seen.at(-1).body.model, 'glm-5.3');
});

test('falls back from chunked HTTP 400 Content Exists Risk', async () => {
  chunkedContentRiskHitsRemaining = 1;
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'deepseek-flash', input: 'authorized research', stream: true },
  });
  assert.equal(res.status, 200);
  assert.match(res.text, /response\.completed/);
  assert.equal(seen.length - before, 2);
  assert.equal(seen.at(-2).name, 'B');
  assert.equal(seen.at(-1).body.model, 'glm-5.3');
});

test('falls back from a glm-5.3 SSE cyber_policy failure to glm-5.3-flash', async () => {
  ssePolicyHits.set('glm-5.3', {
    remaining: 1,
    payload: {
      response: { error: { code: 'cyber_policy', message: 'blocked' }, status: 'failed' },
      type: 'response.failed',
    },
  });
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'glm-5.3', input: 'authorized research', stream: true, store: true },
  });
  assert.equal(res.status, 200);
  assert.match(res.text, /response\.completed/);
  assert.equal(seen.length - before, 2);
  assert.equal(seen.at(-2).body.model, 'glm-5.3');
  assert.equal(seen.at(-1).body.model, 'glm-5.3-flash');
  assert.equal('store' in seen.at(-1).body, false, 'next route rewrites applied');
});

test('keeps sniffing through output_item/content_part additions before cyber_policy', async () => {
  const prefix = [
    'event: response.created\ndata: {"type":"response.created"}\n\n',
    'event: response.output_item.added\ndata: {"type":"response.output_item.added","item":{"type":"reasoning"}}\n\n',
    'event: response.content_part.added\ndata: {"type":"response.content_part.added","part":{"type":"reasoning_text"}}\n\n',
  ].join('');
  ssePolicyHits.set('glm-after-item', {
    remaining: 1,
    prefix,
    payload: {
      response: { error: { code: 'cyber_policy', message: 'blocked after item setup' }, status: 'failed' },
      type: 'response.failed',
    },
  });
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'glm-after-item', input: 'authorized research', stream: true },
  });
  assert.equal(res.status, 200);
  assert.match(res.text, /response\.completed/);
  assert.equal(seen.length - before, 2);
  assert.equal(seen.at(-2).body.model, 'glm-after-item');
  assert.equal(seen.at(-1).body.model, 'deepseek-flash');
  assert.doesNotMatch(res.text, /cyber_policy/);
});

test('falls back from glm-5.3-flash to a successful glm-5-turbo', async () => {
  ssePolicyHits.set('glm-5.3-flash', {
    remaining: 1,
    payload: {
      type: 'response.failed',
      error: { code: 'cyber_policy', message: 'blocked' },
    },
  });
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'glm-5.3-flash', input: 'authorized research', stream: true, store: true, top_p: 0.9 },
  });
  assert.equal(res.status, 200);
  assert.match(res.text, /response\.completed/);
  assert.equal(seen.length - before, 2);
  assert.equal(seen.at(-2).body.model, 'glm-5.3-flash');
  assert.equal(seen.at(-1).body.model, 'glm-5-turbo');
  assert.equal(seen.at(-1).body.temperature, 0.2);
  assert.equal('top_p' in seen.at(-1).body, false);
});

test('walks the full three-model fallback chain', async () => {
  ssePolicyHits.set('glm-5.3', {
    remaining: 1,
    payload: { response: { error: { code: 'cyber_policy' } }, type: 'response.failed' },
  });
  ssePolicyHits.set('glm-5.3-flash', {
    remaining: 1,
    payload: { response: { error: { code: 'cyber_policy' } }, type: 'response.failed' },
  });
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'glm-5.3', input: 'authorized research', stream: true },
  });
  assert.equal(res.status, 200);
  assert.match(res.text, /response\.completed/);
  assert.deepEqual(
    seen.slice(before).map((entry) => entry.body.model),
    ['glm-5.3', 'glm-5.3-flash', 'glm-5-turbo'],
  );
});

test('falls back on explicit Content Exists Risk text in an SSE error', async () => {
  ssePolicyHits.set('glm-sse-risk-text', {
    remaining: 1,
    payload: {
      response: { error: { code: 'invalid_request_error', message: 'Content Exists Risk (sse)' } },
      type: 'response.failed',
    },
  });
  const res = await request(routerPort, {
    body: { model: 'glm-sse-risk-text', input: 'authorized research', stream: true },
  });
  assert.equal(res.status, 200);
  assert.match(res.text, /response\.completed/);
  assert.equal(seen.at(-1).body.model, 'deepseek-flash');
});

test('content risk falls back past a usage-limited glm-5.3', async () => {
  contentRiskHitsRemaining = 1;
  limitHitsRemaining = 1;
  limitResetAt = Date.now() + 600;
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'deepseek-flash', input: 'authorized research', stream: true },
  });
  assert.equal(res.status, 200);
  assert.doesNotMatch(res.text, /router\.hold/);
  assert.match(res.text, /response\.completed/);
  assert.deepEqual(
    seen.slice(before).map((entry) => entry.body.model),
    ['deepseek-flash', 'glm-5.3', 'glm-5.3-flash'],
  );
});

test('usage limits walk the complete GLM fallback chain', async () => {
  limitHitsRemaining = 2;
  limitResetAt = Date.now() + 600;
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'glm-5.3', input: 'authorized research', stream: true },
  });
  assert.equal(res.status, 200);
  assert.doesNotMatch(res.text, /router\.hold/);
  assert.match(res.text, /response\.completed/);
  assert.deepEqual(
    seen.slice(before).map((entry) => entry.body.model),
    ['glm-5.3', 'glm-5.3-flash', 'glm-5-turbo'],
  );
});

test('exhausted GLM usage limits continue to DeepSeek', async () => {
  limitHitsRemaining = 3;
  limitResetAt = Date.now() + 600;
  const before = seen.length;
  try {
    const res = await request(routerPort, {
      body: { model: 'glm-5.3', input: 'authorized research', stream: true },
    });
    assert.equal(res.status, 200);
    assert.doesNotMatch(res.text, /router\.hold/);
    assert.match(res.text, /response\.completed/);
    assert.deepEqual(
      seen.slice(before).map((entry) => entry.body.model),
      ['glm-5.3', 'glm-5.3-flash', 'glm-5-turbo', 'deepseek-flash'],
    );
    assert.equal(seen.at(-1).name, 'B');
  } finally {
    limitHitsRemaining = 0;
  }
});

test('a request that started on DeepSeek does not retry it after GLM exhaustion', async () => {
  const routes = [
    {
      name: 'loop-deepseek', match: 'loop-deepseek-', base: new URL(`http://127.0.0.1:${upstreamB.address().port}`),
      keyEnv: 'KEY_B', drop: [], set: {}, fallbackModel: 'loop-glm', usageLimit: { enabled: false },
    },
    {
      name: 'loop-glm', match: 'loop-glm', base: new URL(`http://127.0.0.1:${upstreamA.address().port}/api/v1`),
      keyEnv: 'KEY_A', drop: [], set: {}, fallbackModel: 'loop-deepseek-flash',
      usageLimit: { enabled: true, resetTzOffsetMinutes: 480, maxHoldMs: 0 },
    },
  ];
  const localRouter = createRouter({ routes, token: TOKEN, logger: { line() {} }, idleMs: 5000 });
  const localPort = await listen(localRouter);
  contentRiskHitsRemaining = 1;
  limitHitsRemaining = 1;
  limitResetAt = Date.now() + 60_000;
  const before = seen.length;
  try {
    const res = await request(localPort, {
      body: { model: 'loop-deepseek-flash', input: 'authorized research', stream: true },
    });
    assert.equal(res.status, 200);
    assert.doesNotMatch(res.text, /router\.hold/);
    assert.doesNotMatch(res.text, /response\.completed/);
    assert.match(res.text, /rate_limit_exceeded/);
    assert.deepEqual(
      seen.slice(before).map((entry) => entry.body.model),
      ['loop-deepseek-flash', 'loop-glm'],
    );
    assert.equal(
      seen.slice(before).filter((entry) => entry.name === 'B').length,
      1,
      'DeepSeek was attempted only once',
    );
  } finally {
    contentRiskHitsRemaining = 0;
    limitHitsRemaining = 0;
    await close(localRouter);
  }
});

test('hold occurs only after every usage-limited fallback is exhausted', async () => {
  contentRiskHitsRemaining = 1;
  limitHitsRemaining = 3;
  limitResetAt = Date.now() + 600;
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'deepseek-flash', input: 'authorized research', stream: true },
  });
  assert.equal(res.status, 200);
  assert.match(res.text, /event: router\.hold/);
  assert.match(res.text, /response\.completed/);
  assert.deepEqual(
    seen.slice(before).map((entry) => entry.body.model),
    ['deepseek-flash', 'glm-5.3', 'glm-5.3-flash', 'glm-5-turbo', 'glm-5-turbo'],
  );
});

test('passes ordinary SSE through byte-for-byte', async () => {
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'glm-5-turbo', input: 'ordinary request', stream: true },
  });
  assert.equal(res.status, 200);
  assert.equal(res.text, normalSse);
  assert.equal(seen.length - before, 1, 'ordinary output did not trigger fallback');
});

test('passes a policy SSE through unchanged when fallback cannot start', async () => {
  const payload = {
    response: { error: { code: 'cyber_policy', message: 'blocked' }, status: 'failed' },
    type: 'response.failed',
  };
  ssePolicyHits.set('blocked-model', { remaining: 1, payload });
  const localRouter = createRouter({
    routes: [
      {
        name: 'blocked-initial', match: 'blocked-', base: new URL(`http://127.0.0.1:${upstreamA.address().port}/api/v1`),
        keyEnv: 'KEY_A', drop: [], set: {}, fallbackModel: 'glm-5.3',
        usageLimit: { enabled: true, resetTzOffsetMinutes: 480, maxHoldMs: 1000, pollMs: 100, heartbeatMs: 50, resetSlackMs: 0, retryBaseMs: 50 },
      },
      {
        name: 'blocked-fallback', match: 'glm-', base: new URL(`http://127.0.0.1:${upstreamA.address().port}/api/v1`),
        keyEnv: 'KEY_MISSING', drop: [], set: {}, usageLimit: { enabled: true },
      },
    ],
    token: TOKEN,
    logger: { line() {} },
    idleMs: 5000,
  });
  const localPort = await listen(localRouter);
  try {
    const res = await request(localPort, {
      body: { model: 'blocked-model', input: 'authorized research', stream: true },
    });
    assert.equal(res.status, 200);
    assert.equal(res.text, policySse(payload));
  } finally {
    await close(localRouter);
  }
});

test('accepts intentional fallback cycles and stops when a request revisits a node', async () => {
  const base = new URL('http://127.0.0.1:1');
  const routes = [
    {
      name: 'cycle-a', match: 'cycle-a-', base: new URL(`http://127.0.0.1:${upstreamA.address().port}/api/v1`),
      keyEnv: 'KEY_A', drop: [], set: {},
      fallbackModel: 'cycle-b-1', usageLimit: { enabled: false },
    },
    {
      name: 'cycle-b', match: 'cycle-b-', base: new URL(`http://127.0.0.1:${upstreamA.address().port}/api/v1`),
      keyEnv: 'KEY_A', drop: [], set: {},
      fallbackModel: 'cycle-a-1', usageLimit: { enabled: false },
    },
  ];
  const payload = {
    response: { error: { code: 'cyber_policy', message: 'blocked' }, status: 'failed' },
    type: 'response.failed',
  };
  ssePolicyHits.set('cycle-a-1', { remaining: 1, payload });
  ssePolicyHits.set('cycle-b-1', { remaining: 1, payload });
  const localRouter = createRouter({ routes, token: TOKEN, logger: { line() {} } });
  const localPort = await listen(localRouter);
  const before = seen.length;
  try {
    const res = await request(localPort, {
      body: { model: 'cycle-a-1', input: 'authorized research', stream: true },
    });
    assert.equal(res.status, 200);
    assert.deepEqual(
      seen.slice(before).map((entry) => entry.body.model),
      ['cycle-a-1', 'cycle-b-1'],
      'the visited route+model node prevented the third attempt',
    );
    assert.match(res.text, /cyber_policy/);
  } finally {
    ssePolicyHits.delete('cycle-a-1');
    ssePolicyHits.delete('cycle-b-1');
    await close(localRouter);
  }
});

test('production routes permit the bidirectional provider fallback graph', () => {
  const routes = loadRoutes(new URL('./routes.json', import.meta.url).pathname);
  assert.doesNotThrow(() => createRouter({ routes, token: TOKEN, logger: { line() {} } }));
});

test('passes non-risk 400 responses through without falling back', async () => {
  invalidRequestHitsRemaining = 1;
  const before = seen.length;
  const res = await request(routerPort, {
    body: { model: 'deepseek-flash', input: 'ordinary bad request', stream: true },
  });
  assert.equal(res.status, 400);
  assert.match(res.text, /A different invalid request/);
  assert.equal(seen.length - before, 1);
  assert.equal(seen.at(-1).name, 'B');
});

test('rejects a wrong router token before touching any upstream', async () => {
  const before = seen.length;
  const res = await request(routerPort, { token: 'nope', body: { model: 'glm-5.3' } });
  assert.equal(res.status, 401);
  assert.equal(seen.length, before);
});

test('refuses models with no route', async () => {
  const res = await request(routerPort, { body: { model: 'gpt-5', input: 'x' } });
  assert.equal(res.status, 404);
  assert.match(JSON.parse(res.text).error.message, /no route for model "gpt-5"/);
});

test('turns a 200-with-error-body into the embedded status when SSE was requested', async () => {
  process.env.KEY_A = 'wrong';
  try {
    const res = await request(routerPort, { body: { model: 'glm-5.3', input: 'x', stream: true } });
    assert.equal(res.status, 401);
    assert.match(JSON.parse(res.text).error.message, /token expired or incorrect/);
  } finally {
    process.env.KEY_A = 'key-a';
  }
});

test('a client abort mid-stream releases the in-flight slot', async () => {
  const before = JSON.parse((await request(routerPort, { method: 'GET', path: '/health', token: null })).text);
  await new Promise((resolve) => {
    const payload = JSON.stringify({ model: 'glm-5.3', input: 'x', stream: true });
    const req = http.request({ host: '127.0.0.1', port: routerPort, method: 'POST', path: '/v1/responses',
      headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json', 'content-length': payload.length } }, (res) => {
      res.once('data', () => { req.destroy(); setTimeout(resolve, 50); });
    });
    req.on('error', () => {});
    req.end(payload);
  });
  const after = JSON.parse((await request(routerPort, { method: 'GET', path: '/health', token: null })).text);
  assert.equal(after.inFlight, before.inFlight);
});

test('rejects bodies without a model and non-JSON bodies', async () => {
  assert.equal((await request(routerPort, { body: { input: 'x' } })).status, 422);
  assert.equal((await request(routerPort, { body: 'not json' })).status, 400);
});

test('parseUsageLimitEvent reads z.ai reset times as UTC+8', () => {
  const event = 'event: response.failed\ndata: {"response":{"error":{"code":"rate_limit_exceeded","message":"Usage limit reached for 5 hour. Your limit will reset at 2026-09-20 10:10:23[2026]"}},"type":"response.failed"}';
  const parsed = parseUsageLimitEvent(event, { resetTzOffsetMinutes: 480 });
  assert.equal(parsed.resetAt, Date.UTC(2026, 8, 20, 2, 10, 23));
  assert.equal(parseUsageLimitEvent('event: response.created\ndata: {"type":"response.created"}', { resetTzOffsetMinutes: 480 }), null);
});

test('a usage-limit failure is held with heartbeats and retried after the reset time', async () => {
  const localRouter = createRouter({
    routes: [{
      name: 'hold-terminal', match: 'hold-', base: new URL(`http://127.0.0.1:${upstreamA.address().port}/api/v1`),
      keyEnv: 'KEY_A', drop: [], set: {},
      usageLimit: { enabled: true, resetTzOffsetMinutes: 480, maxHoldMs: 10_000, pollMs: 500,
        heartbeatMs: 100, resetSlackMs: 0, retryBaseMs: 100 },
    }],
    token: TOKEN,
    logger: { line() {} },
    idleMs: 5000,
  });
  const localPort = await listen(localRouter);
  limitHitsRemaining = 2;
  limitResetAt = Date.now() + 600;
  const before = seen.length;
  try {
    const res = await request(localPort, { body: { model: 'hold-model', input: 'x', stream: true } });
    assert.equal(res.status, 200);
    assert.match(res.headers['content-type'], /text\/event-stream/);
    assert.match(res.text, /^event: router\.hold\ndata: \{"type":"router\.hold"/m, 'heartbeat events were sent while holding');
    assert.match(res.text, /response\.completed/, 'real stream delivered after the hold');
    assert.doesNotMatch(res.text, /rate_limit_exceeded/, 'the failure never reached the client');
    assert.equal(seen.length - before, 3, 'two limited attempts plus the successful one');
  } finally {
    limitHitsRemaining = 0;
    await close(localRouter);
  }
});

test('a terminal usage limit fails immediately when holding is disabled', async () => {
  const localRouter = createRouter({
    routes: [{
      name: 'fail-fast', match: 'fail-fast-', base: new URL(`http://127.0.0.1:${upstreamA.address().port}/api/v1`),
      keyEnv: 'KEY_A', drop: [], set: {},
      usageLimit: { enabled: true, resetTzOffsetMinutes: 480, maxHoldMs: 0, pollMs: 100,
        heartbeatMs: 50, resetSlackMs: 0, retryBaseMs: 50 },
    }],
    token: TOKEN,
    logger: { line() {} },
    idleMs: 5000,
  });
  const localPort = await listen(localRouter);
  limitHitsRemaining = 1;
  limitResetAt = Date.now() + 60_000;
  try {
    const res = await request(localPort, {
      body: { model: 'fail-fast-model', input: 'x', stream: true },
    });
    assert.equal(res.status, 200);
    assert.doesNotMatch(res.text, /router\.hold/);
    assert.match(res.text, /event: error\ndata: .*rate_limit_exceeded/);
  } finally {
    limitHitsRemaining = 0;
    await close(localRouter);
  }
});

test('a usage limit that never clears is reported after maxHoldMs', async () => {
  const localRouter = createRouter({
    routes: [{
      name: 'never-clears', match: 'never-clears-', base: new URL(`http://127.0.0.1:${upstreamA.address().port}/api/v1`),
      keyEnv: 'KEY_A', drop: [], set: {},
      usageLimit: { enabled: true, resetTzOffsetMinutes: 480, maxHoldMs: 100, pollMs: 20,
        heartbeatMs: 10, resetSlackMs: 0, retryBaseMs: 20 },
    }],
    token: TOKEN,
    logger: { line() {} },
    idleMs: 5000,
  });
  const localPort = await listen(localRouter);
  limitHitsRemaining = 1000;
  limitResetAt = Date.now() - 1000; // stale reset time -> poll cadence
  try {
    const res = await request(localPort, { body: { model: 'never-clears-model', input: 'x', stream: true } });
    assert.equal(res.status, 200);
    assert.match(res.text, /event: error\ndata: .*rate_limit_exceeded/);
  } finally {
    limitHitsRemaining = 0;
    await close(localRouter);
  }
});
