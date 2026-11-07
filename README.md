# Folder-local Codex: DeepSeek Flash orchestrator and subagents, Landlock cage

```
git clone git@github.com:coffeegrind123/deepseek-codex.git codex && cd codex
./setup.sh                   npm ci, patched Codex build, keys -> .codex/secrets.env,
                             catalog, skills, router, key probes, ~/.local/bin/codex symlink
./setup.sh --check           verify an existing clone

./codex                      interactive TUI (cwd = caller's folder, CODEX_HOME = ./.codex)
./codex exec 'prompt' </dev/null   non-interactive; close stdin or exec waits for piped input
./codex-router status|logs   the model router
npm test                     router unit tests
./codex team-solve "task" [--until "end"]  team of fresh roles on a task from a prompt (README "Team solve")
npm run models               regenerate .codex/models.json from .codex/catalog/
./codex ovis status|token|run   Ovis: Codex sessions in a browser or phone (README "Ovis")
```

## Day-to-day use

### Starting

```sh
./codex                          # interactive TUI
./codex exec "prompt" </dev/null # one-shot; close stdin or exec waits for piped input
./codex --help                   # every arg is passed straight to codex
```

### Updating

```sh
./codex update                   # update the workspace-local CLI to the latest npm release
./codex update --check           # show pinned, installed, and latest versions
./codex update 0.166.0           # install an exact version
```

The npm cache is redirected to `/tmp/codex-npm-cache` so the update works inside this
container's Landlock policy. The updater installs the candidate into a staging directory
and builds the matching patched Codex runtime before touching the live tree. Only then
does it ask to stop active workspace Codex sessions, promote both the npm baseline and
`tools/codex-local/current` as one rollback transaction, and restore both if
post-promotion checks fail. A failed download, build, or staged verification leaves
running sessions and the live installation untouched. `./codex-update` is the equivalent
standalone entry point.

The executable is kept outside `node_modules`, because npm replaces that whole tree on
updates. Resolver precedence is `CODEX_WORKSPACE_BIN`, then
`tools/codex-local/current/codex`, then the npm launcher. An explicitly selected or
already-enabled local build fails closed if it is missing, broken, or version-mismatched;
Codex never silently falls back to an unpatched build.

```sh
npm run build:codex-local -- 0.166.0  # build and publish a matching patched runtime
scripts/resolve-codex-bin.sh          # print the active absolute executable
```

Patch filenames select their target: `codex-<version>-<name>.patch` applies only to that
`rust-v<version>` tag, while `codex-common-<name>.patch` applies to every target. The
updater also compares the recorded patch-series hash, so editing a patch rebuilds the
same Codex version instead of reusing a stale runtime.

**Building in CI instead of here.** `.github/workflows/build-codex.yml` runs
`scripts/build-codex-local.sh` on Ubuntu 22.04 (glibc 2.35, older than this container's
Debian 12, and the build fails if the binary needs a newer glibc) on every push that changes `patches/codex/`, the build script or the version
pin, and on demand (`gh workflow run build-codex.yml -f version=X.Y.Z`). It uploads the
release as the artifact `codex-local-<version>-<patch id>`. `scripts/fetch-codex-local.sh
[VERSION]` downloads the artifact for this checkout's exact patch series and lets the build
script verify and promote it. On a machine that must not compile, `CODEX_LOCAL_BUILD=ci` or
the word `ci` in `tools/codex-local/build-mode` makes the build script, and so `setup.sh` and
`./codex update`, fetch the CI build instead of running cargo.

