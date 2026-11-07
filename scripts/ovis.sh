#!/usr/bin/env bash
#
# Ovis for this workspace: a web and phone client for Codex sessions that run in
# herdr panes (github.com/devswha/ovis, pinned bundle in tools/ovis).
#
#   ./codex ovis start        start herdr, the Ovis server and the gate (idempotent)
#   ./codex ovis stop         stop the Ovis server; herdr and its panes stay
#   ./codex ovis status       what is running, and where
#   ./codex ovis token        print the access token (creating it if needed)
#   ./codex ovis enabled      exit 0 when OVIS_ENABLED is on
#   ./codex ovis run [-w DIR] [-- <codex args>]
#                             Codex in a new herdr pane, attached to this terminal
#   ./codex ovis gate [ensure|status|rm]
#                             the container that publishes Ovis on the Docker host
#   ./codex ovis install [--check]
#                             install or verify the pinned bundle (setup.sh runs it)
#
# Runs OUTSIDE the Landlock cage: herdr is the parent of every pane, so a caged
# herdr would cage every Codex started in it a second time, with the first one's
# folders. ./codex starts it in the background on every interactive launch.
#
# OVIS_ENABLED (default 1) decides whether `start` does anything. Ovis only sees
# Codex when Codex runs in a herdr pane, which is what `run` and the default
# pane arrange. Detach from a pane with Ctrl+B then Q: Codex keeps running, and
# Ovis keeps showing it.
#
# Access: Ovis listens on 127.0.0.1:7318 behind a relay on 0.0.0.0:7317
# (scripts/ovis_forward.py), because a non-loopback bind makes Ovis show a
# "reachable on your network" banner. This container publishes no ports, so the
# gate container (codex-ovis-gate, same relay) publishes the host's
# 127.0.0.1:${OVIS_PORT:-7327} and forwards to this container over the
# codex-ovis network. Every request except /api/health and the page itself
# needs the token. Telemetry and self-update are always off.
#
# OVIS_TOKEN=off runs it with no token, so the page opens without a sign-in.
# Then anything that reaches the port gets in and can type into Codex, until a
# device is paired: every program on the host, every container on the same
# Docker networks, and a web page that rebinds its own name to 127.0.0.1.
#
# Every new herdr pane starts Codex (scripts/ovis-shims/pane-shell), so a new
# workspace, tab or split in Ovis opens on a Codex chat; Ovis's own New
# workspace / New tab picker decides for its panes and defaults to Codex
# (scripts/ovis_patch.py). OVIS_CODEX_PANE=0 makes new panes plain shells.
#
# Settings come from the environment, then .codex/secrets.env: OVIS_ENABLED,
# OVIS_TOKEN, OVIS_PORT, OVIS_GATE, OVIS_CODEX_PANE, OVIS_CODEX_DIR.

set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"

# --- the pinned bundle -------------------------------------------------------
# The prebuilt release bundle (built web UI, server run by the bundled bun,
# herdr itself and a Rust PTY host), checked against the sha256 its own
# manifest.json publishes.
OVIS_BUNDLE_TAG=remote-v40
OVIS_SHA256_LINUX_X64=beb56b9bb55519f5ad2f54fe5bd5bb20ef01c0b0725a680d1016d16085095416
OVIS_SHA256_LINUX_ARM64=64e5e95399f219a8862c809bc79a531d3ebad3af45e25f9d287f7c7477044024
OVIS_RELEASE_URL=https://github.com/devswha/ovis/releases/download

OVIS_DIR="${OVIS_DIR:-$ROOT/tools/ovis}"
HERDR="$OVIS_DIR/bin/herdr"
BUN="$OVIS_DIR/bin/bun"
PIN_FILE="$OVIS_DIR/.pin"
# herdr's own Codex SessionStart hook, generated from the pinned herdr at install
# time; scripts/ovis-codex-hook.sh runs it inside herdr panes only.
CODEX_INTEGRATION="$OVIS_DIR/integrations/herdr-codex-agent-state.sh"

SECRETS="${OVIS_SECRETS_FILE:-$ROOT/.codex/secrets.env}"
STATE_DIR="${OVIS_STATE_DIR:-$ROOT/.codex/ovis}"
TOKEN_FILE="$STATE_DIR/token"
# Per container: the supervisor lock, pids and launch scripts must not be shared
# with another container that mounts the same workspace.
RUN_DIR="${OVIS_RUN_DIR:-/tmp/codex-ovis}"
LOG="$RUN_DIR/ovis.log"
SUPERVISOR_PID="$RUN_DIR/supervisor.pid"
SERVER_PID="$RUN_DIR/server.pid"
FORWARD_PID="$RUN_DIR/forward.pid"
LOG_MAX_BYTES=$(( 10 * 1024 * 1024 ))

