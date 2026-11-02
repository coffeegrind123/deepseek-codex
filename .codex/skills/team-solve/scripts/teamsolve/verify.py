"""Ground truth: the thing that decides "solved" each round and at the end -- a check, not
a model's say-so. That check is what makes the team loop beat a single call, so it stays
the engine whatever the task.

Two verifiers, one interface:

* SampleVerifier -- the authors' public-sample check. The loop's byte-for-byte parity with
  their code depends on the default path running samples exactly as they do, so the loop
  keeps calling samples.py directly on this path; this class only carries the final strict
  verdict (team_solve's `check`), which was already the sample checker.
* CommandVerifier -- a user-supplied command run in the workspace. Exit 0 is solved; its
  output is the feedback the manager reads. That turns the same loop into a solver for any
  task whose success you can check with a command: make a test suite pass, make a build
  compile, satisfy a grader, hit a benchmark, reproduce an output. Contest samples are then
  just the built-in default.

`round(ws)` returns a result shaped like samples.run_samples (ran/passed/total/fail) plus
`ok` and the manager `text`, so the loop's existing done-override arithmetic
(`passed < total`) works unchanged. `verdict(ws, out)` returns the failure count for the
strict final check (0 == solved).
"""
import subprocess

FEEDBACK_TAIL = 4000
TIMEOUT_EXIT = 124


class SampleVerifier:
    kind = "samples"

    def __init__(self, python, include_functional):
        self.python = python
        self.include_functional = include_functional

    def verdict(self, ws, out):
        from . import samples
        return samples.check(ws, self.python, out)


class CommandVerifier:
    kind = "command"

    def __init__(self, cmd, timeout=1800):
        self.cmd = cmd
        self.timeout = timeout

    def _run(self, ws):
        try:
            r = subprocess.run(["bash", "-lc", self.cmd], cwd=ws, capture_output=True,
                               text=True, timeout=self.timeout)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired as e:
            partial = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
            return TIMEOUT_EXIT, f"{partial}\n<verification command timed out after {self.timeout}s>\n"
        except OSError as e:
            return 127, f"<verification command could not run: {e}>\n"

    def round(self, ws):
        rc, out = self._run(ws)
        ok = rc == 0
        tail = out.strip()[-FEEDBACK_TAIL:]
        if ok:
            text = "[CHECK: PASSED -- the verification command exited 0, so the task is done.] "
        else:
            text = (f"[CHECK: FAILED -- the verification command `{self.cmd}` exited {rc}. The current "
                    f"work is WRONG or incomplete. Fix it, or if this approach keeps failing switch to a "
                    f"different one. Command output:\n{tail}\n] ")
        return {"ran": True, "passed": 1 if ok else 0, "total": 1, "ok": ok,
                "fail": None if ok else {"exit": rc, "output": tail}, "text": text}

    def verdict(self, ws, out):
        rc, output = self._run(ws)
        out.write(output)
        if not output.endswith("\n"):
            out.write("\n")
        out.write(f"Verification command `{self.cmd}` exited {rc}\n")
        return 0 if rc == 0 else 1
