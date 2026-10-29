# Final Report and Review Directives

Use this guidance when authoring canonical report semantics and returning the generated report and review directives.

## Final Outputs

The final readable output is a deterministic projection of `scan-manifest.json`, `findings.json`, and `coverage.json`:

- primary readable markdown report at the final scan report path from `scan-artifacts.md`
- optional detailed vulnerability write-ups under `findings/<slug>/`, linked from the primary report through `finding.writeup.reportPath`
- optional structural hardening guidance under `hardening/`, linked from the primary report through `scan.hardening.portfolioPath`

When writing `findings.json` alongside this readable output, populate the optional structured details in `finding-detail-fields.md` from the same validated evidence. Do not parse the rendered report back into finding data.

Use `report.md` as the primary readable entry point. Explain report-relevant artifact paths in the report itself, especially in `Scope`, `Reviewed Surfaces`, and `Open Questions And Follow Up`.

In the final response, link the generated markdown report path as the primary readable artifact.

Every scan mode uses the same final report pipeline. Author the canonical semantics as an **unsealed draft** in `<scan_dir>/scan-manifest.json`, `findings.json`, and `coverage.json`. Omit `scan.sealedAt`, `scan.artifacts`, and each finding's `findingId`, `occurrenceId`, and `fingerprints`; the finalizer supplies the seal, artifact digests, exact timestamps, and derived finding identities. A deep scan's rounds each return a complete semantic result, and the coordinator aggregates them into that same single draft. No mode authors, repairs, or treats an existing `report.md` as input. Finalization validates and enriches the canonical JSON, seals the canonical JSON and evidence artifacts, then deterministically generates `report.md`. Supply report prose through structured canonical semantics rather than a separately authored report.

For each finding, supply an evidence-supported lowercase vulnerability-family `ruleId`; `taxonomy: { category, cwe }` using its exact known CWEs; verified locations; and `provenance.source`, using `"local_plugin"` only when this plugin actually discovered the finding. Preserve genuine worker or source provenance and any existing canonical candidate identity in the finding extensions. A finding with no known CWE retains `cwe: []`; never invent a classification. Include optional `codeEvidence` only when its actual code is nonempty and every referenced evidence ID is present.

Supply semantic coverage as `{ surfaces, explicitExclusions, deferred }`; there is no `completeness` field. Omit it; the finalizer silently drops a legacy value, so it carries no meaning. Give each surface the actual `label` and one existing `disposition`. Retain deferred items and `needs_follow_up` surfaces with their reasons and evidence. Exclusions and unresolved questions do not prevent the configured review from finishing. Each deferred item needs a meaningful reason and a stable `id` (or `candidateId`) derived from its candidate identity or deferred-work details; use each deferred `id` only once per draft and keep it stable across checkpoints. Open questions may be nonempty strings or `{ question, followUpPrompt? }` objects. The finalizer derives finding identities and fingerprints; author everything else — target and scope metadata, scope include and exclude paths, coverage mode and inventory strategy, and surface IDs — in the draft yourself, and leave the sealed artifact list out of it.

To close a saved task, move it out of `coverage.deferred` and into `coverage.resolvedDeferred: [{ id, reason }]` in the final draft, using its saved ID and a completion reason; the finalizer rejects a closure whose ID is still in `deferred`, and duplicate closure IDs. Update linked surfaces by saved ID and keep unfinished work in `deferred`. Candidate-linked tasks are closed by a finding or a terminal disposition, not by a closure entry. A deep scan's coordinator cannot close a round's tasks. Earlier checkpoints remain unchanged.

While the scan runs, checkpoint saved findings and pending candidates to `<scan_dir>/checkpoint-findings.json`; a checkpoint is not a completed audit. Once the complete unsealed draft is written, finalize with `python3 $CXSEC_HOME/scripts/finalize_scan_contract.py --scan-dir <scan_dir> --source-root <repo_root>`, then read the sealed `scan-manifest.json` and the generated `report.md`. Do not treat the coverage objective as met until finalization succeeds and the generated markdown report exists.

