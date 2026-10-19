#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-update-test.XXXXXX")"
WORKSPACE="$TEST_ROOT/workspace"
FAKE_BIN="$TEST_ROOT/bin"
FAKE_PID=""

cleanup() {
  if [[ -n "$FAKE_PID" ]] && kill -0 "$FAKE_PID" 2>/dev/null; then
    kill -KILL "$FAKE_PID" 2>/dev/null || true
  fi
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

fail() {
  printf 'test_codex_update: %s\n' "$*" >&2
  exit 1
}

mkdir -p \
  "$FAKE_BIN" \
  "$WORKSPACE/node_modules/.bin" \
  "$WORKSPACE/node_modules/@openai/codex" \
  "$WORKSPACE/node_modules/@openai/codex-linux-x64/vendor/fake/bin"

cp "$SOURCE_ROOT/codex-update" "$WORKSPACE/codex-update"
chmod +x "$WORKSPACE/codex-update"

cat > "$WORKSPACE/package.json" <<'EOF'
{
  "private": true,
  "dependencies": {
    "@openai/codex": "1.2.3"
  }
}
EOF

cat > "$WORKSPACE/node_modules/@openai/codex/package.json" <<'EOF'
{
  "version": "1.2.3"
}
EOF

cat > "$FAKE_BIN/npm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  view)
    printf '1.2.3\n'
    ;;
  test)
    exit 0
    ;;
  install)
    exit 0
    ;;
  *)
    printf 'unexpected npm invocation: %s\n' "$*" >&2
    exit 2
    ;;
esac
EOF
chmod +x "$FAKE_BIN/npm"

cat > "$WORKSPACE/node_modules/.bin/codex" <<'EOF'
#!/usr/bin/env bash
printf 'codex-cli 1.2.3\n'
EOF
chmod +x "$WORKSPACE/node_modules/.bin/codex"

cat > "$WORKSPACE/codex" <<'EOF'
#!/usr/bin/env bash
printf 'codex-cli 1.2.3\n'
EOF
chmod +x "$WORKSPACE/codex"

# A real path under node_modules/@openai makes this indistinguishable from the
# native Codex process to the updater, without starting an actual Codex session.
cp "$(command -v sleep)" "$WORKSPACE/node_modules/@openai/codex-linux-x64/vendor/fake/bin/codex"

run_update() {
  PATH="$FAKE_BIN:$PATH" CODEX_NPM_CACHE="$TEST_ROOT/cache" "$WORKSPACE/codex-update"
}

"$WORKSPACE/node_modules/@openai/codex-linux-x64/vendor/fake/bin/codex" 300 &
FAKE_PID="$!"
disown "$FAKE_PID" 2>/dev/null || true
sleep 0.05

set +e
yes_output="$(printf 'yes\n' | run_update 2>&1)"
yes_status="$?"
set -e

[[ "$yes_status" -eq 0 ]] || fail "yes response failed with status $yes_status: $yes_output"
[[ "$yes_output" == *"Kill these Codex processes automatically and continue? [y/N]"* ]] \
  || fail "yes response did not show the confirmation prompt"
[[ "$yes_output" == *"Codex processes stopped."* ]] || fail "yes response did not stop the process"
[[ "$yes_output" == *"Updated and verified: codex-cli 1.2.3"* ]] \
  || fail "yes response did not complete the update"
if kill -0 "$FAKE_PID" 2>/dev/null; then
  fail "yes response left the fake Codex process running"
fi
FAKE_PID=""

"$WORKSPACE/node_modules/@openai/codex-linux-x64/vendor/fake/bin/codex" 300 &
FAKE_PID="$!"
disown "$FAKE_PID" 2>/dev/null || true
sleep 0.05

set +e
no_output="$(printf 'no\n' | run_update 2>&1)"
no_status="$?"
set -e

[[ "$no_status" -ne 0 ]] || fail "no response unexpectedly continued the update"
[[ "$no_output" == *"update cancelled"* ]] || fail "no response did not report cancellation"
kill -0 "$FAKE_PID" 2>/dev/null || fail "no response killed the fake Codex process"

printf 'test_codex_update: ok\n'
