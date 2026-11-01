#!/usr/bin/env python3
"""Concatenate the SECURITY.md files that apply to a scan path."""

from __future__ import annotations

import errno
import json
import os
import stat
import sys

MAX_SECURITY_MD_BYTES = 1024 * 1024
MISSING_ERRNOS = (errno.ENOENT, errno.ENOTDIR)
USAGE = (
    "Concatenate the SECURITY.md files that apply to a scan path.\n\n"
    "Usage: resolve_security_md.py --repo PATH [--list | --scope PATH] [--out PATH]\n\n"
    "--out PATH  output path, or - for stdout (default: -)"
)
STRING_OPTIONS = ("repo", "scope", "out")
BOOLEAN_OPTIONS = ("list", "help")


class ResolutionError(ValueError):
    """Raised when a SECURITY.md chain cannot be resolved."""


def _inside(path: str, root: str, label: str) -> str:
    if path == root:
        return ""
    prefix = root if root.endswith(os.sep) else root + os.sep
    if path.startswith(prefix):
        return path[len(prefix) :]
    raise ResolutionError(f"{label} is outside the scan root: {path}")


def _expand_home(path: str) -> str:
    if not path.startswith("~"):
        return path
    # os.path.expanduser returns the path unchanged when it cannot find the home.
    expanded = os.path.expanduser(path)
    if expanded.startswith("~"):
        raise ResolutionError("Could not determine home directory.")
    return expanded


def _resolve(path: str) -> str:
    # os.stat applies realpath(3)'s rules (ENOTDIR for "file/", ELOOP for link
    # cycles) that os.path.realpath and pathlib skip or report as RuntimeError.
    os.stat(path)
    return os.path.realpath(path)


def _resolve_root(repo: str) -> str:
    try:
        root = _resolve(_expand_home(repo) or ".")
    except OSError as exc:
        raise ResolutionError(f"scan root does not exist: {repo}") from exc
    if not os.path.isdir(root):
        raise ResolutionError(f"scan root is not a directory: {root}")
    return root


def _file_stat(path: str) -> os.stat_result | None:
    try:
        return os.stat(path)
    except OSError as exc:
        if exc.errno in (*MISSING_ERRNOS, errno.ELOOP):
            return None
        raise


def _utf16_order(value: str) -> bytes:
    # Upstream sorts JavaScript strings, which compare by UTF-16 code unit.
    return value.encode("utf-16-be", "surrogatepass")


def list_security_md(repo: str) -> list[str]:
    """Return a stable, safely framed inventory without traversing Git metadata."""
    root = _resolve_root(repo)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    policies: list[str] = []

    def walk(directory: str, prefix: str) -> None:
        with os.scandir(directory) as iterator:
            entries = list(iterator)
        for listed in entries:
            name = listed.name
            if name == ".git":
                continue
            path = os.path.join(directory, name)
            source = f"{prefix}/{name}" if prefix else name

            # Entry types come from the directory listing; only an unknown type
            # costs an lstat, and an entry that vanished meanwhile is skipped.
            try:
                listed_directory = listed.is_dir(follow_symlinks=False)
            except OSError as exc:
                if exc.errno in MISSING_ERRNOS:
                    continue
                raise
            if not listed_directory and name != "SECURITY.md":
                continue

            try:
                mode_stat = os.lstat(path)
            except OSError as exc:
                if listed_directory or exc.errno not in MISSING_ERRNOS:
                    raise
                continue
            mode = mode_stat.st_mode
            if listed_directory and not stat.S_ISDIR(mode):
                continue

            if stat.S_ISDIR(mode):
                if not getattr(mode_stat, "st_file_attributes", 0) & reparse_point:
                    walk(path, source)
                continue
            if name != "SECURITY.md" or not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                continue
            # Directory links named SECURITY.md are not policies; broken links are.
            if stat.S_ISLNK(mode):
                target = _file_stat(path)
                if target is not None and stat.S_ISDIR(target.st_mode):
                    continue
            policies.append(source)

    walk(root, "")
    return sorted(policies, key=_utf16_order)


