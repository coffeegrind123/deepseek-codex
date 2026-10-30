#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-update-test.XXXXXX")"
WORKSPACE="$TEST_ROOT/workspace"
FAKE_BIN="$TEST_ROOT/bin"
EVENTS="$TEST_ROOT/events"
EXTERNAL_RUNTIME="$TEST_ROOT/external-runtime"
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
    kill -TERM "$FAKE_PID" 2>/dev/null || true
    wait "$FAKE_PID" 2>/dev/null || true
  fi
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
    [[ "$PWD" != "$FAKE_WORKSPACE" || "${FAKE_TEST_FAIL:-0}" != "1" ]] || exit 45
    ;;
  *)
    printf 'unexpected npm invocation: %s\n' "$*" >&2
    exit 2
    ;;
esac
EOF
  chmod +x "$FAKE_BIN/npm"
}

write_runtime_release() {
  local target="$1"
  local patch_id="$2"
  local binary_version="${3:-$target}"
  local build_target="${4:-$target}"
  local release="$WORKSPACE/tools/codex-local/releases/$target-$patch_id"
  local executable_sha

  mkdir -p "$release"
  cat > "$release/codex" <<EOF
#!/usr/bin/env bash
printf 'codex-cli $binary_version\n'
EOF
  chmod +x "$release/codex"
  executable_sha="$(sha256sum "$release/codex" | awk '{print $1}')"
  cat > "$release/build.json" <<EOF
{"target":"$build_target","patch_sha":"fixture-patch-sha","binary":"codex","executable_sha":"$executable_sha"}
EOF
}

publish_local_runtime() {
  local target="$1"
  local patch_id="$2"
  local binary_version="${3:-$target}"
  local build_target="${4:-$target}"

  write_runtime_release "$target" "$patch_id" "$binary_version" "$build_target"
  rm -f "$WORKSPACE/tools/codex-local/current"
  ln -s "releases/$target-$patch_id" "$WORKSPACE/tools/codex-local/current"
}

write_workspace_scripts() {
  mkdir -p "$WORKSPACE/scripts"

  cat > "$WORKSPACE/scripts/resolve-codex-bin.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${CODEX_WORKSPACE_BIN:-}" ]]; then
  realpath -e -- "$CODEX_WORKSPACE_BIN"
  exit 0
fi
if [[ -e "$ROOT/tools/codex-local/current" || -L "$ROOT/tools/codex-local/current" ]]; then
  if [[ -x "$ROOT/tools/codex-local/current/codex" ]]; then
    readlink -f "$ROOT/tools/codex-local/current/codex"
    exit 0
  fi
  printf 'broken local current\n' >&2
  exit 1
fi
if [[ -x "$ROOT/node_modules/.bin/codex" ]]; then
  readlink -f "$ROOT/node_modules/.bin/codex"
  exit 0
fi
printf 'no Codex executable found\n' >&2
exit 1
EOF

  cat > "$WORKSPACE/scripts/build-codex-local.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EMPTY_DIR="$ROOT/tools/codex-local"
CURRENT="$EMPTY_DIR/current"
if [[ "${1:-}" == "--print-patch-sha" ]]; then
  [[ -n "${2:-}" ]] || exit 2
  printf 'fixture-patch-sha\n'
  exit 0
fi
target="${1:?target required}"
printf '%s\n' "$target" >> "$FAKE_EVENTS/local-build"

if [[ "${FAKE_LOCAL_BUILD_FAIL:-0}" == "1" ]]; then
  ln -sfn "releases/broken-fixture" "$CURRENT"
  exit 40
fi

patch_id="fixture"
release_name="$target-$patch_id"
release="$EMPTY_DIR/releases/$release_name"
build_target="${FAKE_LOCAL_META_VERSION:-$target}"
binary_version="${FAKE_LOCAL_BINARY_VERSION:-$target}"
mktemp -d "$release.tmp.XXXXXX" >/dev/null
temporary_release="$(find "$EMPTY_DIR/releases" -maxdepth 1 -type d -name "$release_name.tmp.*" | head -n 1)"
[[ -n "$temporary_release" ]] || exit 41
cat > "$temporary_release/codex" <<CLI
#!/usr/bin/env bash
printf 'codex-cli $binary_version\\n'
CLI
chmod +x "$temporary_release/codex"
executable_sha="$(sha256sum "$temporary_release/codex" | awk '{print $1}')"
cat > "$temporary_release/build.json" <<JSON
{"target":"$build_target","patch_sha":"fixture-patch-sha","binary":"codex","executable_sha":"$executable_sha"}
JSON
rm -rf "$release"
mv "$temporary_release" "$release"

