"""`--mode agents`: memdoor's eight-step workflow, each role one fresh `codex exec` agent.

    plan --+-- ideate-a --+-- solve -- test -- review -- repair -- verify
           +-- ideate-b --+

Unlike loop mode, every role here is a full Codex agent with tools (it reads and patches
the ledger files itself, runs the program), so a role costs a whole agent turn: memdoor
measured this shape at roughly ten times the one-call-per-role cost. test and verify are
not agents: they run the strict sample checker, and verify's exit status is the verdict.

Codex is invoked the way .codex/hooks/verify.py does (same binary resolution, CODEX_HOME,
`-a never exec --skip-git-repo-check`, stdin closed), plus `--disable multi_agent` because
every prompt forbids spawning, the team-solve skill hidden (HIDE_SELF), and VERIFIER_ACTIVE=1 so the end-result verifier hook,
when enabled, does not fire inside a role.
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid

from . import ledger, prompts, samples, verify

ROLE_TIMEOUT_SEC = 600   # memdoor: timeout: 10m
ROLE_RETRIES = 1         # memdoor: max_retries: 1 (a dropped stream killed its first run)
ROLES = ("plan", "ideate-a", "ideate-b", "solve", "review", "repair")
# A role must not see this skill: the planner's "ledger-based team" prompt made a live role
# load SKILL.md (2026-11-01), one step from starting a nested team run. team_solve.py also
# refuses to run when TEAM_SOLVE_ROLE is set.
HIDE_SELF = 'skills.config=[{name="team-solve",enabled=false}]'



def workspace_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", "..", "..", ".."))


def codex_bin():
    explicit = os.environ.get("CODEX_WORKSPACE_BIN")
    if explicit:
        return explicit
    resolver = os.path.join(workspace_root(), "scripts", "resolve-codex-bin.sh")
    r = subprocess.run([resolver], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise SystemExit(f"team-solve: cannot resolve the Codex binary: {r.stderr.strip()}")
    return r.stdout.strip()


def _context(ws, task_file, checker, verify_cmd=None):
    lines = ["", "Workspace facts (from the harness, not from another agent):"]
    if verify_cmd is None:
        lines += [
            f"- The problem statement is {task_file}. Public samples are tests/N.in with expected tests/N.out.",
            f"- The strict sample checker is: python3 {checker} check --dir {ws}",
        ]
        fname = samples.func_name(ws)
        if fname:
            lines.append(
                f"- This is a starter-code problem: solution.py must define the starter class with method "
                f"`{fname}`. The checker imports solution.py and calls it with one JSON argument per line of "
                f"tests/N.in, comparing the JSON result to tests/N.out; solution.py must not read stdin.")
    else:
        lines += [
            f"- The task is described in {task_file}. You may read and edit any file in this workspace.",
            f"- Success is decided by this command exiting zero (run it yourself to check):\n    {verify_cmd}",
            "- Do not edit the task file or change/weaken the verification command.",
        ]
    return "\n".join(lines)


class AgentRun:
    def __init__(self, ws, task_file, model, effort, log, fresh, role_timeout=ROLE_TIMEOUT_SEC,
                 verify_cmd=None, verifier=None):
        self.ws, self.task_file, self.model, self.effort, self.log = ws, task_file, model, effort, log
        self.verify_cmd = verify_cmd                 # None -> sample checker (contest default)
        self.verifier = verifier or verify.SampleVerifier(sys.executable, False)
        self.prompts = prompts.AGENT_PROMPTS_GENERIC if verify_cmd else prompts.AGENT_PROMPTS
        self.role_timeout = role_timeout
        self.dir = ledger.state_path(ws, "agents")
        os.makedirs(self.dir, exist_ok=True)
        rid_path = os.path.join(self.dir, "run-id")
        if fresh or not os.path.exists(rid_path):
            stale = [f for f in os.listdir(self.dir) if f != "run-id"]
            if stale:
                dest = ledger.state_path(ws, "stale", "agents-" + time.strftime("%Y%m%d-%H%M%S"))
                os.makedirs(dest, exist_ok=True)
                for f in stale:
                    os.replace(os.path.join(self.dir, f), os.path.join(dest, f))
            ledger.write(ws, os.path.relpath(rid_path, ws), uuid.uuid4().hex[:12])
        self.run_id = ledger.read(ws, os.path.relpath(rid_path, ws)).strip()
        self.bin = codex_bin()
        self.checker = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "team_solve.py"))
        self.usage = {}
        self.lock = threading.Lock()

    def _done(self, role):
        return os.path.join(self.dir, f"{role}.done")

    def role(self, role):
        if os.path.exists(self._done(role)):
            self.log(f"  [{role}] already done in run {self.run_id}; skipped (use --fresh to redo)")
            return True
        prompt = self.prompts[role].format(task=self.task_file, run_id=self.run_id,
                                           verify=self.verify_cmd or "") \
            + _context(self.ws, self.task_file, self.checker, self.verify_cmd)
        env = dict(os.environ)
        env["CODEX_HOME"] = os.path.join(workspace_root(), ".codex")
        env["VERIFIER_ACTIVE"] = "1"
        env["TEAM_SOLVE_ROLE"] = role
        cmd = [self.bin, "-a", "never", "exec", "--skip-git-repo-check", "--cd", self.ws,
               "-m", self.model, "--disable", "multi_agent", "-c", HIDE_SELF, "--json",
               "-o", os.path.join(self.dir, f"{role}.last.md")]
        if self.effort:
            cmd += ["-c", f'model_reasoning_effort="{self.effort}"']
        cmd.append(prompt)
        for attempt in range(1, ROLE_RETRIES + 2):
            started = time.time()
            self.log(f"  [{role}] codex exec (attempt {attempt})")
            log_path = os.path.join(self.dir, f"{role}.{attempt}.jsonl")
            with open(log_path, "w") as out, open(log_path[:-6] + ".stderr", "w") as err:
                try:
                    rc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env,
                                        cwd=self.ws, timeout=self.role_timeout).returncode
                except subprocess.TimeoutExpired:
                    rc = None
            secs = int(time.time() - started)
            self._meter(role, attempt, log_path, rc, secs)
            if rc == 0:
                ledger.write(self.ws, os.path.relpath(self._done(role), self.ws), f"{time.time()}\n")
                self.log(f"  [{role}] done in {secs}s")
                return True
            why = "timed out" if rc is None else f"exit {rc}"
            self.log(f"  [{role}] FAILED ({why} after {secs}s); stderr: "
                     f"{ledger.read(self.ws, os.path.relpath(log_path[:-6] + '.stderr', self.ws))[-400:]!r}")
        return False

    def _meter(self, role, attempt, log_path, rc, secs):
        usage = {}
        for line in open(log_path, errors="replace"):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
                for k, v in ev["usage"].items():
                    if isinstance(v, int):
                        usage[k] = usage.get(k, 0) + v
        with self.lock:
            for k, v in usage.items():
                self.usage[k] = self.usage.get(k, 0) + v
            with open(ledger.state_path(self.ws, "meter.jsonl"), "a") as f:
                f.write(json.dumps({"t": time.time(), "mode": "agents", "role": role, "attempt": attempt,
                                    "rc": rc, "secs": secs, "usage": usage}) + "\n")

    def parallel(self, *roles):
        results = {}

        def go(r):
            results[r] = self.role(r)
        threads = [threading.Thread(target=go, args=(r,)) for r in roles]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return all(results.get(r) for r in roles)

    def check_to(self, log_name):
        with open(os.path.join(self.ws, log_name), "w") as f:
            failures = self.verifier.verdict(self.ws, f)
        sys.stdout.write(ledger.read(self.ws, log_name))
        return failures

    def run(self):
        steps = []
        if not self.role("plan"):
            return {"failed_step": "plan", "steps": steps}
        steps.append("plan")
        if not self.parallel("ideate-a", "ideate-b"):
            return {"failed_step": "ideate", "steps": steps}
        steps.append("ideate")
        if not self.role("solve"):
            return {"failed_step": "solve", "steps": steps}
        steps.append("solve")
        status = 1 if self.check_to("test-output.log") else 0
        ledger.write(self.ws, "test-status.txt", f"{status}\n")
        self.log(f"  [test] initial sample-test exit status: {status}")
        steps.append("test")
        if not self.role("review"):
            return {"failed_step": "review", "steps": steps, "initial_status": status}
        steps.append("review")
        if not self.role("repair"):
            return {"failed_step": "repair", "steps": steps, "initial_status": status}
        steps.append("repair")
        failures = self.check_to("final-test-output.log")
        steps.append("verify")
        return {"failed_step": None if failures == 0 else "verify", "steps": steps,
                "initial_status": status, "final_failures": failures, "run_id": self.run_id,
                "usage": self.usage}
