#!/usr/bin/env python3
"""team-solve: several fresh-context roles solve one task through files on disk.

    team_solve.py [run] "what to do" [--until "end condition"] [--dir D]   a task from a prompt (agents mode)
    team_solve.py run    [--dir D] [--mode loop|agents] [options]           the folder's task.md / problem.md
    team_solve.py check  [--dir D]                                          the strict check on its own
    team_solve.py status [--dir D]                                          the last run's result and token totals

A prompt is the task: the run works in --dir (default: the current folder, usually the
project), a planner settles the Check (a command, the samples, or a judge when nothing can
be run), and the team works until it passes. --until "sentence" makes the person's own end
condition binding; --verify-cmd fixes the Check. The run's own files are in
.team-solve/runs/<id>/, outside the project's diff.

Without a prompt the folder holds the statement (task.md, or problem.md, or --problem FILE)
and, for a contest problem, paired public samples tests/N.in + tests/N.out (plus
tests/func_name for a LeetCode-style starter-code problem). Loop mode writes plan.md,
notes.md, tasks.json, solution.py (or answer.md for --spec aime|math500|gpqa|hle),
final-test-output.log and .team-solve/.

Exit status: 0 solved, 1 not solved, 2 usage or infrastructure error (router down, no
token, a role that could not run).
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from teamsolve import ledger, parse, prompts, samples  # noqa: E402

EXIT_SOLVED = 0
EXIT_UNSOLVED = 1
EXIT_ERROR = 2
PROBLEM_FILES = ("task.md", "problem.md")
DEFAULT_MODEL = "deepseek-flash"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def duration(text):
    """'90', '90s', '10m', '4h' -> seconds."""
    text = str(text).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}.get(text[-1:], None)
    return int(float(text[:-1]) * mult) if mult else int(float(text))


def find_problem(ws, explicit):
    if explicit:
        p = explicit if os.path.isabs(explicit) else os.path.join(ws, explicit)
        if not os.path.isfile(p):
            raise SystemExit(f"team-solve: problem file not found: {p}")
        return p
    for name in PROBLEM_FILES:
        p = os.path.join(ws, name)
        if os.path.isfile(p):
            return p
    raise SystemExit(f"team-solve: no {' or '.join(PROBLEM_FILES)} in {ws} (or pass --problem FILE)")


def balance_snapshot():
    path = os.environ.get("DEEPSEEK_BALANCE_CACHE_FILE") or os.path.join(
        HERE, "..", "..", "..", "cache", "deepseek-balance.json")
    try:
        d = json.load(open(path))
        return {"fetched_at": d.get("fetched_at"), "total_usd": (d.get("balance") or {}).get("total_usd"),
                "stale": d.get("stale")}
    except (OSError, ValueError):
        return None


def strict_verdict(ws, spec_kind, verifier=None):
    if verifier is None and spec_kind != "code":
        ans = ledger.read(ws, "answer.md")
        return (EXIT_SOLVED if parse.ANS_RE.search(ans) else EXIT_UNSOLVED), None
    with open(os.path.join(ws, "final-test-output.log"), "w") as f:
        failures = verifier.verdict(ws, f) if verifier is not None else samples.check(ws, out=f)
    return (EXIT_SOLVED if failures == 0 else EXIT_UNSOLVED), failures


def prompt_from_args(a):
    """The prompt words joined, or stdin for a lone '-'; '' when none was given."""
    words = a.prompt or []
    if words == ["-"]:
        return sys.stdin.read()
    return " ".join(words).strip()


def pick_mode(a, ws, prompt):
    """auto: agents for a prompt, an --until, or a folder with nothing a loop could check;
    loop (the authors' benchmarked method) for samples, a --verify-cmd folder or a math spec."""
    if a.mode != "auto":
        return a.mode
    if prompt or a.until:
        return "agents"
    if a.spec != "code" or a.verify_cmd or samples.load(ws):
        return "loop"
    return "agents"


def cmd_run(a):
    if os.environ.get("TEAM_SOLVE_ROLE"):
        log(f"team-solve: refusing to start a team run inside role '{os.environ['TEAM_SOLVE_ROLE']}' "
            f"of another one")
        return EXIT_ERROR
    ws = os.path.abspath(a.dir)
    if not os.path.isdir(ws):
        log(f"team-solve: no such folder: {ws}")
        return EXIT_ERROR
    prompt = prompt_from_args(a)
    a.mode = pick_mode(a, ws, prompt)
    if a.until and a.mode == "loop":
        log("team-solve: --until needs --mode agents: loop mode's ground truth is the samples or "
            "--verify-cmd, and it writes one file")
        return EXIT_ERROR
    if a.mode == "agents":
        return run_prompt(a, ws, prompt)
    if prompt:
        problem = prompt
    else:
        problem = open(find_problem(ws, a.problem)).read()
    if a.wrap_lcb:
        problem = prompts.lcb_prompt(problem.strip())
    # A custom verification command makes the task generic; use the neutral solver spec
    # unless the user named one explicitly.
    if a.verify_cmd and a.spec == "code":
        a.spec = "generic"
    spec = prompts.SPECS[a.spec]
    verifier = build_verifier(a)
    if a.verify_cmd:
        log(f"team-solve: ground truth is the command `{a.verify_cmd}` (exit 0 = solved)")
    elif spec["kind"] == "code" and not samples.load(ws):
        log(f"team-solve: WARNING: no tests/*.in in {ws} and no --verify-cmd: the loop gets no "
            f"verdicts and the final check fails by design")
    if not a.verify_cmd and spec["kind"] == "code" and samples.func_name(ws) and a.samples != "all" and a.mode == "loop":
        log("team-solve: tests/func_name present: the authors' loop runs stdin samples only; "
            "pass --samples all to give the manager functional sample verdicts")

    started = time.time()
    bal0 = balance_snapshot()
    with ledger.Lock(ws):
        if a.fresh:
            calls = ledger.state_path(ws, "calls")
            if os.path.isdir(calls) and os.listdir(calls):
                dest = ledger.state_path(ws, "stale", "fresh-" + time.strftime("%Y%m%d-%H%M%S"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(calls, dest)
                log(f"team-solve: --fresh: previous calls moved to {os.path.relpath(dest, ws)}")
        result = run_loop(a, ws, problem, spec, verifier)
        if result.get("error"):
            write_run(ws, a, result, started, bal0, None, None)
            log(f"team-solve: {result['error']}")
            return EXIT_ERROR
        code, failures = strict_verdict(ws, spec["kind"], verifier)
        write_run(ws, a, result, started, bal0, code, failures)
    if spec["kind"] == "code":
        sys.stdout.write(ledger.read(ws, "final-test-output.log"))
    log(f"team-solve: {'SOLVED' if code == EXIT_SOLVED else 'NOT SOLVED'} -- "
        f"{os.path.relpath(os.path.join(ws, '.team-solve', 'run.json'))}")
    return code


def build_verifier(a):
    """None (default) means the sample check; --verify-cmd means a CommandVerifier."""
    from teamsolve import verify
    if a.verify_cmd:
        return verify.CommandVerifier(a.verify_cmd, duration(a.verify_timeout))
    return None


def run_loop(a, ws, problem, spec, verifier):
    from teamsolve import client, loop
    try:
        c = client.RouterClient(a.model, a.max_output_tokens, a.effort, a.top_p, a.top_k,
                                a.attempts, duration(a.call_timeout), log=log)
    except client.CallError as e:
        return {"error": str(e)}
    ok, detail = c.health()
    if not ok:
        return {"error": f"router not reachable at {client.router_base()} ({detail}); "
                         f"start it with ./codex-router ensure"}
    calls = ledger.Calls(ws, c, log, replay=True)
    opts = loop.Options(max_iters=a.max_iters, max_tasks=a.max_tasks, strict_format=a.strict_format,
                        include_functional=a.samples == "all", keep_best=a.keep_best, stress=a.stress,
                        stress_cases=a.stress_cases, stress_attempts=a.stress_attempts,
                        python=a.python, model=a.model, solution_file=a.solution_file)
    try:
        result = loop.Team(ws, problem, spec, calls.chat, opts, log, verifier).run()
    except client.CallError as e:
        return {"error": f"model call failed: {e}", "calls": calls.n}
    result.update(calls=calls.n, live_calls=calls.live, replayed_calls=calls.replayed, tokens=calls.tokens)
    return result


def run_prompt(a, ws, prompt):
    """agents mode: the task a prompt defines, in the project (teamsolve/pipeline.py)."""
    from teamsolve import agents, client, gitstate, pipeline
    if not prompt:
        if a.problem or any(os.path.isfile(os.path.join(ws, n)) for n in PROBLEM_FILES):
            prompt = open(find_problem(ws, a.problem)).read()
        else:
            log('team-solve: no task: team_solve.py "<what to do>" [--until "<end condition>"], '
                f"or a {' or '.join(PROBLEM_FILES)} in {ws}")
            return EXIT_ERROR
    if a.verify_cmd:
        log(f"team-solve: the Check is the command `{a.verify_cmd}` (exit 0 = solved)")
    if a.until:
        log(f"team-solve: end condition: {a.until}")
    started = time.time()
    bal0 = balance_snapshot()
    try:
        c = client.RouterClient(a.model, a.max_output_tokens, a.effort, a.top_p, a.top_k,
                                a.attempts, duration(a.call_timeout), log=log)
    except client.CallError as e:
        log(f"team-solve: {e}")
        return EXIT_ERROR
    ok, detail = c.health()
    if not ok:
        log(f"team-solve: router not reachable at {client.router_base()} ({detail}); start it with "
            f"./codex-router ensure")
        return EXIT_ERROR
    timeouts = {"direct_timeout": pipeline.DIRECT_TIMEOUT, "work_timeout": pipeline.WORK_TIMEOUT}
    if a.role_timeout != "auto":
        timeouts = {k: duration(a.role_timeout) for k in timeouts}
    opts = pipeline.Options(until=a.until, verify_cmd=a.verify_cmd or None, verify_timeout=duration(a.verify_timeout),
                            max_iters=a.max_iters, python=a.python, fresh=a.fresh, **timeouts)
    with ledger.Lock(ws):
        try:
            run = pipeline.PromptRun(
                ws, prompt, opts, log,
                make_calls=lambda state: ledger.Calls(ws, c, log, replay=True, state=state),
                make_codex=lambda d, meter: agents.Codex(ws, d, a.model, a.effort, log, meter))
            log(f"team-solve: run {run.run_id} in {run.rel}/")
            result = run.run()
        except (agents.RoleError, client.CallError, gitstate.GitError) as e:
            result = {"error": str(e)}
        if result.get("error"):
            write_run(ws, a, result, started, bal0, None, None)
            log(f"team-solve: {result['error']}")
            return EXIT_ERROR
        code = EXIT_SOLVED if result["solved"] else EXIT_UNSOLVED
        write_run(ws, a, result, started, bal0, code, None if code == EXIT_SOLVED else 1)
    sys.stdout.write(ledger.read(ws, os.path.join(result["run_dir"], "final-check.log")))
    how = "judged, not checked: no command could check it" if result["check_kind"] == "judge" else \
        f"checked by {'the samples' if result['check_kind'] == 'samples' else '`' + result['check_cmd'] + '`'}"
    log(f"team-solve: {'SOLVED' if code == EXIT_SOLVED else 'NOT SOLVED'} ({how}) -- "
        f"{result['run_dir']}/  ({result['keep']['note']})")
    return code


def write_run(ws, a, result, started, bal0, code, failures):
    bal1 = balance_snapshot()
    rec = {"finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "seconds": int(time.time() - started),
           "options": {k: v for k, v in vars(a).items() if k != "func"}, "result": result,
           "exit": code, "final_failures": failures, "balance_before": bal0, "balance_after": bal1}
    if bal0 and bal1 and bal0.get("fetched_at") != bal1.get("fetched_at"):
        try:
            rec["balance_delta_usd"] = round(float(bal0["total_usd"]) - float(bal1["total_usd"]), 4)
        except (TypeError, ValueError):
            pass
    ledger.write(ws, os.path.join(ledger.STATE_DIR, "run.json"), json.dumps(rec, indent=1, default=str))


def cmd_check(a):
    ws = os.path.abspath(a.dir)
    from teamsolve import verify
    if getattr(a, "verify_cmd", None):
        failures = verify.CommandVerifier(a.verify_cmd, duration(a.verify_timeout)).verdict(ws, sys.stdout)
    elif not samples.load(ws) and last_prompt_run(ws):
        # A prompt run's settled Check: its command, or the judge's recorded verdict.
        rec = last_prompt_run(ws)
        r = rec["result"]
        if r.get("check_kind") == "command":
            failures = verify.CommandVerifier(r["check_cmd"], duration(a.verify_timeout)).verdict(ws, sys.stdout)
        else:
            from teamsolve.pipeline import verdict_of
            v = verdict_of(ledger.read(ws, os.path.join(r["run_dir"], "verdict.md")))
            print(f"no command to run: the run's judge said {v} ({r['run_dir']}/verdict.md)")
            failures = 0 if v == "PASS" else 1
    else:
        failures = samples.check(ws, python=a.python)
    return EXIT_SOLVED if failures == 0 else EXIT_UNSOLVED


def last_prompt_run(ws):
    p = ledger.state_path(ws, "run.json")
    if not os.path.exists(p):
        return None
    rec = json.load(open(p))
    r = rec.get("result") or {}
    return rec if r.get("run_dir") and r.get("check_kind") else None


def cmd_status(a):
    ws = os.path.abspath(a.dir)
    p = ledger.state_path(ws, "run.json")
    if not os.path.exists(p):
        print(f"no run recorded in {ws}")
        return EXIT_ERROR
    rec = json.load(open(p))
    r = rec.get("result") or {}
    o = rec.get("options") or {}
    print(f"mode {o.get('mode')}  model {o.get('model')}  spec {o.get('spec')}  "
          f"finished {rec.get('finished_at')}  {rec.get('seconds')}s")
    if o.get("mode") == "loop":
        ground = f"command `{o.get('verify_cmd')}`" if o.get("verify_cmd") else "samples"
        print(f"ground truth: {ground}")
    fcount = rec.get("final_failures")
    print(f"verdict: {'SOLVED' if rec.get('exit') == 0 else 'NOT SOLVED'}"
          + (f"  (check failures: {fcount})" if fcount is not None else ""))
    if o.get("mode") == "loop":
        print(f"rounds {r.get('rounds')}  manager_done {r.get('manager_done')}  finalized {r.get('finalized')}  "
              f"stopped {r.get('stopped')}  cutoffs {r.get('cutoffs')}")
        print(f"calls {r.get('calls')} ({r.get('live_calls')} live, {r.get('replayed_calls')} replayed)  "
              f"tokens {r.get('tokens')}")
        if o.get("keep_best"):
            print(f"keep-best: best round {r.get('best_round')}  restored {r.get('restored_best')}")
        if o.get("stress"):
            print(f"stress: {'on' if r.get('stress') else 'disabled: ' + str(r.get('stress_reason'))}")
        for h in r.get("history") or []:
            verdict = h.get("check") or h.get("samples")
            print(f"  round {h['round']}: check {verdict}  stress {h.get('stress')}  task: {h['task'][:90]}")
    else:
        if r.get("error"):
            print(f"error: {r['error']}")
        else:
            kind = r.get("check_kind")
            how = {"command": f"command `{r.get('check_cmd')}`", "samples": "samples",
                   "judge": "the judge (judged, not checked: no command could check it)"}.get(kind, kind)
            print(f"run {r.get('run_id')} in {r.get('run_dir')}/  check settled: {how}")
            if o.get("until"):
                print(f"end condition: {o.get('until')}")
            print(f"direct attempt {'passed' if r.get('direct_passed') else 'did not pass'}  rounds {r.get('rounds')}  "
                  f"accepted {r.get('accepted')}  stopped {r.get('stopped')}  final check {r.get('final_check')}  "
                  f"outside scope {r.get('outside_scope')}")
            print(f"{(r.get('keep') or {}).get('note')}")
            for h in r.get("history") or []:
                print(f"  round {h['round']}: check {h['check']}  review {h['review']}  judge {h['judge']}  "
                      f"{'ACCEPTED' if h['accepted'] else 'not accepted'}")
            print(f"calls {r.get('calls')} ({r.get('live_calls')} live, {r.get('replayed_calls')} replayed)  "
                  f"tokens {r.get('tokens')}  agent usage {r.get('usage')}")
    if "balance_delta_usd" in rec:
        print(f"DeepSeek balance change during the run: ${rec['balance_delta_usd']} "
              f"(account-wide; includes any other session's spend)")
    return EXIT_SOLVED if rec.get("exit") == 0 else EXIT_UNSOLVED


def parser():
    ap = argparse.ArgumentParser(prog="team-solve", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=".", help="problem folder (default: cwd)")
    common.add_argument("--python", default=sys.executable, help="interpreter that runs solution.py")

    r = sub.add_parser("run", parents=[common], help="do the task a prompt defines, or the problem in --dir")
    r.add_argument("prompt", nargs="*",
                   help="the task, in plain words ('-' reads it from stdin). Without one, the folder's "
                        "task.md / problem.md is the task")
    r.add_argument("--until", metavar="SENTENCE",
                   help="the end condition in plain words: the run is done only when the judge (and the "
                        "Check, when one can be run) agree it holds. Agents mode")
    r.add_argument("--mode", choices=("auto", "loop", "agents"), default="auto",
                   help="agents: direct attempt, planner, ideators, solver, review/judge/repair rounds, "
                        "editing the project with codex exec (the default for a prompt, --until, or a "
                        "folder with nothing to check); loop: the authors' one-call-per-role loop writing "
                        "one file (the default for samples, a --verify-cmd folder or a math spec)")
    r.add_argument("--problem", help="statement file (default: task.md, else problem.md)")
    r.add_argument("--spec", choices=sorted(prompts.SPECS), default="code",
                   help="role prompts: code (default), generic (any command-checkable task), or a "
                        "math/QA spec from the authors' bench. --verify-cmd implies generic.")
    r.add_argument("--verify-cmd",
                   help="the command that decides success (run in --dir; exit 0 = solved, its output "
                        "feeds the manager). Replaces the sample check: the team iterates until it passes. "
                        "In agents mode the roles edit the whole workspace; in loop mode they write one "
                        "Python file (--solution-file).")
    r.add_argument("--verify-timeout", default="30m", help="wall clock for one --verify-cmd run")
    r.add_argument("--solution-file", default="",
                   help="loop mode: the file the single-file solution is written to (default solution.py)")
    r.add_argument("--wrap-lcb", action="store_true",
                   help="wrap a bare statement in LiveCodeBench's stdin framing, as the authors' bench did")
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--effort", default="high", help="reasoning effort sent with every call (''=omit)")
    r.add_argument("--max-output-tokens", type=int, default=128000, help="reply cap per call (authors: 128K)")
    r.add_argument("--top-p", type=float, default=0.95, help="authors' value; --top-p -1 omits it")
    r.add_argument("--top-k", type=int, default=None, help="omitted by default (a Qwen generation-config value)")
    r.add_argument("--max-iters", type=int, default=10,
                   help="loop: manager/worker rounds; agents: review rounds (each after the first follows a repair)")
    r.add_argument("--max-tasks", type=int, default=12)
    r.add_argument("--strict-format", choices=("auto", "on", "off"), default="auto")
    r.add_argument("--samples", choices=("stdin", "all"), default="stdin",
                   help="all: also run functional (tests/func_name) samples inside the loop")
    r.add_argument("--keep-best", action="store_true", help="restore the last sample-passing program if the final one fails")
    r.add_argument("--stress", action="store_true", help="validate programs against a generated brute force")
    r.add_argument("--stress-cases", type=int, default=200)
    r.add_argument("--stress-attempts", type=int, default=2)
    r.add_argument("--attempts", type=int, default=4, help="attempts per model call")
    r.add_argument("--call-timeout", default="4h", help="wall clock per model call (memdoor: 4h)")
    r.add_argument("--role-timeout", default="auto",
                   help="agents mode: wall clock per agent turn (auto: direct 30m, solve and repair 90m, memdoor's)")
    r.add_argument("--fresh", action="store_true", help="discard recorded calls instead of replaying them")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("check", parents=[common], help="strict sample check of solution.py (or --verify-cmd)")
    c.add_argument("--verify-cmd", help="run this command as the check instead of the samples (exit 0 = pass)")
    c.add_argument("--verify-timeout", default="30m")
    c.set_defaults(func=cmd_check)
    s = sub.add_parser("status", parents=[common], help="the last run's result")
    s.set_defaults(func=cmd_status)
    return ap


SUBCOMMANDS = ("run", "check", "status")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # `team_solve.py "do this"` is `team_solve.py run "do this"`.
    if argv and argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        argv.insert(0, "run")
    a = parser().parse_args(argv)
    if getattr(a, "top_p", None) is not None and a.top_p < 0:
        a.top_p = None
    if getattr(a, "effort", None) == "":
        a.effort = None
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