# The relay owns OVIS_CONTAINER_PORT; Ovis itself listens on OVIS_INTERNAL_PORT.
OVIS_CONTAINER_PORT=7317
OVIS_INTERNAL_PORT=7318
# The host side: 7327, as 7317 (Ovis's own default) is often held by another Ovis.
OVIS_DEFAULT_HOST_PORT=7327
HEALTH_URL="http://127.0.0.1:${OVIS_CONTAINER_PORT}/api/health"

# `codex` on the herdr server's PATH, so a codex typed into a pane, or started
# there by Ovis (which types `codex` at the pane's prompt), runs through ./codex.
SHIM_DIR="$ROOT/scripts/ovis-shims"
# What a pane runs. Overridable so tests/test_ovis.py can run a launch script.
CODEX_WRAPPER="${OVIS_CODEX_WRAPPER:-$ROOT/codex}"
# Per-launch settings a pane must keep. herdr starts a pane from the SERVER's
# environment, so these are written into the launch script. A fixed list: a
# pattern would also catch tokens, and the launch file is on disk.
FORWARD_KEYS=(CODEX_NET CODEX_HOOK_TRUST CODEX_START_HOOK CODEX_DEEPSEEK_BALANCE_TITLE
              CODEX_WORKSPACE_BIN CODEX_ROUTER_PORT CODEX_ROUTER_PIDFILE TMPDIR)

# herdr's server only starts behind a client with a sized terminal; headless,
# that client runs under `script`. Measured 2026-11-01.
HERDR_COLS=200
HERDR_ROWS=50

GATE_NAME="${OVIS_GATE_CONTAINER:-codex-ovis-gate}"
GATE_NETWORK="${OVIS_GATE_NETWORK:-codex-ovis}"
GATE_LISTEN_PORT=7317

die()  { printf 'ovis: %s\n' "$*" >&2; exit 1; }
warn() { printf 'ovis: warning: %s\n' "$*" >&2; }
ok()   { printf '%s\n' "$*"; }
dim()  { if [[ -t 1 ]]; then printf '\033[2m%s\033[0m\n' "$*"; else printf '%s\n' "$*"; fi; }

usage() { awk 'NR > 2 && !/^#/ { exit } NR > 2' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# --- settings ------------------------------------------------------------------
# The environment wins; otherwise the last KEY=value in secrets.env, read as
# text (never sourced, so nothing in it runs), surrounding quotes removed.
cfg() {
  local key="$1"
  if [[ -n "${!key:-}" ]]; then
    printf '%s' "${!key}"
    return 0
  fi
  [[ -f "$SECRETS" ]] || return 0
  printf '%s' "$(sed -nE "s/^[[:space:]]*(export[[:space:]]+)?${key}=(.*)$/\\2/p" "$SECRETS" | tail -n 1 \
    | sed -E "s/[[:space:]]+$//; s/^\"(.*)\"$/\\1/; s/^'(.*)'$/\\1/")"
}

is_off() { [[ "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" =~ ^(0|false|no|off)$ ]]; }

# Default ON: unset or empty is on; 0/false/no/off is off.
ovis_enabled() { ! is_off "$(cfg OVIS_ENABLED)"; }
gate_enabled() { ! is_off "$(cfg OVIS_GATE)"; }

host_port() {
  local p; p="$(cfg OVIS_PORT)"
  printf '%s' "${p:-$OVIS_DEFAULT_HOST_PORT}"
}

# PATH ($1) without one directory ($2), every occurrence.
strip_path() {
  local p=":$1:" drop="$2"
  while [[ "$p" == *":$drop:"* ]]; do
    p="${p//:$drop:/:}"
  done
  p="${p#:}"; p="${p%:}"
  printf '%s' "$p"
}

outside_cage() {
  [[ -z "${CODEX_LANDLOCK_CAGE:-}" ]] \
    || die "runs outside the Landlock cage (this shell is caged to ${CODEX_LANDLOCK_CAGE}); run ./codex ovis from a plain shell"
}

have_bundle() { [[ -x "$HERDR" && -x "$BUN" && -f "$OVIS_DIR/server/index.ts" ]]; }

alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null; }

# --- install -------------------------------------------------------------------
bundle_platform() {
  case "$(uname -m)" in
    x86_64|amd64)  printf 'linux-x64 %s\n' "$OVIS_SHA256_LINUX_X64" ;;
    aarch64|arm64) printf 'linux-arm64 %s\n' "$OVIS_SHA256_LINUX_ARM64" ;;
    *) return 1 ;;
  esac
}

pin_line() { printf '%s %s %s\n' "$OVIS_BUNDLE_TAG" "$1" "$2"; }

