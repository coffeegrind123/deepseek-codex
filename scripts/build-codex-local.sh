#!/usr/bin/env bash
# Build a tagged upstream Codex CLI with the workspace patch series and publish it locally.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd -P)"
SOURCE_REPO="https://github.com/openai/codex.git"
LOCAL_ROOT="$ROOT/tools/codex-local"
RELEASES_DIR="$LOCAL_ROOT/releases"
STAGING_DIR="$LOCAL_ROOT/.staging"
CURRENT_LINK="$LOCAL_ROOT/current"
CARGO_HOME_DIR="/tmp/codex-local-cargo-home"
CARGO_TARGET_DIR_DEFAULT="/tmp/codex-local-build-target"

WORKTREE=""
CLONE_TMP=""
STAGE_RELEASE=""
CURRENT_TMP=""
DISCOVERED_PATCH_FILES=()
SELECTED_PATCH_FILES=()

usage() {
  cat <<'EOF'
Usage: scripts/build-codex-local.sh VERSION
       scripts/build-codex-local.sh --list-patches
       scripts/build-codex-local.sh --print-patch-sha VERSION

Build OpenAI Codex tag rust-vVERSION, apply workspace patches/codex/*.patch files,
including uncommitted patches, and publish the verified binary under
tools/codex-local/releases/.

Patch names select their target version:
  codex-VERSION-description.patch  applies only to rust-vVERSION
  codex-common-description.patch   applies to every version
EOF
}

die() {
  printf 'build-codex-local: %s\n' "$*" >&2
  exit 1
}

discover_patch_files() {
  git -C "$ROOT" ls-files -z --cached --others --exclude-standard \
    -- 'patches/codex/*.patch' | LC_ALL=C sort -zu
}

select_patch_files() {
  local target="$1"
  local patch patch_name selected=0

  SELECTED_PATCH_FILES=()
  for patch in "${DISCOVERED_PATCH_FILES[@]}"; do
    patch_name="${patch##*/}"
    case "$patch_name" in
      codex-common-*.patch|codex-"$target"-*.patch)
        SELECTED_PATCH_FILES+=("$patch")
        selected=1
        ;;
      codex-[0-9]*-*.patch)
        ;;
      *)
        die "invalid patch name: $patch (expected codex-VERSION-*.patch or codex-common-*.patch)"
        ;;
    esac
  done

  [[ "$selected" -eq 1 ]] \
    || die "no patch files target Codex $target (add codex-$target-*.patch)"
}

patch_sha_for() {
  local target="$1"
  local patch

  select_patch_files "$target"
  {
    for patch in "${SELECTED_PATCH_FILES[@]}"; do
      cat -- "$ROOT/$patch"
    done
  } | sha256sum | awk '{print $1}'
}

