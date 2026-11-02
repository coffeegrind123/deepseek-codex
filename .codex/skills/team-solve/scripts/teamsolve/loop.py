"""The authors' multi-agent loop (GVS5H multiagent.py `multiagent_solve`), one call per role.

    plan -> ideate -> manage-0 -> { worker-N [-> cutoff-N] -> samples -> manage-N } x <=10 -> [finalize]

Every role is the same model in a fresh context; the roles talk only through plan.md,
notes.md, tasks.json and solution.py / answer.md. The control flow below mirrors the
authors' line for line -- including the reissued-task stop, the 'done' override when the
samples fail, and finalize being skipped when the manager says done -- and the parity
test runs both against one scripted model.

Two opt-in guards (off by default, so the default run is the authors' method):

* keep_best: best.py holds the latest program that passed the samples (and the stress test
  when on). If the final program does not, best.py is restored. memdoor's pass 4 lost a
  correct program to a later round twice.
* stress: see stress.py.
"""
import json
import os
import re
import shutil
from dataclasses import dataclass

from . import ledger, parse, prompts, samples, stress

TEMPERATURE = {"plan": 0.3, "ideate": 0.4, "manage": 0.2, "worker": 0.2, "cutoff": 0.2, "finalize": 0.2}
BEST_FILE = "best.py"
LEDGER_FILES = ("notes.md", "plan.md", "solution.py", "answer.md", "tasks.json")
STRICT_MODEL_HINTS = ("muse", "glimmer")


@dataclass
class Options:
    max_iters: int = 10
    max_tasks: int = 12
    strict_format: str = "auto"   # auto: the authors' rule (on for muse/glimmer models only)
    include_functional: bool = False
    keep_best: bool = False
    stress: bool = False
    stress_cases: int = 200
    stress_attempts: int = 2
    python: str = ""
    model: str = ""
    solution_file: str = ""   # the code artifact's name; default solution.py

    def strict(self):
        return self.strict_format == "on" or (
            self.strict_format == "auto" and any(s in self.model.lower() for s in STRICT_MODEL_HINTS))


