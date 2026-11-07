"""Every prompt the team sends, in one place.

Loop-mode strings are the authors' own (GVS5H codebase/v2-current/escalation/multiagent.py
and orchestrator.py at b4cc2f7, MIT). They are copied verbatim on purpose:
tests/test_team_solve_parity.py runs the authors' code and ours against one scripted
model and fails on a single changed byte. Edit them only together with that test.

Agent-mode prompts are memdoor's workflows/team-solve/steps/*.yaml at 47dad8b (Apache-2.0),
with memdoor's run folder and tools replaced by this skill's (.team-solve/runs/<id>/, codex
exec roles), and the review/repair pair made a loop of rounds instead of one repair.
"""

# --- task specs (orchestrator.py) ---------------------------------------------

CODE_SOLVER = (
    "You are an elite competitive programmer. Solve the given problem in Python. "
    "Think carefully about algorithmic complexity and edge cases. "
    "Output EXACTLY ONE complete, self-contained Python program inside a single "
    "```python ...``` fenced block, and nothing else after it."
)

# Generic single-file solver for loop mode under a custom --verify-cmd: no contest framing,
# the verification command is the spec. Still one fenced program, because loop-mode workers
# are tool-less and emit text; multi-file or non-Python work belongs in agents mode.
GENERIC_SOLVER = (
    "You are an expert programmer. Write a single, complete, self-contained Python program "
    "that accomplishes the task and makes the stated verification command pass. "
    "Output EXACTLY ONE complete Python program inside a single ```python ...``` fenced block, "
    "and nothing else after it."
)

SPECS = {
    "code": {"kind": "code", "solver_system": CODE_SOLVER},
    "generic": {"kind": "code", "solver_system": GENERIC_SOLVER},
    "aime": {"kind": "math", "solver_system": (
        "You are an elite mathematician solving an AIME problem. The answer is an integer "
        "from 0 to 999. Reason step by step, then on the FINAL line output exactly "
        "'ANSWER: <integer>'."
    )},
    "math500": {"kind": "math", "solver_system": (
        "You are an elite mathematician. Solve the problem. Reason step by step, then on the FINAL "
        "line output exactly 'ANSWER: <final answer>' with the answer in simplest form (a number or "
        "simplified expression, written as it would appear in a textbook)."
    )},
    "gpqa": {"kind": "math", "solver_system": (
        "You are a PhD-level expert in physics, chemistry, and biology answering a multiple-choice "
        "question with options A, B, C, D. Reason carefully, then on the FINAL line output exactly "
        "'ANSWER: <letter>' where <letter> is the single correct option (A, B, C, or D)."
    )},
    "hle": {"kind": "math", "solver_system": (
        "You are answering a question from Humanity's Last Exam: an extremely difficult, "
        "expert-level question that may come from any academic discipline. It may be "
        "multiple-choice or require an exact free-form answer (a number, expression, name, or "
        "short phrase). If it is multiple-choice, answer with the option LETTER only -- note "
        "there may be more than four options. Reason carefully, then finish with exactly "
        "these two lines:\n"
        "ANSWER: <your succinct, exact final answer -- no explanation, no units unless asked>\n"
        "CONFIDENCE: <integer 0-100>"
    )},
}

# run_bench.py's LiveCodeBench framing, for `--wrap-lcb` on a bare statement.
FMT_STARTER = ("You will use the following starter code to write the solution to the "
               "problem and enclose your code within delimiters.")
FMT_STDIN = ("Read the inputs from stdin solve the problem and write the answer to stdout "
             "(do not directly test on the sample inputs). Enclose your code within "
             "delimiters as follows. Ensure that when the python program runs, it reads "
             "the inputs, runs the algorithm and writes output to STDOUT.")


def lcb_prompt(question, starter_code=""):
    p = f"### Question\n{question}\n\n"
    if starter_code:
        p += f"### Format: {FMT_STARTER}\n"
        p += f"```python\n{starter_code}\n```\n\n"
    else:
        p += f"### Format: {FMT_STDIN}\n\n"
    return p


# --- loop roles (multiagent.py) -----------------------------------------------