installed_ok() {
  local plat="$1" want="$2"
  have_bundle && [[ -f "$CODEX_INTEGRATION" ]] \
    && [[ "$(cat "$PIN_FILE" 2>/dev/null)" == "$(pin_line "$plat" "$want")" ]]
}

# The download is verified before anything installed is touched. The old tree
# is removed and the new one extracted into a fresh directory, never renamed
# into place: renaming a directory on this 9p mount corrupts the container's
# view of it.
install_bundle() {
  local check=0; [[ "${1:-}" == "--check" ]] && check=1
  local plat want tmp got agent
  read -r plat want < <(bundle_platform) || die "no Ovis bundle for $(uname -m)"

  if installed_ok "$plat" "$want"; then
    ok "ovis bundle $OVIS_BUNDLE_TAG ($plat), herdr $("$HERDR" --version 2>/dev/null | awk '{print $2}')"
    return 0
  fi
  (( check )) && die "Ovis bundle $OVIS_BUNDLE_TAG is not installed in $OVIS_DIR; run: ./codex ovis install"
  outside_cage

  tmp="$(mktemp -d "${TMPDIR:-/tmp}/codex-ovis-install.XXXXXX")"
  INSTALL_TMP="$tmp"
  trap 'rm -rf -- "$INSTALL_TMP"' EXIT
  curl -fsSL --max-time 600 -o "$tmp/ovis.tgz" \
    "$OVIS_RELEASE_URL/$OVIS_BUNDLE_TAG/herdr-web-ui-$plat.tgz" \
    || die "could not download the Ovis bundle $OVIS_BUNDLE_TAG ($plat)"
  got="$(sha256sum "$tmp/ovis.tgz" | awk '{print $1}')"
  [[ "$got" == "$want" ]] || die "Ovis bundle checksum mismatch: expected $want, got $got"
  mkdir -p "$tmp/tree"
  tar xzf "$tmp/ovis.tgz" -C "$tmp/tree" --no-same-owner
  "$tmp/tree/bin/herdr" --version >/dev/null || die "the bundled herdr does not run here"
  "$tmp/tree/bin/bun" --version >/dev/null || die "the bundled bun does not run here"

  # herdr writes its Codex hook into CODEX_HOME; a scratch home keeps it out of
  # .codex, where .codex/hooks.json calls it through scripts/ovis-codex-hook.sh.
  agent="$tmp/codex-home"
  mkdir -p "$agent" "$tmp/home"
  HOME="$tmp/home" XDG_CONFIG_HOME="$tmp/home/.config" CODEX_HOME="$agent" \
    "$tmp/tree/bin/herdr" integration install codex >/dev/null \
    || die "herdr integration install codex failed"
  [[ -f "$agent/herdr-agent-state.sh" ]] || die "herdr did not write its Codex hook"
  mkdir -p "$tmp/tree/integrations"
  install -m 0755 "$agent/herdr-agent-state.sh" "$tmp/tree/integrations/herdr-codex-agent-state.sh"

  if have_bundle && "$HERDR" status 2>/dev/null | grep -q 'status: running'; then
    warn "herdr from the old bundle is still running; ./codex ovis stop and $HERDR server stop, then start again"
  fi
  rm -rf -- "$OVIS_DIR"
  mkdir -p "$OVIS_DIR"
  cp -a "$tmp/tree/." "$OVIS_DIR/"
  pin_line "$plat" "$want" > "$PIN_FILE"
  installed_ok "$plat" "$want" || die "Ovis bundle did not verify after install in $OVIS_DIR"
  ok "ovis bundle $OVIS_BUNDLE_TAG ($plat) installed in $OVIS_DIR: $(cat "$OVIS_DIR/bundle.json")"
}

# --- the token -----------------------------------------------------------------
# OVIS_TOKEN from the environment or secrets.env wins. Otherwise one is generated
# once into .codex/ovis/token, mode 0600, so it never sits in a tracked file or
# on a command line. OVIS_TOKEN=off (any case): no token, and none is generated.
auth_off() { [[ "$(cfg OVIS_TOKEN | tr '[:upper:]' '[:lower:]')" == off ]]; }

token() {
  auth_off && return 0
  local t; t="$(cfg OVIS_TOKEN)"
  if [[ -n "$t" ]]; then printf '%s' "$t"; return 0; fi
  if [[ ! -s "$TOKEN_FILE" ]]; then
    ( umask 077
      mkdir -p "$(dirname "$TOKEN_FILE")"
      od -An -tx1 -N24 /dev/urandom | tr -d ' \n' > "$TOKEN_FILE" )
  fi
  tr -d '[:space:]' < "$TOKEN_FILE"
}

