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
  mkdir -p "$ROOT/.tmp/npm-cache"
  (cd "$ROOT" && NPM_CONFIG_CACHE="$ROOT/.tmp/npm-cache" npm ci --no-fund --no-audit >/dev/null)
fi
[[ -x "$ROOT/node_modules/.bin/codex" ]] || die "node_modules/.bin/codex missing; run without --check"
ok "codex $("$ROOT/node_modules/.bin/codex" --version)"

printf '== secrets\n'
if [[ ! -f "$SECRETS" ]]; then
  (( CHECK_ONLY )) && die "$SECRETS missing"
  umask 077
  read -r -p "  DEEPSEEK_API_KEY (sk-...): " ds
  read -r -p "  ZAI_API_KEY (optional, for /model glm-5.3; 32hex.16chars): " zai
  token="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat >"$SECRETS" <<EOS
# Loaded by ./codex and ./codex-router. Never commit this file.
DEEPSEEK_API_KEY=$ds
ZAI_API_KEY=$zai
# Shared secret between Codex and the local router (generated at setup).
CODEX_ROUTER_TOKEN=$token
CODEX_ROUTER_PORT=8877
EOS
  umask 022
fi
chmod 600 "$SECRETS"
# shellcheck disable=SC1090
. "$SECRETS"
for var in DEEPSEEK_API_KEY CODEX_ROUTER_TOKEN; do
  [[ -n "${!var:-}" && "${!var}" != *'<'* ]] || die "$var not set in $SECRETS"
done
ok "secrets present, mode $(stat -c %a "$SECRETS")"
[[ -n "${ZAI_API_KEY:-}" && "$ZAI_API_KEY" != *'<'* ]] || warn "ZAI_API_KEY not set; /model glm-5.3 will fail with 502"

printf '== config paths\n'
# The trusted-project key must be this clone's absolute path.
if ! grep -qF "[projects.\"$ROOT\"]" "$CONFIG"; then
  (( CHECK_ONLY )) && die "config.toml trusts a different path than $ROOT"
  sed -i -E "s|^\[projects\.\"[^\"]+\"\]|[projects.\"$ROOT\"]|" "$CONFIG"
fi
ok "projects trust -> $ROOT"

printf '== verifier hook\n'
for f in .codex/hooks/verify.py .codex/hooks/verifier_prompt.md .codex/agents/verifier.toml; do
  [[ -f "$ROOT/$f" ]] || die "$f missing"
done
if (( ! CHECK_ONLY )); then
  python3 "$ROOT/.codex/hooks/verify_test.py" >/dev/null 2>&1 || die "verify_test.py failed"
fi
if grep -qE '^command = .*verify\.py' "$CONFIG"; then
  ok "verify.py hooks enabled + verifier role"
else
  ok "verify.py hooks disabled in config.toml (verifier role still spawnable)"
fi
node "$ROOT/scripts/check_subagent_hooks.mjs" || die "subagent label hooks are not trusted"
ok "subagent label hooks trusted"

printf '== codex and cs on PATH\n'
# ~/.local/bin/{codex,cs} -> ./codex and ./cs, so plain `codex` and `cs` work in any folder.
for name in codex cs; do
  link="$HOME/.local/bin/$name"
  if [[ "$(readlink -f "$link" 2>/dev/null)" != "$ROOT/$name" ]]; then
    (( CHECK_ONLY )) && die "$link does not point at $ROOT/$name; run without --check"
    mkdir -p "$(dirname "$link")"
    ln -sfn "$ROOT/$name" "$link"
  fi
  ok "$link -> $ROOT/$name"
done
[[ ":$PATH:" == *":$HOME/.local/bin:"* ]] || warn "$HOME/.local/bin is not on PATH; add it to use plain \`codex\`"

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

