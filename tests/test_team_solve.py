"""team-solve skill: parity with the authors' loop, replay, guards, client wire handling,
the sample checker, agent mode and the CLI.

python3 -m pytest -q tests/test_team_solve.py   (npm run test:team-solve fetches the reference)

The parity tests need the GVS5H reference loop: GVS5H_ESCALATION=<.../escalation>, which
tests/fetch_gvs5h.sh prints. Missing reference is a failure, not a skip.
"""
import http.server
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, ".codex", "skills", "team-solve")
SCRIPTS = os.path.join(SKILL, "scripts")
CLI = os.path.join(SCRIPTS, "team_solve.py")
sys.path.insert(0, SCRIPTS)

from teamsolve import agents, client, ledger, loop, prompts, samples, verify  # noqa: E402

PROBLEM = ("### Question\nAdd two numbers.\n\n### Format: Read the inputs from stdin solve the problem and "
           "write the answer to stdout (do not directly test on the sample inputs). Enclose your code "
           "within delimiters as follows.")
TESTS = [("2 3\n", "5\n"), ("10 7\n", "17\n")]
GOOD = "import sys\na,b=map(int,sys.stdin.read().split())\nprint(a+b)"
BAD = "print(0)"
# Right on both samples (a < 20), wrong elsewhere: only a stress test can see it.
SNEAKY = "import sys\na,b=map(int,sys.stdin.read().split())\nprint(a+b if a < 20 else a-b)"
BRUTE = "import sys\na,b=map(int,sys.stdin.read().split())\ns=0\nfor _ in range(a): s+=1\nfor _ in range(b): s+=1\nprint(s)"
GEN = "import random,sys\nrandom.seed(int(sys.argv[1]))\nprint(random.randint(0,60), random.randint(0,60))"


def role(messages):
    s = messages[0]["content"]
    if s.startswith("You are the PRIMARY orchestrator (manager)"):
        return "plan"
    if s.startswith("You are the FIRST WORKER"):
        return "ideate"
    if s.startswith("You are the PRIMARY orchestrator and manager"):
        return "manage"
    if s.startswith("A worker's solution attempt was CUT OFF"):
        return "cutoff"
    if s.startswith("You are a test engineer"):
        return "stress"
    if messages[1]["content"].endswith(prompts.FINALIZE_GOAL):
        return "finalize"
    return "worker"


def worker_reply(code, notes, nxt="none", status="continue"):
    return f"### CODE\n```python\n{code}\n```\n### NOTES\n- **Idea:** {notes}\n### NEXT\n- {nxt}\n### STATUS\n{status}"


# memdoor's parity scenarios (bench/results/parity-5pass/workflow/lib/test_parity.py, Apache-2.0)
SCENARIOS = {
    "happy": {
        "plan": ["### PLAN\nAdd them.\n### TASKS\n- Read input\n- Print sum\n- Read input"],
        "ideate": ["### NOTES\nTrivial.\n```python\nx\n```\n### NEXT\n- Use int()\n- Use `eval` ```code```"],
        "manage": ["### STATUS\ncontinue\n### NEXT\n- Implement the sum\n### TASKS\n- [todo] Implement the sum\n- [done] Read input",
                   "### STATUS\ndone\n### TASKS\n- [done] Implement the sum"],
        "worker": [worker_reply(GOOD, "sum works", status="solved")],
    },
    "hard": {
        "plan": [("We need answer user's request. Let's think about the cuts..." * 40, "length")],
        "ideate": ["no sections at all, just prose about approaches"],
        "manage": ["### STATUS\ncontinue\n### NEXT\nTry approach A\n### TASKS\n- [todo] Try approach A\n- [wip] Think",
                   "### STATUS\ncontinue\n### NEXT\n* Try approach B\n### TASKS\n- [todo] Try approach B",
                   "### STATUS\ndone\n### TASKS\n- [done] Try approach B",
                   "### STATUS\ncontinue\n### NEXT\nFix the bug\n### TASKS\n- [todo] Fix the bug",
                   "### STATUS\ncontinue\n### NEXT\nfix the bug \n### TASKS\n- [todo] Fix the bug"],
        "worker": [("We need solve. " + "derivation " * 3000, "length"),
                   worker_reply(BAD, "B is wrong somewhere", nxt="check"),
                   worker_reply(BAD, "still wrong")],
        "cutoff": ["<think>hmm</think>It tried approach A and got half way."],
        "finalize": [worker_reply(GOOD, "final", status="solved")],
    },
    "ten": {
        "plan": ["### PLAN\nP\n### TASKS\n" + "\n".join(f"- task {i}" for i in range(15))],
        "ideate": ["### NOTES\nN\n### NEXT\n- a"],
        "manage": [f"### STATUS\ncontinue\n### NEXT\nround {i}\n### TASKS\n- [todo] round {i}" for i in range(12)],
        "worker": [worker_reply(BAD if i % 2 else GOOD, f"r{i}") for i in range(10)],
        "finalize": [("cut " * 100, "length")],
    },
    "empty": {
        "plan": [""],
        "ideate": [""],
        "manage": ["", "", "", "### STATUS\ndone"] + [""] * 10,
        "worker": ["no code here"] * 10,
        "finalize": ["still nothing"],
    },
}


