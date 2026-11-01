#!/usr/bin/env python3
"""Copy SDK scoped-path targets into the unsealed manifest and coverage documents."""

# Upstream retired generate_rank_input.py's bind-repo-scopes subcommand and moved it
# into its TypeScript MCP launcher (mcp-app/src/helpers/bind-repo-scopes.ts), which
# this port does not vendor. This carried copy tracks that implementation: both
# documents are read and checked before either is written, unrelated JSON values
# (including exact large integers) are preserved, and output is two-space JSON.

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

LONE_SURROGATE = re.compile("[\ud800-\udfff]")
BIND_FAILED = "Unable to bind requested scopes into the scan contract"


def escape_controls(text: str) -> str:
    def escape(character: str) -> str:
        point = ord(character)
        return f"\\u{{{point:x}}}" if point > 0xFFFF else f"\\u{point:04x}"

    return "".join(
        escape(character) if unicodedata.category(character) in ("Cc", "Cf") else character
        for character in text
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes().decode("utf-8"))


def stringify(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    # JSON.stringify escapes lone surrogates; they cannot be written as UTF-8 otherwise.
    return LONE_SURROGATE.sub(lambda match: f"\\u{ord(match[0]):04x}", text)


def load_scopes(path: Path) -> list[str]:
    try:
        scopes = read_json(path)
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError(f"Unable to read scopes file: {path}") from error
    if (
        not isinstance(scopes, list)
        or not scopes
        or any(not isinstance(scope, str) or not scope for scope in scopes)
    ):
        raise ValueError(f"Scopes file must contain a non-empty JSON string array: {path}")
    return scopes


def load_contract(manifest_path: Path, coverage_path: Path) -> tuple[dict, dict]:
    try:
        manifest = read_json(manifest_path)
        coverage = read_json(coverage_path)
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError(BIND_FAILED) from error
    scan = manifest.get("scan") if isinstance(manifest, dict) else None
    if (
        not isinstance(coverage, dict)
        or not isinstance(scan, dict)
        or not isinstance(scan.get("scope"), dict)
    ):
        raise ValueError(BIND_FAILED)
    return manifest, coverage


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="bind-repo-scopes", description=__doc__, allow_abbrev=False
    )
    parser.add_argument("--scopes-file", required=True, metavar="PATH")
    parser.add_argument("--manifest", required=True, metavar="PATH")
    parser.add_argument("--coverage", required=True, metavar="PATH")
    args = parser.parse_args()
    try:
        scopes = load_scopes(Path(args.scopes_file).expanduser())
        manifest_path = Path(args.manifest).expanduser()
        coverage_path = Path(args.coverage).expanduser()
        manifest, coverage = load_contract(manifest_path, coverage_path)
        manifest["scan"]["scope"]["includePaths"] = scopes
        coverage["includePaths"] = scopes
        for path, value in ((manifest_path, manifest), (coverage_path, coverage)):
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(stringify(value) + "\n")
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        name = escape_controls(str(error.filename)) if error.filename is not None else ""
        print(f"[Errno {error.errno}] {error.strerror}: {name}".rstrip(": "), file=sys.stderr)
        return 1
    print(f"Bound {len(scopes)} requested scopes into the scan contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