printf '== local setup hook\n'
if [[ -n "${CODEX_LOCAL_SETUP_HOOK:-}" ]]; then
  [[ -x "$CODEX_LOCAL_SETUP_HOOK" ]] || die "CODEX_LOCAL_SETUP_HOOK is not executable"
  (( CHECK_ONLY )) || "$CODEX_LOCAL_SETUP_HOOK"
  ok "$CODEX_LOCAL_SETUP_HOOK"
else
  ok "none configured"
fi

printf '== codex-security plugin\n'
# The plugin ships prebuilt inside the npm package. Codex installs plugins only from a
# marketplace and rejects symlinked or out-of-root plugin paths, so the bundled copy is
# patched into marketplace/plugins/ and installed into .codex/plugins/cache from there.
CS_PKG="$ROOT/node_modules/@openai/codex-security/_bundled_plugin"
CS_STAGE="$ROOT/marketplace/plugins/codex-security"
CS_PATCH="$ROOT/scripts/patch_codex_security_plugin.py"
DAYBREAK_PATCH="$ROOT/.codex/skills/cxsec-security/scripts/patch_daybreak_access.mjs"
CS_CACHE="$ROOT/.codex/plugins/cache/workspace/codex-security"
[[ -d "$CS_PKG" ]] || die "$CS_PKG missing; run without --check"
[[ -f "$CS_PATCH" ]] || die "$CS_PATCH missing"
CS_VERSION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$CS_PKG/.codex-plugin/plugin.json")"
if (( CHECK_ONLY )); then
  if ! python3 "$CS_PATCH" --source "$CS_PKG" --output "$CS_STAGE" --check --quiet \
    --allow-extra 'mcp/daybreak-original.server.mjs.br.part-*' \
    --allow-modified 'mcp/server.mjs.br.part-*'; then
    die "codex-security plugin at $CS_STAGE is not a valid patched copy; run without --check"
  fi
else
  mkdir -p "$(dirname "$CS_STAGE")"
  if ! python3 "$CS_PATCH" --source "$CS_PKG" --output "$CS_STAGE" --quiet; then
    die "failed to patch codex-security plugin at $CS_STAGE"
  fi
  if [[ -f "$DAYBREAK_PATCH" ]]; then
    node "$DAYBREAK_PATCH" >/dev/null
  fi
  sed -i -E "/^\[marketplaces\.workspace\]/,/^source =/ s|^source = .*|source = \"$ROOT/marketplace\"|" "$CONFIG"
  if ! grep -q '^\[marketplaces\.workspace\]' "$CONFIG"; then
    CODEX_HOME="$ROOT/.codex" "$ROOT/node_modules/.bin/codex" plugin marketplace add "$ROOT/marketplace" >/dev/null
  fi
  if ! python3 "$CS_PATCH" --source "$CS_PKG" --output "$CS_CACHE/$CS_VERSION" --check --quiet \
    --allow-extra 'mcp/daybreak-original.server.mjs.br.part-*' \
    --allow-modified 'mcp/server.mjs.br.part-*' 2>/dev/null; then
    CODEX_HOME="$ROOT/.codex" "$ROOT/node_modules/.bin/codex" plugin add codex-security@workspace >/dev/null
  fi
fi
[[ -f "$CS_CACHE/$CS_VERSION/.codex-plugin/plugin.json" ]] \
  || die "codex-security $CS_VERSION not installed; run without --check"