class Model:
    """A scripted model: the n-th call of a role gets the n-th entry (the last one repeats)."""

    def __init__(self, script):
        self.script, self.used, self.calls = script, {}, []

    def __call__(self, messages, temperature):
        r = role(messages)
        i = self.used.get(r, 0)
        self.used[r] = i + 1
        entry = self.script[r][min(i, len(self.script[r]) - 1)]
        content, finish = (entry, "stop") if isinstance(entry, str) else entry
        self.calls.append((r, temperature, messages[0]["content"], messages[1]["content"]))
        return content, finish


class ScriptedClient:
    def __init__(self, model):
        self.model = model
        self.live = 0

    def params(self):
        return {"model": "scripted"}

    def chat(self, messages, temperature):
        self.live += 1
        content, finish = self.model(messages, temperature)
        return client.Reply(content, finish, model="scripted")


def make_ws(tmp, tests=TESTS, problem=PROBLEM):
    os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
    for n, (i, o) in enumerate(tests, 1):
        open(os.path.join(tmp, "tests", f"{n}.in"), "w").write(i)
        open(os.path.join(tmp, "tests", f"{n}.out"), "w").write(o)
    open(os.path.join(tmp, "task.md"), "w").write(problem)
    return tmp


def run_ours(ws, script, spec="code", verifier=None, **opt):
    model = Model(script)
    sc = ScriptedClient(model)
    calls = ledger.Calls(ws, sc, lambda *a: None)
    o = loop.Options(python=sys.executable, model="scripted", **opt)
    result = loop.Team(ws, PROBLEM, prompts.SPECS[spec], calls.chat, o, lambda *a: None, verifier).run()
    return model, sc, calls, result


# --- parity with the authors' code ---------------------------------------------------

def authors_escalation():
    esc = os.environ.get("GVS5H_ESCALATION")
    if not esc or not os.path.isfile(os.path.join(esc, "multiagent.py")):
        pytest.fail("GVS5H_ESCALATION must point at GVS5H codebase/v2-current/escalation "
                    "(run tests/fetch_gvs5h.sh, or npm run test:team-solve)")
    return esc


def run_authors(script):
    esc = authors_escalation()
    ws_root = tempfile.mkdtemp()
    os.environ.update(MULTIAGENT_WS=ws_root, MULTIAGENT_MODEL="groq:small-model",
                      MULTIAGENT_MAX_ITERS="10", MULTIAGENT_MAX_TASKS="12")
    if esc not in sys.path:
        sys.path.insert(0, esc)
    orch = importlib.import_module("orchestrator")
    ma = importlib.reload(importlib.import_module("multiagent"))
    model = Model(script)

    def chat(m, messages, temperature=0.2, meta=None):
        content, finish = model(messages, temperature)
        if meta is not None:
            meta["finish_reason"] = finish
        return content
    ma.chat = chat
    tests = [SimpleNamespace(testtype="stdin", input=i, output=o) for i, o in TESTS]
    cwd = os.getcwd()
    os.chdir(ws_root)
    try:
        ma.multiagent_solve(PROBLEM, orch.CODE_SPEC, tests=tests)
    finally:
        os.chdir(cwd)
    ws = os.path.join(ws_root, ma._slug(PROBLEM))
    files = {f: open(os.path.join(ws, f)).read() for f in ("solution.py", "notes.md", "plan.md", "tasks.json")}
    shutil.rmtree(ws_root)
    return model.calls, files


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_parity_with_authors(name, tmp_path):
    script = SCENARIOS[name]
    theirs, their_files = run_authors(script)
    ws = make_ws(str(tmp_path))
    model, _, _, _ = run_ours(ws, script)
    ours = model.calls
    assert [c[0] for c in ours] == [c[0] for c in theirs]
    for k, (x, y) in enumerate(zip(theirs, ours)):
        assert x == y, f"call {k} ({x[0]}) differs"
    for f, content in their_files.items():
        assert ledger.read(ws, f) == content, f"{f} differs"


