#!/usr/bin/env bash
#
# Codex SessionStart hook (.codex/hooks.json): tells herdr which Codex session
# runs in its pane, so Ovis follows that session's rollout through /resume, /new
# and several sessions in one CODEX_HOME. The work is herdr's own Codex hook,
# generated from the pinned herdr into tools/ovis by `./codex ovis install`.
#
# Outside a herdr pane, or without the bundle, it reads its input and does
# nothing. It runs inside the Landlock cage; herdr's hook only connects to
# herdr's socket, which the cage allows (measured 2026-11-01).

root="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
hook="${OVIS_DIR:-$root/tools/ovis}/integrations/herdr-codex-agent-state.sh"

if [[ "${HERDR_ENV:-}" != 1 || ! -f "$hook" ]]; then
  cat >/dev/null 2>&1
  exit 0
fi
exec sh "$hook" "$@"
