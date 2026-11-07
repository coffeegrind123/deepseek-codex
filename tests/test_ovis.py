#!/usr/bin/env python3
"""Ovis for this workspace: the switch, the token, the gate, the panes, the relay.

What these pin, none of which needs Docker, herdr or a running Ovis:

  * OVIS_ENABLED defaults ON, 0/false/no/off turn it off, and settings come
    from the environment first, then .codex/secrets.env read as text.
  * The token is generated once, mode 0600; OVIS_TOKEN overrides it without
    touching the file; OVIS_TOKEN=off means none, and the server gets none.
  * ovis.sh refuses to run inside the Landlock cage (herdr would cage every
    pane), and `start` stays soft when Ovis is off or not installed.
  * The gate publishes the Docker host's 127.0.0.1 only, from OVIS_PORT
    (default 7327), hardened, and is recreated when what it runs changes.
  * A pane's launch script carries only the listed per-launch keys (herdr
    starts panes from its own environment), never a secret, deletes itself,
    drops the shim from PATH and execs ./codex with the arguments intact.
  * herdr's pane shell starts Codex and then bash in every new pane, and only
    bash for a command, a plain pane (OVIS_PANE_PLAIN) or OVIS_CODEX_PANE=0;
    herdr is told not to type resume commands into those panes.
  * scripts/ovis_patch.py applies Codex 0.166's command table, plain panes for
    Ovis's own New workspace / New tab and the Codex picker default, once, and
    refuses another bundle or a changed file without writing anything.
  * The `codex` shim removes itself from PATH before it runs ./codex.
  * The SessionStart hook does nothing outside a herdr pane, runs herdr's hook
    inside one, and is trusted in config.toml.
  * The relay passes bytes unchanged both ways and passes on a half-close.
  * `install --check` accepts only the pinned bundle.

The end-to-end path (a caged ./codex in a herdr pane read by Ovis as
`codex-transcript`, and herdr's hook reaching the herdr socket from inside the
cage) was measured live on 2026-11-01.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

BASH = shutil.which("bash") or "/bin/bash"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
OVIS = os.path.join(SCRIPTS, "ovis.sh")
SHIM = os.path.join(SCRIPTS, "ovis-shims", "codex")
HOOK = os.path.join(SCRIPTS, "ovis-codex-hook.sh")
RELAY = os.path.join(SCRIPTS, "ovis_forward.py")
WRAPPER = os.path.join(REPO, "codex")

# Keys a caller might have exported for unrelated reasons; cleared so the host's
# own environment cannot decide a test.
SCRUB = ("OVIS_ENABLED", "OVIS_PORT", "OVIS_TOKEN", "OVIS_GATE", "OVIS_CODEX_PANE",
         "OVIS_CODEX_DIR", "OVIS_DIR", "HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH",
         "CODEX_LANDLOCK_CAGE", "CODEX_NET", "CODEX_HOOK_TRUST")


class Isolated(unittest.TestCase):
    """Each test gets its own secrets file, state dir and run dir."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="test-ovis-")
        self.secrets = os.path.join(self.tmp, "secrets.env")
        self.state = os.path.join(self.tmp, "state")
        self.run_dir = os.path.join(self.tmp, "run")
        open(self.secrets, "w").close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in SCRUB}
        env.update(OVIS_SECRETS_FILE=self.secrets, OVIS_STATE_DIR=self.state,
                   OVIS_RUN_DIR=self.run_dir)
        env.update(extra)
        return env

    def sourced(self, script: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([BASH, "-c", f'source "{OVIS}"; {script}'], capture_output=True,
                              text=True, env=self.env(**extra), timeout=60)

    def ovis(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([BASH, OVIS, *args], capture_output=True, text=True,
                              env=self.env(**extra), timeout=60, stdin=subprocess.DEVNULL)

    def write_secrets(self, text: str) -> None:
        with open(self.secrets, "w", encoding="utf-8") as fh:
            fh.write(text)


def executable(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, 0o755)


class Switch(Isolated):
    def enabled(self, **extra: str) -> bool:
        return self.ovis("enabled", **extra).returncode == 0

    def test_on_by_default(self) -> None:
        self.assertTrue(self.enabled())

    def test_off_spellings(self) -> None:
        for value in ("0", "false", "no", "off", "OFF"):
            self.assertFalse(self.enabled(OVIS_ENABLED=value), value)

    def test_on_spellings(self) -> None:
        for value in ("1", "true", "yes", ""):
            self.assertTrue(self.enabled(OVIS_ENABLED=value), repr(value))

    def test_secrets_file_turns_it_off_and_the_environment_wins(self) -> None:
        self.write_secrets("OVIS_ENABLED=0\n")
        self.assertFalse(self.enabled())
        self.assertTrue(self.enabled(OVIS_ENABLED="1"))

    def test_wrapper_dispatches_before_the_router(self) -> None:
        # `./codex ovis` must reach ovis.sh without the router, secrets or cage.
        proc = subprocess.run([BASH, WRAPPER, "ovis", "enabled"], env=self.env(OVIS_ENABLED="off"),
                              capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        proc = subprocess.run([BASH, WRAPPER, "ovis", "enabled"], env=self.env(OVIS_ENABLED="1"),
                              capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class Settings(Isolated):
    def cfg(self, key: str, **extra: str) -> str:
        return self.sourced(f"cfg {key}", **extra).stdout

    def test_quotes_export_and_last_wins(self) -> None:
        self.write_secrets('OVIS_PORT=1111\nexport OVIS_PORT="2222"\nOVIS_CODEX_DIR=\'/a b\'\n')
        self.assertEqual(self.cfg("OVIS_PORT"), "2222")
        self.assertEqual(self.cfg("OVIS_CODEX_DIR"), "/a b")

    def test_read_as_text_never_run(self) -> None:
        marker = os.path.join(self.tmp, "ran")
        self.write_secrets(f'OVIS_TOKEN=$(touch {marker})\n')
        self.assertEqual(self.cfg("OVIS_TOKEN"), f"$(touch {marker})")
        self.assertFalse(os.path.exists(marker))

    def test_host_port_default_and_override(self) -> None:
        self.assertEqual(self.sourced("host_port").stdout, "7327")
        self.assertEqual(self.sourced("host_port", OVIS_PORT="9123").stdout, "9123")

    def test_strip_path_every_occurrence(self) -> None:
        out = self.sourced('strip_path "/s:/a:/s:/b:/s" /s').stdout
        self.assertEqual(out, "/a:/b")
        self.assertEqual(self.sourced('strip_path "/a:/sx:/b" /s').stdout, "/a:/sx:/b")


class Token(Isolated):
    def token_file(self) -> str:
        return os.path.join(self.state, "token")

    def test_generated_once_private_and_stable(self) -> None:
        first = self.sourced("token").stdout
        second = self.sourced("token").stdout
        self.assertRegex(first, r"^[0-9a-f]{48}$")
        self.assertEqual(first, second)
        self.assertEqual(stat.S_IMODE(os.stat(self.token_file()).st_mode), 0o600)

    def test_ovis_token_wins_and_leaves_the_file_alone(self) -> None:
        self.assertEqual(self.sourced("token", OVIS_TOKEN="from-env").stdout, "from-env")
        self.write_secrets("OVIS_TOKEN=from-secrets\n")
        self.assertEqual(self.sourced("token").stdout, "from-secrets")
        self.assertFalse(os.path.exists(self.token_file()))

    def test_off_means_no_token_and_no_file(self) -> None:
        for value in ("off", "OFF"):
            self.assertEqual(self.sourced("token", OVIS_TOKEN=value).stdout, "", value)
            self.assertFalse(os.path.exists(self.token_file()), value)

    def test_the_server_starts_without_ovis_token_when_off(self) -> None:
        # The supervisor's own subshell, with bun replaced by a recorder.
        seen = os.path.join(self.tmp, "seen")
        for value, want in (("off", "UNSET"), ("s3cret", "s3cret")):
            script = (f'tok="$(token)"; ( if [[ -n "$tok" ]]; then export OVIS_TOKEN="$tok"; '
                      f'else unset OVIS_TOKEN; fi && echo "${{OVIS_TOKEN-UNSET}}" > "{seen}" )')
            self.sourced(script, OVIS_TOKEN=value)
            with open(seen, encoding="utf-8") as fh:
                self.assertEqual(fh.read().strip(), want, value)


class Cage(Isolated):
    def test_refuses_inside_the_cage(self) -> None:
        for cmd in (["start"], ["stop"], ["run"], ["supervise"], ["gate", "ensure"], ["gate", "rm"]):
            proc = self.ovis(*cmd, CODEX_LANDLOCK_CAGE="/home/x")
            self.assertNotEqual(proc.returncode, 0, cmd)
            self.assertIn("outside the Landlock cage", proc.stderr, cmd)

    def test_start_is_soft_when_off_or_missing(self) -> None:
        proc = self.ovis("start", OVIS_ENABLED="0")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("off", proc.stdout)
        proc = self.ovis("start", OVIS_DIR=os.path.join(self.tmp, "none"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("./codex ovis install", proc.stderr)
        proc = self.ovis("start", "--quiet", OVIS_DIR=os.path.join(self.tmp, "none"))
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))


class Gate(Isolated):
    def args(self, port: str = "7327") -> list[str]:
        # NUL-separated, one element per docker argument: the relay source has
        # newlines and must reach `python3 -c` as one argument.
        out = self.sourced(f'gate_run_args img:1 workspace {port} abc123').stdout
        self.assertTrue(out.endswith("\0"), repr(out[-20:]))
        return out[:-1].split("\0")

    def test_loopback_only_and_hardened(self) -> None:
        args = self.args()
        joined = " ".join(args)
        self.assertIn("-p 127.0.0.1:7327:7317", joined)
        self.assertNotIn("0.0.0.0:7327", joined)
        for flag in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                     "--user 65534:65534", "--restart unless-stopped", "--network codex-ovis",
                     "--label codex.ovis.spec=abc123", "--entrypoint python3"):
            self.assertIn(flag, joined, flag)
        # The relay arguments: listen on the gate's own address, forward to the
        # workspace container's relay by name.
        self.assertEqual(args[-4:], ["0.0.0.0", "7317", "workspace", "7317"])

    def test_host_port_follows_ovis_port(self) -> None:
        self.assertIn("127.0.0.1:9123:7317", self.args("9123"))

    def test_runs_the_relay_source_as_one_argument(self) -> None:
        args = self.args()
        with open(RELAY, encoding="utf-8") as fh:
            source = fh.read().rstrip("\n")
        self.assertEqual(args[args.index("-c") + 1], source)
        self.assertEqual(len(args), args.index("-c") + 6)

    def test_spec_changes_with_what_it_runs(self) -> None:
        def spec(image: str, target: str, port: str) -> str:
            return self.sourced(f"gate_spec {image} {target} {port}").stdout

        base = spec("img:1", "workspace", "7327")
        self.assertRegex(base, r"^[0-9a-f]{16}$")
        self.assertEqual(base, spec("img:1", "workspace", "7327"))
        self.assertNotEqual(base, spec("img:1", "workspace", "7328"))
        self.assertNotEqual(base, spec("img:2", "workspace", "7327"))
        self.assertNotEqual(base, spec("img:1", "other", "7327"))

    def test_off(self) -> None:
        self.assertIn("off", self.ovis("gate", "status", OVIS_GATE="off").stdout)
        self.assertEqual(self.sourced("gate_ensure; echo rc=$?", OVIS_GATE="0").stdout, "rc=0\n")


class Launch(Isolated):
    """The script a herdr pane runs, executed here with a recorder for ./codex."""

    def setUp(self) -> None:
        super().setUp()
        self.record = os.path.join(self.tmp, "record.json")
        self.fake = os.path.join(self.tmp, "fake-codex")
        executable(self.fake, f"""#!/usr/bin/env python3
import json, os, sys
json.dump({{"cwd": os.getcwd(), "argv": sys.argv[1:], "env": dict(os.environ)}},
          open({self.record!r}, "w"))
""")
        self.work = os.path.join(self.tmp, "work dir")
        os.makedirs(self.work)

    def write_launch(self, *args: str, **extra: str) -> str:
        quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)
        shim = os.path.join(SCRIPTS, "ovis-shims")
        proc = self.sourced(f'write_launch "{self.work}" {quoted}', OVIS_CODEX_WRAPPER=self.fake,
                            PATH=f"{shim}:{os.environ['PATH']}:{shim}", **extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def run_launch(self, path: str) -> dict:
        # The pane's environment is herdr's, not the caller's: nearly empty.
        subprocess.run([BASH, path], env={"PATH": "/usr/bin:/bin", "HOME": self.tmp},
                       check=True, timeout=60)
        with open(self.record, encoding="utf-8") as fh:
            return json.load(fh)

    def test_private_self_deleting_and_faithful(self) -> None:
        path = self.write_launch("-m", "glm-5.3", "it's a \"prompt\" with $HOME",
                                 CODEX_NET="restricted", CODEX_HOOK_TRUST="strict")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        seen = self.run_launch(path)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(seen["cwd"], self.work)
        self.assertEqual(seen["argv"], ["-m", "glm-5.3", "it's a \"prompt\" with $HOME"])
        self.assertEqual(seen["env"]["CODEX_NET"], "restricted")
        self.assertEqual(seen["env"]["CODEX_HOOK_TRUST"], "strict")
        self.assertNotIn(os.path.join(SCRIPTS, "ovis-shims"), seen["env"]["PATH"].split(":"))

    def test_never_carries_a_secret(self) -> None:
        path = self.write_launch(DEEPSEEK_API_KEY="sk-leak", CODEX_ROUTER_TOKEN="tok-leak",
                                 OVIS_TOKEN="ovis-leak", ZAI_API_KEY="zai-leak")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for secret in ("sk-leak", "tok-leak", "ovis-leak", "zai-leak"):
            self.assertNotIn(secret, text)
        os.unlink(path)


class PaneShell(Isolated):
    """scripts/ovis-shims/pane-shell, herdr's shell for every new pane, in a copy of
    the tree with a recorder for the `codex` shim and for bash."""

    def setUp(self) -> None:
        super().setUp()
        self.tree = os.path.join(self.tmp, "ws")
        shims = os.path.join(self.tree, "scripts", "ovis-shims")
        os.makedirs(shims)
        shutil.copy(os.path.join(SCRIPTS, "ovis-shims", "pane-shell"), os.path.join(shims, "pane-shell"))
        shutil.copy(OVIS, os.path.join(self.tree, "scripts", "ovis.sh"))
        self.record = os.path.join(self.tmp, "record")
        executable(os.path.join(shims, "codex"), f'#!/usr/bin/env bash\necho "codex $*" >> {self.record!r}\n')
        self.bash = os.path.join(self.tmp, "fake-bash")
        executable(self.bash, f'#!/usr/bin/env bash\necho "bash $* SHELL=$SHELL" >> {self.record!r}\n')

    def run_shell(self, *args: str, **extra: str) -> list[str]:
        subprocess.run([BASH, os.path.join(self.tree, "scripts", "ovis-shims", "pane-shell"), *args],
                       env=self.env(OVIS_PANE_BASH=self.bash, **extra), check=True, timeout=60,
                       stdin=subprocess.DEVNULL)
        with open(self.record, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_codex_first_then_an_interactive_bash(self) -> None:
        self.assertEqual(self.run_shell(), ["codex ", "bash -i SHELL=/bin/bash"])

    def test_login_flag_is_still_a_pane(self) -> None:
        self.assertEqual(self.run_shell("-l"), ["codex ", "bash -i SHELL=/bin/bash"])

    def test_a_command_gets_plain_bash(self) -> None:
        lines = self.run_shell("-c", "echo hi")
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("bash -c echo hi "), lines)

    def test_plain_panes(self) -> None:
        self.assertEqual(self.run_shell(OVIS_PANE_PLAIN="1"), ["bash -i SHELL=/bin/bash"])
        os.unlink(self.record)
        self.assertEqual(self.run_shell(OVIS_CODEX_PANE="off"), ["bash -i SHELL=/bin/bash"])
        os.unlink(self.record)
        self.write_secrets("OVIS_CODEX_PANE=0\n")
        self.assertEqual(self.run_shell(), ["bash -i SHELL=/bin/bash"])


class HerdrConfig(Isolated):
    """ensure_herdr_config: herdr must not type a resume command into a pane the
    pane shell already started Codex in."""

    def config(self, text: str | None) -> tuple[str, subprocess.CompletedProcess[str]]:
        path = os.path.join(self.tmp, "herdr", "config.toml")
        if text is not None:
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        proc = self.sourced("ensure_herdr_config", OVIS_HERDR_CONFIG=path)
        with open(path, encoding="utf-8") as fh:
            return fh.read(), proc

    def test_created(self) -> None:
        self.assertEqual(self.config(None)[0], "[session]\nresume_agents_on_restore = false\n")

    def test_added_to_the_existing_table(self) -> None:
        text, _ = self.config("[ui]\nx = 1\n[session]\nother = 2\n")
        self.assertEqual(text, "[ui]\nx = 1\n[session]\nresume_agents_on_restore = false\nother = 2\n")

    def test_an_explicit_choice_is_kept_and_true_is_warned_about(self) -> None:
        text, proc = self.config("[session]\nresume_agents_on_restore = true\n")
        self.assertEqual(text, "[session]\nresume_agents_on_restore = true\n")
        self.assertIn("two Codex starts", proc.stderr)


class OvisPatch(unittest.TestCase):
    """scripts/ovis_patch.py against the bundle's original files (the .orig copies the
    patch keeps beside the installed bundle; skipped where it is not installed)."""

    FILES = ("server/skills/codex.ts", "server/workspace-routes.ts", "dist/index.html",
             "server/core/composer.ts", "server/core/pending.ts", "server/pane-routes.ts")
    PATCH = os.path.join(SCRIPTS, "ovis_patch.py")

    def setUp(self) -> None:
        installed = os.path.join(REPO, "tools", "ovis")
        self.tmp = tempfile.mkdtemp(prefix="test-ovis-patch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for rel in self.FILES:
            source = os.path.join(installed, rel + ".orig")
            if not os.path.exists(source):
                source = os.path.join(installed, rel)
            if not os.path.exists(source):
                self.skipTest("Ovis bundle not installed (./codex ovis install)")
            os.makedirs(os.path.dirname(os.path.join(self.tmp, rel)), exist_ok=True)
            shutil.copy(source, os.path.join(self.tmp, rel))
        with open(os.path.join(self.tmp, "bundle.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": "40"}, fh)
        if "codex-workspace:" in self.read("server/skills/codex.ts"):
            self.skipTest("no original copy of the bundle files to patch")

    def read(self, rel: str) -> str:
        with open(os.path.join(self.tmp, rel), encoding="utf-8") as fh:
            return fh.read()

    def patch(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, self.PATCH, self.tmp], capture_output=True, text=True, timeout=60)

    def test_applies_once(self) -> None:
        self.assertEqual(self.patch().stdout, "changed\n")
        self.assertEqual(self.patch().stdout, "unchanged\n")
        codex = self.read("server/skills/codex.ts")
        self.assertIn('builtins: ["model", "presets", "daybreak", ', codex)
        self.assertIn('pin: "0.166.0",', codex)
        self.assertIn('title: [...TERMINAL, { match: "other", interaction: "none" }],', codex)
        self.assertIn('env: { OVIS_PANE_PLAIN: "1" },', self.read("server/workspace-routes.ts"))
        self.assertIn('<script src="/codex-workspace-defaults.js"></script>', self.read("dist/index.html"))
        self.assertIn('"ovis:new-session-agent", "codex"', self.read("dist/codex-workspace-defaults.js"))
        self.assertIn('arrivedAt += await waitForCodexReady(paneId, authorize);', self.read("server/core/composer.ts"))
        self.assertIn('arrivedAt += await waitForCodexReady(paneId, () => authorizePending(owner, paneId, lease));',
                      self.read("server/core/pending.ts"))
        self.assertIn("export async function waitForCodexReady", self.read("server/codex-ready.ts"))
        index = self.read("dist/index.html")
        routes = self.read("server/pane-routes.ts")
        with open(os.path.join(REPO, "patches", "ovis", "deepseek-balance.js"), "rb") as fh:
            balance_version = hashlib.sha256(fh.read()).hexdigest()[:12]
        # versioned by content: Ovis caches root files for a day
        self.assertIn(f'<script src="/deepseek-balance.js?v={balance_version}"></script>', index)
        self.assertIn('import { deepseekBalanceRoute, BALANCE_PATH } from "./deepseek-balance.ts";', routes)
        self.assertLess(routes.index("if (pathname === BALANCE_PATH)"), routes.index('pathname === "/api/pane/conversation")'))
        self.assertIn("export async function deepseekBalanceRoute", self.read("server/deepseek-balance.ts"))
        self.assertIn("__codexWorkspaceBalance", self.read("dist/deepseek-balance.js"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".codex-workspace-patched")))

    def test_refuses_another_bundle_and_changes_nothing(self) -> None:
        with open(os.path.join(self.tmp, "bundle.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": "41"}, fh)
        before = {rel: self.read(rel) for rel in self.FILES}
        proc = self.patch()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("these edits are for 40", proc.stderr)
        self.assertEqual({rel: self.read(rel) for rel in self.FILES}, before)

    def test_refuses_a_changed_file_before_writing_any(self) -> None:
        with open(os.path.join(self.tmp, "server/workspace-routes.ts"), "a", encoding="utf-8") as fh:
            fh.write("// changed upstream\n")
        before = self.read("server/skills/codex.ts")
        proc = self.patch()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("server/workspace-routes.ts is neither", proc.stderr)
        self.assertEqual(self.read("server/skills/codex.ts"), before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "dist/codex-workspace-defaults.js")))