def test_authors_specs_match():
    esc = authors_escalation()
    if esc not in sys.path:
        sys.path.insert(0, esc)
    orch = importlib.import_module("orchestrator")
    for ours, theirs in (("code", "CODE_SPEC"), ("aime", "MATH_SPEC"), ("math500", "MATH500_SPEC"),
                         ("gpqa", "GPQA_SPEC"), ("hle", "HLE_SPEC")):
        t = getattr(orch, theirs)
        assert prompts.SPECS[ours] == {"kind": t["kind"], "solver_system": t["solver_system"]}


def test_strict_format_matches_authors(tmp_path):
    esc = authors_escalation()
    if esc not in sys.path:
        sys.path.insert(0, esc)
    os.environ["MULTIAGENT_MODEL"] = "x:muse-1"
    ma = importlib.reload(importlib.import_module("multiagent"))
    assert ma._strict()
    assert prompts.format_rule("STATUS", "NEXT", "TASKS") == ma._format_rule("STATUS", "NEXT", "TASKS")
    assert loop.Options(model="x:muse-1").strict()
    assert not loop.Options(model="deepseek-flash").strict()


# --- replay ----------------------------------------------------------------------------

def test_replay_reuses_every_call_and_rebuilds_the_ledger(tmp_path):
    ws = make_ws(str(tmp_path))
    _, first, c1, _ = run_ours(ws, SCENARIOS["hard"])
    snap = {f: ledger.read(ws, f) for f in ("solution.py", "notes.md", "plan.md", "tasks.json")}
    _, second, c2, _ = run_ours(ws, SCENARIOS["hard"])
    assert first.live == c1.n and c1.n > 0
    assert second.live == 0 and c2.replayed == c1.n
    assert {f: ledger.read(ws, f) for f in snap} == snap


def test_replay_diverges_where_a_request_changes(tmp_path):
    ws = make_ws(str(tmp_path))
    _, _, first, _ = run_ours(ws, SCENARIOS["hard"])
    names = [n[4:-5] for n in sorted(os.listdir(ledger.state_path(ws, "calls")))]
    # The feedback quotes the first failing sample, so changing its expected output changes
    # the request of the first manager call after a worker wrote code (manage-2 in "hard").
    open(os.path.join(ws, "tests", "1.out"), "w").write("6\n")
    _, sc, second, _ = run_ours(ws, SCENARIOS["hard"])
    diverged_at = names.index("manage-2")
    assert second.replayed == diverged_at
    assert sc.live == second.n - diverged_at
    stale = os.listdir(ledger.state_path(ws, "stale"))
    assert len(stale) == 1
    assert len(os.listdir(ledger.state_path(ws, "stale", stale[0]))) == first.n - diverged_at


# --- guards ------------------------------------------------------------------------

def test_keep_best_restores_the_last_passing_program(tmp_path):
    ws = make_ws(str(tmp_path))
    script = {
        "plan": ["### PLAN\nP\n### TASKS\n- a"],
        "ideate": ["### NOTES\nN\n### NEXT\n- a"],
        "manage": [f"### STATUS\ncontinue\n### NEXT\nround {i}\n### TASKS\n- [todo] round {i}" for i in range(4)]
                  + ["### STATUS\ndone"],
        "worker": [worker_reply(GOOD, "good"), worker_reply(BAD, "regressed")],
        "finalize": [worker_reply(BAD, "final but wrong")],
    }
    *_, result = run_ours(ws, script, max_iters=3, keep_best=True)
    assert result["best_round"] == 1
    assert result["restored_best"] is True
    assert ledger.read(ws, "solution.py").strip() == GOOD
    # The same run without the guard ends on the wrong program: the guard is what changed it.
    ws2 = make_ws(str(tmp_path / "plain"))
    *_, plain = run_ours(ws2, script, max_iters=3)
    assert ledger.read(ws2, "solution.py").strip() == BAD
    assert plain["restored_best"] is False


def test_stress_catches_a_sample_passing_wrong_program(tmp_path):
    ws = make_ws(str(tmp_path))
    script = {
        "plan": ["### PLAN\nP\n### TASKS\n- a"],
        "ideate": ["### NOTES\nN\n### NEXT\n- a"],
        "stress": [f"### BRUTE\n```python\n{BRUTE}\n```\n### GEN\n```python\n{GEN}\n```"],
        "manage": ["### STATUS\ncontinue\n### NEXT\nwrite it\n### TASKS\n- [todo] write it",
                   "### STATUS\ndone\n### TASKS\n- [done] write it",
                   "### STATUS\ndone\n### TASKS\n- [done] fix"],
        "worker": [worker_reply(SNEAKY, "fast path"), worker_reply(GOOD, "fixed")],
    }
    model, _, _, result = run_ours(ws, script, stress=True, stress_cases=50)
    assert result["stress"] is True
    manage_users = [c[3] for c in model.calls if c[0] == "manage"]
    assert "[STRESS TEST: FAILED" in manage_users[1]
    assert "[SAMPLE TESTS: PASSED all 2" in manage_users[1]
    # the manager's 'done' after the failed stress test was overridden, so round 2 ran
    assert result["rounds"] == 2
    assert "[STRESS TEST: PASSED 50" in manage_users[2]
    assert ledger.read(ws, "solution.py").strip() == GOOD