class Team:
    def __init__(self, ws, problem, spec, chat, opts, log, verifier=None):
        self.ws, self.problem, self.spec, self.chat, self.o, self.log = ws, problem, spec, chat, opts, log
        self.verifier = verifier   # None -> the authors' sample check (parity path)
        self.kind = spec["kind"]
        self.out_file = (opts.solution_file or "solution.py") if self.kind == "code" else "answer.md"
        self.stress_ok = False
        self.stress_reason = "off"
        self.best_round = None
        self.rounds = 0
        self.stopped = None
        self.cutoffs = 0
        self.history = []

    # --- helpers --------------------------------------------------------------

    def r(self, name):
        return ledger.read(self.ws, name)

    def w(self, name, content):
        ledger.write(self.ws, name, content)

    def has_answer(self):
        if self.kind == "code":
            return bool(self.r(self.out_file).strip())
        return bool(parse.ANS_RE.search(self.r("answer.md")))

    def save_tasks(self, tasks):
        self.w("tasks.json", json.dumps(tasks, indent=2))

    # --- roles (multiagent.py, same order and arithmetic) -----------------------

    def plan(self):
        reply = self.chat("plan", [
            {"role": "system", "content": prompts.plan_system(self.kind, self.o.strict())},
            {"role": "user", "content": self.problem},
        ], TEMPERATURE["plan"]).text
        sec = parse.sections(reply)
        self.w("plan.md", (sec.get("PLAN") or reply.strip())[:parse.MAX_PLAN_CHARS])
        tasks = []
        parse.add_tasks(tasks, parse.bullets(sec.get("TASKS", "")), self.o.max_tasks)
        self.log(f"    [primary] plan + {len(tasks)} initial tasks")
        return tasks

    def ideate(self):
        reply = self.chat("ideate", [
            {"role": "system", "content": prompts.ideate_system(self.o.strict())},
            {"role": "user", "content": prompts.ideate_user(self.problem, self.r("plan.md"))},
        ], TEMPERATURE["ideate"]).text
        sec = parse.sections(reply)
        ledger.append(self.ws, "notes.md",
                      f"\n## ideation\n{parse.strip_code(sec.get('NOTES') or reply.strip())[:parse.MAX_PLAN_CHARS * 2]}\n")
        proposals = [parse.strip_code(p) for p in parse.bullets(sec.get("NEXT", ""))]
        self.log(f"    [worker:ideate] proposed {len(proposals)} approaches")
        return proposals

    def manage(self, round_no, tasks, proposals, last_summary):
        cur = self.r(self.out_file).strip()
        task_lines = "\n".join(
            f"- [{'done' if t['status'] == 'done' else 'todo'}] {t['desc']}" for t in tasks
        ) or "(none)"
        prop_lines = "\n".join(f"- {p}" for p in proposals) or "(none)"
        reply = self.chat(f"manage-{round_no}", [
            {"role": "system", "content": prompts.manage_system(self.o.strict())},
            {"role": "user", "content": prompts.manage_user(self.problem, self.kind, cur, self.r("notes.md"),
                                                            task_lines, last_summary, prop_lines)},
        ], TEMPERATURE["manage"]).text
        sec = parse.sections(reply)
        curated = parse.parse_tasks(sec.get("TASKS", ""), self.o.max_tasks) or tasks
        status = "done" if sec.get("STATUS", "").strip().lower().startswith("done") else "continue"
        if status == "done" and not cur:
            status = "continue"
        nxt = (parse.bullets(sec.get("NEXT", ""))[:1] or [sec.get("NEXT", "").strip()])[0]
        nxt = re.sub(r"^[-*]\s*", "", nxt).strip()
        if status == "continue" and not nxt:
            todo = [t for t in curated if t["status"] != "done"]
            nxt = todo[0]["desc"] if todo else ""
        if not cur and not nxt:
            status, nxt = "continue", "Implement the full working solution for the most promising approach in the notes."
        self.log(f"    [primary-manage] {status}" + (f", next: {nxt[:60]}" if status == "continue" else ""))
        return status, nxt, curated

    def summarize_cutoff(self, round_no, reply, task_desc):
        txt = parse.strip_think(reply) or reply
        snippet = txt if len(txt) <= 9000 else txt[:3500] + "\n...[middle omitted]...\n" + txt[-5500:]
        s = self.chat(f"cutoff-{round_no}", [
            {"role": "system", "content": prompts.CUTOFF_SYSTEM},
            {"role": "user", "content": f"TASK: {task_desc}\n\nCUT-OFF ATTEMPT:\n{snippet}"},
        ], TEMPERATURE["cutoff"]).text
        return parse.strip_think(s).strip() or "(the cut-off attempt could not be summarized)"

    def worker(self, task, finalize=False):
        cur = self.r(self.out_file)
        goal = prompts.FINALIZE_GOAL if finalize else f"Complete this task: {task['desc']}"
        res = self.chat("finalize" if finalize else f"worker-{task['id']}", [
            {"role": "system", "content": prompts.worker_system(self.kind, self.spec["solver_system"])},
            {"role": "user", "content": prompts.worker_user(self.problem, self.r("plan.md"), self.r("notes.md"),
                                                            cur, goal)},
        ], TEMPERATURE["finalize" if finalize else "worker"])
        reply, finish = res.text, res.finish
        sec = parse.sections(reply)
        wrote = False
        if self.kind == "code":
            code = parse.extract_py(sec.get("CODE", "")) or parse.extract_py(reply)
            if code:
                self.w(self.out_file, code)
                wrote = True
        else:
            ans = sec.get("ANSWER", "").strip()
            if not ans and parse.ANS_RE.search(reply) and len(reply) < parse.MAX_ANSWER_CHARS:
                ans = parse.strip_think(reply).strip()
            if ans and (parse.ANS_RE.search(ans) or not self.r("answer.md").strip()):
                self.w("answer.md", ans)
                wrote = True
        if sec.get("NOTES"):
            if finish != "length":
                self.w("notes.md", sec["NOTES"].strip()[:parse.MAX_PLAN_CHARS * 2] + "\n")
        nexts = [b for b in parse.bullets(sec.get("NEXT", "")) if b.lower() != "none"]
        status = sec.get("STATUS", "continue").strip().lower()
        if finish == "length" and not finalize:
            self.cutoffs += 1
            digest = self.summarize_cutoff(task["id"], reply, task["desc"])
            summary = (f"worker EXCEEDED THE TOKEN LIMIT and was cut off before finishing task: "
                       f"{task['desc'][:80]}. No complete solution was produced -- the approach was "
                       f"too long to finish in one call, so prefer a simpler or different approach "
                       f"next. Summary of its partial thinking: {digest[:500]}")
            self.log(f"    [worker] task {task['id']} -> CUT OFF at token limit; summarized thinking, told manager")
            return "continue", nexts, summary, wrote
        summary = f"worker reported '{status}' on: {task['desc'][:80]}. " + (sec.get("NOTES", "")[:200])
        self.log(f"    [worker] task {task['id']} -> {status}, +{len(nexts)} next steps")
        return status, nexts, summary, wrote

    # --- guards -----------------------------------------------------------------

    def check_program(self, round_no):
        """Sample run (authors') + optional stress run, for the current program."""
        res = samples.run_samples(self.ws, self.o.python, self.o.include_functional, self.out_file)
        sres = None
        if self.stress_ok and res.get("ran") and res["passed"] == res["total"]:
            sres = stress.run(self.ws, self.o.python, self.o.stress_cases, round_no, self.out_file)
            if sres.get("ran"):
                tail = "all agree" if sres["fail"] is None else f"FAILED at seed {sres['fail']['seed']}"
                self.log(f"    [stress] {sres['passed']}/{sres['total']} agree, {sres['skipped']} skipped: {tail}")
        return res, sres

    def recheck(self, round_no):
        """Re-run the ground-truth check (custom command, or samples+stress) on the program."""
        if self.verifier is not None:
            return self.verifier.round(self.ws), None
        return self.check_program(round_no)

    def good(self, res, sres):
        if self.verifier is not None:
            return bool(res and res.get("ok"))
        if not (res and res.get("ran") and res["passed"] == res["total"]):
            return False
        if self.stress_ok:
            return bool(sres and sres.get("ran") and sres["fail"] is None)
        return True

    def note_best(self, round_no, res, sres):
        if self.o.keep_best and self.good(res, sres):
            shutil.copyfile(os.path.join(self.ws, self.out_file), os.path.join(self.ws, BEST_FILE))
            self.best_round = round_no

    # --- the loop -----------------------------------------------------------------

    def run(self):
        for f in LEDGER_FILES + ((BEST_FILE,) if self.o.keep_best else ()) + (
                (self.out_file,) if self.out_file not in LEDGER_FILES else ()):
            self.w(f, "")
        tasks = self.plan()
        proposals = self.ideate()
        if self.o.stress and self.kind == "code" and self.verifier is None:
            self.stress_ok, self.stress_reason = stress.prepare(
                self.ws, self.problem, self.chat, self.o.python, self.o.stress_attempts, self.log)
            if not self.stress_ok:
                self.log(f"    [stress] DISABLED for this run: {self.stress_reason}")
        status, next_desc, tasks = self.manage(0, tasks, proposals, "ideation complete")
        self.save_tasks(tasks)

        iters, prev_desc = 0, None
        while status == "continue" and next_desc and iters < self.o.max_iters:
            if prev_desc is not None and next_desc.strip().lower() == prev_desc.strip().lower():
                self.log(f"    [primary] reissued the same task; no progress, stopping after {iters} iters")
                self.stopped = "reissued"
                break
            prev_desc = next_desc
            iters += 1
            task = {"id": iters, "desc": next_desc, "status": "in_progress", "result": ""}
            _, nexts, summary, wrote = self.worker(task)
            res = sres = None
            if self.kind == "code" and wrote and self.verifier is not None:
                res = self.verifier.round(self.ws)
                summary = res["text"] + summary
                self.log(f"    [check] verification command {'PASSED' if res['ok'] else 'FAILED'}")
                self.note_best(iters, res, None)
            elif self.kind == "code" and wrote:
                res, sres = self.check_program(iters)
                if res.get("ran"):
                    summary = samples.feedback(res) + stress.feedback(sres) + summary
                    self.log(f"    [samples] {res['passed']}/{res['total']} public tests passed")
                    self.note_best(iters, res, sres)
            self.history.append({"round": iters, "task": next_desc, "wrote": wrote,
                                 "samples": res and res.get("ran") and f"{res['passed']}/{res['total']}",
                                 "check": res and ("pass" if res.get("ok") else "fail") if self.verifier else None,
                                 "stress": sres and sres.get("ran") and (sres["fail"] is None)})
            status, next_desc, tasks = self.manage(iters, tasks, nexts, summary)
            if res and res.get("ran") and res["passed"] < res["total"] and status == "done":
                status = "continue"
                if not next_desc:
                    next_desc = ("The verification command still fails; fix it or try a different approach."
                                 if self.verifier is not None
                                 else "The solution fails the public sample tests; fix it or try a different approach.")
                self.log("    [primary-manage] overriding 'done' -- ground-truth check still failing")
            elif sres and sres.get("ran") and sres["fail"] is not None and status == "done":
                status = "continue"
                if not next_desc:
                    next_desc = "The solution fails the stress test against the brute force; fix it or try a different approach."
                self.log("    [primary-manage] overriding 'done' -- stress test still failing")
            self.save_tasks(tasks)
        else:
            if iters >= self.o.max_iters and status == "continue" and next_desc:
                self.stopped = "max_iters"
        self.rounds = iters
        manager_done = status == "done"

        if manager_done and self.has_answer():
            self.log("    [finalize] skipped (primary marked done)")
            finalized = False
        else:
            self.worker({"id": 0, "desc": "finalize"}, finalize=True)
            finalized = True

        restored = False
        if self.kind == "code" and self.o.keep_best and self.best_round is not None:
            res, sres = self.recheck(self.o.max_iters + 1)
            if not self.good(res, sres):
                shutil.copyfile(os.path.join(self.ws, BEST_FILE), os.path.join(self.ws, self.out_file))
                restored = True
                self.log(f"    [keep-best] final program failed its checks; restored round {self.best_round}'s")

        return {"manager_done": manager_done, "finalized": finalized, "rounds": self.rounds,
                "stopped": self.stopped, "cutoffs": self.cutoffs, "best_round": self.best_round,
                "restored_best": restored, "stress": self.stress_ok, "stress_reason": self.stress_reason,
                "verifier": self.verifier.kind if self.verifier else "samples", "history": self.history}
