# Installing and verifying the runtime

`scripts/install.sh` vendors the runtime that every scan mode depends on, patches
it, and then proves it works. Read this file when a check fails, when you need to
re-vendor, or when the host has no network.

## Commands

```bash
bash scripts/install.sh                 # install if absent, otherwise verify
bash scripts/install.sh --check         # verify only: no network, no writes
bash scripts/install.sh --force         # re-vendor over an existing tree
bash scripts/install.sh --local         # install from the bundled runtime/ tree, offline
bash scripts/install.sh --home <dir>    # install somewhere other than $CXSEC_HOME
bash scripts/install.sh --ref <sha>     # vendor a ref other than the pinned one
bash scripts/install.sh --no-smoke      # skip the end-to-end finalizer test
```

Exit codes: `0` ok, `1` verification failed, `2` usage error, `3` missing
prerequisite, `4` fetch or extract failed, `5` patch failed.

The default run never re-downloads over a working install — it verifies instead.
Only `--force` overwrites, and it moves each replaced directory to
`<name>.bak.<UTC timestamp>` first. Delete stale `*.bak.*` directories by hand
once a new install verifies.

## What lands where

Into `$CXSEC_HOME` (default `~/.claude/codex-security`), from
`openai/codex-security` at the pinned commit, path `plugins/codex-security`
(upstream moved it there from `sdk/typescript/_bundled_plugin` in 0.1.16+):

| Directory | Contents |
|---|---|
| `scripts/` | Upstream's stdlib-only Python helpers (the `workbench/` subpackage included) plus two `.mjs` modules the port never runs, then the carried local additions `resolve_security_md.py`, `normalize_candidates.py`, `copy_deep_review_input.py`, and `bind_repo_scopes.py` (see `patches/README.md`). No pip install, no network calls. |
| `schemas/` | 12 JSON Schemas for the scan contract and patch-risk assessment. |
| `references/` | Upstream shared reference set. |
| `examples/` | `completed-scan/` — the fixture the smoke test finalizes. |
| `shared/` | Workbench migration tables. `workbench_schema.py` reads them as `<script_parent>/../shared`, so `shared/` must sit beside `scripts/`. |
| `skills/assess-patch-risk/scripts/` | Mode 10's assessment validator — a carried local addition (upstream moved it into its launcher in 0.3.0), kept at its upstream depth. |

Plus `LICENSE` (Apache-2.0), a generated `PROVENANCE.md`, and `patches/` holding
the applied patch.

**Two paths are load-bearing.** `finalize_scan_contract.py` resolves schemas as
`<script_parent>/../schemas`, so `scripts/` and `schemas/` must stay siblings;
moving either one breaks report generation with a schema-resolution error, not an
obvious layout error. `validate_patch_risk_assessment.py` resolves the plugin root
as `parents[3]`, so it must stay at
`$CXSEC_HOME/skills/assess-patch-risk/scripts/` — flattening it into `scripts/`
breaks its schema lookup.

## What the checks mean

| Check | Proves |
|---|---|
| `python 3.x` | An interpreter 3.11+ exists. The runtime is stdlib-only, but `deep_scan_config.py` and `workbench/runtime_toml.py` import `tomllib`, which is stdlib only from 3.11; 3.9/3.10 work only with `tomli` installed. |
| `scripts/ and schemas/ are siblings` | The load-bearing layout constraint holds. |
| `<script>.py runs` | Each load-bearing helper parses and starts under this interpreter. |
| `SARIF rebrand patch applied` | 0 upstream branding sites, 4 `cxsec` sites in the finalizer (upstream dropped the fifth, the coverage-completeness run property, in 0.3.0). |
| `patch-risk validator present at its load-bearing depth` | Mode 10 can find its schema. |
| `end-to-end` | The bundled example scan, stripped back to an unsealed draft, finalizes into a sealed manifest, `report.md`, and SARIF 2.1.0 attributed to `cxsec-security`. |
| `sealed bundle re-validates` | `validate_scan_contract.py` re-reads the whole sealed bundle — manifest, findings, coverage and the generated report — and reports `status: valid`. |
| `patch-risk validator resolves its schema and rejects an invalid assessment` | Mode 10's validator loads the finalizer by path and its schema by depth, and actually enforces the contract. |

The end-to-end check is the one that matters: it exercises schema resolution,
identity derivation, sealing, report generation, and SARIF export in one pass.

## When a check fails

- **`\$CXSEC_HOME does not exist`** — nothing is installed. Run `install.sh`.
- **`missing scripts/ or schemas/`**, **`not siblings`** — a partial or moved
  tree. Re-vendor with `--force`.
- **`<script>.py present but --help failed`** — truncated file or a broken
  interpreter. Check `python3 -V`, then `--force`.
- **`SARIF rebrand not applied`** — the finalizer is upstream-branded or was
  edited. `--force` re-vendors and re-applies. Generated SARIF is otherwise
  attributed to Codex Security, which re-baselines GitHub code-scanning alerts.
- **`finalizer failed on the example scan`** — the log is printed inline. A schema
  error here almost always means the sibling layout broke.
- **`patch failed against <ref>`** — upstream changed the SARIF emitter. Vendor
  the pinned ref instead, or regenerate `assets/0001-rebrand-sarif-output.patch`
  against the new source and re-run. Do not skip it silently.
- **`examples/completed-scan absent`** — an install predating the fixture. The
  end-to-end check is skipped, not failed; `--force` vendors it.
- **`skills/assess-patch-risk/scripts/ absent`** — an install predating mode 10.
  Warned, not failed; `--force` restores it from `assets/local-additions/`.
- **`shared/ absent`** — an install from before the port vendored it. Warned,
  not failed, but any workbench helper that imports `workbench_schema.py` fails
  until `--force` vendors it.
- **`<SRC_SUBDIR> not present at this ref`** — upstream moved the plugin again.
  The installer searches the tarball for `scripts/finalize_scan_contract.py` and
  reports the path it found; update `SRC_SUBDIR` in `install.sh` to match.

## No network

The runtime makes no network calls; only fetching needs one.

When the skill was installed from its git repository it ships a `runtime/` tree:
`install.sh --local` installs from that copy and never touches the network. A
default run also falls back to it automatically if the download fails. The bundled
tree is already patched, so the installer reports `already applied (source tree was
pre-patched)` rather than patching twice.

Otherwise, on an air-gapped host, copy a verified `$CXSEC_HOME` across whole, then
run `install.sh --check --home <dir>` to confirm it survived the trip. The check
path never touches the network.

`gh` is used for the fetch when available (authenticated, no rate limit), with a
`curl` fallback to `codeload.github.com`. Either is enough.

## Transports the installer cannot provide

Modes 1–3, 5–7, 9 and 10 need nothing beyond the runtime. Modes 4 and 8
additionally need a transport:

- **GitHub** — `gh` authenticated (`gh auth login`), or a connected MCP server
  exposing GitHub tools.
- **Linear**, **Jira** — a connected MCP server. There is no CLI fallback and no
  REST substitute; `mode-track.md` says to stop rather than switch transports.

The installer reports which of these are present. It cannot install MCP servers —
configure them in your MCP settings and re-check tool presence in-session.

## Deliberately not vendored

The upstream MCP server (`mcp-app/`), the native helper sources (`native/`), the
Codex-CLI-driven `deep-security-scan` coordinator, the upstream test suite, and
`plugin-files.json`. Upstream retired `preflight/` and `config_preflight.py` in
0.5.0; there is nothing to vendor for them.
`workbench-file-protocol.md` is the file-based replacement for every upstream MCP
tool.
