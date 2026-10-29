# Local patches to the vendored runtime

These are deliberate local changes to upstream code. **Re-vendoring from
openai/codex-security overwrites them** — re-apply every patch here afterwards
and re-run the verification below.

## 0001-rebrand-sarif-output.patch

Renames the four SARIF branding sites in `scripts/finalize_scan_contract.py` so
generated SARIF is attributed to this port rather than to Codex Security. These
values are cosmetic: nothing in the codebase reads them back, and neither
`validate_scan_contract.py` nor the report projection checks them.

| SARIF field | Upstream | Here |
|---|---|---|
| `tool.driver.name` | `Codex Security` | `cxsec-security` |
| `partialFingerprints` key | `codexSecurity/v1` | `cxsec/v1` |
| `run.properties.*SchemaVersion` | `codexSecuritySchemaVersion` | `cxsecSchemaVersion` |
| `run.properties.*TargetKind` | `codexSecurityTargetKind` | `cxsecTargetKind` |

`tool.driver.name` and the `partialFingerprints` namespace are what GitHub code
scanning uses to correlate alerts across uploads. Changing them re-baselines
alerts once. This rename was applied before any SARIF upload, so nothing was
orphaned — do not change them again casually.

### Apply

```bash
cd ~/.claude/codex-security && patch -p1 < patches/0001-rebrand-sarif-output.patch
```

### Verify

```bash
cd ~/.claude/codex-security
grep -c 'codexSecurity\|"Codex Security"' scripts/finalize_scan_contract.py   # must be 0
grep -c 'cxsec' scripts/finalize_scan_contract.py                            # must be 4
```

## Local additions

Files this port carries under `assets/local-additions/` are mirrored by path and
copied into `$CXSEC_HOME` after vendoring, so a `--force` re-vendor cannot lose
them. This covers helpers upstream has deleted and deliberate overrides of
upstream runtime defaults.

### Local runtime defaults

`assets/local-additions/scripts/deep_scan_defaults.json` deliberately overrides
the upstream file at the same path. It keeps deep scans cost-safe by default:

| Setting | Upstream | Here |
|---|---:|---:|
| `workers` | 4 | 2 |
| `subagents` | 3 | 1 |
| `stopAfterNoNew` | 4 | 3 |
| `stopAfterConsecutiveErrors` | 3 | 2 |
| `maxDiscoveryRuns` | 40 | 10 |
| `maxTimeHours` | 96 | 1.5 |

The installer restores this asset after re-vendoring. Keep it byte-identical to
`runtime/scripts/deep_scan_defaults.json`; otherwise a fresh install silently
reverts to the upstream budget.

### Carried helper files

Upstream keeps moving its stdlib Python helpers into the TypeScript launcher
(`launch_codex_security_mcp --helper <name>`) inside its MCP app. This port does
**not** vendor the MCP app — it is the offline, stdlib-only half — so each helper a
mode still invokes is carried as Python. Every carried helper tracks the *current*
TypeScript implementation, not the version it was deleted at: behaviour changes
made upstream after the move are ported back, and each one is held to upstream's
current TypeScript test suite, ported to pytest under the skill repo's `tests/`.

| Carried file | Upstream move | Invoked by |
|---|---|---|
| `scripts/resolve_security_md.py` | 0.1.28 (`--helper resolve-security-md`) | modes 1–3 policy chain, mode 7, `security-guidance.md` |
| `scripts/normalize_candidates.py` | 0.2.0 (`--helper normalize-candidates`) | mode 2 discovery (`workbench-file-protocol.md`) |
| `scripts/copy_deep_review_input.py` | 0.3.0 (`--helper copy-deep-review-input`) | mode 2 worklist (`phase-2-discovery.md`, `scan-artifacts-and-ledger.md`) |
| `scripts/bind_repo_scopes.py` | 0.3.0 (`--helper bind-repo-scopes`) | mode 1 scoped-path binding |
| `skills/assess-patch-risk/scripts/validate_patch_risk_assessment.py` | 0.3.0 (`--helper validate-patch-risk-assessment`) | mode 10 |

The two worklist helpers were subcommands of `generate_rank_input.py`; they are
separate scripts here with the same flags. The patch-risk validator stays at its
upstream depth because it resolves the plugin root as `parents[3]`, and it uses the
vendored finalizer's private `_validate_schema_node` to match the TypeScript
schema checks exactly (a trailing `$` in a pattern must not match before a final
newline); a re-vendor that renames that function makes the installer's
patch-risk check fail loudly rather than silently.

Where Python and JavaScript semantics differ, the carried code follows
JavaScript: UTF-16 code-unit sort order, `String.prototype.trim` whitespace,
ASCII-only `\d`, lone surrogates escaped in JSON output, raw POSIX path bytes via
`surrogateescape`, control characters escaped in filesystem diagnostics, and
strict option parsing (no abbreviated long options).

Verify:

```bash
cd ~/.claude/skills/cxsec-security && python3 -m pytest -q tests/
```

## Deliberately NOT patched

The canonical `documentType` literals (`codex-security.scan-manifest`,
`codex-security.findings`, `codex-security.coverage`), the fingerprint algorithm
`codex-security/v1`, and the snapshot-digest prefix `codex-security-snapshot/v1`
are **left alone on purpose**:

- No skill author ever types them. The finalizer stamps `documentType` and the
  finding identities onto an unsealed draft itself — verified by stripping them and
  finalizing successfully.
- `references/mode-triage.md` documents ingesting an existing
  `codex-security.findings` JSON artifact as a compatibility input. Renaming the
  literal would break that.
- Changing them means editing 3 JSON schemas plus ~8 sites in the finalizer, for
  zero user-visible difference.

`scan.producer` is author-supplied, not validated against any brand, and this port
already emits `{"name": "cxsec-security", ...}`. Nothing to patch.
