#!/usr/bin/env bash
# Install or verify the vendored codex-security runtime that cxsec-security needs.
#
#   scripts/install.sh              install if absent, otherwise verify
#   scripts/install.sh --check      verify only, never touch the network or disk
#   scripts/install.sh --force      re-vendor from upstream over an existing tree
#
# Exit codes: 0 ok | 1 verification failed | 2 usage error | 3 missing prerequisite
#             4 fetch/extract failed | 5 patch failed

set -euo pipefail

UPSTREAM_REPO="openai/codex-security"
PINNED_VERSION="0.6.0"
PINNED_SHA="13dcc6fec00aaf4c23a6fa7e375ccc1726b22836"
SRC_SUBDIR="plugins/codex-security"
# shared/ must sit beside scripts/: workbench_schema.py reads ../shared/*.json at import.
VENDOR_DIRS=(scripts schemas references examples shared)
PATCH_NAME="0001-rebrand-sarif-output.patch"
# Sites the rebrand patch renames in finalize_scan_contract.py.
PATCH_SITES=4

SKILL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_SRC="$SKILL_DIR/runtime"
# Files this port carries that upstream no longer ships. Restored into $CXSEC_HOME
# after vendoring, with their path under this dir mirrored into the install. See
# patches/README.md ("Local additions").
LOCAL_ADD_DIR="$SKILL_DIR/assets/local-additions"
CXSEC_HOME="${CXSEC_HOME:-$HOME/.claude/codex-security}"
REF="$PINNED_SHA"
MODE="auto"
SOURCE="auto"
RUN_SMOKE=1
FAILURES=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "$C_DIM" "$C_OFF" "$*"; }
ok()   { printf '  %s[ ok ]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '  %s[warn]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
bad()  { printf '  %s[fail]%s %s\n' "$C_ERR" "$C_OFF" "$*"; FAILURES=$((FAILURES + 1)); }
die()  { printf '%s[fail]%s %s\n' "$C_ERR" "$C_OFF" "$1" >&2; exit "${2:-1}"; }

usage() {
  cat <<EOF
Install or verify the codex-security runtime for the cxsec-security skill.

Usage: install.sh [options]

  --check, --verify   Verify an existing install. No network, no writes.
  --force             Re-vendor even if already installed.
  --local             Install from the bundled runtime/ tree. No network.
  --ref <sha|tag>     Vendor a ref other than the pinned $PINNED_SHA.
                      The local patch may not apply to an unpinned ref.
  --home <dir>        Install root. Default \$CXSEC_HOME, else ~/.claude/codex-security.
  --no-smoke          Skip the end-to-end finalizer smoke test.
  -h, --help          This text.

Installs $UPSTREAM_REPO@${PINNED_VERSION} ($SRC_SUBDIR) into \$CXSEC_HOME:
${VENDOR_DIRS[*]} plus LICENSE, then applies $PATCH_NAME and restores the
local additions (helpers upstream moved into its unvendored MCP app).

Source: upstream by default, falling back to the bundled runtime/ tree when the
download fails. --local skips the network entirely. The runtime itself never makes
network calls once installed.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check|--verify) MODE="check"; shift ;;
    --force)          MODE="force"; shift ;;
    --local)          SOURCE="local"; shift ;;
    --no-smoke)       RUN_SMOKE=0; shift ;;
    --ref)            [ $# -ge 2 ] || die "--ref needs a value" 2; REF="$2"; shift 2 ;;
    --home)           [ $# -ge 2 ] || die "--home needs a value" 2; CXSEC_HOME="$2"; shift 2 ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "unknown option: $1 (try --help)" 2 ;;
  esac
done

CXSEC_HOME="${CXSEC_HOME%/}"
PY="${PYTHON:-python3}"

have() { command -v "$1" >/dev/null 2>&1; }

# Temp dirs are tracked and removed once, on exit. Do not use RETURN traps here:
# a RETURN trap set inside a function stays registered and re-fires when the
# calling function returns, by which point its local $tmp is gone (set -u kill).
TMPDIRS=()
cleanup_tmpdirs() {
  local d
  for d in ${TMPDIRS[@]+"${TMPDIRS[@]}"}; do rm -rf "$d"; done
}
trap cleanup_tmpdirs EXIT

mktmp() {
  local d
  d="$(mktemp -d)"
  TMPDIRS+=("$d")
  printf '%s' "$d"
}

require_tool() {
  have "$1" || die "missing prerequisite: $1${2:+ ($2)}" 3
}

# ---------------------------------------------------------------- verification

check_python() {
  have "$PY" || { bad "no $PY on PATH — the runtime is stdlib Python, but it needs an interpreter"; return; }
  local v
  v="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?")"
  if "$PY" -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
    ok "python $v"
  elif "$PY" -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
    if "$PY" -c 'import tomli' 2>/dev/null; then
      warn "python $v — below the 3.11 floor, but tomli is installed so the deep-scan config can still read TOML"
    else
      bad "python $v is too old — need 3.11+ for stdlib tomllib, or 3.9+ with tomli installed"
    fi
  else
    bad "python $v is too old — need 3.11+ (3.9+ only with tomli installed)"
  fi
}