def test_stress_rejects_a_brute_force_that_fails_the_samples(tmp_path):
    ws = make_ws(str(tmp_path))
    wrong_brute = "print(42)"
    script = {
        "plan": ["### PLAN\nP\n### TASKS\n- a"],
        "ideate": ["### NOTES\nN\n### NEXT\n- a"],
        "stress": [f"### BRUTE\n```python\n{wrong_brute}\n```\n### GEN\n```python\n{GEN}\n```"],
        "manage": ["### STATUS\ncontinue\n### NEXT\nwrite it\n### TASKS\n- [todo] w", "### STATUS\ndone"],
        "worker": [worker_reply(GOOD, "ok")],
    }
    model, _, _, result = run_ours(ws, script, stress=True, stress_attempts=2)
    assert result["stress"] is False
    assert "WRONG on sample" in result["stress_reason"]
    stress_users = [c[3] for c in model.calls if c[0] == "stress"]
    assert len(stress_users) == 2 and "PREVIOUS ATTEMPT WAS REJECTED" in stress_users[1]
    assert os.path.exists(os.path.join(ws, "brute.py.rejected"))


# --- custom verifier: any command-checkable task -------------------------------------

GOODCMD = "print('ok')"                 # `python3 solution.py` exits 0
BADCMD = "import sys; sys.exit(1)"       # exits 1


def test_command_verifier_round_and_verdict(tmp_path):
    ws = str(tmp_path)
    open(os.path.join(ws, "solution.py"), "w").write(GOODCMD)
    v = verify.CommandVerifier("python3 solution.py")
    r = v.round(ws)
    assert r["ok"] and r["passed"] == 1 and r["total"] == 1 and "[CHECK: PASSED" in r["text"]
    buf = io.StringIO()
    assert v.verdict(ws, buf) == 0
    open(os.path.join(ws, "solution.py"), "w").write(BADCMD)
    r = v.round(ws)
    assert not r["ok"] and r["passed"] == 0 and "[CHECK: FAILED" in r["text"] and "exited 1" in r["text"]
    buf = io.StringIO()
    assert v.verdict(ws, buf) == 1 and "exited 1" in buf.getvalue()


def test_loop_with_command_verifier_solves(tmp_path):
    ws = make_ws(str(tmp_path))
    script = {
        "plan": ["### PLAN\nP\n### TASKS\n- a"],
        "ideate": ["### NOTES\nN\n### NEXT\n- a"],
        "manage": ["### STATUS\ncontinue\n### NEXT\nwrite it\n### TASKS\n- [todo] write it", "### STATUS\ndone"],
        "worker": [worker_reply(GOODCMD, "ok", status="solved")],
    }
    v = verify.CommandVerifier("python3 solution.py")
    model, _, _, result = run_ours(ws, script, spec="generic", verifier=v)
    assert result["verifier"] == "command" and result["manager_done"] and not result["stress"]
    assert result["history"][0]["check"] == "pass"
    assert ledger.read(ws, "solution.py").strip() == GOODCMD
    # the manager saw the command's verdict, not a sample verdict
    assert "[CHECK: PASSED" in [c[3] for c in model.calls if c[0] == "manage"][1]


def test_loop_command_verifier_overrides_done_and_keeps_best(tmp_path):
    ws = make_ws(str(tmp_path))
    script = {
        "plan": ["### PLAN\nP\n### TASKS\n- a"],
        "ideate": ["### NOTES\nN\n### NEXT\n- a"],
        "manage": ["### STATUS\ncontinue\n### NEXT\nwrite\n### TASKS\n- [todo] w",
                   "### STATUS\ndone\n### TASKS\n- [done] w",     # overruled: command still fails
                   "### STATUS\ndone\n### TASKS\n- [done] w"],
        "worker": [worker_reply(BADCMD, "broke"), worker_reply(GOODCMD, "fixed")],
    }
    v = verify.CommandVerifier("python3 solution.py")
    model, _, _, result = run_ours(ws, script, spec="generic", verifier=v, keep_best=True)
    assert result["rounds"] == 2 and result["best_round"] == 2 and not result["restored_best"]
    assert ledger.read(ws, "solution.py").strip() == GOODCMD
    assert "[CHECK: FAILED" in [c[3] for c in model.calls if c[0] == "manage"][1]


