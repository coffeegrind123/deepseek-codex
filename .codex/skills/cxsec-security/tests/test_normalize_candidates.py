"""Carried normalize_candidates.py, held to upstream's current TypeScript contract.

Ported from openai/codex-security 0.6.0:
  sdk/typescript/tests-ts/candidate-normalizer.test.ts
  plugins/codex-security/mcp-app/tests/test_normalize_candidates.ts
Launcher- and Node-specific cases (packaged launcher argv, Node 20 decoder) have no
Python equivalent and are omitted. Run with: python3 -m pytest tests/
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
SCRIPT = SKILL_ROOT / "assets" / "local-additions" / "scripts" / "normalize_candidates.py"
POSIX = sys.platform != "win32"

Row = dict[str, Any]


def location(path: str = "app/routes.py", start_line: int = 2, role: str = "entrypoint") -> Row:
    return {"path": path, "start_line": start_line, "role": role}


def candidate(locations: list[Row] | None = None, **extra: Any) -> Row:
    return {
        "cwe_ids": ["CWE-89"],
        "locations": [location()] if locations is None else locations,
        "summary": "Request input reaches an unsafe operation",
        "evidence": "The input reaches the operation without a check",
        **extra,
    }


def write(path: Path | bytes, data: str | bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data.encode() if isinstance(data, str) else data)


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "İrepository"
        for path in ("app/routes.py", "app/query.py", "app/export.py", "helpers/shared.py"):
            write(self.repo / path, "one\ntwo\nthree\nfour\nfive\n")
        self.scope = root / "in-scope.txt"
        write(self.scope, "app/routes.py\napp/query.py\napp/export.py\n")
        self.output = root / "combined.jsonl"

    def invoke(
        self,
        inputs: list[Path],
        extra: list[str] | None = None,
        *,
        output: Path | str | None = None,
        repo: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                "--input",
                *map(str, inputs),
                "--out",
                str(self.output if output is None else output),
                "--repo-root",
                str(self.repo if repo is None else repo),
                "--in-scope-files",
                str(self.scope),
                *(extra or []),
            ],
            capture_output=True,
            text=True,
            cwd=self.root,
            errors="surrogateescape",
        )

    def run(self, groups: list[list[Row]], extra: list[str] | None = None, **kwargs: Any):
        inputs = []
        for index, rows in enumerate(groups):
            path = self.root / f"candidates-{index}.jsonl"
            write(path, "\n".join(json.dumps(row) for row in rows) + "\n")
            inputs.append(path)
        return self.invoke(inputs, extra, **kwargs)

    def ledger(self, output: Path | None = None) -> list[Row]:
        text = (self.output if output is None else output).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line]


@pytest.fixture
def f(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_combines_canonical_code_paths_and_preserves_separate_instances(f: Fixture) -> None:
    locations = [
        location("app/query.py", 4, "sink"),
        location("./app/routes.py"),
        location("app/query.py", 3, "root_control"),
    ]
    first = candidate(
        locations,
        cwe_ids=["cwe-089", "CWE-89"],
        summary="Z: request reaches SQL\nA: query runs",
        evidence="First trace",
        context="First review",
    )
    second = candidate(
        [*reversed(locations), location("app/query.py", 4, "sink")],
        summary="A second review",
        evidence="Second trace",
        context="Second review",
    )
    result = f.run([[first], [second, candidate(locations, instance="sort parameter")]])
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Combined 3 candidate rows into 2 rows in {f.output}\n"
    rows = f.ledger()
    assert len(rows) == 2
    merged = next(row for row in rows if "instance" not in row)
    assert merged["cwe_ids"] == ["CWE-89"]
    assert merged["locations"] == [
        {"path": "app/routes.py", "start_line": 2, "end_line": 2, "role": "entrypoint"},
        {"path": "app/query.py", "start_line": 3, "end_line": 3, "role": "root_control"},
        {"path": "app/query.py", "start_line": 4, "end_line": 4, "role": "sink"},
    ]
    assert merged["summary"] == "A second review\nZ: request reaches SQL\nA: query runs"
    assert merged["evidence"] == "First trace\nSecond trace"
    assert merged["context"] == "First review\nSecond review"
    assert any(row.get("instance") == "sort parameter" for row in rows)
    if POSIX:
        assert f.output.stat().st_mode & 0o777 == 0o600


def test_byte_stable_across_input_order_duplicate_aliases_and_recombination(f: Fixture) -> None:
    first = candidate([location(), location("app/query.py", 4, "sink")], evidence="First trace")
    second = candidate(list(reversed(first["locations"])), evidence="Second trace")
    separate = candidate(
        [location("app/export.py", 2), location("app/export.py", 4, "sink")], cwe_ids=["CWE-22"]
    )
    assert f.run([[first, separate], [second]]).returncode == 0
    expected = f.output.read_bytes()
    assert f.run([[second], [separate, first]]).returncode == 0
    assert f.output.read_bytes() == expected
    previous = f.root / "previous.jsonl"
    write(previous, expected)
    assert f.invoke([previous, previous]).returncode == 0
    assert f.output.read_bytes() == expected
    if POSIX:
        alias = f.root / "input-alias.jsonl"
        alias.symlink_to(previous)
        result = f.invoke([previous, alias])
        assert result.returncode == 0, result.stderr
        assert "Combined 2 candidate rows into 2 rows" in result.stdout
        assert f.output.read_bytes() == expected


def test_keeps_distinct_paths_and_allows_supporting_files_outside_scope(f: Fixture) -> None:
    shared = location("helpers/shared.py", 3, "root_control")
    rows = [
        candidate([location(), shared, location("app/query.py", 4, "sink")], cwe_ids=[]),
        candidate([location(), shared, location("app/export.py", 4, "sink")], cwe_ids=[]),
        candidate(
            [location("app/export.py", 2), shared, location("app/query.py", 4, "sink")], cwe_ids=[]
        ),
    ]
    assert f.run([rows]).returncode == 0
    assert len(f.ledger()) == 3
    assert all(row["cwe_ids"] == [] for row in f.ledger())
    rejected = f.run([[candidate([shared])]])
    assert rejected.returncode == 2
    assert "expected at least one in-scope file" in rejected.stderr


def test_normalizes_unicode_deterministically_with_native_string_order(f: Fixture) -> None:
    # JavaScript orders by UTF-16 code unit: U+10000 (a surrogate pair) sorts before
    # U+E000, the reverse of code-point order.
    names = ["app/\U00010000.py", "app/.py"]
    for name in names:
        write(f.repo / name, "line\n")
    write(f.scope, "\n".join(names) + "\n")
    row = candidate(
        [location(name, 1, "evidence") for name in names],
        cwe_ids=["CWE-002", "cwe-0089", "CWE-89", "CWE-9007199254740993"],
        summary=" ﻿Summary ",
        evidence="\U00010000",
    )
    result = f.run([[row, {**row, "evidence": ""}]])
    assert result.returncode == 0, result.stderr
    value = f.ledger()[0]
    assert value["cwe_ids"] == ["CWE-2", "CWE-89", "CWE-9007199254740993"]
    assert [item["path"] for item in value["locations"]] == names
    assert value["summary"] == "Summary"
    assert value["evidence"] == "\U00010000\n"
    reordered = {**row, "locations": list(reversed(row["locations"]))}
    assert f.run([[{**reordered, "evidence": ""}, reordered]]).returncode == 0
    assert f.ledger() == [value]
    assert f.output.read_text(encoding="utf-8").startswith('{"candidate_id":')


def test_rejects_non_ascii_cwe_digits(f: Fixture) -> None:
    result = f.run([[candidate(cwe_ids=["CWE-٨٩"])]])
    assert result.returncode == 2
    assert "cwe_ids: unsupported value" in result.stderr


@pytest.mark.parametrize(
    ("contents", "count"),
    [
        (b"a\rb\r\nc\n", 3),
        ("a\vb\fc\u0085d e".encode(), 1),
        (b"unterminated", 1),
        (b"", 0),
        (bytes([0xFF, 0x0A, 0xFE]), 2),
    ],
)
def test_counts_source_lines_as_bytes_separated_by_cr_lf_or_crlf(
    f: Fixture, contents: bytes, count: int
) -> None:
    write(f.repo / "app/routes.py", contents)
    if count > 0:
        assert f.run([[candidate([location("app/routes.py", count)])]]).returncode == 0
    result = f.run([[candidate([location("app/routes.py", count + 1)])]])
    assert result.returncode == 2
    assert f"exceeds app/routes.py:{count}" in result.stderr


def test_accepts_integral_json_numbers_as_lines(f: Fixture) -> None:
    raw = f.root / "raw.jsonl"
    write(raw, json.dumps(candidate([location("app/routes.py", 1)])).replace('"start_line": 1', '"start_line": 2.0') + "\n")
    result = f.invoke([raw])
    assert result.returncode == 0, result.stderr
    assert f.ledger()[0]["locations"][0] == {
        "path": "app/routes.py",
        "start_line": 2,
        "end_line": 2,
        "role": "entrypoint",
    }


def test_rejects_invalid_lines_malformed_rows_and_invalid_utf8_atomically(f: Fixture) -> None:
    raw = f.root / "raw.jsonl"
    write(f.output, "previous output\n")
    for number in ("1.5", "true", "0", "-1", "NaN", "Infinity", "1e9999", '"1"'):
        write(
            raw,
            json.dumps(candidate([location("app/routes.py", 1)])).replace(
                '"start_line": 1', f'"start_line": {number}'
            )
            + "\n",
        )
        result = f.invoke([raw])
        assert result.returncode == 2, number
        assert "row 1:" in result.stderr
        assert f.output.read_text() == "previous output\n"
    prefix = json.dumps(candidate(cwe_ids=[], summary="s", evidence=""))[:-2].encode()
    invalid_long = prefix + b"a" * (191 - len(prefix)) + bytes([0xFF]) + b'"}'
    for invalid in (invalid_long, "not-json", "[]", "null", '{"locations":', "﻿{}", bytes([0xFF])):
        write(raw, invalid)
        result = f.invoke([raw])
        assert result.returncode == 2, invalid
        assert f.output.read_text() == "previous output\n"
    write(raw, json.dumps(candidate()) + "\r\n\nnot-json\n")
    assert "raw.jsonl row 3:" in f.invoke([raw]).stderr
    assert f.output.read_text() == "previous output\n"
    assert not any(name.endswith(".tmp") for name in os.listdir(f.root))


def test_reports_invalid_fields_with_input_row_and_preserves_output(f: Fixture) -> None:
    cases: list[tuple[Row, str]] = [
        (candidate(cwe_ids=None), "cwe_ids: expected an array"),
        (candidate(cwe_ids=[12]), "cwe_ids: expected CWE strings"),
        (candidate(cwe_ids=["SQL injection"]), "cwe_ids: unsupported value"),
        (candidate(cwe_ids=["CWE-0"]), "cwe_ids: unsupported value"),
        (candidate([]), "locations: expected a non-empty array"),
        (candidate([None]), "locations: expected location objects"),  # type: ignore[list-item]
        (candidate([location("app/missing.py")]), "missing.py"),
        (candidate([location("../outside.py")]), "repository-relative path without traversal"),
        (candidate([location(str(f.repo))]), "repository-relative path without traversal"),
        (candidate([location("app")]), "path: expected a regular file"),
        (candidate([location("app/routes.py", 6)]), "line range 6-6 exceeds app/routes.py:5"),
        (candidate([{**location(), "end_line": 1}]), "greater than or equal"),
        (candidate([location("app/routes.py", 1, "rootControl")]), "role: unsupported value"),
        (candidate([{**location(), "line": 2}]), "locations: unsupported fields line"),
        (candidate(technically_validated=True), "unsupported fields technically_validated"),
        (candidate(disposition="reportable"), "unsupported fields disposition"),
        (candidate(candidate_id=" "), "candidate_id: expected a non-empty string"),
        (candidate(summary=" \t\r\n"), "summary: expected a non-empty string"),
        (candidate(evidence=None), "evidence: expected a non-empty string"),
        (candidate(context=" "), "context: expected a non-empty string"),
        (candidate(instance=False), "instance: expected a non-empty string"),
    ]
    write(f.output, "previous output\n")
    for row, message in cases:
        result = f.run([[candidate(), row]])
        assert result.returncode == 2, message
        assert "candidates-0.jsonl row 2:" in result.stderr
        assert message in result.stderr
        assert f.output.read_text() == "previous output\n"


@pytest.mark.skipif(not POSIX, reason="POSIX path bytes")
def test_rejects_unsupported_surrogate_paths_before_sibling_lookup(f: Fixture) -> None:
    write(f.repo / "�.py", "replacement sibling\n")
    write(f.scope, "�.py\n")
    write(f.output, "previous output\n")
    for name in ("\ud800.py", "\udc00.py"):
        result = f.run([[candidate([location(name, 1)])]])
        assert result.returncode == 2
        assert "unpaired surrogate" in result.stderr
        assert f.output.read_text() == "previous output\n"


def test_writes_long_output_basenames_through_a_short_temporary_name(f: Fixture) -> None:
    output = f.root / f"{'x' * 249}.jsonl"
    result = f.run([[candidate()]], output=output)
    assert result.returncode == 0, result.stderr
    assert len(f.ledger(output)) == 1
    assert not any(name.endswith(".tmp") for name in os.listdir(f.root))


@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_rejects_scope_symlink_loops_without_replacing_output(f: Fixture) -> None:
    loop = f.repo / "app/loop.py"
    loop.symlink_to(loop)
    write(f.scope, "app/loop.py\n")
    write(f.output, "previous output\n")
    result = f.run([[candidate()]])
    assert result.returncode == 2
    assert "in-scope file row 1:" in result.stderr
    assert "Traceback" not in result.stderr
    assert f.output.read_text() == "previous output\n"


def test_handles_empty_ledgers_and_protects_inputs_and_inventory(f: Fixture) -> None:
    assert f.run([[]]).returncode == 0
    assert f.output.read_text() == ""
    source = f.root / "candidates-0.jsonl"
    assert "--out: must not also be an input" in f.invoke([source], output=source).stderr
    scope_before = f.scope.read_bytes()
    assert "--out: must not replace --in-scope-files" in f.invoke([source], output=f.scope).stderr
    assert f.scope.read_bytes() == scope_before
    if POSIX:
        alias = f.root / "output-alias"
        alias.symlink_to(source)
        assert "--out: must not also be an input" in f.invoke([source], output=alias).stderr
        missing = f.root / "missing.jsonl"
        dangling = f.root / "dangling-output"
        dangling.symlink_to(missing)
        assert f.run([[candidate()]], output=dangling).returncode == 2
        assert not missing.exists()
        (f.root / "directory-alias").symlink_to(f.root, target_is_directory=True)
        for protected in (source, f.scope):
            before = protected.read_bytes()
            output = f"{f.root}/missing/../directory-alias/{protected.name}"
            assert f.invoke([source], output=output).returncode == 2
            assert protected.read_bytes() == before
        assert not (f.root / "missing").exists()
    nested = f.root / "new" / "nested" / "ledger.jsonl"
    assert f.run([[candidate()]], output=nested).returncode == 0
    assert len(f.ledger(nested)) == 1


def test_allows_deleted_inventory_paths_only_when_requested(f: Fixture) -> None:
    write(f.scope, "app/deleted.py\napp/routes.py\n")
    strict = f.run([[candidate()]])
    assert strict.returncode == 2
    assert "in-scope file row 1:" in strict.stderr
    assert f.run([[candidate()]], ["--allow-missing-in-scope"]).returncode == 0
    assert (
        f.run([[candidate([location("app/deleted.py")])]], ["--allow-missing-in-scope"]).returncode
        == 2
    )
    write(f.scope, "../deleted.py\napp/routes.py\n")
    assert f.run([[candidate()]], ["--allow-missing-in-scope"]).returncode == 2
    if POSIX:
        outside = f.root / "outside"
        outside.mkdir()
        (f.repo / "outside").symlink_to(outside, target_is_directory=True)
        write(f.scope, "outside/deleted.py\napp/routes.py\n")
        escaped = f.run([[candidate()]], ["--allow-missing-in-scope"])
        assert escaped.returncode == 2
        assert "path escapes repository" in escaped.stderr


@pytest.mark.parametrize(
    "inventory",
    [
        "\r\napp/routes.py\r\napp/query.py\r\n",
        "app/routes.py\r\napp/query.py\n",
        "app/routes.py\r\napp/query.py",
    ],
)
def test_accepts_crlf_mixed_and_unterminated_inventories(f: Fixture, inventory: str) -> None:
    write(f.scope, inventory)
    assert f.run([[candidate()]]).returncode == 0
    assert f.ledger()[0]["locations"][0]["path"] == "app/routes.py"


@pytest.mark.parametrize(
    ("files", "inventory", "path", "allow_missing", "error"),
    [
        ([" file.ts", "other.ts"], "\n file.ts\nother.ts\n", " file.ts", False, None),
        (["file", "other"], "\r\nfile\r\nother\r\n", "file", False, None),
        (["file", "other"], "other\nfile", "file", False, None),
        (["file"], "file\r\ndeleted\r\n", "file", True, None),
        (["file"], "deleted\nfile\n", "file", False, "No such file"),
    ],
)
def test_mcp_app_inventory_cases(
    tmp_path: Path,
    files: list[str],
    inventory: str,
    path: str,
    allow_missing: bool,
    error: str | None,
) -> None:
    f = Fixture(tmp_path)
    repo = tmp_path / "repo"
    for name in files:
        write(repo / name, "source\n")
    write(f.scope, inventory)
    row = {
        "cwe_ids": ["CWE-20"],
        "locations": [{"path": path, "start_line": 1, "end_line": 1, "role": "entrypoint"}],
        "summary": "Synthetic candidate",
        "evidence": "Synthetic evidence",
    }
    result = f.run([[row]], ["--allow-missing-in-scope"] if allow_missing else [], repo=repo)
    if error:
        assert result.returncode == 2
        assert error in result.stderr
        assert not f.output.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert f.ledger()[0]["locations"][0]["path"] == path


@pytest.mark.skipif(not POSIX, reason="carriage returns are separators on Windows")
def test_final_row_carriage_return_is_part_of_the_name(f: Fixture) -> None:
    write(f.scope, "app/routes.py\napp/query.py\r")
    result = f.run([[candidate()]])
    assert result.returncode == 2
    assert "in-scope file row 2:" in result.stderr


@pytest.mark.skipif(not POSIX, reason="carriage returns are separators on Windows")
def test_ambiguous_carriage_return_with_deleted_paths_allowed(f: Fixture) -> None:
    write(f.repo / "app/gone.py\r", "x\n")
    write(f.scope, "app/gone.py\r\napp/routes.py\n")
    assert f.run([[candidate()]]).returncode == 0
    result = f.run([[candidate()]], ["--allow-missing-in-scope"])
    assert result.returncode == 2
    assert "ambiguous carriage-return paths" in result.stderr


@pytest.mark.skipif(not POSIX, reason="POSIX path bytes")
def test_preserves_literal_posix_names_and_rejects_incompatible_paths(f: Fixture) -> None:
    for name in ("app/ leading.py", "app/trailing .py", "app/résumé.py", "app/路径.py"):
        write(f.repo / name, "line\n")
        write(f.scope, name + "\n")
        result = f.run([[candidate([location(name, 1)])]])
        assert result.returncode == 0, result.stderr
        assert f.ledger()[0]["locations"][0]["path"] == name
    for name in ("app/literal\\name.py", "app/C:foo.py", " ", "   "):
        write(f.repo / name, "line\n")
        write(f.scope, name + "\n")
        result = f.run([[candidate([location(name, 1)])]])
        assert result.returncode == 2
        assert "safe repository-relative POSIX path" in result.stderr


@pytest.mark.skipif(not POSIX, reason="POSIX path bytes")
def test_preserves_raw_path_bytes_and_rejects_links_outside_repository(f: Fixture) -> None:
    raw = os.fsencode(f.root) + b"/repository-" + (b"\xc3\xa9" if sys.platform == "darwin" else b"\xff")
    os.mkdir(raw)
    write(raw + b"/entry.py", "one\n")
    alias = f.root / "alias"
    alias.symlink_to(os.fsdecode(raw), target_is_directory=True)
    write(f.scope, "entry.py\n")
    result = f.run([[candidate([location("entry.py", 1)])]], repo=alias)
    assert result.returncode == 0, result.stderr
    assert f.ledger()[0]["locations"][0]["path"] == "entry.py"
    outside = f.root / "outside.py"
    write(outside, "one\n")
    (f.repo / "outside.py").symlink_to(outside)
    write(f.scope, "app/routes.py\n")
    escaped = f.run([[candidate([location(), location("outside.py", 1, "sink")])]])
    assert escaped.returncode == 2
    assert "must resolve inside --repo-root" in escaped.stderr


@pytest.mark.skipif(not POSIX, reason="POSIX path bytes")
def test_raw_byte_inventory_names_round_trip_as_escaped_surrogates(f: Fixture) -> None:
    write(os.fsencode(f.repo) + b"/app/raw\xff.py", "one\n")
    write(f.scope, b"app/raw\xff.py\n")
    result = f.run([[candidate([location("app/raw\udcff.py", 1)])]])
    assert result.returncode == 0, result.stderr
    assert b'"path":"app/raw\\udcff.py"' in f.output.read_bytes()


def test_accepts_documented_options_home_expansion_and_literal_dash_output(f: Fixture) -> None:
    f.run([[candidate()]])
    source = f.root / "candidates-0.jsonl"
    env = {**os.environ, "HOME": str(f.root), "USERPROFILE": str(f.root)}
    base = [sys.executable, "-I", str(SCRIPT)]
    result = subprocess.run(
        [
            *base,
            "--input",
            str(source),
            str(source),
            "--out=-",
            "--repo-root",
            "~/İrepository",
            "--in-scope-files=~/in-scope.txt",
        ],
        capture_output=True,
        text=True,
        cwd=f.root,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "candidate_id" in json.loads((f.root / "-").read_text(encoding="utf-8"))
    assert subprocess.run([*base, "--help"], capture_output=True).returncode == 0
    for args in ([], ["--input"], ["--unknown", str(source)], ["--allow-missing-in-scope=true"]):
        assert subprocess.run([*base, *args], capture_output=True).returncode == 2, args


def test_rejects_abbreviated_long_options(f: Fixture) -> None:
    # Upstream's parseArgs has no prefix matching; argparse does unless disabled.
    source = f.root / "candidates-0.jsonl"
    f.run([[candidate()]])
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--inp", str(source), "--out", str(f.output),
         "--repo-root", str(f.repo), "--in-scope-files", str(f.scope)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
