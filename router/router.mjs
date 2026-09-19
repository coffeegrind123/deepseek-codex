#!/usr/bin/env node
// Local model router for Codex.
//
// Codex only supports ONE model_provider per session, and agent roles can override
// `model` but not `model_provider` (codex-rs/core/src/agent/role.rs, AgentRoleOverrides).
// To run the orchestrator on z.ai GLM and subagents on DeepSeek, Codex points at this
// process and the router picks the upstream from the `model` field of each request:
//
//   Codex ──► 127.0.0.1:PORT/v1/responses ──┬─ glm-*      ──► https://api.z.ai/api/v1/responses
//             (Authorization: router token) └─ deepseek-* ──► https://api.deepseek.com/responses
//
// The router swaps the bearer token for the upstream key, streams the body back
// untouched (SSE passthrough), and logs one line per request with raw evidence.
// Everything it needs comes from the environment (see .codex/secrets.env).

import http from 'node:http';
import https from 'node:https';
import { URL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { timingSafeEqual } from 'node:crypto';

const HTTP_OK = 200;
const HTTP_BAD_REQUEST = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_NOT_FOUND = 404;
const HTTP_METHOD_NOT_ALLOWED = 405;
const HTTP_PAYLOAD_TOO_LARGE = 413;
const HTTP_UNPROCESSABLE = 422;
const HTTP_BAD_GATEWAY = 502;
const HTTP_GATEWAY_TIMEOUT = 504;

const DEFAULT_PORT = 8877;
const BIND_HOST = '127.0.0.1';
// Codex prompts carry the whole context window; 1M tokens of UTF-8 stays well under this.
const MAX_BODY_BYTES = 64 * 1024 * 1024;
// Upstream socket silence tolerated before the request is failed. Codex has its own
// stream_idle_timeout_ms; this only guards against half-open sockets it cannot see.
const DEFAULT_UPSTREAM_IDLE_MS = 600_000;
const SOFT_ERROR_CAPTURE_BYTES = 1024 * 1024;
const HEALTH_PATH = '/health';
const ROUTES_PATH = '/routes';
// Hop-by-hop headers must not be forwarded (RFC 9110 §7.6.1); the rest are rewritten.
const DROPPED_REQUEST_HEADERS = new Set([
  'host', 'authorization', 'connection', 'keep-alive', 'proxy-authorization',
  'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length',
]);
const DROPPED_RESPONSE_HEADERS = new Set([
  'connection', 'keep-alive', 'transfer-encoding', 'content-length',
]);

const here = path.dirname(fileURLToPath(import.meta.url));

/**
 * Route table. Order matters: first prefix match wins.
 * `keyEnv` names the environment variable holding the upstream API key so keys never
 * appear in this file or in logs. `base` is the upstream prefix that replaces
 * Codex's provider base_url (`.../v1`).
 */
function loadRoutes() {
  const routesFile = process.env.CODEX_ROUTER_ROUTES ?? path.join(here, 'routes.json');
  const raw = fs.readFileSync(routesFile, 'utf8');
  const parsed = JSON.parse(raw);
  if (!Array.isArray(parsed.routes) || parsed.routes.length === 0) {
    throw new Error(`${routesFile}: "routes" must be a non-empty array`);
  }
  return parsed.routes.map((route, index) => {
    for (const field of ['name', 'match', 'base', 'keyEnv']) {
      if (typeof route[field] !== 'string' || route[field].length === 0) {
        throw new Error(`${routesFile}: routes[${index}].${field} must be a non-empty string`);
      }
    }
    const base = new URL(route.base);
    if (base.protocol !== 'https:' && base.protocol !== 'http:') {
      throw new Error(`${routesFile}: routes[${index}].base must be http(s)`);
    }
    return {
      name: route.name,
      match: route.match,
      base,
      keyEnv: route.keyEnv,
      // Optional per-upstream request rewrites, applied to the parsed JSON body.
      drop: Array.isArray(route.dropFields) ? route.dropFields : [],
      set: typeof route.setFields === 'object' && route.setFields !== null ? route.setFields : {},
    };
  });
}

function requireEnv(name) {
  const value = process.env[name];
  if (!value) {
    throw new Error(`environment variable ${name} is not set (source .codex/secrets.env)`);
  }
  return value;
}

function nowIso() {
  return new Date().toISOString();
}

class Logger {
  constructor(file) {
    this.stream = file ? fs.createWriteStream(file, { flags: 'a' }) : null;
  }

  line(fields) {
    const text = `${nowIso()} ${Object.entries(fields)
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join(' ')}`;
    if (this.stream) {
      this.stream.write(`${text}\n`);
      return;
    }
    process.stdout.write(`${text}\n`);
  }
}

function sendJson(res, status, body) {
  const payload = Buffer.from(JSON.stringify(body));
  res.writeHead(status, {
    'content-type': 'application/json',
    'content-length': payload.length,
  });
  res.end(payload);
}

/** Codex-style error envelope so the CLI renders the message instead of a raw status. */
function sendError(res, status, code, message, extra = {}) {
  sendJson(res, status, { error: { type: code, code, message, ...extra } });
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(Object.assign(new Error('request body too large'), { status: HTTP_PAYLOAD_TOO_LARGE }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function constantTimeEquals(a, b) {
  const bufA = Buffer.from(a);
  const bufB = Buffer.from(b);
  if (bufA.length !== bufB.length) {
    return false;
  }
  return timingSafeEqual(bufA, bufB);
}

function bearerToken(req) {
  const header = req.headers.authorization ?? '';
  const [scheme, token] = header.split(' ', 2);
  if (!scheme || scheme.toLowerCase() !== 'bearer' || !token) {
    return null;
  }
  return token.trim();
}

function selectRoute(routes, model) {
  return routes.find((route) => model.startsWith(route.match)) ?? null;
}

/**
 * Strip Codex's provider prefix so `/v1/responses` becomes `/responses` before it is
 * appended to the upstream base. The base already carries whatever version segment
 * the upstream wants (`/api/v1` for z.ai, none for DeepSeek).
 */
function upstreamUrl(base, requestUrl) {
  const incoming = new URL(requestUrl, 'http://router.invalid');
  const relative = incoming.pathname.replace(/^\/v1(?=\/|$)/, '');
  const basePath = base.pathname.replace(/\/$/, '');
  const target = new URL(base.toString());
  target.pathname = `${basePath}${relative}`;
  target.search = incoming.search;
  return target;
}

function applyRewrites(route, body) {
  if (route.drop.length === 0 && Object.keys(route.set).length === 0) {
    return null;
  }
  for (const field of route.drop) {
    delete body[field];
  }
  Object.assign(body, route.set);
  return Buffer.from(JSON.stringify(body));
}

/** Map an upstream 200-with-error body to an HTTP status Codex can act on. */
function classifySoftError(text) {
  let parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { status: HTTP_BAD_GATEWAY, message: `expected text/event-stream, got: ${text.slice(0, 200)}` };
  }
  const code = Number(parsed?.code ?? parsed?.error?.code ?? parsed?.error?.status);
  const status = Number.isInteger(code) && code >= 400 && code <= 599 ? code : HTTP_BAD_GATEWAY;
  const message = parsed?.msg ?? parsed?.message ?? parsed?.error?.message ?? text.slice(0, 200);
  return { status, message: String(message) };
}

function forwardHeaders(req, apiKey, contentLength) {
  const headers = {};
  for (const [name, value] of Object.entries(req.headers)) {
    if (!DROPPED_REQUEST_HEADERS.has(name.toLowerCase())) {
      headers[name] = value;
    }
  }
  headers.authorization = `Bearer ${apiKey}`;
  headers['content-length'] = String(contentLength);
  return headers;
}

export function createRouter({ routes, token, logger, idleMs = DEFAULT_UPSTREAM_IDLE_MS }) {
  const startedAt = nowIso();
  let inFlight = 0;
  let served = 0;

  async function handle(req, res) {
    const requestUrl = new URL(req.url, 'http://router.invalid');

    if (req.method === 'GET' && requestUrl.pathname === HEALTH_PATH) {
      sendJson(res, HTTP_OK, { ok: true, startedAt, inFlight, served, pid: process.pid });
      return;
    }

    if (req.method === 'GET' && requestUrl.pathname === ROUTES_PATH) {
      sendJson(res, HTTP_OK, {
        routes: routes.map((r) => ({
          name: r.name, match: r.match, base: r.base.toString(), keyEnv: r.keyEnv,
          keyPresent: Boolean(process.env[r.keyEnv]),
        })),
      });
      return;
    }

    const presented = bearerToken(req);
    if (presented === null || !constantTimeEquals(presented, token)) {
      logger.line({ event: 'reject', reason: 'bad-router-token', path: requestUrl.pathname, from: req.socket.remoteAddress });
      sendError(res, HTTP_UNAUTHORIZED, 'router_unauthorized',
        'router token mismatch: CODEX_ROUTER_TOKEN in the Codex environment must equal the one the router was started with');
      return;
    }

    if (req.method !== 'POST') {
      sendError(res, HTTP_METHOD_NOT_ALLOWED, 'router_method_not_allowed',
        `router only forwards POST requests carrying a "model" field; got ${req.method} ${requestUrl.pathname}`);
      return;
    }

    let rawBody;
    try {
      rawBody = await readBody(req, MAX_BODY_BYTES);
    } catch (err) {
      sendError(res, err.status ?? HTTP_BAD_REQUEST, 'router_body_error', err.message);
      return;
    }

    let body;
    try {
      body = JSON.parse(rawBody.toString('utf8'));
    } catch (err) {
      logger.line({ event: 'reject', reason: 'non-json', path: requestUrl.pathname, size: rawBody.length, head: rawBody.subarray(0, 64).toString('utf8') });
      sendError(res, HTTP_BAD_REQUEST, 'router_invalid_json', `request body is not JSON: ${err.message}`);
      return;
    }

    const model = typeof body?.model === 'string' ? body.model : null;
    if (!model) {
      logger.line({ event: 'reject', reason: 'no-model', path: requestUrl.pathname, keys: Object.keys(body ?? {}) });
      sendError(res, HTTP_UNPROCESSABLE, 'router_missing_model', 'request has no "model" field; the router cannot choose an upstream');
      return;
    }

    const route = selectRoute(routes, model);
    if (!route) {
      logger.line({ event: 'reject', reason: 'unrouted-model', model, known: routes.map((r) => r.match) });
      sendError(res, HTTP_NOT_FOUND, 'router_unknown_model',
        `no route for model "${model}". Known prefixes: ${routes.map((r) => `${r.match}* -> ${r.name}`).join(', ')}`);
      return;
    }

    const apiKey = process.env[route.keyEnv];
    if (!apiKey) {
      logger.line({ event: 'reject', reason: 'missing-upstream-key', model, route: route.name, keyEnv: route.keyEnv });
      sendError(res, HTTP_BAD_GATEWAY, 'router_missing_upstream_key',
        `route ${route.name} needs ${route.keyEnv} in the router's environment`);
      return;
    }

    const outgoingBody = applyRewrites(route, body) ?? rawBody;
    const target = upstreamUrl(route.base, req.url);
    const stream = body.stream === true;
    const started = Date.now();
    inFlight += 1;

    const transport = target.protocol === 'https:' ? https : http;
    const upstream = transport.request(target, {
      method: 'POST',
      headers: forwardHeaders(req, apiKey, outgoingBody.length),
    });

    let bytesOut = 0;
    let finished = false;
    const finish = (fields) => {
      if (finished) {
        return;
      }
      finished = true;
      inFlight -= 1;
      served += 1;
      logger.line({
        event: 'proxy', model, route: route.name, path: target.pathname, stream,
        ms: Date.now() - started, reqBytes: outgoingBody.length, resBytes: bytesOut, ...fields,
      });
    };

    upstream.setTimeout(idleMs, () => {
      upstream.destroy(Object.assign(new Error(`upstream idle for ${idleMs} ms`), { code: 'ROUTER_IDLE' }));
    });

    upstream.on('response', (up) => {
      // z.ai answers some failures (bad key, quota) with HTTP 200 and a JSON body such as
      // {"code":401,"msg":"token expired or incorrect","success":false}. Codex asked for
      // SSE, so a non-SSE 200 is always an error; surface it with a real status so the
      // CLI shows the upstream message instead of a stream parse failure.
      const contentType = String(up.headers['content-type'] ?? '');
      if (stream && up.statusCode === HTTP_OK && !contentType.startsWith('text/event-stream')) {
        const chunks = [];
        let size = 0;
        up.on('data', (chunk) => {
          if (size < SOFT_ERROR_CAPTURE_BYTES) {
            chunks.push(chunk);
            size += chunk.length;
          }
        });
        up.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          const { status, message } = classifySoftError(text);
          bytesOut = size;
          finish({ status, softError: true, upstreamError: text.slice(0, 2048) });
          sendError(res, status, 'router_upstream_soft_error', `${route.name}: ${message}`);
        });
        up.on('error', (err) => {
          finish({ status: HTTP_BAD_GATEWAY, error: `upstream body: ${err.message}` });
          sendError(res, HTTP_BAD_GATEWAY, 'router_upstream_error', err.message);
        });
        return;
      }

      const headers = {};
      for (const [name, value] of Object.entries(up.headers)) {
        if (!DROPPED_RESPONSE_HEADERS.has(name.toLowerCase())) {
          headers[name] = value;
        }
      }
      res.writeHead(up.statusCode, headers);

      // Keep the first bytes of an upstream error so the log carries evidence, not a verdict.
      const errorHead = [];
      let errorHeadBytes = 0;
      up.on('data', (chunk) => {
        bytesOut += chunk.length;
        if (up.statusCode >= 400 && errorHeadBytes < 2048) {
          errorHead.push(chunk.subarray(0, 2048 - errorHeadBytes));
          errorHeadBytes += chunk.length;
        }
        upstream.setTimeout(idleMs);
      });
      up.on('end', () => {
        const fields = { status: up.statusCode };
        if (up.statusCode >= 400) {
          fields.upstreamError = Buffer.concat(errorHead).toString('utf8');
        }
        finish(fields);
      });
      up.on('error', (err) => finish({ status: up.statusCode, error: `upstream body: ${err.message}` }));
      // A client abort mid-stream tears the upstream down without 'end' or 'error'; 'close'
      // always fires, so it is the accounting backstop.
      up.on('close', () => finish({ status: up.statusCode, error: 'stream closed before end' }));
      up.pipe(res);
    });

    upstream.on('error', (err) => {
      const timedOut = err.code === 'ROUTER_IDLE';
      finish({ status: timedOut ? HTTP_GATEWAY_TIMEOUT : HTTP_BAD_GATEWAY, error: err.message });
      if (!res.headersSent) {
        sendError(res, timedOut ? HTTP_GATEWAY_TIMEOUT : HTTP_BAD_GATEWAY, 'router_upstream_error',
          `${route.name} (${target.host}): ${err.message}`);
      } else {
        res.destroy();
      }
    });

    // Codex aborting (interrupt, retry) must release the upstream connection too. `res`
    // 'close' is the reliable signal: once the body has been consumed, `req` may stay
    // silent, and a paused pipe into a dead socket never reaches 'end' on the upstream.
    res.on('close', () => {
      if (!finished) {
        upstream.destroy(new Error('client closed'));
        finish({ status: HTTP_BAD_GATEWAY, error: 'client closed before upstream finished' });
      }
    });

    upstream.end(outgoingBody);
  }

  const server = http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      logger.line({ event: 'internal-error', error: err.stack ?? String(err) });
      if (!res.headersSent) {
        sendError(res, HTTP_BAD_GATEWAY, 'router_internal_error', err.message);
      } else {
        res.destroy();
      }
    });
  });
  server.stats = () => ({ inFlight, served });
  return server;
}

function main() {
  const routes = loadRoutes();
  const token = requireEnv('CODEX_ROUTER_TOKEN');
  const port = Number(process.env.CODEX_ROUTER_PORT ?? DEFAULT_PORT);
  const idleMs = Number(process.env.CODEX_ROUTER_IDLE_MS ?? DEFAULT_UPSTREAM_IDLE_MS);
  const logger = new Logger(process.env.CODEX_ROUTER_LOG ?? null);

  for (const route of routes) {
    if (!process.env[route.keyEnv]) {
      logger.line({ event: 'warn', message: `${route.keyEnv} unset; requests for ${route.match}* will fail with 502` });
    }
  }

  const server = createRouter({ routes, token, logger, idleMs });
  server.keepAliveTimeout = 65_000;
  server.requestTimeout = 0;
  server.headersTimeout = 60_000;

  server.listen(port, BIND_HOST, () => {
    logger.line({
      event: 'listen', url: `http://${BIND_HOST}:${port}/v1`, pid: process.pid,
      routes: routes.map((r) => `${r.match}* -> ${r.name} (${r.base})`),
    });
  });

  const shutdown = (signal) => {
    logger.line({ event: 'shutdown', signal, ...server.stats() });
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 5_000).unref();
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
