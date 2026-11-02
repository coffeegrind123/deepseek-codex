"""`--stress`: an independent brute force checks every sample-passing program.

Why: memdoor's 5-pass run lost abc399_e because the manager invented a counterexample with
a wrong expected answer and the team replaced a correct program; the samples did not cover
the case (examples/workflows/team-solve/NOTES.md, "Where it lost"). Samples are the
authors' only ground truth. This adds a second one that scales: a brute force and a tiny-
input generator written once, by a fresh call that never sees any solution, validated on
the public samples before it is trusted.

The verdict reaches the manager the same way the sample verdict does (a bracketed line at
the front of LATEST WORKER RESULT), and a failed stress test overrides the manager's
'done' exactly as a failed sample does. Seeds are fixed per round, so replay is exact.
Only stdin problems: a functional (LeetCode) brute force would need the harness's
argument conventions, which a generator cannot be validated against.
"""
import os
import subprocess

from . import ledger, parse, prompts, samples

BRUTE_FILE = "brute.py"
GEN_FILE = "gen.py"
CASE_TIMEOUT_SEC = 10
GEN_TIMEOUT_SEC = 5
VALIDATION_SEEDS = 5


def _run(python, path, inp, args=(), timeout=CASE_TIMEOUT_SEC):
    try:
        r = subprocess.run([python, path, *args], input=inp, capture_output=True, text=True,
                           timeout=timeout, cwd=os.path.dirname(path))
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if r.returncode != 0:
        return None, (r.stderr or "")[-300:] or f"exit {r.returncode}"
    return r.stdout, ""


def _validate(ws, python):
    """None when brute.py and gen.py are usable, else the reason they are not."""
    brute, gen = os.path.join(ws, BRUTE_FILE), os.path.join(ws, GEN_FILE)
    for seed in range(1, VALIDATION_SEEDS + 1):
        out, err = _run(python, gen, "", (str(seed),), GEN_TIMEOUT_SEC)
        if out is None or not out.strip():
            return f"GEN failed for seed {seed}: {err or 'empty output'}"
        got, err = _run(python, brute, out)
        if got is None:
            return f"BRUTE failed on GEN's input for seed {seed} ({err}); input={out[:300]!r}"
    finished = 0
    for t in samples.load(ws):
        got, err = _run(python, brute, t.input)
        if got is None:
            if err == "timeout":
                continue  # a sample can be too large for a brute force; that is not wrong
            return f"BRUTE crashed on sample {t.name}: {err}"
        finished += 1
        if got.strip() != (t.output or "").strip():
            return (f"BRUTE is WRONG on sample {t.name}: input={t.input[:300]!r} "
                    f"expected={(t.output or '').strip()[:200]!r} got={got.strip()[:200]!r}")
    if not finished:
        return "BRUTE finished no public sample within the time limit, so it could not be validated"
    return None


def prepare(ws, problem, chat, python, attempts, log):
    """Ask for brute.py + gen.py, validate them, retry with the reason. Returns (ok, reason)."""
    if samples.func_name(ws):
        return False, "functional (starter-code) problem: stress testing covers stdin problems only"
    if not samples.load(ws):
        return False, "no public samples to validate a brute force against"
    feedback = ""
    for i in range(1, attempts + 1):
        reply = chat(f"stress-{i}", [
            {"role": "system", "content": prompts.STRESS_SYSTEM},
            {"role": "user", "content": prompts.stress_user(problem, feedback)},
        ], 0.2)
        sec = parse.sections(reply.text)
        brute = parse.extract_py(sec.get("BRUTE", ""))
        gen = parse.extract_py(sec.get("GEN", ""))
        if not brute or not gen:
            feedback = "the reply did not contain both a ### BRUTE and a ### GEN python block"
            log(f"    [stress] attempt {i}: {feedback}")
            continue
        ledger.write(ws, BRUTE_FILE, brute + "\n")
        ledger.write(ws, GEN_FILE, gen + "\n")
        feedback = _validate(ws, python)
        if feedback is None:
            log(f"    [stress] brute force + generator validated (attempt {i})")
            return True, ""
        log(f"    [stress] attempt {i} rejected: {feedback[:200]}")
    for f in (BRUTE_FILE, GEN_FILE):
        p = os.path.join(ws, f)
        if os.path.exists(p):
            os.replace(p, p + ".rejected")
    return False, f"no valid brute force after {attempts} attempt(s): {feedback}"


def run(ws, python, cases, round_no, solution="solution.py"):
    sol = os.path.join(ws, solution)
    brute, gen = os.path.join(ws, BRUTE_FILE), os.path.join(ws, GEN_FILE)
    passed = skipped = 0
    for i in range(cases):
        seed = round_no * 100003 + i
        inp, _ = _run(python, gen, "", (str(seed),), GEN_TIMEOUT_SEC)
        if inp is None:
            skipped += 1
            continue
        exp, _ = _run(python, brute, inp)
        if exp is None:
            skipped += 1
            continue
        got, err = _run(python, sol, inp)
        got_s = got.strip() if got is not None else f"<{'timed out' if err == 'timeout' else 'runtime error: ' + err[:200]}>"
        if got_s == exp.strip():
            passed += 1
            continue
        return {"ran": True, "passed": passed, "total": cases, "skipped": skipped,
                "fail": {"seed": seed, "input": inp[:600], "expected": exp.strip()[:400], "got": got_s[:400]}}
    return {"ran": passed > 0, "passed": passed, "total": cases, "skipped": skipped, "fail": None}


def feedback(res):
    if not res or not res.get("ran"):
        return ""
    if res.get("fail") is None:
        return (f"[STRESS TEST: PASSED {res['passed']} random small inputs against an independent, "
                f"sample-validated brute force -- treat a claimed counterexample that contradicts "
                f"this with suspicion and check it by hand.] ")
    f = res["fail"]
    return ("[STRESS TEST: FAILED -- an independent, sample-validated brute force disagrees on a "
            "random small input after {p} agreeing cases. input={i!r} brute force expects={e!r} "
            "got={g!r}. The current solution is WRONG on this input.] ").format(
                p=res["passed"], i=f["input"], e=f["expected"], g=f["got"])
