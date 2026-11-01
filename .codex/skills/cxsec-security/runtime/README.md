# codex-security → Claude Code port

Port of the security-review doctrine from
[openai/codex-security](https://github.com/openai/codex-security) to Claude Code.
Apache-2.0. See `PROVENANCE.md` for the exact upstream version and commit.

## What is installed

**This runtime** (`~/.claude/codex-security`, referred to as `$CXSEC_HOME`):

- `scripts/` — upstream's stdlib-only Python helpers, including the `workbench/`
  subpackage, plus four carried local additions (`resolve_security_md.py`,
  `normalize_candidates.py`, `copy_deep_review_input.py`, `bind_repo_scopes.py`).
  No pip install, no network calls, no OpenAI credentials. Python 3.11+
  (`tomllib`). The two `.mjs` modules upstream ships here are never run.
- `schemas/` — 12 JSON Schemas for the scan contract and patch-risk assessment.
- `references/` — the full upstream shared reference set.
- `shared/` — workbench migration tables; `workbench_schema.py` reads them as
  `<script_parent>/../shared`, so it must sit beside `scripts/`.
- `examples/completed-scan/` — upstream example artifacts, used as the installer's
  end-to-end smoke fixture.
- `skills/assess-patch-risk/scripts/` — mode 10's assessment validator (carried;
  upstream moved it into its launcher), kept at its upstream depth because it
  resolves the plugin root as `parents[3]`.

Install, repair, or verify this runtime with the skill's installer:
`bash ~/.claude/skills/cxsec-security/scripts/install.sh [--check|--force]`.
It vendors the pinned upstream tree, re-applies the SARIF rebrand patch, and proves
the result by finalizing the example scan. See `references/install.md` in the skill.

**One skill** at `~/.claude/skills/cxsec-security/` — a router `SKILL.md` (~2k tok)
plus 40 on-demand reference files, loaded per mode:

| # | Mode | Reference |
|---|---|---|
| 1 | Repository scan (default) | `mode-repo-scan.md` |
| 2 | Diff scan (PR/commit/branch/patch) | `mode-diff-scan.md` |
| 3 | Deep multi-pass scan | `mode-deep-scan.md` |
| 4 | Triage existing findings | `mode-triage.md` |
| 5 | Fix and verify a finding (or verify-only) | `mode-fix.md` |
| 6 | Vulnerability write-up | `mode-writeup.md` |
| 7 | Author/review `SECURITY.md` | `mode-policy.md` |
| 8 | File findings (Linear/Jira/GitHub) | `mode-track.md` |
| 9 | Structural hardening | `mode-harden.md` |
| 10 | Patch merge-risk assessment | `mode-patch-risk.md` |

Modes 1 and 3 perform the single self-contained audit in `core-scan.md`. Mode 2
invokes `phase-1-threat-model.md` → `phase-2-discovery.md` →
`phase-3-validation.md` → `phase-4-attack-path.md` internally. Phase files are
never entry points.

`references/workbench-file-protocol.md` is the authoritative mapping from every
upstream MCP tool to its file-based replacement. Read it before any scan mode.

## The load-bearing constraint

`finalize_scan_contract.py` resolves schemas as `<script_parent>/../schemas`, so
**`scripts/` and `schemas/` must remain siblings.** Moving one breaks report
generation.

## Generating a report

The finalizer is the payload: it validates hand-authored canonical artifacts and
deterministically emits `report.md`, `threatmodel.md` (a projection of
`scan.threatModel`), and SARIF 2.1.0.

```bash
python3 $CXSEC_HOME/scripts/finalize_scan_contract.py \
  --scan-dir <scan_dir> --source-root <repo_root>
```

Author `scan-manifest.json`, `findings.json`, and `coverage.json` in `<scan_dir>`
as an **unsealed draft**: omit `scan.sealedAt` and `scan.artifacts`, and omit each
finding's `findingId`, `occurrenceId`, and `fingerprints`. The finalizer derives
those deterministically. Supplying them by hand fails validation.

`scan.target.snapshotDigest` is required for `git_worktree`, `git_diff`, and
`directory_snapshot` target kinds. Compute it with:

```bash
cd $CXSEC_HOME/scripts && python3 -c "
import sys; sys.path.insert(0,'.')
from pathlib import Path
import workbench_target as wt
print(wt.worktree_content_digest(Path('<repo_root>')))"
```