temporary_link="$EMPTY_DIR/.current.$$"
ln -s "releases/$release_name" "$temporary_link"
mv -Tf "$temporary_link" "$CURRENT"
EOF

  chmod +x \
    "$WORKSPACE/scripts/resolve-codex-bin.sh" \
    "$WORKSPACE/scripts/build-codex-local.sh"
}

reset_workspace() {
  stop_fake_codex
  rm -rf "$WORKSPACE" "$EVENTS" "$EXTERNAL_RUNTIME"
  mkdir -p \
    "$EVENTS" \
    "$WORKSPACE/node_modules/.bin" \
    "$WORKSPACE/node_modules/@openai/codex" \
    "$WORKSPACE/node_modules/@openai/codex-linux-x64/vendor/fake/bin" \
    "$WORKSPACE/tools/codex-local/releases"

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

  write_workspace_scripts
  publish_local_runtime 1.2.3 baseline

  # Read the resolved binary before exec so the fixture behaves like the real wrapper.
  cat > "$WORKSPACE/codex" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$("$ROOT/scripts/resolve-codex-bin.sh")"
exec "$BIN" "$@"
EOF
  chmod +x "$WORKSPACE/codex"

  # A native executable makes local-active process detection observable.
  cp "$(command -v sleep)" "$WORKSPACE/tools/codex-local/releases/1.2.3-baseline/active-codex"
}

create_external_runtime() {
  local version="$1"
  mkdir -p "$EXTERNAL_RUNTIME"
  cat > "$EXTERNAL_RUNTIME/codex" <<EOF
#!/usr/bin/env bash
set -euo pipefail
case "\${1:-}" in
  --version)
    printf 'codex-cli $version\n'
    ;;
  serve)
    trap 'kill "\$child" 2>/dev/null || true; wait "\$child" 2>/dev/null || true; exit 0' TERM INT
    sleep 300 &
    child=\$!
    wait "\$child"
    ;;
  *)
    exit 2
    ;;
esac
EOF
  chmod +x "$EXTERNAL_RUNTIME/codex"
}

start_fake_codex() {
  local runtime="$1"
  shift

  "$runtime" "$@" &
  FAKE_PID="$!"
  disown "$FAKE_PID" 2>/dev/null || true
  sleep 0.05
  kill -0 "$FAKE_PID" 2>/dev/null || fail "fake Codex process did not start: $runtime"
}

start_local_codex() {
  start_fake_codex "$WORKSPACE/tools/codex-local/releases/1.2.3-baseline/active-codex" 300
}

start_external_codex() {
  start_fake_codex "$EXTERNAL_RUNTIME/codex" serve
}