This build is intentional. Stock Codex accepts only fixed identifiers in
[`tui.status_line`](https://developers.openai.com/codex/config-reference/), and the
official Linux payload is a stripped static-PIE executable. A byte-diff from the stock
binary must therefore be regenerated for every exact release from a corresponding patched
build; hand-maintained code-cave patches would need a new reverse-engineering pass per
release and are more brittle than the source patch. The updater rebuilds locally so a
future upstream release fails closed rather than applying a stale binary recipe.

### DeepSeek balance

```sh
./codex balance          # human-readable USD balance
./codex balance --json   # normalized machine-readable balance
```

The local `deepseek-balance` plugin also exposes `get_deepseek_balance` to agents. It uses
DeepSeek's official `GET https://api.deepseek.com/user/balance` endpoint, selects the USD
entry explicitly, and never returns the API key.

The patched Codex TUI adds a generic command-backed status-line item. `config.toml`
selects `deepseek-balance` and renders `DeepSeek $12.34 USD` in the bottom status bar.
The wrapped TUI cannot reach DeepSeek directly under `CODEX_NET=restricted`, so the
out-of-cage wrapper daemon refreshes `.codex/cache/deepseek-balance.json`; the footer
command reads that atomic cache once per minute through a helper added to `PATH` by the
wrapper. Output is limited to 4 KiB and one 240-character display line before it reaches
the renderer; controls, bidi markers, ANSI/OSC sequences, and the command's process group
are handled, and failures replace the value with `DeepSeek unavailable` while a previous
successful value is marked stale. Cache entries older than five minutes are also stale.
The wrapper retains the terminal-title balance indicator by default; set
`CODEX_DEEPSEEK_BALANCE_TITLE=off` to disable only the title while keeping the footer cache
fresh. Inside herdr panes it does the same, since herdr names a pane after its title.

Ovis shows the same balance in the composer pill, after the model and effort
(`deepseek-flash · High · $19.55`), only when the pane's model is a DeepSeek one. It turns the
warning colour below $2 or when DeepSeek reports the account unavailable, dims when stale, and its
tooltip gives granted/topped-up amounts and the age. The Ovis server (outside the cage) answers
`/api/deepseek/balance` from the footer cache above when it is under 90 s old, and otherwise
queries DeepSeek itself through the plugin library, at most once a minute; the key never reaches the
browser. Code: `patches/ovis/deepseek-balance.{ts,js}`, applied by `scripts/ovis_patch.py`.

`./codex` starts the router if needed, loads `.codex/secrets.env`, applies the Landlock cage,
then runs Codex with `CODEX_HOME` here and cwd in the folder you ran it from (or `-C DIR`),
which the cage also makes writable. From `/` or `$HOME` it falls back to this folder.
`~/.local/bin/codex` and `~/.local/bin/cs` symlink here, so plain `codex` and `cs` work
anywhere. `./setup.sh` creates the links; on a clone without them, create them by hand
(`~/.local/bin` must be on `PATH`):

```sh
ln -sfn ~/codex/codex ~/.local/bin/codex
ln -sfn ~/codex/cs ~/.local/bin/cs
```

The session takes ~15s to start (see *Startup time* under Operations); the TUI drops typed
input until it is ready. Two startup warnings are expected: `--dangerously-bypass-hook-trust`
(passed by `./codex`, see *End-result verifier*) and "Running without the shared background
server" (the TUI runs its app server in-process).

### Voice API

The local `voiceapi` plugin connects agents to a self-hosted
[voiceapi](https://github.com/coffeegrind123/voiceapi) gateway (Qwen3-TTS voice cloning,
NVIDIA RE-USE cleanup, Qwen3-ASR). Drop a recording into the Ovis chat and say "make this a
voice called Narrator": the agent calls `create_voice_from_file` with the `/.ovis/` path Ovis
put in the message, and the plugin uploads the file itself. The audio never enters the model's
context. Then "say hello" calls `speak`, which saves an mp3 next to the conversation.

A voice made this way becomes voiceapi's **default voice**, stored in the gateway rather than in
the session, so a new session with no history still speaks with it. It stays the default until
you ask to change it or turn it off (`set_default_voice`).

**Spoken replies in Ovis.** With `VOICEAPI_KEY` set, every reply an agent writes in the Ovis chat
is read aloud in the default voice as it arrives: the assistant's text only, never tool calls or
reasoning, for Codex, pi and any other agent Ovis understands. A chip in the page header
cycles *all replies → final answers only → off* (remembered per browser) and skips the current
reply while it speaks; sending a message stops it. Browsers allow sound only after you click or
tap the page once. History already in a chat when you open it is not read. The Ovis server holds
the key (`scripts/ovis.sh` passes `VOICEAPI_URL`/`VOICEAPI_KEY`); the browser sees only reply ids
and audio. Code: `patches/ovis/voice-narration.{ts,js}`, applied by `scripts/ovis_patch.py`.

Tools: `create_voice_from_file`, `speak`, `get_default_voice`, `set_default_voice`,
`transcribe_file`, `enhance_file`, `list_voices`, `delete_voice`, `get_job`. Set `VOICEAPI_KEY` (and `VOICEAPI_URL` if the gateway is not at
`http://host.docker.internal:8080`) in `.codex/secrets.env`. It needs the open network policy:
`CODEX_NET=restricted` blocks the gateway. Only audio and video files are uploaded, and
credential-looking paths are refused before anything is read. Tests: `npm run test:voiceapi`.

### Approvals — yolo by default

`config.toml` sets `approval_policy = "never"`: the model runs every command on its own
without asking. That's the intended mode here. It's safe only because `sandbox_mode` is
`danger-full-access` and the **Landlock cage**, not Codex, is what bounds writes — an
approval prompt would gate nothing the cage doesn't already stop. So
plain `./codex` and `./codex exec` are already fully hands-off; there is nothing extra to pass.

```sh
./codex                 # yolo: never asks (approval_policy = "never")
./codex --yolo          # identical effect; the explicit flag if you prefer it
./codex -a on-request   # opposite: make the model ask this run
```

Switch mid-session with `/permissions`. Not applicable in this build/setup: `--full-auto`
(flag does not exist in 0.158.0) and `--approve-for-me` (routes approvals through an
OpenAI-hosted reviewer, which these providers do not have).

### Resuming

```sh
./codex resume                   # picker of past sessions
./codex resume --last            # most recent session
./codex resume <id-or-name>      # a specific one; /rename <name> in the TUI names a session
./codex fork --last              # branch a past session into a new one
./codex exec resume --last "next prompt" </dev/null   # non-interactive continuation
```

Sessions live in `.codex/sessions/` (git-ignored). Every subagent gets its own rollout file
there too; `session_meta.source.subagent` records role, depth and parent.

### Goals

A goal is a persistent objective the TUI keeps working toward whenever the session is
idle, with an optional token budget:

```
/goal <objective>          set it and start
/goal pause | resume | edit | clear
```

The status line reports `Goal paused / stalled / hit usage limits (/goal resume)` when it
needs you. Cap budgets with `goals.max_goal_token_budget` in `config.toml`.

#### Usage-limit hold (GLM only)

The router first walks the configured fallback chain when an upstream reports a usage
limit. If the terminal route is still limited, it can either fail immediately
(`maxHoldMs: 0`) or keep Codex's stream alive with `router.hold` heartbeats and retry
after the window resets. This workspace fails fast because all z.ai GLM models share
the same five-hour account quota. A route opts in with
`"usageLimit": { "enabled": true, ... }` in `routes.json` (see `router/router.mjs`,
`DEFAULT_USAGE_LIMIT`; covered by `npm test`).

#### State-aware fallback

The GLM fallback chain is `glm-5.3 -> glm-5.3-flash -> glm-5-turbo -> deepseek-flash`,
and DeepSeek requests can also fall back into GLM. `routes.json` may therefore contain
fallback cycles: each request tracks its visited route+model nodes at runtime and refuses
to revisit one. A request already attempted on DeepSeek will not retry it after GLM
exhaustion.

### Subagents

The orchestrator and every subagent run on `deepseek-flash` (orchestrator at `max`
reasoning, `default`/`worker` at `high`, `explorer` at `low`). `.codex/AGENTS.md`
("Delegate to subagents by default") and the role descriptions in `config.toml` push the
orchestrator to fan work out: explorers for reading, workers for disjoint chunks of
implementation, `default` for anything else self-contained.

Each configured role also has human-readable `nickname_candidates`, such as
`Codebase Scout`, `Updater Engineer`, or `Independent Reviewer`. Codex renders these names
in spawn rows, the active-agent footer, and `/subagents`; the UUID remains the internal
tool identifier. Subagent labels and footer rendering are core TUI configuration, not a
plugin extension point, so they live in `.codex/config.toml` rather than a plugin.

Codex 0.163 can still bake a UUID into a spawn row when the TUI renders the completion
before thread metadata is hydrated. The local `subagent-labels` plugin supplies a
transcript-aware labeler used by project `PreToolUse` and `SubagentStart` hooks. The
`PreToolUse` message appears before the child exists, and the start message adds the
generated nickname and model once the child rollout is available, for example
`Updater Engineer started: Fix the staged updater transaction [worker] · deepseek-flash`.
Hooks can add these messages but cannot rename an already-rendered UUID row.

**GLM-5.3 (opt-in).** `/model` in the TUI also lists `glm-5.3` (z.ai GLM Coding Plan,
needs `ZAI_API_KEY` in `.codex/secrets.env`); for one run, `./codex -m glm-5.3`. That
switches only the orchestrator: roles are pinned to `deepseek-flash`, so subagents and
the verifier stay on DeepSeek.

At most 15 subagents are open at once (`agents.max_concurrent_threads_per_session`);
the orchestrator is told a user-stated number wins. To actually allow more in a run:

```sh
./codex -c agents.max_concurrent_threads_per_session=30
```

Nesting is capped at three levels, orchestrator → agent → agent: `max_depth = 2` on the
v1 multi-agent backend (depth counts from the orchestrator at 0). Agents at depth 2
get no spawn tool. The 15-agent cap is per session and covers the whole tree.

### End-result verifier

**Disabled.** The `[[hooks.PreToolUse]]` and `[[hooks.Stop]]` tables in `.codex/config.toml`
are commented out, so nothing runs automatically on completion. Uncomment them to re-enable;
the `verifier` role can still be spawned by hand. The rest of this section describes how it
works when enabled.

Codex 0.158 has no built-in end-of-task verifier — it only lets a hook reject "done" and
feed a reason back to the model (verified at rust-v0.158.0: `hooks/src/events/stop.rs`,
`pre_tool_use.rs`; `core/src/session/turn.rs` resumes the same turn on a block). This
workspace adds one. It checks the **final result against your original request**, not each
subagent's output.

`.codex/hooks/verify.py` serves two hook points (wired in `config.toml`), both meaning
"the agent thinks it's finished":

- **PreToolUse on `update_goal`** — gates `/goal` completion. An unverified goal can never
  be marked `complete`; the honest escape is `blocked` (which passes through untouched, so
  the model must justify it to you). Nothing passes silently.
- **Stop on a root turn that actually changed something** — ordinary tasks. A pure Q&A
  turn (no command, edit or spawn) is skipped. While a goal is active in that session, the
  `update_goal` gate owns verification and Stop stands down.

On either, it spawns a fresh, adversarial verifier (`deepseek-flash`, clean context, none
of the implementer's reasoning) that derives the acceptance criteria from your own words,
**re-runs the real build/tests itself**, and returns a JSON verdict with per-criterion
evidence. Design points:

- **Independent.** It sees the request + `git` diff, never the author's reasoning or a
  subagent's claim. Same model, fresh context — the point is independence, not a smarter
  model (`.codex/agents/verifier.toml` is the same role, spawnable by hand).
- **Read-only, enforced.** The container can't run Codex's read-only sandbox without
  breaking build caches, so instead the hook hashes `git diff` before and after: if the
  verifier touched a tracked file, its verdict is discarded and the work goes back.
- **Fail-closed.** No verdict, a crash, or a timeout counts as failure, never a free pass.
- **Bounded.** At most 3 verify rounds per task (`MAX_ROUNDS`); at the cap the model is
  ordered to tell you exactly what is unverified rather than the loop ending quietly.
- **No recursion.** The verifier's own `codex exec` carries `VERIFIER_ACTIVE=1`, which
  makes `verify.py` a no-op for that process.

Config hooks run only once trusted, which non-interactive `./codex exec` can't do
interactively, so the `./codex` wrapper passes `--dangerously-bypass-hook-trust` (its help:
"intended only for automation that already vets hook sources" — these hooks are
version-controlled here and `.codex/AGENTS.md` forbids the agent editing config from
untrusted content). It affects hooks only, not the cage or command approvals. Opt out with
`CODEX_HOOK_TRUST=strict ./codex` (then trust once via `/hooks` in the TUI).

Logic tests: `python3 .codex/hooks/verify_test.py`.

### Codex Security

[openai/codex-security](https://github.com/openai/codex-security) (`@openai/codex-security`,
pinned in `package.json`) is installed two ways, both running on the router like
everything else:

- **Plugin, inside `codex`.** 17 skills (`$codex-security:security-scan`,
  `security-diff-scan`, `deep-security-scan`, `triage-finding`, `fix-finding`,
  `verify-fix`, `threat-model`, ...) plus its MCP server. Ask in plain language: "Run a
  Codex Security scan on this repository". The package's bundled plugin is staged in
  `marketplace/plugins/` (git-ignored) and installed from the local `workspace`
  marketplace; the curated `openai-api-curated` copy is older and not used.
- **`cs` CLI, in a sibling container.** `cs scan` refuses to run unless Codex's bubblewrap
  sandbox starts, and this container's seccomp denies the user namespaces bwrap needs. So
  `./cs` runs the CLI through the Docker daemon in the published scanner image
  (`ghcr.io/openai/codex-security`, pinned by digest), with OpenAI's seccomp profile
  (`sandbox/codex-security-seccomp.json`), `--cap-drop ALL`, `no-new-privileges`, a read-only
  rootfs and uid 10001. It mounts only the repo (read-only; read-write for `patch` and
  `install-hook`), `.codex/state/cs` (its scan history, sticky `1777` so the non-root
  scanner can use it; the plugin's root-owned `0700` state is not shared), and
  `.codex/codex-security.yaml` + `models.json` read-only, each over its Docker-host path at
  the same absolute path. `scan`/`policy`/`scan-components`/`install-hook` run from a
  scratch cwd with the repo passed absolutely, because Codex's sandbox preflight mkdirs
  `<cwd>/.codex` and the repo mount is read-only. It refuses `~`, anything outside `~`, and any folder holding
  `.ssh`, `.claude`, `.codex/secrets.env` or other credentials. The only secret passed is
  the router token, as `CODEX_ROUTER_TOKEN` and as `CODEX_API_KEY` (cs refuses to start
  without an OpenAI-style key even when the provider has its own; `auth: api-key` in the
  project file selects it, and analytics are off). It shares this container's network
  namespace to reach the router.
  `cs` ignores `config.toml`, so `.codex/codex-security.yaml` restates the router provider
  and model; without it scans would default to `gpt-5.6-sol` on OpenAI.

```sh
cs scan .                        # standard scan of the current repo
cs scan . --diff origin/main     # committed changes only
cs scan . --mode deep            # repeated complete audits, cost-capped by default
cs findings                      # saved findings
```

Cost controls are persistent in `.codex/codex-security.yaml` for `cs` and in
`.codex/codex-security/config.toml` plus the `cxsec-security` runtime defaults
under `$CXSEC_HOME/scripts/deep_scan_defaults.json`. The `cs` wrapper supplies a
5-minute advisory investigation budget for Standard scans and a 90-minute limit
for Deep scans unless the caller passes an explicit time limit. Deep scans also
default to 2 workers, 1 subagent per worker, 3 consecutive no-new rounds, 2
consecutive errors, and at most 10 discovery runs.

Each deep worker repeats the complete audit, so deep cost is driven by
`workers × (1 + subagents)` concurrent audit agents plus coordinator/reduction
work and by how many discovery rounds complete. Override the caps without editing
the files:

```sh
cs scan . --max-time 5 --max-cost 2
cs scan . --mode deep --max-time 90 --max-cost 10 \
  --workers 1 --subagents 0 --max-discovery-runs 4
```

`--stop-after-no-new` is also available. The local cxsec runtime accepts
`workers`, `subagents`, `stop_after_no_new`,
`stop_after_consecutive_errors`, `max_discovery_runs`, and `max_time_hours` under
`[deep_scan]`.

Audit actual session costs and reference-heavy context growth without making a
model request:

```sh
python3 scripts/codex_security_usage.py
python3 scripts/codex_security_usage.py --scan-id <scan-id>
python3 scripts/codex_security_usage.py --scan-id <scan-id> --json
```

The time defaults live in the wrapper because `cs` 0.6.0 treats
`scan.max_time_minutes` and `scan.deep.max_time_hours` as conflicting values when
both appear in the project file. Explicit `--max-time` or `--max-time-hours`
arguments always win. Increasing discovery time does not raise the run-count or
cost caps.

Limits found in this container (2026-10-25):

- **Every ancestor of the scan output must be non-world-writable or sticky.** The 9p
  home folder was `0777`, so scans refused to save; it is now `1777` (`chmod +t`).
  If a container restart resets it, scans fail with "Scan output parent must not be group-
  or world-writable without the sticky bit".
- **Cost.** A measured standard scan of a 298-byte app used ~2.46M raw tokens,
  including ~2.26M cached tokens. Parent context grew from 11.9k to 90.5k
  tokens. References accounted for 158k of the parent's 187k tool-output
  characters. The new defaults cap routine standard investigation at 5 minutes
  and deep runs at the limits above.

Scan state lives in `.codex/state/` (git-ignored). `setup.sh` stages and installs the
plugin, pulls the `cs` image and links `~/.local/bin/cs`. To upgrade, bump the package
version and the matching image digest in `./cs` (`docker buildx imagetools inspect
ghcr.io/openai/codex-security:<ver>`), then re-run `./setup.sh`. Cloud, Linear and other connected apps need a ChatGPT account and stay off.

### Team solve (GVS5H)

`.codex/skills/team-solve/` runs a team of fresh-context roles that share only files, with
a check deciding each round. Two modes:

- **agents mode: a task from a prompt.** Port of memdoor's built-in team-solve
  (`workflows/team-solve` at `47dad8b`). You hand it a sentence, and it works in the current
  folder, usually a project. A planner settles the **Check**: a command in backticks, the
  public samples, or the **judge** when nothing can be run. `--until "sentence"` is your
  own end condition: it becomes the first line of Done when, word for word, and the run
  is not done until it holds. `--verify-cmd` fixes the Check.
- **loop mode: the GVS5H method itself** (Lee, 2026: five Qwen3.8-27B roles matched Fable 5
  on LiveCodeBench Hard). A planner, an ideator, a manager that owns the task list, workers
  and a cut-off summarizer, for up to 10 rounds, writing one file and checked by the
  samples or a `--verify-cmd`. Byte-identical to the authors' prompts.

Provenance and licences are in the skill's `NOTICE`.

```sh
./codex team-solve "Rename ReadGGUF to ParseGGUF everywhere; go test ./... must pass"
./codex team-solve "Write docs/branching.md for new teammates" --until "it names the feature/ prefix and the review rule"
./codex team-solve "Fix the flaky retry in client.py" --verify-cmd "pytest -q tests/test_client.py"
./codex team-solve - < task.md                                   # a long prompt
./codex team-solve run --dir my-task                             # contest: task.md + tests/N.in/N.out (loop)
./codex team-solve run --dir my-task --keep-best --stress        # contest, both guards
./codex team-solve run --dir my-task --verify-cmd "pytest -q"    # loop: one Python file passing a command
./codex team-solve status [--dir D]                              # rounds, verdicts, keep, tokens
./codex team-solve check  [--dir D] [--verify-cmd "CMD"]         # the strict check on its own
```

Inside a Codex session the orchestrator uses it as the `$team-solve` skill: `$team-solve do
this and this` (Codex 0.166 has no custom slash commands, so `/team-solve` cannot be
added). Exit 0 means solved, 1 means not solved, 2 means an infrastructure failure.

**Agents mode, step by step** (each role starts fresh; llm roles are one tool-less router call):

```
problem -+- direct (codex: the prompt as written) ----------+- test-0 -+- passed -------------------------------+
         +- outline - plan (llm) - settle - baseline -------+          +- failed: context - ideate a||b - solve --+
round r: test - review (llm) - [judge (llm, has seen none of the team's reasoning)] -> accepted, or repair (codex) -> r+1
keep (the net) - final check
```

- **First the single call, then the team.** `direct` is one agent turn on your words.
  When it passes the Check, only the review (and the judge, when needed) runs after it.
- **A round is accepted** when the Check passes (or there is none to run), the review does
  not FAIL, and the fresh judge says PASS whenever a command cannot settle it alone (no
  command, a repaired change, a review with no clear verdict). The samples decide alone.
  Rounds repeat up to `--max-iters` (10), and stop early when a repair changes nothing.
  Memdoor has one repair round.
- **Baseline.** A command Check first runs on a copy of the tree as found, with ignored
  dependencies linked in. Tests that already fail there do not count: only failures the
  change adds count (pytest, unittest, Django, go test logs). A failure that reads exactly
  as before is passed to the judge, with the log. Any other failure counts against the
  change. Memdoor switches the Check off there, and its judge never sees the log.
- **The keep net.** The team's work replaces the direct attempt only when it is better
  proven. With a command or samples it puts the direct attempt back (the team's work goes
  to `team.patch`). With a judge-only task, where neither can be proven, it keeps the
  latest work.
- **The tree.** Every state is a git tree written through a private index, so the
  project's index, branch and stash are never touched. A state holds untracked files too,
  so an edit to a file that was already untracked shows up. Secrets (`.env*`, `*.pem`,
  `*.key`, `*secret*`, `*credential*`) never enter a state, a diff or a restore. A role that
  moves HEAD gets it put back. Outside git, a shadow repository in `.team-solve/shadow.git` gives the same
  diff, snapshots and keep net. The run's files are in `.team-solve/runs/<id>/` (`task.md`,
  `plan.md`, `notes.md`, `changes.diff`, `review-N.md`, `verdict-N.md`, `keep.txt`,
  `final-check.log`, `calls/`), and `.team-solve/` is git-ignored by its own `.gitignore`.
- **Resume.** Rerunning the same prompt while its run is unfinished continues from the
  first unfinished step; the llm calls replay from the run's `calls/`. `--fresh` starts
  a new run.
- With the judge as the Check, the verdict is two models agreeing, not a command:
  `status` and the final line say **judged, not checked**.

**Loop mode, in detail:**

- **The check is the ground truth.** Without `--verify-cmd` it is the public-sample runner
  (contest problems, `tests/N.in`/`N.out`). With `--verify-cmd "CMD"` the team iterates
  until `CMD`, run in the folder, exits 0, and the command's output is the manager's
  feedback each round; `--verify-cmd` implies the neutral `generic` solver spec.
- One tool-less model call per role, sent straight to the router's `/v1/responses`
  (streamed, because the router cuts an upstream silent for 600 s). Authors' temperatures
  (0.2–0.4), top_p 0.95, 128K reply cap. The artifact is one file (`solution.py`, or
  `--solution-file`). `tests/test_team_solve.py` runs the authors' own `multiagent.py`
  (fetched at a pinned commit by `tests/fetch_gvs5h.sh`) and this port against one
  scripted model, and requires every request to be byte-identical across four scenarios.
- **Guards (sample mode), off by default so the default stays the authors' method.** Both
  close gaps that cost memdoor points in its 5-pass run. `--keep-best` restores the last
  passing program when the final one fails (works with `--verify-cmd` too). `--stress` has
  a fresh call write `brute.py` and `gen.py`, checks them against the samples, then compares
  every passing program with the brute force on 200 random small inputs; a mismatch
  overrides the manager's "done".
- **Resume by replay.** Rerunning the same command replays every recorded call whose
  request is byte-identical (`.team-solve/calls/`), continuing live from the first that
  differs. `--fresh` starts an independent attempt.
- Also supported: `--samples all` (LeetCode-style `tests/func_name` samples inside the
  loop), `--spec aime|math500|gpqa|hle` (the authors' math/QA prompts, result in
  `answer.md`), `--model` for any routed model.
- `--mode auto` (the default) picks agents mode for a prompt, an `--until`, or a folder
  with nothing to check, and loop mode for samples, a `--verify-cmd` folder or a math spec.

Measured 2026-11-01 on deepseek-flash:
- contest sample (balanced substrings, N ≤ 200000), loop mode `--keep-best --stress`:
  solved in 91 s and 6 calls, 4.7K in / 6.1K out tokens, brute force agreed 200/200. Agents
  mode on the same problem (the earlier eight-step agents shape, since replaced): 611 s,
  1.11M in (1.08M cached) / 30K out — about 100x the loop input volume.
- `--verify-cmd` loop mode (a Fibonacci task checked by a `check.py` script): solved in
  23 s and 5 calls, 3.6K in / 2.8K out tokens.

### Ovis (web and phone client)

[Ovis](https://github.com/devswha/ovis) shows the Codex sessions running in
[herdr](https://github.com/herdrdev/herdr) panes as chats in a browser or on a phone:
answer approvals and questions, send messages, follow the plan, open the live terminal.

```sh
./codex ovis status              # server, herdr, gate, access
./codex ovis token               # the access token (generated once, .codex/ovis/token, 0600)
./codex ovis run [-w DIR] [-- <codex args>]   # Codex in a new herdr pane, attached here
./codex ovis start | stop        # stop leaves herdr and its panes running
./codex ovis gate ensure | status | rm        # the host-side port (below)
./codex ovis install [--check]   # the pinned bundle; setup.sh runs it
```

Open **http://127.0.0.1:7327/** on the Docker host (with Docker Desktop, the Windows host) and
sign in with the token.

- **Always on.** Every interactive `./codex` starts it in the background (detached, so
  the TUI never waits). `exec` runs, other unattended launches, shells inside the
  cage and herdr panes don't start it. `OVIS_ENABLED=0` turns it off.
- **Every new pane is a Codex chat.** herdr runs `scripts/ovis-shims/pane-shell` for each
  new pane: `./codex` in the pane's folder, then bash once Codex quits. A new workspace, tab
  or split, and herdr's first pane (started in `OVIS_CODEX_DIR`, default this folder), open on
  a Codex chat. Ovis's own **New workspace / New tab** panes are plain, so its agent picker
  decides. The picker defaults to Codex until a browser picks something else, Shell included.
  Panes restored after a herdr restart start a fresh Codex (herdr's
  `resume_agents_on_restore` is kept `false`, or its resume command would be typed into that
  Codex), and the earlier conversation is one `/resume` away. `OVIS_CODEX_PANE=0` makes new
  panes plain shells. Detach from `ovis run` with Ctrl+B then Q: Codex keeps running, and
  Ovis keeps showing it.
- **Startup.** A Codex TUI loses its session ("No active thread is available", every later
  message dropped) when keystrokes arrive, or its skills change, before its session thread is
  up. Keep big, busy folders (a browser profile, logs) out of skill folders: Codex walks every
  skill folder at startup and reloads the skills of every running Codex on each change there.
  And keep anything that reads the terminal (`sudo` with `use_pty`, for one) out of the shell's
  startup files, or it eats the `codex` herdr types into a new pane.

  Ovis also holds a chat message or queued delivery to a Codex pane until that Codex's session
  thread is up. It reads the start from Codex's own log database, waits at most 180 s, refuses
  when no Codex runs in the pane, and never types into the shell instead
  (`patches/ovis/codex-ready.ts`). Typing in the terminal view is not held. A message entered in
  the New workspace dialog goes to Codex as an argument and is safe.
- **Commands.** Ovis lists Codex's builtins after `/` and the skills after `$`: this
  workspace's (`team-solve`, `cxsec-security`, `ghidra-re`, `browser-automation`), the plugin skills
  (`codex-security:*`, `deepseek-balance:*`, `subagent-labels:*`) and Codex's own.
  `scripts/ovis_patch.py` brings Ovis's command table, which is pinned to Codex 0.163, up to
  0.166: `/presets` (opens Codex's saved-prompts picker in the terminal), plus `/model`,
  `/permissions`, `/title` and `/statusline` with an argument, which act without a picker.
  The patch also makes Ovis's New workspace / New tab panes plain, sets the picker default
  above and adds the startup hold. It runs at every `./codex ovis start`, each edited file is pinned to the bundle's
  original sha256, and it refuses a different bundle without writing anything.
- **Every pane is the full stack.** `scripts/ovis-shims/codex` is first on herdr's
  PATH, so `codex` typed into a pane, or started there by Ovis, goes through `./codex`
  (router, secrets, Landlock cage, patched build). herdr itself runs outside the cage and
  never sees `secrets.env`. `ovis run` writes the per-launch settings herdr would drop
  (`CODEX_NET`, `CODEX_HOOK_TRUST`, `CODEX_START_HOOK`, ...) into a 0600 launch script that
  deletes itself. A fixed list, never a key or token.
- **Transcripts.** Ovis reads each pane's rollout from the `CODEX_HOME` of its Codex
  process. The SessionStart hook in `.codex/hooks.json` (`scripts/ovis-codex-hook.sh`, which
  runs herdr's own Codex hook from the bundle) tells herdr which session the pane runs, so
  `/resume`, `/new` and several sessions in `.codex/` are followed. It does nothing
  outside a herdr pane. Measured 2026-11-01: a caged `./codex` in a pane read as
  `source: codex-transcript`, and the hook reached herdr's socket from inside the cage.

**Network path.** This container publishes no ports, and Docker cannot add one to a
running container. So:

```
browser ─► host 127.0.0.1:7327 ─► codex-ovis-gate :7317 ─(codex-ovis network)─► <this container>:7317
            relay (scripts/ovis_forward.py) ─► Ovis 127.0.0.1:7318 (bun, tools/ovis)
```

`codex-ovis-gate` runs the same relay from this container's image: read-only root, no
capabilities, `no-new-privileges`, uid 65534, 64 MB, `--restart unless-stopped`. It is
recreated when its `docker run` arguments change. `ovis gate ensure` creates the
`codex-ovis` network and connects this container to it. Ovis binds loopback behind the
in-container relay because a non-loopback bind shows a permanent "reachable on your
network" banner. Host port 7317 is Ovis's own default and often taken, so this workspace
defaults to 7327 (`OVIS_PORT`). `OVIS_GATE=off` publishes nothing (reachable inside this container only).

**Access.** Every request except `/api/health` and the page itself needs the token
(measured: 401 without it, from the host). The relay listens on all of this container's
networks, so other containers on them reach the port, and they still need the token.
`OVIS_TOKEN=off` drops the token: then anything that reaches the port can type into
Codex until a device is paired, including every program on the Docker host and a web page
that rebinds its name to 127.0.0.1. Telemetry and self-update are always off.

**Settings** come from the environment, then `.codex/secrets.env` (read as text):
`OVIS_ENABLED`, `OVIS_TOKEN`, `OVIS_PORT`, `OVIS_GATE`, `OVIS_CODEX_PANE`, `OVIS_CODEX_DIR`.
State (the token, Ovis's pairing and push keys in `state/`, pasted files in `paste/`) is in
`.codex/ovis/`, which is git-ignored. Runtime files and the log are in `/tmp/codex-ovis/`, per
container. The bundle is `remote-v40` (herdr 0.9.3, bun 1.4.2), pinned by sha256 in
`scripts/ovis.sh` and installed into `tools/ovis/` without compiling anything. This
workspace's edits to it are `scripts/ovis_patch.py` and `patches/ovis/`. In a herdr pane
the balance stays in the footer, not in the terminal title, because herdr and Ovis name a pane
after its title. Tests: `npm run test:ovis`.

### Other useful commands

```sh
./codex-router status | logs | restart     # router health, request log, restart after key changes
./codex mcp list                           # MCP servers as Codex sees them
./codex debug models                       # the catalog Codex loaded
./codex debug prompt-input                 # what the model actually receives (AGENTS.md etc.)
CODEX_NET=restricted ./codex               # TCP limited to the router port (breaks browser/Ghidra MCP)
```

Optional local integrations can run after setup or whenever the wrapper starts:

```sh
CODEX_LOCAL_SETUP_HOOK=/path/to/hook ./setup.sh
CODEX_START_HOOK=/path/to/hook ./codex
```

Both hooks are explicit opt-ins and remain outside this repository.

## How the pieces fit

```
./codex ──► codex-router ensure ──► router/router.mjs :8877 (outside the cage)
   │                                     ├─ deepseek-* ──► https://api.deepseek.com     (DEEPSEEK_API_KEY)
   │                                     └─ glm-*      ──► https://api.z.ai/api/v1      (ZAI_API_KEY, /model only)
   └─► sandbox/landlock-exec.py --rw . --rw /tmp ... ──► tools/codex-local/current/codex
            (kernel policy inherited by subagents, MCP servers, every shell command)
```

**Why a router.** Codex allows one `model_provider` per session; agent roles may pin a
`model` but not a provider (`codex-rs/core/src/agent/role.rs`, `AgentRoleOverrides`).
The router is the single provider and picks the upstream from each request's `model`.
Pre-0.149 community setups put `[model_providers.*]` inside the role TOML; that stopped
working in 0.149+, which is why the router exists. It also gives per-request logging,
body rewrites, the GLM usage-limit hold, and a multi-hop fallback chain for provider
content-policy false positives. The chain covers DeepSeek's HTTP 400 `Content Exists
Risk` and GLM's HTTP 200 SSE `response.failed` policy errors before Codex sees them.

**Why Landlock and not Codex's sandbox.** Codex's Linux sandbox is bubblewrap and needs
user namespaces; this container's seccomp profile denies them (`unshare -Ur` → EPERM) and
the legacy Landlock path panics with 0.155 permission profiles. `landlock-exec.py` applies
a Landlock ruleset (ABI 7 here) to the whole tree instead: read/execute everywhere, write
only under this folder plus `.codex/cage.conf` roots. `sandbox_mode` is therefore
`danger-full-access` in `config.toml`: Codex must not try to wrap commands itself.
`CODEX_NET=restricted ./codex` additionally limits TCP to the router port (breaks the
browser and Ghidra MCP servers, which need the network).

## Files

| Path | Purpose |
| --- | --- |
| `.codex/config.toml` | provider, models, roles, feature flags, MCP servers, trust, verifier hooks |
| `.codex/AGENTS.md` | the only instructions file (Codex's `~/.claude/CLAUDE.md` equivalent): agentic prompt + this workspace's cage, models and layout |
| `.codex/agents/*.toml` | role layers: `default`, `explorer`, `worker`, `verifier` → `deepseek-flash`; multi-agent pinned to v1 + `max_depth = 2` (orchestrator → agent → agent); at most 15 open at once |
| `plugins/deepseek-balance/` | local plugin: USD balance MCP tool, skill, and reusable balance client |
| `plugins/subagent-labels/` | local labeler bundled as a plugin; `.codex/hooks.json` wires it to `PreToolUse` and `SubagentStart` so subagents get human-readable task labels even when the TUI metadata race would otherwise show only a UUID |
| `scripts/deepseek-balance.mjs` | `./codex balance`, footer status, and terminal-title daemon |
| `scripts/build-codex-local.sh` | builds and atomically publishes a patched Codex runtime for one upstream version |
| `scripts/resolve-codex-bin.sh` | selects the external local build, explicit override, or npm fallback with fail-closed validation |
| `patches/codex/` | status-line command extension applied to the matching Codex source tag |
| `.codex/hooks/` | end-result verifier: `verify.py` (Stop + `update_goal` hook), `verifier_prompt.md`, `verify_test.py`; `state/` (round counters) git-ignored |
| `.codex/catalog/` | model metadata sources; `deepseek-flash.json` (DeepSeek's official entry, default), `glm-5.3.json` (opt-in via `/model`), `base_instructions.md` (Codex `models-manager/prompt.md`, unchanged through rust-v0.158.0) |
| `.codex/models.json` | generated catalog (`npm run models`) |
| `.codex/cage.conf` | extra writable roots and net mode for the cage |
| `.codex/secrets.env` | API keys + router token, mode 600, git-ignored |
| `.codex/skills/team-solve/` | team solver: `scripts/team_solve.py` (agents mode: a task from a prompt, `teamsolve/pipeline.py`; loop mode: GVS5H with guards and replay), `SKILL.md`, `NOTICE`, `sample/`; tests in `tests/test_team_solve.py` |
| `.codex/skills/` | symlinks to `~/.claude/skills/browser-automation` and `ghidra-re` |
| `router/routes.json` | model prefix → upstream table (`deepseek-*`, `glm-*`), per-route `usageLimit` hold policy (on for `glm-*`) and state-aware fallback chains (`glm-5.3 -> glm-5.3-flash -> glm-5-turbo -> deepseek-flash`, with DeepSeek able to enter GLM) |
| `scripts/ovis.sh` | `./codex ovis`: the Ovis supervisor (herdr, server, relay), default Codex pane, `run`, the `codex-ovis-gate` container, pinned bundle install; `scripts/ovis_forward.py` (relay), `scripts/ovis-shims/codex` (pane shim), `scripts/ovis-shims/pane-shell` (herdr's pane shell: Codex, then bash), `scripts/ovis-codex-hook.sh` (SessionStart hook), `scripts/ovis_patch.py` + `patches/ovis/` (edits to the bundle) |
| `tools/ovis/` | the pinned Ovis bundle and herdr's generated Codex hook (git-ignored, `./codex ovis install`) |
| `setup.sh` | reproduce on a fresh clone; `--check` verifies |

Machine-specific paths that `setup.sh` does not manage: the `ghidra` MCP launcher
(`/opt/ghidra-mcp/launch_ghidra_mcp.py` as shipped, `GHIDRA_HOME`, `JAVA_HOME`) and the
`browser` launcher (`/opt/zendriver-mcp/run.py`) in `config.toml`, and the skill symlinks'
source (`CLAUDE_SKILLS_DIR`, default `~/.claude/skills`). Requires Linux with Landlock
(kernel ≥ 5.13; ≥ 6.7 for `CODEX_NET=restricted`), Node ≥ 20, and a Rust toolchain with
`cargo`, `rustc`, `git`, `sha256sum`, and `flock` for the patched runtime build.

Not committed (see `.gitignore`): `.codex/secrets.env`, sessions, sqlite state, router
log/pid, `node_modules`, local integrations, `.codex/hooks/state/`, and `.codex/ovis/`
(the Ovis token and state).

## MCP servers

`browser` (zendriver-mcp, gateway mode, `DISPLAY=:99`) and `ghidra` (headless Ghidra +
bridge) are the same servers Claude Code uses, declared in `config.toml`. Tool names are
`mcp__browser__*` / `mcp__ghidra__*`, matching the skills' `allowed-tools`.

## Operations

- Router log: `.codex/router.log` — one line per request with model, route, status,
  latency, bytes and fallback chain; non-fallback upstream error bodies are captured
  verbatim, while policy-failure fallbacks log the model and policy code.
- Add a model: drop a catalog entry in `.codex/catalog/`, add a prefix to
  `router/routes.json` if it is a new upstream, `npm run models`, `./codex-router restart`.
- Startup time: before the session exists Codex recursively walks every skill root
  (`.codex/skills/`, symlinks followed, `.git` included, depth 6), and every entry costs
  several round trips on the 9p mount — a 22k-entry skill took ~75s. Keep large
  trees (data, models, venvs, build output) out of skill directories and `git gc` skill
  checkouts with many loose objects. `remote_plugin` and `apps` are off in `[features]`:
  they need a ChatGPT account. `plugins` is on for codex-security; on 0.163 the curated
  `github.com/openai/plugins` sync no longer delays startup (measured 2026-10-25: 46s/33s
  with plugins on vs 52s/37s off), though on an older Codex it blocked each start ~47s.
- Upgrade Codex: `./codex update [<ver>]`; refresh `.codex/catalog/base_instructions.md`
  from `codex-rs/models-manager/prompt.md` at the matching `rust-v<ver>` tag.
  The updater must also build the matching patch series; npm baseline updates do not
  replace or bypass the external local runtime.
