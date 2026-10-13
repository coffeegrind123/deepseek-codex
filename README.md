# Folder-local Codex: DeepSeek Flash orchestrator and subagents, Landlock cage

```
git clone git@github.com:coffeegrind123/deepseek-codex.git codex && cd codex
./setup.sh                   npm ci, keys -> .codex/secrets.env, catalog, skills, router, key probes
./setup.sh --check           verify an existing clone

./codex                      interactive TUI (cwd = this folder, CODEX_HOME = ./.codex)
./codex exec 'prompt' </dev/null   non-interactive; close stdin or exec waits for piped input
./codex-router status|logs   the model router
npm test                     router unit tests
npm run models               regenerate .codex/models.json from .codex/catalog/
```

## Day-to-day use

### Starting

```sh
./codex                          # interactive TUI
./codex exec "prompt" </dev/null # one-shot; close stdin or exec waits for piped input
./codex --help                   # every arg is passed straight to codex
```

`./codex` starts the router if needed, loads `.codex/secrets.env`, applies the Landlock cage,
then runs Codex with this folder as cwd and `CODEX_HOME`.

The session takes ~15s to start (see *Startup time* under Operations); the TUI drops typed
input until it is ready. Two startup warnings are expected: `--dangerously-bypass-hook-trust`
(passed by `./codex`, see *End-result verifier*) and "Running without the shared background
server" (the TUI runs its app server in-process).

### Approvals — yolo by default

`config.toml` sets `approval_policy = "never"`: the model runs every command on its own
without asking. That's the intended mode here. It's safe only because `sandbox_mode` is
`danger-full-access` and the **Landlock cage**, not Codex, is what bounds writes — an
approval prompt would gate nothing the cage doesn't already stop, and the end-result
verifier still gates completion. So plain `./codex` and `./codex exec` are already fully
hands-off; there is nothing extra to pass.

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

The router can swallow an upstream's "usage limit reached" SSE failure, keep Codex's
stream alive with `router.hold` heartbeats, and re-send the request once the window
resets, so a goal never parks as `UsageLimited`. It is on for the `zai` route (z.ai's
5-hour GLM window) and off for DeepSeek, which is pay-as-you-go. A route opts in with
`"usageLimit": { "enabled": true, ... }` in `routes.json` (see `router/router.mjs`,
`DEFAULT_USAGE_LIMIT`; covered by `npm test`).

### Daily skill automation

`./codex-schedule` keeps the **veikkaus-browser** skill running unattended once a day at
**08:00 Europe/Helsinki** (`VEIKKAUS_RUN_AT=HH:MM` / `VEIKKAUS_TZ` override). Each run is exactly
what a human would type in this folder:

```sh
./codex exec -m deepseek-flash --skip-git-repo-check --cd /home/claudeuser/codex '$veikkaus-browser' </dev/null
```

The model is pinned with `-m` (`VEIKKAUS_MODEL`, default `deepseek-flash`) and must stay that way:
without it the unattended run reads `.codex/config.toml`, which the TUI rewrites on every `/model`
switch, so a `glm-5.3` pick at the keyboard silently moved the job onto the z.ai plan — on
2026-10-13 that run burned the plan's 5-hour quota in 16 minutes and the router held the request
for 4h45m, pushing the day's screening to ~14:41. A run is also capped at `VEIKKAUS_RUN_TIMEOUT`
seconds (default 4h, `0` disables), so a wedged run releases the run lock instead of blocking the
same-day retry and the next slot.

A killed run does not take its work with it. The skill detaches its batch pipeline with `setsid`,
so it survives the run's TERM, reparents to pid 1, and keeps driving the shared Chrome — and a
second run's pipeline would then race it for the browser and `/tmp/vb-logs/*` (a wrong-result
failure, not a crash). So every run sweeps orphaned pipelines first, and again after one that was
killed: matched on argv against the skill's pipeline scripts with the cwd pinned to the skill
checkout, session leaders killed by process group, `VEIKKAUS_REAP_GRACE` seconds (default 10) of
TERM before escalating to KILL. The reaps appear in the daemon log, and
`./codex-schedule reap [--dry-run]` reports or reaps on demand.