# --- herdr ---------------------------------------------------------------------
herdr_up() {
  "$HERDR" status 2>/dev/null | awk '/^server:/{s=1; next} s && /status:/{print $2; exit}' | grep -qx running
}

# herdr restores its panes on restart and resumes each agent into its session
# by typing its resume command into the pane. The pane shell has already started
# a Codex there, so that line would land in Codex's composer (measured
# 2026-11-01). Resume is off; the earlier conversation stays one /resume away.
# An explicit choice in the file is left alone, with a warning when it is true.
HERDR_CONFIG="${OVIS_HERDR_CONFIG:-$HOME/.config/herdr/config.toml}"

ensure_herdr_config() {
  python3 - "$HERDR_CONFIG" <<'PY' || warn "could not set resume_agents_on_restore in $HERDR_CONFIG"
import os, re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
key = re.search(r"^\s*resume_agents_on_restore\s*=\s*(\S+)", text, re.M)
if key:
    if key.group(1).lower() == "true":
        print(f"ovis: warning: {path} has resume_agents_on_restore = true: restored panes get two Codex starts", file=sys.stderr)
    sys.exit(0)
line = "resume_agents_on_restore = false\n"
table = re.search(r"^\[session\]\s*\n", text, re.M)
if table:
    text = text[:table.end()] + line + text[table.end():]
else:
    text = text + ("" if text.endswith("\n") or not text else "\n") + "[session]\n" + line
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".tmp", "w", encoding="utf-8") as fh:
    fh.write(text)
os.replace(path + ".tmp", path)
PY
}

start_herdr() {
  herdr_up && return 0
  ensure_herdr_config
  # PATH is the herdr SERVER's, and every pane inherits it. `9>&-`: herdr
  # outlives the supervisor by design, and with the supervisor's lock open it
  # would hold that lock forever, so every later supervisor's flock would fail
  # in silence (measured 2026-11-01).
  # SHELL is what herdr runs for every new pane: scripts/ovis-shims/pane-shell,
  # Codex first and then bash, so a new workspace, tab or split opens on a Codex
  # chat. Never left unset: herdr then falls back to /bin/sh, which has no
  # bracketed paste, so text pasted from Ovis arrived as `^[[200~...` and ran
  # as a command. `script` runs its -c line with $SHELL, so it gets bash and
  # only herdr gets the pane shell.
  # Started from OVIS_CODEX_DIR (default this workspace): herdr's first pane,
  # and so its first Codex, works there.
  local dir; dir="$(cfg OVIS_CODEX_DIR)"; [[ -d "${dir:-}" ]] || dir="$ROOT"
  ( cd "$dir" && SHELL=/bin/bash PATH="$SHIM_DIR:$(strip_path "$PATH" "$SHIM_DIR")" setsid script -qfc \
    "stty cols $HERDR_COLS rows $HERDR_ROWS; SHELL='$SHIM_DIR/pane-shell' exec '$HERDR'" /dev/null \
    </dev/null >/dev/null 2>&1 9>&- & )
  local i
  for i in $(seq 1 30); do
    herdr_up && return 0
    sleep 0.5
  done
  return 1
}

# --- the supervisor ------------------------------------------------------------
# One per container (flock). Restarts the server when it exits, with a backoff
# that resets after a run longer than a minute, and re-reads OVIS_ENABLED each
# time so `OVIS_ENABLED=0` plus `stop` is final.
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

rotate_log() {
  [[ -f "$LOG" ]] || return 0
  if (( $(stat -c %s "$LOG") > LOG_MAX_BYTES )); then
    mv -f "$LOG" "$LOG.1"
  fi
  return 0
}

