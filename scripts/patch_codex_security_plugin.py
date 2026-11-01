#!/usr/bin/env python3
"""Stage a cost-optimized copy of the bundled Codex Security plugin."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


MARKER_NAME = ".codex-security-cost-patch.json"
PLUGIN_INFO = Path(".codex-plugin/plugin.json")
SKILL_FILE = Path("skills/security-scan/SKILL.md")
CORE_SCAN_FILE = Path("references/core-scan.md")
PATCH_VERSION = 2
PATCHES = (
    "security-scan-context-budget",
    "core-scan-deferred-report-reads",
    "subagent-context-isolation",
    "schema-read-on-validation-error",
)

WORKFLOW_ANCHOR = "## Workflow\n"
CONTEXT_BUDGET = """## Context Budget

Load files under `references/` and `schemas/` only at the workflow step that explicitly names them. Defer `references/final-report.md` and `references/finding-detail-fields.md` until after semantic review and validation are complete. Read each required file once. Never list, browse, or recursively search the plugin tree to discover files.

Do not read `scan-manifest.schema.json`, `findings.schema.json`, or `coverage.schema.json` during setup or discovery. The host draft tool validates those documents. Read a schema only after a named validation error, and only the section needed to correct that error. Do not reread generated scan artifacts unless the user requests a recheck.

Subagents must not read bundled references, schemas, or the plugin tree.

