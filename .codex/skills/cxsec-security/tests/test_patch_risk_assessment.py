"""Carried validate_patch_risk_assessment.py, held to upstream's current TypeScript contract.

Ported from openai/codex-security 0.6.0:
  sdk/typescript/tests-ts/patch-risk-contract.test.ts
  plugins/codex-security/mcp-app/src/helpers/validate-patch-risk-assessment.ts
The validator runs from the bundled runtime copy so PLUGIN_ROOT resolves to runtime/.
Omitted upstream cases, all Windows- or launcher-specific:
  - "executes the documented PowerShell command with {absolute,relative,unc} literal
    assessment and plugin paths": drives the Windows launcher fixture and PowerShell.
  - the launch_codex_security_mcp step of "reads file and stdin inputs without
    rewriting the assessment": exercises the packaged Node launcher, not the helper.
"Without Python" in upstream names means the Node helper needs no Python; the
assertions themselves are ported. The last two tests are Python-only: they cover
diagnostics this carried validator echoes and upstream's does not (duplicate keys,
schema property names). Run with: python3 -m pytest tests/
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
from jsonschema import Draft202012Validator

SKILL_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = SKILL_ROOT / "runtime"
SCRIPT = PLUGIN_ROOT / "skills" / "assess-patch-risk" / "scripts" / "validate_patch_risk_assessment.py"
SCHEMA_PATH = PLUGIN_ROOT / "schemas" / "patch-risk-assessment.schema.json"
POSIX = sys.platform != "win32"
NON_APPLICABLE = ["no_live_effect", "wrong_owner", "duplicate", "superseded"]
CONTROLS = [("\x1b", "\\u001b"), ("‮", "\\u202e"), ("\U000e0001", "\\u{e0001}")]

Row = dict[str, Any]


class Result:
    def __init__(self, process: subprocess.CompletedProcess[bytes]) -> None:
        self.status = process.returncode
        self.stdout = process.stdout.decode("utf-8", "surrogateescape")
        self.stderr = process.stderr.decode("utf-8", "surrogateescape")


def assessment() -> Row:
    return {
        "schemaVersion": 1,
        "patch": {
            "repository": "example/project",
            "sourceType": "pull_request_diff",
            "base": "a" * 40,
            "head": "b" * 40,
            "changedFiles": ["src/request.ts"],
            "sha256": "c" * 64,
        },
        "recommendation": "merge",
        "workflowLabel": "human_review_required",
        "impact": {"rating": "moderate", "rationale": "A bounded caller can fail."},
        "regressionLikelihood": {
            "rating": "low",
            "rationale": "The changed path and its caller are covered.",
        },
        "regressionProtection": {
            "rating": "strong",
            "rationale": "Focused and integration checks passed at the exact head.",
            "exactHeadChecksPassed": True,
        },
        "recoverability": {"rating": "easy", "rationale": "A revert is isolated."},
        "confidence": {"rating": "high", "rationale": "Runtime callers are known."},
        "applicability": {"status": "confirmed", "rationale": "The path is deployed."},
        "statusQuoRisk": {"rating": "moderate", "rationale": "The defect remains."},
        "autoMergeExclusions": [],
        "affectedRuntimeRoots": ["service.request"],
        "materialBoundaries": [
            {
                "id": "request-contract",
                "invariant": "Supported requests retain their existing response contract.",
                "runtimeRoot": "service.request",
                "counterexample": "A supported request takes the changed branch.",
                "legitimateControl": "A supported request takes the unchanged branch.",
                "result": "supported",
            }
        ],
        "validation": [
            {
                "name": "focused request tests",
                "status": "passed",
                "protects": "Changed behavior through the production caller.",
            }
        ],
        "unknowns": [],
        "evidencePlan": [],
    }


def serialize(payload: Row, indent: int | None = None) -> str:
    # Match JSON.stringify: compact separators, raw non-ASCII.
    separators = None if indent else (",", ":")
    return json.dumps(payload, indent=indent, separators=separators, ensure_ascii=False)


def validate_text(
    text: str | bytes, cwd: Path = PLUGIN_ROOT, args: list[str] | None = None, timeout: float = 60
) -> Result:
    process = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *(["-"] if args is None else args)],
        cwd=cwd,
        input=text.encode("utf-8", "surrogateescape") if isinstance(text, str) else text,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return Result(process)


def validate(payload: Row) -> Result:
    return validate_text(serialize(payload))


def write(path: Path, data: str | bytes) -> None:
    path.write_bytes(data.encode() if isinstance(data, str) else data)


def lines(result: Result) -> list[str]:
    return result.stderr.strip().split("\n")


def evidence_step(outcomes: dict[str, str], question: str = "Is the boundary protected?") -> Row:
    return {"question": question, "action": "Inspect the corresponding evidence.", "outcomes": outcomes}


def test_loads_the_installed_helper_from_another_working_directory(tmp_path: Path) -> None:
    result = validate_text(serialize(assessment()), tmp_path)
    assert result.status == 0, result.stderr


def test_publishes_a_valid_draft_2020_12_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert validator.is_valid(assessment()), list(validator.iter_errors(assessment()))

    raw_worktree = assessment()
    raw_worktree["patch"]["sourceType"] = "raw_worktree"
    assert not validator.is_valid(raw_worktree)


def test_enforces_the_published_patch_risk_schema() -> None:
    valid = validate(assessment())
    assert valid.status == 0, valid.stderr

    duplicate_changed_files = assessment()
    duplicate_changed_files["patch"]["changedFiles"] = ["src/request.ts", "src/request.ts"]
    assert validate(duplicate_changed_files).status != 0

    empty_rationale = assessment()
    empty_rationale["impact"]["rationale"] = ""
    assert validate(empty_rationale).status != 0

    duplicate_items = assessment()
    duplicate_items["autoMergeExclusions"] = ["migration", "migration"]
    assert validate(duplicate_items).status != 0

    too_many = assessment()
    too_many["evidencePlan"] = [
        evidence_step({"supported": "merge", "contradicted": "revise"}, f"Question {index}")
        for index in range(4)
    ]
    result = validate(too_many)
    assert result.status == 1
    assert "evidencePlan" in result.stderr

    too_few = assessment()
    too_few["evidencePlan"] = [evidence_step({"supported": "merge"})]
    result = validate(too_few)
    assert result.status == 1
    assert "outcomes" in result.stderr

    empty_outcome = assessment()
    empty_outcome["evidencePlan"] = [evidence_step({"supported": "", "contradicted": "revise"})]
    result = validate(empty_outcome)
    assert result.status == 1
    assert "outcomes/supported" in result.stderr


def test_enforces_the_published_schema_for_each_invalid_shape() -> None:
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    invalid: list[Row] = []

    missing_required = assessment()
    del missing_required["patch"]
    invalid.append(missing_required)

    invalid.append({**assessment(), "unexpected": True})

    invalid_pattern = assessment()
    invalid_pattern["patch"]["sha256"] = "g" * 64
    invalid.append(invalid_pattern)

    trailing_newline_digest = assessment()
    trailing_newline_digest["patch"]["sha256"] = "c" * 64 + "\n"
    invalid.append(trailing_newline_digest)

    empty_validation = assessment()
    empty_validation["validation"] = []
    invalid.append(empty_validation)

    duplicate_items = assessment()
    duplicate_items["autoMergeExclusions"] = ["migration", "migration"]
    invalid.append(duplicate_items)

    empty_string = assessment()
    empty_string["impact"]["rationale"] = ""
    invalid.append(empty_string)

    for payload in invalid:
        # jsonschema's Python "$" also matches before a final newline, unlike ajv's.
        if payload is not trailing_newline_digest:
            assert not validator.is_valid(payload)
        assert validate(payload).status != 0, serialize(payload)


def test_accepts_a_supported_human_review_merge() -> None:
    result = validate(assessment())
    assert result.status == 0, result.stderr


def test_rejects_critical_regression_likelihood_for_every_merge_workflow() -> None:
    for workflow_label in ("human_review_required", "auto_merge_candidate"):
        payload = assessment()
        payload["workflowLabel"] = workflow_label
        payload["impact"]["rating"] = "low"
        payload["regressionLikelihood"]["rating"] = "critical"
        result = validate(payload)
        assert result.status == 1
        assert "merge cannot retain critical regression likelihood" in result.stderr


@pytest.mark.parametrize("rating", ["low", "moderate", "high"])
def test_accepts_critical_impact_human_review_merge(rating: str) -> None:
    payload = assessment()
    payload["impact"]["rating"] = "critical"
    payload["regressionLikelihood"]["rating"] = rating
    result = validate(payload)
    assert result.status == 0, result.stderr


def test_compares_json_numeric_constants_by_value() -> None:
    serialized = serialize(assessment()).replace('"schemaVersion":1', '"schemaVersion":1.0')
    result = validate_text(serialized)
    assert result.status == 0, result.stderr


def test_enforces_strict_auto_merge_gates() -> None:
    payload = assessment()
    payload["workflowLabel"] = "auto_merge_candidate"
    assert validate(payload).status != 0

    payload["impact"]["rating"] = "low"
    accepted = validate(payload)
    assert accepted.status == 0, accepted.stderr


def test_requires_a_bounded_evidence_plan_for_an_evidence_hold() -> None:
    payload = assessment()
    payload["recommendation"] = payload["workflowLabel"] = "hold_for_evidence"
    payload["unknowns"] = [{"summary": "The rollout target is unavailable.", "decisionCritical": True}]
    assert validate(payload).status != 0

    payload["evidencePlan"] = [
        {
            "question": "Does the changed configuration own the rollout target?",
            "action": "Inspect the checked-in deployment mapping.",
            "outcomes": {
                "supported": "merge",
                "contradicted": "no_op",
                "unavailable": "hold_for_evidence",
            },
        }
    ]
    accepted = validate(payload)
    assert accepted.status == 0, accepted.stderr

    payload["evidencePlan"][0]["outcomes"] = {"__proto__": "merge", "contradicted": "revise"}
    assert validate(payload).status == 0
    payload["evidencePlan"][0]["outcomes"] = {"__proto__": "unsupported", "contradicted": "revise"}
    result = validate(payload)
    assert result.status == 1
    assert "outcomes" in result.stderr


def test_requires_an_established_non_applicable_no_op() -> None:
    payload = assessment()
    payload["recommendation"] = payload["workflowLabel"] = "no_op"
    assert validate(payload).status != 0

    payload["applicability"] = {"status": "superseded", "rationale": "A narrower patch already landed."}
    accepted = validate(payload)
    assert accepted.status == 0, accepted.stderr


def test_requires_affirmative_failure_evidence_for_a_block() -> None:
    payload = assessment()
    payload["recommendation"] = payload["workflowLabel"] = "block"
    assert validate(payload).status != 0

    payload["materialBoundaries"][0]["result"] = "contradicted"
    accepted = validate(payload)
    assert accepted.status == 0, accepted.stderr


def test_requires_affirmative_failure_evidence_for_a_revision() -> None:
    payload = assessment()
    payload["recommendation"] = payload["workflowLabel"] = "revise"
    assert validate(payload).status != 0

    payload["validation"][0]["status"] = "failed"
    accepted = validate(payload)
    assert accepted.status == 0, accepted.stderr


def test_keeps_failed_validation_and_established_defects_out_of_merge_and_hold() -> None:
    merge = assessment()
    merge["validation"][0]["status"] = "failed"
    assert validate(merge).status != 0

    hold = assessment()
    hold["recommendation"] = hold["workflowLabel"] = "hold_for_evidence"
    hold["materialBoundaries"][0]["result"] = "contradicted"
    hold["unknowns"] = [{"summary": "A separate rollout detail is unavailable.", "decisionCritical": True}]
    hold["evidencePlan"] = [
        {
            "question": "Which rollout target is selected?",
            "action": "Inspect the checked-in deployment mapping.",
            "outcomes": {"found": "revise", "unavailable": "hold_for_evidence"},
        }
    ]
    assert validate(hold).status != 0


def test_requires_no_op_for_an_established_non_applicable_disposition() -> None:
    payload = assessment()
    payload["recommendation"] = payload["workflowLabel"] = "block"
    payload["applicability"]["status"] = "wrong_owner"
    payload["materialBoundaries"][0]["result"] = "contradicted"
    assert validate(payload).status != 0


GATES: list[tuple[str, Callable[[Row], None]]] = [
    ("impact.rating", lambda value: value["impact"].update(rating="moderate")),
    ("regressionLikelihood.rating", lambda value: value["regressionLikelihood"].update(rating="high")),
    ("regressionProtection.rating", lambda value: value["regressionProtection"].update(rating="partial")),
    (
        "regressionProtection.exactHeadChecksPassed",
        lambda value: value["regressionProtection"].update(exactHeadChecksPassed=False),
    ),
    ("recoverability.rating", lambda value: value["recoverability"].update(rating="managed")),
    ("confidence.rating", lambda value: value["confidence"].update(rating="moderate")),
    ("applicability.status", lambda value: value["applicability"].update(status="unknown")),
    ("affectedRuntimeRoots", lambda value: value.update(affectedRuntimeRoots=[])),
    ("statusQuoRisk.rating", lambda value: value["statusQuoRisk"].update(rating="unknown")),
    ("autoMergeExclusions", lambda value: value.update(autoMergeExclusions=["public_contract"])),
    (
        "unknowns",
        lambda value: value.update(
            unknowns=[{"summary": "A non-critical detail.", "decisionCritical": False}]
        ),
    ),
    ("validation", lambda value: value["validation"][0].update(status="skipped")),
]


def test_preserves_the_ordered_auto_merge_gates() -> None:
    payload = assessment()
    payload["workflowLabel"] = "auto_merge_candidate"
    payload["impact"]["rating"] = "low"
    assert validate(payload).status == 0

    for field, mutate in GATES:
        changed = copy.deepcopy(payload)
        mutate(changed)
        result = validate(changed)
        assert result.status == 1
        assert f"auto_merge_candidate gate failed: {field}" in result.stderr
        mutate(payload)

    assert lines(validate(payload)) == [
        "merge requires confirmed applicability",
        *(f"auto_merge_candidate gate failed: {field}" for field, _ in GATES),
    ]


def test_keeps_all_merge_errors_in_their_established_order() -> None:
    payload = assessment()
    payload["regressionLikelihood"]["rating"] = "critical"
    payload["workflowLabel"] = "block"
    payload["applicability"]["status"] = "wrong_owner"
    payload["unknowns"] = [{"summary": "The owner is unknown.", "decisionCritical": True}]
    payload["materialBoundaries"][0]["result"] = "unresolved"
    payload["validation"][0]["status"] = "failed"
    payload["evidencePlan"] = [
        {
            "question": "Who owns this?",
            "action": "Inspect the mapping.",
            "outcomes": {"owned": "revise", "unowned": "no_op"},
        }
    ]
    result = validate(payload)
    assert result.status == 1
    assert result.stdout == ""
    assert lines(result) == [
        "merge requires an auto-merge or human-review workflow label",
        "merge requires confirmed applicability",
        "merge cannot retain a decision-critical unknown",
        "merge requires every material boundary to be supported",
        "merge cannot retain a failed validation",
        "merge cannot retain an evidence plan",
        "merge cannot retain critical regression likelihood",
        "only hold_for_evidence may include an evidence plan",
        "an established non-applicable disposition requires no_op",
    ]

    del payload["patch"]
    missing_patch = validate(payload)
    assert missing_patch.status == 1
    assert "patch-risk-assessment.schema" in missing_patch.stderr


def test_requires_matching_non_merge_labels_and_a_settled_no_op_disposition() -> None:
    payload = assessment()
    payload["recommendation"] = "revise"
    payload["validation"][0]["status"] = "failed"
    assert validate(payload).stderr == "non-merge workflow label must match the recommendation\n"
    payload["workflowLabel"] = "revise"
    assert validate(payload).status == 0

    for status in NON_APPLICABLE:
        no_op = assessment()
        no_op["recommendation"] = no_op["workflowLabel"] = "no_op"
        no_op["applicability"]["status"] = status
        assert validate(no_op).status == 0
        no_op["unknowns"] = [{"summary": "Coverage is unresolved.", "decisionCritical": True}]
        assert validate(no_op).stderr == "no_op cannot retain a decision-critical unknown\n"


FAILURES: list[Callable[[Row], None]] = [
    lambda value: value["regressionLikelihood"].update(rating="critical"),
    lambda value: value["materialBoundaries"][0].update(result="contradicted"),
    lambda value: value["validation"][0].update(status="failed"),
]


@pytest.mark.parametrize("fail", FAILURES, ids=["critical", "contradicted", "failed"])
def test_keeps_each_established_failure_out_of_an_evidence_hold(fail: Callable[[Row], None]) -> None:
    payload = assessment()
    payload["recommendation"] = payload["workflowLabel"] = "hold_for_evidence"
    payload["unknowns"] = [{"summary": "A rollout detail.", "decisionCritical": True}]
    payload["evidencePlan"] = [
        {
            "question": "Which rollout?",
            "action": "Inspect deployment.",
            "outcomes": {"owned": "merge", "unowned": "no_op"},
        }
    ]
    fail(payload)
    assert validate(payload).stderr == "hold_for_evidence cannot defer an established defect\n"

    payload["evidencePlan"] = []
    for recommendation in ("revise", "block"):
        payload["recommendation"] = payload["workflowLabel"] = recommendation
        assert validate(payload).status == 0


def test_requires_numeric_schema_version_1() -> None:
    serialized = serialize(assessment())
    for token in ("1", "1.0", "1e0"):
        result = validate_text(serialized.replace('"schemaVersion":1', f'"schemaVersion":{token}'))
        assert result.status == 0, (token, result.stderr)
    for token in ("true", "false", '"1"', "0", "null", "9007199254740993"):
        result = validate_text(serialized.replace('"schemaVersion":1', f'"schemaVersion":{token}'))
        assert result.status == 1, token
        assert "schemaVersion" in result.stderr, token


def test_accepts_large_assessments_and_rejects_truncated_json() -> None:
    complete = assessment()
    complete["impact"]["rationale"] = '"' * 5_000_000
    truncated = '{"impact":{"rationale":"' + '\\"' * 5_000_000 + "\\"

    result = validate_text(truncated)
    assert result.status == 1, result.stderr
    assert "cannot read assessment:" in result.stderr

    result = validate_text(serialize(complete))
    assert result.status == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize(
    "text",
    [
        "﻿{}",
        '{"x":1,}',
        '{"x" "\\q"}',
        '[0 "\\q"]',
        '{"x":"\\uZZZZ"}',
        '{"x":"\\q"}',
        '"\\q',
        '"\\u123',
        '"truncated\\',
        '{"x":"line\n"}',
        '{"x":"\U0001f600\\q"}',
        "{} false",
        '{"schemaVersion":NaN}',
        '{"schemaVersion":Infinity}',
    ],
)
def test_rejects_malformed_json_with_a_useful_diagnostic(text: str) -> None:
    result = validate_text(text)
    assert result.status == 1
    assert "cannot read assessment:" in result.stderr


def test_rejects_a_non_object_document() -> None:
    result = validate_text("[]")
    assert result.status == 1
    assert "assessment must be a JSON object" in result.stderr


@pytest.mark.parametrize("control,escaped", CONTROLS)
def test_escapes_terminal_controls_echoed_by_json_syntax_errors(control: str, escaped: str) -> None:
    result = validate_text(f"{control}[2J")
    assert result.status == 1
    assert "cannot read assessment:" in result.stderr
    assert control not in result.stderr
    assert escaped in result.stderr


def test_reads_file_and_stdin_inputs_without_rewriting_the_assessment(tmp_path: Path) -> None:
    original = serialize(assessment(), indent=2).replace("\n", "\r\n") + "\r\n"
    for name in ("assessment with spaces.json", "-1", "- item", "-"):
        path = tmp_path / name
        write(path, original)
        result = validate_text("not stdin", tmp_path, [f"./{name}" if name.startswith("-") else name])
        assert result.status == 0, (name, result.stderr)
        assert result.stdout == ""
        assert result.stderr == ""
        assert path.read_bytes() == original.encode()
        assert validate_text("", tmp_path, ["--", name]).status == (1 if name == "-" else 0), name

    file = str(tmp_path / "assessment with spaces.json")
    if POSIX:
        for suffix in ("/.", "///", "/./."):
            assert validate_text("", tmp_path, [file + suffix]).status == 0, suffix
        (tmp_path / "child" / "nested").mkdir(parents=True)
        (tmp_path / "link").symlink_to("child/nested")
        write(tmp_path / "child" / "assessment.json", original)
        write(tmp_path / "assessment.json", "wrong sibling")
        assert validate_text("", tmp_path, ["link/../assessment.json/."]).status == 0

    assert validate_text(original, tmp_path).status == 0

    invalid = tmp_path / "invalid.json"
    malformed = '{\r\n"x":1\r\n"y":2}'
    write(invalid, malformed)
    assert "cannot read assessment:" in validate_text(malformed).stderr
    assert "cannot read assessment:" in validate_text("", tmp_path, [str(invalid)]).stderr
    write(invalid, bytes([0xFF]))
    assert validate_text("", tmp_path, [str(invalid)]).status == 1
    assert "cannot read assessment:" in validate_text("", tmp_path, ["missing.json"]).stderr


@pytest.mark.parametrize("control,escaped", CONTROLS)
def test_escapes_terminal_controls_in_assessment_file_read_error_paths(
    tmp_path: Path, control: str, escaped: str
) -> None:
    file = tmp_path / f"missing-{control}[2J.json"
    result = validate_text("", tmp_path, [str(file)])
    assert result.status == 1
    assert result.stdout == ""
    assert f"missing-{escaped}[2J.json" in result.stderr
    assert control not in result.stderr


@pytest.mark.skipif(not POSIX, reason="POSIX stdin bytes")
def test_keeps_stdin_surrogate_escapes_distinct_from_replacement_characters() -> None:
    serialized = serialize(assessment()).replace(
        '"changedFiles":["src/request.ts"]', '"changedFiles":["RAW","\\ufffd"]'
    )
    before, after = serialized.split("RAW")
    result = validate_text(before.encode() + bytes([0xFF]) + after.encode())
    assert result.status == 0, result.stderr


def test_supports_documented_help_positional_arguments_and_parser_exit_statuses() -> None:
    for args in (["-h"], ["--help"]):
        result = validate_text("", PLUGIN_ROOT, args)
        assert result.status == 0
        assert "Validate a patch-risk assessment." in result.stdout
    for args in ([], ["--"], ["--bad"], ["one", "two"], ["--help=bad"], ["-h=bad"]):
        assert validate_text("", PLUGIN_ROOT, args).status == 2, args
    assert validate_text("", PLUGIN_ROOT, ["--", "-h"]).status == 1


def test_escapes_terminal_controls_in_duplicate_key_diagnostics() -> None:
    # Python-only: upstream's JSON.parse keeps the last duplicate; the carried
    # validator rejects duplicates, so the echoed key must not carry controls.
    result = validate_text('{"\\u001b[2J":1,"\\u001b[2J":2}')
    assert result.status == 1
    assert "cannot read assessment:" in result.stderr
    assert "\x1b" not in result.stderr
    assert "\\u001b[2J" in result.stderr


def test_escapes_terminal_controls_in_schema_diagnostics() -> None:
    # Python-only: the carried schema engine echoes property names, which ajv does not.
    result = validate_text(serialize({**assessment(), "\x1b[2J": True}))
    assert result.status == 1
    assert "patch-risk-assessment.schema/\\u001b[2J: unexpected schema property" in result.stderr
    assert "\x1b" not in result.stderr