Related helpers: `validate_scan_contract.py --scan-dir <dir>` (re-validate a
sealed bundle without mutating it — upstream retired `validate_report_format.py`
in favour of this), `report_projection.py`, `threat_model_projection.py`,
`resolve_security_md.py`, `normalize_candidates.py`, `copy_deep_review_input.py`,
`bind_repo_scopes.py`, `validate_tracking_source.py`, `generate_rank_input.py`
(`make-repo-scope-input`, `make-diff-rank-input`), `generate_in_scope_files.py`,
`deep_scan_config.py`, and `workbench_db.py finding-issues` (mode 8's local issue
history).

## Runtime tools

There is no capability preflight. Upstream retired `config_preflight.py` and
`preflight/capability-profiles.toml` in 0.5.0 and replaced the gate with
`references/scan-runtime.md`: use the delegation tools actually present (load a
deferred `Agent` tool once via tool search), fall back to doing the work yourself,
and never block source review on optional tools.

## Deliberately not ported

- **The upstream MCP server** (`mcp-app/`). It is a local SQLite scan-state
  workbench, not a service client, and it runs without credentials — but its
  scan-start tools require a Codex thread id in MCP `_meta`, which Claude Code does
  not send. The Python layer reaches the same state directly, so the server adds
  nothing here. Its tools are mapped to file operations in
  `references/workbench-file-protocol.md`. Helpers upstream moved into its
  TypeScript launcher are carried as Python (see `patches/README.md`).
- **The Codex-managed deep-scan coordinator.** Upstream spawns real `codex` CLI
  subprocesses under a reserved worker permission profile. `mode-deep-scan.md`
  keeps the doctrine — independent complete rounds, saturation via
  `stopAfterNoNew`, the reduction boundary, "recurrence is search evidence, not
  reportability proof" — and runs the rounds with `Agent` subagents instead.
- **The Codex desktop reference** (`desktop-scan.md`) and the ChatGPT "Daybreak"
  access advisory (`get_codex_security_daybreak_access`). Desktop-app and
  hosted-account only; neither gates a scan upstream either.
- **`publish-findings` and `cloud-findings`** (new upstream skills). Both need the
  `codex-security` npm CLI and/or Codex Security Cloud with a ChatGPT login; mode 4
  already ingests SARIF, scanner output, and advisories directly.
- **`references/standalone-validation.md`**. Only upstream's MCP server uses it,
  as a custom-validation prompt; mode 4 covers standalone finding assessment.
- **The Codex desktop setup UI, goals, handoff/claim, and remediation-request
  flows.** No equivalent exists. Coverage objectives are stated in the first visible
  update; `<scan_dir>` is the durable state.
- **Codex project GitHub attachment** as a repository-inference source. Falls back
  to the local repository's GitHub remote.

## Transport availability for mode-track

All four tracking transports are now wired:

| Transport | Status |
|---|---|
| GitHub issues / advisories | Works — pre-authenticated `gh` CLI |
| Linear | MCP server registered at user scope; **needs OAuth** |
| Jira | MCP server registered at user scope; **needs OAuth** |

```
linear    : https://mcp.linear.app/mcp        (http)
atlassian : https://mcp.atlassian.com/v1/sse  (sse)
```

Complete authentication with `/mcp` in-session — it needs an interactive browser
login and cannot be done non-interactively. Until then both report
`! Needs authentication` and `mode-track` will stop rather than substitute another
transport.

Note: Linear's `/sse` endpoint returns 404 and is retired; `/mcp` streamable-HTTP
is the live one. Also, interactively-authenticated MCP servers can be absent from
headless or cron runs, so a scheduled scan may find Linear/Jira unavailable even
after you authenticate here.

`mode-track` forbids falling back to direct Jira/Linear REST with an API token.
That is an upstream disclosure-boundary rule — the OAuth connector enforces
per-user permissions while a raw token usually has broader access. Relax it only
deliberately.

## SARIF attribution

Generated SARIF is attributed to this port, not to Codex Security:

```
tool.driver.name    : cxsec-security
partialFingerprints : cxsec/v1
run.properties      : cxsecSchemaVersion, cxsecTargetKind
```

This is a local patch to vendored code — see `patches/README.md`. Nothing reads
these values back, but `tool.driver.name` and the `partialFingerprints` namespace
are what GitHub code scanning uses to correlate alerts across uploads, so treat
further renames as a re-baseline event.

