#!/usr/bin/env bash
# Reproduce this workspace on a fresh clone. Idempotent; re-run after pulling.
#
#   ./setup.sh                 install + configure, prompts for missing keys
#   ./setup.sh --check         verify only (Landlock, node, keys, router, catalog)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS="$ROOT/.codex/secrets.env"
CONFIG="$ROOT/.codex/config.toml"
CLAUDE_SKILLS="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
SKILLS=(browser-automation ghidra-re)
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

ok()   { printf '  ok   %s\n' "$*"; }
warn() { printf '  WARN %s\n' "$*" >&2; }
die()  { printf 'setup: %s\n' "$*" >&2; exit 1; }

printf '== toolchain\n'
command -v node >/dev/null || die "node not found (>= 20 required)"
command -v python3 >/dev/null || die "python3 not found (landlock-exec.py needs it)"
command -v curl >/dev/null || die "curl not found"
ok "node $(node --version), $(python3 --version)"

printf '== Landlock\n'
if python3 "$ROOT/sandbox/landlock-exec.py" --rw "$ROOT" --net open -- true 2>/dev/null; then
  ok "landlock ruleset applied and released"
else
  die "Landlock unavailable: kernel needs CONFIG_SECURITY_LANDLOCK and the seccomp profile must allow landlock_* syscalls"
fi

printf '== dependencies\n'
if (( ! CHECK_ONLY )); then
  (cd "$ROOT" && npm ci --no-fund --no-audit >/dev/null)
fi
[[ -x "$ROOT/node_modules/.bin/codex" ]] || die "node_modules/.bin/codex missing; run without --check"
ok "codex $("$ROOT/node_modules/.bin/codex" --version)"

printf '== secrets\n'
if [[ ! -f "$SECRETS" ]]; then
  (( CHECK_ONLY )) && die "$SECRETS missing"
  umask 077
  read -r -p "  ZAI_API_KEY (32hex.16chars): " zai
  read -r -p "  DEEPSEEK_API_KEY (sk-...): " ds
  token="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat >"$SECRETS" <<EOS
# Loaded by ./codex and ./codex-router. Never commit this file.
ZAI_API_KEY=$zai
DEEPSEEK_API_KEY=$ds
# Shared secret between Codex and the local router (generated at setup).
CODEX_ROUTER_TOKEN=$token
CODEX_ROUTER_PORT=8877
EOS
  umask 022
fi
chmod 600 "$SECRETS"
# shellcheck disable=SC1090
. "$SECRETS"
for var in ZAI_API_KEY DEEPSEEK_API_KEY CODEX_ROUTER_TOKEN; do
  [[ -n "${!var:-}" && "${!var}" != *'<'* ]] || die "$var not set in $SECRETS"
done
ok "secrets present, mode $(stat -c %a "$SECRETS")"

printf '== config paths\n'
# The trusted-project key must be this clone's absolute path.
if ! grep -qF "[projects.\"$ROOT\"]" "$CONFIG"; then
  (( CHECK_ONLY )) && die "config.toml trusts a different path than $ROOT"
  sed -i -E "s|^\[projects\.\"[^\"]+\"\]|[projects.\"$ROOT\"]|" "$CONFIG"
fi
ok "projects trust -> $ROOT"

printf '== model catalog\n'
if (( ! CHECK_ONLY )); then
  node "$ROOT/scripts/build-models.mjs" >/dev/null
fi
ok "$(python3 -c 'import json,sys;print(", ".join(m["slug"] for m in json.load(open(sys.argv[1]))["models"]))' "$ROOT/.codex/models.json")"

printf '== skills (symlinked from %s)\n' "$CLAUDE_SKILLS"
mkdir -p "$ROOT/.codex/skills"
for skill in "${SKILLS[@]}"; do
  if [[ -d "$CLAUDE_SKILLS/$skill" ]]; then
    (( CHECK_ONLY )) || ln -sfn "$CLAUDE_SKILLS/$skill" "$ROOT/.codex/skills/$skill"
    ok "$skill"
  else
    warn "$skill not found under $CLAUDE_SKILLS; set CLAUDE_SKILLS_DIR or drop the skill"
  fi
done

printf '== MCP server launchers (from config.toml)\n'
for path in /opt/zendriver-mcp/run.py "$HOME/ghidra-in-claude-code/launch_ghidra_mcp.py"; do
  [[ -e "$path" ]] && ok "$path" || warn "$path missing; that MCP server will fail to start (disable it in config.toml)"
done

printf '== router\n'
"$ROOT/codex-router" ensure >/dev/null
health="$(curl -fsS -m 3 "http://127.0.0.1:${CODEX_ROUTER_PORT:-8877}/health")"
ok "router $health"

printf '== upstream keys\n'
probe() {
  local model="$1"
  curl -sS -m 60 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${CODEX_ROUTER_PORT:-8877}/v1/responses" \
    -H "Authorization: Bearer $CODEX_ROUTER_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$model\",\"input\":\"Reply OK\",\"stream\":true,\"max_output_tokens\":16}"
}
for model in glm-5.3 deepseek-flash; do
  code="$(probe "$model")"
  [[ "$code" == 200 ]] && ok "$model -> HTTP $code" || warn "$model -> HTTP $code (see .codex/router.log)"
done

printf '\nReady: ./codex            (TUI)\n       ./codex exec "prompt" </dev/null\n'
