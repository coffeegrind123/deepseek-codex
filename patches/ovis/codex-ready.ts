/**
 * codex-workspace: hold a chat message for a Codex pane until its session thread is up.
 *
 * In this workspace a Codex session takes about 80 s to start. Anything typed into its TUI
 * before the session thread exists loses the session: Codex prints "No active thread is
 * available" and drops every later message, in a herdr pane and a plain terminal alike
 * (measured 2026-11-01 against Codex 0.166.0). The one signal that the thread is up is the
 * app server's own trace when it announces it, which Codex writes to the log database in its
 * CODEX_HOME: target `codex_app_server::outgoing_message`, body naming
 * `app_server.thread_start.notify_started`, process_uuid `pid:<pid>:<uuid>`.
 *
 * So a send to a Codex pane first waits for that row for the pane's Codex process, at most
 * CODEX_START_WAIT_MS, then is typed as usual; past that it is refused with nothing typed.
 * Whatever cannot be read (no process, no CODEX_HOME, no log database, or one without any
 * row for that process) is not a reason to hold anything: the send goes on as Ovis shipped it.
 *
 * Installed by scripts/ovis_patch.py as server/codex-ready.ts.
 */
import { Database } from "bun:sqlite";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { paneProcessRoots } from "./conversation.ts";
import { HerdrError } from "./herdr/client.ts";

export const CODEX_START_WAIT_MS = 180_000;
const POLL_MS = 500;
const READY_TARGET = "codex_app_server::outgoing_message";
const READY_MARK = "%app_server.thread_start.notify_started%";
// Codex's own partial index for a process's rows without a thread (logs_*.sqlite); the plain
// planner picks the thread_id index and scans for seconds on a big log.
const INDEX = "idx_logs_process_uuid_threadless_ts";

export type CodexReadiness = "ready" | "starting" | "unknown";

/** The newest logs_<n>.sqlite in a Codex home, or null. */
export function codexLogDatabase(codexHome: string): string | null {
  let names: string[];
  try { names = readdirSync(codexHome); } catch { return null; }
  const found = names
    .map((name) => /^logs_(\d+)\.sqlite$/.exec(name))
    .filter((match): match is RegExpExecArray => match !== null)
    .sort((a, b) => Number(b[1]) - Number(a[1]));
  return found.length ? join(codexHome, found[0]![0]) : null;
}

/** What the log database says about one Codex process's session thread. */
export function codexReadiness(database: string, pid: number): CodexReadiness {
  let db: Database | null = null;
  try {
    db = new Database(database, { readonly: true });
    const low = `pid:${pid}:`;
    const high = `pid:${pid};`;
    const row = (extra: string, ...args: string[]): unknown => db!.query(
      `SELECT 1 FROM logs INDEXED BY ${INDEX} WHERE thread_id IS NULL AND process_uuid >= ? AND process_uuid < ?${extra} LIMIT 1`,
    ).get(low, high, ...args);
    if (row(" AND target = ? AND feedback_log_body LIKE ?", READY_TARGET, READY_MARK)) return "ready";
    // rows for the process, but not that one: it is still starting. None at all: its logging
    // is not reaching this database, which says nothing either way.
    return row("") ? "starting" : "unknown";
  } catch {
    return "unknown";
  } finally {
    db?.close();
  }
}

/** The CODEX_HOME a process runs with: its own variable, else ~/.codex of its HOME. */
function codexHomeOf(environ: readonly string[] | null): string | null {
  if (!environ) return null;
  const value = (key: string): string | undefined => environ.find((entry) => entry.startsWith(`${key}=`))?.slice(key.length + 1);
  const home = value("CODEX_HOME") || (value("HOME") ? join(value("HOME")!, ".codex") : "");
  try { return home && statSync(home).isDirectory() ? home : null; } catch { return null; }
}

// Processes seen ready, by pid and start time, so a pane is read once, not on every send.
const ready = new Set<string>();

/** Field 22 of /proc/<pid>/stat (start time in clock ticks): a reused pid is another process. */
function startTime(pid: number): string | null {
  try {
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
    // the command name (field 2) may hold spaces and parentheses; fields after it start at 3
    return stat.slice(stat.lastIndexOf(") ") + 2).split(" ")[19] ?? null;
  } catch {
    return null;
  }
}

/**
 * Waits until the Codex in `paneId` has its session thread, and returns how long it waited (ms).
 * `stillAllowed` is called on every poll and throws when the sender lost the right to type.
 */
export async function waitForCodexReady(paneId: string, stillAllowed: () => void, waitMs = CODEX_START_WAIT_MS): Promise<number> {
  const roots = await paneProcessRoots(paneId, "codex").catch(() => null);
  // herdr could not be asked: nothing is known, so nothing is held
  if (!roots) return 0;
  const pid = roots.pid;
  // herdr answered and no Codex runs in front: typing would land in the shell under it
  if (!pid) throw new HerdrError("agent_not_ready", "Codex is not running in this pane; nothing was typed.");
  const key = `${pid}:${startTime(pid) ?? ""}`;
  if (ready.has(key)) return 0;
  const home = codexHomeOf(roots?.environ ?? null);
  const database = home ? codexLogDatabase(home) : null;
  if (!database) return 0;
  const started = Date.now();
  for (;;) {
    const state = codexReadiness(database, pid);
    if (state === "ready") {
      ready.add(key);
      return Date.now() - started;
    }
    if (state === "unknown") return Date.now() - started;
    if (Date.now() - started >= waitMs) {
      throw new HerdrError("codex_starting",
        "Codex in this pane is still starting (its session thread is not up yet), and a message typed now would lose the session; nothing was typed. Send it again in a moment.");
    }
    stillAllowed();
    await Bun.sleep(POLL_MS);
  }
}
