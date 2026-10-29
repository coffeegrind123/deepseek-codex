#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-update-test.XXXXXX")"
WORKSPACE="$TEST_ROOT/workspace"
FAKE_BIN="$TEST_ROOT/bin"
EVENTS="$TEST_ROOT/events"
FAKE_PID=""

cleanup() {
  stop_fake_codex
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

fail() {
  printf 'test_codex_update: %s\n' "$*" >&2
  exit 1
}

stop_fake_codex() {
  if [[ -n "$FAKE_PID" ]] && kill -0 "$FAKE_PID" 2>/dev/null; then
    kill -KILL "$FAKE_PID" 2>/dev/null || true
    wait "$FAKE_PID" 2>/dev/null || true
  fi
  FAKE_PID=""
}

write_fake_npm() {
  cat > "$FAKE_BIN/npm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  view)
    printf '%s\n' "${FAKE_LATEST:?}"
    ;;
  install)
    printf '%s\n' "$PWD" >> "$FAKE_EVENTS/install-cwd"
    if [[ "$PWD" == "$FAKE_WORKSPACE" ]]; then
      printf 'live install\n' >> "$FAKE_EVENTS/live-install"
      exit 91
    fi
    case "$PWD" in
      "$FAKE_WORKSPACE"/.codex-update-stage.*) ;;
      *)
        printf 'install outside workspace stage: %s\n' "$PWD" >> "$FAKE_EVENTS/outside-stage"
        exit 92
        ;;
    esac
    if [[ -n "${FAKE_ACTIVE_PID:-}" ]] && ! kill -0 "$FAKE_ACTIVE_PID" 2>/dev/null; then
      printf 'active process was stopped before staged install\n' >> "$FAKE_EVENTS/early-kill"
      exit 93
    fi
    if [[ "${FAKE_INSTALL_FAIL:-0}" == "1" ]]; then
      printf '{"private":true,"dependencies":{"@openai/codex":"corrupt"}}\n' > package.json
      mkdir -p node_modules/@openai/codex
      printf '{"version":"corrupt"}\n' > node_modules/@openai/codex/package.json
      exit 42
    fi

    version=""
    for arg in "$@"; do
      case "$arg" in
        "@openai/codex@"*) version="${arg##*@}" ;;
      esac
    done
    [[ -n "$version" ]] || exit 94
    printf '%s\n' "$version" > "$FAKE_EVENTS/installed-version"

    cat > package.json <<JSON
{
  "private": true,
  "dependencies": {
    "@openai/codex": "$version"
  }
}
JSON
    cat > package-lock.json <<JSON
{
  "name": "fixture",
  "lockfileVersion": 3,
  "packages": {
    "": {
      "dependencies": {
        "@openai/codex": "$version"
      }
    }
  }
}
JSON

    mkdir -p \
      node_modules/.bin \
      node_modules/@openai/codex \
      node_modules/@openai/codex-linux-x64/vendor/fake/bin
    cat > node_modules/@openai/codex/package.json <<JSON
{"version":"$version"}
JSON
    cat > node_modules/.bin/codex <<CLI
#!/usr/bin/env bash
printf 'codex-cli $version\\n'
CLI
    chmod +x node_modules/.bin/codex
    ;;
  test)
    printf '%s\n' "$PWD" >> "$FAKE_EVENTS/test-cwd"
    [[ "${FAKE_TEST_FAIL:-0}" != "1" ]] || exit 45
    ;;
  *)
    printf 'unexpected npm invocation: %s\n' "$*" >&2
    exit 2
    ;;
esac
EOF
  chmod +x "$FAKE_BIN/npm"
}

reset_workspace() {
  stop_fake_codex
  rm -rf "$WORKSPACE" "$EVENTS"
  mkdir -p \
    "$EVENTS" \
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

  cat > "$WORKSPACE/package-lock.json" <<'EOF'
{
  "name": "fixture",
  "lockfileVersion": 3,
  "packages": {
    "": {
      "dependencies": {
        "@openai/codex": "1.2.3"
      }
    }
  }
}
EOF

  cat > "$WORKSPACE/node_modules/@openai/codex/package.json" <<'EOF'
{
  "version": "1.2.3"
}
EOF

  cat > "$WORKSPACE/node_modules/.bin/codex" <<'EOF'
#!/usr/bin/env bash
printf 'codex-cli 1.2.3\n'
EOF
  chmod +x "$WORKSPACE/node_modules/.bin/codex"

  cat > "$WORKSPACE/codex" <<EOF
#!/usr/bin/env bash
exec "$WORKSPACE/node_modules/.bin/codex" "\$@"
EOF
  chmod +x "$WORKSPACE/codex"

  # This real path under node_modules makes the stop signal observable without
  # launching a real Codex session.
  cp "$(command -v sleep)" "$WORKSPACE/node_modules/@openai/codex-linux-x64/vendor/fake/bin/codex"
}