"""

STEP_EIGHT_ANCHOR = "Assemble complete semantic"
STEP_EIGHT_PREFIX = (
    "Defer reading `final-report.md` and `finding-detail-fields.md` until "
    "after semantic review and validation are complete; never read either "
    "during discovery. "
)

BASELINE_HEADING = "## Baseline Auditor Prompt"
FOCUSED_HEADING = "## Focused Investigator Prompt"
BASELINE_FORBID_ANCHOR = (
    "Do not load Codex Security skills, start another scan, use security-scan "
    "tools, or delegate."
)
FOCUSED_FORBID_ANCHOR = (
    "Do not invoke Codex Security phase skills or MCP tools, load their "
    "references, or delegate to another worker."
)
CONTEXT_FORBID = (
    "Do not read bundled references or schemas, and never list or browse the "
    "plugin tree."
)
RETURN_JSON_ANCHOR = "Return only JSON with a"
RETURN_SEMANTIC_JSON = "Return only semantic JSON with a"


class PatchError(RuntimeError):
    """A user-actionable staging or validation error."""


@dataclass(frozen=True)
class SourceState:
    version: str
    digest: str
    patched_files: dict[str, bytes]


TreeEntry = tuple[str, bytes]


def _replace_exactly(text: str, old: str, new: str, location: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(
            f"{location}: expected exactly one upstream marker {old!r}; found {count}"
        )
    return text.replace(old, new, 1)


def _section_bounds(text: str, heading: str, location: str) -> tuple[int, int]:
    count = text.count(heading)
    if count != 1:
        raise PatchError(
            f"{location}: expected exactly one prompt heading {heading!r}; found {count}"
        )

    start = text.index(heading)
    next_heading = text.find("\n## ", start + len(heading))
    end = len(text) if next_heading < 0 else next_heading + 1
    return start, end


def _patch_prompt(
    text: str, heading: str, forbid_anchor: str, location: str
) -> str:
    start, end = _section_bounds(text, heading, location)
    section = text[start:end]
    section = _replace_exactly(
        section,
        forbid_anchor,
        f"{forbid_anchor} {CONTEXT_FORBID}",
        f"{location} section {heading!r}",
    )
    section = _replace_exactly(
        section,
        RETURN_JSON_ANCHOR,
        RETURN_SEMANTIC_JSON,
        f"{location} section {heading!r}",
    )
    return text[:start] + section + text[end:]


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PatchError(f"required file is missing: {path}") from exc
    except UnicodeDecodeError as exc:
        raise PatchError(f"required file is not valid UTF-8: {path}: {exc}") from exc


def _patched_skill(text: str, location: str) -> str:
    return _replace_exactly(
        text,
        WORKFLOW_ANCHOR,
        CONTEXT_BUDGET + WORKFLOW_ANCHOR,
        location,
    )


def _patched_core_scan(text: str, location: str) -> str:
    text = _replace_exactly(
        text,
        STEP_EIGHT_ANCHOR,
        STEP_EIGHT_PREFIX + STEP_EIGHT_ANCHOR,
        location,
    )
    text = _patch_prompt(text, BASELINE_HEADING, BASELINE_FORBID_ANCHOR, location)
    return _patch_prompt(text, FOCUSED_HEADING, FOCUSED_FORBID_ANCHOR, location)


def _tree_entries(root: Path) -> dict[str, TreeEntry]:
    entries: dict[str, TreeEntry] = {}
    pending: list[tuple[Path, str]] = [(root, "")]

    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as scanner:
                children = sorted(scanner, key=lambda item: item.name)
        except OSError as exc:
            raise PatchError(f"cannot read directory {directory}: {exc}") from exc

        for child in children:
            relative = f"{prefix}/{child.name}" if prefix else child.name
            if not prefix and child.name == MARKER_NAME:
                continue

            child_path = Path(child.path)
            if child.is_symlink():
                entries[relative] = (
                    "symlink",
                    os.readlink(child_path).encode("utf-8", "surrogateescape"),
                )
            elif child.is_dir(follow_symlinks=False):
                entries[relative] = ("directory", b"")
                pending.append((child_path, relative))
            elif child.is_file(follow_symlinks=False):
                try:
                    entries[relative] = ("file", child_path.read_bytes())
                except OSError as exc:
                    raise PatchError(f"cannot read file {child_path}: {exc}") from exc
            else:
                raise PatchError(f"unsupported source entry type: {child_path}")

    return entries


def _digest_field(digest: "hashlib._Hash", value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"codex-security-cost-patch-source-v1\0")
    for relative, (kind, content) in sorted(_tree_entries(source).items()):
        _digest_field(digest, kind.encode("ascii"))
        _digest_field(digest, relative.encode("utf-8", "surrogateescape"))
        _digest_field(digest, content)
    return f"sha256:{digest.hexdigest()}"


def _load_source(source: Path) -> SourceState:
    if not source.is_dir():
        raise PatchError(f"source is not a directory: {source}")

    plugin_info_path = source / PLUGIN_INFO
    try:
        plugin_info = json.loads(_read_utf8(plugin_info_path))
    except json.JSONDecodeError as exc:
        raise PatchError(f"invalid plugin metadata {plugin_info_path}: {exc}") from exc

    version = plugin_info.get("version") if isinstance(plugin_info, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise PatchError(f"{plugin_info_path}: missing nonempty string 'version'")

    skill_path = source / SKILL_FILE
    core_scan_path = source / CORE_SCAN_FILE
    for target in (skill_path, core_scan_path):
        if target.is_symlink() or not target.is_file():
            raise PatchError(f"patch target must be a regular source file: {target}")
    patched_skill = _patched_skill(_read_utf8(skill_path), str(skill_path))
    patched_core_scan = _patched_core_scan(
        _read_utf8(core_scan_path), str(core_scan_path)
    )

    return SourceState(
        version=version,
        digest=_source_digest(source),
        patched_files={
            SKILL_FILE.as_posix(): patched_skill.encode("utf-8"),
            CORE_SCAN_FILE.as_posix(): patched_core_scan.encode("utf-8"),
        },
    )


def _expected_manifest(source: Path, state: SourceState) -> dict[str, TreeEntry]:
    expected = _tree_entries(source)
    for relative, content in state.patched_files.items():
        if relative not in expected or expected[relative][0] != "file":
            raise PatchError(f"patched target is not a regular source file: {relative}")
        expected[relative] = ("file", content)
    return expected


def _marker_data(state: SourceState) -> dict[str, object]:
    return {
        "patchVersion": PATCH_VERSION,
        "sourceVersion": state.version,
        "sourceDigest": state.digest,
        "patches": list(PATCHES),
    }


def _read_marker(output: Path) -> dict[str, object]:
    marker_path = output / MARKER_NAME
    if not marker_path.exists():
        raise PatchError(
            f"output {output} is missing {MARKER_NAME}; run without --check to stage it"
        )
    if marker_path.is_symlink() or not marker_path.is_file():
        raise PatchError(f"patch marker is not a regular file: {marker_path}")

    try:
        marker = json.loads(_read_utf8(marker_path))
    except json.JSONDecodeError as exc:
        raise PatchError(f"invalid patch marker {marker_path}: {exc}") from exc
    if not isinstance(marker, dict):
        raise PatchError(f"invalid patch marker {marker_path}: expected a JSON object")
    return marker


def _validate_marker(marker: dict[str, object], state: SourceState, output: Path) -> None:
    expected = _marker_data(state)
    for key in ("patchVersion", "sourceVersion", "sourceDigest", "patches"):
        if key not in marker:
            raise PatchError(f"patch marker for {output} is missing '{key}'")
        if marker[key] != expected[key]:
            raise PatchError(
                f"patch marker for {output} has {key}={marker[key]!r}; "
                f"expected {expected[key]!r}"
            )


def _compare_tree(
    output: Path,
    expected: dict[str, TreeEntry],
    allow_extra: Sequence[str] = (),
    allow_modified: Sequence[str] = (),
) -> None:
    actual = _tree_entries(output)
    for relative in sorted(set(expected) | set(actual)):
        if relative not in actual:
            raise PatchError(f"output is missing source entry: {relative}")
        if relative not in expected:
            if any(fnmatch.fnmatchcase(relative, pattern) for pattern in allow_extra):
                continue
            if actual[relative][0] == "directory" and any(
                child.startswith(f"{relative}/")
                and any(fnmatch.fnmatchcase(child, pattern) for pattern in allow_extra)
                for child in actual
            ):
                continue
            raise PatchError(f"output has an unexpected entry: {relative}")
        if actual[relative] != expected[relative]:
            if any(fnmatch.fnmatchcase(relative, pattern) for pattern in allow_modified):
                continue
            raise PatchError(
                f"output entry does not match source plus patch version "
                f"{PATCH_VERSION}: {relative}"
            )

    marker = output / MARKER_NAME
    if not marker.is_file() or marker.is_symlink():
        raise PatchError(f"output is missing regular patch marker: {marker}")


def validate_output(
    source: Path,
    output: Path,
    state: SourceState,
    allow_extra: Sequence[str] = (),
    allow_modified: Sequence[str] = (),
) -> None:
    if not output.is_dir():
        raise PatchError(f"output is not a directory: {output}")
    marker = _read_marker(output)
    _validate_marker(marker, state, output)
    _compare_tree(
        output,
        _expected_manifest(source, state),
        allow_extra,
        allow_modified,
    )


def _ensure_disjoint(source: Path, output: Path) -> tuple[Path, Path]:
    source_real = source.resolve()
    output_absolute = Path(os.path.abspath(output))
    output_real = output_absolute.resolve(strict=False)
    try:
        common = Path(os.path.commonpath((source_real, output_real)))
    except ValueError:
        pass
    else:
        if common == source_real or common == output_real:
            raise PatchError(
                f"source and output trees must not overlap: source={source_real}, "
                f"output={output_real}"
            )

    if output_absolute.is_symlink():
        raise PatchError(f"output must not be a symbolic link: {output_absolute}")
    return source_real, output_absolute


def _make_staged_tree_writable(staged: Path) -> None:
    directories = [staged]
    directories.extend(path for path in staged.rglob("*") if path.is_dir())
    try:
        for directory in directories:
            if not directory.is_symlink():
                mode = directory.stat().st_mode
                directory.chmod(mode | stat.S_IWUSR | stat.S_IXUSR)
    except OSError as exc:
        raise PatchError(f"cannot make staging tree writable: {exc}") from exc


def _write_staged_tree(source: Path, staged: Path, state: SourceState) -> None:
    try:
        shutil.copytree(source, staged, symlinks=True)
    except OSError as exc:
        raise PatchError(f"cannot copy source {source} to staging tree: {exc}") from exc

    _make_staged_tree_writable(staged)
    for relative, content in state.patched_files.items():
        target = staged / relative
        try:
            mode = target.stat().st_mode
            target.chmod(mode | stat.S_IWUSR)
            target.write_bytes(content)
        except OSError as exc:
            raise PatchError(f"cannot write patched file {target}: {exc}") from exc

    marker = staged / MARKER_NAME
    marker_content = json.dumps(
        _marker_data(state), indent=2, sort_keys=True, ensure_ascii=False
    ) + "\n"
    try:
        marker.write_text(marker_content, encoding="utf-8")
    except OSError as exc:
        raise PatchError(f"cannot write patch marker {marker}: {exc}") from exc


def _publish_tree(staged: Path, output: Path, staging_root: Path) -> None:
    if os.path.lexists(output):
        if output.is_symlink() or not output.is_dir():
            raise PatchError(f"output exists but is not a directory: {output}")
        backup = staging_root / "previous-output"
        try:
            os.replace(output, backup)
        except OSError as exc:
            raise PatchError(f"cannot move previous output aside: {exc}") from exc
    else:
        backup = None

    try:
        os.replace(staged, output)
    except OSError as exc:
        if backup is not None:
            try:
                os.replace(backup, output)
            except OSError as restore_exc:
                raise PatchError(
                    f"cannot publish output ({exc}) or restore previous output "
                    f"({restore_exc}); previous output is at {backup}"
                ) from exc
        raise PatchError(f"cannot atomically publish patched output: {exc}") from exc


def stage_output(source: Path, output: Path, quiet: bool) -> bool:
    source_real, output_real = _ensure_disjoint(source, output)
    state = _load_source(source_real)

    if output_real.is_dir() and not output_real.is_symlink():
        try:
            validate_output(source_real, output_real, state)
        except PatchError as exc:
            if not quiet:
                print(f"restaging {output_real}: {exc}")
        else:
            if not quiet:
                print(f"reused patched output: {output_real}")
            return True
    elif os.path.lexists(output_real):
        raise PatchError(f"output exists but is not a directory: {output_real}")

    output_parent = output_real.parent
    try:
        output_parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(
            tempfile.mkdtemp(prefix=f".{output_real.name}.stage-", dir=output_parent)
        )
    except OSError as exc:
        raise PatchError(f"cannot create staging directory for {output_real}: {exc}") from exc

    try:
        staged = staging_root / "tree"
        _write_staged_tree(source_real, staged, state)
        _publish_tree(staged, output_real, staging_root)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    if not quiet:
        print(f"staged patched output: {output_real}")
    return False


def check_output(
    source: Path,
    output: Path,
    quiet: bool,
    allow_extra: Sequence[str] = (),
    allow_modified: Sequence[str] = (),
) -> None:
    source_real, output_real = _ensure_disjoint(source, output)
    state = _load_source(source_real)
    validate_output(source_real, output_real, state, allow_extra, allow_modified)
    if not quiet:
        print(f"verified patched output: {output_real}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="bundled plugin source")
    parser.add_argument("--output", required=True, type=Path, help="patched staging tree")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify an existing patched output without modifying it",
    )
    parser.add_argument(
        "--allow-extra",
        action="append",
        default=[],
        metavar="GLOB",
        help="allow known local companion files matching a relative-path glob",
    )
    parser.add_argument(
        "--allow-modified",
        action="append",
        default=[],
        metavar="GLOB",
        help="allow known local modifications matching a relative-path glob",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress success messages")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.check:
            check_output(
                args.source,
                args.output,
                args.quiet,
                args.allow_extra,
                args.allow_modified,
            )
        else:
            stage_output(args.source, args.output, args.quiet)
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
