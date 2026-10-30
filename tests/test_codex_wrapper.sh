#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-wrapper-test.XXXXXX")"
ROOT="$TEST_ROOT/workspace"

cleanup() {
  rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

fail() {
  printf 'test_codex_wrapper: %s\n' "$*" >&2
  exit 1
}

write_executable() {
  local path="$1"
  local version="$2"
  mkdir -p "$(dirname "$path")"
  printf '#!/usr/bin/env bash\nprintf "codex-cli %s\\n"\n' "$version" >"$path"
  chmod +x "$path"
}

expect_path() {
  local expected="$1"
  local actual
  actual="$("$ROOT/scripts/resolve-codex-bin.sh")"
  [[ "$actual" == "$expected" ]] \
    || fail "resolver returned '$actual', expected '$expected'"
}

mkdir -p "$ROOT/scripts" "$ROOT/node_modules/.bin" "$ROOT/tools/codex-local" "$TEST_ROOT/override"
cp "$SOURCE_ROOT/scripts/resolve-codex-bin.sh" "$ROOT/scripts/resolve-codex-bin.sh"
chmod +x "$ROOT/scripts/resolve-codex-bin.sh"
cp "$SOURCE_ROOT/package.json" "$ROOT/package.json"

NPM_BIN="$ROOT/node_modules/@openai/codex/bin/codex.js"
write_executable "$NPM_BIN" "0.166.0"
ln -s "../@openai/codex/bin/codex.js" "$ROOT/node_modules/.bin/codex"
expect_path "$NPM_BIN"

write_executable "$ROOT/tools/codex-local/releases/0.166.0-patched/codex" "0.166.0"
ln -s "releases/0.166.0-patched" "$ROOT/tools/codex-local/current"
expect_path "$ROOT/tools/codex-local/releases/0.166.0-patched/codex"

OVERRIDE="$TEST_ROOT/override/codex"
write_executable "$OVERRIDE" "9.9.9"
set +e
CODEX_WORKSPACE_BIN="$OVERRIDE" "$ROOT/scripts/resolve-codex-bin.sh" >/dev/null 2>&1
STATUS="$?"
set -e
[[ "$STATUS" -ne 0 ]] || fail "version-mismatched explicit override was accepted"

set +e
CODEX_WORKSPACE_BIN="$TEST_ROOT/missing-codex" "$ROOT/scripts/resolve-codex-bin.sh" >/dev/null 2>&1
STATUS="$?"
set -e
[[ "$STATUS" -ne 0 ]] || fail "missing explicit override did not fail closed"

rm -f "$ROOT/tools/codex-local/current"
ln -s "releases/missing" "$ROOT/tools/codex-local/current"
set +e
"$ROOT/scripts/resolve-codex-bin.sh" >/dev/null 2>&1
STATUS="$?"
set -e
[[ "$STATUS" -ne 0 ]] || fail "broken local current silently fell back to npm"

write_executable "$OVERRIDE" "0.166.0"
ACTUAL="$(CODEX_WORKSPACE_BIN="$OVERRIDE" "$ROOT/scripts/resolve-codex-bin.sh")"
[[ "$ACTUAL" == "$OVERRIDE" ]] \
  || fail "valid explicit override was not preferred"

write_executable "$OVERRIDE" "9.9.9"
ACTUAL="$(CODEX_BIN_EXPECTED_VERSION=9.9.9 CODEX_WORKSPACE_BIN="$OVERRIDE" \
  "$ROOT/scripts/resolve-codex-bin.sh")"
[[ "$ACTUAL" == "$OVERRIDE" ]] \
  || fail "expected-version override was not honored"

set +e
CODEX_WORKSPACE_BIN=/usr/bin/true "$ROOT/scripts/resolve-codex-bin.sh" >/dev/null 2>&1
STATUS="$?"
set -e
[[ "$STATUS" -ne 0 ]] || fail "non-Codex explicit override was accepted"

[[ -x "$SOURCE_ROOT/scripts/deepseek-balance.mjs" ]] \
  || fail "footer status helper is not executable"
grep -Fqx 'command = ["deepseek-balance.mjs", "status"]' "$SOURCE_ROOT/.codex/config.toml" \
  || fail "footer status command depends on a checkout-specific path"
grep -Fq 'balance_args+=(--no-title)' "$SOURCE_ROOT/codex" \
  || fail "disabling the title also disables footer cache refresh"

printf 'codex wrapper resolver: ok\n'
