#!/usr/bin/env bash
# Fetch the GVS5H reference loop (escalation/multiagent.py) that the team-solve parity test
# runs against. Pinned: the prompts in .codex/skills/team-solve/scripts/teamsolve/prompts.py
# are copied from this exact commit. Prints the escalation directory.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
COMMIT=b4cc2f734a28fdab6c37af48e612c4d9e40a7a20
REPO=https://github.com/slee-persis/GVS5H.git
SUBDIR=codebase/v2-current/escalation
DEST="${GVS5H_DIR:-$ROOT/.tmp/gvs5h-${COMMIT:0:7}}"

if [[ ! -f "$DEST/$SUBDIR/multiagent.py" ]]; then
  rm -rf "$DEST.partial"
  git clone -q --filter=blob:none --no-checkout "$REPO" "$DEST.partial" >&2
  git -C "$DEST.partial" sparse-checkout set "$SUBDIR" >&2
  git -C "$DEST.partial" checkout -q "$COMMIT" >&2
  rm -rf "$DEST"
  mkdir -p "$DEST"
  cp -a "$DEST.partial/." "$DEST/"
  rm -rf "$DEST.partial"
fi
[[ "$(git -C "$DEST" rev-parse HEAD)" == "$COMMIT" ]] || { echo "fetch_gvs5h: $DEST is not at $COMMIT" >&2; exit 1; }
printf '%s\n' "$DEST/$SUBDIR"