def test_loop_solution_file_override(tmp_path):
    ws = make_ws(str(tmp_path))
    script = {
        "plan": ["### PLAN\nP\n### TASKS\n- a"],
        "ideate": ["### NOTES\nN\n### NEXT\n- a"],
        "manage": ["### STATUS\ncontinue\n### NEXT\nwrite\n### TASKS\n- [todo] w", "### STATUS\ndone"],
        "worker": [worker_reply(GOODCMD, "ok", status="solved")],
    }
    v = verify.CommandVerifier("python3 run.py")
    run_ours(ws, script, spec="generic", verifier=v, solution_file="run.py")
    assert ledger.read(ws, "run.py").strip() == GOODCMD
    assert ledger.read(ws, "solution.py").strip() == ""   # default name stays the empty ledger stub


# --- samples -------------------------------------------------------------------------

def test_check_is_exact_and_run_samples_strips(tmp_path):
    ws = make_ws(str(tmp_path))
    open(os.path.join(ws, "solution.py"), "w").write(GOOD.replace("print(a+b)", "print(a+b, end='  \\n')"))
    res = samples.run_samples(ws)
    assert res == {"ran": True, "passed": 2, "total": 2, "fail": None}
    buf = io.StringIO()
    assert samples.check(ws, out=buf) == 2
    assert "FAIL tests/1.in: output differs" in buf.getvalue()
    open(os.path.join(ws, "solution.py"), "w").write(GOOD)
    buf = io.StringIO()
    assert samples.check(ws, out=buf) == 0
    assert "Samples: 2; failures: 0" in buf.getvalue()


def test_check_without_samples_fails(tmp_path):
    buf = io.StringIO()
    assert samples.check(str(tmp_path), out=buf) == 1
    assert "no sample inputs" in buf.getvalue()


def test_functional_samples(tmp_path):
    ws = str(tmp_path)
    make_ws(ws, tests=[("[1, 2]\n3\n", "[4, 5]\n"), ("[]\n1\n", "[]\n")])
    open(os.path.join(ws, "tests", "func_name"), "w").write("addAll\n")
    open(os.path.join(ws, "solution.py"), "w").write(
        "class Solution:\n    def addAll(self, nums, k):\n        return tuple(x + k for x in nums)\n")
    assert samples.run_samples(ws) == {"ran": False}  # the authors' loop: stdin samples only
    assert samples.run_samples(ws, include_functional=True) == {"ran": True, "passed": 2, "total": 2, "fail": None}
    buf = io.StringIO()
    assert samples.check(ws, out=buf) == 0, buf.getvalue()
    open(os.path.join(ws, "solution.py"), "w").write(
        "class Solution:\n    def addAll(self, nums, k):\n        return [x for x in nums]\n")
    res = samples.run_samples(ws, include_functional=True)
    assert res["passed"] == 1 and res["fail"]["expected"] == "[4, 5]"


# --- math specs ------------------------------------------------------------------------

def test_math_spec_writes_answer_md(tmp_path):
    ws = str(tmp_path)
    open(os.path.join(ws, "task.md"), "w").write("What is 6*7?")
    script = {
        "plan": ["### PLAN\nmultiply\n### TASKS\n- multiply"],
        "ideate": ["### NOTES\nsix sevens\n### NEXT\n- add 7 six times"],
        "manage": ["### STATUS\ncontinue\n### NEXT\nmultiply\n### TASKS\n- [todo] multiply", "### STATUS\ndone"],
        "worker": ["### ANSWER\n6*7 = 42\nANSWER: 42\n### NOTES\n- **x:** y\n### NEXT\n- none\n### STATUS\nsolved"],
    }
    model = Model(script)
    calls = ledger.Calls(ws, ScriptedClient(model), lambda *a: None)
    result = loop.Team(ws, "What is 6*7?", prompts.SPECS["aime"], calls.chat,
                       loop.Options(python=sys.executable), lambda *a: None).run()
    assert "ANSWER: 42" in ledger.read(ws, "answer.md")
    assert result["manager_done"] and not result["finalized"]
    assert "CURRENT ANSWER:" in [c for c in model.calls if c[0] == "manage"][1][3]


# --- router client ---------------------------------------------------------------------