check_layout() {
  [ -d "$CXSEC_HOME" ] || { bad "\$CXSEC_HOME does not exist: $CXSEC_HOME"; return; }
  local d missing=0
  for d in scripts schemas; do
    [ -d "$CXSEC_HOME/$d" ] || { bad "missing $d/ under $CXSEC_HOME"; missing=1; }
  done
  [ "$missing" -eq 0 ] || return
  # finalize_scan_contract.py resolves schemas as <script_parent>/../schemas.
  local resolved
  resolved="$(cd "$CXSEC_HOME/scripts/.." && pwd)/schemas"
  if [ -d "$resolved" ]; then
    ok "scripts/ and schemas/ are siblings (schema root resolves)"
  else
    bad "scripts/ and schemas/ are not siblings — report generation cannot resolve schemas"
  fi
  for d in references shared; do
    [ -d "$CXSEC_HOME/$d" ] && ok "$d/ present" || warn "$d/ absent (re-run with --force to vendor it)"
  done
  # Mode 10's validator resolves the plugin root as parents[3], so this exact depth is
  # load-bearing: skills/assess-patch-risk/scripts/ must sit directly under $CXSEC_HOME.
  if [ -f "$CXSEC_HOME/skills/assess-patch-risk/scripts/validate_patch_risk_assessment.py" ]; then
    ok "patch-risk validator present at its load-bearing depth"
  else
    warn "skills/assess-patch-risk/scripts/ absent — mode 10 cannot validate an assessment (--force restores it)"
  fi
  [ -f "$CXSEC_HOME/LICENSE" ] && ok "LICENSE present" || warn "LICENSE absent — this runtime is Apache-2.0, ship the license"
}

check_scripts() {
  local s
  for s in finalize_scan_contract.py validate_scan_contract.py \
           report_projection.py validate_tracking_source.py normalize_candidates.py \
           generate_in_scope_files.py generate_rank_input.py resolve_security_md.py \
           copy_deep_review_input.py bind_repo_scopes.py threat_model_projection.py; do
    if [ -f "$CXSEC_HOME/scripts/$s" ]; then
      if "$PY" "$CXSEC_HOME/scripts/$s" --help >/dev/null 2>&1; then
        ok "$s runs"
      else
        bad "$s present but --help failed (broken interpreter or truncated file)"
      fi
    else
      bad "$s missing"
    fi
  done
}

check_patch_applied() {
  local f="$CXSEC_HOME/scripts/finalize_scan_contract.py"
  [ -f "$f" ] || return
  local upstream_hits cxsec_hits
  upstream_hits="$(grep -c 'codexSecurity\|"Codex Security"' "$f" || true)"
  cxsec_hits="$(grep -c 'cxsec' "$f" || true)"
  if [ "$upstream_hits" -eq 0 ] && [ "$cxsec_hits" -ge "$PATCH_SITES" ]; then
    ok "SARIF rebrand patch applied ($cxsec_hits cxsec sites, 0 upstream)"
  else
    bad "SARIF rebrand not applied: $upstream_hits upstream branding sites, $cxsec_hits cxsec sites (want 0 and $PATCH_SITES)"
  fi
}