class DeepSeekBalance(unittest.TestCase):
    """patches/ovis/deepseek-balance.{ts,js}: the server module under the bundle's bun, the
    pill's formatting under node (tests/ovis-deepseek-balance*.test.*)."""

    BUN = os.path.join(REPO, "tools", "ovis", "bin", "bun")

    def test_server_module(self) -> None:
        if not os.access(self.BUN, os.X_OK):
            self.skipTest("Ovis bundle not installed (./codex ovis install)")
        proc = subprocess.run([self.BUN, "test", os.path.join(REPO, "tests", "ovis-deepseek-balance.test.ts")],
                              capture_output=True, text=True, cwd=REPO, timeout=600)
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        self.assertRegex(output, r"\b[1-9]\d* pass\b")
        self.assertRegex(output, r"\b0 fail\b")

    def test_browser_pill(self) -> None:
        proc = subprocess.run(["node", "--test", os.path.join(REPO, "tests", "ovis-deepseek-balance-client.test.mjs")],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"# pass [1-9]\d*")

    def test_route_is_not_claimed_by_ovis(self) -> None:
        dispatch = os.path.join(REPO, "tools", "ovis", "server", "dispatch.ts")
        if not os.path.exists(dispatch):
            self.skipTest("Ovis bundle not installed (./codex ovis install)")
        with open(dispatch, encoding="utf-8") as fh:
            self.assertNotIn('"/api/deepseek', fh.read())

    def test_supervisor_passes_the_key_read_as_text_and_the_shared_cache(self) -> None:
        with open(os.path.join(SCRIPTS, "ovis.sh"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn('DEEPSEEK_API_KEY="$(cfg DEEPSEEK_API_KEY)"', script)
        # the same file the ./codex wrapper's footer daemon writes
        self.assertIn('DEEPSEEK_BALANCE_CACHE_FILE="$ROOT/.codex/cache/deepseek-balance.json"', script)
        with open(os.path.join(REPO, "codex"), encoding="utf-8") as fh:
            self.assertIn('DEEPSEEK_BALANCE_CACHE_FILE="$ROOT/.codex/cache/deepseek-balance.json"', fh.read())
        self.assertIn('DEEPSEEK_BALANCE_LIB="$ROOT/plugins/deepseek-balance/lib/deepseek-balance.mjs"', script)
        self.assertTrue(os.path.exists(os.path.join(REPO, "plugins", "deepseek-balance", "lib", "deepseek-balance.mjs")))


class Shim(Isolated):
    def test_removes_itself_and_runs_the_wrapper(self) -> None:
        tree = os.path.join(self.tmp, "ws")
        shim_dir = os.path.join(tree, "scripts", "ovis-shims")
        os.makedirs(shim_dir)
        shutil.copy(SHIM, os.path.join(shim_dir, "codex"))
        record = os.path.join(self.tmp, "record")
        executable(os.path.join(tree, "codex"),
                   f'#!/usr/bin/env bash\nprintf "%s\\n" "$PATH" "$@" > {record!r}\n')
        env = self.env(PATH=f"{shim_dir}:/usr/bin:{shim_dir}:/bin")
        subprocess.run([BASH, os.path.join(shim_dir, "codex"), "resume", "--last"],
                       env=env, check=True, timeout=60)
        with open(record, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines, ["/usr/bin:/bin", "resume", "--last"])


class Hook(Isolated):
    def run_hook(self, stdin: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([BASH, HOOK, "session"], input=stdin, capture_output=True, text=True,
                              env=self.env(**extra), timeout=60)

    def test_nothing_outside_herdr(self) -> None:
        proc = self.run_hook('{"hook_event_name":"SessionStart"}')
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_nothing_without_the_bundle(self) -> None:
        proc = self.run_hook("{}", HERDR_ENV="1", OVIS_DIR=os.path.join(self.tmp, "none"))
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_runs_herdrs_hook_inside_a_pane(self) -> None:
        bundle = os.path.join(self.tmp, "bundle")
        record = os.path.join(self.tmp, "record")
        executable(os.path.join(bundle, "integrations", "herdr-codex-agent-state.sh"),
                   f'#!/bin/sh\n{{ echo "$1"; cat; }} > {record!r}\n')
        proc = self.run_hook('{"session_id":"s1"}', HERDR_ENV="1", OVIS_DIR=bundle)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(record, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), 'session\n{"session_id":"s1"}')

    def test_wired_and_trusted(self) -> None:
        with open(os.path.join(REPO, ".codex", "hooks.json"), encoding="utf-8") as fh:
            hooks = json.load(fh)["hooks"]["SessionStart"]
        commands = [h["command"] for group in hooks for h in group["hooks"]]
        self.assertIn('bash "$CODEX_HOME/../scripts/ovis-codex-hook.sh" session', commands)
        with open(os.path.join(REPO, ".codex", "config.toml"), encoding="utf-8") as fh:
            self.assertIn('/.codex/hooks.json:session_start:0:0"]', fh.read())


class Install(Isolated):
    def fake_bundle(self, pin: str) -> str:
        bundle = os.path.join(self.tmp, "bundle")
        executable(os.path.join(bundle, "bin", "herdr"), '#!/bin/sh\necho "herdr 0.9.3"\n')
        executable(os.path.join(bundle, "bin", "bun"), "#!/bin/sh\necho 1.4.2\n")
        executable(os.path.join(bundle, "integrations", "herdr-codex-agent-state.sh"), "#!/bin/sh\n")
        os.makedirs(os.path.join(bundle, "server"), exist_ok=True)
        open(os.path.join(bundle, "server", "index.ts"), "w").close()
        with open(os.path.join(bundle, ".pin"), "w", encoding="utf-8") as fh:
            fh.write(pin + "\n")
        return bundle

    def pinned(self) -> str:
        return self.sourced('read -r plat want < <(bundle_platform); pin_line "$plat" "$want"').stdout.strip()

    def test_check_accepts_the_pin_only(self) -> None:
        bundle = self.fake_bundle(self.pinned())
        proc = self.ovis("install", "--check", OVIS_DIR=bundle)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("remote-v40", proc.stdout)

        self.fake_bundle("remote-v39 linux-x64 " + "0" * 64)
        proc = self.ovis("install", "--check", OVIS_DIR=bundle)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("./codex ovis install", proc.stderr)

    def test_check_fails_when_missing(self) -> None:
        proc = self.ovis("install", "--check", OVIS_DIR=os.path.join(self.tmp, "none"))
        self.assertNotEqual(proc.returncode, 0)


class CodexReady(unittest.TestCase):
    """patches/ovis/codex-ready.ts against a log database with Codex's own schema and index,
    run by the bundle's bun (skipped where the bundle is not installed)."""

    BUN = os.path.join(REPO, "tools", "ovis", "bin", "bun")
    SOURCE = os.path.join(REPO, "patches", "ovis", "codex-ready.ts")
    # it imports its neighbours in the bundle, so it runs from where ovis_patch.py installs it
    MODULE = os.path.join(REPO, "tools", "ovis", "server", "codex-ready.ts")
    MARK = ("app_server.thread_start.notify_started{otel.name=\"app_server.thread_start.notify_started\"}: "
            "app-server event: thread/started targeted_connections=0")

    def setUp(self) -> None:
        if not os.access(self.BUN, os.X_OK) or not os.path.exists(self.MODULE):
            self.skipTest("Ovis bundle not installed and patched (./codex ovis install, ./codex ovis start)")
        with open(self.SOURCE, encoding="utf-8") as a, open(self.MODULE, encoding="utf-8") as b:
            self.assertEqual(a.read(), b.read(), "the installed codex-ready.ts is not patches/ovis/codex-ready.ts; run scripts/ovis_patch.py")
        self.home = tempfile.mkdtemp(prefix="test-codex-ready-")
        self.addCleanup(shutil.rmtree, self.home, True)
        import sqlite3
        db = sqlite3.connect(os.path.join(self.home, "logs_2.sqlite"))
        db.executescript("""
            CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, ts_nanos INTEGER NOT NULL,
              level TEXT NOT NULL, target TEXT NOT NULL, feedback_log_body TEXT, module_path TEXT, file TEXT, line INTEGER,
              thread_id TEXT, process_uuid TEXT, estimated_bytes INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX idx_logs_process_uuid_threadless_ts ON logs(process_uuid, ts DESC, ts_nanos DESC, id DESC)
              WHERE thread_id IS NULL;
        """)
        rows = [
            (100, "codex_tui::app::startup", "tui startup initial frame scheduled"),
            (100, "codex_app_server::outgoing_message", self.MARK),
            (200, "codex_tui::app::startup", "tui startup initial frame scheduled"),
            (300, "codex_app_server::outgoing_message", self.MARK),
        ]
        for pid, target, body in rows:
            thread = "t1" if pid == 300 else None
            db.execute("INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body, thread_id, process_uuid) "
                       "VALUES (1, 0, 'TRACE', ?, ?, ?, ?)", (target, body, thread, f"pid:{pid}:uuid"))
        db.commit()
        db.close()
        open(os.path.join(self.home, "logs_1.sqlite"), "w").close()

    def readiness(self, *pids: int) -> list[str]:
        script = (f'const m = await import({json.dumps(self.MODULE)}); const db = m.codexLogDatabase({json.dumps(self.home)});'
                  f' console.log(JSON.stringify([db.split("/").pop(), ...{json.dumps(list(pids))}.map((p) => m.codexReadiness(db, p))]));')
        out = subprocess.run([self.BUN, "-e", script], capture_output=True, text=True, timeout=60,
                             cwd=os.path.join(REPO, "tools", "ovis"))
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_ready_starting_unknown(self) -> None:
        # 100 announced its thread; 200 logs but has not; 300 has only a thread's row, outside the
        # rows the startup announcement is among, and 400 logs nothing here: neither says anything
        self.assertEqual(self.readiness(100, 200, 300, 400), ["logs_2.sqlite", "ready", "starting", "unknown", "unknown"])

    def test_pid_prefix_is_exact(self) -> None:
        # pid 10 must not match pid:100's rows
        self.assertEqual(self.readiness(10, 1), ["logs_2.sqlite", "unknown", "unknown"])

    def test_without_the_index_nothing_is_held(self) -> None:
        import sqlite3
        db = sqlite3.connect(os.path.join(self.home, "logs_2.sqlite"))
        db.execute("DROP INDEX idx_logs_process_uuid_threadless_ts")
        db.commit()
        db.close()
        self.assertEqual(self.readiness(100, 200), ["logs_2.sqlite", "unknown", "unknown"])


class Relay(unittest.TestCase):
    """scripts/ovis_forward.py between a client and an upstream that echoes."""

    @staticmethod
    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def setUp(self) -> None:
        self.upstream = socket.socket()
        self.upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.upstream.bind(("127.0.0.1", 0))
        self.upstream.listen()
        threading.Thread(target=self.echo, daemon=True).start()
        self.port = self.free_port()
        self.proc = subprocess.Popen([sys.executable, RELAY, "127.0.0.1", str(self.port),
                                      "127.0.0.1", str(self.upstream.getsockname()[1])],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertIn("ovis-forward", self.proc.stdout.readline())

    def tearDown(self) -> None:
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.upstream.close()

    def echo(self) -> None:
        while True:
            try:
                conn, _ = self.upstream.accept()
            except OSError:
                return
            with conn:
                data = b""
                while chunk := conn.recv(65536):
                    data += chunk
                # Answer only after the client's half-close arrived.
                conn.sendall(b"ECHO:" + data)

    def test_bytes_unchanged_and_half_close_passed_on(self) -> None:
        payload = os.urandom(3 * 1024 * 1024)
        with socket.create_connection(("127.0.0.1", self.port), timeout=30) as c:
            c.sendall(payload)
            c.shutdown(socket.SHUT_WR)
            got = b""
            while chunk := c.recv(65536):
                got += chunk
        self.assertEqual(got, b"ECHO:" + payload)

    def test_upstream_down_closes_the_client(self) -> None:
        port, dead = self.free_port(), self.free_port()
        proc = subprocess.Popen([sys.executable, RELAY, "127.0.0.1", str(port), "127.0.0.1", str(dead)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            self.assertIn("ovis-forward", proc.stdout.readline())
            with socket.create_connection(("127.0.0.1", port), timeout=10) as c:
                self.assertEqual(c.recv(1), b"")
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