grep -q '^\[plugins\."codex-security@workspace"\]' "$CONFIG" || die "config.toml does not enable codex-security@workspace"
ok "codex-security plugin $CS_VERSION"
# ./cs runs the CLI in a sibling container from this pinned image (see the script header).
CS_IMAGE="$(sed -n -E 's/^IMAGE="([^"]+)"$/\1/p' "$ROOT/cs")"
if ! docker image inspect "$CS_IMAGE" >/dev/null 2>&1; then
  (( CHECK_ONLY )) && die "$CS_IMAGE not pulled; run without --check"
  docker pull -q "$CS_IMAGE" >/dev/null
fi
ok "cs image ${CS_IMAGE##*@}"

printf '== deepseek-balance plugin\n'
DB_SRC="$ROOT/plugins/deepseek-balance"
DB_STAGE="$ROOT/marketplace/plugins/deepseek-balance"
[[ -f "$DB_SRC/.codex-plugin/plugin.json" ]] || die "$DB_SRC/.codex-plugin/plugin.json missing"
DB_VERSION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$DB_SRC/.codex-plugin/plugin.json")"
if (( CHECK_ONLY )); then
  [[ -d "$DB_STAGE" ]] || die "deepseek-balance plugin is not staged; run without --check"
  if ! diff -qr "$DB_SRC" "$DB_STAGE" >/dev/null; then
    die "deepseek-balance plugin stage differs from $DB_SRC; run without --check"
  fi
else
  rm -rf -- "$DB_STAGE"
  mkdir -p "$DB_STAGE"
  cp -a "$DB_SRC/." "$DB_STAGE/"
  CODEX_HOME="$ROOT/.codex" "$ROOT/node_modules/.bin/codex" plugin add deepseek-balance@workspace >/dev/null
  node --test "$ROOT/plugins/deepseek-balance/test/deepseek-balance.test.mjs" \
    "$ROOT/plugins/deepseek-balance/test/mcp-server.test.mjs" \
    "$ROOT/tests/test_deepseek_balance_title.mjs" >/dev/null
fi
DB_CACHE="$ROOT/.codex/plugins/cache/workspace/deepseek-balance/$DB_VERSION/.codex-plugin/plugin.json"
[[ -f "$DB_CACHE" ]] || die "deepseek-balance $DB_VERSION not installed; run without --check"
grep -q '^\[plugins\."deepseek-balance@workspace"\]' "$CONFIG" \
  || die "config.toml does not enable deepseek-balance@workspace"
ok "deepseek-balance plugin $DB_VERSION"

printf '== subagent-labels plugin\n'
SL_SRC="$ROOT/plugins/subagent-labels"
SL_STAGE="$ROOT/marketplace/plugins/subagent-labels"
[[ -f "$SL_SRC/.codex-plugin/plugin.json" ]] || die "$SL_SRC/.codex-plugin/plugin.json missing"
SL_VERSION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$SL_SRC/.codex-plugin/plugin.json")"
if (( CHECK_ONLY )); then
  [[ -d "$SL_STAGE" ]] || die "subagent-labels plugin is not staged; run without --check"
  if ! diff -qr "$SL_SRC" "$SL_STAGE" >/dev/null; then
    die "subagent-labels plugin stage differs from $SL_SRC; run without --check"
  fi
else
  rm -rf -- "$SL_STAGE"
  mkdir -p "$SL_STAGE"
  cp -a "$SL_SRC/." "$SL_STAGE/"
  CODEX_HOME="$ROOT/.codex" "$ROOT/node_modules/.bin/codex" plugin add subagent-labels@workspace >/dev/null
  node --test "$ROOT/plugins/subagent-labels/test/subagent-start.test.mjs" >/dev/null
fi
SL_CACHE="$ROOT/.codex/plugins/cache/workspace/subagent-labels/$SL_VERSION/.codex-plugin/plugin.json"
[[ -f "$SL_CACHE" ]] || die "subagent-labels $SL_VERSION not installed; run without --check"
grep -q '^\[plugins\."subagent-labels@workspace"\]' "$CONFIG" \
  || die "config.toml does not enable subagent-labels@workspace"
ok "subagent-labels plugin $SL_VERSION"

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
models=(deepseek-flash)
[[ -n "${ZAI_API_KEY:-}" && "$ZAI_API_KEY" != *'<'* ]] && models+=(glm-5.3)
for model in "${models[@]}"; do
  code="$(probe "$model")"
  [[ "$code" == 200 ]] && ok "$model -> HTTP $code" || warn "$model -> HTTP $code (see .codex/router.log)"
done

printf '\nReady: codex             (TUI, in the current folder)\n       codex exec "prompt" </dev/null\n'
