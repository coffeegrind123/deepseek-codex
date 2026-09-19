// Run with: npm test  (node --test router/)
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createRouter } from './router.mjs';

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

before(async () => {
  // Upstream A mimics z.ai: real SSE on success, HTTP 200 + JSON on a bad key.
  upstreamA = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      seen.push({ name: 'A', url: req.url, auth: req.headers.authorization, body: JSON.parse(raw) });
      if (req.headers.authorization !== 'Bearer key-a') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ code: 401, msg: 'token expired or incorrect', success: false }));
        return;
      }
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('event: response.created\ndata: {}\n\n');
      setTimeout(() => { res.write('event: response.completed\ndata: {"ok":true}\n\n'); res.end(); }, 20);
    });
  });
  // Upstream B mimics DeepSeek: echoes path and body for inspection.
  upstreamB = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      seen.push({ name: 'B', url: req.url, auth: req.headers.authorization, body: JSON.parse(raw) });
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ path: req.url, auth: req.headers.authorization }));
    });
  });
  const portA = await listen(upstreamA);
  const portB = await listen(upstreamB);
  process.env.KEY_A = 'key-a';
  process.env.KEY_B = 'key-b';
  const routes = [
    { name: 'a', match: 'glm-', base: new URL(`http://127.0.0.1:${portA}/api/v1`), keyEnv: 'KEY_A', drop: [], set: {} },
    { name: 'b', match: 'deepseek-', base: new URL(`http://127.0.0.1:${portB}`), keyEnv: 'KEY_B', drop: ['store'], set: {} },
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

test('prefixes the upstream base path', async () => {
  const res = await request(routerPort, { body: { model: 'glm-5.3', input: 'hi', stream: true } });
  assert.equal(res.status, 200);
  assert.match(res.headers['content-type'], /text\/event-stream/);
  assert.match(res.text, /response\.completed/);
  assert.equal(seen.at(-1).url, '/api/v1/responses');
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