supervise() {
  exec 9>"$RUN_DIR/supervisor.lock"
  flock -n 9 || exit 0
  echo $$ > "$SUPERVISOR_PID"
  local child="" backoff=2 started ran rc tok
  trap '[[ -n "$child" ]] && kill "$child" 2>/dev/null; alive "$FORWARD_PID" && kill "$(cat "$FORWARD_PID" 2>/dev/null)" 2>/dev/null; rm -f "$SUPERVISOR_PID" "$SERVER_PID" "$FORWARD_PID"; exit 0' TERM INT

  while ovis_enabled; do
    rotate_log
    if ! start_herdr; then
      log "herdr did not start; retrying in ${backoff}s"
      sleep "$backoff"; backoff=$(( backoff * 2 > 60 ? 60 : backoff * 2 ))
      continue
    fi
    if ! alive "$FORWARD_PID"; then
      python3 "$ROOT/scripts/ovis_forward.py" 0.0.0.0 "$OVIS_CONTAINER_PORT" \
        127.0.0.1 "$OVIS_INTERNAL_PORT" >> "$LOG" 2>&1 9>&- &
      echo $! > "$FORWARD_PID"
    fi
    tok="$(token)"
    started=$(date +%s)
    log "starting Ovis ($(cat "$OVIS_DIR/bundle.json" 2>/dev/null))"
    # Reply narration (patches/ovis/voice-narration.ts) gets the voiceapi gateway and its key, read
    # from secrets.env as text by cfg like every other setting: the server never sources the file.
    # The composer's DeepSeek balance (patches/ovis/deepseek-balance.ts) reads the Codex footer's
    # cache first and queries DeepSeek itself through the plugin library when it is not fresh.
    # Ovis reads its token from OVIS_TOKEN alone, and empty or unset means none,
    # so OVIS_TOKEN=off is an unset variable here. CODEX_HOME is the store Ovis
    # falls back to; a pane's own Codex store is read from its process.
    ( cd "$OVIS_DIR" && \
      if [[ -n "$tok" ]]; then export OVIS_TOKEN="$tok"; else unset OVIS_TOKEN; fi && \
      HOST=127.0.0.1 PORT="$OVIS_INTERNAL_PORT" \
      OVIS_TELEMETRY=0 DO_NOT_TRACK=1 OVIS_AUTO_UPDATE=0 NO_OPEN_BROWSER=1 OVIS_NO_OPEN=1 \
      OVIS_STATE_DIR="$STATE_DIR/state" OVIS_PASTE_DIR="$STATE_DIR/paste" \
      OVIS_HERDR_BIN="$HERDR" CODEX_HOME="$ROOT/.codex" \
      VOICEAPI_URL="$(cfg VOICEAPI_URL)" VOICEAPI_KEY="$(cfg VOICEAPI_KEY)" \
      DEEPSEEK_API_KEY="$(cfg DEEPSEEK_API_KEY)" DEEPSEEK_BALANCE_URL="$(cfg DEEPSEEK_BALANCE_URL)" \
      DEEPSEEK_BALANCE_CACHE_FILE="$ROOT/.codex/cache/deepseek-balance.json" \
      DEEPSEEK_BALANCE_LIB="$ROOT/plugins/deepseek-balance/lib/deepseek-balance.mjs" \
      PATH="$SHIM_DIR:$(strip_path "$PATH" "$SHIM_DIR")" \
      exec "$BUN" server/index.ts ) >> "$LOG" 2>&1 9>&- &
    child=$!
    echo "$child" > "$SERVER_PID"
    rc=0; wait "$child" || rc=$?
    child=""
    ran=$(( $(date +%s) - started ))
    if (( ran > 60 )); then backoff=2; else backoff=$(( backoff * 2 > 60 ? 60 : backoff * 2 )); fi
    log "Ovis exited rc=$rc after ${ran}s; restarting in ${backoff}s"
    sleep "$backoff"
  done
  log "OVIS_ENABLED is off; supervisor exiting"
  alive "$FORWARD_PID" && kill "$(cat "$FORWARD_PID" 2>/dev/null)" 2>/dev/null
  rm -f "$SUPERVISOR_PID" "$SERVER_PID" "$FORWARD_PID"
}

healthy() { curl -fsS -m 2 -o /dev/null "$HEALTH_URL" 2>/dev/null; }

# The address a browser uses: the gate's on the Docker host, else the relay here.
url() {
  if gate_enabled; then
    printf 'http://127.0.0.1:%s/' "$(host_port)"
  else
    printf 'http://127.0.0.1:%s/ (inside this container only: OVIS_GATE=off)' "$OVIS_CONTAINER_PORT"
  fi
}

# --- the gate: publishing Ovis on the Docker host -------------------------------
# This container was created without published ports, and Docker cannot add one
# to a running container or to a container sharing its network namespace. So a
# small container runs the same relay, publishes the host's 127.0.0.1 only, and
# forwards over a dedicated network to this container by name (Docker's DNS,
# looked up per connection, so a restart that changes this container's address
# changes nothing). It restarts with Docker; with this container down it refuses
# connections. Hardened: read-only root, no capabilities, nobody, small limits.
self_container() { printf '%s' "${OVIS_SELF_CONTAINER:-$(hostname)}"; }

gate_target() {
  docker inspect -f '{{.Name}}' "$(self_container)" 2>/dev/null | sed 's#^/##'
}

gate_image() {
  if [[ -n "${OVIS_GATE_IMAGE:-}" ]]; then printf '%s' "$OVIS_GATE_IMAGE"; return 0; fi
  docker inspect -f '{{.Config.Image}}' "$(self_container)" 2>/dev/null
}

