"""Carried resolve_security_md.py, held to upstream's current TypeScript contract.

Ported from openai/codex-security 0.6.0:
  sdk/typescript/tests-ts/security-policy-helper.test.ts
  plugins/codex-security/mcp-app/tests/test_helper_files.ts (POSIX expandHome cases)
  sdk/typescript/tests-ts/mcp-launcher.test.ts (resolve-security-md helper cases)

Omitted, with reasons:
  - Windows-only cases (home-variable precedence and drive-relative homes, directory
    identity containment, drive and native scope normalization, named profiles beside
    the current profile, junction links): they exercise the TypeScript Windows file
    binding, which this POSIX-tested port does not carry.
  - Launcher plumbing (Node runtime selection, managed/PATH/batch Node discovery, .cmd
    percent quoting, the macOS sandbox-exec read-only run, `node` vs Bun execPath
    loops): no launcher exists here. The argv-byte and raw-HOME cases that went through
    the /bin/sh launcher are ported by passing raw bytes straight to Python.
  - Node fs hooks are replaced by an equivalent os.scandir/os.lstat hook installed by a
    wrapper (see `run_hooked`), so the enumeration-race and unknown-entry-type cases
    still run.
Run with: python3 -m pytest tests/
"""

from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_ROOT / "assets" / "local-additions" / "scripts" / "resolve_security_md.py"
POSIX = sys.platform != "win32"
LINUX = sys.platform.startswith("linux")
ROOT_USER = POSIX and os.getuid() == 0
LIMIT = 1024 * 1024


def write(base: Path | bytes, relative: str | bytes, data: str | bytes) -> None:
    base = os.fsencode(base)
    path = os.path.join(base, os.fsencode(relative))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data.encode() if isinstance(data, str) else data)


def run(args: list, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        errors="surrogateescape",
        env=env,
        cwd=cwd,
    )


def inventory(root: Path | str) -> subprocess.CompletedProcess[str]:
    return run(["--repo", root, "--list"])


def resolve(root: Path | str, scope: Path | str, output: Path | str = "-"):
    return run(["--repo", root, "--scope", scope, "--out", output])


# Stands in for the TypeScript tests' NODE_OPTIONS --require hooks: patches os before
# running the resolver so directory enumeration can race or report unknown entry types.
HOOK = """
import errno, os, runpy, shutil, stat, sys
mode, root, extra, script = sys.argv[1:5]
sys.argv = [script, *sys.argv[5:]]
real_root = os.path.realpath(root)
original_scandir = os.scandir
original_lstat = os.lstat

class Entry:
    # A DirEntry whose d_type is DT_UNKNOWN: type queries fall back to lstat.
    def __init__(self, entry):
        self.name, self.path = entry.name, entry.path
    def _mode(self):
        try:
            return os.lstat(self.path).st_mode
        except FileNotFoundError:
            return 0
    def is_dir(self, follow_symlinks=True):
        if follow_symlinks:
            try:
                return stat.S_ISDIR(os.stat(self.path).st_mode)
            except FileNotFoundError:
                return False
        return stat.S_ISDIR(self._mode())
    def is_file(self, follow_symlinks=True):
        return stat.S_ISREG(self._mode())
    def is_symlink(self):
        return stat.S_ISLNK(self._mode())
    def stat(self, follow_symlinks=True):
        return os.stat(self.path, follow_symlinks=follow_symlinks)

class Listing:
    # Mirrors the ScandirIterator protocol: iterator plus context manager.
    def __init__(self, entries):
        self._entries = iter(list(entries))
    def __iter__(self):
        return self
    def __next__(self):
        return next(self._entries)
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def close(self):
        pass

fired = []

def scandir(path="."):
    if isinstance(path, int):
        return original_scandir(path)
    entries = Listing(original_scandir(path))
    at_root = os.path.realpath(path) == real_root
    if mode == "replace" and at_root and not fired:
        fired.append(path)
        os.unlink(os.path.join(root, "temporary.tmp"))
        shutil.rmtree(os.path.join(root, "changed-directory"))
        os.symlink(extra, os.path.join(root, "changed-directory"))
    if mode in ("unknown", "ENOENT", "ENOTDIR", "EACCES"):
        entries = Listing(Entry(entry) for entry in entries)
    if mode == "ENOENT" and at_root:
        os.unlink(os.path.join(root, "unrelated.txt"))
    return entries

def lstat(path, *args, **kwargs):
    if mode in ("ENOTDIR", "EACCES") and os.fsdecode(path) == os.path.join(real_root, "unrelated.txt"):
        raise OSError(getattr(errno, mode), "synthetic raw entry " + mode)
    return original_lstat(path, *args, **kwargs)

os.scandir = scandir
os.lstat = lstat
runpy.run_path(script, run_name="__main__")
"""