On a confirmed failure or explicit cancellation, `<scan_dir>` retains what was actually saved and the scan is not successful. Report validated findings separately from pending candidates and describe scope limitations and remaining work. Use the retained artifacts; do not start additional model work after cancellation or replace saved results with a no-findings response.

Token usage is not measured here. Say that it could not be measured rather than reporting zero or estimating a cost. Report only metadata you actually have. Usage metadata is never a reason to modify sealed scan artifacts or the deterministic report.

Before finalizing, require the complete canonical draft to exist on disk and verify the canonical JSON. For a deep scan, require the coordinator's terminal round manifest first; do not author a second draft or rerun a round's internal phases. Finalization is validation and sealing only: it validates and seals already-authored canonical artifacts and generates `report.md`; it does not create missing artifacts or run skipped scan phases.

When the finalizer rejects named semantic paths, no seal happened. Correct only the named paths in the same draft, preserving all valid findings, fields, evidence, and deferred work; retry at most twice. Stop after the first successful finalization; a checkpoint does not end the audit. Do not blindly retry an ambiguous write failure.

For any other required scan phase, canonical-artifact write, or on-disk existence check that fails before completion, stop the current response and surface the exact workflow blocker. Do not call completion with missing artifacts, return a final report or no-findings result, or satisfy a structured output schema. Leave the durable scan available for a later continuation instead of canceling or failing it solely because canonical assembly is blocked.

If the finalizer fails, stop the current response and surface its exact error. Do not retry finalization blindly in the same response, return a final report or no-findings result, or satisfy a structured output schema. Leave `<scan_dir>` intact for a later continuation instead of recording the scan as failed solely because finalization failed.

Canonical report semantics live in these fields:

- `scan-manifest.json`: `scan.scope` and `scan.threatModel`
- `findings.json`: each finding's `summary`, `codeEvidence`, `rootCause`, `validation`, `attackPath.dataflow`, `attackPath.reachability`, `severity.rationale`, `severity.changeConditions`, `remediation`, `remediationTests`, and `preventiveControls`
- `findings.json`: optional `writeup.reportPath` for a derived, unsealed detailed vulnerability report written under `findings/<slug>/<slug>.md`
- `scan-manifest.json`: optional `scan.hardening.portfolioPath` for the derived, unsealed design portfolio at `hardening/hardening.md`
- `coverage.json`: `surfaces` including `riskArea` and `notes`, plus `openQuestions`

For a whole-repository deep scan, set `coverage.inventoryStrategy: "repository"`; repeated rounds are workflow metadata, not a different inventory strategy.

Older v1 producers may omit the new optional fields. Finalization uses explicit JSON-derived fallback text in that case; it never reads a pre-existing report to fill gaps.

When `scan.hardening.portfolioPath` is present, include a short `## Structural Hardening` section linking the portfolio. Keep individual proposal links and the full option analysis in `hardening/hardening.md` rather than copying them into the primary scan report. The portfolio is design guidance, not evidence that any finding has been remediated.

When there are no reportable findings, include a short `No findings` section that explains why nothing survived discovery or the later reportability gates. For repository-wide and scoped-path scans with a coverage ledger, still include `Reviewed Surfaces` so checked, rejected, not-applicable, and follow-up-needed surfaces remain auditable.

When there are reportable findings, render them as readable markdown findings rather than raw JSON or a dumped schema object.
Order findings from highest severity to lowest severity: `critical`, then `high`, then `medium`, then `low`.

Group observations only when they share the same broken security control and effective remediation. Preserve every affected route, operation, sink, and supporting source location; keep distinct security failures separate even when they share a CWE.