# Identifies what the gate runs: its whole `docker run` argument list (with no
# spec label), so a change to any part, the relay included, recreates it.
gate_spec() {
  gate_run_args "$1" "$2" "$3" "" | sha256sum | cut -c1-16
}

# NUL-separated: the relay source has newlines and is one argument.
gate_run_args() {
  local image="$1" target="$2" port="$3" spec="$4"
  printf '%s\0' \
    --name "$GATE_NAME" \
    --restart unless-stopped \
    --network "$GATE_NETWORK" \
    --label codex.role=ovis-gate \
    --label "codex.ovis.spec=$spec" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --user 65534:65534 \
    --memory 64m \
    --pids-limit 64 \
    -p "127.0.0.1:$port:$GATE_LISTEN_PORT" \
    --entrypoint python3 \
    "$image" \
    -c "$(cat "$ROOT/scripts/ovis_forward.py")" \
    0.0.0.0 "$GATE_LISTEN_PORT" "$target" "$OVIS_CONTAINER_PORT"
}

gate_ensure() {
  gate_enabled || return 0
  command -v docker >/dev/null 2>&1 || { warn "no docker CLI; Ovis is reachable inside this container only"; return 0; }
  local self target image port spec have args=()
  self="$(self_container)"
  target="$(gate_target)"
  [[ -n "$target" ]] || { warn "this container ($self) is unknown to Docker; set OVIS_SELF_CONTAINER or OVIS_GATE=off"; return 0; }
  image="$(gate_image)"
  [[ -n "$image" ]] || { warn "no image for the gate; set OVIS_GATE_IMAGE"; return 0; }
  port="$(host_port)"
  [[ "$port" =~ ^[0-9]+$ ]] && (( port > 0 && port < 65536 )) || { warn "OVIS_PORT=$port is not a port"; return 0; }
  spec="$(gate_spec "$image" "$target" "$port")"

  mkdir -p "$RUN_DIR"
  exec 8>"$RUN_DIR/gate.lock"
  flock 8

  docker network inspect "$GATE_NETWORK" >/dev/null 2>&1 \
    || docker network create --label codex.role=ovis-gate "$GATE_NETWORK" >/dev/null \
    || { warn "could not create the $GATE_NETWORK network"; exec 8>&-; return 0; }
  if ! docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$self" \
      | tr ' ' '\n' | grep -qx "$GATE_NETWORK"; then
    docker network connect "$GATE_NETWORK" "$self" >/dev/null \
      || { warn "could not connect $target to $GATE_NETWORK"; exec 8>&-; return 0; }
  fi

  have="$(docker inspect -f '{{index .Config.Labels "codex.ovis.spec"}}' "$GATE_NAME" 2>/dev/null || true)"
  if [[ -n "$have" && "$have" != "$spec" ]]; then
    docker rm -f "$GATE_NAME" >/dev/null 2>&1 || true
    have=""
  fi
  if [[ -z "$have" ]]; then
    mapfile -d '' -t args < <(gate_run_args "$image" "$target" "$port" "$spec")
    docker run -d "${args[@]}" >/dev/null \
      || { warn "could not start $GATE_NAME (is 127.0.0.1:$port free on the Docker host?)"; exec 8>&-; return 0; }
  elif [[ "$(docker inspect -f '{{.State.Running}}' "$GATE_NAME" 2>/dev/null)" != true ]]; then
    docker start "$GATE_NAME" >/dev/null || warn "could not start $GATE_NAME"
  fi
  exec 8>&-
}

gate_status() {
  if ! gate_enabled; then printf 'off (OVIS_GATE=off)'; return 0; fi
  command -v docker >/dev/null 2>&1 || { printf 'no docker CLI'; return 0; }
  local state
  state="$(docker inspect -f '{{.State.Status}}' "$GATE_NAME" 2>/dev/null || true)"
  if [[ -z "$state" ]]; then printf 'not created (./codex ovis gate ensure)'; return 0; fi
  printf '%s, 127.0.0.1:%s on the Docker host -> %s:%s' \
    "$state" "$(host_port)" "$(gate_target)" "$OVIS_CONTAINER_PORT"
}

gate_rm() {
  command -v docker >/dev/null 2>&1 || die "no docker CLI"
  docker rm -f "$GATE_NAME" >/dev/null 2>&1 || true
  docker network disconnect "$GATE_NETWORK" "$(self_container)" >/dev/null 2>&1 || true
  docker network rm "$GATE_NETWORK" >/dev/null 2>&1 || true
  ok "gate removed; Ovis is reachable inside this container only until the next start (OVIS_GATE=off keeps it off)"
}

