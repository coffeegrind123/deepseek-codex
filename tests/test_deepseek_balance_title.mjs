import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPT = path.join(ROOT, "scripts/deepseek-balance.mjs");
const API_KEY = "title-secret-key-that-must-not-leak";

async function listen(handler) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return {
    server,
    url: `http://127.0.0.1:${port}/user/balance`,
  };
}

function run(args, env = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SCRIPT, ...args], {
      cwd: ROOT,
      env: {
        ...process.env,
        DEEPSEEK_BALANCE_CACHE_FILE: "",
        DEEPSEEK_API_KEY: API_KEY,
        ...env,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });
}

test("title mode writes the USD balance as an OSC title and hides the key", async () => {
  const { server, url } = await listen((request, response) => {
    assert.equal(request.headers.authorization, `Bearer ${API_KEY}`);
    response.writeHead(200, { "content-type": "application/json" });
    response.end(
      JSON.stringify({
        is_available: true,
        balance_infos: [
          {
            currency: "USD",
            total_balance: "45.53",
            granted_balance: "0.00",
            topped_up_balance: "45.53",
          },
        ],
      }),
    );
  });

  try {
    const project = path.join(ROOT, "sample-project");
    const result = await run([
      "title",
      "--once",
      "--parent-pid",
      String(process.pid),
      "--project",
      project,
      "--endpoint",
      url,
    ]);

    assert.equal(result.code, 0, result.stderr);
    assert.match(result.stdout, /\u001b\]0;DeepSeek \$45\.53 USD \| sample-project\u0007/);
    assert.equal(result.stdout.includes(API_KEY), false);
    assert.equal(result.stderr.includes(API_KEY), false);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("show mode reports a safe HTTP error without printing the key", async () => {
  const { server, url } = await listen((_request, response) => {
    response.writeHead(401, { "content-type": "application/json" });
    response.end(JSON.stringify({ error: API_KEY }));
  });

  try {
    const result = await run(["show", "--endpoint", url]);
    assert.equal(result.code, 1);
    assert.match(result.stderr, /HTTP_ERROR: DeepSeek balance request failed with HTTP 401/);
    assert.equal(result.stdout.includes(API_KEY), false);
    assert.equal(result.stderr.includes(API_KEY), false);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("status mode emits one footer-safe balance line and hides the key", async () => {
  const { server, url } = await listen((request, response) => {
    assert.equal(request.headers.authorization, `Bearer ${API_KEY}`);
    response.writeHead(200, { "content-type": "application/json" });
    response.end(
      JSON.stringify({
        is_available: true,
        balance_infos: [
          {
            currency: "USD",
            total_balance: "18.75",
            granted_balance: "1.25",
            topped_up_balance: "17.50",
          },
        ],
      }),
    );
  });

  try {
    const result = await run(["status", "--endpoint", url]);

    assert.equal(result.code, 0, result.stderr);
    assert.equal(result.stdout, "DeepSeek $18.75 USD\n");
    assert.equal(result.stdout.includes(API_KEY), false);
    assert.equal(result.stderr.includes(API_KEY), false);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("title mode publishes a cache that status can read without network access", async () => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "deepseek-balance-cache-"));
  const cacheFile = path.join(tempDir, "balance.json");
  const { server, url } = await listen((request, response) => {
    assert.equal(request.headers.authorization, `Bearer ${API_KEY}`);
    response.writeHead(200, { "content-type": "application/json" });
    response.end(
      JSON.stringify({
        is_available: true,
        balance_infos: [
          {
            currency: "USD",
            total_balance: "23.45",
            granted_balance: "3.00",
            topped_up_balance: "20.45",
          },
        ],
      }),
    );
  });

  try {
    const titleResult = await run([
      "title",
      "--once",
      "--parent-pid",
      String(process.pid),
      "--cache-file",
      cacheFile,
      "--endpoint",
      url,
    ]);
    assert.equal(titleResult.code, 0, titleResult.stderr);

    const cachedText = await readFile(cacheFile, "utf8");
    assert.equal(cachedText.includes(API_KEY), false);

    const statusResult = await run(["status", "--cache-file", cacheFile], {
      DEEPSEEK_API_KEY: "",
    });
    assert.equal(statusResult.code, 0, statusResult.stderr);
    assert.equal(statusResult.stdout, "DeepSeek $23.45 USD\n");
    assert.equal(statusResult.stderr.includes(API_KEY), false);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(tempDir, { recursive: true, force: true });
  }
});

test("no-title mode keeps the footer cache but writes no terminal title", async () => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "deepseek-balance-no-title-"));
  const cacheFile = path.join(tempDir, "balance.json");
  const { server, url } = await listen((request, response) => {
    assert.equal(request.headers.authorization, `Bearer ${API_KEY}`);
    response.writeHead(200, { "content-type": "application/json" });
    response.end(
      JSON.stringify({
        is_available: true,
        balance_infos: [
          {
            currency: "USD",
            total_balance: "9.50",
            granted_balance: "0.50",
            topped_up_balance: "9.00",
          },
        ],
      }),
    );
  });

  try {
    const titleResult = await run([
      "title",
      "--once",
      "--no-title",
      "--parent-pid",
      String(process.pid),
      "--cache-file",
      cacheFile,
      "--endpoint",
      url,
    ]);
    assert.equal(titleResult.code, 0, titleResult.stderr);
    assert.equal(titleResult.stdout, "");

    const statusResult = await run(["status", "--cache-file", cacheFile], {
      DEEPSEEK_API_KEY: "",
    });
    assert.equal(statusResult.code, 0, statusResult.stderr);
    assert.equal(statusResult.stdout, "DeepSeek $9.50 USD\n");
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(tempDir, { recursive: true, force: true });
  }
});

test("status mode marks a cached balance as stale", async () => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "deepseek-balance-stale-"));
  const cacheFile = path.join(tempDir, "balance.json");
  await writeFile(
    cacheFile,
    JSON.stringify({
      version: 1,
      fetched_at: "2026-10-29T00:00:00.000Z",
      stale: true,
      balance: {
        currency: "USD",
        total_usd: "12.34",
        granted_usd: "0.00",
        topped_up_usd: "12.34",
        available: true,
      },
    }),
  );

  try {
    const result = await run(["status", "--cache-file", cacheFile], {
      DEEPSEEK_API_KEY: "",
    });
    assert.equal(result.code, 0, result.stderr);
    assert.equal(result.stdout, "DeepSeek $12.34 USD (stale)\n");
  } finally {
    await rm(tempDir, { recursive: true, force: true });
  }
});

test("status mode ages out a cache even when it was last written as fresh", async () => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "deepseek-balance-aged-"));
  const cacheFile = path.join(tempDir, "balance.json");
  await writeFile(
    cacheFile,
    JSON.stringify({
      version: 1,
      fetched_at: "2026-10-29T00:00:00.000Z",
      stale: false,
      balance: {
        currency: "USD",
        total_usd: "12.34",
        granted_usd: "0.00",
        topped_up_usd: "12.34",
        available: true,
      },
    }),
  );

  try {
    const result = await run(["status", "--cache-file", cacheFile], {
      DEEPSEEK_API_KEY: "",
    });
    assert.equal(result.code, 0, result.stderr);
    assert.equal(result.stdout, "DeepSeek $12.34 USD (stale)\n");
  } finally {
    await rm(tempDir, { recursive: true, force: true });
  }
});