Set the finding category and CWE from the primary broken control. Do not add secondary support-impact CWEs, such as data exposure or missing authentication, to an injection/RCE/path/file/parser finding merely because they make exploitation worse; mention those impacts in prose or emit a separate finding if that secondary control is independently vulnerable.

A repository scan writes its source-backed final findings and coverage straight into the unsealed canonical files. A deep scan semantically aggregates complete round results, preserving validated findings, attack-path analysis, affected locations, threat-model context, and honest coverage; the coordinator writes that one canonical draft and does not read candidate ledgers, run separate validation or attack-path phases, or author a second draft. Canonical `severity.changeConditions` must be one non-empty string; when `attack_path.change_conditions` contains multiple strings, join them into one prose string before writing `findings.json`.

For diff candidates, read the candidates from `<discovery_dir>/candidates.jsonl` and their receipts from each `<findings_dir>/<candidate_id>/candidate_ledger.jsonl`. Map each `validation` receipt into the finding's validation fields, map its confidence and rationale into top-level `confidence.level` and `confidence.rationale`, and map each `attack_path` receipt into dataflow, reachability, severity, and change conditions. Apply row outcomes in this order: validation disposition `reportable` plus attack-path decision `reportable` becomes a finding with its distinct instance and all relevant entrypoint, root-control, sink, and supporting locations; otherwise, a `deferred` result from either phase becomes `needs_follow_up` coverage and a `coverage.deferred` entry using the recorded uncertainty or proof gap; otherwise, validation disposition `not_applicable` becomes `not_applicable` coverage; otherwise, validation disposition `suppressed` or attack-path decision `ignore` becomes `rejected` coverage. A missing required phase record leaves the candidate unresolved; retain it with its available evidence and the missing step. Do not require phase receipts, per-candidate narratives, or another reconciliation pass.

Diff scans provide per-candidate ledgers, validation closure tables, and coverage ledgers. When those artifacts exist, retain their traceability: start from reportable/surviving rows, preserve exact affected locations, and map suppressed, not-applicable, or deferred rows to public-facing coverage outcomes. Do not silently drop a seeded row because a same-family neighbor survived.

## Report Structure

Use this report structure:

`# Security Review: <repo_or_target_name>`

`## Scope`

Populate `scan.scope` with in-scope context, artifacts reviewed, runtime or test status, validation mode, and explicit limitations. Include/exclude paths and coverage fields supply the remaining projected scope content. If the threat model was generated during Phase 1 rather than provided by the user, say that in canonical scope context. Do not call generated threat-model material an external input.

After the scope bullets, include a compact `### Scan Summary` table when the scan has findings or repository-wide coverage. Use columns `Field` and `Value`. Include the count of reportable findings, severity mix, confidence mix, validation mode, and concrete limitations when those values are known. Keep artifact paths below this table.

`## Threat Model`

Use the completed canonical `threatModel` when one exists. For a workflow that produced a Markdown model, retain the text exactly as `{ "format": "markdown", "content": "<completed model text>" }` in the canonical draft. A historical `<context_dir>/threat_model.md` may supply missing canonical content; it must not override a later canonical model. Use the field mapping and scenario reconciliation in `threat-model.md` when building a generated structured model; do not regenerate it from the final finding list. Preserve source citations, capability boundaries, deployment assumptions, and material unknowns. Finalization reads the canonical model when projecting this section and the root-level `threatmodel.md`. Exporting that saved model does not perform another analysis.

`## Findings`

Start this section with the findings summary table.