# End-to-end proof: take the vendored upstream example scan, strip the fields the
# finalizer is supposed to derive, finalize it, and assert the real outputs.
check_smoke() {
  local ex="$CXSEC_HOME/examples/completed-scan"
  if [ ! -d "$ex" ]; then
    warn "examples/completed-scan absent — skipping end-to-end test (re-run with --force to vendor it)"
    return
  fi
  local tmp
  tmp="$(mktmp)"
  cp "$ex"/scan-manifest.json "$ex"/findings.json "$ex"/coverage.json "$tmp/" 2>/dev/null || {
    bad "example scan is incomplete"; return; }

  "$PY" - "$tmp" <<'PY' || { bad "could not build an unsealed draft from the example scan"; return; }
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
m = json.loads((d / "scan-manifest.json").read_text())
m["scan"].pop("sealedAt", None)
m["scan"].pop("artifacts", None)
(d / "scan-manifest.json").write_text(json.dumps(m, indent=2))
f = json.loads((d / "findings.json").read_text())
for item in f.get("findings", []):
    for derived in ("findingId", "occurrenceId", "fingerprints"):
        item.pop(derived, None)
(d / "findings.json").write_text(json.dumps(f, indent=2))
PY

  if ! "$PY" "$CXSEC_HOME/scripts/finalize_scan_contract.py" \
        --scan-dir "$tmp" --source-root "$tmp" >"$tmp/finalize.log" 2>&1; then
    bad "finalizer failed on the example scan:"
    sed 's/^/         /' "$tmp/finalize.log" >&2
    return
  fi

  local report="$tmp/report.md" sarif="$tmp/exports/results.sarif"
  [ -s "$report" ] || { bad "finalizer produced no report.md"; return; }
  [ -s "$sarif" ]  || { bad "finalizer produced no SARIF"; return; }

  if "$PY" - "$tmp" <<'PY'
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
s = json.loads((d / "exports" / "results.sarif").read_text())
assert s.get("version") == "2.1.0", "SARIF version is %r" % s.get("version")
run = s["runs"][0]
assert run["tool"]["driver"]["name"] == "cxsec-security", \
    "SARIF attributed to %r" % run["tool"]["driver"]["name"]
assert run["results"], "SARIF has no results"
assert "cxsec/v1" in run["results"][0].get("partialFingerprints", {}), \
    "fingerprint namespace not rebranded"
m = json.loads((d / "scan-manifest.json").read_text())
assert m["scan"].get("sealedAt"), "finalizer did not seal the manifest"
assert m["scan"].get("artifacts"), "finalizer did not derive the artifact list"
f = json.loads((d / "findings.json").read_text())
assert f["findings"][0].get("findingId"), "finalizer did not derive findingId"
PY
  then
    ok "end-to-end: draft -> sealed manifest, report.md, SARIF 2.1.0 attributed to cxsec-security"
  else
    bad "finalizer output failed its assertions (see above)"
    return
  fi

  # Upstream retired validate_report_format.py. validate_scan_contract.py is the stronger
  # gate: it re-reads the sealed bundle whole — manifest, findings, coverage and the
  # generated report — and refuses anything the finalizer left inconsistent.
  if "$PY" "$CXSEC_HOME/scripts/validate_scan_contract.py" --scan-dir "$tmp" >"$tmp/validate.log" 2>&1 \
     && grep -q '"status": *"valid"' "$tmp/validate.log"; then
    ok "sealed bundle re-validates: validate_scan_contract.py reports status valid"
  else
    bad "sealed bundle fails validate_scan_contract.py:"
    sed 's/^/         /' "$tmp/validate.log" >&2
  fi

  check_patch_risk_validator "$tmp"
}