class FakeRouter(http.server.BaseHTTPRequestHandler):
    """Serves queued responses: ("sse", text, reasoning, status, reason, out_tokens) or ("http", code, body)."""
    queue = []
    bodies = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        FakeRouter.bodies.append(json.loads(self.rfile.read(n)))
        item = FakeRouter.queue.pop(0)
        if item[0] == "http":
            self.send_response(item[1])
            self.end_headers()
            self.wfile.write(item[2].encode())
            return
        _, text, reasoning, status, reason, out_tok = item
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def ev(name, obj):
            obj["type"] = name
            self.wfile.write(f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode())
        for ch in reasoning:
            ev("response.reasoning_text.delta", {"delta": ch})
        for ch in text:
            ev("response.output_text.delta", {"delta": ch})
        output = []
        if reasoning:
            output.append({"type": "reasoning", "content": [{"type": "reasoning_text", "text": reasoning}]})
        if text:
            output.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
        resp = {"status": status, "output": output,
                "incomplete_details": {"reason": reason} if reason else None,
                "usage": {"input_tokens": 10, "output_tokens": out_tok,
                          "output_tokens_details": {"reasoning_tokens": 1}}}
        ev("response.completed" if status == "completed" else "response.incomplete", {"response": resp})


@pytest.fixture
def fake_router():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    FakeRouter.queue, FakeRouter.bodies = [], []
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def mk_client(base, cap=1000):
    c = client.RouterClient("deepseek-flash", cap, "high", 0.95, None, attempts=3, base_url=base, token="t")
    return c