start_fake_codex() {
  "$WORKSPACE/node_modules/@openai/codex-linux-x64/vendor/fake/bin/codex" 300 &
  FAKE_PID="$!"
  disown "$FAKE_PID" 2>/dev/null || true
  sleep 0.05
  kill -0 "$FAKE_PID" 2>/dev/null || fail "fake Codex process did not start"
}

run_update() {
  PATH="$FAKE_BIN:$PATH" \
    CODEX_NPM_CACHE="$TEST_ROOT/cache" \
    FAKE_LATEST="${FAKE_LATEST:-1.2.3}" \
    FAKE_WORKSPACE="$WORKSPACE" \
    FAKE_EVENTS="$EVENTS" \
    FAKE_ACTIVE_PID="$FAKE_PID" \
    FAKE_INSTALL_FAIL="${FAKE_INSTALL_FAIL:-0}" \
    FAKE_TEST_FAIL="${FAKE_TEST_FAIL:-0}" \
    "$WORKSPACE/codex-update" "$@"
}

run_capture() {
  local input="$1"
  shift
  set +e
  OUTPUT="$(printf '%s' "$input" | run_update "$@" 2>&1)"
  STATUS="$?"
  set -e
}

live_snapshot() {
  {
    sha256sum "$WORKSPACE/package.json" "$WORKSPACE/package-lock.json"
    find "$WORKSPACE/node_modules" -type f -print0 \
      | sort -z \
      | xargs -0 sha256sum \
      | sed "s#  $WORKSPACE/##"
  } | sort
}

assert_live_unmodified() {
  local expected_snapshot="$1" current_snapshot
  current_snapshot="$(live_snapshot)"
  [[ "$current_snapshot" == "$expected_snapshot" ]] \
    || fail "live manifests or node_modules changed before promotion"
}

assert_no_stage_left() {
  local path
  for path in "$WORKSPACE"/.codex-update-stage.* "$WORKSPACE"/.codex-update-rollback.*; do
    [[ -e "$path" ]] || continue
    fail "temporary update directory was left behind: $path"
  done
}

assert_version() {
  local expected="$1" manifest installed cli wrapper lock
  manifest="$(node -e 'process.stdout.write(require(process.argv[1]).dependencies["@openai/codex"])' "$WORKSPACE/package.json")"
  installed="$(node -e 'process.stdout.write(require(process.argv[1]).version)' "$WORKSPACE/node_modules/@openai/codex/package.json")"
  lock="$(node -e 'process.stdout.write(require(process.argv[1]).packages[""].dependencies["@openai/codex"])' "$WORKSPACE/package-lock.json")"
  cli="$("$WORKSPACE/node_modules/.bin/codex" --version)"
  wrapper="$("$WORKSPACE/codex" --version)"
  [[ "$manifest" == "$expected" ]] || fail "package.json has $manifest, expected $expected"
  [[ "$installed" == "$expected" ]] || fail "node_modules has $installed, expected $expected"
  [[ "$lock" == "$expected" ]] || fail "package-lock.json has $lock, expected $expected"
  [[ "$cli" == "codex-cli $expected" ]] || fail "CLI reports '$cli', expected codex-cli $expected"
  [[ "$wrapper" == "codex-cli $expected" ]] || fail "wrapper reports '$wrapper', expected codex-cli $expected"
}

mkdir -p "$FAKE_BIN"
write_fake_npm

# A failed candidate install must leave active sessions and the live tree alone.
reset_workspace
start_fake_codex
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 FAKE_INSTALL_FAIL=1 run_capture $'yes\n' 1.3.0

