#!/usr/bin/env node

import { basename } from "node:path";
import { readFileSync, writeSync } from "node:fs";

import {
  DeepSeekBalanceError,
  formatDeepSeekBalance,
  formatDeepSeekBalanceTitle,
  queryDeepSeekBalance,
} from "../plugins/deepseek-balance/lib/deepseek-balance.mjs";

const DEFAULT_INTERVAL_MS = 60_000;
const MIN_INTERVAL_MS = 15_000;
const DEFAULT_TITLE = "Codex";

function parseArgs(argv) {
  const [command = "show", ...rest] = argv;
  const options = {
    command,
    json: false,
    once: false,
    parentPid: null,
    ttyFd: 1,
    project: "",
    endpoint: process.env.DEEPSEEK_BALANCE_URL,
    intervalMs: Number(
      process.env.CODEX_DEEPSEEK_BALANCE_INTERVAL_MS ?? DEFAULT_INTERVAL_MS,
    ),
  };

  for (let index = 0; index < rest.length; index += 1) {
    const arg = rest[index];
    switch (arg) {
      case "--json":
        options.json = true;
        break;
      case "--once":
        options.once = true;
        break;
      case "--parent-pid":
        options.parentPid = Number(rest[++index]);
        break;
      case "--tty-fd":
        options.ttyFd = Number(rest[++index]);
        break;
      case "--project":
        options.project = rest[++index] ?? "";
        break;
      case "--endpoint":
        options.endpoint = rest[++index];
        break;
      case "--interval-ms":
        options.intervalMs = Number(rest[++index]);
        break;
      case "-h":
      case "--help":
        options.command = "help";
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }

  return options;
}

function usage() {
  return [
    "Usage:",
    "  node scripts/deepseek-balance.mjs show [--json]",
    "  node scripts/deepseek-balance.mjs title --parent-pid PID [options]",
    "",
    "The title command owns the terminal window/tab title while the wrapped",
    "Codex process is alive. It refreshes the DeepSeek USD balance at a bounded",
    "interval and never prints the API key.",
  ].join("\n");
}

async function show(options) {
  const balance = await queryDeepSeekBalance({ endpoint: options.endpoint });

  if (options.json) {
    process.stdout.write(`${JSON.stringify(balance, null, 2)}\n`);
    return;
  }

  process.stdout.write(`${formatDeepSeekBalance(balance)}\n`);
}

function titleText(balance, project) {
  const suffix = project ? ` | ${basename(project)}` : "";
  return `${formatDeepSeekBalanceTitle(balance)}${suffix}`;
}

function safeTitle(value) {
  return value.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
}

function writeTitleToFd(fd, title) {
  const clean = safeTitle(title);
  if (!clean) {
    return;
  }

  try {
    writeSync(fd, `\u001b]0;${clean}\u0007`);
  } catch {
    // A detached terminal should not keep the daemon alive.
  }
}

function parentAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) {
    return true;
  }

  try {
    process.kill(pid, 0);
  } catch {
    return false;
  }

  try {
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
    return !stat.includes(") Z ");
  } catch {
    return false;
  }
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function waitForNextPoll(intervalMs, parentPid) {
  const deadline = Date.now() + intervalMs;
  while (Date.now() < deadline) {
    if (!parentAlive(parentPid)) {
      return false;
    }
    await sleep(Math.min(1_000, deadline - Date.now()));
  }
  return parentAlive(parentPid);
}

async function titleDaemon(options) {
  const intervalMs = Number.isFinite(options.intervalMs)
    ? Math.max(MIN_INTERVAL_MS, options.intervalMs)
    : DEFAULT_INTERVAL_MS;
  let lastBalance = null;

  while (parentAlive(options.parentPid)) {
    try {
      lastBalance = await queryDeepSeekBalance({ endpoint: options.endpoint });
      writeTitleToFd(options.ttyFd, titleText(lastBalance, options.project));
    } catch (error) {
      const title = lastBalance
        ? `${titleText(lastBalance, options.project)} (stale)`
        : `DeepSeek balance unavailable${options.project ? ` | ${basename(options.project)}` : ""}`;
      writeTitleToFd(options.ttyFd, title);
    }

    if (options.once) {
      return;
    }

    if (!(await waitForNextPoll(intervalMs, options.parentPid))) {
      break;
    }
  }

  writeTitleToFd(
    options.ttyFd,
    options.project ? `Codex | ${basename(options.project)}` : DEFAULT_TITLE,
  );
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.command === "help") {
    process.stdout.write(`${usage()}\n`);
    return;
  }
  if (options.command === "show") {
    await show(options);
    return;
  }
  if (options.command === "title") {
    await titleDaemon(options);
    return;
  }

  throw new Error(`unknown command: ${options.command}`);
}

try {
  await main();
} catch (error) {
  const message =
    error instanceof DeepSeekBalanceError
      ? `${error.code}: ${error.message}`
      : error instanceof Error
        ? error.message
        : "DeepSeek balance command failed";
  process.stderr.write(`deepseek-balance: ${message}\n`);
  process.exitCode = 1;
}