The container has no cron and no systemd (pid 1 is bash), so the schedule is a detached daemon
(`scripts/veikkaus-daily.sh`) in the same pidfile-verified style as `codex-router`, not a crontab.
The `./codex` wrapper calls `codex-schedule ensure` on every invocation, so the schedule re-arms
itself after a container restart (opt out with `CODEX_SCHEDULE=off`).

```sh
./codex-schedule status              # running? next run? last result? exact command
./codex-schedule run-now             # run one session now (counts as the day's run)
./codex-schedule run-now --dry-run   # print the invocation without running it
./codex-schedule stop | start        # stop sets a flag; an active run finishes first
```

Behaviour: one run at a time (`flock`, so a still-running pipeline never overlaps the next slot);
one attempt per day, recorded in `.codex/veikkaus-daily.state`; a slot missed while the container
was down is caught up on the next daemon start unless the day is already recorded; a failed run
gets one same-day retry after 15 min. Daemon and per-run logs live in `.codex/logs/veikkaus-daily/`
(kept 90 days). `run-now` counts as the day's run.

`codex-schedule` and `scripts/veikkaus-daily.sh` are **git-ignored in this repo** (local-only, like
the `.codex/skills/veikkaus-browser` checkout itself), so a fresh clone runs without them and the
`./codex` ensure step is a no-op there.

#### Surviving Docker Desktop restarts (the `veikkaus-scheduler` container)

The daemon above lives inside this container, and this container has `restart=no`. So killing
Docker Desktop — or stopping this container — silently ends the schedule: nothing brings the daemon
back, and nothing runs `./codex` to re-arm it. To fix that without depending on this container at
all, the run is owned by a **separate, always-restarting container**:

```sh
./scripts/veikkaus-scheduler-container.sh ensure    # create + start (idempotent)
./scripts/veikkaus-scheduler-container.sh status | logs | restart | rm
```

`ensure` creates `veikkaus-scheduler` from the same image (`claude-code:latest`), with
`--restart unless-stopped`, mounting the **same host path** at `/home/claudeuser` and the Docker
socket — so paths, secrets, the skill checkout and the state files are shared, and every command
the skill runs behaves identically. Docker Desktop restarts it automatically; its entry
(`scripts/veikkaus-scheduler-entry.sh`) brings up Xvfb, then supervises the daemon, retrying until
it can take the shared daemon lock, so a slot missed while everything was down is caught up.

Because the state file and the `flock` locks are on the shared mount, two daemons (here and in the
scheduler container) still run the job **at most once per day**. While the scheduler container is
up, `./codex-schedule` here refuses to start a competing daemon — `status` reports
`owned by the veikkaus-scheduler container` — and `ensure` hands the run over (it stops any local
daemon so the scheduler can take the lock). Remove the container and the in-container daemon takes
over again on the next `./codex`. Both scheduler scripts are git-ignored, like the rest of the
daily automation.

### Subagents

The orchestrator and every subagent run on `deepseek-flash` (orchestrator at `max`
reasoning, `default`/`worker` at `high`, `explorer` at `low`). `.codex/AGENTS.md`
("Delegate to subagents by default") and the role descriptions in `config.toml` push the
orchestrator to fan work out: explorers for reading, workers for disjoint chunks of
implementation, `default` for anything else self-contained.

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

### Other useful commands

```sh
./codex-router status | logs | restart     # router health, request log, restart after key changes
./codex-schedule status | start | stop     # daily veikkaus-browser run (08:00 Europe/Helsinki)
./codex mcp list                           # MCP servers as Codex sees them
./codex debug models                       # the catalog Codex loaded
./codex debug prompt-input                 # what the model actually receives (AGENTS.md etc.)
CODEX_NET=restricted ./codex               # TCP limited to the router port (breaks browser/Ghidra MCP)
```

## How the pieces fit

```
./codex ──► codex-router ensure ──► router/router.mjs :8877 (outside the cage)
   │                                     ├─ deepseek-* ──► https://api.deepseek.com     (DEEPSEEK_API_KEY)
   │                                     └─ glm-*      ──► https://api.z.ai/api/v1      (ZAI_API_KEY, /model only)
   └─► sandbox/landlock-exec.py --rw . --rw /tmp ... ──► node_modules/.bin/codex
            (kernel policy inherited by subagents, MCP servers, every shell command)
```