def test_client_completed_and_request_shape(fake_router, monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    FakeRouter.queue = [("sse", "hello", "think", "completed", None, 5)]
    r = mk_client(fake_router).chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], 0.3)
    assert (r.text, r.finish, r.reasoning, r.attempts) == ("hello", "stop", "think", 1)
    b = FakeRouter.bodies[0]
    assert b["model"] == "deepseek-flash" and b["temperature"] == 0.3 and b["top_p"] == 0.95
    assert b["stream"] is True and b["max_output_tokens"] == 1000 and b["reasoning"] == {"effort": "high"}
    assert "top_k" not in b
    assert b["input"] == [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_client_cut_at_cap_is_length(fake_router, monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    FakeRouter.queue = [("sse", "partial", "", "incomplete", "max_output_tokens", 1000)]
    r = mk_client(fake_router).chat([{"role": "user", "content": "u"}], 0.2)
    assert (r.text, r.finish, r.attempts) == ("partial", "length", 1)


def test_client_clamped_and_empty_are_retried(fake_router, monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    FakeRouter.queue = [("sse", "short", "", "incomplete", "max_output_tokens", 100),  # clamped: 100 < 0.9*1000
                        ("sse", "", "", "completed", None, 0),                         # empty
                        ("sse", "ok", "", "completed", None, 2)]
    r = mk_client(fake_router).chat([{"role": "user", "content": "u"}], 0.2)
    assert (r.text, r.attempts, len(r.discarded)) == ("ok", 3, 2)


def test_client_reasoning_answers_an_empty_text(fake_router, monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    FakeRouter.queue = [("sse", "", "### CODE\n```python\nprint(1)\n```", "completed", None, 9)]
    r = mk_client(fake_router).chat([{"role": "user", "content": "u"}], 0.2)
    assert r.text.startswith("### CODE") and r.finish == "stop"


def test_client_retries_429_and_fails_on_401(fake_router, monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    FakeRouter.queue = [("http", 429, '{"error":"slow down"}'), ("sse", "ok", "", "completed", None, 2)]
    assert mk_client(fake_router).chat([{"role": "user", "content": "u"}], 0.2).text == "ok"
    FakeRouter.queue = [("http", 401, '{"error":"bad token"}')]
    with pytest.raises(client.CallError):
        mk_client(fake_router).chat([{"role": "user", "content": "u"}], 0.2)


def test_client_gives_up_flagging_infra(fake_router, monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    FakeRouter.queue = [("sse", "", "", "completed", None, 0)] * 3
    r = mk_client(fake_router).chat([{"role": "user", "content": "u"}], 0.2)
    assert r.infra_exhausted and r.attempts == 3


# --- CLI against the fake router ----------------------------------------------------------

def test_cli_loop_run_status_and_exit_codes(fake_router, tmp_path):
    ws = make_ws(str(tmp_path))
    plan = "### PLAN\nadd\n### TASKS\n- add"
    FakeRouter.queue = [("sse", t, "", "completed", None, 5) for t in (
        plan, "### NOTES\nn\n### NEXT\n- add",
        "### STATUS\ncontinue\n### NEXT\nadd\n### TASKS\n- [todo] add",
        worker_reply(GOOD, "done", status="solved"), "### STATUS\ndone")]
    env = dict(os.environ, TEAM_SOLVE_BASE_URL=fake_router, TEAM_SOLVE_API_KEY="t")
    r = subprocess.run([sys.executable, CLI, "run", "--dir", ws], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "Samples: 2; failures: 0" in r.stdout
    rec = json.load(open(ledger.state_path(ws, "run.json")))
    assert rec["result"]["calls"] == 5 and rec["result"]["manager_done"] is True
    # replay: no router traffic needed the second time
    r2 = subprocess.run([sys.executable, CLI, "run", "--dir", ws], capture_output=True, text=True, env=env, timeout=60)
    assert r2.returncode == 0, r2.stderr
    assert json.load(open(ledger.state_path(ws, "run.json")))["result"]["live_calls"] == 0
    s = subprocess.run([sys.executable, CLI, "status", "--dir", ws], capture_output=True, text=True, timeout=60)
    assert s.returncode == 0 and "SOLVED" in s.stdout
    open(os.path.join(ws, "solution.py"), "w").write(BAD)
    c = subprocess.run([sys.executable, CLI, "check", "--dir", ws], capture_output=True, text=True, timeout=60)
    assert c.returncode == 1 and "failures: 2" in c.stdout


def test_cli_loop_verify_cmd_implies_generic(fake_router, tmp_path):
    ws = make_ws(str(tmp_path))
    FakeRouter.queue = [("sse", t, "", "completed", None, 5) for t in (
        "### PLAN\np\n### TASKS\n- w", "### NOTES\nn\n### NEXT\n- w",
        "### STATUS\ncontinue\n### NEXT\nwrite\n### TASKS\n- [todo] w",
        worker_reply(GOODCMD, "ok", status="solved"), "### STATUS\ndone")]
    env = dict(os.environ, TEAM_SOLVE_BASE_URL=fake_router, TEAM_SOLVE_API_KEY="t")
    r = subprocess.run([sys.executable, CLI, "run", "--dir", ws, "--verify-cmd", "python3 solution.py"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "exited 0" in ledger.read(ws, "final-test-output.log")
    rec = json.load(open(ledger.state_path(ws, "run.json")))
    assert rec["options"]["spec"] == "generic" and rec["result"]["verifier"] == "command"
    s = subprocess.run([sys.executable, CLI, "status", "--dir", ws], capture_output=True, text=True, timeout=60)
    assert "ground truth: command `python3 solution.py`" in s.stdout
    # a failing program -> check subcommand with the same command is exit 1
    open(os.path.join(ws, "solution.py"), "w").write(BADCMD)
    c = subprocess.run([sys.executable, CLI, "check", "--dir", ws, "--verify-cmd", "python3 solution.py"],
                       capture_output=True, text=True, timeout=60)
    assert c.returncode == 1 and "exited 1" in c.stdout


def test_cli_refuses_to_nest_inside_a_role(tmp_path):
    ws = make_ws(str(tmp_path))
    env = dict(os.environ, TEAM_SOLVE_ROLE="solve", TEAM_SOLVE_BASE_URL="http://127.0.0.1:9/v1")
    r = subprocess.run([sys.executable, CLI, "run", "--dir", ws], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2 and "refusing to start a team run inside role 'solve'" in r.stderr


def test_cli_router_down_is_exit_2(tmp_path):
    ws = make_ws(str(tmp_path))
    env = dict(os.environ, TEAM_SOLVE_BASE_URL="http://127.0.0.1:9/v1", TEAM_SOLVE_API_KEY="t")
    r = subprocess.run([sys.executable, CLI, "run", "--dir", ws], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2 and "router not reachable" in r.stderr


# --- agent mode against a fake codex ------------------------------------------------------

FAKE_CODEX = r"""#!/usr/bin/env python3
import json, os, sys
role = os.environ["TEAM_SOLVE_ROLE"]
args = sys.argv[1:]
ws = args[args.index("--cd") + 1]
assert "--disable" in args and args[args.index("--disable") + 1] == "multi_agent"
assert 'skills.config=[{name="team-solve",enabled=false}]' in args
assert args[args.index("-a") + 1] == "never" and "exec" in args
open(os.path.join(ws, "roles.log"), "a").write(role + "\n")
def w(n, s): open(os.path.join(ws, n), "w").write(s)
if role == "plan":
    w("plan.md", "plan\n"); w("notes.md", "## Proposal A\n<!-- proposal-a -->\n## Proposal B\n<!-- proposal-b -->\n")
elif role == "solve":
    w("solution.py", os.environ.get("FAKE_FIRST", "print(0)") + "\n")
elif role == "review":
    w("review.md", "status " + open(os.path.join(ws, "test-status.txt")).read())
elif role == "repair":
    if open(os.path.join(ws, "test-status.txt")).read().strip() != "0":
        w("solution.py", os.environ["FAKE_FIX"] + "\n")
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 7}}))
sys.exit(int(os.environ.get("FAKE_FAIL_" + role.replace("-", "_").upper(), "0")))
"""


def fake_codex(tmp_path):
    p = tmp_path / "fake-codex"
    p.write_text(FAKE_CODEX)
    p.chmod(0o755)
    return str(p)


def test_agents_mode_repairs_and_verifies(tmp_path):
    ws = make_ws(str(tmp_path / "p"))
    env = dict(os.environ, CODEX_WORKSPACE_BIN=fake_codex(tmp_path), FAKE_FIRST=BAD, FAKE_FIX=GOOD)
    r = subprocess.run([sys.executable, CLI, "run", "--mode", "agents", "--dir", ws],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    roles = open(os.path.join(ws, "roles.log")).read().split()
    assert roles[0] == "plan" and set(roles[1:3]) == {"ideate-a", "ideate-b"}
    assert roles[3:] == ["solve", "review", "repair"]
    assert ledger.read(ws, "test-status.txt").strip() == "1"
    assert "failures: 0" in ledger.read(ws, "final-test-output.log")
    rec = json.load(open(ledger.state_path(ws, "run.json")))
    assert rec["result"]["usage"] == {"input_tokens": 600, "output_tokens": 42}


def test_agents_mode_unrepaired_is_exit_1_and_failed_role_is_exit_2(tmp_path):
    ws = make_ws(str(tmp_path / "p"))
    env = dict(os.environ, CODEX_WORKSPACE_BIN=fake_codex(tmp_path), FAKE_FIRST=BAD, FAKE_FIX=BAD)
    r = subprocess.run([sys.executable, CLI, "run", "--mode", "agents", "--dir", ws],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 1, r.stderr
    ws2 = make_ws(str(tmp_path / "q"))
    env.update(FAKE_FAIL_SOLVE="3")
    r = subprocess.run([sys.executable, CLI, "run", "--mode", "agents", "--dir", ws2],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 2 and "agent step 'solve' failed" in r.stderr
    # solve was attempted twice (max_retries: 1), and plan/ideate are resumable markers now
    assert open(os.path.join(ws2, "roles.log")).read().split().count("solve") == 2
    assert os.path.exists(os.path.join(ws2, ".team-solve", "agents", "plan.done"))


def test_agent_prompts_name_the_checker_and_starter_contract(tmp_path):
    ws = make_ws(str(tmp_path))
    open(os.path.join(ws, "tests", "func_name"), "w").write("f\n")
    text = agents._context(ws, "task.md", "/x/team_solve.py")
    assert "python3 /x/team_solve.py check --dir" in text and "`f`" in text
    for role_name in agents.ROLES:
        assert "{" not in prompts.AGENT_PROMPTS[role_name].format(task="task.md", run_id="r")


def test_agent_generic_prompts_and_context_name_the_command(tmp_path):
    ws = make_ws(str(tmp_path))
    text = agents._context(ws, "task.md", "/x/team_solve.py", verify_cmd="pytest -q")
    assert "pytest -q" in text and "edit any file" in text
    assert "python3 /x/team_solve.py check" not in text   # no sample-checker instruction
    for role_name in agents.ROLES:
        out = prompts.AGENT_PROMPTS_GENERIC[role_name].format(task="task.md", run_id="r", verify="pytest -q")
        assert "{" not in out and "pytest -q" in out


def test_agents_generic_mode_solves_via_command(tmp_path):
    ws = make_ws(str(tmp_path / "p"))
    env = dict(os.environ, CODEX_WORKSPACE_BIN=fake_codex(tmp_path), FAKE_FIRST=GOODCMD)
    r = subprocess.run([sys.executable, CLI, "run", "--mode", "agents", "--dir", ws,
                        "--verify-cmd", "python3 solution.py"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    roles = open(os.path.join(ws, "roles.log")).read().split()
    assert roles[0] == "plan" and set(roles[1:3]) == {"ideate-a", "ideate-b"} and roles[3:] == ["solve", "review", "repair"]
    assert ledger.read(ws, "test-status.txt").strip() == "0"      # the command passed after solve
    assert "exited 0" in ledger.read(ws, "final-test-output.log")
    rec = json.load(open(ledger.state_path(ws, "run.json")))
    assert rec["options"]["verify_cmd"] == "python3 solution.py"