# --- this workspace's edits to Ovis -------------------------------------------------
# scripts/ovis_patch.py: Codex 0.166's command table, plain panes for Ovis's
# own New workspace / New tab, and its picker defaulting to Codex. Refused on
# any bundle they were not written for, and then Ovis runs as shipped. A
# server older than the patched files runs the code from before them, so it is
# restarted (the supervisor brings it back).
PATCH_STAMP="$OVIS_DIR/.codex-workspace-patched"

patch_bundle() {
  local pid
  if ! python3 "$ROOT/scripts/ovis_patch.py" "$OVIS_DIR" >/dev/null 2>>"$LOG"; then
    warn "Ovis patches refused (see ${LOG}); Ovis runs as shipped, without Codex 0.166's commands or the Codex default for new workspaces"
    return 0
  fi
  pid="$(cat "$SERVER_PID" 2>/dev/null || true)"
  if [[ -n "$pid" && -d "/proc/$pid" && -f "$PATCH_STAMP" ]] \
     && (( $(stat -c %Y "/proc/$pid") < $(stat -c %Y "$PATCH_STAMP") )); then
    log "Ovis server predates its patches; restarting it to load them"
    kill "$pid" 2>/dev/null || true
  fi
  return 0
}

# --- start / stop / status -------------------------------------------------------
start() {
  local quiet=0; [[ "${1:-}" == "--quiet" ]] && quiet=1
  outside_cage
  if ! ovis_enabled; then
    (( quiet )) || dim "Ovis:    off (OVIS_ENABLED=0)"
    return 0
  fi
  if ! have_bundle; then
    # Soft: a clone without the bundle must still start and run Codex.
    (( quiet )) || warn "Ovis is enabled but not installed ($OVIS_DIR) — ./codex ovis install"
    return 0
  fi
  mkdir -p "$RUN_DIR"
  token >/dev/null
  patch_bundle
  if ! alive "$SUPERVISOR_PID"; then
    setsid "$ROOT/scripts/ovis.sh" supervise </dev/null >/dev/null 2>&1 &
  fi
  gate_ensure
  local i
  for i in $(seq 1 60); do
    healthy && break
    sleep 0.5
  done
  if ! healthy; then
    warn "Ovis did not answer within 30 s — see ${LOG}"
    return 0
  fi
  if (( ! quiet )); then
    if auth_off; then
      echo "Ovis:    $(url)  (no token: OVIS_TOKEN=off; sessions: ./codex ovis run)"
    else
      echo "Ovis:    $(url)  (token: ./codex ovis token; sessions: ./codex ovis run)"
    fi
  fi
}

# The supervisor's TERM trap removes the pid files, which can race this loop,
# so a vanished file is not an error. It then waits for the supervisor to be
# gone, so a `start` right after cannot find the old one still holding the lock.
stop() {
  outside_cage
  local p pid sup="" i
  [[ -f "$SUPERVISOR_PID" ]] && sup="$(cat "$SUPERVISOR_PID" 2>/dev/null || true)"
  for p in "$SUPERVISOR_PID" "$SERVER_PID" "$FORWARD_PID"; do
    pid="$(cat "$p" 2>/dev/null || true)"
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  if [[ -n "$sup" ]]; then
    for i in $(seq 1 20); do
      kill -0 "$sup" 2>/dev/null || break
      sleep 0.25
    done
  fi
  rm -f "$SUPERVISOR_PID" "$SERVER_PID" "$FORWARD_PID"
  ok "Ovis stopped; herdr and its panes are still running ($HERDR server stop ends them; the gate stays up)"
}

status() {
  local state
  if ! ovis_enabled; then state="off (OVIS_ENABLED=0)"
  elif ! have_bundle; then state="enabled, but not installed — ./codex ovis install"
  elif healthy; then state="up at $(url)"; auth_off && state+=" (no token: OVIS_TOKEN=off)"
  elif alive "$SUPERVISOR_PID"; then state="starting or restarting — see ${LOG}"
  else state="enabled, not running — ./codex ovis start"
  fi
  printf 'ovis         %s\n' "$state"
  have_bundle || return 0
  printf 'bundle       %s\n' "$(cat "$OVIS_DIR/bundle.json" 2>/dev/null)"
  if herdr_up; then
    # Parsed, not grepped: a pane whose session was reported names its agent twice.
    printf 'herdr        running, %s codex pane(s)\n' \
      "$("$HERDR" agent list 2>/dev/null | python3 -c '
import json, sys
try:
    agents = json.load(sys.stdin)["result"]["agents"]
except Exception:
    agents = []
print(sum(1 for a in agents if a.get("agent") == "codex"))' 2>/dev/null || echo '?')"
  else
    printf 'herdr        not running\n'
  fi
  printf 'gate         %s\n' "$(gate_status)"
  if auth_off; then
    printf 'access       no token (OVIS_TOKEN=off): open to anything that reaches the port\n'
  elif [[ -n "$(cfg OVIS_TOKEN)" ]]; then
    printf 'access       token required (OVIS_TOKEN)\n'
  else
    printf 'access       token required (%s)\n' "$TOKEN_FILE"
  fi
  printf 'log          %s\n' "$LOG"
}