For Deep Security Scan outputs, group the summary table by `extensions.candidateId` and use columns `Findings`, `Reports`, `Severity`, `Confidence`, and `Detailed write-up`. `deep_repository` scans always use this presentation. Scoped-path deep outputs use it when the reportable child findings carry the deep-scan identifiers described below; ordinary scoped standard scans keep the standard table. Emit one table row per reportable canonical DSS finding. `Findings` contains the vulnerability title without duplicated trailing report or provenance annotations. `Reports` lists every child finding's unique `extensions.reportId`, with each id linking to that child's detailed summary section. For compatibility with older deep-scan artifacts, fall back to `extensions.ledgerRowId`; when a ledger row produced multiple reports, use each child's unique trailing title annotation or instance identity rather than displaying duplicate labels. `Detailed write-up` lists the corresponding `writeup.reportPath` link for every child report. Preserve every child report as its own detailed section below the grouped table. When child reports have different severity or confidence levels, list every distinct level in the grouped row. In the scan summary, distinguish the number of reportable DSS findings from the number of child report instances. For every other scan mode, preserve the standard columns `Finding`, `Severity`, `Confidence`, and `Detailed write-up`, with the finding title linking to the detailed summary section.

After the summary table, include a compact `### Confidence Scale` table with columns `Label` and `Meaning`:

- `high`: direct source, configuration, or runtime evidence supports the finding, with no material unresolved reachability or exploitability blocker.
- `medium`: source evidence supports a plausible issue, but runtime behavior, deployment configuration, role reachability, type constraints, or exploit reliability still need proof.
- `low`: weak or incomplete evidence; include only when the user explicitly wants follow-up candidates in the final report.

When a finding records `writeup.reportPath`, link the detailed report from the findings summary and do not duplicate the complete finding inline. The linked write-up is a derived, unsealed readable output; the canonical finding remains the source of truth for adapters and regeneration. Findings without a write-up continue to use the inline format below for compatibility.

Render each inline finding as:

`### [<number>] <title>`

For each finding include a compact two-column metadata table immediately below the heading. Use columns `Field` and `Value`. Include these rows:

- `Severity`: `critical|high|medium|low`
- `Confidence`: `high|medium|low` or a short calibrated confidence label
- `Confidence rationale`: one sentence explaining why the confidence label is calibrated that way, grounded in the validation method, direct evidence, and missing proof if any
- `Category`: concrete vulnerability class
- `CWE`: id and name list, or `none`
- `Affected lines`: path:line-range list

Use a concrete category such as `Authorization bypass / IDOR`, `Path traversal`, `SQL injection`, `XXE`, `Open redirect`, or `Hardcoded credentials`. Do not use generic placeholders such as `security scan finding`.

For standard scan modes, the summary table should link each finding title to its detailed finding section with an intra-document markdown anchor. For the Deep Security Scan grouped presentation, the report id in the `Reports` column owns that link instead. Keep the displayed vulnerability title and report id aligned with the corresponding detailed heading. Use the explicit finding anchor emitted by the deterministic projection.
The summary table and the detailed finding sections must use the same descending severity order: all `critical` findings first, then `high`, then `medium`, then `low`. Renumber findings after sorting so the table order, detailed headings, and anchors match.

