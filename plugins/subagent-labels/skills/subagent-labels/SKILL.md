---
name: subagent-labels
description: Explain and maintain human-readable subagent startup labels. Use when subagent activity appears as a bare UUID or when changing subagent naming.
---

# Subagent Labels

The project `.codex/hooks.json` runs `scripts/subagent-start.mjs` on
`SubagentStart`. The script reads the child rollout before the TUI renders normal
agent metadata, extracts the generated nickname, role, and first task instruction,
and emits a concise `systemMessage`.

Do not replace the hook with a plugin-bundled hook without verifying that the
plugin hook is trusted for non-interactive sessions. The workspace wrapper passes
`--dangerously-bypass-hook-trust` for project hooks, which is why this project
registration works in `codex exec`.
