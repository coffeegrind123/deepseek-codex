import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const PLUGIN_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const SCRIPT = path.join(PLUGIN_ROOT, "scripts/subagent-start.mjs");

function runHook(input) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SCRIPT], {
      stdio: ["pipe", "pipe", "pipe"],
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
    child.stdin.end(JSON.stringify(input));
  });
}

async function writeTranscript() {
  const dir = await mkdtemp(path.join(os.tmpdir(), "subagent-labels-"));
  const transcript = path.join(dir, "child.jsonl");
  const rows = [
    {
      type: "session_meta",
      payload: {
        source: {
          subagent: {
            thread_spawn: {
              agent_nickname: "Updater Engineer",
              agent_role: "worker",
            },
          },
        },
      },
    },
    {
      type: "response_item",
      payload: {
        type: "message",
        role: "user",
        content: [
          {
            type: "input_text",
            text: "<environment_context>\n<cwd>/tmp</cwd>\n</environment_context>",
          },
        ],
      },
    },
    {
      type: "response_item",
      payload: {
        type: "message",
        role: "user",
        content: [
          {
            type: "input_text",
            text: "Fix the staged updater transaction. Own only codex-update and its tests.",
          },
        ],
      },
    },
  ];
  await writeFile(transcript, `${rows.map(JSON.stringify).join("\n")}\n`);
  return transcript;
}

test("emits nickname, task summary, role, and model", async () => {
  const transcriptPath = await writeTranscript();
  const result = await runHook({
    agent_id: "01900000-0000-7000-8000-000000000000",
    agent_type: "worker",
    cwd: "/tmp",
    hook_event_name: "SubagentStart",
    model: "deepseek-flash",
    permission_mode: "bypassPermissions",
    session_id: "01900000-0000-7000-8000-000000000001",
    transcript_path: transcriptPath,
    turn_id: "turn-1",
  });

  assert.equal(result.code, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.match(output.systemMessage, /^Updater Engineer started:/);
  assert.match(output.systemMessage, /Fix the staged updater transaction/);
  assert.match(output.systemMessage, /\[worker\] · deepseek-flash$/);
  assert.equal(output.systemMessage.includes("01900000"), false);
});

test("falls back to the role when transcript metadata is unavailable", async () => {
  const result = await runHook({
    agent_id: "01900000-0000-7000-8000-000000000000",
    agent_type: "explorer",
    cwd: "/tmp",
    hook_event_name: "SubagentStart",
    model: "deepseek-flash",
    permission_mode: "bypassPermissions",
    session_id: "01900000-0000-7000-8000-000000000001",
    transcript_path: "/tmp/does-not-exist.jsonl",
    turn_id: "turn-2",
  });

  assert.equal(result.code, 0, result.stderr);
  assert.equal(
    JSON.parse(result.stdout).systemMessage,
    "Codebase Explorer started [explorer] · deepseek-flash",
  );
});

test("labels the spawn tool call before a UUID exists", async () => {
  const result = await runHook({
    cwd: "/home/claudeuser/codex",
    hook_event_name: "PreToolUse",
    model: "deepseek-flash",
    permission_mode: "bypassPermissions",
    session_id: "01900000-0000-7000-8000-000000000001",
    tool_input: {
      agent_type: "explorer",
      message:
        "Trace the DeepSeek balance request and report the exact endpoint.",
    },
    tool_name: "spawn_agent",
    tool_use_id: "call-1",
    transcript_path: null,
    turn_id: "turn-3",
  });

  assert.equal(result.code, 0, result.stderr);
  assert.equal(
    JSON.parse(result.stdout).systemMessage,
    "Codebase Explorer starting: Trace the DeepSeek balance request and report the exact endpoint. [explorer]",
  );
});
