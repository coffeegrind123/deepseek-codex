#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-patch-discovery.XXXXXX")"
ROOT="$TEST_ROOT/workspace"

cleanup() {
  rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

fail() {
  printf 'test_codex_patch_discovery: %s\n' "$*" >&2
  exit 1
}

mkdir -p "$ROOT/scripts" "$ROOT/patches/codex"
cp "$SOURCE_ROOT/scripts/build-codex-local.sh" "$ROOT/scripts/build-codex-local.sh"
chmod +x "$ROOT/scripts/build-codex-local.sh"

printf 'tracked\n' >"$ROOT/patches/codex/codex-1.2.3-tracked.patch"
printf 'common\n' >"$ROOT/patches/codex/codex-common-shared.patch"
printf 'other\n' >"$ROOT/patches/codex/codex-9.9.9-other.patch"
printf 'ignored\n' >"$ROOT/patches/codex/notes.txt"

git -C "$ROOT" init -q
git -C "$ROOT" config user.email test@example.invalid
git -C "$ROOT" config user.name Test
git -C "$ROOT" add \
  scripts/build-codex-local.sh \
  patches/codex/codex-1.2.3-tracked.patch \
  patches/codex/codex-common-shared.patch
git -C "$ROOT" commit -qm baseline

EXPECTED=$'patches/codex/codex-1.2.3-tracked.patch\npatches/codex/codex-9.9.9-other.patch\npatches/codex/codex-common-shared.patch'
ACTUAL="$("$ROOT/scripts/build-codex-local.sh" --list-patches)"
[[ "$ACTUAL" == "$EXPECTED" ]] \
  || fail "patch discovery omitted an untracked workspace patch: $ACTUAL"

EXPECTED_SHA="$(
  cat \
    "$ROOT/patches/codex/codex-1.2.3-tracked.patch" \
    "$ROOT/patches/codex/codex-common-shared.patch" \
    | sha256sum | awk '{print $1}'
)"
ACTUAL_SHA="$("$ROOT/scripts/build-codex-local.sh" --print-patch-sha 1.2.3)"
[[ "$ACTUAL_SHA" == "$EXPECTED_SHA" ]] \
  || fail "patch hash did not select only the target and common patches"

printf 'codex build patch discovery: ok\n'