## Porting notes

All 13 upstream skills are ported into the single `cxsec-security` skill. Changes
applied, in case you extend it:

1. Top-level skills became `references/mode-*.md`; the four phase skills became
   `references/phase-*.md`. Their frontmatter was converted to a
   `<!-- when-to-use: … -->` comment so the router can quote the trigger without a
   second frontmatter block.
2. `agents/openai.yaml` deleted — Codex-only, and the sole source of `$skill`
   self-references.
3. All reference paths flattened into one `references/` namespace: 87 path
   rewrites, 28 `$skill` → file rewrites, 16 `$codex-security:<skill>` rewrites.
4. `<plugin_dir>` / `<plugin-root>` → `$CXSEC_HOME`.
5. All 143 MCP tool references converted to file operations. The mapping lives in
   `references/workbench-file-protocol.md`; discovery/validation/attack-path state
   moved to `candidates.jsonl` plus per-candidate `candidate_ledger.jsonl`
   receipts, normalized by `normalize_candidates.py` for deterministic
   `candidate_id` values.
6. Codex app links (`app://connector_…`) → "a connected MCP server exposing …",
   with an explicit instruction to verify tool presence rather than assume it.
   "Atlassian Rovo" → Atlassian/Jira Cloud MCP tools (the underlying tool names
   like `getVisibleJiraProjects` are unchanged — they are real).
7. "Codex Security" de-branded to "security scan" in prose so mode selection is not
   gated on an absent product. **Schema string literals are unchanged** —
   `codex-security.findings`, `codex-security-snapshot/v1:…`, `codexSecurity/v1`,
   and the `source_type: codex_security_finding` enum, because the validators match
   on them.

## Upstream 0.1.24 architecture change

Upstream replaced the four-phase repository scan with one self-contained audit:
an independent baseline auditor plus focused investigators, whose results the
parent reconciles and validates once (`core-scan.md`). Deep scan now runs
*complete* scans repeatedly rather than repeating discovery only. The four phase
skills survive for the diff scan, which still ranks changed files, records
candidates, and keeps per-candidate ledgers.

That is why a repository scan in this port no longer writes `rank_input.jsonl`,
`work_ledger.jsonl`, or `candidate_ledger.jsonl`. Those files are diff-scan
artifacts now.

## Upstream 0.6.0 changes

Ported from npm-v0.1.32 → npm-v0.6.0:

- **Capability preflight retired** (0.5.0) → `scan-runtime.md`.
- **Coverage is no longer graded.** `coverage.completeness` is gone from the
  schema; coverage lists reviewed surfaces, exclusions, and follow-up work, and the
  manifest records execution status. Finished tasks are closed with
  `coverage.resolvedDeferred`. Repository scans no longer reconcile against a
  file inventory; workers return `reviewed_files`.
- **Threat model is canonical-first.** `scan.threatModel` (structured, or
  `{format: "markdown", content, origin?, scope?}`) is retained in the first
  checkpoint; the finalizer projects `<scan_dir>/threatmodel.md` from it.
- **Core scan** gained a secrets-in-source review, a fixture-policy rule, reviewer
  false-positive feedback, and an optional investigation time allowance
  (`investigation-time-budget.md`, only on explicit user request).
- **Diff scans** inventory every changed text file regardless of extension.
- **Triage and tracking** were rewritten upstream (much shorter); mode 8 now reads
  and records issue associations through `workbench_db.py finding-issues`.
- **Fix** gained static/standard verification modes, and upstream's `verify-fix`
  skill is now ported as mode 5's Verify-Only section.
- **Helpers moved to TypeScript upstream** and carried here as Python:
  `normalize_candidates.py`, `copy_deep_review_input.py`, `bind_repo_scopes.py`,
  and the patch-risk validator, alongside `resolve_security_md.py`.

## Verified working (0.1.x port)

End-to-end on a deliberately vulnerable Express app, with no OpenAI credentials:
`SECURITY.md` policy chain composed root→leaf → worklist →
discovery → `normalize_candidates.py` → three ledger receipts per candidate →
unsealed canonical draft → `finalize_scan_contract.py` → `status: valid`,
valid `report.md`, valid SARIF 2.1.0 with correct rule ids and severities →
`validate_tracking_source.py` reading back the derived finding ids.
