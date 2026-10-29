#!/usr/bin/env python3
"""Create deep_review_input.jsonl from rank_input.jsonl."""

# Upstream retired generate_rank_input.py's copy-deep-review-input subcommand and
# moved it into its TypeScript MCP launcher (mcp-app/src/helpers/deep-review-input.ts,
# rank-worklists.ts), which this port does not vendor. This carried copy tracks that
# implementation: rows must carry exactly path, area and preview; lines split on CR,
# LF or CRLF; blank rows are errors; output keeps path and area only.

from __future__ import annotations

import argparse
import errno
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

FIELDS = ("path", "area", "preview")
# Errors pathlib.Path.exists() treats as "absent"; upstream reports them as missing.
MISSING_ERRNOS = {errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP}
INVALID_UTF8 = "The encoded data was not valid for encoding utf-8"
LONE_SURROGATE = re.compile("[\ud800-\udfff]")
# String.prototype.trim whitespace, so "blank" matches upstream exactly.
JS_WHITESPACE = (
    "\t\n\v\f\r          "
    "        　﻿"
)


def blank(value: str) -> bool:
    return not value.strip(JS_WHITESPACE)


def code_point_order(values: set[str]) -> list[str]:
    return sorted(values)


def escape_controls(text: str) -> str:
    def escape(character: str) -> str:
        point = ord(character)
        return f"\\u{{{point:x}}}" if point > 0xFFFF else f"\\u{point:04x}"

    return "".join(
        escape(character) if unicodedata.category(character) in ("Cc", "Cf") else character
        for character in text
    )


def diagnostic(value: Any) -> str:
    return escape_controls(stable_json(value))


def read_lines(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except OSError as error:
        if error.errno in MISSING_ERRNOS:
            raise ValueError(f"Rank input missing: {path}") from error
        raise
    lines = re.split(rb"\r\n|\r|\n", data)
    if lines and lines[-1] == b"":
        lines.pop()
    try:
        return [line.decode("utf-8") for line in lines]
    except UnicodeDecodeError as error:
        raise ValueError(INVALID_UTF8) from error


def load_rank_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for number, line in enumerate(read_lines(path), 1):
        prefix = f"{path}:{number}: "
        if blank(line):
            raise ValueError(prefix + "blank JSONL rows are not allowed")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(prefix + escape_controls(f"invalid JSON: {error}")) from error
        if not isinstance(row, dict):
            raise ValueError(prefix + "expected a JSON object")
        details = []
        missing = code_point_order(set(FIELDS) - set(row))
        unexpected = code_point_order(set(row) - set(FIELDS))
        if missing:
            details.append(f"missing fields {diagnostic(missing)}")
        if unexpected:
            details.append(f"unexpected fields {diagnostic(unexpected)}")
        if details:
            raise ValueError(prefix + "; ".join(details))
        for field in FIELDS:
            value = row[field]
            if not isinstance(value, str) or (field == "path" and blank(value)):
                kind = "a non-empty string" if field == "path" else "a string"
                raise ValueError(prefix + f"{field} must be {kind}")
        rows.append(row)
    return rows


def require_unique_paths(rows: list[dict[str, str]], label: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in rows:
        if row["path"] in seen:
            duplicates.add(row["path"])
        seen.add(row["path"])
    if duplicates:
        raise ValueError(
            f"{label} contains duplicate paths: {diagnostic(code_point_order(duplicates))}"
        )


def stable_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    # JSON.stringify escapes lone surrogates; they cannot be written as UTF-8 otherwise.
    return LONE_SURROGATE.sub(lambda match: f"\\u{ord(match[0]):04x}", text)


def write_rows(output: Path, rows: list[dict[str, str]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    newline = "\r\n" if sys.platform == "win32" else "\n"
    with output.open("w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(stable_json({"path": row["path"], "area": row["area"]}) + newline)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="copy-deep-review-input", description=__doc__, allow_abbrev=False
    )
    parser.add_argument("--rank-input", required=True, help="Deterministic rank input JSONL.")
    parser.add_argument("--out", required=True, help="Output deep_review_input.jsonl path.")
    args = parser.parse_args()
    try:
        rows = load_rank_rows(Path(args.rank_input).expanduser())
        require_unique_paths(rows, "Rank input")
        output = Path(args.out).expanduser()
        write_rows(output, rows)
    except ValueError as error:
        print(LONE_SURROGATE.sub(lambda m: f"\\u{ord(m[0]):04x}", str(error)), file=sys.stderr)
        return 1
    except OSError as error:
        print(escape_controls(str(error)), file=sys.stderr)
        return 1
    print(f"Copied {len(rows)} rows into {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
