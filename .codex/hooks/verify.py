#!/usr/bin/env python3
"""End-result verifier hook for the folder-caged Codex.

Codex 0.158 has no built-in end-of-task verifier; it only lets a hook reject "done" and
feed a reason back to the model (verified against rust-v0.158.0: hooks/src/events/stop.rs
and pre_tool_use.rs). This one script serves two hook points that both mean "the agent
thinks it is finished":

  * PreToolUse on `update_goal` with status == "complete"  -> the /goal completion gate.
  * Stop on a root turn that actually CHANGED something     -> ordinary task completion.

On either, it spawns a fresh, adversarial verifier agent (same model, clean context, none
of the implementer's reasoning) that derives the acceptance criteria from the user's own
words, re-runs the build/tests itself, and returns a JSON verdict. A FAIL is sent back to
the model as a continuation prompt so it keeps working; the same turn resumes. A goal is
NEVER allowed to be marked `complete` while unverified — the only escape is `blocked`,
which the model must justify to the user, so nothing passes silently.

Design points, each with a reason:
  * Fresh child, not the same context: a verifier that sees the author's reasoning rubber-
    stamps it. It gets the request + diff only.
  * Criteria come from the ORIGINAL REQUEST, not the plan, so a shrunk scope is caught.
  * Read-only is ENFORCED, not just asked for: we hash `git diff` before and after; if the
    verifier changed a tracked file its verdict is discarded (the sandbox can't be made
    read-only in this container without breaking build caches).
  * No verdict / crash / timeout => FAIL (fail-closed), never a free pass.
  * Bounded: at most MAX_ROUNDS verify rounds per task; at the cap the model is ordered to
    tell the user exactly what is unverified. A goal-complete is denied past the cap too,
    so an unverified goal can never be marked complete by exhausting the loop.

Recursion guard: the verifier is itself a `codex exec` run that reloads this same config,
so VERIFIER_ACTIVE short-circuits the hook for that nested process.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

MAX_ROUNDS = 3
VERIFIER_TIMEOUT_SEC = 780  # under the hook's own 900s timeout, leaving slack to report
ROOT = Path(__file__).resolve().parents[2]           # workspace root (.codex/hooks -> root)
CODEX_HOME = Path(os.environ.get("CODEX_HOME", ROOT / ".codex"))
STATE_DIR = Path(__file__).resolve().parent / "state"
PROMPT_FILE = Path(__file__).resolve().parent / "verifier_prompt.md"
CODEX_BIN = ROOT / "node_modules" / ".bin" / "codex"
VERDICT_OPEN = "<<<VERDICT"
VERDICT_CLOSE = "VERDICT"
# A turn "changed something" if it ran a command, edited files, or spawned an agent. A
# pure question-and-answer turn has none of these and is not worth verifying.
MUTATING_ITEM_TYPES = {"CommandExecution", "FileChange", "PatchApply", "FileUpdate"}
MUTATING_CALL_NAMES = {"apply_patch", "spawn_agent", "resume_agent", "send_input"}


def allow():
    """Let the action through. Exit 0 with no stdout is 'no opinion' for every event."""
    sys.exit(0)


def block_stop(reason: str):
    """Keep the model working after a Stop: block the stop, feed the reason back."""
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    sys.exit(0)


def deny_tool(reason: str):
    """Veto a PreToolUse tool call (here: update_goal complete) and return the reason."""
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, sys.stdout)
    sys.exit(0)


def read_payload():
    try:
        return json.load(sys.stdin)
    except Exception:
        # A hook that can't read its own payload must not wedge the session.
        allow()


# ---- round state ---------------------------------------------------------------------
# Keyed per task so one long session can finish several tasks, each with its own budget.

def state_path(key: str) -> Path:
    safe = hashlib.sha1(key.encode()).hexdigest()[:16]
    return STATE_DIR / f"{safe}.json"


def get_round(key: str) -> int:
    try:
        return json.loads(state_path(key).read_text()).get("round", 0)
    except Exception:
        return 0


def set_round(key: str, n: int):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path(key).write_text(json.dumps({"round": n, "at": time.time()}))


def clear_round(key: str):
    try:
        state_path(key).unlink()
    except FileNotFoundError:
        pass


# ---- transcript ----------------------------------------------------------------------

def read_transcript(path: str):
    rows = []
    if not path or path in ("null", "None"):
        return rows
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return rows


def _text_of(content):
    """Rollout message content is a list of {type,text} parts; join the text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    out.append(t)
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    return ""


def user_requests(rows):
    """Every message the user actually typed, verbatim, oldest first.

    role=="user" is the human; the AGENTS.md instructions ride in on role=="developer",
    so they are excluded here.
    """
    reqs = []
    for r in rows:
        p = r.get("payload", {})
        if p.get("type") == "message" and p.get("role") == "user":
            txt = _text_of(p.get("content")).strip()
            if txt:
                reqs.append(txt)
    return reqs