Affected lines must include the root broken control or dangerous sink line when that line is identifiable, not only the public wrapper, route, or caller that makes it reachable. For wrapper-to-shared-helper findings, list both the reachable wrapper/entrypoint and the underlying parser, deserializer, path/archive helper, expression evaluator, or auth/authz control line. If a seeded file, class, package, or hunk shares the surviving proof tuple, keep that seed anchor in affected lines instead of replacing it with a broader sibling-only location. If the bug is caused by unsafe transformation or selection before the sink, include the split, parse, canonicalization, normalization, comparison, regex, object-selection, or object-binding line where the control fails. For parser, XML, deserialization, and object-construction findings, include the concrete codec, converter, deserializer, parser feature setup, resolver, class filter, or container handler line when that line performs recursive parsing, type resolution, object conversion, class filtering, or fail-open hardening. For central file-format object models, include low-level helper lines such as `to*Array`, `toList`, `getObject`, numeric conversion, iterator, size-based allocation, unchecked cast, or collection-to-array loops when those helpers are the broken malformed-input control. For recursive placeholder/template findings, include the helper/parser setup line that enables recursive expansion or expression evaluation, not only the later resolver or render call. For resource-serving findings, include the allowlist, path-matcher, URL decoding, canonicalization, or resource-selection line that decides whether the attacker-selected resource is allowed. For stateful authentication protocol findings, include the principal/credential/token/issuer installation, rebind/reauthentication, or validated-vs-consumed object-selection line that creates the auth bypass. For SSO/SAML/federation findings, include the response/assertion selection, signed-object lookup, cloned/returned assertion, subject, audience, recipient, destination, ACS URL, or issuer-binding line that determines which identity object is trusted. For polymorphic or request-selected handler, operation, converter, filter, validator, or strategy families, include the concrete subclass/implementation line that transforms, validates, canonicalizes, selects, or reinterprets attacker input before a shared sink/control, including specialized helper methods and branch predicates inside the concrete class when they perform or enable the unsafe transform. If a special-case branch such as append, wildcard, fallback, copy/move `from`, default-value, or type-resolution handling bypasses or narrows validation, include that branch-local root-control line even when a shared helper is also affected. If the finding text says a shared flaw affects "all", "every", or "any" concrete operation, codec, converter, handler, validator, filter, or resolver, the affected lines must include the concrete implementations identified during discovery or validation; do not rely on "and related classes" prose for independently reachable root-control lines. If equivalent resolver/filter controls are duplicated across core, server, client, remoting, plugin, or import packages, include the runtime/exported implementation that enforces the broken control. For repeated vulnerable templates, routes, query builders, parser operations, or auth/object-access endpoints, keep each independently vulnerable file and line as its own affected instance; do not hide sibling instances as extra context on one representative finding when they can be attacked independently. The review directive should point at the tightest root-cause line unless the wrapper or concrete implementation line is the actual broken control.

Then render these subsections under each finding:

- `#### Summary`
  - Start with how to reproduce the issue and what happens in the product, then explain why the code causes that behavior.
  - Wrap code identifiers, RPC names, functions, types, fields, parameters, configuration keys, and literal values in single backticks.
- `#### Root Cause`
  - State the violated security invariant and explain exactly how the implementation breaks it.
  - Walk the vulnerable call stack from the code that accepts or decodes user-controlled input through each meaningful call or transformation to the missing control, dangerous operation, and security-relevant consumer. Do not begin at the sink when an earlier input boundary is known.
  - Give every displayed `codeEvidence` item a role and an explanation that names the carried value and the next callee or state transition. Order `rootCause.evidenceRefs` from input to outcome, with any `expected_control` comparison after the vulnerable stack.
  - Show the smallest complete snippets needed for that walkthrough. Omit incidental helpers, but do not skip a call boundary whose behavior is necessary to understand how attacker input reaches the broken invariant.
  - Do not emit generic prose that only repeats an affected `path:line` already present in the metadata table.
- `#### Validation`
  - Include method, checklist items, evidence, and remaining uncertainty.
  - Pair each important validation claim with actual source in `validation.evidenceRefs`; a list of file names and line numbers is not sufficient evidence for the readable finding.
- `#### Dataflow`
  - Show the technical source-to-sink path inside the code, such as request parameter -> controller -> service/helper -> dangerous sink -> response or side effect.
- `#### Reachability`
  - Explain who can realistically trigger the dataflow, from what boundary, under what preconditions, and what attacker outcome follows. Fold any attack-path facts into this prose or compact bullets instead of emitting a separate `Attack Path Facts` section.
  - Use `attackPath.evidenceRefs` for the few code transitions that establish attacker input, the missing control, and the resulting sink or state change. Keep this shorter than Validation.
- `#### Severity`
  - State the final severity and then explain the rationale.
  - Treat likelihood and impact as inputs to the final severity, not as separate report labels.
  - The rationale should fold in reachability: attacker role, exposed entry point, exploit steps, required feature flags/config, runtime/deployment assumptions, and any counterevidence or blockers.
  - The rationale should explain the concrete security consequence using repository evidence: data exposed, integrity boundary broken, credential/control-plane effect, code execution path, or why impact is narrower.
  - Include one concise sentence explaining what specific additional evidence would raise or lower the severity.
  - Avoid circular phrasing such as `this is high because it is high severity`, `maps to high`, or `high-severity issue`.