[[ "$STATUS" -ne 0 ]] || fail "failed staged install unexpectedly succeeded"
[[ "$OUTPUT" == *"staged install failed"* ]] || fail "failed install did not report a staged failure"
[[ "$OUTPUT" != *"Kill these Codex processes"* ]] || fail "failed install prompted to stop sessions"
[[ ! -e "$EVENTS/live-install" ]] || fail "candidate install touched the live workspace"
[[ ! -e "$EVENTS/outside-stage" ]] || fail "candidate install ran outside the workspace staging area"
[[ ! -e "$EVENTS/early-kill" ]] || fail "active session was stopped before the staged install"
kill -0 "$FAKE_PID" 2>/dev/null || fail "failed staged install killed the active session"
assert_live_unmodified "$before"
assert_no_stage_left

# Explicit versions are staged, verified, and promoted without querying latest
# as the target.
reset_workspace
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 run_capture '' 1.3.0

[[ "$STATUS" -eq 0 ]] || fail "exact-version update failed with status $STATUS: $OUTPUT"
assert_version 1.3.0
[[ "$(cat "$EVENTS/installed-version")" == "1.3.0" ]] || fail "exact version was not installed"
[[ ! -e "$EVENTS/live-install" ]] || fail "exact-version install touched the live workspace"
assert_no_stage_left

# Latest resolves through npm, stages while the active process runs, then stops
# it only after staged verification and immediately before promotion.
reset_workspace
start_fake_codex
FAKE_LATEST=2.0.0 run_capture $'yes\n'

[[ "$STATUS" -eq 0 ]] || fail "latest update failed with status $STATUS: $OUTPUT"
[[ "$OUTPUT" == *"Installing @openai/codex@2.0.0"* ]] || fail "latest target was not resolved"
[[ "$OUTPUT" == *"Staged install verified: codex-cli 2.0.0"* ]] \
  || fail "staged CLI was not verified before promotion"
[[ "$OUTPUT" == *"Kill these Codex processes automatically and continue? [y/N]"* ]] \
  || fail "latest update did not prompt before promotion"
[[ "$OUTPUT" != *"Kill these Codex processes"*"Installing"* ]] \
  || fail "latest update prompted before installing the candidate"
[[ "$OUTPUT" == *"Codex processes stopped."* ]] || fail "latest update did not stop the process"
[[ "$OUTPUT" == *"Promoting staged Codex installation"* ]] || fail "update did not promote the stage"
[[ "$OUTPUT" == *"Updated and verified: codex-cli 2.0.0"* ]] || fail "update did not verify the promoted install"
[[ "$(cat "$EVENTS/installed-version")" == "2.0.0" ]] || fail "latest version was not installed"
[[ ! -e "$EVENTS/live-install" ]] || fail "latest install touched the live workspace"
[[ ! -e "$EVENTS/outside-stage" ]] || fail "latest install ran outside the workspace staging area"
[[ ! -e "$EVENTS/early-kill" ]] || fail "latest update stopped the active process before staging"
assert_version 2.0.0
assert_no_stage_left

# Declining the stop prompt leaves sessions and the live tree unchanged.
reset_workspace
start_fake_codex
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 run_capture $'no\n'

[[ "$STATUS" -ne 0 ]] || fail "no response unexpectedly continued the update"
[[ "$OUTPUT" == *"update cancelled"* ]] || fail "no response did not report cancellation"
kill -0 "$FAKE_PID" 2>/dev/null || fail "cancelled update killed the active session"
assert_live_unmodified "$before"
assert_no_stage_left

# A failure after promotion restores the previous manifests and node_modules.
reset_workspace
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 FAKE_TEST_FAIL=1 run_capture '' 1.3.0

[[ "$STATUS" -ne 0 ]] || fail "post-promotion verification failure unexpectedly succeeded"
[[ "$OUTPUT" == *"live installation was rolled back"* ]] \
  || fail "post-promotion failure did not report a rollback"
assert_live_unmodified "$before"
assert_no_stage_left

# --check remains read-only and reports against latest.
reset_workspace
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 run_capture '' --check

[[ "$STATUS" -eq 0 ]] || fail "--check failed with status $STATUS: $OUTPUT"
[[ "$OUTPUT" == *"Status:       update available; run ./codex update"* ]] \
  || fail "--check did not report an available update"
[[ ! -e "$EVENTS/install-cwd" ]] || fail "--check attempted an install"
assert_live_unmodified "$before"

printf 'test_codex_update: ok\n'