def _read_policy(path: str, displayed: str) -> str:
    with open(path, "rb") as policy_file:
        policy_bytes = policy_file.read(MAX_SECURITY_MD_BYTES + 1)
    if len(policy_bytes) > MAX_SECURITY_MD_BYTES:
        raise ResolutionError(f"SECURITY.md exceeds 1 MiB: {displayed}")
    try:
        return policy_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResolutionError(f"SECURITY.md is not valid UTF-8: {displayed}") from exc


def resolve_security_md(repo: str, scope: str) -> str:
    """Return applicable SECURITY.md files, concatenated root to leaf."""
    root = _resolve_root(repo)

    # os.path.join keeps an absolute scope as-is and keeps trailing separators.
    requested_scope = os.path.join(root, _expand_home(scope) or ".")
    try:
        resolved_scope = _resolve(requested_scope)
    except OSError as exc:
        raise ResolutionError(f"scan scope does not exist: {requested_scope}") from exc
    _inside(resolved_scope, root, "scan scope")

    target_directory = (
        resolved_scope if os.path.isdir(resolved_scope) else os.path.dirname(resolved_scope)
    )
    relative_directory = _inside(target_directory, root, "scan scope")
    directories = [root]
    current = root
    for part in relative_directory.split(os.sep) if relative_directory else []:
        current = os.path.join(current, part)
        directories.append(current)

    sections: list[str] = []
    for directory in directories:
        policy = os.path.join(directory, "SECURITY.md")
        policy_stat = _file_stat(policy)
        if policy_stat is None or not stat.S_ISREG(policy_stat.st_mode):
            continue
        resolved_policy = os.path.realpath(policy)
        _inside(resolved_policy, root, "SECURITY.md")
        content = _read_policy(resolved_policy, policy)
        if not content.strip():
            continue

        source = _inside(policy, root, "SECURITY.md").replace(os.sep, "/")
        section = f"## SECURITY.md source: {json.dumps(source)}\n\n{content}"
        if not section.endswith("\n"):
            section += "\n"
        sections.append(section)

    return "\n".join(sections)


def parse_args(argv: list[str]) -> dict[str, str | bool]:
    """Parse strictly, like upstream's node:util parseArgs.

    Long options must be spelled in full, booleans take no value, positionals are
    rejected, and a dash-prefixed value needs the --option=value form.
    """
    values: dict[str, str | bool] = {"out": "-"}
    index = 0
    while index < len(argv):
        argument = argv[index]
        index += 1
        if argument == "-h":
            values["help"] = True
            continue
        if argument == "--":
            if index < len(argv):
                raise ResolutionError(f"Unexpected argument '{argv[index]}'")
            break
        if not argument.startswith("--"):
            raise ResolutionError(f"Unexpected argument '{argument}'")

        name, has_value, value = argument[2:].partition("=")
        if name in BOOLEAN_OPTIONS:
            if has_value:
                raise ResolutionError(f"Option '--{name}' does not take an argument")
            values[name] = True
            continue
        if name not in STRING_OPTIONS:
            raise ResolutionError(f"Unknown option '--{name}'")
        if not has_value:
            if index == len(argv):
                raise ResolutionError(f"Option '--{name} <value>' argument missing")
            value = argv[index]
            index += 1
            if len(value) > 1 and value.startswith("-"):
                raise ResolutionError(
                    f"Option '--{name}' argument is ambiguous; "
                    f"use '--{name}=-XYZ' for a value starting with a dash"
                )
        values[name] = value
    return values


def main() -> int:
    try:
        args = parse_args(sys.argv[1:])
        if args.get("help"):
            print(USAGE)
            return 0
        if "repo" not in args:
            raise ResolutionError("--repo is required")
        if args.get("list") and "scope" in args:
            raise ResolutionError("--list cannot be combined with --scope")
        if not args.get("list") and "scope" not in args:
            raise ResolutionError("--scope is required unless --list is specified")

        guidance = (
            json.dumps(list_security_md(args["repo"]), ensure_ascii=True) + "\n"
            if args.get("list")
            else resolve_security_md(args["repo"], args["scope"])
        )
        # --out keeps tildes literal; only the exact string "-" means stdout.
        output = args["out"] or "."
        if output == "-":
            sys.stdout.buffer.write(guidance.encode("utf-8"))
        else:
            os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
            with open(output, "w", encoding="utf-8") as output_file:
                output_file.write(guidance)
    except (OSError, ResolutionError) as exc:
        print(f"resolve_security_md.py: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
