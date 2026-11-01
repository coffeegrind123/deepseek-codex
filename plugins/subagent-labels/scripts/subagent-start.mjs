#!/usr/bin/env node

import { appendFileSync, readFileSync } from "node:fs";

const ROLE_NAMES = {
  default: "General Subagent",
  explorer: "Codebase Explorer",
  worker: "Implementation Worker",
  verifier: "Independent Verifier",
};

const SKIP_PREFIXES = [
  "# AGENTS.md instructions",
  "<environment_context>",
  "<permissions instructions>",
  "<skills_instructions>",
  "<hook_prompt>",
];

function readStdin() {
  return readFileSync(0, "utf8");
}

function cleanText(text) {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function shouldSkip(text) {
  const trimmed = text.trim();
  return (
    trimmed.length === 0 ||
    SKIP_PREFIXES.some((prefix) => trimmed.startsWith(prefix))
  );
}

function extractUserText(item) {
  if (item?.type !== "response_item") {
    return null;
  }
  const payload = item.payload;
  if (payload?.type !== "message" || payload.role !== "user") {
    return null;
  }

  const parts = [];
  for (const content of payload.content ?? []) {
    if (content?.type === "input_text" && typeof content.text === "string") {
      parts.push(content.text);
    }
  }

  return parts.length > 0 ? parts.join("\n") : null;
}

function extractSessionIdentity(item) {
  if (item?.type !== "session_meta") {
    return {};
  }
  const payload = item.payload ?? {};
  const source = payload.source?.subagent?.thread_spawn ?? {};
  return {
    nickname:
      payload.agent_nickname ?? source.agent_nickname ?? source.nickname ?? null,
    role: payload.agent_role ?? source.agent_role ?? null,
  };
}

function readTranscript(path) {
  const lines = readFileSync(path, "utf8").split(/\r?\n/);
  const identity = {};
  let task = null;

  for (const line of lines) {
    if (!line.trim()) {
      continue;
    }

    let item;
    try {
      item = JSON.parse(line);
    } catch {
      continue;
    }

    const foundIdentity = extractSessionIdentity(item);
    if (foundIdentity.nickname && !identity.nickname) {
      identity.nickname = foundIdentity.nickname;
    }
    if (foundIdentity.role && !identity.role) {
      identity.role = foundIdentity.role;
    }

    const text = extractUserText(item);
    if (text && !shouldSkip(text)) {
      task = cleanText(text);
      break;
    }
  }

  return { ...identity, task };
}

function compactTask(text, maxLength = 110) {
  if (!text) {
    return "";
  }

  const firstSentence = text.split(/(?<=[.!?])\s/)[0] ?? text;
  if (firstSentence.length <= maxLength) {
    return firstSentence;
  }

  return `${firstSentence.slice(0, maxLength - 1).trimEnd()}…`;
}

function inputText(items) {
  const parts = [];
  for (const item of items ?? []) {
    if (item?.type === "text" && typeof item.text === "string") {
      parts.push(item.text);
    }
    if (item?.type === "input_text" && typeof item.text === "string") {
      parts.push(item.text);
    }
  }
  return parts.join("\n");
}

function roleName(role) {
  return ROLE_NAMES[role] ?? role.replaceAll("_", " ");
}

function buildPendingLabel(hookInput) {
  const toolInput = hookInput.tool_input ?? {};
  const role = toolInput.agent_type?.trim() || "default";
  const task = compactTask(
    cleanText(toolInput.message ?? inputText(toolInput.items)),
  );
  const taskSuffix = task ? `: ${task}` : " a delegated task";
  const roleSuffix = role !== "default" ? ` [${role}]` : "";
  return `${roleName(role)} starting${taskSuffix}${roleSuffix}`;
}

function buildStartLabel(hookInput) {
  const role = hookInput.agent_type?.trim() || "default";
  const roleName = ROLE_NAMES[role] ?? role.replaceAll("_", " ");
  let identity = {};

  if (hookInput.transcript_path) {
    try {
      identity = readTranscript(hookInput.transcript_path);
    } catch {
      // The start hook may race transcript creation; role/model still provide a label.
    }
  }

  const label = identity.nickname?.trim() || roleName;
  const task = compactTask(identity.task);
  const roleSuffix = role && role !== "default" ? ` [${role}]` : "";
  const modelSuffix = hookInput.model ? ` · ${hookInput.model}` : "";

  if (task) {
    return `${label} started: ${task}${roleSuffix}${modelSuffix}`;
  }

  return `${label} started${roleSuffix}${modelSuffix}`;
}

function main() {
  const hookInput = JSON.parse(readStdin());
  const systemMessage =
    hookInput.hook_event_name === "PreToolUse"
      ? buildPendingLabel(hookInput)
      : buildStartLabel(hookInput);
  if (process.env.CODEX_SUBAGENT_LABEL_LOG) {
    appendFileSync(
      process.env.CODEX_SUBAGENT_LABEL_LOG,
      `${JSON.stringify({
        hook_event_name: hookInput.hook_event_name,
        tool_name: hookInput.tool_name ?? null,
        agent_type: hookInput.agent_type ?? hookInput.tool_input?.agent_type ?? null,
        systemMessage,
      })}\n`,
    );
  }
  process.stdout.write(`${JSON.stringify({ systemMessage })}\n`);
}

try {
  main();
} catch {
  const fallback = {
    systemMessage: "A subagent started (human-readable label unavailable)",
  };
  process.stdout.write(`${JSON.stringify(fallback)}\n`);
}
