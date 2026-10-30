#!/usr/bin/env bash
# Resolve the Codex executable used by this workspace. Print only its absolute path.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd -P)"

die() {
  printf 'resolve-codex-bin: %s\n' "$*" >&2
  exit 1
}

expected_version() {
  local version

  if [[ -n "${CODEX_BIN_EXPECTED_VERSION:-}" ]]; then
    printf '%s\n' "$CODEX_BIN_EXPECTED_VERSION"
    return
  fi
  [[ -f "$ROOT/package.json" ]] || die "package.json is missing"
  version="$(node -e '
    try {
      process.stdout.write(
        require(process.argv[1]).dependencies?.["@openai/codex"] ?? "",
      );
    } catch {}
  ' "$ROOT/package.json")"
  [[ -n "$version" ]] || die "package.json does not pin @openai/codex"
  printf '%s\n' "$version"
}

resolve_executable() {
  local label="$1"
  local candidate="$2"
  local actual expected resolved root_wrapper

  [[ -n "$candidate" ]] || die "$label is empty"
  resolved="$(realpath -e -- "$candidate" 2>/dev/null)" \
    || die "$label does not resolve: $candidate"
  [[ -f "$resolved" ]] || die "$label is not a regular file: $candidate"
  [[ -x "$resolved" ]] || die "$label is not executable: $candidate"
  root_wrapper="$(realpath -m -- "$ROOT/codex")"
  [[ "$resolved" != "$root_wrapper" ]] \
    || die "$label resolves to the workspace wrapper, not a Codex executable"

  expected="$(expected_version)"
  actual="$("$resolved" --version 2>/dev/null)" \
    || die "$label could not report its version: $candidate"
  [[ "$actual" == "codex-cli $expected" ]] \
    || die "$label reports '$actual', expected 'codex-cli $expected'"
  printf '%s\n' "$resolved"
}

if [[ ${CODEX_WORKSPACE_BIN+x} ]]; then
  resolve_executable "CODEX_WORKSPACE_BIN" "$CODEX_WORKSPACE_BIN"
  exit 0
fi

CURRENT="$ROOT/tools/codex-local/current"
if [[ -e "$CURRENT" || -L "$CURRENT" ]]; then
  resolve_executable "local Codex current" "$CURRENT/codex"
  exit 0
fi

resolve_executable "npm Codex shim" "$ROOT/node_modules/.bin/codex"