def run_hooked(tmp: Path, mode: str, root: Path, extra: str, args: list[str]):
    hook = tmp / "hook" / "resolver_hook.py"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(HOOK, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", str(hook), mode, str(root), extra, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        errors="surrogateescape",
    )


HEADING = re.compile(r'^## [^\r\n]*: ("(?:[^"\\]|\\.)*")\r?$', re.M)


def expect_guidance(text: str, policies: list[tuple[str, str]]) -> None:
    headings = list(HEADING.finditer(text))
    sections = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end() : end].strip("\r\n")
        sections.append((json.loads(heading.group(1)), body))
    assert sections == [(source, content.strip("\r\n")) for source, content in policies]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "repository"
    path.mkdir()
    return path


@pytest.fixture
def output(tmp_path: Path) -> Path:
    return tmp_path / "output"


def test_accepts_dash_prefixed_paths_with_equals_syntax(root: Path) -> None:
    for repo, scope, out in (("-1", "-2", "-3"), ("--repo space", "--scope space", "--output= guidance")):
        write(root, f"{repo}/{scope}/SECURITY.md", "dash path policy\n")
        result = run([f"--repo={repo}", f"--scope={scope}", f"--out={out}"], cwd=root)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert "dash path policy" in (root / out).read_text(encoding="utf-8")


def test_rejects_dash_prefixed_values_without_equals_syntax(root: Path) -> None:
    write(root, "-1/SECURITY.md", "dash path policy\n")
    result = run(["--repo", "-1", "--list"], cwd=root)
    assert result.returncode == 2
    assert "ambiguous" in result.stderr
    assert result.stdout == ""


