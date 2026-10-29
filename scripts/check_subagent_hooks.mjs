#!/usr/bin/env node

import { spawn } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CODEX = path.join(ROOT, "node_modules/.bin/codex");
const REQUIRED_EVENTS = new Set(["preToolUse", "subagentStart"]);
const TIMEOUT_MS = 15_000;

function fail(message) {
  process.stderr.write(`check_subagent_hooks: ${message}\n`);
  process.exitCode = 1;
}

const child = spawn(CODEX, ["app-server", "--stdio"], {
  cwd: ROOT,
  env: {
    ...process.env,
    CODEX_HOME: path.join(ROOT, ".codex"),
  },
  stdio: ["pipe", "pipe", "pipe"],
});
const lines = readline.createInterface({
  input: child.stdout,
  crlfDelay: Infinity,
});
let stderr = "";
let finished = false;

child.stderr.on("data", (chunk) => {
  stderr += chunk;
});

const timer = setTimeout(() => {
  finish(`timed out after ${TIMEOUT_MS}ms${stderr ? `: ${stderr.trim()}` : ""}`);
}, TIMEOUT_MS);

function finish(error) {
  if (finished) {
    return;
  }
  finished = true;
  clearTimeout(timer);
  child.kill("SIGTERM");
  if (error) {
    fail(error);
    return;
  }
  process.stdout.write("subagent hook trust: preToolUse and subagentStart trusted\n");
}

function send(message) {
  child.stdin.write(`${JSON.stringify(message)}\n`);
}

child.on("error", (error) => {
  finish(error.message);
});

child.on("close", (code, signal) => {
  if (!finished) {
    finish(
      `app-server exited before hooks/list responded (code=${code}, signal=${signal})`,
    );
  }
});

lines.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return;
  }

  if (message.id === 1) {
    send({ jsonrpc: "2.0", method: "notifications/initialized" });
    send({
      jsonrpc: "2.0",
      id: 2,
      method: "hooks/list",
      params: { cwds: [ROOT] },
    });
    return;
  }

  if (message.id !== 2) {
    return;
  }

  if (message.error) {
    finish(`hooks/list failed: ${message.error.message}`);
    return;
  }

  const hooks = message.result?.data?.[0]?.hooks ?? [];
  for (const event of REQUIRED_EVENTS) {
    const hook = hooks.find((entry) => entry.eventName === event);
    if (!hook) {
      finish(`missing ${event} hook from .codex/hooks.json`);
      return;
    }
    if (!hook.enabled || hook.trustStatus !== "trusted") {
      finish(
        `${event} hook is ${hook.enabled ? hook.trustStatus : "disabled"}; ` +
          `update hooks.state for key ${hook.key} to ${hook.currentHash}`,
      );
      return;
    }
  }

  finish();
});

send({
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: {
    clientInfo: {
      name: "codex-workspace-hook-check",
      version: "1.0.0",
    },
    capabilities: {
      experimentalApi: true,
    },
  },
});