# Mode 10's validator loads finalize_scan_contract.py by path and resolves the schema as
# parents[3]/schemas. Prove both resolve, by rejecting an assessment we know is invalid:
# a silent import or schema-path break would otherwise only surface mid-assessment.
check_patch_risk_validator() {
  local tmp="$1"
  local v="$CXSEC_HOME/skills/assess-patch-risk/scripts/validate_patch_risk_assessment.py"
  [ -f "$v" ] || return 0
  printf '%s' '{"schemaVersion":"patch-risk-assessment/v0"}' >"$tmp/bad-assessment.json"
  local out
  out="$("$PY" "$v" "$tmp/bad-assessment.json" 2>&1)" && {
    bad "patch-risk validator accepted an assessment missing every required field"; return; }
  if printf '%s' "$out" | grep -qi 'traceback\|ModuleNotFoundError\|No such file'; then
    bad "patch-risk validator crashed instead of rejecting: $(printf '%s' "$out" | tail -1)"
  else
    ok "patch-risk validator resolves its schema and rejects an invalid assessment"
  fi
}

# Optional transports. Never fatal: modes 1-3, 5-7 and 9 need none of this.
report_transports() {
  step "Optional transports (modes 4 and 8)"
  if have gh; then
    if gh auth status >/dev/null 2>&1; then
      ok "gh authenticated — GitHub advisory/issue filing and REST finding intake available"
    else
      warn "gh present but not authenticated — run: gh auth login"
    fi
  else
    warn "gh absent — GitHub intake (mode 4) and GitHub filing (mode 8) unavailable"
  fi
  say "  ${C_DIM}Linear and Jira need connected MCP servers. This installer cannot add them:"
  say "  configure them in your MCP settings, then re-check tool presence in-session.${C_OFF}"
}

verify_all() {
  step "Verifying $CXSEC_HOME"
  check_python
  check_layout
  check_scripts
  check_patch_applied
  if [ "$RUN_SMOKE" -eq 1 ]; then check_smoke; fi
  return 0
}

# ------------------------------------------------------------------- install

# Copy the tree that ships in this repo. Already patched, so apply_patch will
# report "already applied" rather than patching twice.
stage_from_local() {
  local stage="$1"
  [ -d "$LOCAL_SRC/scripts" ] && [ -d "$LOCAL_SRC/schemas" ] \
    || die "no bundled runtime at $LOCAL_SRC — drop --local to fetch from upstream" 4

  step "Staging from bundled runtime/"
  mkdir -p "$stage"
  local d
  for d in "${VENDOR_DIRS[@]}"; do
    if [ -d "$LOCAL_SRC/$d" ]; then
      cp -a "$LOCAL_SRC/$d" "$stage/$d"
      find "$stage/$d" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
      ok "$d/ ($(find "$stage/$d" -type f | wc -l) files)"
    else
      warn "$d/ not in the bundled runtime — skipped"
    fi
  done
  for f in LICENSE PROVENANCE.md; do
    if [ -f "$LOCAL_SRC/$f" ]; then cp -a "$LOCAL_SRC/$f" "$stage/$f"; ok "$f"; fi
  done
  SOURCE="local"
}

