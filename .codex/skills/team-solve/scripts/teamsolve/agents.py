"""The agent turns of `--mode agents`: direct, solve and repair, each one fresh `codex exec`.

Codex is invoked the way .codex/hooks/verify.py does (same binary resolution, CODEX_HOME,
`-a never exec --skip-git-repo-check`, stdin closed), plus `--disable multi_agent` because
every prompt forbids spawning, the team-solve skill hidden (HIDE_SELF), and
VERIFIER_ACTIVE=1 so the end-result verifier hook, when enabled, does not fire inside a role.

A step is done when its target is (memdoor's target: on every agent step). A turn that
exits 0 with its target failing is resumed in the same session (`codex exec resume <id>`)
and told what is missing; when it still fails, the attempt counts as failed and the step
gets one fresh retry (memdoor max_retries: 1).
"""
import json
import os
import subprocess
import threading
import time

from . import prompts

ROLE_RETRIES = 1
NUDGES = 1
# A role must not see this skill: a planner prompt about a "ledger-based team" made a live
# role load SKILL.md (2026-11-01), one step from starting a nested team run. team_solve.py
# also refuses to run when TEAM_SOLVE_ROLE is set.
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
        raise RoleError(f"cannot resolve the Codex binary: {r.stderr.strip()}")
    return r.stdout.strip()


class RoleError(RuntimeError):
    pass


class Codex:
    def __init__(self, ws, log_dir, model, effort, log, meter_path):
        self.ws, self.dir, self.model, self.effort, self.log = ws, log_dir, model, effort, log
        self.meter_path = meter_path
        os.makedirs(self.dir, exist_ok=True)
        self.bin = codex_bin()
        self.usage = {}
        self.lock = threading.Lock()

    def _base(self, role):
        cmd = [self.bin, "-a", "never", "exec", "--skip-git-repo-check", "--cd", self.ws,
               "-m", self.model, "--disable", "multi_agent", "-c", HIDE_SELF, "--json",
               "-o", os.path.join(self.dir, f"{role}.last.md")]
        if self.effort:
            cmd += ["-c", f'model_reasoning_effort="{self.effort}"']
        return cmd

    def _env(self, role):
        env = dict(os.environ)
        env["CODEX_HOME"] = os.path.join(workspace_root(), ".codex")
        env["VERIFIER_ACTIVE"] = "1"
        env["TEAM_SOLVE_ROLE"] = role
        return env

    def _exec(self, role, cmd, tag, timeout):
        log_path = os.path.join(self.dir, f"{role}.{tag}.jsonl")
        started = time.time()
        with open(log_path, "w") as out, open(log_path[:-6] + ".stderr", "w") as err:
            try:
                rc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=self._env(role),
                                    cwd=self.ws, timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                rc = None
            except OSError as e:
                err.write(f"could not start {cmd[0]}: {e}\n")
                rc = 127
        secs = int(time.time() - started)
        thread = self._meter(role, tag, log_path, rc, secs)
        return rc, secs, thread, log_path

    def run(self, role, prompt, timeout, target=None):
        """One agent step. target() -> (ok, missing) decides whether it did its part.
        Returns True when the turn exited 0 and its target holds."""
        for attempt in range(1, ROLE_RETRIES + 2):
            self.log(f"  [{role}] codex exec (attempt {attempt})")
            rc, secs, thread, log_path = self._exec(role, self._base(role) + [prompt], str(attempt), timeout)
            nudges = 0
            while rc == 0 and target is not None:
                ok, missing = target()
                if ok:
                    break
                if nudges >= NUDGES or not thread:
                    self.log(f"  [{role}] turn ended without its target: {missing}")
                    rc = "target"
                    break
                nudges += 1
                self.log(f"  [{role}] not done ({missing}); resuming the same session")
                rc, s2, _, log_path = self._exec(role, self._base(role) + ["resume", thread, prompts.nudge(missing)],
                                                 f"{attempt}.resume{nudges}", timeout)
                secs += s2
            if rc == 0:
                self.log(f"  [{role}] done in {secs}s")
                return True
            why = {None: "timed out", "target": "target not met"}.get(rc, f"exit {rc}")
            tail = open(log_path[:-6] + ".stderr", errors="replace").read()[-400:]
            self.log(f"  [{role}] FAILED ({why} after {secs}s); stderr: {tail!r}")
        return False

    def _meter(self, role, tag, log_path, rc, secs):
        usage, thread = {}, ""
        for line in open(log_path, errors="replace"):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "thread.started" and ev.get("thread_id"):
                thread = str(ev["thread_id"])
            if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
                for k, v in ev["usage"].items():
                    if isinstance(v, int):
                        usage[k] = usage.get(k, 0) + v
        with self.lock:
            for k, v in usage.items():
                self.usage[k] = self.usage.get(k, 0) + v
            with open(self.meter_path, "a") as f:
                f.write(json.dumps({"t": time.time(), "mode": "agents", "role": role, "attempt": tag,
                                    "rc": rc, "secs": secs, "usage": usage}) + "\n")
        return thread
