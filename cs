#!/usr/bin/env bash
# Run the Codex Security CLI (cs) in a sibling Docker container. cs refuses to scan unless
# Codex's bubblewrap sandbox starts, and this container's seccomp denies the user
# namespaces bwrap needs; the Docker daemon can still start a sibling where it works.
#
# The sibling sees only what is listed below, each at its same absolute path:
#   the repo (cwd, or the first non-option arg)  read-only (rw for `patch`, `install-hook`)
#   .codex/state/cs                              read-write (scan history)
#   .codex/codex-security.yaml, .codex/models.json  read-only
# Never the home root, .ssh, .claude, secrets.env, docker.sock or the host bridge token.
# The only secret passed is the router token, as CODEX_ROUTER_TOKEN and CODEX_API_KEY (by
# name, not on argv). It shares this
# container's network namespace so 127.0.0.1:8877 reaches the router.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# openai/codex-security scanner image, pinned to the 0.6.0 index digest (matches package.json).
IMAGE="ghcr.io/openai/codex-security@sha256:ac020d2422e0aab59c2ad5207796e7d5ece73332edf1ab6ccbdaf548b82db407"
SECCOMP="$ROOT/sandbox/codex-security-seccomp.json"
# Not the plugin's state dir: that is root-owned 0700 and bind mounts enforce it, so the
# non-root scanner (uid 10001) could not use it. A sticky 1777 dir passes cs's
# "no world-writable parent without the sticky bit" check and keeps the scanner non-root.
STATE="$ROOT/.codex/state/cs"
CONFIG="$ROOT/.codex/codex-security.yaml"
MODELS="$ROOT/.codex/models.json"
CS_PKG="$ROOT/node_modules/@openai/codex-security/_bundled_plugin"
CS_PATCH="$ROOT/scripts/patch_codex_security_plugin.py"
CONTAINER_USER="10001:10001"

die() { printf 'cs: %s\n' "$*" >&2; exit 2; }

command -v docker >/dev/null || die "docker CLI not found"
[[ -f "$SECCOMP" ]] || die "$SECCOMP missing"