def format_rule(first, *rest):
    others = "".join(f", then the literal line '### {r}'" for r in rest)
    return ("\n\nTHE FORMAT IS MANDATORY AND YOUR REPLY IS PARSED BY A PROGRAM. "
            f"Begin your reply with the literal line '### {first}'{others}. "
            "Write nothing before the first header and nothing outside these sections. "
            "Do NOT solve the problem and do NOT write code -- a later worker does that. "
            "A reply without these exact header lines is discarded.")


def plan_system(kind, strict):
    s = (
        "You are the PRIMARY orchestrator (manager) of a small team of workers, all "
        f"expert at {'competitive programming' if kind == 'code' else 'olympiad mathematics'}. "
        "Given a problem, produce a short overarching plan to solve it, then a task list "
        "the workers can pick up. Respond with EXACTLY these sections:\n"
        "### PLAN\n<3-6 sentence strategy>\n"
        "### TASKS\n<3-6 bullet tasks, each a concrete unit of work>"
    )
    return s + format_rule("PLAN", "TASKS") if strict else s


def ideate_system(strict):
    s = (
        "You are the FIRST WORKER. Do NOT solve the problem and do NOT write any code. "
        "Just think about it: identify the core difficulty, then list SEVERAL DISTINCT "
        "candidate approaches (genuinely different algorithms / data structures / problem "
        "reductions, not variations of one idea), and note pitfalls for each. Describe each "
        "approach in prose only -- absolutely no code blocks; a later worker will implement. "
        "Respond with EXACTLY:\n### NOTES\n<your analysis>\n"
        "### NEXT\n<bullet list of distinct approaches to try next>"
    )
    return s + format_rule("NOTES", "NEXT") if strict else s


def ideate_user(problem, plan):
    return f"PROBLEM:\n{problem}\n\nPLAN:\n{plan}"


MANAGE_SYSTEM = (
    "You are the PRIMARY orchestrator and manager. You OWN the task list and decide "
    "when the problem is solved. Review the current progress and the latest worker's "
    "result, then:\n"
    "- The LATEST WORKER RESULT may include a SAMPLE TESTS verdict from actually running "
    "the code. Treat it as ground truth: only set STATUS 'done' if the solution PASSED the "
    "sample tests; if it FAILED, you MUST set STATUS 'continue' and choose a task that "
    "fixes the failing case or switches to a different approach.\n"
    "- If the current solution/answer is complete and correct, set STATUS to 'done'.\n"
    "- Otherwise CURATE the task list: merge duplicates, drop finished or irrelevant "
    "items, mark completed ones [done], and fold in ONLY genuinely new sub-tasks from "
    "the proposals. Then choose the single most valuable next task.\n"
    "- IMPORTANT: if the current solution keeps failing, or the last worker made no real "
    "progress, do NOT keep refining the same idea. Switch to a DIFFERENT approach "
    "(a different algorithm / data structure / reduction) from the notes, or ask for a "
    "new one. You have many rounds -- use them to try distinct approaches, not to polish "
    "a stuck one.\n"
    "Respond with EXACTLY these sections:\n"
    "### STATUS\n<done|continue>\n"
    "### NEXT\n<exact text of the ONE task to do next; omit if done>\n"
    "### TASKS\n<curated list, one per line, each '- [done] ...' or '- [todo] ...'>"
)


def manage_system(strict):
    return MANAGE_SYSTEM + format_rule("STATUS", "NEXT", "TASKS") if strict else MANAGE_SYSTEM


def manage_user(problem, kind, cur, notes, task_lines, last_summary, prop_lines):
    return (
        f"PROBLEM:\n{problem}\n\nCURRENT {'SOLUTION' if kind == 'code' else 'ANSWER'}:\n"
        f"{cur or '(none yet)'}\n\nNOTES:\n{notes}\n\n"
        f"CURRENT TASK LIST:\n{task_lines}\n\nLATEST WORKER RESULT: {last_summary}\n\n"
        f"PROPOSED NEW STEPS:\n{prop_lines}"
    )


CUTOFF_SYSTEM = (
    "A worker's solution attempt was CUT OFF when it hit the token limit. Summarize its "
    "partial attempt in 3-5 sentences: which approach it was pursuing, what it established "
    "or ruled out, how far it got, and what remained unfinished. Be concrete so another "
    "worker can resume or judge it. Do NOT try to finish the solution yourself."
)

