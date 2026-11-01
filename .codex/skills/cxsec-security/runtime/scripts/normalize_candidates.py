#!/usr/bin/env python3
"""Validate and combine security-scan candidates into deterministic JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

# Upstream ported this helper to TypeScript (mcp-app/src/helpers/normalize-candidates.ts)
# and deleted it. This carried copy tracks that implementation, so string handling
# follows JavaScript semantics wherever Python's differs: ASCII-only \d, the
# String.prototype.trim whitespace set, and UTF-16 code-unit sort order.
CWE = re.compile(r"(?i)CWE-(\d+)", re.ASCII)
LONE_SURROGATE = re.compile("[\ud800-\udfff]")
# Surrogates surrogateescape cannot map back to a byte (only U+DC80-U+DCFF can).
UNENCODABLE_SURROGATE = re.compile("[\ud800-\udc7f\udd00-\udfff]")
JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
WINDOWS = sys.platform == "win32"
ROLES = {
    "entrypoint": 0,
    "entrypoint/wrapper": 1,
    "source": 2,
    "root_control": 3,
    "sink": 4,
    "concrete_implementation": 5,
    "evidence": 6,
}
FIELDS = {
    "candidate_id",
    "cwe_ids",
    "locations",
    "summary",
    "evidence",
    "context",
    "instance",
}


def js_trim(value: str) -> str:
    return value.strip(JS_WHITESPACE)


def utf16_key(value: str) -> bytes:
    # Big-endian UTF-16 bytes compare in code-unit order, which is JavaScript's
    # default string order; surrogatepass keeps surrogateescape'd path bytes sortable.
    return value.encode("utf-16-be", "surrogatepass")


def text_field(row: dict[str, Any], field: str, *, required: bool = True) -> str | None:
    value = row.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not js_trim(value):
        raise ValueError(f"{field}: expected a non-empty string")
    return js_trim(value)


def cwe_ids(row: dict[str, Any]) -> list[str]:
    value = row.get("cwe_ids")
    if not isinstance(value, list):
        raise ValueError("cwe_ids: expected an array")
    found: set[int] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("cwe_ids: expected CWE strings")
        match = CWE.fullmatch(js_trim(item))
        if match is None or int(match[1]) < 1:
            raise ValueError(f"cwe_ids: unsupported value {item!r}")
        found.add(int(match[1]))
    return [f"CWE-{number}" for number in sorted(found)]


def resolve_path(value: str | Path, *, strict: bool = True) -> Path:
    """Canonicalize a path the way upstream's resolvePosixPath does.

    Non-strict resolution appends only genuinely absent trailing components to the
    deepest existing canonical ancestor. Unlike Path.resolve(strict=False) it rejects
    an existing dangling symlink (which would otherwise redirect a write to its
    target) and refuses to cancel an unresolved component with "..". Symlink loops
    surface as OSError, never as Python 3.11's RuntimeError.
    """
    path = Path(value)
    if UNENCODABLE_SURROGATE.search(str(path)) and not WINDOWS:
        raise ValueError("path: UTF-8 cannot encode an unpaired surrogate")
    current = str(path if path.is_absolute() else Path.cwd() / path)
    missing: list[str] = []
    while True:
        try:
            return Path(os.path.realpath(current, strict=True), *reversed(missing))
        except FileNotFoundError:
            if strict or os.path.lexists(current):
                raise
            name = os.path.basename(current)
            if name == "..":
                raise
            missing.append(name)
            current = os.path.dirname(current)


def relative_file(value: Any, repo_root: Path) -> tuple[str, Path]:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("path: expected a non-empty repository-relative path")
    raw = value
    if WINDOWS:
        raw = raw.replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or ".." in path.parts
        or (WINDOWS and re.match(r"^[A-Za-z]:", raw))
    ):
        raise ValueError("path: expected a repository-relative path without traversal")
    resolved = resolve_path(repo_root / raw)
    try:
        relative = resolved.relative_to(repo_root).as_posix()
    except ValueError as error:
        raise ValueError("path: must resolve inside --repo-root") from error
    if not resolved.is_file():
        raise ValueError("path: expected a regular file")
    return relative, resolved


def positive_line(value: Any, field: str) -> int:
    # JSON has one number type: 2.0 is the integer 2, as Number.isInteger agrees.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field}: expected a positive integer")
    return value


def normalize_locations(
    row: dict[str, Any], repo_root: Path, line_counts: dict[Path, int]
) -> list[dict[str, Any]]:
    value = row.get("locations")
    if not isinstance(value, list) or not value:
        raise ValueError("locations: expected a non-empty array")
    normalized: dict[tuple[str, int, int, str], dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("locations: expected location objects")
        unknown = set(item) - {"path", "start_line", "end_line", "role"}
        if unknown:
            raise ValueError(f"locations: unsupported fields {', '.join(sorted(unknown))}")
        relative, source = relative_file(item.get("path"), repo_root)
        if (
            not js_trim(relative)
            or "\\" in relative
            or any(":" in part for part in PurePosixPath(relative).parts)
        ):
            raise ValueError("path: expected a safe repository-relative POSIX path")
        start = positive_line(item.get("start_line"), "start_line")
        end = positive_line(item.get("end_line", start), "end_line")
        if end < start:
            raise ValueError("end_line: must be greater than or equal to start_line")
        if source not in line_counts:
            line_counts[source] = len(source.read_bytes().splitlines())
        if end > line_counts[source]:
            raise ValueError(f"line range {start}-{end} exceeds {relative}:{line_counts[source]}")
        role = item.get("role")
        if not isinstance(role, str) or role not in ROLES:
            raise ValueError(f"role: unsupported value {role!r}")
        key = (relative, start, end, role)
        normalized[key] = {
            "path": relative,
            "start_line": start,
            "end_line": end,
            "role": role,
        }
    return sorted(
        normalized.values(),
        key=lambda item: (
            ROLES[item["role"]],
            utf16_key(item["path"]),
            item["start_line"],
            item["end_line"],
        ),
    )


def read_scope(path: Path, repo_root: Path, *, allow_missing: bool = False) -> set[str]:
    raw = path.read_bytes()
    # POSIX inventories may list raw, non-UTF-8 path bytes; surrogateescape keeps them
    # round-trippable through pathlib (upstream's decodePosixBytes does the same).
    contents = raw.decode("utf-8") if WINDOWS else raw.decode("utf-8", "surrogateescape")
    lines = contents.split("\n")
    listed_rows = set(lines)

    def is_scope_file(value: str) -> bool:
        try:
            relative_file(value, repo_root)
        except (OSError, ValueError):
            return False
        return True

    carriage_rows = {
        line: (is_scope_file(line), is_scope_file(line.removesuffix("\r")))
        for line in lines
        if line.endswith("\r") and line != "\r" and not WINDOWS
    }
    # The final row has no terminating newline, so a trailing "\r" there is part of the
    # name, never a line ending: it neither counts as CRLF evidence nor gets stripped.
    crlf_evidence = any(
        line == "\r" or (line in carriage_rows and carriage_rows[line][1] and not carriage_rows[line][0])
        for line in lines[:-1]
    )
    literal_evidence = any(literal and not stripped for literal, stripped in carriage_rows.values())

    scope: set[str] = set()
    for number, line in enumerate(lines, 1):
        final_literal = number == len(lines)
        if not final_literal and (WINDOWS or line == "\r"):
            line = line.removesuffix("\r")
        elif not final_literal and line.endswith("\r"):
            literal, stripped = carriage_rows[line]
            trimmed = line.removesuffix("\r")
            # With deleted paths allowed, a missing stripped name may be a deleted file
            # rather than a CRLF artifact, so the literal match alone cannot decide.
            if allow_missing and literal and not stripped and trimmed not in listed_rows:
                raise ValueError(f"in-scope file row {number}: ambiguous carriage-return paths")
            if stripped and not literal:
                line = trimmed
            elif stripped and literal:
                if trimmed not in listed_rows:
                    if crlf_evidence and not literal_evidence:
                        line = trimmed
                    elif not literal_evidence or crlf_evidence:
                        raise ValueError(
                            f"in-scope file row {number}: ambiguous carriage-return paths"
                        )
            elif not literal and crlf_evidence:
                line = trimmed
        if not line:
            continue
        try:
            relative, _ = relative_file(line, repo_root)
        except (OSError, ValueError) as error:
            if allow_missing and isinstance(error, FileNotFoundError):
                candidate = PurePosixPath(line)
                if candidate.is_absolute() or ".." in candidate.parts or "\0" in line:
                    raise ValueError(f"in-scope file row {number}: unsafe deleted path") from error
                try:
                    resolved = resolve_path(repo_root / line, strict=False)
                    relative = resolved.relative_to(repo_root).as_posix()
                except (OSError, ValueError) as escaped:
                    raise ValueError(
                        f"in-scope file row {number}: path escapes repository"
                    ) from escaped
                scope.add(relative)
                continue
            raise ValueError(f"in-scope file row {number}: {error}") from error
        scope.add(relative)
    return scope


def normalize_candidate(
    row: dict[str, Any], repo_root: Path, scope: set[str], line_counts: dict[Path, int]
) -> dict[str, Any]:
    unknown = set(row) - FIELDS
    if unknown:
        raise ValueError(f"unsupported fields {', '.join(sorted(unknown))}")
    if "candidate_id" in row:
        text_field(row, "candidate_id")
    candidate_locations = normalize_locations(row, repo_root, line_counts)
    if not any(item["path"] in scope for item in candidate_locations):
        raise ValueError("locations: expected at least one in-scope file")
    result: dict[str, Any] = {
        "cwe_ids": cwe_ids(row),
        "locations": candidate_locations,
        "summary": text_field(row, "summary"),
        "evidence": text_field(row, "evidence"),
    }
    context = text_field(row, "context", required=False)
    if context is not None:
        result["context"] = context
    instance = text_field(row, "instance", required=False)
    if instance is not None:
        result["instance"] = instance
    return result


def stable_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    # Match upstream's JSON.stringify, which escapes lone surrogates. They arise from
    # surrogateescape-decoded path bytes and cannot be written as UTF-8 otherwise.
    return LONE_SURROGATE.sub(lambda match: f"\\u{ord(match[0]):04x}", text)


def identity(row: dict[str, Any]) -> str:
    return stable_json(
        {
            "cwe_ids": row["cwe_ids"],
            "locations": row["locations"],
            "instance": row.get("instance"),
        }
    )


def merged_text(group: list[dict[str, Any]], field: str) -> str:
    return "\n".join(sorted({item[field] for item in group if field in item}, key=utf16_key))


def combine(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(identity(row), []).append(row)
    combined: list[dict[str, Any]] = []
    for key, group in sorted(groups.items(), key=lambda entry: utf16_key(entry[0])):
        candidate_id = hashlib.sha256(key.encode()).hexdigest()[:16]

        result = {
            "candidate_id": f"candidate-{candidate_id}",
            "cwe_ids": group[0]["cwe_ids"],
            "locations": group[0]["locations"],
            "summary": merged_text(group, "summary"),
            "evidence": merged_text(group, "evidence"),
        }
        context = merged_text(group, "context")
        if context:
            result["context"] = context
        if "instance" in group[0]:
            result["instance"] = group[0]["instance"]
        combined.append(result)
    return combined


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--input", nargs="+", required=True, help="Candidate JSONL inputs.")
    parser.add_argument("--out", required=True, help="Combined candidate JSONL output.")
    parser.add_argument("--repo-root", required=True, help="Repository root for candidate paths.")
    parser.add_argument("--in-scope-files", required=True, help="Repository-relative file list.")
    parser.add_argument(
        "--allow-missing-in-scope",
        action="store_true",
        help="Keep deleted Git paths in a diff inventory while validating existing candidate files.",
    )
    args = parser.parse_args()
    try:
        repo_root = resolve_path(Path(args.repo_root).expanduser())
        if not repo_root.is_dir():
            raise ValueError("--repo-root: expected a directory")
        output = resolve_path(Path(args.out).expanduser(), strict=False)
        scope_path = resolve_path(Path(args.in_scope_files).expanduser())
        inputs = sorted(
            {resolve_path(Path(value).expanduser()) for value in args.input},
            key=lambda path: utf16_key(str(path)),
        )
        if output in inputs:
            raise ValueError("--out: must not also be an input")
        if output == scope_path:
            raise ValueError("--out: must not replace --in-scope-files")
        scope = read_scope(scope_path, repo_root, allow_missing=args.allow_missing_in_scope)
        line_counts: dict[Path, int] = {}
        rows: list[dict[str, Any]] = []
        for source in inputs:
            # Split on "\n" only, like upstream: a bare "\r" is not a row separator.
            for number, line in enumerate(source.read_bytes().decode("utf-8").split("\n"), 1):
                if not js_trim(line):
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("expected a JSON object")
                    rows.append(normalize_candidate(row, repo_root, scope, line_counts))
                except (ValueError, TypeError, OSError) as error:
                    raise ValueError(f"{source} row {number}: {error}") from error
        combined = combine(rows)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output.parent,
                # Fixed short prefix: a long output basename plus a random suffix
                # would exceed NAME_MAX.
                prefix=".candidates-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                for row in combined:
                    handle.write(stable_json(row) + "\n")
            temporary.replace(output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(f"Combined {len(rows)} candidate rows into {len(combined)} rows in {output}")
    except (OSError, ValueError) as error:
        print(f"normalize_candidates: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