cleanup() {
  local status="$?"
  set +e
  trap - EXIT INT TERM HUP

  if [[ -n "$CURRENT_TMP" && ( -e "$CURRENT_TMP" || -L "$CURRENT_TMP" ) ]]; then
    rm -f -- "$CURRENT_TMP"
  fi
  if [[ -n "$STAGE_RELEASE" && -d "$STAGE_RELEASE" ]]; then
    rm -rf -- "$STAGE_RELEASE"
  fi
  if [[ -n "$WORKTREE" && -e "$WORKTREE" ]]; then
    git -C "$SOURCE_DIR" worktree remove --force -- "$WORKTREE" >/dev/null 2>&1 \
      || rm -rf -- "$WORKTREE"
  fi
  if [[ -n "$CLONE_TMP" && -e "$CLONE_TMP" ]]; then
    rm -rf -- "$CLONE_TMP"
  fi

  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

manifest_value() {
  local key="$1"
  local manifest="$2"
  sed -n -E "s/^  \"$key\": \"([^\"]*)\",?$/\\1/p" "$manifest" | head -n 1
}

verify_release() {
  local release_dir="$1"
  local manifest="$release_dir/build.json"
  local executable="$release_dir/codex"
  local actual_hash actual_version

  [[ -d "$release_dir" && ! -L "$release_dir" ]] || return 1
  [[ -f "$manifest" && -f "$executable" && -x "$executable" ]] || return 1
  [[ "$(manifest_value target "$manifest")" == "$TARGET" ]] || return 1
  [[ "$(manifest_value source_commit "$manifest")" == "$SOURCE_COMMIT" ]] || return 1
  [[ "$(manifest_value patch_sha "$manifest")" == "$PATCH_SHA" ]] || return 1
  [[ "$(manifest_value triple "$manifest")" == "$TRIPLE" ]] || return 1

  actual_hash="$(sha256sum -- "$executable" | awk '{print $1}')"
  [[ "$(manifest_value executable_sha "$manifest")" == "$actual_hash" ]] || return 1
  actual_version="$("$executable" --version 2>/dev/null)" || return 1
  [[ "$actual_version" == "codex-cli $TARGET" ]] || return 1
}

PRINT_PATCH_SHA=0
if [[ "${1:-}" == "--print-patch-sha" ]]; then
  [[ $# -eq 2 ]] || {
    usage >&2
    exit 2
  }
  PRINT_PATCH_SHA=1
  shift
fi

[[ $# -eq 1 ]] || {
  usage >&2
  exit 2
}
case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
  --list-patches)
    while IFS= read -r -d '' patch; do
      printf '%s\n' "$patch"
    done < <(discover_patch_files)
    exit 0
    ;;
esac

mapfile -d '' -t DISCOVERED_PATCH_FILES < <(discover_patch_files)

TARGET="$1"
[[ "$TARGET" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] \
  || die "invalid version: $TARGET"

if [[ "$PRINT_PATCH_SHA" -eq 1 ]]; then
  patch_sha_for "$TARGET"
  exit 0
fi

TAG="rust-v$TARGET"
SOURCE_DIR="/tmp/codex-source-$TARGET"

command -v cargo >/dev/null || die "cargo is not installed"
command -v git >/dev/null || die "git is not installed"
command -v rustc >/dev/null || die "rustc is not installed"
command -v sha256sum >/dev/null || die "sha256sum is not installed"
command -v flock >/dev/null || die "flock is not installed"

LOCK_FILE="/tmp/codex-local-build-$(printf '%s' "$ROOT" | sha256sum | cut -c1-12).lock"
exec 9>"$LOCK_FILE"
flock -n 9 || die "another local Codex build is already running"

if [[ -e "$SOURCE_DIR" || -L "$SOURCE_DIR" ]]; then
  [[ -d "$SOURCE_DIR" && ! -L "$SOURCE_DIR" ]] \
    || die "source cache is not a directory: $SOURCE_DIR"
  git -C "$SOURCE_DIR" rev-parse --git-dir >/dev/null 2>&1 \
    || die "source cache is not a Git repository: $SOURCE_DIR"
else
  CLONE_TMP="$(mktemp -d "/tmp/.codex-source-$TARGET.XXXXXX")"
  rmdir -- "$CLONE_TMP"
  if ! git clone --no-checkout --no-tags --filter=blob:none "$SOURCE_REPO" "$CLONE_TMP"; then
    rm -rf -- "$CLONE_TMP"
    git clone --no-checkout --no-tags "$SOURCE_REPO" "$CLONE_TMP" \
      || die "could not clone $SOURCE_REPO"
  fi
  mv -- "$CLONE_TMP" "$SOURCE_DIR"
  CLONE_TMP=""
fi

if ! git -C "$SOURCE_DIR" rev-parse --verify --quiet "refs/tags/$TAG^{commit}" >/dev/null; then
  git -C "$SOURCE_DIR" fetch --force --no-tags "$SOURCE_REPO" \
    "+refs/tags/$TAG:refs/tags/$TAG" \
    || die "could not fetch tag $TAG"
fi
SOURCE_COMMIT="$(git -C "$SOURCE_DIR" rev-parse --verify "$TAG^{commit}")"

select_patch_files "$TARGET"
PATCH_FILES=("${SELECTED_PATCH_FILES[@]}")
PATCH_SHA="$(patch_sha_for "$TARGET")"
PATCH_ID="${PATCH_SHA:0:12}"
RELEASE_ID="$TARGET-$PATCH_ID"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
TRIPLE="$(rustc -vV | awk '$1 == "host:" { print $2; exit }')"
[[ -n "$TRIPLE" ]] || die "rustc did not report a host triple"

mkdir -p "$RELEASES_DIR" "$STAGING_DIR" "$CARGO_HOME_DIR"

if [[ -e "$RELEASE_DIR" || -L "$RELEASE_DIR" ]]; then
  verify_release "$RELEASE_DIR" \
    || die "existing release failed verification: $RELEASE_DIR"

  CURRENT_TMP="$(mktemp "$LOCAL_ROOT/.current.XXXXXX")"
  rm -f -- "$CURRENT_TMP"
  ln -s -- "releases/$RELEASE_ID" "$CURRENT_TMP"
  current_version="$("$CURRENT_TMP/codex" --version)"
  [[ "$current_version" == "codex-cli $TARGET" ]] \
    || die "reused release did not report codex-cli $TARGET"
  mv -Tf -- "$CURRENT_TMP" "$CURRENT_LINK"
  CURRENT_TMP=""
  printf 'Already built and verified: %s\n' "$RELEASE_DIR/codex"
  exit 0
fi

WORKTREE="$(mktemp -d "/tmp/.codex-worktree-$TARGET.XXXXXX")"
rmdir -- "$WORKTREE"
git -C "$SOURCE_DIR" worktree add --detach "$WORKTREE" "$SOURCE_COMMIT" >/dev/null \
  || die "could not create a clean worktree at $SOURCE_COMMIT"
[[ -z "$(git -C "$WORKTREE" status --porcelain)" ]] \
  || die "source worktree is not clean"

for patch in "${PATCH_FILES[@]}"; do
  git -C "$WORKTREE" apply --index --whitespace=nowarn "$ROOT/$patch" \
    || die "could not apply tracked patch: $patch"
done

if [[ -f "$WORKTREE/Cargo.toml" ]]; then
  MANIFEST_DIR="$WORKTREE"
elif [[ -f "$WORKTREE/codex-rs/Cargo.toml" ]]; then
  MANIFEST_DIR="$WORKTREE/codex-rs"
else
  die "Cargo workspace not found in $WORKTREE"
fi

printf 'Building codex-cli %s from %s (%s)\n' "$TARGET" "$SOURCE_COMMIT" "$PATCH_ID"
(
  cd -- "$MANIFEST_DIR"
  CARGO_HOME="${CODEX_CARGO_HOME:-$CARGO_HOME_DIR}" \
    CARGO_TARGET_DIR="${CODEX_CARGO_TARGET_DIR:-$CARGO_TARGET_DIR_DEFAULT}" \
    cargo build -p codex-cli --bin codex --release
) || die "Cargo build failed"

BUILT_BIN="${CODEX_CARGO_TARGET_DIR:-$CARGO_TARGET_DIR_DEFAULT}/release/codex"
[[ -f "$BUILT_BIN" && -x "$BUILT_BIN" ]] || die "built executable is missing: $BUILT_BIN"
BUILT_VERSION="$("$BUILT_BIN" --version)" || die "built executable could not report its version"
[[ "$BUILT_VERSION" == "codex-cli $TARGET" ]] \
  || die "built executable reports '$BUILT_VERSION', expected 'codex-cli $TARGET'"
FINAL_PATCH_SHA="$(patch_sha_for "$TARGET")"
[[ "$FINAL_PATCH_SHA" == "$PATCH_SHA" ]] \
  || die "patch series changed during the build; discard the candidate and retry"
EXECUTABLE_SHA="$(sha256sum -- "$BUILT_BIN" | awk '{print $1}')"

STAGE_RELEASE="$(mktemp -d "$STAGING_DIR/$RELEASE_ID.XXXXXX")"
cp -- "$BUILT_BIN" "$STAGE_RELEASE/codex"
chmod 0755 "$STAGE_RELEASE/codex"
printf '{\n  "target": "%s",\n  "source_commit": "%s",\n  "patch_sha": "%s",\n  "executable_sha": "%s",\n  "triple": "%s"\n}\n' \
  "$TARGET" "$SOURCE_COMMIT" "$PATCH_SHA" "$EXECUTABLE_SHA" "$TRIPLE" \
  >"$STAGE_RELEASE/build.json"

if [[ -e "$RELEASE_DIR" || -L "$RELEASE_DIR" ]]; then
  verify_release "$RELEASE_DIR" \
    || die "release appeared during build and failed verification: $RELEASE_DIR"
  rm -rf -- "$STAGE_RELEASE"
  STAGE_RELEASE=""
else
  mv -T -- "$STAGE_RELEASE" "$RELEASE_DIR"
  STAGE_RELEASE=""
fi

CURRENT_TMP="$(mktemp "$LOCAL_ROOT/.current.XXXXXX")"
rm -f -- "$CURRENT_TMP"
ln -s -- "releases/$RELEASE_ID" "$CURRENT_TMP"
CURRENT_VERSION="$("$CURRENT_TMP/codex" --version)"
[[ "$CURRENT_VERSION" == "codex-cli $TARGET" ]] \
  || die "staged current link did not report codex-cli $TARGET"
mv -Tf -- "$CURRENT_TMP" "$CURRENT_LINK"
CURRENT_TMP=""

printf 'Published and verified: %s\n' "$RELEASE_DIR/codex"