def last_assistant(rows):
    for r in reversed(rows):
        p = r.get("payload", {})
        if p.get("type") == "message" and p.get("role") == "assistant":
            txt = _text_of(p.get("content")).strip()
            if txt:
                return txt
    return ""


def turn_changed_something(rows, turn_id: str) -> bool:
    """Did this specific turn run a command, edit files, or spawn an agent?

    item_completed events carry turn_id and the completed item; function_call response
    items name the tool. Either signal counts. Absent a turn_id match we fall back to
    'any mutating activity present', which errs toward verifying.
    """
    saw_turn_id = False
    for r in rows:
        p = r.get("payload", {})
        ptype = p.get("type")
        if ptype == "item_completed":
            item = p.get("item", {})
            if turn_id and p.get("turn_id") == turn_id:
                saw_turn_id = True
                if item.get("type") in MUTATING_ITEM_TYPES or "command" in item:
                    return True
        elif ptype == "function_call":
            if p.get("name") in MUTATING_CALL_NAMES:
                if not turn_id or p.get("turn_id") in (None, turn_id):
                    return True
    if turn_id and saw_turn_id:
        return False  # we saw this turn and it had no mutating item
    # No usable turn_id: verify only if the transcript shows any mutating item at all.
    for r in rows:
        p = r.get("payload", {})
        if p.get("type") == "item_completed":
            item = p.get("item", {})
            if item.get("type") in MUTATING_ITEM_TYPES or "command" in item:
                return True
    return False


# ---- git fingerprint (read-only enforcement) -----------------------------------------