fetch_and_stage() {
  local stage="$1" tmp
  tmp="$(mktmp)"

  step "Fetching $UPSTREAM_REPO@${REF:0:12}"
  local tarball="$tmp/src.tar.gz"
  if have gh && gh api "repos/$UPSTREAM_REPO/tarball/$REF" >"$tarball" 2>"$tmp/fetch.err"; then
    ok "fetched via gh api ($(wc -c <"$tarball") bytes)"
  elif have curl && curl -fsSL "https://codeload.github.com/$UPSTREAM_REPO/tar.gz/$REF" -o "$tarball"; then
    ok "fetched via codeload ($(wc -c <"$tarball") bytes)"
  elif [ -d "$LOCAL_SRC/scripts" ]; then
    warn "download failed — falling back to the bundled runtime/ tree"
    stage_from_local "$stage"
    return 0
  else
    say "$(cat "$tmp/fetch.err" 2>/dev/null || true)" >&2
    die "could not download $UPSTREAM_REPO@$REF — need authenticated gh or network access to codeload.github.com" 4
  fi

  tar -xzf "$tarball" -C "$tmp" || die "tarball did not extract" 4
  local root
  root="$(find "$tmp" -maxdepth 1 -mindepth 1 -type d -name "*codex-security*" | head -1)"
  [ -n "$root" ] || die "unexpected tarball layout: no top-level source directory" 4
  local src="$root/$SRC_SUBDIR"
  if [ ! -d "$src" ]; then
    warn "$SRC_SUBDIR not present at this ref — upstream moved the plugin again; searching for it"
    local found
    found="$(find "$root" -type f -name finalize_scan_contract.py -path '*/scripts/*' | head -1)"
    [ -n "$found" ] || die "no scripts/finalize_scan_contract.py anywhere in the tarball — this ref does not ship the plugin runtime" 4
    src="$(cd "$(dirname "$found")/.." && pwd)"
    warn "found it at ${src#$root/} — update SRC_SUBDIR in this script"
  fi

  step "Staging"
  mkdir -p "$stage"
  local d
  for d in "${VENDOR_DIRS[@]}"; do
    if [ -d "$src/$d" ]; then
      cp -a "$src/$d" "$stage/$d"
      ok "$d/ ($(find "$stage/$d" -type f | wc -l) files)"
    else
      warn "$d/ not present upstream at this ref — skipped"
    fi
  done
  if [ -f "$root/LICENSE" ]; then
    cp -a "$root/LICENSE" "$stage/LICENSE"; ok "LICENSE"
  else
    warn "no LICENSE at the repo root — check upstream licensing before redistributing"
  fi
}

apply_patch() {
  local patch_file=""
  if [ -f "$SKILL_DIR/assets/$PATCH_NAME" ]; then
    patch_file="$SKILL_DIR/assets/$PATCH_NAME"
  elif [ -f "$CXSEC_HOME/patches/$PATCH_NAME" ]; then
    patch_file="$CXSEC_HOME/patches/$PATCH_NAME"
  else
    warn "no $PATCH_NAME found — generated SARIF will be attributed to Codex Security"
    return 0
  fi

  step "Applying $PATCH_NAME"
  local target="$CXSEC_HOME/scripts/finalize_scan_contract.py"

  # The bundled runtime/ tree ships pre-patched. Detect that before running patch,
  # so it never prints "Reversed patch detected" or drops a .rej in the install.
  if [ -f "$target" ] \
     && ! grep -q 'codexSecurity\|"Codex Security"' "$target" \
     && [ "$(grep -c 'cxsec' "$target")" -ge "$PATCH_SITES" ]; then
    ok "already applied (source tree was pre-patched)"
    mkdir -p "$CXSEC_HOME/patches"
    cp -a "$patch_file" "$CXSEC_HOME/patches/$PATCH_NAME"
    return 0
  fi

  require_tool patch "needed to apply the SARIF rebrand"
  # -r - discards rejects rather than littering the install.
  if ( cd "$CXSEC_HOME" && patch -p1 --forward --silent -r - <"$patch_file" ); then
    ok "applied"
  else
    die "patch failed against ${REF:0:12} — upstream changed the SARIF emitter; refresh $PATCH_NAME" 5
  fi
  find "$CXSEC_HOME/scripts" -name '*.rej' -o -name '*.orig' -delete 2>/dev/null || true
  mkdir -p "$CXSEC_HOME/patches"
  cp -a "$patch_file" "$CXSEC_HOME/patches/$PATCH_NAME"
}

# Restore files this port carries that upstream has since deleted. Each lands at its
# path relative to $LOCAL_ADD_DIR, so a file at assets/local-additions/scripts/foo.py
# installs as $CXSEC_HOME/scripts/foo.py. Runs after vendoring (which wipes scripts/)
# so a --force re-vendor never loses them. Currently the Python helpers upstream
# ported into its (unvendored) TypeScript MCP launcher: resolve_security_md.py,
# normalize_candidates.py, copy_deep_review_input.py, bind_repo_scopes.py and
# skills/assess-patch-risk/scripts/validate_patch_risk_assessment.py. See
# patches/README.md.
restore_local_additions() {
  [ -d "$LOCAL_ADD_DIR" ] || return 0
  local added=0 rel dest f
  while IFS= read -r -d '' f; do
    rel="${f#"$LOCAL_ADD_DIR"/}"
    dest="$CXSEC_HOME/$rel"
    mkdir -p "$(dirname "$dest")"
    cp -a "$f" "$dest"
    ok "local addition: $rel"
    added=$((added + 1))
  done < <(find "$LOCAL_ADD_DIR" -name __pycache__ -prune -o -type f -print0 2>/dev/null)
  [ "$added" -gt 0 ] || true
}

