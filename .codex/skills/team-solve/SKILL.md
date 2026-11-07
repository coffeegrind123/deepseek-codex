---
name: team-solve
description: "Do a task with a team of fresh-context roles that talk only through files, checked every round. Agents mode (the default for a prompt): `team_solve.py \"what to do\" [--until \"end condition\"]` works in the project; a planner settles the Check (a command, the public samples, or a judge when nothing can be run), one direct agent attempt goes first, then ideators, a solver and review/judge/repair rounds until it passes; --until makes the person's own end condition binding, --verify-cmd fixes the Check. Loop mode: the GVS5H method byte for byte (planner, ideator, manager, workers, cut-off summarizer, up to 10 rounds, one Python file) for contest problems with samples, a --verify-cmd folder, or a math/QA spec."
when_to_use: "Use when the user hands over a task to drive to done with a team: '$team-solve do this and this', 'team-solve this until X holds', 'make these tests pass', 'get this to build', an algorithmic/contest problem with samples ('solve this AtCoder/Codeforces/LeetCode problem'), or a hard math or multiple-choice question with an ANSWER line (--spec aime|math500|gpqa|hle). Open-ended work without a command is fine: pass the user's end condition with --until, or let the planner fall back to the judge."
argument-hint: "\"<task>\" [--until \"<end condition>\"] [--verify-cmd CMD] [--dir D] | run --dir D [--mode loop|agents] [--keep-best] [--stress]"
metadata:
  author: coffeegrind123
  version: "2.0"
---

# team-solve

Run the team; do not do the task yourself in this context. The point of the method is that
every role starts fresh and reads only the files, and that a check, not any one model's
claim, decides success.

## 1. Pick the form

The user's words are the task. Pass them through as they wrote them; do not rewrite them
into a spec.

- **"$team-solve <task>"** with an end condition in the words ("until X", "done when X",
  "X must hold") → `"<task>" --until "<X>"`, with the condition in the user's own words.
- **"$team-solve <task>"** naming a command ("pytest must pass", "make check") → `"<task>"`
  alone is enough: the planner takes the command the prompt names. Add `--verify-cmd "CMD"`
  only when the user gave the command separately and it must be the Check.
- **"$team-solve <task>"** with neither → `"<task>"`. The planner settles a command from the
  project's own tests, or the judge when nothing can be run.
- **A contest problem with examples** → a folder with `task.md` and byte-exact
  `tests/N.in` / `tests/N.out` (only samples the statement contains), then `run --dir D
  --keep-best --stress` (loop mode, the benchmarked method).
- **Math / multiple choice** → `run --dir D --spec aime|math500|gpqa|hle` (loop mode).

The run works in `--dir` (default: the current folder). For a change to a project, that is
the project's root, or the subfolder the task is about.

## 2. Run it

```sh
S="$CODEX_HOME/skills/team-solve/scripts/team_solve.py"   # ./codex sets CODEX_HOME to this workspace's .codex
python3 $S "Rename X to Y everywhere; go test ./... must pass" --dir /path/to/project
python3 $S "Write docs/branching.md" --until "it names the feature/ prefix and the review rule" --dir P
python3 $S "Fix the retry bug" --verify-cmd "pytest -q tests/test_client.py" --dir P
python3 $S - --dir P < long-task.md                          # a long prompt from stdin
python3 $S run --dir my-task --keep-best --stress           # contest, loop mode
python3 $S status --dir P                                   # check kind, rounds, verdicts, keep, tokens
python3 $S check --dir P                                    # the settled Check again on its own
```

From a shell outside Codex the same is `./codex team-solve "<task>" [--until "..."]` (router
started, secrets loaded, Landlock cage applied). A run takes minutes to an hour; run it in
the background and wait for it, do not poll.

Exit status: `0` solved, `1` not solved, `2` infrastructure (router down, no token, an agent
role that could not run). Report exit 2 as an infrastructure failure with its message, not as
"the team could not solve it". Rerunning the same prompt after an exit 2 resumes the run.

## 3. Read the result

- Agents mode leaves its result in the project's tree. The run's own files are in
  `.team-solve/runs/<id>/`, git-ignored: `task.md` (the prompt plus the settled Deliverable,
  Check, Scope, Done when), `plan.md`, `notes.md`, `changes.diff`, `review-N.md`,
  `verdict-N.md`, `keep.txt`, `final-check.log`.
- `status` shows how it was decided:
  - **checked by `<cmd>`** / **the samples**: a command passed. The result is exactly as strong
    as that command; say what it checks.
  - **judged, not checked**: nothing could be run, so a reviewer and a fresh judge agreed the
    end condition holds. Say so plainly when you report: it is two model verdicts, not a test.
- `keep.txt` says whether the team's work or the direct attempt was kept. When the direct
  attempt was put back, the team's work is in `team.patch`.
- Loop mode: `solution.py` (or `answer.md`) in the folder; passing the samples is not passing
  the hidden tests — say so. `--stress` adds a validated brute-force cross-check.

## Options that matter

| option | default | effect |
|---|---|---|
| `"<task>"` / `-` | — | the task; implies agents mode. Without it the folder's `task.md` / `problem.md` is the task |
| `--until` | — | the person's end condition: first line of Done when, word for word, and part of the Prompt every role reads. The run is done only when it holds |
| `--verify-cmd` | — | the Check, fixed (agents) / the command that decides success (loop) |
| `--mode` | `auto` | agents for a prompt, `--until`, or a folder with nothing to check; loop for samples, a `--verify-cmd` folder, or a math spec |
| `--max-iters` | 10 | agents: review rounds (each after the first follows a repair); loop: manager/worker rounds |
| `--role-timeout` | auto | agents: per agent turn (direct 30m, solve/repair 90m) |
| `--keep-best` / `--stress` | off | loop mode guards (see README) |
| `--model` / `--effort` | `deepseek-flash` / `high` | any routed model, for every role |
| `--fresh` | off | a new independent run instead of resuming / replaying |

## What not to do

- Do not edit the Check — `tests/`, the command, or the run's `task.md` — to force a pass, and
  do not hand-edit the result and report it as the team's.
- Do not change the loop-mode prompts in `scripts/teamsolve/prompts.py` casually: they are
  pinned byte for byte to the authors' code by `tests/test_team_solve.py`.
- Do not start team-solve from inside a role (`TEAM_SOLVE_ROLE` is set): it refuses.

Provenance and licences: `NOTICE`.
