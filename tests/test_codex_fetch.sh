#!/usr/bin/env bash
# scripts/fetch-codex-local.sh and the CI build mode of scripts/build-codex-local.sh, offline:
# gh, cargo and rustc are stubs, and no Codex source is cloned.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-fetch-test.XXXXXX")"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

fail() { printf 'test_codex_fetch: %s\n' "$*" >&2; exit 1; }

STUBS="$TEST_ROOT/stubs"
mkdir -p "$STUBS"
# cargo must never run: a CI-mode machine does not compile
printf '#!/usr/bin/env bash\ntouch "%s/cargo-ran"\nexit 1\n' "$TEST_ROOT" >"$STUBS/cargo"
printf '#!/usr/bin/env bash\necho "host: x86_64-unknown-linux-gnu"\n' >"$STUBS/rustc"
# gh: an artifact listing answers FAKE_GH_ID (empty: none); a zip download serves FAKE_GH_ZIP
cat >"$STUBS/gh" <<'EOF'
#!/usr/bin/env bash
case "$2" in
  *"/actions/artifacts?name="*) printf '%s\n' "${FAKE_GH_ID:-}" ;;
  *"/actions/artifacts/"*"/zip") cat "$FAKE_GH_ZIP" ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$STUBS"/*
export PATH="$STUBS:$PATH"

make_root() {
  local root="$1"
  mkdir -p "$root/scripts" "$root/patches/codex"
  cp "$SOURCE_ROOT/scripts/build-codex-local.sh" "$SOURCE_ROOT/scripts/fetch-codex-local.sh" "$root/scripts/"
  printf '{"dependencies":{"@openai/codex":"1.2.3"}}\n' >"$root/package.json"
  printf 'patch\n' >"$root/patches/codex/codex-1.2.3-footer.patch"
  git -C "$root" init -q
  git -C "$root" remote add origin https://github.com/example/workspace.git
}

# 1. CI mode with no artifact: refused with the release id, nothing compiled
ROOT="$TEST_ROOT/ci-none"
make_root "$ROOT"
SHA="$("$ROOT/scripts/build-codex-local.sh" --print-patch-sha 1.2.3)"
set +e
out="$(CODEX_LOCAL_BUILD=ci FAKE_GH_ID= "$ROOT/scripts/build-codex-local.sh" 1.2.3 2>&1)"
rc=$?
set -e
[[ $rc -ne 0 ]] || fail "CI mode without an artifact succeeded"
[[ "$out" == *"no CI build of 1.2.3-${SHA:0:12}"* ]] || fail "CI mode did not name the missing release: $out"
[[ ! -e "$TEST_ROOT/cargo-ran" ]] || fail "CI mode ran cargo"

# 2. the mode file works too, and an unknown mode is refused
mkdir -p "$ROOT/tools/codex-local"
printf 'ci\n' >"$ROOT/tools/codex-local/build-mode"
set +e
out="$(FAKE_GH_ID= "$ROOT/scripts/build-codex-local.sh" 1.2.3 2>&1)"
set -e
[[ "$out" == *"no CI build of"* ]] || fail "build-mode file was not honoured: $out"
set +e
out="$(CODEX_LOCAL_BUILD=remote "$ROOT/scripts/build-codex-local.sh" 1.2.3 2>&1)"
rc=$?
set -e
[[ $rc -ne 0 && "$out" == *"unknown build mode 'remote'"* ]] || fail "unknown build mode was not refused: $out"
[[ ! -e "$TEST_ROOT/cargo-ran" ]] || fail "a refused mode ran cargo"

# 3. an artifact built from another patch series is refused and leaves nothing behind
ROOT="$TEST_ROOT/fetch"
make_root "$ROOT"
SHA="$("$ROOT/scripts/build-codex-local.sh" --print-patch-sha 1.2.3)"
ART="$TEST_ROOT/art"
mkdir -p "$ART"
printf '#!/usr/bin/env bash\necho "codex-cli 1.2.3"\n' >"$ART/codex"
write_manifest() {
  printf '{"target":"1.2.3","patch_sha":"%s","executable_sha":"%s"}\n' \
    "$1" "$(sha256sum "$ART/codex" | awk '{print $1}')" >"$ART/build.json"
  rm -f "$TEST_ROOT/art.zip"
  (cd "$ART" && python3 -c 'import sys, zipfile; z = zipfile.ZipFile(sys.argv[1], "w"); [z.write(f) for f in ("codex", "build.json")]; z.close()' "$TEST_ROOT/art.zip")
}
write_manifest "0000"
set +e
out="$(FAKE_GH_ID=42 FAKE_GH_ZIP="$TEST_ROOT/art.zip" "$ROOT/scripts/fetch-codex-local.sh" 2>&1)"
rc=$?
set -e
[[ $rc -ne 0 && "$out" == *"built from patch 0000"* ]] || fail "a mismatched artifact was not refused: $out"
[[ ! -e "$ROOT/tools/codex-local/releases/1.2.3-${SHA:0:12}" ]] || fail "a refused artifact left a release folder"

# 4. a matching artifact is unpacked into a fresh release folder, executable, and handed to the
#    build script for verification (stubbed here: the real one would fetch Codex's source)
write_manifest "$SHA"
cat >"$ROOT/scripts/build-codex-local.sh" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == "--print-patch-sha" ]]; then echo "$SHA"; exit 0; fi
echo "verify \$1" >>"$TEST_ROOT/build-calls"
EOF
FAKE_GH_ID=42 FAKE_GH_ZIP="$TEST_ROOT/art.zip" "$ROOT/scripts/fetch-codex-local.sh" >/dev/null
RELEASE="$ROOT/tools/codex-local/releases/1.2.3-${SHA:0:12}"
[[ -x "$RELEASE/codex" && -f "$RELEASE/build.json" ]] || fail "the release was not installed"
[[ "$(cat "$TEST_ROOT/build-calls")" == "verify 1.2.3" ]] || fail "the build script was not asked to verify 1.2.3"

# 5. a release that fails the build script's verification is removed again
rm -rf "$RELEASE" "$TEST_ROOT/build-calls"
printf '#!/usr/bin/env bash\n[[ "$1" == "--print-patch-sha" ]] && { echo "%s"; exit 0; }\nexit 1\n' "$SHA" \
  >"$ROOT/scripts/build-codex-local.sh"
set +e
FAKE_GH_ID=42 FAKE_GH_ZIP="$TEST_ROOT/art.zip" "$ROOT/scripts/fetch-codex-local.sh" >/dev/null 2>&1
rc=$?
set -e
[[ $rc -ne 0 && ! -e "$RELEASE" ]] || fail "a release that failed verification was kept"

printf 'codex fetch and CI build mode: ok\n'