run_update() {
  PATH="$FAKE_BIN:$PATH" \
    CODEX_NPM_CACHE="$TEST_ROOT/cache" \
    CODEX_WORKSPACE_BIN="${TEST_WORKSPACE_BIN:-}" \
    FAKE_LATEST="${FAKE_LATEST:-1.2.3}" \
    FAKE_WORKSPACE="$WORKSPACE" \
    FAKE_EVENTS="$EVENTS" \
    FAKE_ACTIVE_PID="$FAKE_PID" \
    FAKE_INSTALL_FAIL="${FAKE_INSTALL_FAIL:-0}" \
    FAKE_TEST_FAIL="${FAKE_TEST_FAIL:-0}" \
    FAKE_LOCAL_BUILD_FAIL="${FAKE_LOCAL_BUILD_FAIL:-0}" \
    FAKE_LOCAL_META_VERSION="${FAKE_LOCAL_META_VERSION:-}" \
    FAKE_LOCAL_BINARY_VERSION="${FAKE_LOCAL_BINARY_VERSION:-}" \
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

local_snapshot() {
  if [[ -e "$WORKSPACE/tools/codex-local/current" || -L "$WORKSPACE/tools/codex-local/current" ]]; then
    readlink "$WORKSPACE/tools/codex-local/current"
    sha256sum \
      "$WORKSPACE/tools/codex-local/current/codex" \
      "$WORKSPACE/tools/codex-local/current/build.json"
  else
    printf 'local current missing\n'
  fi
}

live_snapshot() {
  {
    sha256sum "$WORKSPACE/package.json" "$WORKSPACE/package-lock.json"
    find "$WORKSPACE/node_modules" -type f -print0 \
      | sort -z \
      | xargs -0 sha256sum \
      | sed "s#  $WORKSPACE/##"
    local_snapshot | sed "s#  $WORKSPACE/##"
  } | sort
}

assert_live_unmodified() {
  local expected_snapshot="$1" current_snapshot
  current_snapshot="$(live_snapshot)"
  [[ "$current_snapshot" == "$expected_snapshot" ]] \
    || fail "live npm or local runtime changed before promotion"
}

assert_no_stage_left() {
  local path
  for path in \
    "$WORKSPACE"/.codex-update-stage.* \
    "$WORKSPACE"/.codex-update-rollback.* \
    "$WORKSPACE"/tools/codex-local/.current.* \
    "$WORKSPACE"/tools/codex-local/current.pending.*; do
    [[ -e "$path" || -L "$path" ]] || continue
    fail "temporary update path was left behind: $path"
  done
}

assert_version() {
  local expected="$1"
  local external="${2:-}"
  local manifest installed build cli wrapper local_cli resolved

  manifest="$(node -e 'process.stdout.write(require(process.argv[1]).dependencies["@openai/codex"])' "$WORKSPACE/package.json")"
  installed="$(node -e 'process.stdout.write(require(process.argv[1]).version)' "$WORKSPACE/node_modules/@openai/codex/package.json")"
  build="$(node -e 'const b=require(process.argv[1]);process.stdout.write(b.target)' "$WORKSPACE/tools/codex-local/current/build.json")"
  cli="$("$WORKSPACE/node_modules/.bin/codex" --version)"
  local_cli="$("$WORKSPACE/tools/codex-local/current/codex" --version)"
  wrapper="$(CODEX_WORKSPACE_BIN="$external" "$WORKSPACE/codex" --version)"
  resolved="$(CODEX_WORKSPACE_BIN="$external" "$WORKSPACE/scripts/resolve-codex-bin.sh")"

  [[ "$manifest" == "$expected" ]] || fail "package.json has $manifest, expected $expected"
  [[ "$installed" == "$expected" ]] || fail "node_modules has $installed, expected $expected"
  [[ "$build" == "$expected" ]] || fail "local build records $build, expected $expected"
  [[ "$cli" == "codex-cli $expected" ]] || fail "npm CLI reports '$cli', expected codex-cli $expected"
  [[ "$local_cli" == "codex-cli $expected" ]] || fail "local CLI reports '$local_cli', expected codex-cli $expected"
  [[ "$wrapper" == "codex-cli $expected" ]] || fail "wrapper reports '$wrapper', expected codex-cli $expected"
  if [[ -n "$external" ]]; then
    [[ "$resolved" == "$(readlink -f "$external")" ]] \
      || fail "resolver selected $resolved, expected external runtime $external"
  else
    [[ "$resolved" == "$(readlink -f "$WORKSPACE/tools/codex-local/current/codex")" ]] \
      || fail "resolver selected $resolved instead of local current"
  fi
}

mkdir -p "$FAKE_BIN"
write_fake_npm

# Resolver precedence is external override, local current, then npm.
reset_workspace
local_resolved="$("$WORKSPACE/scripts/resolve-codex-bin.sh")"
[[ "$local_resolved" == "$(readlink -f "$WORKSPACE/tools/codex-local/current/codex")" ]] \
  || fail "resolver did not select local current"
create_external_runtime 2.0.0
external_resolved="$(CODEX_WORKSPACE_BIN="$EXTERNAL_RUNTIME/codex" "$WORKSPACE/scripts/resolve-codex-bin.sh")"
[[ "$external_resolved" == "$(readlink -f "$EXTERNAL_RUNTIME/codex")" ]] \
  || fail "resolver ignored CODEX_WORKSPACE_BIN"

# --check reports both runtimes and never treats a binary/version mismatch as OK.
reset_workspace
cat > "$WORKSPACE/tools/codex-local/current/codex" <<'EOF'
#!/usr/bin/env bash
printf 'codex-cli 1.2.4\n'
EOF
chmod +x "$WORKSPACE/tools/codex-local/current/codex"
before="$(live_snapshot)"
FAKE_LATEST=1.2.3 run_capture '' --check
[[ "$STATUS" -ne 0 ]] || fail "--check accepted a local binary/build version mismatch"
[[ "$OUTPUT" == *"NPM status:"* ]] || fail "--check did not report npm status"
[[ "$OUTPUT" == *"Local status:  inconsistent"* ]] || fail "--check did not report local inconsistency"
[[ "$OUTPUT" == *"Status:       inconsistent; run ./codex update"* ]] \
  || fail "--check did not expose the mismatch"
[[ ! -e "$EVENTS/install-cwd" ]] || fail "--check attempted an npm install"
[[ ! -e "$EVENTS/local-build" ]] || fail "--check attempted a local build"
assert_live_unmodified "$before"

# A failed staged install must leave active sessions and both live runtimes alone.
reset_workspace
start_local_codex
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 FAKE_INSTALL_FAIL=1 run_capture $'yes\n' 1.3.0
[[ "$STATUS" -ne 0 ]] || fail "failed staged install unexpectedly succeeded"
[[ "$OUTPUT" == *"staged install failed"* ]] || fail "failed install did not report a staged failure"
[[ "$OUTPUT" != *"Kill these Codex processes"* ]] || fail "failed install prompted to stop sessions"
[[ ! -e "$EVENTS/live-install" ]] || fail "candidate install touched the live workspace"
[[ ! -e "$EVENTS/outside-stage" ]] || fail "candidate install ran outside the staging area"
[[ ! -e "$EVENTS/early-kill" ]] || fail "active session was stopped before the staged install"
[[ ! -e "$EVENTS/local-build" ]] || fail "local runtime was built after npm staging failed"
kill -0 "$FAKE_PID" 2>/dev/null || fail "failed staged install killed the active session"
assert_live_unmodified "$before"
assert_no_stage_left

# A patch candidate with a mismatched binary is rejected while the prior current stays live.
reset_workspace
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 FAKE_LOCAL_BINARY_VERSION=9.9.9 run_capture '' 1.3.0
[[ "$STATUS" -ne 0 ]] || fail "mismatched local candidate unexpectedly succeeded"
[[ "$OUTPUT" == *"candidate local Codex executable reports 'codex-cli 9.9.9'"* ]] \
  || fail "candidate version mismatch was not reported: $OUTPUT"
[[ "$OUTPUT" == *"prior current remains active"* ]] \
  || fail "candidate failure did not preserve the prior current"
assert_live_unmodified "$before"
assert_no_stage_left

# A builder that corrupts current before failing is rolled back before the prompt.
reset_workspace
start_local_codex
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 FAKE_LOCAL_BUILD_FAIL=1 run_capture $'yes\n' 1.3.0
[[ "$STATUS" -ne 0 ]] || fail "failed local build unexpectedly succeeded"
[[ "$OUTPUT" == *"candidate build failed; the prior current was restored"* ]] \
  || fail "failed local build did not report restoring current"
[[ "$OUTPUT" != *"Kill these Codex processes"* ]] || fail "failed local build prompted before validation"
kill -0 "$FAKE_PID" 2>/dev/null || fail "failed local build killed the active session"
assert_live_unmodified "$before"
assert_no_stage_left

# Explicit versions stage npm and the patched runtime, then promote both.
reset_workspace
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 run_capture '' 1.3.0
[[ "$STATUS" -eq 0 ]] || fail "exact-version update failed with status $STATUS: $OUTPUT"
[[ "$OUTPUT" == *"Staged npm install verified: codex-cli 1.3.0"* ]] \
  || fail "staged npm candidate was not verified"
[[ "$OUTPUT" == *"Patched local runtime candidate verified: codex-cli 1.3.0"* ]] \
  || fail "local runtime candidate was not verified"
[[ "$(cat "$EVENTS/installed-version")" == "1.3.0" ]] || fail "exact npm version was not installed"
[[ "$(cat "$EVENTS/local-build")" == "1.3.0" ]] || fail "exact local target was not built"
[[ ! -e "$EVENTS/live-install" ]] || fail "exact-version install touched the live workspace"
assert_version 1.3.0
assert_no_stage_left

# Latest resolves through npm, stages while the local process runs, then stops it
# only after both candidate checks and immediately before promotion.
reset_workspace
start_local_codex
FAKE_LATEST=2.0.0 run_capture $'yes\n'
[[ "$STATUS" -eq 0 ]] || fail "latest update failed with status $STATUS: $OUTPUT"
[[ "$OUTPUT" == *"Installing @openai/codex@2.0.0"* ]] || fail "latest target was not resolved"
[[ "$OUTPUT" == *"Kill these Codex processes automatically and continue? [y/N]"* ]] \
  || fail "latest update did not prompt before promotion"
[[ "$OUTPUT" != *"Kill these Codex processes"*"Installing"* ]] \
  || fail "latest update prompted before installing the candidate"
[[ "$OUTPUT" == *"Codex processes stopped."* ]] || fail "latest update did not stop the process"
[[ "$OUTPUT" == *"Promoting staged Codex installation"* ]] || fail "update did not promote"
[[ "$OUTPUT" == *"Updated and verified: codex-cli 2.0.0"* ]] \
  || fail "update did not verify the promoted runtime"
[[ ! -e "$EVENTS/early-kill" ]] || fail "latest update stopped the local process before candidate checks"
assert_version 2.0.0
assert_no_stage_left

# External runtimes resolved by CODEX_WORKSPACE_BIN are active processes too.
reset_workspace
create_external_runtime 2.0.0
start_external_codex
TEST_WORKSPACE_BIN="$EXTERNAL_RUNTIME/codex" FAKE_LATEST=2.0.0 run_capture $'yes\n'
[[ "$STATUS" -eq 0 ]] || fail "external-runtime update failed with status $STATUS: $OUTPUT"
[[ "$OUTPUT" == *"Active Codex processes for this workspace:"*"$EXTERNAL_RUNTIME/codex"* ]] \
  || fail "active-process detection omitted the external Codex executable"
[[ "$OUTPUT" == *"Codex processes stopped."* ]] || fail "external runtime process was not stopped"
assert_version 2.0.0 "$EXTERNAL_RUNTIME/codex"
assert_no_stage_left

# Declining after candidate validation leaves the prior npm and local current active.
reset_workspace
start_local_codex
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 run_capture $'no\n'
[[ "$STATUS" -ne 0 ]] || fail "declined update unexpectedly continued"
[[ "$OUTPUT" == *"update cancelled"* ]] || fail "declined update did not report cancellation"
[[ "$OUTPUT" == *"Patched local runtime candidate verified: codex-cli 2.0.0"* ]] \
  || fail "decline happened before candidate validation"
kill -0 "$FAKE_PID" 2>/dev/null || fail "declined update killed the active session"
assert_live_unmodified "$before"
assert_no_stage_left

# Post-promotion verification failure rolls back npm and local current together,
# while the separately resolved external runtime remains untouched.
reset_workspace
create_external_runtime 1.3.0
before="$(live_snapshot)"
TEST_WORKSPACE_BIN="$EXTERNAL_RUNTIME/codex" \
  FAKE_LATEST=2.0.0 FAKE_TEST_FAIL=1 run_capture '' 1.3.0
[[ "$STATUS" -ne 0 ]] || fail "post-promotion verification failure unexpectedly succeeded"
[[ "$OUTPUT" == *"npm and the local current were rolled back"* ]] \
  || fail "post-promotion failure did not report transactional rollback"
assert_live_unmodified "$before"
[[ "$("$EXTERNAL_RUNTIME/codex" --version)" == "codex-cli 1.3.0" ]] \
  || fail "external runtime changed during npm/local rollback"
assert_no_stage_left

# A mismatched external runtime is rejected before either live runtime changes.
reset_workspace
create_external_runtime 1.9.0
before="$(live_snapshot)"
TEST_WORKSPACE_BIN="$EXTERNAL_RUNTIME/codex" FAKE_LATEST=2.0.0 run_capture '' 2.0.0
[[ "$STATUS" -ne 0 ]] || fail "mismatched external runtime unexpectedly succeeded"
[[ "$OUTPUT" == *"external active Codex executable reports 'codex-cli 1.9.0'"* ]] \
  || fail "external runtime mismatch was not reported"
[[ "$OUTPUT" == *"no live files were promoted"* ]] \
  || fail "external runtime mismatch did not stop before promotion"
assert_live_unmodified "$before"
assert_no_stage_left

# When npm is already at target, only the stale local candidate is rebuilt.
reset_workspace
local_sha="$(sha256sum "$WORKSPACE/tools/codex-local/current/codex" | awk '{print $1}')"
cat > "$WORKSPACE/tools/codex-local/current/build.json" <<EOF
{"target":"0.0.1","patch_sha":"stale","binary":"codex","executable_sha":"$local_sha"}
EOF
FAKE_LATEST=1.2.3 run_capture '' 1.2.3
[[ "$STATUS" -eq 0 ]] || fail "local-only repair failed with status $STATUS: $OUTPUT"
[[ ! -e "$EVENTS/install-cwd" ]] || fail "local-only repair reinstalled matching npm"
[[ "$(cat "$EVENTS/local-build")" == "1.2.3" ]] || fail "local-only repair did not build local target"
assert_version 1.2.3
assert_no_stage_left

# A patch-series change at the same Codex version invalidates the local build.
reset_workspace
local_sha="$(sha256sum "$WORKSPACE/tools/codex-local/current/codex" | awk '{print $1}')"
cat > "$WORKSPACE/tools/codex-local/current/build.json" <<EOF
{"target":"1.2.3","patch_sha":"stale","binary":"codex","executable_sha":"$local_sha"}
EOF
FAKE_LATEST=1.2.3 run_capture '' 1.2.3
[[ "$STATUS" -eq 0 ]] || fail "same-version patch repair failed with status $STATUS: $OUTPUT"
[[ ! -e "$EVENTS/install-cwd" ]] || fail "same-version patch repair reinstalled matching npm"
[[ "$(cat "$EVENTS/local-build")" == "1.2.3" ]] \
  || fail "same-version patch repair reused the stale local runtime"
assert_version 1.2.3
assert_no_stage_left

# No build is required when npm and the local target already match.
reset_workspace
FAKE_LATEST=2.0.0 run_capture '' 1.2.3
[[ "$STATUS" -eq 0 ]] || fail "already-matching update failed with status $STATUS: $OUTPUT"
[[ "$OUTPUT" == *"Dependencies and local runtime already match 1.2.3"* ]] \
  || fail "already-matching update did not take the verification-only path"
[[ ! -e "$EVENTS/install-cwd" ]] || fail "already-matching update reinstalled npm"
[[ ! -e "$EVENTS/local-build" ]] || fail "already-matching update rebuilt local runtime"
assert_version 1.2.3

# --check remains read-only and reports npm plus local runtime status.
reset_workspace
before="$(live_snapshot)"
FAKE_LATEST=2.0.0 run_capture '' --check
[[ "$STATUS" -eq 0 ]] || fail "--check failed with status $STATUS: $OUTPUT"
[[ "$OUTPUT" == *"NPM status:    update required for 2.0.0"* ]] \
  || fail "--check did not report npm status"
[[ "$OUTPUT" == *"Local status:  update required for 2.0.0"* ]] \
  || fail "--check did not report local status"
[[ "$OUTPUT" == *"Status:       update available; run ./codex update"* ]] \
  || fail "--check did not report an available update"
[[ ! -e "$EVENTS/install-cwd" ]] || fail "--check attempted an npm install"
[[ ! -e "$EVENTS/local-build" ]] || fail "--check attempted a local build"
assert_live_unmodified "$before"

# --check diagnoses a broken local current instead of failing during resolution.
reset_workspace
rm -f "$WORKSPACE/tools/codex-local/current"
ln -s "releases/missing" "$WORKSPACE/tools/codex-local/current"
FAKE_LATEST=1.2.3 run_capture '' --check
[[ "$STATUS" -ne 0 ]] || fail "--check accepted a broken local current"
[[ "$OUTPUT" == *"Active binary: unavailable"* ]] \
  || fail "--check did not report resolver failure"
[[ "$OUTPUT" == *"NPM status:    inconsistent"* ]] \
  || fail "--check did not mark the broken runtime inconsistent"
[[ ! -e "$EVENTS/install-cwd" ]] || fail "--check attempted an npm install"
[[ ! -e "$EVENTS/local-build" ]] || fail "--check attempted a local build"

printf 'test_codex_update: ok\n'