write_provenance() {
  if [ "$SOURCE" = "local" ]; then
    if [ -f "$LOCAL_SRC/PROVENANCE.md" ]; then
      cp -a "$LOCAL_SRC/PROVENANCE.md" "$CXSEC_HOME/PROVENANCE.md"
      printf '\nInstalled from the bundled `runtime/` tree by `install.sh --local` on %s.\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$CXSEC_HOME/PROVENANCE.md"
      ok "PROVENANCE.md carried over from the bundled runtime"
    else
      warn "bundled runtime has no PROVENANCE.md — upstream version unrecorded"
    fi
    return 0
  fi
  local resolved="$REF"
  if have gh; then
    resolved="$(gh api "repos/$UPSTREAM_REPO/commits/$REF" --jq .sha 2>/dev/null || printf '%s' "$REF")"
  fi
  cat >"$CXSEC_HOME/PROVENANCE.md" <<EOF
# codex-security runtime (vendored)

Vendored from [$UPSTREAM_REPO](https://github.com/$UPSTREAM_REPO),
plugin version \`$PINNED_VERSION\`, commit \`${resolved:0:7}\`.
Full commit: \`$resolved\`
Licensed Apache-2.0 (see LICENSE).

Source path: \`$SRC_SUBDIR/{$(IFS=,; echo "${VENDOR_DIRS[*]}")}\`.

Installed by \`cxsec-security/scripts/install.sh\` on $(date -u +%Y-%m-%dT%H:%M:%SZ).

This is the offline, model-agnostic half of the upstream plugin: stdlib-only Python
that validates scan-contract artifacts and deterministically generates \`report.md\`
and SARIF 2.1.0. It makes no network calls and needs no OpenAI credentials.

The upstream MCP server (\`mcp-app/\`) and the Codex-CLI-driven
\`deep-security-scan\` worker fan-out are deliberately NOT vendored.

Layout is load-bearing: \`finalize_scan_contract.py\` resolves schemas as
\`<script_parent>/../schemas\`, so \`scripts/\` and \`schemas/\` must stay siblings.

## Local modifications

The vendored code is **not** a pristine copy. See \`patches/README.md\`.

- \`patches/$PATCH_NAME\` — renames the four cosmetic SARIF branding sites in
  \`scripts/finalize_scan_contract.py\` so generated SARIF is attributed to
  \`cxsec-security\`.
- \`scripts/resolve_security_md.py\` — a carried-forward local addition. Upstream
  ported the offline SECURITY.md policy resolver into the MCP app in 0.1.28
  (\`launch_codex_security_mcp --helper resolve-security-md\`) and deleted the
  standalone helper. This port does not vendor the MCP app, so it keeps the last
  upstream Python version (byte-identical 0.1.24–0.1.26), which mode 7 and the
  repo-scan policy chain invoke. Self-contained stdlib; same concatenation
  semantics the TS helper documents.
- \`scripts/normalize_candidates.py\` — a carried-forward local addition. Upstream
  ported diff-scan candidate normalization into the MCP app in 0.2.0 and deleted
  the Python helper. Kept at its last upstream version with the later TypeScript
  behaviour changes ported back (raw POSIX path bytes, final-row and ambiguous
  carriage-return handling). Output and \`candidate_id\` values are unchanged.
- \`skills/assess-patch-risk/scripts/validate_patch_risk_assessment.py\` — a
  carried-forward local addition, kept at its upstream depth (it resolves the
  plugin root as \`parents[3]\`). Upstream ported it into the MCP app in 0.3.0;
  the carried copy includes the later rule rejecting \`merge\` with critical
  regression likelihood.
- \`scripts/copy_deep_review_input.py\`, \`scripts/bind_repo_scopes.py\` — carried-forward
  local additions. Upstream retired these \`generate_rank_input.py\` subcommands
  (\`copy-deep-review-input\`, \`bind-repo-scopes\`) into the MCP launcher in 0.3.0.
  Re-implemented from the TypeScript helpers; same CLI and output format.

All carried helpers are held to upstream's current TypeScript test suites, ported to
pytest under the skill repo's \`tests/\`.

Re-vendoring overwrites the patch and removes the additions. \`install.sh\` re-applies the patch and restores the
local additions automatically, then verifies the result.

Canonical wire literals (\`codex-security.*\` documentTypes, the
\`codex-security/v1\` fingerprint algorithm, the \`codex-security-snapshot/v1\`
digest prefix) are intentionally unchanged — they are stamped by the finalizer,
never authored, and \`mode-triage\` relies on them to ingest genuine upstream
artifacts.
EOF
  ok "PROVENANCE.md written (${resolved:0:12})"
}

do_install() {
  local stage
  stage="$(mktmp)/stage"
  if [ "$SOURCE" = "local" ]; then
    stage_from_local "$stage"
  else
    require_tool tar
    have gh || have curl || [ -d "$LOCAL_SRC/scripts" ] \
      || die "need gh or curl to download the runtime, or a bundled runtime/ tree" 3
    fetch_and_stage "$stage"
  fi

  step "Installing into $CXSEC_HOME"
  mkdir -p "$CXSEC_HOME"
  local d
  for d in "${VENDOR_DIRS[@]}"; do
    [ -d "$stage/$d" ] || continue
    if [ -e "$CXSEC_HOME/$d" ]; then
      local backup="$CXSEC_HOME/$d.bak.$(date -u +%Y%m%dT%H%M%SZ)"
      mv "$CXSEC_HOME/$d" "$backup"
      warn "existing $d/ moved to $(basename "$backup")"
    fi
    cp -a "$stage/$d" "$CXSEC_HOME/$d"
  done
  if [ -f "$stage/LICENSE" ]; then cp -a "$stage/LICENSE" "$CXSEC_HOME/LICENSE"; fi
  ok "vendored ${VENDOR_DIRS[*]}"

  apply_patch
  restore_local_additions
  write_provenance
}

# ----------------------------------------------------------------------- main

say "cxsec-security runtime installer"
say "${C_DIM}skill: $SKILL_DIR${C_OFF}"
say "${C_DIM}\$CXSEC_HOME: $CXSEC_HOME${C_OFF}"

INSTALLED=0
if [ -d "$CXSEC_HOME/scripts" ] && [ -d "$CXSEC_HOME/schemas" ]; then INSTALLED=1; fi

case "$MODE" in
  check)
    # Verify whatever is there. A partial tree must report which piece is missing,
    # not bail out with a blanket "nothing installed".
    [ -d "$CXSEC_HOME" ] || die "nothing at $CXSEC_HOME — run install.sh without --check" 1
    verify_all
    ;;
  force)
    do_install
    verify_all
    ;;
  auto)
    if [ "$INSTALLED" -eq 1 ]; then
      say "${C_DIM}runtime already present — verifying instead of re-downloading${C_OFF}"
      verify_all
      if [ "$FAILURES" -gt 0 ]; then
        say ""
        say "Existing install is broken. Re-vendor with: $0 --force"
        exit 1
      fi
    else
      do_install
      verify_all
    fi
    ;;
esac

report_transports

step "Result"
if [ "$FAILURES" -eq 0 ]; then
  ok "cxsec-security runtime ready at $CXSEC_HOME"
  say ""
  say "  Scan modes call:"
  say "    ${C_DIM}python3 \$CXSEC_HOME/scripts/finalize_scan_contract.py --scan-dir <dir> --source-root <repo>${C_OFF}"
  exit 0
fi
bad_count="$FAILURES"
say ""
die "$bad_count check(s) failed — see above" 1