NOTES_FMT = (
    "### NOTES\n<the COMPLETE notes file, rewritten. You are shown the current NOTES "
    "above: fold your findings into them, keep what still matters, and DELETE anything "
    "superseded, disproven, or now obvious. This REPLACES the file, so whatever you omit "
    "is gone. Organise it as '- **Topic:** ...' bullets, under ~800 words. Do NOT use "
    "markdown headings (#, ##), bold-only lines, or ALLCAPS: lines anywhere inside this "
    "section -- the reply is split on those, so they would truncate your notes.>\n"
)


def worker_system(kind, solver_system):
    if kind == "code":
        out_fmt = (
            "### CODE\n```python\n<the FULL updated self-contained program>\n```\n"
            + NOTES_FMT +
            "### NEXT\n<bullet list of remaining steps, or 'none'>\n### STATUS\n<solved|continue>"
        )
    else:
        out_fmt = (
            "### ANSWER\n<full worked solution, ending with the exact final "
            "ANSWER: line your instructions require>\n"
            + NOTES_FMT +
            "### NEXT\n<bullet list of remaining checks, or 'none'>\n### STATUS\n<solved|continue>"
        )
    return (
        f"You are a WORKER subagent, {solver_system} "
        "You share a workspace with the team. Build on the current work and notes where "
        "useful -- but if your task is to try a different approach, write a FRESH solution "
        "for that approach instead of patching the stuck one. "
        f"Respond with EXACTLY these sections:\n{out_fmt}"
    )


FINALIZE_GOAL = "Produce the DEFINITIVE final solution now, using all notes and current work."


def worker_user(problem, plan, notes, cur, goal):
    return (
        f"PROBLEM:\n{problem}\n\nPLAN:\n{plan}\n\n"
        f"NOTES:\n{notes}\n\nCURRENT WORK:\n{cur or '(none yet)'}\n\n"
        f"YOUR TASK: {goal}"
    )


# --- stress guard (ours; not in the authors' loop) ------------------------------

STRESS_SYSTEM = (
    "You are a test engineer for a competitive-programming team. Do NOT solve the problem "
    "efficiently. Write two small, independent Python programs:\n"
    "1. BRUTE: an obviously correct brute force that reads the input from stdin exactly as the "
    "problem specifies and prints the answer exactly as the problem specifies. Use exhaustive "
    "search or direct simulation; speed does not matter, correctness does. It only has to "
    "handle tiny inputs.\n"
    "2. GEN: a random generator of TINY valid inputs. It takes one integer seed as its first "
    "command-line argument (sys.argv[1]), seeds random with it, and prints one complete valid "
    "input in the problem's exact format. Keep every size small enough that BRUTE finishes in "
    "well under a second, cover edge cases (minimum sizes, equal values, extremes of the "
    "small range), and respect EVERY constraint and guarantee in the statement.\n"
    "Respond with EXACTLY these sections:\n"
    "### BRUTE\n```python\n<program>\n```\n"
    "### GEN\n```python\n<program>\n```"
)


def stress_user(problem, feedback=""):
    u = f"PROBLEM:\n{problem}"
    if feedback:
        u += f"\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED: {feedback}\nFix it and answer again in the same format."
    return u




# --- agent mode: a task from a prompt (memdoor workflows/team-solve/steps/*.yaml) --------
#
# The planner, the ideators, the reviewer and the judge are one router call each with no
# tools: they read what the harness gathered and answer. Only direct, solve and repair are
# Codex agent turns that change the tree. {rd} is the run folder relative to the project.

def no_tools_system(first_line):
    return ("You answer in this one message. You have no tools: you cannot open a file, list a "
            "directory or run a command, and nothing you write will be run. Everything you need is in "
            f"the message. Do not say what you will look at: answer. Start your answer with the line {first_line}.")


PLAN_FIRST = "## Deliverable"
VERDICT_FIRST = "verdict: PASS or verdict: FAIL"