**Why a router.** Codex allows one `model_provider` per session; agent roles may pin a
`model` but not a provider (`codex-rs/core/src/agent/role.rs`, `AgentRoleOverrides`).
The router is the single provider and picks the upstream from each request's `model`.
Pre-0.149 community setups put `[model_providers.*]` inside the role TOML; that stopped
working in 0.149+, which is why the router exists. It also gives per-request logging,
body rewrites, and the GLM usage-limit hold.

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
| `.codex/hooks/` | end-result verifier: `verify.py` (Stop + `update_goal` hook), `verifier_prompt.md`, `verify_test.py`; `state/` (round counters) git-ignored |
| `codex-schedule` | control for the daily unattended run: `start\|stop\|status\|ensure\|logs\|run-now` (git-ignored, local-only) |
| `scripts/veikkaus-daily.sh` | the daily daemon: sleeps to 08:00 Europe/Helsinki, runs `./codex exec '$veikkaus-browser'`, one attempt/day + catch-up/retry; logs in `.codex/logs/veikkaus-daily/` (git-ignored, local-only) |
| `.codex/catalog/` | model metadata sources; `deepseek-flash.json` (DeepSeek's official entry, default), `glm-5.3.json` (opt-in via `/model`), `base_instructions.md` (Codex `models-manager/prompt.md`, unchanged through rust-v0.158.0) |
| `.codex/models.json` | generated catalog (`npm run models`) |
| `.codex/cage.conf` | extra writable roots and net mode for the cage |
| `.codex/secrets.env` | API keys + router token, mode 600, git-ignored |
| `.codex/skills/` | symlinks to `~/.claude/skills/browser-automation` and `ghidra-re`; `veikkaus-browser` is a separate copy (its own git checkout, `.env` and state), git-ignored here |
| `router/routes.json` | model prefix → upstream table (`deepseek-*`, `glm-*`), per-route `usageLimit` hold policy (on for `glm-*`) |
| `setup.sh` | reproduce on a fresh clone; `--check` verifies |

Machine-specific paths that `setup.sh` does not manage: the `ghidra` MCP launcher
(`~/ghidra-in-claude-code/launch_ghidra_mcp.py`, `GHIDRA_HOME`, `JAVA_HOME`) and the
`browser` launcher (`/opt/zendriver-mcp/run.py`) in `config.toml`, and the skill symlinks'
source (`CLAUDE_SKILLS_DIR`, default `~/.claude/skills`). Requires Linux with Landlock
(kernel ≥ 5.13; ≥ 6.7 for `CODEX_NET=restricted`) and Node ≥ 20.

Not committed (see `.gitignore`): `.codex/secrets.env`, sessions, sqlite state, router
log/pid, `node_modules`, the `veikkaus-browser` skill copy, and `.codex/hooks/state/`.

## MCP servers

`browser` (zendriver-mcp, gateway mode, `DISPLAY=:99`) and `ghidra` (headless Ghidra +
bridge) are the same servers Claude Code uses, declared in `config.toml`. Tool names are
`mcp__browser__*` / `mcp__ghidra__*`, matching the skills' `allowed-tools`.

## Operations

- Router log: `.codex/router.log` — one line per request with model, route, status,
  latency, bytes; upstream error bodies are captured verbatim.
- Add a model: drop a catalog entry in `.codex/catalog/`, add a prefix to
  `router/routes.json` if it is a new upstream, `npm run models`, `./codex-router restart`.
- Startup time: before the session exists Codex recursively walks every skill root
  (`.codex/skills/`, symlinks followed, `.git` included, depth 6), and every entry costs
  several round trips on the 9p mount — a 22k-entry skill took ~75s. Keep large
  trees (data, models, venvs, build output) out of skill directories and `git gc` skill
  checkouts with many loose objects. `plugins`, `remote_plugin` and `apps` are off in
  `[features]`: they need a ChatGPT account, and plugins made each start block ~47s on a
  timed-out `git fetch` of `github.com/openai/plugins`.
- Upgrade Codex: `npm install @openai/codex@<ver>`; refresh `.codex/catalog/base_instructions.md`
  from `codex-rs/models-manager/prompt.md` at the matching `rust-v<ver>` tag.
