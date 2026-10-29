<!-- when-to-use: Use when the user explicitly asks to fix and verify a validated or plausible security finding. Do not use as the primary trigger for full PR, commit, branch, patch, or repository scans. -->

# Fix Finding

## Objective

Turn a current security finding into a minimal, validated code change. If the code is already safe, prove that and report that no change was needed.

Judge the result in this order:

1. the current state is correctly classified as vulnerable, already safe, or unproven
2. any fix completely closes the broken security boundary
3. legitimate behavior and compatibility are preserved
4. the checks required by the selected verification mode pass
5. the implementation follows repository conventions
6. the patch contains only the scope necessary for the earlier properties

Never trade an earlier property for a later one. Minimal means the smallest repository-native change that satisfies all earlier properties, not the fewest lines.

## Verification Mode

Use `standard` verification unless the caller explicitly selects
`verificationMode: static`. The selected mode applies throughout this mode,
including delegated reviews and workbench stages; pass it to every subagent.

In static mode, assess source and diff evidence only. Do not install dependencies
or run builds, tests, reproducers, or the application, including when validation
instructions request them. Repository-reading tools remain available. State that
runtime checks were not performed; do not count them as passed or require them
for this assessment. Missing runtime tooling alone is not a blocker, but a
material gap in the source evidence still requires `blocked`.

## Patch Contract

Before editing, inspect the affected implementation, its direct callers, nearby
helpers, and relevant existing tests. Establish from repository evidence:

- the attacker-controlled input and concrete source-to-sink path or broken control
- the security invariant and narrowest shared enforcement boundary
- legitimate behavior, APIs, error semantics, and compatibility constraints that must remain
- the closest existing implementation, validation, and error-handling precedents

Treat the finding as a data-flow and boundary problem, not merely the named input
example. Check equivalent encodings, parser forms, aliases, callers, sinks, and
every representation or copy of security-sensitive state that could bypass the
proposed change. Handle unsafe state explicitly; do not silently accept, truncate,
or reinterpret it into another reachable form.

## Pre-Patch Investigation

You own the patch and independently trace the reported path. Before editing,
launch one fresh-context read-only `Agent` subagent when subagents are available.
If they are not, perform the same perspective yourself as a separate pass:

- **Security-boundary and compatibility investigator:** Independently trace the
  source-to-sink path and identify the shared enforcement boundary, affected entry
  points, alternate representations or lifecycle states, parser and
  validation-to-use transitions, concrete sibling paths, and source-backed bypass
  risks. Establish the legitimate workflows and public behavior that must remain,
  then inspect callers, implementations, optional modes, errors, side effects,
  repository conventions, existing helpers, and focused validation commands for
  integration constraints.

The investigation requires repository-relative evidence and a clear separation
between facts, inferences, and unresolved questions. When it completes, reconcile
its findings with your own investigation and choose the patch boundary.

## Implementation Workflow

1. Trace the reported path and inspect only the context needed to identify the
   real shared boundary. Return `no_change` when repository evidence shows that
   the reported path is already safe; do not make a speculative change.
2. In standard mode, when feasible, run the smallest high-signal reproduction
   through that boundary and one legitimate control through the same path.
3. Implement the smallest repository-native fix at the shared boundary. Prefer
   nearby helpers and established APIs. Do not broaden into unrelated redesign,
   cleanup, or sibling findings.
4. Before verification, challenge the patch rather than defending it: inspect
   every direct caller of each changed helper and both outcomes of each changed
   condition. Look for one sibling path, representation, or copy that still
   reaches the vulnerable sink, and one ordinary or default input that the patch
   newly rejects or reinterprets; revise the implementation if either exists.
5. In standard mode, verify in order:
   - inspect the final diff and run the narrowest syntax, import, build, or type
     check relevant to it
   - rerun the security trigger or strongest focused substitute and review one
     alternate malicious input class
   - rerun the legitimate control, the nearest existing tests, and the owning
     package's applicable required checks

In static mode, use these verification gates instead: inspect the final diff;
trace the original exploit path and relevant alternate inputs through the changed
control; then inspect callers and existing tests as source to establish that
legitimate behavior and compatibility remain intact. Cite the source evidence for
each gate and identify anything it cannot establish.

Return `blocked` if the vulnerability may be real, but essential evidence,
tooling, access, or a product or compatibility decision is missing, so a safe fix
cannot be responsibly completed or verified.

## Patch Candidate Review

After implementing and completing the checks for the selected mode, launch one
fresh-context read-only `Agent` subagent when subagents are available. Give it
only the finding, the repository root, the authorized scope, the repository
policy, the verification mode, and the current candidate diff — **not** the patch
rationale, the investigator report, or the claim that tests passed. If subagents
are unavailable, perform the same review perspective yourself as a separate pass
before final verification. Either way, use this assignment:

- **Bypass and regression reviewer:** Reconstruct the invariant and look for a
  concrete surviving route through affected entry points, equivalent
  representations, parser boundaries, aliases, backend or platform variants, and
  validation-to-use gaps. Trace changed conditions and direct callers for concrete
  breakage of legitimate inputs, public contracts, errors, side effects, state
  transitions, optional modes, compatibility, resource behavior, and repository
  conventions.

The reviewer must not edit or delegate. It reports only concrete, source-backed
bypasses or regressions and explains how each can be verified. Treat reviewer
findings as hypotheses: confirm them against the source or, in standard mode,
focused execution before revising the implementation. Address only confirmed
issues within the finding and compatibility boundary; do not broaden into
speculative concerns or redesign. Then repeat the selected mode's verification and
ensure no temporary or unrelated changes remain. Perform only one review cycle.

## Workbench Remediation Stages