- `#### Remediation`
  - Give concrete minimal fixes, tests, and preventive controls.

For repository-wide and scoped-path scans with a coverage ledger, include a concise `## Reviewed Surfaces` section after the findings. This section summarizes what was inspected, what came out of each reviewed surface, and seeded/root-control rows that were suppressed, not applicable, or deferred so an auditor can see why they did not become findings. Use a table with `Surface`, `Risk Area`, `Outcome`, and `Notes`.

Recommended outcomes:

- `Reported`: became a final finding.
- `No issue found`: reviewed and no credible issue survived.
- `Rejected`: plausible-looking candidate was ruled out with specific counterevidence.
- `Not applicable`: the risk class does not apply to that surface.
- `Needs follow-up`: plausible but not fully closed because of a concrete blocker or proof gap.

For broad scans where the completed coverage is useful for triage but too large for high-precision review, include a concise `## Open Questions And Follow Up` section near the end of the report. Use concrete, copyable prompt ideas that narrow the next review to individual commits from the current scan. Do not include this section for precise scans where the requested scope was already sufficient.

Follow-up prompts should be tailored to the actual scan results:

- use exact commit SHAs, PR numbers, short titles, file paths, or component names from the report
- focus each prompt on the specific boundary that made the commit worth follow-up, such as auth, plugin/MCP exposure, artifact downloads, signed URLs, or gateway routing
- avoid generic placeholders

Each finding should make it easy for an application security engineer or software engineer to answer:

- what changed or what path is vulnerable
- what attacker-controlled input or trust boundary matters
- what direct evidence supports the claim
- what counterevidence or uncertainty remains
- why the severity landed where it did
- what the smallest safe fix is

Include the final markdown report path in the response so the user can find the readable report easily.

After a completed scan:

- If the scan found reportable issues, ask whether the user wants to export the findings as JSON, SARIF, or CSV, generate patches, or track selected findings. Name the highest-priority finding.
- Offer tracking only when ``mode-track.md`` can use an available destination, such as Linear, Jira, or GitHub Issues. Name the destination in the question.
- If the report names a specific follow-up, ask whether the user wants to investigate it.
- If the scan found nothing and has no specific follow-up, do not add a generic question.
- Wait for the user's answer before exporting findings, generating patches, tracking findings, or starting another scan.

## Review Directives (optional structured output)

When the user asks for structured per-finding output, emit one `::code-comment{...}` directive per surviving finding in the final response. The markdown report and review directives should agree on title, file, line range, and core explanation.

Map the final report severity to directive priority only when emitting the directive:

- `critical` -> `P0`, `priority=0`
- `high` -> `P1`, `priority=1`
- `medium` -> `P2`, `priority=2`
- `low` -> `P3`, `priority=3`

For each reportable finding, emit a review directive in this form:

`::code-comment{title="[P1] Example title" body="One-paragraph review explanation." file="/absolute/path/to/file" start=10 end=12 priority=1 confidence=0.55}`

Directive requirements:

- `title`, `body`, and `file` are required
- `title` should include the mapped directive priority, formatted like `[P1] Example title`
- `file` should be an absolute path
- `start` and `end` should be tight 1-based line numbers
- `priority` should match the mapped directive priority
- `confidence` should be numeric when available
- emit one directive per finding and none when there are no findings
- inline Markdown code spans are allowed and encouraged for short identifiers, flags, function names, and config keys, such as `git -c`, `--config`, and `diff.external`
- do not put double quote characters inside quoted attribute values, including escaped quotes like `\"`; rewrite quoted command examples without quotes or leave them only in the markdown report
