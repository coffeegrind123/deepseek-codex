---
name: team-solve
description: "Solve any task whose success can be checked by a command, with a team of fresh-context roles that talk only through files, the GVS5H method: a planner, an ideator, a manager that owns the task list, workers, and a cut-off summarizer, up to 10 rounds, with a check (not a model) as ground truth each round. The check is either the built-in public-sample runner (contest problems) or a command you supply with --verify-cmd (make a test suite pass, make a build compile, satisfy a grader, hit a benchmark, reproduce an output); exit 0 means solved. Two modes: loop (default; one model call per role through the workspace router, byte-identical to the authors' prompts, one Python file as the artifact) and agents (memdoor's eight steps, each role a fresh `codex exec` agent that edits the whole workspace). Optional guards: --keep-best and --stress (a brute-force cross-check, samples only)."
when_to_use: "Use when the user hands over a self-contained task with a clear success check and wants it driven to pass: an algorithmic/contest problem with sample tests ('solve this AtCoder/Codeforces/LeetCode problem', 'team-solve this'), or any task with a checkable command via --verify-cmd ('make these tests pass', 'get this to build', 'satisfy this grader'). Also a hard math or multiple-choice question with an ANSWER line (--spec aime|math500|gpqa|hle). Not for open-ended work with no way to check success by a command."
argument-hint: "[problem folder] [--verify-cmd CMD] [--mode loop|agents] [--keep-best] [--stress]"
metadata:
  author: coffeegrind123
  version: "1.0"
---

# team-solve

Run the team; do not solve the problem yourself in this context. The point of the method is
that every role starts fresh and reads only the files, and that a program run on the samples,
not any model's claim, decides success.

Ground truth is a check, not a model. Pick the check:

- **contest / algorithmic problem** → the built-in public-sample runner. Lay out `tests/`
  (below) and run with no `--verify-cmd`.
- **any other command-checkable task** (make a test suite pass, make a build compile,
  satisfy a grader, hit a benchmark, reproduce an output) → pass `--verify-cmd "CMD"`. The
  team iterates until `CMD`, run in the folder, exits 0; its output is the manager's
  feedback each round. In **agents mode** the roles edit the whole workspace with tools; in
  **loop mode** they write one Python file (`--solution-file`, default `solution.py`), so
  loop + `--verify-cmd` is for "write one script that passes this check" and multi-file
  work wants `--mode agents`.

## 1. Lay out the task folder

One folder per task. The run writes into it. `task.md` (or `problem.md`, or `--problem
FILE`) holds the statement. For the built-in sample check also add:

```
my-task/
  task.md          the statement / task description
  tests/1.in       public sample input         } sample check only
  tests/1.out      its exact expected output    }  (omit when using --verify-cmd)
  tests/func_name  only for LeetCode-style starter code: the method name. Each line of N.in is
                   one JSON argument, N.out is the JSON result; solution.py defines class Solution.
```

If the user pasted a statement, write `task.md` verbatim (keep any starter code, the format
section and the constraints) and write each sample pair into `tests/` byte for byte, with a
trailing newline. Never invent samples: only ones the statement contains. For a `--verify-cmd`
task, make sure the command already exists and runs in the folder. `sample/` in this skill is
a worked contest example.

## 2. Run it

```sh
S="$CODEX_HOME/skills/team-solve/scripts/team_solve.py"   # ./codex sets CODEX_HOME to this workspace's .codex
python3 $S run --dir my-task                                  # contest: the authors' sample loop
python3 $S run --dir my-task --keep-best --stress             # contest, both guards (recommended)
python3 $S run --dir my-task --verify-cmd "pytest -q"         # generic loop: one Python file to pass pytest
python3 $S run --dir my-task --verify-cmd "make" --mode agents # generic, whole-workspace agents
python3 $S status --dir my-task                               # rounds, per-round verdict, tokens
python3 $S check --dir my-task [--verify-cmd "CMD"]           # the strict check on its own
```

From a shell outside Codex the same thing is `./codex team-solve [args]` (router started,
secrets loaded, Landlock cage applied). A run takes minutes to an hour; run it in the
background and wait for it, do not poll.

Exit status: `0` solved (the check passed / math: an `ANSWER:` line exists), `1` not solved,
`2` infrastructure (router down, no token, an agent role that could not run). Report exit 2 as
an infrastructure failure with its message, not as "the team could not solve it".

## 3. Read the result

- With the sample check, `solution.py` (or `answer.md`) is the answer. With `--verify-cmd`,
  the result is the workspace the command passes against (agents mode) or the single solution
  file (loop mode). `final-test-output.log` is the strict final check.
- `status` shows each round's task and verdict (sample pass count, or the command's
  pass/fail). `plan.md`, `notes.md`, `tasks.json` are the team's ledger. `.team-solve/calls/`
  holds every request and reply; `transcript.jsonl` is the authors' transcript format;
  `meter.jsonl` the tokens per call.
- With the sample check, passing the samples is not passing the hidden tests — say so when
  you report. With `--verify-cmd`, the result is exactly as strong as the command: report what
  the command actually checks. `--stress` (samples only) adds a validated brute-force
  cross-check, which is much stronger evidence.

## Options that matter

| option | default | effect |
|---|---|---|
| `--verify-cmd` | off (samples) | the command that decides success (run in `--dir`, exit 0 = solved, output feeds the manager). Replaces the sample check and implies `--spec generic` |
| `--solution-file` | `solution.py` | loop mode: the file the single-file solution is written to |
| `--mode` | `loop` | `agents`: each role a full Codex agent editing the whole workspace; ~10x the tokens (memdoor's measurement). The real path for multi-file `--verify-cmd` tasks |
| `--keep-best` | off | if the final program fails its check, restore the last one that passed (sample or command) |
| `--stress` | off | samples only: one extra call writes `brute.py` + `gen.py`, validated on the samples; every passing program is compared on `--stress-cases` random small inputs, and a mismatch overrides the manager's "done" |
| `--samples all` | `stdin` | also run functional (`tests/func_name`) samples inside the loop; the authors ran stdin ones only |
| `--spec` | `code` | `generic` (neutral solver; implied by `--verify-cmd`), or `aime`, `math500`, `gpqa`, `hle` (the authors' math/QA prompts, result in `answer.md`) |
| `--model` / `--effort` | `deepseek-flash` / `high` | any model the router routes (`glm-5.3` etc.) |
| `--max-iters` | 10 | manager/worker rounds (authors') |
| `--fresh` | off | a new independent attempt; default replays recorded calls |

Rerunning the same command resumes: recorded calls whose request is byte-identical are
replayed, and the run continues live from the first one that differs. That makes an
interrupted run cheap to finish. For a second, independent attempt at the same problem,
pass `--fresh`.

## What not to do

- Do not edit the check — `tests/` or the `--verify-cmd` command — to force a pass, and do
  not hand-edit the result and report it as the team's.
- Do not change the role prompts in `scripts/teamsolve/prompts.py` casually: loop mode's
  contest prompts are pinned byte for byte to the authors' code by `tests/test_team_solve.py`.
- Loop-mode roles have no tools and produce one file; for multi-file or whole-workspace
  `--verify-cmd` tasks use `--mode agents`.
- Do not use it for open-ended work with no command that can decide success.

Provenance and licences: `NOTICE`.