# Map a path in this container to its source on the Docker host via this container's own
# bind mount of $HOME. A sibling's -v resolves on the host, so /home/claudeuser/... would
# mount a different (often empty) tree. Paths outside that mount (/tmp, overlay) can't be
# shared and are refused.
host_home() {
  local src
  src="$(docker inspect "$(hostname)" \
    --format "{{range .Mounts}}{{if eq .Destination \"$HOME\"}}{{.Source}}{{end}}{{end}}" 2>/dev/null)" \
    || die "cannot inspect this container ($(hostname)) to find the host path of $HOME"
  [[ -n "$src" ]] || die "$HOME is not a bind mount of this container"
  # C:\Users\User\downloads\as/data -> //c/Users/User/downloads/as/data
  src="${src//\\//}"
  if [[ "$src" =~ ^([A-Za-z]):(/.*)$ ]]; then
    src="//${BASH_REMATCH[1],,}${BASH_REMATCH[2]}"
  fi
  printf '%s' "$src"
}

HOME_REAL="$(realpath "$HOME")"
HOST_HOME="$(host_home)"
[[ -n "$HOST_HOME" ]] || exit 2

to_host() {
  local p="$1"
  [[ "$p" == "$HOME_REAL"/* ]] || die "$p is outside $HOME_REAL; the Docker host cannot see it"
  printf '%s%s' "$HOST_HOME" "${p#"$HOME_REAL"}"
}

# The repo: the argument right after the subcommand when it is a directory (cs scan <repo>),
# else cwd. Only that slot is checked because option values (--path src) are directories
# too.
sub="${1:-}"
args=("$@")
repo="$PWD"
has_repo_arg=0
if [[ $# -ge 2 && "$2" != -* && -d "$2" ]]; then
  repo="$2"
  has_repo_arg=1
fi
repo="$(realpath -e -- "$repo")"

# Codex's sandbox preflight runs in the working directory and mkdirs <cwd>/.codex as a
# protected mount point, which fails on the read-only repo mount. Commands whose first
# argument is the repository run from scratch with the repo passed absolutely; the rest
# default to the current directory as the repo, so they keep it as their cwd.
workdir="$repo"
case "$sub" in
  scan|policy|scan-components|install-hook)
    workdir=/home/cs
    if (( has_repo_arg )); then
      args[1]="$repo"
    else
      args=("$sub" "$repo" "${@:2}")
    fi
    ;;
  *)
    (( has_repo_arg )) && args[1]="."
    ;;
esac

# Refuse anything that would expose more than one project.
[[ "$repo" != "$HOME_REAL" && "$repo" != "/" ]] || die "refusing to mount $repo; cd into a project"
case "$repo/" in
  "$ROOT/.codex/"*|"$HOME_REAL/.ssh/"*|"$HOME_REAL/.claude/"*) die "refusing to mount $repo" ;;
esac
for secret in .ssh .claude .codex/secrets.env .claude-host-bridge-token .git-credentials .docker; do
  [[ ! -e "$repo/$secret" ]] || die "refusing to mount $repo: it contains $secret"
done
[[ "$ROOT/" != "$repo/"* ]] || die "refusing to mount $repo: it contains this workspace"

repo_mode=ro
[[ "$sub" == "patch" || "$sub" == "install-hook" ]] && repo_mode=rw

mkdir -p "$STATE"
chmod 1777 "$STATE"

# Scans must use the patched plugin when no override is supplied. The state mount makes
# this copy visible at the same absolute path inside the sibling container.
help=0
has_plugin_path=0
has_time_limit=0
deep_mode=0
for arg in "$@"; do
  case "$arg" in
    -h|--help) help=1 ;;
    --plugin-path|--plugin-path=*) has_plugin_path=1 ;;
    --max-time|--max-time=*|--max-time-hours|--max-time-hours=*) has_time_limit=1 ;;
    --mode=deep) deep_mode=1 ;;
  esac
done
previous=""
for arg in "$@"; do
  if [[ "$previous" == "--mode" && "$arg" == "deep" ]]; then
    deep_mode=1
  fi
  previous="$arg"
done
if [[ "$sub" == "scan" && "$help" -eq 0 && "$has_plugin_path" -eq 0 ]]; then
  [[ -d "$CS_PKG" ]] || die "$CS_PKG missing"
  [[ -f "$CS_PATCH" ]] || die "$CS_PATCH missing"
  plugin_version="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$CS_PKG/.codex-plugin/plugin.json")" \
    || die "cannot read codex-security plugin version"
  [[ "$plugin_version" =~ ^[A-Za-z0-9._+-]+$ ]] || die "invalid codex-security plugin version: $plugin_version"
  plugin_path="$STATE/plugin-$plugin_version"
  if ! python3 "$CS_PATCH" --source "$CS_PKG" --output "$plugin_path" --quiet; then
    die "failed to patch codex-security plugin at $plugin_path"
  fi
  [[ -d "$plugin_path" ]] || die "patching did not create $plugin_path"
  chmod -R a+rX "$plugin_path"
  args+=("--plugin-path" "$plugin_path")
fi
if [[ "$sub" == "scan" && "$help" -eq 0 && "$has_time_limit" -eq 0 ]]; then
  # Keep routine Standard scans short and give repeated Deep audits the full
  # 90-minute default while avoiding a 0.6.0 project-config duration conflict.
  if (( deep_mode )); then
    args+=("--max-time" "90")
  else
    args+=("--max-time" "5")
  fi
fi

# Resolved here, not inline in `docker run`: a die inside $(...) there would only end the
# subshell and leave docker an empty mount source.
host_repo="$(to_host "$repo")"
host_state="$(to_host "$STATE")"
host_config="$(to_host "$CONFIG")"
host_models="$(to_host "$MODELS")"
set -a
# shellcheck disable=SC1091
. "$ROOT/.codex/secrets.env"
set +a
"$ROOT/codex-router" ensure >/dev/null
# cs refuses to start without OPENAI_API_KEY or CODEX_API_KEY, even when the provider has its
# own env_key. Model traffic goes only to the router (codex-security.yaml), so hand it the
# router token, which is worthless anywhere else.
export CODEX_API_KEY="$CODEX_ROUTER_TOKEN"

tty_args=()
[[ -t 0 && -t 1 ]] && tty_args=(-it)

exec docker run --rm --init "${tty_args[@]}" \
  --user "$CONTAINER_USER" \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --security-opt "seccomp=$SECCOMP" \
  --read-only \
  --tmpfs /tmp:rw,exec,size=1g \
  --tmpfs /home/cs:rw,size=512m,uid=10001,gid=10001,mode=0700 \
  --network "container:$(hostname)" \
  -e HOME=/home/cs \
  -e CODEX_ROUTER_TOKEN \
  -e CODEX_API_KEY \
  -e CODEX_SECURITY_STATE_DIR="$STATE" \
  -e CODEX_SECURITY_PROJECT_CONFIG="$CONFIG" \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0="$repo" \
  -v "$host_repo:$repo:$repo_mode" \
  -v "$host_state:$STATE:rw" \
  -v "$host_config:$CONFIG:ro" \
  -v "$host_models:$MODELS:ro" \
  -w "$workdir" \
  "$IMAGE" "${args[@]}"
