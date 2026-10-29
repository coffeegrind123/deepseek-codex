import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import http from "node:http";
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
