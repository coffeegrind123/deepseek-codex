import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createMcpMessageHandler } from "../mcp/server.mjs";

const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const API_KEY = "mcp-secret-key-that-must-not-leak";

test("plugin manifest and MCP wiring load the local stdio server", async () => {
  const manifest = JSON.parse(
    await readFile(path.join(PLUGIN_ROOT, ".codex-plugin/plugin.json"), "utf8"),
  );
  const mcp = JSON.parse(
    await readFile(path.join(PLUGIN_ROOT, ".mcp.json"), "utf8"),
  );
  const server = mcp.mcpServers["deepseek-balance"];

  assert.equal(manifest.name, "deepseek-balance");
  assert.equal(manifest.skills, "./skills/");
  assert.equal(manifest.mcpServers, "./.mcp.json");
  assert.equal(server.command, "node");
  assert.deepEqual(server.args, ["./mcp/server.mjs"]);
  assert.deepEqual(server.env_vars, ["DEEPSEEK_API_KEY"]);
});

test("MCP tool call returns structured USD balance without leaking the key", async () => {
  let authorization;
  const handleMessage = createMcpMessageHandler({
    env: {
      DEEPSEEK_API_KEY: API_KEY,
    },
    fetchImpl: async (_url, init) => {
      authorization = init.headers.Authorization;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          is_available: true,
          balance_infos: [
            {
              currency: "CNY",
              total_balance: "20.00",
              granted_balance: "5.00",
              topped_up_balance: "15.00",
            },
            {
              currency: "USD",
              total_balance: "3.50",
              granted_balance: "0.50",
              topped_up_balance: "3.00",
            },
          ],
        }),
      };
    },
  });

  const initialized = await handleMessage({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: {
        name: "test",
        version: "1.0.0",
      },
    },
  });
  assert.equal(initialized.result.serverInfo.name, "deepseek-balance");

  const listed = await handleMessage({
    jsonrpc: "2.0",
    id: 2,
    method: "tools/list",
  });
  assert.equal(listed.result.tools[0].name, "get_deepseek_balance");

  const called = await handleMessage({
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: {
      name: "get_deepseek_balance",
      arguments: {},
    },
  });

  assert.equal(authorization, `Bearer ${API_KEY}`);
  assert.deepEqual(called.result.structuredContent, {
    currency: "USD",
    total_usd: "3.50",
    granted_usd: "0.50",
    topped_up_usd: "3.00",
    available: true,
  });
  assert.equal(called.result.isError, false);
  assert.equal(JSON.stringify(called).includes(API_KEY), false);
});