def test_inventories_sorted_hidden_regular_and_file_linked_policies_without_git(root: Path) -> None:
    write(root, "SECURITY.md", "root policy\n")
    write(root, ".hidden/SECURITY.md", "hidden policy\n")
    write(root, ".git/objects/SECURITY.md", "not a policy\n")
    write(root, "shared-policy.md", "shared policy\n")
    (root / "services" / "api").mkdir(parents=True)
    (root / "services" / "api" / "SECURITY.md").symlink_to(root / "shared-policy.md")
    result = inventory(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '[".hidden/SECURITY.md", "SECURITY.md", "services/api/SECURITY.md"]\n'
    assert result.stderr == ""


@pytest.mark.skipif(not POSIX or ROOT_USER, reason="POSIX permissions; root bypasses them")
def test_inventories_unrelated_files_without_directory_search_permission(root: Path) -> None:
    write(root, "SECURITY.md", "root policy\n")
    write(root, "readable/other.txt", "unrelated\n")
    (root / "readable" / ".git").mkdir()
    directory = root / "readable"
    directory.chmod(0o400)
    try:
        result = inventory(root)
        assert result.returncode == 0, result.stderr
        assert result.stdout == '["SECURITY.md"]\n'
    finally:
        directory.chmod(0o700)


@pytest.mark.skipif(not POSIX, reason="POSIX directory symlinks")
def test_ignores_files_and_directory_links_replaced_after_enumeration(
    tmp_path: Path, root: Path, output: Path
) -> None:
    write(root, "SECURITY.md", "root policy\n")
    write(root, "nested/SECURITY.md", "nested policy\n")
    write(root, "temporary.tmp", "temporary file")
    write(root, "unrelated.txt", "unrelated file")
    write(root, "changed-directory/placeholder", "temporary directory")
    write(output, "outside/SECURITY.md", "outside policy\n")
    result = run_hooked(tmp_path, "replace", root, str(output / "outside"), ["--repo", str(root), "--list"])
    assert result.returncode == 0, result.stderr
    assert result.stdout == '["SECURITY.md", "nested/SECURITY.md"]\n'
    assert result.stderr == ""
    assert not (root / "temporary.tmp").exists()


@pytest.mark.skipif(not POSIX, reason="POSIX directory entry types")
@pytest.mark.parametrize("code", ["ENOENT", "ENOTDIR", "EACCES"])
def test_handles_errors_for_an_unrelated_raw_directory_entry(tmp_path: Path, root: Path, code: str) -> None:
    write(root, "SECURITY.md", "root policy\n")
    write(root, "unrelated.txt", "temporary file\n")
    result = run_hooked(tmp_path, code, root, "", ["--repo", str(root), "--list"])
    if code == "EACCES":
        assert result.returncode == 2
        assert "synthetic raw entry EACCES" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == '["SECURITY.md"]\n'
        assert result.stderr == ""
    if code == "ENOENT":
        assert not (root / "unrelated.txt").exists()


def test_frames_unicode_paths_as_ascii_json_in_standard_string_order(root: Path) -> None:
    # JavaScript sorts by UTF-16 code unit, so U+10000 (a surrogate pair) precedes U+E000.
    for name in ("\U00010000", "\ue000", "\u0080", "\u007f"):
        write(root, f"{name}/SECURITY.md", "policy\n")
    result = inventory(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        '["\\u007f/SECURITY.md", "\\u0080/SECURITY.md", '
        '"\\ud800\\udc00/SECURITY.md", "\\ue000/SECURITY.md"]\n'
    )
    guidance = resolve(root, "\U00010000").stdout
    expect_guidance(guidance, [("\U00010000/SECURITY.md", "policy")])
    assert re.search(r'"\\ud800\\udc00/SECURITY\.md"$', guidance.split("\n", 1)[0])


@pytest.mark.skipif(not LINUX, reason="raw filename bytes")
def test_traverses_undecodable_filenames_with_surrogate_escapes(root: Path) -> None:
    write(root, "SECURITY.md", "root policy\n")
    write(root, b"legacy-\xff.txt", "unrelated file")
    write(root, "é".encode() + b"\xff/SECURITY.md", "byte-name policy\n")
    write(root, "é\ue000/SECURITY.md", "BMP policy\n")
    write(root, "é\U00010000/SECURITY.md", "supplementary policy\n")
    raw_root = os.fsencode(root) + b"\xff"
    os.rename(os.fsencode(root), raw_root)
    os.symlink(raw_root, os.fsencode(root))
    result = inventory(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        '["SECURITY.md", "\\u00e9\\ud800\\udc00/SECURITY.md", '
        '"\\u00e9\\udcff/SECURITY.md", "\\u00e9\\ue000/SECURITY.md"]\n'
    )
    assert result.stderr == ""
    scoped = run(["--repo", root, "--scope", "."])
    assert scoped.returncode == 0, scoped.stderr
    expect_guidance(scoped.stdout, [("SECURITY.md", "root policy")])


@pytest.mark.skipif(not POSIX, reason="POSIX directory entry types")
def test_reads_policies_when_directory_entry_types_are_unknown(tmp_path: Path, root: Path) -> None:
    write(root, "SECURITY.md", "root policy\n")
    write(root, "nested/SECURITY.md", "nested policy\n")
    for args in (["--list"], ["--scope", "."]):
        result = run_hooked(tmp_path, "unknown", root, "", ["--repo", str(root), *args])
        assert result.returncode == 0, result.stderr
        if args[0] == "--list":
            assert result.stdout == '["SECURITY.md", "nested/SECURITY.md"]\n'
        else:
            expect_guidance(result.stdout, [("SECURITY.md", "root policy")])


@pytest.mark.skipif(not POSIX, reason="newlines in POSIX filenames")
def test_escapes_newlines_and_terminal_controls_in_inventory_paths(root: Path) -> None:
    write(root, "service\n\x1b[31mname/SECURITY.md", "policy\n")
    result = inventory(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '["service\\n\\u001b[31mname/SECURITY.md"]\n'


@pytest.mark.skipif(not LINUX, reason="raw filename bytes")
def test_reads_raw_byte_policy_target_without_following_replacement_sibling(root: Path, output: Path) -> None:
    target = os.fsencode(root) + b"/policy-\xff"
    write(root, b"policy-\xff", "inside policy\n")
    write(output, "outside.md", "outside policy\n")
    (root / "policy-\ufffd").symlink_to(output / "outside.md")
    os.symlink(target, os.fsencode(root / "SECURITY.md"))
    result = resolve(root, ".")
    assert result.returncode == 0, result.stderr
    expect_guidance(result.stdout, [("SECURITY.md", "inside policy")])


@pytest.mark.skipif(not LINUX, reason="raw filename bytes")
def test_resolves_repository_and_scope_aliases_into_raw_byte_directories(root: Path, output: Path) -> None:
    directory = os.fsencode(root) + b"/component-\xff"
    write(directory, "SECURITY.md", "component policy\n")
    write(root, "SECURITY.md", "root policy\n")
    write(output, "SECURITY.md", "outside policy\n")
    (root / "component-\ufffd").symlink_to(output)
    alias = root / "alias"
    os.symlink(directory, os.fsencode(alias))
    scoped = resolve(root, "alias")
    assert scoped.returncode == 0, scoped.stderr
    expect_guidance(
        scoped.stdout,
        [("SECURITY.md", "root policy"), ("component-\udcff/SECURITY.md", "component policy")],
    )
    rooted = resolve(alias, ".")
    assert rooted.returncode == 0, rooted.stderr
    expect_guidance(rooted.stdout, [("SECURITY.md", "component policy")])
    listed = inventory(alias)
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout == '["SECURITY.md"]\n'


@pytest.mark.skipif(not LINUX, reason="raw argv bytes")
def test_preserves_actual_posix_argv_bytes_for_repository_scope_and_output(root: Path, output: Path) -> None:
    repository = os.fsencode(root) + b"/repo-\xff"
    write(repository, b"scope-\xfe/SECURITY.md", "raw argument policy\n")
    write(root, "repo-\ufffd/scope-\ufffd/SECURITY.md", "wrong policy\n")
    destination = os.fsencode(output) + b"/out-\xfd/guidance.md"
    result = subprocess.run(
        [os.fsencode(sys.executable), b"-I", os.fsencode(SCRIPT), b"--repo", repository,
         b"--scope", b"scope-\xfe", b"--out", destination],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    with open(destination, encoding="utf-8") as handle:
        expect_guidance(handle.read(), [("scope-\udcfe/SECURITY.md", "raw argument policy")])


@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_does_not_inventory_or_follow_directory_links_even_when_named_security_md(
    root: Path, output: Path
) -> None:
    write(output, "SECURITY.md", "outside policy\n")
    (root / "outside-link").symlink_to(output, target_is_directory=True)
    (root / "SECURITY.md").symlink_to(output, target_is_directory=True)
    result = inventory(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "[]\n"


@pytest.mark.skipif(not POSIX, reason="symlinks")
@pytest.mark.parametrize("directory_link", [False, True], ids=["file", "dir"])
def test_inventories_broken_links_and_outside_file_links_without_reading_them(
    root: Path, output: Path, directory_link: bool
) -> None:
    write(output, "outside.md", "outside policy\n")
    (root / "broken").mkdir()
    (root / "broken" / "SECURITY.md").symlink_to(root / "missing.md", target_is_directory=directory_link)
    (root / "SECURITY.md").symlink_to(output / "outside.md")
    result = inventory(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '["SECURITY.md", "broken/SECURITY.md"]\n'


def test_concatenates_plain_folder_guidance_from_root_to_leaf(root: Path) -> None:
    write(root, "SECURITY.md", "root policy\n")
    write(root, "services/SECURITY.md", "service policy\n")
    write(root, "services/api/SECURITY.md", "api policy")
    write(root, "services/api/handler.ts", "export {};\n")
    result = resolve(root, root / "services" / "api" / "handler.ts")
    assert result.returncode == 0, result.stderr
    expect_guidance(
        result.stdout,
        [
            ("SECURITY.md", "root policy"),
            ("services/SECURITY.md", "service policy"),
            ("services/api/SECURITY.md", "api policy"),
        ],
    )
    assert result.stdout.endswith("api policy\n")


def test_uses_file_parent_skips_whitespace_only_guidance_and_preserves_bom(root: Path) -> None:
    write(root, "SECURITY.md", "\ufeff")
    write(root, "src/SECURITY.md", " \n\t\u0085\u001c")
    write(root, "src/app.ts", "export {};\n")
    result = resolve(root, "src/app.ts")
    assert result.returncode == 0, result.stderr
    expect_guidance(result.stdout, [("SECURITY.md", "\ufeff")])


@pytest.mark.skipif(not POSIX, reason="POSIX directory symlinks")
def test_resolves_parent_components_after_directory_symlinks(root: Path, output: Path) -> None:
    write(root, "nested/SECURITY.md", "nested policy\n")
    (root / "nested" / "child").mkdir()
    (root / "alias").symlink_to("nested/child", target_is_directory=True)
    result = resolve(root, "alias/..")
    assert result.returncode == 0, result.stderr
    expect_guidance(result.stdout, [("nested/SECURITY.md", "nested policy")])
    write(output, "SECURITY.md", "outside policy\n")
    (output / "child").mkdir()
    (root / "outside").symlink_to(output / "child", target_is_directory=True)
    outside = resolve(root, "outside/..")
    assert outside.returncode != 0
    assert outside.stdout == ""
    assert "outside the scan root" in outside.stderr


@pytest.mark.skipif(not POSIX, reason="POSIX symlinks")
def test_rejects_scope_link_cycles(root: Path) -> None:
    (root / "first").symlink_to("second", target_is_directory=True)
    (root / "second").symlink_to("first", target_is_directory=True)
    result = resolve(root, "first")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "scan scope does not exist" in result.stderr
    assert "Traceback" not in result.stderr


def test_rejects_file_scope_with_trailing_separator(root: Path) -> None:
    # realpath(3) fails with ENOTDIR here; pathlib would silently drop the separator.
    write(root, "file.ts", "export {};\n")
    result = resolve(root, "file.ts/")
    assert result.returncode == 2
    assert "scan scope does not exist" in result.stderr


def test_creates_output_directories_and_writes_empty_guidance_without_policies(
    root: Path, output: Path
) -> None:
    destination = output / "artifacts" / "guidance.md"
    result = resolve(root, ".", destination)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert destination.read_text(encoding="utf-8") == ""


def test_writes_utf8_guidance_independently_of_console_locale(root: Path, output: Path) -> None:
    content = "Unicode policy: 🔐 東京\n"
    write(root, "SECURITY.md", content)
    result = run(["--repo", root, "--scope", "."], env={**os.environ, "LANG": "C", "LC_ALL": "C"})
    assert result.returncode == 0, result.stderr
    expect_guidance(result.stdout, [("SECURITY.md", content)])
    destination = output / "guidance.md"
    assert resolve(root, ".", destination).returncode == 0
    expected = result.stdout.replace("\n", "\r\n") if not POSIX else result.stdout
    assert destination.read_bytes().decode("utf-8") == expected


def test_expands_current_home_for_repository_and_scope(root: Path) -> None:
    write(root, "SECURITY.md", "home policy\n")
    result = run(["--repo", "~", "--scope", "~"], env={**os.environ, "HOME": str(root), "USERPROFILE": str(root)})
    assert result.returncode == 0, result.stderr
    assert "home policy\n" in result.stdout


def test_keeps_literal_dash_paths_and_dot_dash_output_as_files(root: Path) -> None:
    write(root, "SECURITY.md", "policy\n")
    result = run(["--repo", ".", "--scope", ".", "--out", "./-"], cwd=root)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "policy" in (root / "-").read_text(encoding="utf-8")


@pytest.mark.skipif(not LINUX, reason="raw environment bytes")
def test_preserves_raw_home_bytes_and_keeps_output_tildes_literal(root: Path) -> None:
    home = os.fsencode(root) + b"/home-\xff"
    write(home, "SECURITY.md", "raw home policy\n")
    write(home, "project/SECURITY.md", "project policy\n")
    write(root, "home-\ufffd/SECURITY.md", "replacement sibling\n")
    write(root, "home-\ufffd/project/SECURITY.md", "replacement project\n")
    env = {**os.environb, b"HOME": home}
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--repo", "~", "--scope", "~/project", "--out", "~/guidance.md"],
        capture_output=True,
        text=True,
        cwd=root,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    expect_guidance(
        (root / "~" / "guidance.md").read_text(encoding="utf-8"),
        [("SECURITY.md", "raw home policy"), ("project/SECURITY.md", "project policy")],
    )


@pytest.mark.skipif(not POSIX, reason="POSIX HOME semantics")
def test_distinguishes_ordinary_unset_and_empty_home_for_quoted_tilde_paths(root: Path) -> None:
    write(root, "project/SECURITY.md", "project policy\n")
    project = root / "project"
    passwd_home = pwd.getpwuid(os.getuid()).pw_dir
    for home, path in (
        (str(root), "~/project"),
        (f"{root}/", "~/project"),
        (f"{root}///", "~/project"),
        ("/", f"~{project}"),
        (None, f"~/{os.path.relpath(project, passwd_home)}"),
        ("", f"~{project}"),
    ):
        env = {key: value for key, value in os.environ.items() if key != "HOME"}
        if home is not None:
            env["HOME"] = home
        result = run(["--repo", path, "--scope", path], env=env)
        assert result.returncode == 0, (home, result.stderr)
        expect_guidance(result.stdout, [("SECURITY.md", "project policy")])


@pytest.mark.skipif(not POSIX, reason="POSIX HOME semantics")
@pytest.mark.parametrize(
    ("home", "path", "expected"),
    [
        ("/", "~", "/"),
        ("/", "~/.cxsec-missing", "/.cxsec-missing"),
        ("//", "~/.cxsec-missing", "/.cxsec-missing"),
        ("", "~", "/"),
        ("", "~/cxsec-missing-child", "/cxsec-missing-child"),
        ("/synthetic/home///", "~/alias/../child", "/synthetic/home/alias/../child"),
        ("/synthetic/home/", "//literal/path", "//literal/path"),
    ],
)
def test_posix_home_expansion_removes_boundary_separators_keeping_literal_suffixes(
    root: Path, home: str, path: str, expected: str
) -> None:
    result = run(["--repo", root, "--scope", path], env={**os.environ, "HOME": home})
    assert result.returncode == 2
    if expected == "/":
        assert result.stderr.rstrip("\n").endswith("scan scope is outside the scan root: /")
    else:
        assert result.stderr.rstrip("\n").endswith(f"scan scope does not exist: {expected}")


@pytest.mark.skipif(not POSIX, reason="POSIX account database")
def test_rejects_unknown_named_home(root: Path) -> None:
    result = run(["--repo", "~cxsec-no-such-user-0", "--list"])
    assert result.returncode == 2
    assert "Could not determine home directory." in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(not POSIX, reason="POSIX account database")
def test_expands_named_homes_with_quoted_and_control_characters_in_remaining_path(root: Path) -> None:
    info = pwd.getpwuid(os.getuid())
    directory = 'space "quote" back\\slash\nline\ttab\b'
    write(root, f"{directory}/SECURITY.md", "named-home policy\n")
    path = f"~{info.pw_name}/{os.path.relpath(root / directory, info.pw_dir)}"
    result = run(["--repo", path, "--scope", path], env={**os.environ, "HOME": str(root / "unused home")})
    assert result.returncode == 0, result.stderr
    expect_guidance(result.stdout, [("SECURITY.md", "named-home policy")])


@pytest.mark.skipif(not POSIX, reason="POSIX executables")
def test_expands_named_homes_without_invoking_git_or_python(root: Path, output: Path) -> None:
    write(root, "SECURITY.md", "independent policy\n")
    tools = output / "tools"
    marker = output / "tool-invoked"
    for name in ("git", "python", "python3"):
        write(tools, name, '#!/bin/sh\nprintf invoked > "$HELPER_TOOL_MARKER"\nexit 99\n')
        (tools / name).chmod(0o755)
    info = pwd.getpwuid(os.getuid())
    path = f"~{info.pw_name}/{os.path.relpath(root, info.pw_dir)}"
    result = run(
        ["--repo", path, "--scope", path],
        env={**os.environ, "PATH": str(tools), "HELPER_TOOL_MARKER": str(marker)},
    )
    assert result.returncode == 0, result.stderr
    expect_guidance(result.stdout, [("SECURITY.md", "independent policy")])
    assert result.stderr == ""
    assert not marker.exists()


@pytest.mark.skipif(not POSIX, reason="POSIX symlinks")
def test_resolves_repository_local_file_and_directory_links(root: Path) -> None:
    write(root, "policies/shared.md", "shared policy\n")
    (root / "SECURITY.md").symlink_to(root / "policies" / "shared.md")
    write(root, "components/SECURITY.md", "component policy\n")
    write(root, "components/app.ts", "export {};\n")
    (root / "components" / "leaf").mkdir()
    (root / "alias").symlink_to(root / "components" / "leaf", target_is_directory=True)
    result = resolve(root, "alias/../app.ts")
    assert result.returncode == 0, result.stderr
    assert "shared policy\n" in result.stdout
    assert "component policy\n" in result.stdout


def test_rejects_missing_non_directory_or_outside_repository_and_scope_paths(root: Path, output: Path) -> None:
    output.mkdir()
    write(root, "file.ts", "export {};\n")
    for result, message in (
        (inventory(root / "missing"), "scan root does not exist"),
        (inventory(root / "file.ts"), "scan root is not a directory"),
        (resolve(root, "missing"), "scan scope does not exist"),
        (resolve(root, output), "scan scope is outside the scan root"),
    ):
        assert result.returncode == 2, result.stderr
        assert message in result.stderr
        assert result.stdout == ""


@pytest.mark.skipif(not POSIX, reason="POSIX symlinks")
def test_rejects_scope_and_policy_links_outside_repository(root: Path, output: Path) -> None:
    write(output, "outside.md", "outside policy\n")
    (root / "SECURITY.md").symlink_to(output / "outside.md")
    (root / "outside").symlink_to(output, target_is_directory=True)
    for scope, message in ((".", "SECURITY.md is outside the scan root"), ("outside", "scan scope is outside the scan root")):
        result = resolve(root, scope)
        assert result.returncode == 2, result.stderr
        assert message in result.stderr
        assert result.stdout == ""


@pytest.mark.parametrize("kind", ["non-utf8", "oversized", "linked"])
def test_rejects_non_utf8_and_oversized_regular_or_linked_policies(root: Path, kind: str) -> None:
    if kind == "linked" and not POSIX:
        pytest.skip("symlinks")
    content = b"\xff" if kind == "non-utf8" else b"a" * (LIMIT + 1)
    write(root, "large.md" if kind == "linked" else "SECURITY.md", content)
    if kind == "linked":
        (root / "SECURITY.md").symlink_to(root / "large.md")
    result = resolve(root, ".")
    assert result.returncode == 2, result.stderr
    assert ("not valid UTF-8" if kind == "non-utf8" else "exceeds 1 MiB") in result.stderr
    assert result.stdout == ""


def test_accepts_policy_exactly_at_byte_limit(root: Path) -> None:
    content = "a" * LIMIT
    write(root, "SECURITY.md", content)
    result = resolve(root, ".")
    assert result.returncode == 0, result.stderr
    expect_guidance(result.stdout, [("SECURITY.md", content)])


def test_preserves_required_and_mutually_exclusive_helper_arguments(root: Path) -> None:
    for flag in ("--help", "-h"):
        result = run([flag])
        assert result.returncode == 0, result.stderr
        assert "Usage:" in result.stdout
    for args in (["--unknown"], ["--scope"], ["positional"]):
        result = run(["--repo", root, *args])
        assert result.returncode == 2, args
        assert result.stdout == ""
    for args, message in (
        (["--list"], "--repo is required"),
        (["--repo", root], "--scope is required unless --list is specified"),
        (["--repo", root, "--list", "--scope", "."], "--list cannot be combined with --scope"),
    ):
        result = run(args)
        assert result.returncode == 2, args
        assert message in result.stderr
        assert result.stdout == ""


def test_rejects_abbreviated_and_valued_boolean_options(root: Path) -> None:
    # 0.6.0 security-guidance.md: abbreviated long option names are not accepted.
    write(root, "SECURITY.md", "policy\n")
    for args in (
        ["--rep", str(root), "--list"],
        ["--repo", str(root), "--li"],
        ["--repo", str(root), "--sc", "."],
        ["--repo", str(root), "--scope", ".", "--ou", "-"],
        ["--repo", str(root), "--list=true"],
        ["--repo", str(root), "--list", "--", "extra"],
    ):
        result = run(args)
        assert result.returncode == 2, args
        assert result.stdout == ""


def test_helper_mode_resolves_relative_paths_with_spaces_against_working_directory(tmp_path: Path) -> None:
    repository = tmp_path / "repository with spaces"
    write(repository, "SECURITY.md", "helper policy\n")
    write(repository, "service/SECURITY.md", "scoped policy\n")
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    scoped = run(["--repo", repository, "--scope", "service", "--out", "-"])
    assert scoped.returncode == 0, scoped.stderr
    expect_guidance(
        scoped.stdout,
        [("SECURITY.md", "helper policy"), ("service/SECURITY.md", "scoped policy")],
    )
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    result = run(
        ["--repo", "repository with spaces", "--scope", ".", "--out", "output with spaces/guidance.md"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    expect_guidance(
        (tmp_path / "output with spaces" / "guidance.md").read_text(encoding="utf-8"),
        [("SECURITY.md", "helper policy")],
    )
