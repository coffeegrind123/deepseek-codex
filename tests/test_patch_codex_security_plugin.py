from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "patch_codex_security_plugin.py"
MARKER = ".codex-security-cost-patch.json"

BASELINE_FORBID = (
    "Do not load Codex Security skills, start another scan, use security-scan "
    "tools, or delegate."
)
FOCUSED_FORBID = (
    "Do not invoke Codex Security phase skills or MCP tools, load their "
    "references, or delegate to another worker."
)


def _write_source(root: Path, version: str = "9.8.7") -> None:
    plugin_dir = root / ".codex-plugin"
    skill_dir = root / "skills" / "security-scan"
    references_dir = root / "references"
    plugin_dir.mkdir(parents=True)
    skill_dir.mkdir(parents=True)
    references_dir.mkdir(parents=True)

    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "codex-security", "version": version}) + "\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "# Security Scan\n\nIntro text.\n\n## Workflow\n\n1. Do the work.\n",
        encoding="utf-8",
    )
    (references_dir / "core-scan.md").write_text(
        "\n".join(
            [
                "# Core Security Scan",
                "",
                "8. Assemble complete semantic results and retain source evidence.",
                "",
                "## Baseline Auditor Prompt",
                "",
                BASELINE_FORBID,
                "",
                "Return only JSON with a `findings` array and source evidence.",
                "",
                "## Focused Investigator Prompt",
                "",
                FOCUSED_FORBID,
                "",
                "Return only JSON with a `findings` array and source evidence.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (root / "unchanged.txt").write_text("source payload\n", encoding="utf-8")


def _snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path).encode())
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            snapshot[relative] = ("directory", b"")
    return snapshot


def _run(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(arg) for arg in args)],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _write_source(source)
    return source, output


def test_stage_patches_and_reuses_valid_output(trees: tuple[Path, Path]) -> None:
    source, output = trees
    source_before = _snapshot(source)

    first = _run("--source", source, "--output", output)
    assert first.returncode == 0, first.stderr
    assert "staged patched output" in first.stdout

    marker = json.loads((output / MARKER).read_text(encoding="utf-8"))
    assert marker["patchVersion"] == 2
    assert marker["sourceVersion"] == "9.8.7"
    assert marker["sourceDigest"].startswith("sha256:")
    assert marker["patches"] == [
        "security-scan-context-budget",
        "core-scan-deferred-report-reads",
        "subagent-context-isolation",
        "schema-read-on-validation-error",
    ]

    skill = (output / "skills/security-scan/SKILL.md").read_text(encoding="utf-8")
    budget = skill.index("## Context Budget")
    workflow = skill.index("## Workflow")
    assert budget < workflow
    assert "only at the workflow step that explicitly names them" in skill
    assert "semantic review and validation are complete" in skill
    assert "Read each required file once" in skill
    assert "Never list, browse, or recursively search the plugin tree" in skill
    assert "only after a named validation error" in skill
    assert "Do not reread generated scan artifacts" in skill
    assert "Subagents must not read bundled references, schemas" in skill

    core = (output / "references/core-scan.md").read_text(encoding="utf-8")
    step_eight = core.index(
        "8. Defer reading `final-report.md` and `finding-detail-fields.md`"
    )
    assert step_eight < core.index("Assemble complete semantic")
    assert "never read either during discovery" in core
    assert core.count(BASELINE_FORBID) == 1
    assert core.count(FOCUSED_FORBID) == 1
    assert core.count(
        "Do not read bundled references or schemas, and never list or browse "
        "the plugin tree."
    ) == 2
    assert core.count("Return only semantic JSON with a") == 2

    output_inode = output.stat().st_ino
    second = _run("--source", source, "--output", output)
    assert second.returncode == 0, second.stderr
    assert "reused patched output" in second.stdout
    assert output.stat().st_ino == output_inode

    assert _snapshot(source) == source_before
    assert not (source / MARKER).exists()


def test_check_succeeds_and_reports_tampered_output(
    trees: tuple[Path, Path],
) -> None:
    source, output = trees
    assert _run("--source", source, "--output", output).returncode == 0

    checked = _run("--source", source, "--output", output, "--check")
    assert checked.returncode == 0, checked.stderr
    assert "verified patched output" in checked.stdout

    patched = output / "references/core-scan.md"
    original = patched.read_text(encoding="utf-8")
    patched.write_text(original + "\nlocal edit\n", encoding="utf-8")
    failed = _run("--source", source, "--output", output, "--check")
    assert failed.returncode != 0
    assert "output entry does not match" in failed.stderr


def test_check_rejects_mismatched_source_digest(
    trees: tuple[Path, Path],
) -> None:
    source, output = trees
    assert _run("--source", source, "--output", output).returncode == 0
    (source / "unchanged.txt").write_text("new source payload\n", encoding="utf-8")

    failed = _run("--source", source, "--output", output, "--check")
    assert failed.returncode != 0
    assert "sourceDigest" in failed.stderr


def test_check_can_allow_explicit_local_companion_files(
    trees: tuple[Path, Path],
) -> None:
    source, output = trees
    assert _run("--source", source, "--output", output).returncode == 0
    backup = output / "mcp" / "daybreak-original.server.mjs.br.part-000"
    backup.parent.mkdir()
    backup.write_text("local companion\n", encoding="utf-8")

    rejected = _run("--source", source, "--output", output, "--check")
    assert rejected.returncode != 0
    assert "unexpected entry" in rejected.stderr

    allowed = _run(
        "--source",
        source,
        "--output",
        output,
        "--check",
        "--allow-extra",
        "mcp/daybreak-original.server.mjs.br.part-*",
    )
    assert allowed.returncode == 0, allowed.stderr


def test_check_can_allow_explicit_local_modifications(
    trees: tuple[Path, Path],
) -> None:
    source, output = trees
    assert _run("--source", source, "--output", output).returncode == 0
    modified = output / "unchanged.txt"
    modified.write_text("locally patched payload\n", encoding="utf-8")

    rejected = _run("--source", source, "--output", output, "--check")
    assert rejected.returncode != 0
    assert "does not match" in rejected.stderr

    allowed = _run(
        "--source",
        source,
        "--output",
        output,
        "--check",
        "--allow-modified",
        "unchanged.txt",
    )
    assert allowed.returncode == 0, allowed.stderr


def test_missing_output_marker_fails_without_creating_partial_output(
    trees: tuple[Path, Path],
) -> None:
    source, output = trees
    output.mkdir()
    (output / "partial.txt").write_text("existing data\n", encoding="utf-8")
    before = _snapshot(output)

    failed = _run("--source", source, "--output", output, "--check")
    assert failed.returncode != 0
    assert MARKER in failed.stderr
    assert "run without --check" in failed.stderr
    assert _snapshot(output) == before


def test_missing_upstream_anchor_fails_without_output_or_source_changes(
    trees: tuple[Path, Path],
) -> None:
    source, output = trees
    skill = source / "skills/security-scan/SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace("## Workflow", "Workflow"),
        encoding="utf-8",
    )
    source_before = _snapshot(source)

    failed = _run("--source", source, "--output", output)
    assert failed.returncode != 0
    assert "expected exactly one upstream marker" in failed.stderr
    assert "## Workflow" in failed.stderr
    assert not output.exists()
    assert _snapshot(source) == source_before