def git_diff_fingerprint(cwd: str) -> str:
    """Hash of tracked changes in the agent's workdir. Moves if the verifier edits code."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "-c", "core.fileMode=false", "diff", "HEAD"],
            capture_output=True, timeout=120,
        )
        return hashlib.sha256(out.stdout).hexdigest()
    except Exception:
        return ""  # not a git repo, or git failed: skip the tamper check, don't block


# ---- verifier invocation -------------------------------------------------------------

def build_prompt(requests, claim, status_line, cwd) -> str:
    role = PROMPT_FILE.read_text()
    try:
        diff = subprocess.run(
            ["git", "-C", cwd, "-c", "core.fileMode=false", "status", "--short"],
            capture_output=True, text=True, timeout=120,
        ).stdout
        patch = subprocess.run(
            ["git", "-C", cwd, "-c", "core.fileMode=false", "diff", "HEAD"],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except Exception:
        diff, patch = "(git unavailable)", ""
    if len(patch) > 200_000:
        patch = patch[:200_000] + "\n...(diff truncated; inspect files directly)..."
    req_block = "\n\n".join(f"[user message {i+1}]\n{t}" for i, t in enumerate(requests)) \
        or "(no user message found in transcript)"
    return (
        f"{role}\n\n"
        f"==== ORIGINAL REQUEST (verbatim) ====\n{req_block}\n\n"
        f"==== CLAIM (the agent's final message; unverified) ====\n{status_line}\n"
        f"{claim or '(none)'}\n\n"
        f"==== GIT STATUS ====\n{diff or '(clean)'}\n\n"
        f"==== DIFF vs HEAD ====\n{patch or '(no tracked changes)'}\n\n"
        f"Now verify. Gather your own fresh evidence, then emit the single VERDICT block."
    )


def run_verifier(prompt: str, cwd: str) -> str:
    env = dict(os.environ)
    env["VERIFIER_ACTIVE"] = "1"          # stop the nested codex from re-triggering us
    env["CODEX_HOME"] = str(CODEX_HOME)
    cmd = [
        str(CODEX_BIN), "-a", "never", "exec",
        "--skip-git-repo-check", "--cd", cwd, prompt,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=env,
            timeout=VERIFIER_TIMEOUT_SEC, cwd=cwd,
            stdin=subprocess.DEVNULL,  # exec appends stdin to the prompt; give it EOF
        )
        return proc.stdout + "\n" + proc.stderr
    except subprocess.TimeoutExpired:
        return ""  # -> parsed as no verdict -> fail-closed


def parse_verdict(text: str):
    """Return the parsed dict from the LAST verdict block, or None."""
    idx = text.rfind(VERDICT_OPEN)
    if idx == -1:
        return None
    body = text[idx + len(VERDICT_OPEN):]
    end = body.find("\n" + VERDICT_CLOSE)
    if end != -1:
        body = body[:end]
    body = body.strip()
    # Tolerate a stray closing marker or trailing prose.
    if body.endswith(VERDICT_CLOSE):
        body = body[: -len(VERDICT_CLOSE)].strip()
    try:
        v = json.loads(body)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


def format_failure(verdict, round_no, capped):
    if verdict is None:
        head = ("The end-result verifier produced no parseable verdict, so completion is "
                "NOT confirmed (treated as failure).")
        detail = ""
    else:
        crit = verdict.get("criteria", [])
        lines = [f"  - [{c.get('verdict', '?').upper()}] {c.get('criterion', '')}"
                 f" — {c.get('evidence', '')}" for c in crit]
        unver = verdict.get("unverified") or [
            c.get("criterion", "") for c in crit if c.get("verdict") != "pass"]
        fix = verdict.get("fix_instructions", "")
        head = "The end-result verifier did NOT confirm the work is complete."
        detail = ("\n\nCriteria:\n" + "\n".join(lines) if lines else "") + \
                  ("\n\nUnverified: " + "; ".join(u for u in unver if u) if unver else "") + \
                  ("\n\nFix: " + fix if fix else "")
    if capped:
        tail = (f"\n\nThis was verification round {round_no} of {MAX_ROUNDS} (the limit). "
                "Do NOT claim success. Tell the user plainly which criteria remain "
                "unverified and why, then stop. If this is a goal, mark it `blocked` "
                "(not `complete`) and explain — an unverified goal will not be accepted "
                "as complete.")
    else:
        tail = (f"\n\nThis is verification round {round_no} of {MAX_ROUNDS}. Fix the above "
                "and finish the task, then you'll be re-checked. Send fixes to the SAME "
                "worker agents where you can (resume_agent), rather than re-briefing new "
                "ones.")
    return head + detail + tail


# ---- entry points --------------------------------------------------------------------

def verify_and_decide(rows, turn_id, claim, status_line, task_key, on_fail_block, cwd):
    """Run the verifier, update the round counter, and block/allow via on_fail_block."""
    before = git_diff_fingerprint(cwd)
    requests = user_requests(rows)
    output = run_verifier(build_prompt(requests, claim, status_line, cwd), cwd)
    after = git_diff_fingerprint(cwd)

    if before != after and before and after:
        # The verifier edited tracked code. Its verdict is void; send the work back.
        n = get_round(task_key) + 1
        set_round(task_key, n)
        on_fail_block(
            "The verifier modified tracked files, which it must never do; its verdict is "
            "discarded and nothing here is confirmed. Re-examine the work yourself and "
            "ensure it truly meets every requirement before finishing."
            + (f" (round {n}/{MAX_ROUNDS})"))
        return

    verdict = parse_verdict(output)
    if verdict is not None and verdict.get("result") == "pass":
        clear_round(task_key)
        allow()
        return

    n = get_round(task_key) + 1
    set_round(task_key, n)
    capped = n >= MAX_ROUNDS
    on_fail_block(format_failure(verdict, n, capped))


def handle_pre_tool_use(payload):
    if payload.get("tool_name") != "update_goal":
        allow()
    status = (payload.get("tool_input") or {}).get("status")
    if status != "complete":
        allow()  # blocked / paused are the model's honest escapes; never gated
    rows = read_transcript(payload.get("transcript_path"))
    # Goal id keys the budget so each goal gets its own rounds.
    task_key = f"goal:{_goal_id_for(payload)}"
    verify_and_decide(
        rows,
        payload.get("turn_id"),
        last_assistant(rows),
        "(agent is calling update_goal status=complete)",
        task_key,
        deny_tool,
        payload.get("cwd") or str(ROOT),
    )


def _goal_id_for(payload):
    # thread id is the session id (rollout filename); one active goal per thread.
    tp = payload.get("transcript_path") or ""
    return Path(tp).stem or payload.get("session_id") or "goal"


def handle_stop(payload):
    if payload.get("stop_hook_active"):
        # We are already inside a block loop that has run past the disclosure round;
        # let the turn end so the CLI never hangs.
        allow()
    rows = read_transcript(payload.get("transcript_path"))
    # If THIS session has an active goal, the update_goal gate owns verification; don't
    # double up. Scoped to this thread so another session's goal never suppresses us.
    if _goal_active(payload.get("session_id")):
        allow()
    turn_id = payload.get("turn_id")
    if not turn_changed_something(rows, turn_id):
        allow()  # pure Q&A / no-op turn: nothing to verify
    claim = payload.get("last_assistant_message")
    if claim in (None, "null"):
        claim = last_assistant(rows)
    task_key = f"stop:{payload.get('session_id')}:{turn_id}"
    verify_and_decide(rows, turn_id, claim, "(agent ended its turn)", task_key,
                      block_stop, payload.get("cwd") or str(ROOT))


def _goal_active(session_id) -> bool:
    """True only if THIS thread has an active goal. thread_id == session id."""
    if not session_id:
        return False
    import sqlite3
    db = CODEX_HOME / "goals_1.sqlite"
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        row = con.execute(
            "select 1 from thread_goals where thread_id=? and status='active' limit 1",
            (session_id,)).fetchone()
        con.close()
        return row is not None
    except Exception:
        return False


def main():
    if os.environ.get("VERIFIER_ACTIVE") == "1":
        allow()  # this process IS the verifier's codex run; do not recurse
    payload = read_payload()
    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        handle_pre_tool_use(payload)
    elif event == "Stop":
        handle_stop(payload)
    else:
        allow()


if __name__ == "__main__":
    main()
