"""`--mode agents`: a team does the task a prompt defines, in the project it runs in.

Memdoor's built-in team-solve (workflows/team-solve at 47dad8b), with its single repair
round made a loop of rounds:

    problem -+- direct (agent: the prompt as written) -------------+- test-0 -+- passed -----------------------+
             +- outline - plan (llm) - settle - baseline ----------+          +- failed: context - ideate a||b -+- solve (agent)
                                                                                                                |
    round r = 1..max:  test-r - review-r (llm) - [judge-r (llm, fresh)] -- accepted? -> keep - final
                                                       |  not accepted
                                                       +- repair-r (agent) -> round r+1

* The planner settles the Check: a command in backticks, the samples, or the judge when
  nothing can be run. --verify-cmd fixes it; --until puts the person's end condition first
  in Done when, word for word, and into the Prompt itself.
* A round is accepted when the Check passes (or there is none to run), the review does not
  FAIL, and -- whenever a command cannot settle it alone (no command, a repaired change, a
  review that did not say PASS) -- a judge that has seen none of the team's reasoning says
  PASS. The samples decide alone, as in memdoor (no judge).
* The keep net: the team's work replaces the direct attempt only when it is better proven
  (memdoor keep.yaml). Neither proven: a command/samples Check puts the direct attempt
  back; a judge-only task keeps the latest work, the one every review so far shaped.
* The run's files live in .team-solve/runs/<id>/, never in the project's diff.

Resume: steps are recorded in state.json as they finish. Rerunning the same prompt while
the latest run is unfinished continues it from the first unfinished step (the llm calls
replay from calls/); --fresh, or a finished run, starts a new run on the tree as it is.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from . import agents, failures, gitstate, ledger, prompts, samples, task as tasklib

RUNS = "runs"
TEMPERATURE = 0.2
PLAN_TEMPERATURE = 0.3
IDEATE_TEMPERATURE = 0.4
MAX_DIFF_CHARS = 200_000
MAX_LOG_CHARS = 30_000
OUTLINE_FILES = ("README.md", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "Makefile", "go.mod", "package.json",
                 "pyproject.toml", "Cargo.toml", "setup.py", "requirements.txt")
STATUS_PASS, STATUS_FAIL, STATUS_NONE = "0", "1", "none"
DIRECT_TIMEOUT = 30 * 60      # memdoor: direct 30m
WORK_TIMEOUT = 90 * 60        # memdoor: solve and repair 90m


def cap(text, limit):
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n\n[... {len(text) - limit} characters omitted ...]\n\n{text[-tail:]}"


def _shape(log):
    """A failing log's last lines with numbers (timings, addresses, pids) blanked, so two runs
    of the same broken Check compare equal. The first line (check: <cmd>) is dropped."""
    lines = [l for l in log.split("\n")[1:] if l.strip()]
    return [re.sub(r"[0-9]+", "#", l) for l in lines[-20:]]


def verdict_of(text):
    """PASS, FAIL or none, from the first `verdict:` line in the first ten (markdown emphasis
    aside). A call that narrates instead of answering gave no verdict: none, not a FAIL."""
    for line in (text or "").split("\n")[:10]:
        s = line.translate(str.maketrans("", "", "*`_")).strip().lower()
        if s.startswith("verdict:"):
            v = s[len("verdict:"):].strip().upper()
            if v.startswith("PASS"):
                return "PASS"
            if v.startswith("FAIL"):
                return "FAIL"
    return "none"


class Options:
    def __init__(self, until=None, verify_cmd=None, verify_timeout=1800, max_iters=10, python="python3",
                 direct_timeout=DIRECT_TIMEOUT, work_timeout=WORK_TIMEOUT, fresh=False):
        self.until, self.verify_cmd, self.verify_timeout = until, verify_cmd, verify_timeout
        self.max_iters, self.python = max(1, max_iters), python
        self.direct_timeout, self.work_timeout, self.fresh = direct_timeout, work_timeout, fresh


class PromptRun:
    def __init__(self, ws, prompt, opts, log, make_calls, make_codex):
        """make_calls(state_dir) -> ledger.Calls; make_codex(log_dir, meter_path) -> agents.Codex."""
        self.ws, self.o, self.log = ws, opts, log
        self.prompt = tasklib.prompt_text(prompt, opts.until)
        self.run_id, self.resumed = self._pick_run()
        self.rd = ledger.state_path(ws, RUNS, self.run_id)
        self.rel = os.path.relpath(self.rd, ws)
        os.makedirs(self.rd, exist_ok=True)
        self.state = self._load()
        self.calls = make_calls(self.rd)
        self.codex = make_codex(os.path.join(self.rd, "agents"), os.path.join(self.rd, "meter.jsonl"))
        self.tree = gitstate.Tree(ws, self.rd, self.run_id, log)
        self.spec = None
        self.history = []

    # --- run folder and resume ---------------------------------------------------

    def _pick_run(self):
        latest = ledger.state_path(self.ws, RUNS, "latest")
        digest = hashlib.sha256(self.prompt.encode()).hexdigest()
        if not self.o.fresh and os.path.exists(latest):
            rid = open(latest).read().strip()
            sp = ledger.state_path(self.ws, RUNS, rid, "state.json")
            if os.path.exists(sp):
                st = json.load(open(sp))
                if st.get("prompt_sha") == digest and not st.get("finished"):
                    self.log(f"team-solve: resuming run {rid} from its first unfinished step")
                    return rid, True
        rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        ledger.ensure_state_dir(self.ws)
        os.makedirs(ledger.state_path(self.ws, RUNS, rid), exist_ok=True)
        with open(latest, "w") as f:
            f.write(rid + "\n")
        with open(ledger.state_path(self.ws, RUNS, rid, "state.json"), "w") as f:
            json.dump({"prompt_sha": digest, "done": {}, "finished": False}, f)
        return rid, False

    def _load(self):
        return json.load(open(os.path.join(self.rd, "state.json")))

    def _save(self):
        tmp = os.path.join(self.rd, "state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, os.path.join(self.rd, "state.json"))

    def step(self, name, fn):
        if name in self.state["done"]:
            return self.state["done"][name]
        value = fn()
        self.state["done"][name] = value
        self._save()
        return value

    def p(self, name):
        return os.path.join(self.rd, name)

    def r(self, name):
        return ledger.read(self.rd, name)

    def w(self, name, text):
        ledger.write(self.rd, name, text)

    # --- the Check -------------------------------------------------------------------

    def _run_cmd(self, cmd, cwd, log):
        try:
            r = subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True,
                               timeout=self.o.verify_timeout)
            log.write((r.stdout or "") + (r.stderr or ""))
            return r.returncode
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else ""
            log.write(f"{out}\n<the Check timed out after {self.o.verify_timeout}s>\n")
            return 124

    def check(self, log_name, cwd=None):
        """The Check as task.md names it, into <run>/<log_name>. Returns "0", "1" or "none"
        (nothing to run, or nothing a broken Check says counts: the judge decides)."""
        kind, cmd = self.spec
        path = self.p(log_name)
        if kind == "command":
            with open(path, "w") as log:
                log.write(f"check: {cmd}\n")
                log.flush()
                rc = self._run_cmd(cmd, cwd or self.ws, log)
            if rc == 0:
                return STATUS_PASS
            return self._against_baseline(path)
        if kind == "samples":
            with open(path, "w") as log:
                log.write("check: samples\n")
                fails = samples.check(cwd or self.ws, self.o.python, log)
            return STATUS_PASS if fails == 0 else STATUS_FAIL
        with open(path, "w") as log:
            log.write("check: nothing to run, the judge decides\n")
        return STATUS_NONE

    def _against_baseline(self, path):
        """A Check that already failed on the tree as found counts only the failing tests the
        work adds (memdoor check.sh + failures.sh)."""
        if self.r("baseline-status.txt").strip() != STATUS_FAIL:
            return STATUS_FAIL
        before = failures.parse(self.r("baseline-check.log"))
        after = failures.parse(open(path, errors="replace").read())
        with open(path, "a") as log:
            if before is not None and after is not None:
                added = sorted(set(after) - set(before))
                if not added:
                    log.write("\nthe Check fails on the tree as the run found it too (baseline-check.log), and "
                              "this change adds no failing test to it: it passes\n")
                    return STATUS_PASS
                log.write("\nthe tests this change makes fail (they pass on the tree as the run found it, "
                          "baseline-check.log):\n" + "\n".join(added) + "\n")
                return STATUS_FAIL
            # memdoor stops here ("nothing it says counts, the judge decides") for any log it cannot
            # parse -- so a bug fix whose test script fails before the fix (the normal case) would
            # have its Check turned off on every failing attempt, and its judge never sees the log.
            # Only a failure that reads exactly as it did before any change says nothing about the
            # work (a broken environment); any other failure is the work's.
            if _shape(open(path, errors="replace").read()) == _shape(self.r("baseline-check.log")):
                log.write("\nthe Check fails exactly as it did on the tree as the run found it "
                          "(baseline-check.log): it says nothing about this change, the judge decides with "
                          "this log\n")
                return STATUS_NONE
            log.write("\nthe Check failed on the tree as the run found it too (baseline-check.log), but it "
                      "fails differently now: counted as a failure of this change\n")
        return STATUS_FAIL

    def measure(self, log_name):
        """changes.diff, the Check and the Scope report on the tree as it is now."""
        self.w("changes.diff", self.tree.changes())
        status = self.check(log_name)
        report, outside = tasklib.scope_report(self.r("task.md"), self.tree)
        self.w("scope.txt", report)
        with open(self.p(log_name), "a") as f:
            f.write("\n" + report)
        return {"status": status, "outside": outside, "changed": bool(self.r("changes.diff").strip()),
                "tree": self.tree.snapshot()}

    # --- steps --------------------------------------------------------------------------

    def problem(self):
        self.w("prompt.md", self.prompt)
        self.w("task.md", tasklib.make_task(self.prompt))
        base = self.tree.start()
        return {"base": base, "git": "repository" if self.tree.real else "shadow"}

    def direct(self):
        ok = self.codex.run("direct", prompts.direct_prompt(self.prompt), self.o.direct_timeout)
        return {"ok": ok}

    def outline(self):
        files = self.tree.git("ls-files", "--cached", "--others", "--exclude-standard", "--", ".",
                              *gitstate.EXCLUDES, check=False).split("\n")
        parts = ["# The prompt\n", self.prompt, "\n# The files of the project\n", "```",
                 "\n".join([f for f in files if f][:600]), "```"]
        if self.tree.real:
            last = self.tree.git("log", "-1", "--stat", "--format=%h %s%n%n%b", check=False)
            if last.strip():
                show = self.tree.git("show", "--format=", "HEAD", check=False)
                parts += ["\n# The last commit\n", "```", "\n".join(last.split("\n")[:60]), "```", "```diff",
                          show[:15000], "```"]
        for f in OUTLINE_FILES:
            fp = os.path.join(self.ws, f)
            if os.path.isfile(fp):
                parts += [f"\n# {f} (the start)\n", "```", open(fp, errors="replace").read()[:6000], "```"]
        tests = os.path.join(self.ws, "tests")
        if os.path.isdir(tests) and any(n.endswith(".in") for n in os.listdir(tests)):
            parts += ["\n# Public samples\n", "tests/N.in with expected tests/N.out are in this folder."]
        self.w("outline.md", "\n".join(parts) + "\n")
        return {"bytes": len(self.r("outline.md"))}

    def plan(self):
        reply = self.calls.chat("plan", [
            {"role": "system", "content": prompts.no_tools_system(prompts.PLAN_FIRST)},
            {"role": "user", "content": prompts.plan_user(self.r("outline.md"), self.o.until, self.o.verify_cmd)},
        ], PLAN_TEMPERATURE).text
        self.w("plan.md", reply.strip() + "\n")
        return {"chars": len(reply)}

    def settle(self):
        text, filled = tasklib.settle(self.r("plan.md"), self.r("task.md"), self.prompt, self.ws,
                                      self.o.until, self.o.verify_cmd)
        self.w("task.md", text)
        if not os.path.exists(self.p("notes.md")):
            self.w("notes.md", f"# Notes, run {self.run_id}\n\n## Solver decision\n\n## Review\n\n## Repair\n")
        kind, cmd = tasklib.check_spec(text, self.ws)
        self.log(f"  [settle] Check: {kind}{' `' + cmd + '`' if cmd else ''}"
                 + (f"; filled safely: {', '.join(filled)}" if filled else ""))
        return {"filled": filled, "kind": kind, "cmd": cmd}

    def baseline(self):
        """The Check on a copy of the tree as the run found it: what already fails there is
        not the work's (memdoor baseline.yaml)."""
        tmp = tempfile.mkdtemp(prefix="team-solve-baseline-")
        try:
            cwd = self.tree.baseline_copy(tmp)
            status = self.check("baseline-check.log", cwd=cwd)
        except (gitstate.GitError, OSError) as e:
            self.log(f"  [baseline] no clean copy ({e}); none taken")
            return {"status": None}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        # A baseline run is judged on its own exit, never against itself.
        if status == STATUS_NONE:
            status = STATUS_FAIL
        self.w("baseline-status.txt", status + "\n")
        self.log(f"  [baseline] the Check on the tree as found: "
                 f"{'passes' if status == STATUS_PASS else 'FAILS before any change'}")
        return {"status": status}

    def test0(self):
        m = self.measure("direct-check.log")
        # The Check decides, not the Scope: files outside it are the review's to judge. A
        # command that cannot tell (it fails as it did before any change) is no pass.
        passed = m["changed"] and (m["status"] == STATUS_PASS or
                                   (m["status"] == STATUS_NONE and self.spec[0] == "judge"))
        self.w("direct-status.txt", ("0" if passed else "1") + "\n")
        self.tree.save("direct")
        self.log(f"  [test-0] direct attempt {'passes' if passed else 'does not pass: the team takes over'} "
                 f"(check {m['status']}, {m['outside']} outside the Scope, "
                 f"{len(self.r('changes.diff').splitlines())} diff lines)")
        return dict(m, passed=passed)

    def context(self):
        task = self.r("task.md")
        parts = ["# task.md\n", task, "\n# plan.md\n", self.r("plan.md")]
        for s in tasklib.scope_entries(task):
            fp = os.path.join(self.ws, s)
            if os.path.isfile(fp):
                parts += [f"\n# {s}\n", "```", open(fp, errors="replace").read()[:40000], "```"]
            elif os.path.isdir(fp):
                listing = self.tree.git("ls-files", "--cached", "--others", "--exclude-standard", "--", s,
                                        *gitstate.EXCLUDES, check=False).split("\n")
                parts += [f"\n# {s} (the files in it)\n", "\n".join([f for f in listing if f][:300])]
                if s == "." and os.path.isfile(os.path.join(self.ws, "README.md")):
                    parts += ["\n# README.md\n", open(os.path.join(self.ws, "README.md"), errors="replace").read()[:20000]]
            else:
                parts.append(f"\n# {s}: not there yet (a file the work makes)\n")
        parts += ["\n# The first attempt (a unified diff)\n", "```diff", cap(self.r("changes.diff"), MAX_DIFF_CHARS // 2),
                  "```", "\n# What the Check and the Scope said of it\n", "```",
                  cap(self.r("direct-check.log"), MAX_LOG_CHARS), "```"]
        self.w("context.md", "\n".join(parts) + "\n")
        return {"bytes": len(self.r("context.md"))}

    def ideate(self):
        ctx = self.r("context.md")
        sys_msg = prompts.no_tools_system("the proposal's first line")
        replies = self.calls.chat_many([
            (f"ideate-{x}", [{"role": "system", "content": sys_msg},
                             {"role": "user", "content": prompts.ideate_user_agents(ctx, x)}], IDEATE_TEMPERATURE)
            for x in ("a", "b")])
        for x, rep in zip(("a", "b"), replies):
            self.w(f"proposal-{x}.md", rep.text.strip() + "\n")
        return {"a": len(replies[0].text), "b": len(replies[1].text)}

    def solve(self):
        def target():
            if self.tree.changes().strip():
                return True, ""
            return False, "nothing in the project changed yet: do the task as task.md's Deliverable says"
        ok = self.codex.run("solve", prompts.solve_prompt(self.rel), self.o.work_timeout, target)
        if not ok:
            raise agents.RoleError("agent step 'solve' failed")
        return {"ok": True}

    def review(self, round_no, m):
        status = m["status"]
        self.w("test-status.txt", status + "\n")
        log_text = self.r(f"test-{round_no}.log")
        shutil.copyfile(self.p(f"test-{round_no}.log"), self.p("test-output.log"))
        reply = self.calls.chat(f"review-{round_no}", [
            {"role": "system", "content": prompts.no_tools_system(prompts.VERDICT_FIRST)},
            {"role": "user", "content": prompts.review_user(self.r("task.md"), cap(self.r("changes.diff"), MAX_DIFF_CHARS),
                                                            cap(log_text, MAX_LOG_CHARS), status, round_no)},
        ], TEMPERATURE).text
        self.w(f"review-{round_no}.md", reply.strip() + "\n")
        shutil.copyfile(self.p(f"review-{round_no}.md"), self.p("review.md"))
        v = verdict_of(reply)
        with open(self.p("notes.md"), "a") as f:
            f.write(f"\n### Review, round {round_no}: {v} (check {status})\n\n{reply.strip()[:4000]}\n")
        return v

    def judge(self, round_no, status):
        # The judge sees the Check's log only when a command ran and could not decide.
        log_text = cap(self.r(f"test-{round_no}.log"), MAX_LOG_CHARS) \
            if self.spec[0] == "command" and status == STATUS_NONE else None
        reply = self.calls.chat(f"judge-{round_no}", [
            {"role": "system", "content": prompts.no_tools_system(prompts.VERDICT_FIRST)},
            {"role": "user", "content": prompts.judge_user(self.r("task.md"), cap(self.r("changes.diff"), MAX_DIFF_CHARS),
                                                           log_text)},
        ], TEMPERATURE).text
        self.w(f"verdict-{round_no}.md", reply.strip() + "\n")
        shutil.copyfile(self.p(f"verdict-{round_no}.md"), self.p("verdict.md"))
        return verdict_of(reply)

    def repair(self, round_no, judged):
        if os.path.exists(self.p("repair.md")):
            os.unlink(self.p("repair.md"))

        def target():
            if self.r("repair.md").strip():
                return True, ""
            return False, f"{self.rel}/repair.md is missing: what you changed and what the Check printed"
        ok = self.codex.run(f"repair-{round_no}", prompts.repair_prompt(self.rel, round_no, judged),
                            self.o.work_timeout, target)
        if not ok:
            raise agents.RoleError(f"agent step 'repair-{round_no}' failed")
        shutil.copyfile(self.p("repair.md"), self.p(f"repair-{round_no}.md"))
        return {"ok": True}

    # --- a round ---------------------------------------------------------------------

    def round(self, r, repaired):
        m = self.step(f"test-{r}", lambda: self.measure(f"test-{r}.log"))
        rev = self.step(f"review-{r}", lambda: self.review(r, m))
        kind = self.spec[0]
        if m["status"] == STATUS_FAIL and rev == "PASS":
            rev = "FAIL"
            self.log(f"  [review-{r}] said PASS on a failing Check: counted as FAIL")
        judged = "skipped"
        if m["status"] == STATUS_FAIL or rev == "FAIL" or not m["changed"]:
            accepted = False
        elif kind == "samples":
            accepted = m["status"] == STATUS_PASS
        elif kind == "command" and m["status"] == STATUS_PASS and rev == "PASS" and not repaired:
            accepted = True
        else:
            judged = self.step(f"judge-{r}", lambda: self.judge(r, m["status"]))
            if judged == "PASS":
                accepted = True
            elif judged == "FAIL":
                accepted = False
            else:
                # A judge that answered nothing leaves the Check and the review to decide.
                accepted = m["status"] == STATUS_PASS and rev != "FAIL"
        entry = {"round": r, "check": m["status"], "outside_scope": m["outside"], "review": rev,
                 "judge": judged, "accepted": accepted, "tree": m["tree"]}
        self.history.append(entry)
        self.log(f"  [round {r}] check {m['status']}  review {rev}  judge {judged}  -> "
                 f"{'ACCEPTED' if accepted else 'not accepted'}")
        return entry

    # --- the run -----------------------------------------------------------------------

    def run(self):
        self.step("problem", self.problem)
        direct_box = {}

        def go_direct():
            try:
                direct_box["v"] = self.step("direct", self.direct)
            except Exception as e:  # noqa: BLE001 -- recorded; the team takes over from nothing
                direct_box["error"] = str(e)
        t = threading.Thread(target=go_direct)
        t.start()
        try:
            self.step("outline", self.outline)
            self.step("plan", self.plan)
            st = self.step("settle", self.settle)
            self.spec = (st["kind"], st["cmd"])
            if self.spec[0] == "command":
                self.step("baseline", self.baseline)
        finally:
            t.join()
        if direct_box.get("error"):
            self.log(f"  [direct] could not run: {direct_box['error']}")
        elif not (direct_box.get("v") or {}).get("ok"):
            self.log("  [direct] the turn failed; the team takes over from what it left")

        t0 = self.step("test-0", self.test0)
        if not t0["passed"]:
            self.step("context", self.context)
            self.step("ideate", self.ideate)
            self.step("solve", self.solve)

        accepted, stopped, repaired, last = False, None, False, None
        for r in range(1, self.o.max_iters + 1):
            last = self.round(r, repaired)
            if last["accepted"]:
                accepted = True
                break
            if r == self.o.max_iters:
                stopped = "max_iters"
                break
            judged = last["judge"] == "FAIL"
            self.step(f"repair-{r}", lambda: self.repair(r, judged))
            repaired = True
            if self.tree.snapshot() == last["tree"]:
                # The next round would measure the same tree: the same Check, and a review
                # that already failed it.
                self.log(f"  [repair-{r}] changed nothing; no progress, stopping")
                stopped = "no_progress"
                break

        keep = self.step("keep", lambda: self.keep(accepted, t0, last))
        final = self.measure("final-check.log")
        solved = accepted and keep["kept"] == "final" and final["status"] != STATUS_FAIL
        if accepted and final["status"] == STATUS_FAIL:
            self.log("  [final] the Check fails on the kept tree now (it passed in the round): NOT solved")
        self.state["finished"] = True
        self._save()
        return {"run_id": self.run_id, "run_dir": self.rel, "resumed": self.resumed,
                "check_kind": self.spec[0], "check_cmd": self.spec[1], "accepted": accepted, "solved": solved,
                "stopped": stopped, "rounds": len(self.history), "history": self.history,
                "direct_passed": t0["passed"], "keep": keep, "final_check": final["status"],
                "outside_scope": final["outside"], "usage": self.codex.usage,
                "calls": self.calls.n, "live_calls": self.calls.live, "replayed_calls": self.calls.replayed,
                "tokens": self.calls.tokens}

    def keep(self, accepted, t0, last):
        """THE NET (memdoor keep.yaml): never end worse proven than the single call."""
        if accepted:
            note = "kept: the work the last round accepted"
            kept = "final"
        elif t0["passed"] and not self._repaired():
            note, kept = "kept: the direct attempt, untouched (not accepted)", "final"
        else:
            final_check = last["check"] if last else None
            direct_check = t0["status"]
            kind = self.spec[0]
            if kind != "judge" and final_check == STATUS_PASS:
                note, kept = f"kept: the team's work (its Check passes; the direct attempt's was {direct_check})", "final"
            elif kind == "judge":
                note, kept = ("kept: the team's latest work (nothing can be run, so neither is proven; the direct "
                              f"attempt is in {self.rel}/direct.patch)"), "final"
            else:
                self.tree.save("team")
                self.tree.restore("direct")
                note, kept = (f"put back: the direct attempt (the team's work did not prove better: check "
                              f"{final_check}; the team's work is in {self.rel}/team.patch)"), "direct"
        self.w("keep.txt", note + "\n")
        self.log(f"  [keep] {note}")
        return {"kept": kept, "note": note}

    def _repaired(self):
        return any(k.startswith("repair-") for k in self.state["done"])
