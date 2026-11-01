"""Carried bind_repo_scopes.py, held to upstream's current TypeScript contract.

Ported from openai/codex-security 0.6.0 sdk/typescript/tests-ts/bind-repo-scopes.test.ts.
Omitted: the packaged-launcher raw-byte case (replaced by a direct raw-byte argv case)
and the Windows PowerShell/CMD launcher cases. Run with: python3 -m pytest tests/
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_ROOT / "assets" / "local-additions" / "scripts" / "bind_repo_scopes.py"
POSIX = sys.platform != "win32"
NEWLINE = "\n" if POSIX else "\r\n"
MANIFEST = '{"scan":{"scope":{"includePaths":["old"],"excludePaths":[]}}}'
COVERAGE = '{"includePaths":["old"],"excludePaths":[]}'


class Fixture:
    def __init__(self, root: Path, scopes: list[str]) -> None:
        self.root = root
        self.scopes = root / "requested scopes.json"
        self.manifest = root / "scan-manifest.json"
        self.coverage = root / "coverage.json"
        self.scopes.write_text(json.dumps(scopes), encoding="utf-8")
        self.manifest.write_text(MANIFEST)
        self.coverage.write_text(COVERAGE)

    def run(self, args: list[str] | None = None, env: dict | None = None):
        if args is None:
            args = ["--scopes-file", str(self.scopes), "--manifest", str(self.manifest), "--coverage", str(self.coverage)]
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            cwd=self.root,
            env=env,
            errors="surrogateescape",
        )

    def documents(self) -> list[bytes]:
        return [self.manifest.read_bytes(), self.coverage.read_bytes()]


@pytest.fixture
def make(tmp_path: Path):
    def build(scopes: list[str] | None = None) -> Fixture:
        return Fixture(tmp_path.resolve(), scopes or ["src", "src/runtime.py", "empty", "audit Ignore.py"])

    return build


def test_binds_exact_requested_scopes_and_retains_unrelated_fields(make) -> None:
    f = make(["src", "src/runtime.py", "empty", "audit Ignore.py", "src", " ", "../sibling"])
    result = f.run()
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Bound 7 requested scopes into the scan contract{NEWLINE}"
    scopes = json.loads(f.scopes.read_text(encoding="utf-8"))
    assert json.loads(f.manifest.read_text(encoding="utf-8")) == {
        "scan": {"scope": {"includePaths": scopes, "excludePaths": []}}
    }
    assert json.loads(f.coverage.read_text(encoding="utf-8")) == {"includePaths": scopes, "excludePaths": []}


def test_preserves_unrelated_json_values_and_exact_large_integers(make) -> None:
    f = make(["\udcff", "\U0001f600"])
    f.manifest.write_text(
        '{"10":3,"2":2,"scan":{"scope":{}},"integer":9007199254740993,'
        '"negativeInteger":-9007199254740993,"values":[-0.0,1e20,1e-7],"nested":{"9":{},"1":[]}}'
    )
    f.coverage.write_text('{"é":"😀","__proto__":true}', encoding="utf-8")
    assert f.run().returncode == 0
    contents = f.manifest.read_text(encoding="utf-8")
    assert '"integer": 9007199254740993' in contents
    assert '"negativeInteger": -9007199254740993' in contents
    manifest = json.loads(contents)
    assert manifest["scan"]["scope"]["includePaths"] == ["\udcff", "\U0001f600"]
    assert manifest["values"] == [0, 1e20, 1e-7]
    assert manifest["nested"] == {"9": {}, "1": []}
    coverage = json.loads(f.coverage.read_text(encoding="utf-8"))
    assert coverage["é"] == "😀"
    assert coverage["__proto__"] is True
    assert coverage["includePaths"] == ["\udcff", "\U0001f600"]


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("[]", "Scopes file must contain a non-empty JSON string array"),
        ('[""]', "Scopes file must contain a non-empty JSON string array"),
        ('["src",1]', "Scopes file must contain a non-empty JSON string array"),
        ("{}", "Scopes file must contain a non-empty JSON string array"),
        ("[", "Unable to read scopes file"),
        ('﻿["src"]', "Unable to read scopes file"),
    ],
)
def test_rejects_invalid_scopes_before_changing_either_document(make, contents: str, message: str) -> None:
    f = make()
    before = f.documents()
    f.scopes.write_text(contents, encoding="utf-8")
    result = f.run()
    assert result.returncode == 1
    assert result.stderr == f"{message}: {f.scopes}{NEWLINE}"
    assert f.documents() == before


@pytest.mark.parametrize(
    ("target", "contents"),
    [
        ("manifest", "[]"),
        ("manifest", "{}"),
        ("manifest", '{"scan":[]}'),
        ("manifest", '{"scan":{"scope":null}}'),
        ("coverage", "null"),
        ("coverage", "{"),
        ("coverage", bytes([0xFF])),
    ],
)
def test_rejects_invalid_contract_documents_before_writing(make, target: str, contents: str | bytes) -> None:
    f = make()
    path = getattr(f, target)
    path.write_bytes(contents.encode() if isinstance(contents, str) else contents)
    before = f.documents()
    result = f.run()
    assert result.returncode == 1
    assert result.stderr == f"Unable to bind requested scopes into the scan contract{NEWLINE}"
    assert f.documents() == before


def test_reads_both_aliased_documents_before_the_ordered_writes(make) -> None:
    f = make(["new"])
    f.coverage = f.manifest
    assert f.run().returncode == 0
    assert json.loads(f.manifest.read_text()) == {
        "scan": {"scope": {"includePaths": ["old"], "excludePaths": []}},
        "includePaths": ["new"],
    }


@pytest.mark.skipif(not POSIX or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_escapes_terminal_controls_only_in_filesystem_errors(make) -> None:
    f = make()
    manifest = f.root / "manifest-\x1b[2J.json"
    f.manifest.rename(manifest)
    f.manifest = manifest
    before = f.documents()
    manifest.chmod(0o444)
    try:
        result = f.run()
        assert result.returncode == 1
        assert result.stdout == ""
        assert "manifest-\\u001b[2J.json" in result.stderr
        assert "\x1b" not in result.stderr
        assert f.documents() == before
    finally:
        manifest.chmod(0o644)
    scopes = f.root / "scopes-\x1b[2J.json"
    f.scopes.rename(scopes)
    f.scopes = scopes
    scopes.write_text("[]")
    invalid = f.run()
    assert invalid.returncode == 1
    assert invalid.stderr == f"Scopes file must contain a non-empty JSON string array: {scopes}\n"


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_escapes_terminal_controls_in_write_errors_as_any_user(make) -> None:
    # Root ignores file modes, so force a write failure by making the target a directory.
    f = make()
    coverage = f.root / "coverage-\x1b[2J.json"
    coverage.mkdir()
    result = f.run(["--scopes-file", str(f.scopes), "--manifest", str(f.manifest), "--coverage", str(coverage)])
    assert result.returncode == 1
    assert "\x1b" not in result.stderr


def test_expands_home_and_relative_paths(make) -> None:
    f = make()
    env = {**os.environ, "HOME": str(f.root), "USERPROFILE": str(f.root)}
    result = f.run(
        ["--scopes-file=~/requested scopes.json", "--manifest=./scan-manifest.json", "--coverage=coverage.json"],
        env=env,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not POSIX, reason="POSIX path bytes")
def test_preserves_raw_scope_and_contract_path_bytes(make) -> None:
    f = make()
    root = os.fsencode(f.root)
    paths = {"scopes": root + b"/scopes-\xff", "manifest": root + b"/manifest-\xfe", "coverage": root + b"/coverage-\xfd"}
    for key, raw in paths.items():
        os.rename(getattr(f, key), raw)
        (f.root / f"{key}-�").write_text("replacement sentinel")
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), b"--scopes-file", paths["scopes"], b"--manifest", paths["manifest"], b"--coverage", paths["coverage"]],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    with open(paths["scopes"], encoding="utf-8") as handle:
        requested = json.load(handle)
    with open(paths["manifest"], encoding="utf-8") as handle:
        assert json.load(handle)["scan"]["scope"]["includePaths"] == requested
    with open(paths["coverage"], encoding="utf-8") as handle:
        assert json.load(handle)["includePaths"] == requested
    for key in paths:
        assert (f.root / f"{key}-�").read_text() == "replacement sentinel"


@pytest.mark.skipif(not POSIX, reason="symlinks and modes")
def test_preserves_output_symlinks_and_file_modes(make) -> None:
    f = make()
    target = f.root / "coverage-target.json"
    target.write_text("{}")
    target.chmod(0o640)
    f.coverage.unlink()
    f.coverage.symlink_to(target)
    assert f.run().returncode == 0
    assert f.coverage.resolve() == target
    assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize(
    ("args", "status"),
    [([], 2), (["--manifest"], 2), (["--unknown"], 2), (["--help"], 0), (["--", "--help"], 2)],
)
def test_reports_argument_status(make, args: list[str], status: int) -> None:
    assert make().run(args).returncode == status
