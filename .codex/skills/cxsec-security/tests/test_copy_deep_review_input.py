"""Carried copy_deep_review_input.py, held to upstream's current TypeScript contract.

Ported from openai/codex-security 0.6.0 sdk/typescript/tests-ts/deep-review-input.test.ts.
Omitted: descriptor-lifetime spies on the Node line reader, the packaged-launcher
byte-path case, and the Windows PowerShell/CMD launcher cases; none has a Python
equivalent. Run with: python3 -m pytest tests/
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_ROOT / "assets" / "local-additions" / "scripts" / "copy_deep_review_input.py"
POSIX = sys.platform != "win32"
NEWLINE = "\n" if POSIX else "\r\n"


def candidate(path: str, area: str = "src") -> dict[str, str]:
    return {"path": path, "area": area, "preview": "source preview"}


def dumps(row: Any) -> str:
    return json.dumps(row, separators=(",", ":"))


def write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(dumps(row) + "\n" for row in rows), encoding="utf-8")


def read(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8").strip()
    return [] if text == "" else [json.loads(line) for line in text.splitlines()]


def run(root: Path, rank_input: Path | str, out: Path | str, *args: str, env: dict | None = None):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--rank-input", str(rank_input), "--out", str(out), *args],
        capture_output=True,
        text=True,
        cwd=root,
        env=env,
        errors="surrogateescape",
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


def test_copies_every_candidate_in_order_and_preserves_unicode(root: Path) -> None:
    source, output = root / "input.jsonl", root / "output.jsonl"
    write(source, [candidate("\U00010000.py", "é"), candidate(".py", "\udcff"), candidate("b.py", "api")])
    result = run(root, source, output)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Copied 3 rows into {output}{NEWLINE}"
    assert read(output) == [
        {"path": "\U00010000.py", "area": "é"},
        {"path": ".py", "area": "\udcff"},
        {"path": "b.py", "area": "api"},
    ]


def test_closes_an_empty_worklist_with_an_empty_output(root: Path) -> None:
    source, output = root / "input.jsonl", root / "output.jsonl"
    write(source, [])
    output.write_text("previous contents")
    assert run(root, source, output).returncode == 0
    assert output.read_bytes() == b""


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("terminated", [False, True])
def test_accepts_utf8_rows_with_any_newline(root: Path, separator: str, terminated: bool) -> None:
    source, output = root / "input.jsonl", root / "output.jsonl"
    source.write_bytes(
        (
            dumps(candidate("é.py", "雪"))
            + separator
            + dumps(candidate("b.py"))
            + (separator if terminated else "")
        ).encode()
    )
    assert run(root, source, output).returncode == 0
    assert read(output) == [{"path": "é.py", "area": "雪"}, {"path": "b.py", "area": "src"}]


def test_reads_long_rows_and_crlf_without_adding_or_losing_rows(root: Path) -> None:
    source, output = root / "input.jsonl", root / "output.jsonl"
    prefix = '{"path":"a.py","area":"src","preview":"'
    first = prefix + "x" * (65_535 - len(prefix)) + '雪"}'
    second = dumps(candidate("b.py"))
    padding = 2 * 65_536 - len((first + "\n" + second).encode()) - 1
    source.write_bytes(
        (first + "\n" + second + " " * padding + "\r\n" + dumps(candidate("c.py")) + "\r").encode()
    )
    assert run(root, source, output).returncode == 0
    assert read(output) == [
        {"path": "a.py", "area": "src"},
        {"path": "b.py", "area": "src"},
        {"path": "c.py", "area": "src"},
    ]


INVALID: list[tuple[str | bytes, str]] = [
    ("\n", "blank JSONL rows are not allowed"),
    (dumps(candidate("a.py")) + "\r\n\r", ":2: blank JSONL rows are not allowed"),
    ("\u001c\n", "invalid JSON"),
    ("{}\n\n", "missing fields"),
    ("{not json}\n", "invalid JSON"),
    ("[]\n", "expected a JSON object"),
    (dumps({**candidate("a.py"), "extra": True}), 'unexpected fields ["extra"]'),
    (dumps(candidate("  ")), "path must be a non-empty string"),
    (dumps({**candidate("a.py"), "preview": 1}), "preview must be a string"),
    (dumps({**candidate("a.py"), "area": None}), "area must be a string"),
    ("﻿" + dumps(candidate("a.py")), "invalid JSON"),
    (bytes([0xFF]), "encoded data"),
    ((dumps(candidate("é.py")) + "\r\n").encode() + bytes([0xC3]), "encoded data"),
    (b'{"path":"a.py","area":"src","preview":"' + bytes([0xED, 0xA0, 0x80]) + b'"}', "encoded data"),
]


@pytest.mark.parametrize(("text", "message"), INVALID)
def test_rejects_invalid_worklist_data(root: Path, text: str | bytes, message: str) -> None:
    source, output = root / "input.jsonl", root / "output.jsonl"
    source.write_bytes(text.encode() if isinstance(text, str) else text)
    output.write_text("preserve existing output")
    result = run(root, source, output)
    assert result.returncode == 1
    assert message in result.stderr
    assert output.read_text() == "preserve existing output"


def test_rejects_repeated_paths_before_output_with_escaped_controls(root: Path) -> None:
    source, output = root / "input.jsonl", root / "output.jsonl"
    write(source, [candidate("a-\u007f\u009b.py"), candidate("a-\u007f\u009b.py")])
    result = run(root, source, output)
    assert result.returncode == 1
    assert 'contains duplicate paths: ["a-\\u007f\\u009b.py"]' in result.stderr
    assert not any("\u007f" <= character <= "\u009f" for character in result.stderr)
    assert not output.exists()


def test_accepts_duplicate_keys_and_empty_area_and_preview(root: Path) -> None:
    source, output = root / "input.jsonl", root / "output.jsonl"
    source.write_text('{"path":"unused.py","path":"a.py","area":"","preview":""}')
    assert run(root, source, output).returncode == 0
    assert read(output) == [{"path": "a.py", "area": ""}]


def test_allows_replacing_the_input_after_all_rows_are_read(root: Path) -> None:
    source = root / "input.jsonl"
    write(source, [candidate("b.py"), candidate("a.py")])
    assert run(root, source, source).returncode == 0
    assert read(source) == [{"path": "b.py", "area": "src"}, {"path": "a.py", "area": "src"}]


def test_expands_homes_creates_parents_and_preserves_path_spelling(root: Path) -> None:
    write(root / "input.jsonl", [candidate("a.py")])
    env = {**os.environ, "HOME": str(root), "USERPROFILE": str(root)}
    result = run(root, "~/input.jsonl", "./nested/./output.jsonl", env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Copied 1 rows into {os.path.join('nested', 'output.jsonl')}{NEWLINE}"
    assert len(read(root / "nested" / "output.jsonl")) == 1


@pytest.mark.parametrize("suffix", ["/.", "///", "/./."])
def test_removes_dot_and_empty_components(root: Path, suffix: str) -> None:
    source, output = root / "input.jsonl", root / "output.jsonl"
    write(source, [candidate("a.py")])
    result = run(root, f"{source}{suffix}", f"{output}{suffix}")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Copied 1 rows into {output}{NEWLINE}"
    assert read(output) == [{"path": "a.py", "area": "src"}]


def test_reports_missing_input_and_preserves_argument_parsing_status(root: Path) -> None:
    source, output = root / "input.jsonl", root / "output.jsonl"
    output.write_text("previous output\n")
    inputs = [source, output / "child.jsonl"]
    if POSIX:
        loop = root / "loop-\x1b[2J.jsonl"
        loop.symlink_to(loop)
        inputs.append(loop)
    for missing in inputs:
        result = run(root, missing, output)
        assert result.returncode == 1
        assert result.stderr == f"Rank input missing: {missing}{NEWLINE}"
    assert output.read_text() == "previous output\n"
    for args in (["--unknown"], ["extra"], ["--rank-input"]):
        assert run(root, source, output, *args).returncode == 2, args
    assert "copy-deep-review-input" in run(root, source, output, "--help").stdout


@pytest.mark.skipif(not POSIX, reason="POSIX filenames")
def test_escapes_controls_in_filesystem_errors_but_not_command_text(root: Path) -> None:
    source = root / "input-\x1b[2J.jsonl"
    output = root / "output-\x1b[2J.jsonl"
    missing = run(root, source, output)
    assert missing.returncode == 1
    assert missing.stderr == f"Rank input missing: {source}\n"
    source.write_text("{}\n")
    invalid = run(root, source, output)
    assert invalid.returncode == 1
    assert invalid.stderr.startswith(f"{source}:1: missing fields")
    write(source, [candidate("a.py")])
    output.mkdir()
    collision = run(root, source, output)
    assert collision.returncode == 1
    assert "\\x1b[2J" in collision.stderr or "\\u001b[2J" in collision.stderr
    assert "\x1b" not in collision.stderr
    output.rmdir()
    success = run(root, source, output)
    assert success.returncode == 0
    assert success.stdout == f"Copied 1 rows into {output}\n"


@pytest.mark.skipif(not POSIX, reason="symlinks and modes")
def test_follows_symlinks_and_preserves_existing_output_permissions(root: Path) -> None:
    source, output = root / "input.jsonl", root / "output.jsonl"
    target = root / "target.jsonl"
    target.write_text("existing")
    target.chmod(0o640)
    output.symlink_to(target)
    write(source, [candidate("a.py")])
    link = root / "input-link"
    link.symlink_to(source)
    assert run(root, link, output).returncode == 0
    assert len(read(target)) == 1
    assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_preserves_symlink_sensitive_parent_traversal(root: Path) -> None:
    child = root / "child"
    (child / "nested").mkdir(parents=True)
    (root / "link").symlink_to(child / "nested")
    write(root / "input.jsonl", [candidate("wrong.py")])
    (root / "output.jsonl").write_text("output sentinel")
    write(child / "input.jsonl", [candidate("right.py")])
    result = run(root, "link/../input.jsonl/.", "link/../output.jsonl/.")
    assert result.returncode == 0, result.stderr
    assert read(child / "output.jsonl") == [{"path": "right.py", "area": "src"}]
    assert (root / "output.jsonl").read_text() == "output sentinel"


@pytest.mark.skipif(not POSIX, reason="POSIX path bytes")
def test_preserves_raw_posix_input_and_output_path_bytes(root: Path) -> None:
    source = os.fsencode(root) + b"/in-\xff"
    output = os.fsencode(root) + b"/nested-\xff/out-\xfe"
    with open(source, "w") as handle:
        handle.write(dumps(candidate("a.py")) + "\n")
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), b"--rank-input", source, b"--out", output],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    with open(output, "rb") as handle:
        assert handle.read() == b'{"path":"a.py","area":"src"}\n'
    assert result.stdout == b"Copied 1 rows into " + output + b"\n"
