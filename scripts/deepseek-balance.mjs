#!/usr/bin/env node

import { basename, dirname } from "node:path";
import {
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
  writeSync,
} from "node:fs";

import {
  DeepSeekBalanceError,
  formatDeepSeekBalance,
  formatDeepSeekBalanceTitle,
  queryDeepSeekBalance,
} from "../plugins/deepseek-balance/lib/deepseek-balance.mjs";

const DEFAULT_INTERVAL_MS = 60_000;
const MIN_INTERVAL_MS = 15_000;
const DEFAULT_TITLE = "Codex";
const DEFAULT_CACHE_MAX_AGE_MS = 5 * 60_000;
const CACHE_VERSION = 1;

function parseArgs(argv) {
  const [command = "show", ...rest] = argv;
  const options = {
    command,
    json: false,
    once: false,
    noTitle: false,
    parentPid: null,
    ttyFd: 1,
    project: "",
    cacheFile: process.env.DEEPSEEK_BALANCE_CACHE_FILE,
    endpoint: process.env.DEEPSEEK_BALANCE_URL,
    intervalMs: Number(
      process.env.CODEX_DEEPSEEK_BALANCE_INTERVAL_MS ?? DEFAULT_INTERVAL_MS,
    ),
    cacheMaxAgeMs: Number(
      process.env.DEEPSEEK_BALANCE_CACHE_MAX_AGE_MS ??
        DEFAULT_CACHE_MAX_AGE_MS,
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
      case "--no-title":
        options.noTitle = true;
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
      case "--cache-file":
        options.cacheFile = rest[++index];
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
    "  node scripts/deepseek-balance.mjs status [--cache-file PATH] [--endpoint URL]",
    "  node scripts/deepseek-balance.mjs title --parent-pid PID [--no-title] [--cache-file PATH] [options]",
    "",
    "The status command emits one footer-safe line. The title command owns the",
    "terminal window/tab title while the wrapped Codex process is alive. Both",
    "never print the API key.",
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

async function status(options) {
  if (options.cacheFile) {
    const cached = readBalanceCache(options.cacheFile, options.cacheMaxAgeMs);
    const line = formatDeepSeekBalanceTitle(cached.balance);
    process.stdout.write(`${line}${cached.stale ? " (stale)" : ""}\n`);
    return;
  }

  const balance = await queryDeepSeekBalance({ endpoint: options.endpoint });
  process.stdout.write(`${formatDeepSeekBalanceTitle(balance)}\n`);
}

function readBalanceCache(cacheFile, maxAgeMs = DEFAULT_CACHE_MAX_AGE_MS) {
  let payload;
  try {
    payload = JSON.parse(readFileSync(cacheFile, "utf8"));
  } catch {
    throw new Error("DeepSeek balance cache is missing or invalid.");
  }

  if (
    payload === null ||
    typeof payload !== "object" ||
    payload.version !== CACHE_VERSION ||
    payload.stale !== false && payload.stale !== true
  ) {
    throw new Error("DeepSeek balance cache is missing or invalid.");
  }

  try {
    formatDeepSeekBalanceTitle(payload.balance);
  } catch {
    throw new Error("DeepSeek balance cache is missing or invalid.");
  }

  const fetchedAt = Date.parse(payload.fetched_at);
  const ageMs = Date.now() - fetchedAt;
  const tooOld =
    !Number.isFinite(maxAgeMs) ||
    maxAgeMs <= 0 ||
    !Number.isFinite(ageMs) ||
    ageMs < -maxAgeMs ||
    ageMs > maxAgeMs;

  return {
    balance: payload.balance,
    stale: payload.stale || tooOld,
  };
}

function writeBalanceCache(cacheFile, balance, stale) {
  const directory = dirname(cacheFile);
  mkdirSync(directory, { recursive: true });

  const tempFile = `${cacheFile}.${process.pid}.${Date.now()}.tmp`;
  const payload = {
    version: CACHE_VERSION,
    fetched_at: new Date().toISOString(),
    stale,
    balance,
  };
  writeFileSync(tempFile, `${JSON.stringify(payload)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  renameSync(tempFile, cacheFile);
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
  if (options.cacheFile) {
    try {
      lastBalance = readBalanceCache(
        options.cacheFile,
        options.cacheMaxAgeMs,
      ).balance;
    } catch {
      // A missing cache is expected on the first run.
    }
  }

  while (parentAlive(options.parentPid)) {
    try {
      lastBalance = await queryDeepSeekBalance({ endpoint: options.endpoint });
      if (options.cacheFile) {
        writeBalanceCache(options.cacheFile, lastBalance, /*stale*/ false);
      }
      if (!options.noTitle) {
        writeTitleToFd(options.ttyFd, titleText(lastBalance, options.project));
      }
    } catch (error) {
      if (options.cacheFile && lastBalance) {
        writeBalanceCache(options.cacheFile, lastBalance, /*stale*/ true);
      }
      const title = lastBalance
        ? `${titleText(lastBalance, options.project)} (stale)`
        : `DeepSeek balance unavailable${options.project ? ` | ${basename(options.project)}` : ""}`;
      if (!options.noTitle) {
        writeTitleToFd(options.ttyFd, title);
      }
    }

    if (options.once) {
      return;
    }

    if (!(await waitForNextPoll(intervalMs, options.parentPid))) {
      break;
    }
  }

  if (!options.noTitle) {
    writeTitleToFd(
      options.ttyFd,
      options.project ? `Codex | ${basename(options.project)}` : DEFAULT_TITLE,
    );
  }
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
  if (options.command === "status") {
    await status(options);
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
