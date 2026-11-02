"""Public-sample execution.

Two consumers with deliberately different semantics:

* `run_samples` is the authors' `_run_samples` (multiagent.py): whitespace-stripped
  comparison, 10 s per sample, first failure captured for the manager's feedback. The
  loop's ground truth, so it must not drift from theirs.
* `check` is memdoor's check-samples.sh: byte-exact `diff -u` per sample, PASS/FAIL lines,
  nonzero exit on any failure. The strict final verdict (and the agent mode's test/verify
  steps).

Sample layout (memdoor's): tests/N.in + tests/N.out. A tests/func_name file marks a
LeetCode-style problem: each line of N.in is one JSON argument, N.out is the JSON result,
and solution.py defines `class Solution` with that method (LiveCodeBench's convention).
The authors' loop never ran those (stdin tests only); `--samples all` opts in.
"""
import difflib
import json
import os
import subprocess
import sys
from dataclasses import dataclass

SAMPLE_TIMEOUT_SEC = 10
FUNC_NAME_FILE = "func_name"

# Runs inside a child process: python -c HARNESS <solution.py> <method>; stdin = N.in.
FUNCTIONAL_HARNESS = r"""
import importlib.util, json, sys
path, name = sys.argv[1], sys.argv[2]
args = [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]
spec = importlib.util.spec_from_file_location("solution", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
holder = getattr(mod, "Solution", None)
if holder is not None:
    fn = getattr(holder(), name)
else:
    fn = getattr(mod, name)
def norm(v):
    if isinstance(v, tuple):
        return [norm(x) for x in v]
    if isinstance(v, list):
        return [norm(x) for x in v]
    return v
print(json.dumps(norm(fn(*args))))
"""


@dataclass
class Sample:
    name: str
    input: str
    output: str
    in_path: str
    out_path: str


def tests_dir(ws):
    return os.path.join(ws, "tests")


def func_name(ws):
    p = os.path.join(tests_dir(ws), FUNC_NAME_FILE)
    if not os.path.exists(p):
        return None
    return open(p).read().strip() or None


def load(ws):
    """Every tests/N.in with its N.out, ordered 1, 2, ..., 10 (memdoor's order)."""
    d = tests_dir(ws)
    if not os.path.isdir(d):
        return []
    ids = sorted((f[:-3] for f in os.listdir(d) if f.endswith(".in")), key=lambda s: (len(s), s))
    out = []
    for i in ids:
        ip, op = os.path.join(d, f"{i}.in"), os.path.join(d, f"{i}.out")
        out.append(Sample(i, open(ip).read(), open(op).read() if os.path.exists(op) else None, ip, op))
    return out


def _exec(python, sol, inp, fname, timeout):
    """Run one sample. Returns (stdout, stderr, returncode) or raises TimeoutExpired."""
    if fname:
        cmd = [python, "-c", FUNCTIONAL_HARNESS, sol, fname]
    else:
        cmd = [python, sol]
    r = subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=timeout,
                       cwd=os.path.dirname(sol))
    return r.stdout or "", r.stderr or "", r.returncode


def _json_equal(expected, got):
    try:
        return json.loads(expected) == json.loads(got)
    except (ValueError, TypeError):
        return expected.strip() == got.strip()


def run_samples(ws, python=sys.executable, include_functional=False, solution="solution.py"):
    """The authors' _run_samples, plus functional samples when asked."""
    sol = os.path.join(ws, solution)
    code = open(sol).read() if os.path.exists(sol) else ""
    fname = func_name(ws)
    if fname and not include_functional:
        return {"ran": False}
    tests = load(ws)
    if not code.strip() or not tests:
        return {"ran": False}
    passed, fail = 0, None
    for t in tests:
        inp = t.input or ""
        exp = (t.output or "").strip()
        try:
            out, err, rc = _exec(python, sol, inp, fname, SAMPLE_TIMEOUT_SEC)
            got = out.strip()
            if rc != 0 and not got:
                got = f"<runtime error: {err[:200]}>"
        except subprocess.TimeoutExpired:
            got = "<timed out (>10s)>"
        except Exception as e:  # noqa: BLE001 - the authors' catch-all, reported to the manager
            got = f"<error: {e}>"
        ok = _json_equal(exp, got) if fname else got == exp
        if ok:
            passed += 1
        elif fail is None:
            fail = {"input": inp[:600], "expected": exp[:400], "got": got[:400]}
    return {"ran": True, "passed": passed, "total": len(tests), "fail": fail}


def feedback(res):
    """The authors' _sample_feedback."""
    if not res or not res.get("ran"):
        return ""
    if res["passed"] == res["total"]:
        return f"[SAMPLE TESTS: PASSED all {res['total']} public samples -- the solution looks correct.] "
    f = res.get("fail") or {}
    return ("[SAMPLE TESTS: FAILED -- passed {p}/{t}. The current solution is WRONG. First failing "
            "case: input={i!r} expected={e!r} got={g!r}. Fix the bug or, if this approach keeps "
            "failing, switch to a DIFFERENT approach.] ").format(
                p=res["passed"], t=res["total"], i=f.get("input", ""),
                e=f.get("expected", ""), g=f.get("got", ""))


def check(ws, python=sys.executable, out=sys.stdout, solution="solution.py"):
    """check-samples.sh: exact comparison, a report on `out`, returns the failure count.

    No samples at all is a failure, as in the shell original: a run that cannot be
    checked must not pass.
    """
    tests = load(ws)
    if not tests:
        print("FAIL: no sample inputs found", file=out)
        return 1
    sol = os.path.join(ws, solution)
    fname = func_name(ws)
    failures = 0
    for t in tests:
        rel = os.path.relpath(t.in_path, ws)
        if t.output is None:
            print(f"FAIL {rel}: missing {os.path.relpath(t.out_path, ws)}", file=out)
            failures += 1
            continue
        if not os.path.exists(sol):
            print(f"FAIL {rel}: {solution} does not exist", file=out)
            failures += 1
            continue
        try:
            got, err, rc = _exec(python, sol, t.input, fname, SAMPLE_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            print(f"FAIL {rel}: timed out (>{SAMPLE_TIMEOUT_SEC}s)", file=out)
            failures += 1
            continue
        if rc != 0:
            if err:
                print(err.rstrip()[-2000:], file=out)
            print(f"FAIL {rel}: solution exited unsuccessfully (exit {rc})", file=out)
            failures += 1
            continue
        same = _json_equal(t.output, got) if fname else got == t.output
        if same:
            print(f"PASS {rel}", file=out)
            continue
        diff = difflib.unified_diff(t.output.splitlines(True), got.splitlines(True),
                                    fromfile=os.path.relpath(t.out_path, ws), tofile="actual")
        out.write("".join(diff))
        if not got.endswith("\n") or not t.output.endswith("\n"):
            out.write("\n")
        print(f"FAIL {rel}: output differs", file=out)
        failures += 1
    print(f"Samples: {len(tests)}; failures: {failures}", file=out)
    return failures
