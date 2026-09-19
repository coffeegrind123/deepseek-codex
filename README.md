# Folder-local Codex: GLM-5.3 orchestrator, DeepSeek Flash subagents, Landlock cage

```
git clone git@github.com:coffeegrind123/codex-workspace.git codex && cd codex
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

### Approvals

`config.toml` sets `approval_policy = "on-request"`: the model runs commands on its own and
only stops when it decides to ask. `sandbox_mode` is `danger-full-access` because the cage,
not Codex, is the sandbox — so "bypass sandbox" flags change nothing about what can be
written. Fully hands-off:

```sh
./codex -a never        # never ask; a failed command is returned to the model
./codex --yolo          # alias of --dangerously-bypass-approvals-and-sandbox; same effect here
```

Make it permanent with `approval_policy = "never"` in `.codex/config.toml`, or switch
mid-session with `/permissions`. Not applicable in this build/setup: `--full-auto` (flag
does not exist in 0.155.1) and `--approve-for-me` (routes approvals through an
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

### Other useful commands

```sh
./codex-router status | logs | restart     # router health, request log, restart after key changes
./codex mcp list                           # MCP servers as Codex sees them
./codex debug models                       # the catalog Codex loaded
./codex debug prompt-input                 # what the model actually receives (AGENTS.md etc.)
CODEX_NET=restricted ./codex               # TCP limited to the router port (breaks browser/Ghidra MCP)
```

## How the pieces fit

```
./codex ──► codex-router ensure ──► router/router.mjs :8877 (outside the cage)
   │                                     ├─ glm-*      ──► https://api.z.ai/api/v1      (ZAI_API_KEY)
   │                                     └─ deepseek-* ──► https://api.deepseek.com     (DEEPSEEK_API_KEY)
   └─► sandbox/landlock-exec.py --rw . --rw /tmp ... ──► node_modules/.bin/codex
            (kernel policy inherited by subagents, MCP servers, every shell command)
```

**Why a router.** Codex allows one `model_provider` per session; agent roles may pin a
`model` but not a provider (`codex-rs/core/src/agent/role.rs`, `AgentRoleOverrides`).
The router is the single provider and picks the upstream from each request's `model`.
Pre-0.149 community setups put `[model_providers.*]` inside the role TOML; that stopped
working in 0.149+, which is why the router exists.

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
| `.codex/config.toml` | provider, models, roles, MCP servers, trust |
| `.codex/AGENTS.md` | global instructions (Codex's `~/.claude/CLAUDE.md` equivalent) |
| `AGENTS.md` | project-level notes Codex also loads (cage, layout) |
| `.codex/agents/*.toml` | role layers: `default`, `explorer`, `worker` → `deepseek-flash`; multi-agent pinned to v1 + `max_depth = 1` so subagents cannot nest |
| `.codex/catalog/` | model metadata sources; `glm-5.3.json`, `deepseek-flash.json` (DeepSeek's official entry), `base_instructions.md` (Codex prompt.md @ rust-v0.155.1) |
| `.codex/models.json` | generated catalog (`npm run models`) |
| `.codex/cage.conf` | extra writable roots and net mode for the cage |
| `.codex/secrets.env` | API keys + router token, mode 600, git-ignored |
| `.codex/skills/` | symlinks to `~/.claude/skills/browser-automation` and `ghidra-re` |
| `router/routes.json` | model prefix → upstream table |
| `setup.sh` | reproduce on a fresh clone; `--check` verifies |

Machine-specific paths that `setup.sh` does not manage: the `ghidra` MCP launcher
(`~/ghidra-in-claude-code/launch_ghidra_mcp.py`, `GHIDRA_HOME`, `JAVA_HOME`) and the
`browser` launcher (`/opt/zendriver-mcp/run.py`) in `config.toml`, and the skill symlinks'
source (`CLAUDE_SKILLS_DIR`, default `~/.claude/skills`). Requires Linux with Landlock
(kernel ≥ 5.13; ≥ 6.7 for `CODEX_NET=restricted`) and Node ≥ 20.

Not committed (see `.gitignore`): `.codex/secrets.env`, sessions, sqlite state, router
log/pid, `node_modules`.

## MCP servers

`browser` (zendriver-mcp, gateway mode, `DISPLAY=:99`) and `ghidra` (headless Ghidra +
bridge) are the same servers Claude Code uses, declared in `config.toml`. Tool names are
`mcp__browser__*` / `mcp__ghidra__*`, matching the skills' `allowed-tools`.

## Operations

- Router log: `.codex/router.log` — one line per request with model, route, status,
  latency, bytes; upstream error bodies are captured verbatim.
- Add a model: drop a catalog entry in `.codex/catalog/`, add a prefix to
  `router/routes.json` if it is a new upstream, `npm run models`, `./codex-router restart`.
- Upgrade Codex: `npm install @openai/codex@<ver>`; refresh `.codex/catalog/base_instructions.md`
  from `codex-rs/models-manager/prompt.md` at the matching `rust-v<ver>` tag.