# --- Codex in a herdr pane ---------------------------------------------------------
# herdr starts the pane's process from the SERVER's environment, not from this
# shell's, so a per-launch override (CODEX_NET=restricted ./codex ovis run)
# would be lost. The FORWARD_KEYS set here are written into a launch script,
# mode 0600, which deletes itself before it execs ./codex. Its PATH drops the
# shim, so nothing Codex runs reaches the shim again.
write_launch() {
  local dir="$1"; shift
  local f key
  mkdir -p "$RUN_DIR"
  f="$(mktemp "$RUN_DIR/launch.XXXXXX")"
  chmod 600 "$f"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'rm -f -- %q\n' "$f"
    printf 'cd -- %q || exit 1\n' "$dir"
    for key in "${FORWARD_KEYS[@]}"; do
      [[ -n "${!key+x}" ]] || continue
      printf 'export %s=%q\n' "$key" "${!key}"
    done
    printf 'PATH=%q\n' "$(strip_path "$PATH" "$SHIM_DIR")"
    printf 'export PATH\n'
    printf 'exec %q' "$CODEX_WRAPPER"
    if (( $# )); then
      printf ' %q' "$@"
    fi
    printf '\n'
  } > "$f"
  printf '%s' "$f"
}

run() {
  outside_cage
  local dir="$PWD"
  while (( $# )); do
    case "$1" in
      -w) dir="${2:-}"; shift 2 ;;
      --) shift; break ;;
      *)  break ;;
    esac
  done
  [[ -d "$dir" ]] || die "'$dir' is not a directory"
  dir="$(realpath -e -- "$dir")"
  ovis_enabled || die "Ovis is off (OVIS_ENABLED=0) — run ./codex directly, or turn it on"
  have_bundle || die "Ovis is not installed — ./codex ovis install"
  start --quiet
  herdr_up || die "herdr is not running — see ${LOG}"

  local created pane term launch
  # A plain pane: this launch carries the caller's keys, so the pane must not
  # start a Codex of its own (scripts/ovis-shims/pane-shell).
  created="$("$HERDR" workspace create --cwd "$dir" --label "codex $(basename "$dir")" --no-focus --env OVIS_PANE_PLAIN=1)" \
    || die "herdr could not create a workspace: $created"
  read -r pane term < <(python3 -c '
import json, sys
p = json.load(sys.stdin)["result"]["root_pane"]
print(p["pane_id"], p["terminal_id"])' <<< "$created") \
    || die "unrecognised reply from herdr workspace create: ${created:0:300}"

  launch="$(write_launch "$dir" "$@")"
  "$HERDR" pane run "$pane" "bash $(printf '%q' "$launch")" >/dev/null \
    || { rm -f -- "$launch"; die "herdr could not start Codex in pane $pane"; }

  if [[ ! -t 0 || ! -t 1 ]]; then
    ok "Codex started in herdr pane ${pane}; follow it in Ovis at $(url)"
    return 0
  fi
  dim "Codex in herdr pane ${pane} — Ctrl+B then Q detaches; Codex keeps running in Ovis ($(url))"
  exec "$HERDR" terminal attach "$term"
}

# Sourced (tests/test_ovis.py), it only defines the functions.
[[ "${BASH_SOURCE[0]}" == "$0" ]] || return 0

case "${1:-}" in
  start)     shift; start "$@" ;;
  stop)      stop ;;
  status)    status ;;
  token)     if auth_off; then echo "no token: OVIS_TOKEN=off" >&2; else token; echo; fi ;;
  enabled)   ovis_enabled ;;
  cfg)       [[ "${2:-}" =~ ^OVIS_[A-Z_]+$ ]] || die "cfg takes an OVIS_* setting"; cfg "$2"; echo ;;
  run)       shift; run "$@" ;;
  install)   shift; install_bundle "$@" ;;
  gate)
    case "${2:-ensure}" in
      ensure) outside_cage; gate_ensure; printf 'gate         %s\n' "$(gate_status)" ;;
      status) printf 'gate         %s\n' "$(gate_status)" ;;
      rm)     outside_cage; gate_rm ;;
      *)      die "unknown gate command '$2' — ensure, status or rm" ;;
    esac ;;
  supervise) outside_cage; mkdir -p "$RUN_DIR"; supervise ;;
  -h|--help|"") usage ;;
  *)         die "unknown command '$1' — ./codex ovis --help" ;;
esac
