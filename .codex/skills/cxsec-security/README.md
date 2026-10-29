# cxsec-security

A [Codex and Claude Code Agent Skill](https://developers.openai.com/codex/skills)
for application security review: scan a repo or a diff for vulnerabilities,
triage findings you already have, fix and verify them, write them up, and file
them — with CWE/CVSS-grounded reporting and deterministic SARIF 2.1.0 output.

Ported from [openai/codex-security](https://github.com/openai/codex-security)
(Apache-2.0). See [NOTICE](NOTICE) for what changed.

## Install

Clone into the host's personal skills directory. The directory name must match
the skill name, so clone it as `cxsec-security`.

Codex:

```bash
mkdir -p ~/.agents/skills
git clone https://github.com/coffeegrind123/cxsec-security.git \
  ~/.agents/skills/cxsec-security

bash ~/.agents/skills/cxsec-security/scripts/install.sh
```

Claude Code:

```bash
git clone https://github.com/coffeegrind123/cxsec-security.git \
  ~/.claude/skills/cxsec-security

bash ~/.claude/skills/cxsec-security/scripts/install.sh
```

In Codex, invoke this skill explicitly as `$cxsec-security`. Its
`agents/openai.yaml` disables implicit invocation because the built-in
`codex-security` deep-scan skill has overlapping trigger words. The built-in deep
scan requires a managed filesystem permission profile; this skill deliberately
does not.

The installer vendors the pinned upstream runtime into `$CXSEC_HOME` (default
`~/.claude/codex-security`), applies the SARIF-attribution patch, and then proves
the install by finalizing a bundled example scan end to end.

```bash
scripts/install.sh              # install if absent, otherwise verify
scripts/install.sh --check      # verify only: no network, no writes
scripts/install.sh --local      # install from the bundled runtime/ tree, offline
scripts/install.sh --force      # re-vendor from upstream
```

`runtime/` in this repo is a working copy of that tree, so `--local` gives you an
offline install. Read [`references/install.md`](references/install.md) when a
check fails or you need to re-vendor or pin a different upstream ref.

**Requirements:** `python3` 3.11+ (3.9/3.10 work only with `tomli` installed —
the deep-scan config reads TOML). Nothing else for scanning — the runtime is
stdlib-only and makes no network calls. Filing findings (mode 8) and GitHub
finding intake (mode 4) additionally need authenticated `gh`, or a connected MCP
server exposing GitHub, Linear, or Atlassian/Jira tools.

## The ten modes

`SKILL.md` is a router. It picks one mode, reads only that mode's reference file,
and follows it.

| # | Mode | Use when |
|---|---|---|
| 1 | Repository scan | Audit a whole repo or a scoped path. The default scan. |
| 2 | Diff scan | Review a change set: PR, commit, branch diff, working-tree patch. |
| 3 | Deep scan | Explicitly asked for deep, exhaustive, or multi-pass. |
| 4 | Triage | You already have findings — SARIF, CVE/GHSA, scanner tickets — and want repo-impact verdicts. |
| 5 | Fix | Fix *and verify* one identified finding, or verify an existing fix only. |
| 6 | Write-up | Turn findings and PoCs into a distributable disclosure report. |
| 7 | Policy | Author or review a repository `SECURITY.md`. |
| 8 | Track | File findings as Linear/Jira/GitHub issues or a draft GitHub advisory. |
| 9 | Harden | Structural change beyond per-finding patches, with tradeoffs and a migration plan. |
| 10 | Patch risk | How risky a specific patch, PR diff, or commit range is to *merge*. Read-only. |

Modes 1 and 3 run one self-contained audit (`references/core-scan.md`): an
independent baseline auditor plus focused investigators, validated and reconciled
once. Mode 2 runs the four-phase pipeline — threat model → discovery → validation
→ attack path. Phase files are never entry points.

## How reports are produced

You author the canonical scan JSON as an *unsealed draft*, then finalize once:

```bash
python3 $CXSEC_HOME/scripts/finalize_scan_contract.py \
  --scan-dir <scan_dir> --source-root <repo_root>
```

The finalizer validates and seals it, derives finding identities and
fingerprints, and generates `report.md` plus SARIF 2.1.0. `report.md` and the
SARIF are never hand-written.

`scripts/` and `schemas/` must stay siblings inside `$CXSEC_HOME` — the finalizer
resolves schemas as `<script_parent>/../schemas`.

## Layout

```
SKILL.md                  router: mode selection, standing rules, reference map
references/               40 files, loaded on demand
  mode-*.md               the ten modes
  core-scan.md            the audit engine for modes 1 and 3
  phase-*.md              the four phases of the diff scan
  workbench-file-protocol.md   file-based replacement for every upstream MCP tool
  install.md              installer reference and failure playbook
scripts/install.sh        install / verify / re-vendor the runtime
assets/                   the SARIF-attribution patch and carried local additions
tests/                    pytest ports of upstream's suites for the carried helpers
runtime/                  vendored upstream runtime (stdlib Python + JSON Schemas)
```

## What is not ported

The upstream MCP server (`mcp-app/`) and the Codex-CLI-driven
`deep-security-scan` coordinator. `references/workbench-file-protocol.md` is the
authoritative mapping from every upstream MCP tool to its file-based replacement,
and it is required reading before any scan mode. The Codex-desktop-only
`desktop-scan.md`, the ChatGPT access advisory, and the `publish-findings` and
`cloud-findings` skills (which need the `codex-security` CLI or Codex Security
Cloud) have no equivalent here and are not ported. Helpers upstream moved into its
TypeScript launcher are carried as Python under `assets/local-additions/`, held to
upstream's current test suites by `tests/` (`python3 -m pytest tests/`).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