def plan_user(outline, until=None, verify_cmd=None):
    given = ""
    if until:
        given += ("\n\nThe person gave the end condition at the end of the prompt. It is the first line of "
                  "Done when, word for word; the run is done only when it holds.")
    if verify_cmd:
        given += (f"\n\nThe person fixed the Check: `{verify_cmd}`. Write it as the Check, in backticks, "
                  "unchanged.")
    return f"""{outline}

You are the planner of a team that does the task in the prompt above, in this project. Settle what the work is checked against. Answer with exactly these five sections, in this order, and nothing before them:

## Deliverable
"changes to this tree" when the task changes the project; or the new files by name (a program: solution.py, Python standard library only, stdin in, the answer alone on stdout; a document: answer.md).

## Check
One command in backticks that exits 0 when the work is right: the one the prompt names when it names one, else the project's own (`go test ./pkg/x/`, `npm test`, `make check`), scoped to what the task touches. The word samples when tests/N.in and tests/N.out are in the files above. The word judge when nothing can be run. A command must be one that can pass on a correct result in this project as it is: never a command that needs a network service, a credential or a tool the files above do not show.

## Scope
Each file or directory the work may touch, in backticks, one per line (`scripts/scan.sh`, `docs/`). What the prompt allows is the Scope: when it says "files under cmd/cli/cmd/ only", the Scope is `cmd/cli/cmd/`. A new file whose name the prompt does not give (a test to add, a page to write) is covered by its directory, never by a name you guess. `.` only when the task is the whole tree.

## Done when
What the reviewer and the judge check, one line each, each a thing the prompt asks for, in the prompt's terms. Nothing the prompt does not ask: not the project's conventions (a changelog entry, docs, a release note), not a test of the shape you would write, not a wording the prompt does not give. A line you infer because the prompt is unclear starts with `(inferred)`.

## Approach
The decomposition, and two or three candidate approaches with their tradeoffs, for the solver if the first attempt fails.{given}"""


def ideate_user_agents(context, which):
    if which == "a":
        mine = "the most direct approach you can argue meets every line of Done when."
    else:
        mine = ("an alternative from a different family than the obvious approach (a program: if the obvious "
                "one is dynamic programming, try greedy, math or a data structure; a change to a codebase: a "
                "different place to cut, a different sequence of steps; a document: a different structure or "
                "angle), with its tradeoffs against the obvious one.")
    return f"""{context}

You are independent ideator {which.upper()} on the task above: task.md is the task, plan.md the planner's decomposition, and the files after them are the ones the Scope names, as they are now. The first attempt, its diff and what the Check said of it close the message. Another ideator writes a proposal at the same time; a solver then does the work from the two.

Your proposal: {mine}

For a program: pseudocode, the invariant, correctness, complexity, edge cases. For a change to a codebase: the files and the edits in order (the lines as they are now and as they should be), what could break and how the Check will show it. For a document: the outline, the claims and what backs each. Stay within the Scope and change nothing the task did not ask for.

Answer with the proposal alone, in markdown, five lines at least."""


def review_user(task, diff, check_log, status, round_no):
    return f"""# The task

{task}

# What the run changed (a unified diff; new files in full)

```diff
{diff}
```

# What the Check printed, and the Scope report

```
{check_log}
```

Check status: {status} (0 passed, 1 failed, none: nothing could be run). Review round {round_no}.

You are the reviewer. Judge the change above against the task's Done when, from this evidence alone. The verdict is on the work, not on what it says of itself: a review or an audit the task asked for passes when its findings are true and grounded.

The task's Prompt is what the person asked; Done when is the planner's reading of it. Fail only on what the Prompt asks for. A line of Done when that starts with (inferred), or that asks for what the Prompt does not (a changelog entry, docs, a test of a given shape, a wording the Prompt does not give), is a note in the findings, never a reason to FAIL. An end condition the person wrote into the Prompt is binding: FAIL while it does not hold.

FAIL when the check status is 1, when the change misses something the Prompt asks for, changes behaviour the task did not ask to change, removes content the task did not ask to remove (the report counts the lines each file lost), or changes a file the report says is OUTSIDE THE SCOPE that the task does not need (an instruction file such as AGENTS.md, an unrelated module). A change outside the Scope that the fix needs (the same bug in a sibling file, a test fixture) is not a reason to fail: the planner's Scope is a guess. With a status of none, judge the change against the task yourself. Otherwise PASS.

Answer with the verdict on the first line, exactly `verdict: PASS` or `verdict: FAIL`, then the findings, one per line. On FAIL, end with the repair instructions for the worker who fixes it: what to change, in which file, and why."""


