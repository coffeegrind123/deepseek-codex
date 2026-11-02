#!/usr/bin/env bash
# Install the patched Codex runtime that CI built, instead of compiling it here.
#
#   scripts/fetch-codex-local.sh [VERSION]      (default: the package.json pin)
#
# .github/workflows/build-codex.yml runs scripts/build-codex-local.sh and uploads the
# release as the artifact codex-local-<version>-<patch id>. This finds the newest such
# artifact for the exact patch series in this checkout, unpacks it into a release folder
# created fresh (never renamed into place: renaming a directory on this 9p mount corrupts
# the container's view of it), then runs build-codex-local.sh, which finds the release
# already built and only verifies it (version, source commit, patch hash, platform,
# executable hash) and points tools/codex-local/current at it. Nothing is compiled.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd -P)"
RELEASES_DIR="$ROOT/tools/codex-local/releases"
WORKFLOW="build-codex.yml"

die() { printf 'fetch-codex-local: %s\n' "$*" >&2; exit 1; }

command -v gh >/dev/null || die "the gh CLI is needed to download CI artifacts"
command -v unzip >/dev/null || die "unzip is not installed"

VERSION="${1:-$(node -p "require('$ROOT/package.json').dependencies['@openai/codex']")}"
PATCH_SHA="$("$ROOT/scripts/build-codex-local.sh" --print-patch-sha "$VERSION")" \
  || die "could not hash the patch series for $VERSION"
RELEASE_ID="$VERSION-${PATCH_SHA:0:12}"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
ARTIFACT="codex-local-$RELEASE_ID"

if [[ -d "$RELEASE_DIR" ]]; then
  printf 'Release %s is already here; verifying and promoting it.\n' "$RELEASE_ID"
  exec "$ROOT/scripts/build-codex-local.sh" "$VERSION"
fi

REPO="$(git -C "$ROOT" remote get-url origin | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')"
ARTIFACT_ID="$(gh api "repos/$REPO/actions/artifacts?name=$ARTIFACT&per_page=20" \
  --jq '[.artifacts[] | select(.expired == false)] | sort_by(.created_at) | last | .id // empty')" \
  || die "could not list the CI artifacts of $REPO"
[[ -n "$ARTIFACT_ID" ]] || die "no CI build of $RELEASE_ID yet: push the patch series and wait for the \"Build patched Codex\" workflow ($WORKFLOW; gh run watch)"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/codex-fetch.XXXXXX")"
trap 'rm -rf -- "$TMP"' EXIT
gh api "repos/$REPO/actions/artifacts/$ARTIFACT_ID/zip" > "$TMP/artifact.zip" \
  || die "could not download artifact $ARTIFACT ($ARTIFACT_ID)"
mkdir "$TMP/release"
unzip -q "$TMP/artifact.zip" -d "$TMP/release" || die "artifact $ARTIFACT is not a readable zip"
[[ -f "$TMP/release/codex" && -f "$TMP/release/build.json" ]] \
  || die "artifact $ARTIFACT does not hold codex and build.json"

manifest() { node -p "require(process.argv[1])[process.argv[2]] ?? ''" "$TMP/release/build.json" "$1"; }
[[ "$(manifest target)" == "$VERSION" ]] || die "artifact $ARTIFACT was built for $(manifest target), not $VERSION"
[[ "$(manifest patch_sha)" == "$PATCH_SHA" ]] || die "artifact $ARTIFACT was built from patch $(manifest patch_sha), not $PATCH_SHA"
[[ "$(sha256sum -- "$TMP/release/codex" | awk '{print $1}')" == "$(manifest executable_sha)" ]] \
  || die "artifact $ARTIFACT's codex does not match its recorded hash"

mkdir -p "$RELEASES_DIR"
mkdir -- "$RELEASE_DIR"
cp -- "$TMP/release/codex" "$TMP/release/build.json" "$RELEASE_DIR/"
chmod 0755 "$RELEASE_DIR/codex"
printf 'Downloaded %s (artifact %s); verifying and promoting it.\n' "$RELEASE_ID" "$ARTIFACT_ID"
if ! "$ROOT/scripts/build-codex-local.sh" "$VERSION"; then
  rm -rf -- "$RELEASE_DIR"
  die "the downloaded release failed verification and was removed"
fi