When the user requests a specific remediation stage, follow only that stage. The stage boundary changes when code may be written, but it does not weaken the validation requirements above.

Record each stage's terminal status and evidence in the fix report, or in the visible response when running standalone. Carry the finding identity, base revision, and patch digest across stages yourself; there is no external workbench holding that state.

- **Generate**: Keep the selected target checkout unchanged. Use an isolated worktree or temporary copy when edits are needed to develop or test the fix. Apply the patch contract and strategy gates above, write one canonical unified diff containing the complete source and regression-test change, then record `generated` or `failed` using the supplied workbench identity.
- **Apply**: Verify the recorded base revision and patch digest, then apply exactly that patch to the selected working tree without unrelated edits. Record `applied` or `failed`. Do not verify or close the finding in this stage.
- **Verify**: Do not modify source. Apply the selected mode's verification gates above against the recorded patch. Record `verified` only when evidence establishes that the original vulnerable path is closed, legitimate behavior remains intact, and the required checks pass; preserve exact evidence and any commands run in the verification summary. Otherwise record `failed` and state the failing gate or proof gap. Do not close the finding.

When a parent thread delegates a remediation stage, the worker owns that stage through its terminal workbench update. The parent remains an orchestrator and must not duplicate the worker's edits or treat a chat response as completion.

## Outcome and Output Contract

In the final response, include:

- outcome: `fixed`, `no_change`, or `blocked`
- the concrete vulnerable path, security invariant, and legitimate behavior that had to remain
- the selected patch strategy and why it was the narrowest complete repository-native option, or the unresolved product decision when blocked
- files changed
- tests or validation artifacts added
- commands run and their pass, fail, or unknown results, grouped by the ordered verification gates
- explicit statement of how the original issue was shown not to reproduce
- explicit statement of how legitimate behavior was shown to remain intact
- remaining uncertainty or skipped validation, if any

If using a scan artifact directory, resolve it using `scan-artifacts.md`, then write a visible report to the fix report path. If there is no existing scan directory, a final chat summary is sufficient unless the user asks for a file.

## Hard Rules

- Do not report `fixed` until every verification gate for the selected mode has passed. Omit a check required by that mode only when repository evidence shows it is irrelevant; an unavailable required check makes verification `blocked` and must be reported.
- In standard mode, do not rely only on code inspection when a focused test or reproducer is feasible.
- Do not broaden the patch into unrelated cleanup, sibling findings, or architectural redesign without evidence that the broader change is required for complete closure.
- Do not remove user changes or unrelated local modifications.
- Do not weaken authentication, authorization, tenant isolation, input validation, sandboxing, or logging to make tests pass.
- Do not hide proof gaps. If the environment blocks validation, say exactly which command or setup failed and what evidence is still missing.

## Verify-Only Mode

This section ports upstream's separate `verify-fix` skill. Its trigger: use only
when the user explicitly requests verification that a security fix remediates a
reported vulnerability. Do not invoke it automatically while implementing fixes,
reviewing ordinary code changes, or running tests. Do not use it for non-security
fixes, candidate finding validation, or full repository scans.

### When to Use

Use this section only for an explicit request to verify a security fix. A request
to implement a fix or run its tests does not by itself request it. For other
tasks, follow the user's requested workflow and response format without applying
this section's JSON result contract.

### Objective

Determine whether each supplied security finding has been fixed in the current
checkout. Operate in standalone verification-only mode; do not create, modify, or
delete repository files, apply patches, commit changes, write artifacts, or modify
issue trackers.

### Assessment Method

Use `static-finding-assessment.md` to identify the original attacker-controlled
source, security control, sensitive sink, reachable path, trust boundary,
counterevidence, and proof gaps. If the caller already supplied that reference in
the prompt, use the supplied contents without reading it again.

Use `standard` verification unless the caller explicitly selects
`verificationMode: static`. In static mode, assess the current source and diff,
the original exploit path and plausible bypasses, and legitimate behavior through
callers and existing tests read as source. Do not install dependencies or run
builds, tests, reproducers, or the application, including when validation
instructions request them. Repository-reading tools remain available. State in
the evidence that this is a static assessment and runtime checks were not
performed; do not count them as passed or require them for this assessment.
Missing runtime tooling alone does not make the result inconclusive, but a
material gap in the source evidence does.

### Verification Workflow

1. Establish the original vulnerability, its preconditions, affected security
   boundary, and legitimate behavior that must continue to work.
2. Confirm the current checkout contains the affected component. Follow moved or
   refactored code rather than treating a missing file, removed line, or changed
   function name as proof of remediation.
3. Trace the original exploit path through the current implementation and check
   the nearest relevant control, equivalent paths, and plausible bypasses.
4. In standard mode, run the original reproducer, focused regression checks, or
   legitimate-behavior checks only when they can run without modifying the
   repository. Preserve exact static evidence when runtime checks are unavailable.
5. Return one result per supplied finding, in the requested order. Treat closed
   tickets, unrelated passing tests, and the absence of a new scan finding as
   insufficient proof.

### Result Contract

Return exactly one JSON object:

```json
{
  "results": [
    {
      "id": "finding-or-issue-id",
      "status": "fixed|still_vulnerable|inconclusive",
      "evidence": "specific current source, exploit, test, or proof-gap evidence"
    }
  ]
}
```

- Use `fixed` only when evidence proves the original security boundary is closed
  and legitimate behavior remains intact.
- Use `still_vulnerable` only when evidence proves the original vulnerable path
  remains reachable.
- Use `inconclusive` for a repository mismatch, missing original context,
  unavailable checks required by the selected mode, an unproven legitimate
  control, or another material proof gap.

Never infer a stronger verdict by weakening the read-only boundary, substituting a
different vulnerability, or hiding missing evidence.