def judge_user(task, diff, check_log=None):
    inconclusive = "" if check_log is None else f"""
# What the Check printed

The Check is a command, and it fails on the tree as the run found it too, the same way, so it could not decide. Read its output yourself: a change that should make it pass and does not is a FAIL.

```
{check_log}
```
"""
    return f"""# The task

{task}

# The change (a unified diff; new files in full)

```diff
{diff}
```
{inconclusive}
You are the judge, in a fresh context: you have seen none of the team's reasoning. Decide whether the change does what the task's Prompt asks, completely and correctly, within its Scope, and nothing the task did not ask. An end condition the person wrote into the Prompt is binding: PASS only when the change makes it hold. Done when is the planner's reading of the Prompt: a line of it that starts with (inferred), or that asks for what the Prompt does not (a changelog entry, docs, a test of a given shape, a wording the Prompt does not give), is not a reason to FAIL. The verdict is on the work, not on what it reports: a review that finds a bug passes when the bug is real. A change that removes content the task did not ask to remove fails.

Answer with the verdict on the first line, exactly `verdict: PASS` or `verdict: FAIL`, then at most ten lines saying why, each tied to a line of the task."""


AGENT_RULES = ("Do not commit, check out, reset or stash: the tree's HEAD stays where it is. Do not write a "
               "rule or lesson into AGENTS.md or any other instruction file unless the task asks for it. Leave "
               ".team-solve/ alone except for the run files named here. Do not spawn agents or rely on chat "
               "history.")


def direct_prompt(prompt):
    return (f"{prompt.rstrip()}\n\n{AGENT_RULES} Write what the task asks for as soon as you have a first "
            "version, then improve it: a turn that ends without it has done nothing.")


def solve_prompt(rd):
    return (f"You are the solver in a fresh context. The run's files are in {rd}/: task.md (the task: the "
            "prompt and what the planner settled), plan.md, notes.md (the ledger every role writes to). They "
            f"are never part of the work and never committed. {AGENT_RULES}\n"
            "A first attempt at the task is already in the tree, and it did not pass: changes.diff is what it "
            "changed, direct-check.log what the Check and the Scope said of it. Keep what is right in it, "
            "correct what is wrong, undo what the task did not ask for.\n"
            "Read task.md, plan.md, notes.md, proposal-a.md and proposal-b.md there. Select and justify an "
            "approach in the Solver decision section of notes.md. Then do the task as task.md's Deliverable "
            "says: change the project's files within the Scope, or write the new files it names, complete. "
            "Write a first version of the deliverable early and improve it after: a turn that ends without it "
            "has done nothing. Touch nothing outside the Scope and change nothing the task did not ask for. "
            "Run the Check (the command, or a sample) before you finish and fix what it shows. Do not change "
            "task.md, the prompt or test data. The steps after you check the work and own any repair round.")


def repair_prompt(rd, round_no, judged):
    why = "review.md" + (" and verdict.md (the judge's FAIL)" if judged else "")
    return (f"You are the repair worker for round {round_no}, in a fresh context. The run's files are in {rd}/: "
            "task.md (the task: the prompt and what the planner settled), plan.md, notes.md (the ledger every "
            f"role writes to). They are never part of the work and never committed. {AGENT_RULES}\n"
            f"The work in the tree did not pass. Read {why}, test-status.txt, test-output.log, changes.diff and "
            "notes.md there, then task.md and plan.md, and correct the work using the instructions, as the task "
            "needs. Undo a change outside the Scope only when the review names it as one the task does not "
            "need; a change the fix needs stays, wherever it is. Run the Check to verify the correction. "
            f"Write {rd}/repair.md: what you changed and what the Check printed, and append a short repair note "
            "for this round to notes.md. Do not change task.md, the prompt, test inputs or expected outputs. "
            "The next round's check, review and judge decide the run.")


def nudge(missing):
    return (f"Not done yet: {missing}. Finish it now in this turn, then stop.")
