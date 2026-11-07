#!/usr/bin/env python3
"""Teach the bundled Ovis about this workspace's Codex.

    python3 scripts/ovis_patch.py [OVIS_DIR]        (default tools/ovis)

Prints `changed` when it wrote anything (the server must restart to load it),
`unchanged` when everything was already in place. Exits 2, changing nothing,
when the bundle or a file is not the one these edits were written against.

What it does (measured 2026-11-01 against Codex 0.166.0 and bundle 40):

  server/skills/codex.ts        Ovis's Codex command table is pinned to
                                rust-v0.163.0. 0.166.0 adds /presets (saved
                                prompts and slash commands, a terminal picker)
                                and lets /model, /permissions, /title and
                                /statusline take an argument that acts without
                                a picker (tui/src/slash_command.rs,
                                chatwidget/slash_dispatch.rs). The table and its
                                pin follow 0.166.0.
  server/workspace-routes.ts    Ovis's new workspaces and tabs are plain panes
                                (OVIS_PANE_PLAIN), so its agent picker decides:
                                scripts/ovis-shims/pane-shell starts Codex in
                                every other new pane.
  dist/index.html               loads codex-workspace-defaults.js: the picker
                                defaults to Codex until this browser chose.
  server/codex-ready.ts         NEW. Whether a Codex process's session thread
                                is up, from Codex's own log database.
  dist/voice-narration.js       NEW. Speaks agent replies (assistant text, never
  server/voice-narration.ts     NEW. tool calls) in voiceapi's default voice;
  server/pane-routes.ts         routes /api/narration/* (README "Voice API").
  dist/deepseek-balance.js      NEW. The DeepSeek balance in the composer's
  server/deepseek-balance.ts    NEW. model pill, from the Codex footer's cache
                                or DeepSeek; /api/deepseek/balance
                                (README "DeepSeek balance").
  server/core/composer.ts       a chat message and a queued delivery to a Codex
  server/core/pending.ts        pane wait for that thread (at most 180 s), as
                                anything typed before it loses the session
                                ("No active thread is available").

Applied at every `./codex ovis start`, so a git pull updates it without a
reinstall. Each edited file is pinned to the sha256 of the bundle's original:
an Ovis upgrade that changes one is refused here, loudly, rather than
half-patched. Re-pin these hashes when OVIS_BUNDLE_TAG moves.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys

BUNDLE_VERSION = "40"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH_DIR = os.path.join(REPO, "patches", "ovis")
# (file in patches/ovis, where it goes in the bundle)
NEW_FILES = (
    ("codex-workspace-defaults.js", "dist/codex-workspace-defaults.js"),
    ("codex-ready.ts", "server/codex-ready.ts"),
    ("voice-narration.ts", "server/voice-narration.ts"),
    ("voice-narration.js", "dist/voice-narration.js"),
    ("deepseek-balance.ts", "server/deepseek-balance.ts"),
    ("deepseek-balance.js", "dist/deepseek-balance.js"),
)
MARK = "codex-workspace:"


def _content_version(name: str) -> str:
    """First 12 hex of a patch file's sha256: Ovis serves root files with max-age=86400, so the
    page names the script by its content and an edited script is a new URL, never a stale cache."""
    with open(os.path.join(PATCH_DIR, name), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


NARRATION_VERSION = _content_version("voice-narration.js")
BALANCE_VERSION = _content_version("deepseek-balance.js")
EXIT_REFUSED = 2
# Touched whenever anything here changed: ovis.sh restarts a server older than it.
STAMP = ".codex-workspace-patched"

EDITS: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "server/skills/codex.ts",
        "635eb17d3ca960fd697c341d2f97ff89c20d645e7fa806e32ec869e3605b67f4",
        [
            (
                '  builtins: ["model", "daybreak", ',
                '  // codex-workspace: /presets is new in rust-v0.166.0, second in its popup (scripts/ovis_patch.py)\n'
                '  builtins: ["model", "presets", "daybreak", ',
            ),
            (
                '    model: "Choose what model and reasoning effort to use", ',
                '    model: "Choose what model and reasoning effort to use", '
                'presets: "Run or save reusable prompts and slash commands", ',
            ),
            (
                '    model: [{ match: "bare", interaction: "dialog", dialog: "codex-model" }],\n',
                '    model: [{ match: "bare", interaction: "dialog", dialog: "codex-model" },\n'
                '      // codex-workspace: rust-v0.166.0 applies `/model <slug> [effort]` without a picker\n'
                '      { match: "other", interaction: "none" }],\n'
                '    // codex-workspace: rust-v0.166.0, bare opens the picker, an argument acts and returns\n'
                '    // (chatwidget/slash_dispatch.rs: handle_terminal_title_command_args,\n'
                '    // handle_status_line_command_args, select_permission_profile)\n'
                '    title: [...TERMINAL, { match: "other", interaction: "none" }],\n'
                '    statusline: [...TERMINAL, { match: "other", interaction: "none" }],\n'
                '    permissions: [...TERMINAL, { match: "other", interaction: "none" }],\n',
            ),
            (
                '  terminalBound: ["resume", "permissions", ',
                '  // codex-workspace: /presets opens the saved prompts picker (chatwidget/quick_dials.rs)\n'
                '  terminalBound: ["presets", "resume", "permissions", ',
            ),
            (
                '  pin: "0.163.0",\n',
                '  // codex-workspace: the table above follows rust-v0.166.0 (scripts/ovis_patch.py)\n'
                '  pin: "0.166.0",\n',
            ),
        ],
    ),
    (
        "server/workspace-routes.ts",
        "e99735d862bf394d1b436268ecf7525818152cd5841ac89ba795424fe734c652",
        [(
            '        ...(typeof payload.label === "string" ? { label: payload.label } : {}),\n'
            '      };\n'
            '      const created = inWorkspace\n',
            '        ...(typeof payload.label === "string" ? { label: payload.label } : {}),\n'
            '        // codex-workspace: the picker decides, not the pane shell (scripts/ovis-shims/pane-shell starts\n'
            '        // Codex in every other pane): Shell is a shell, and Codex is started below by agent.start\n'
            '        env: { OVIS_PANE_PLAIN: "1" },\n'
            '      };\n'
            '      const created = inWorkspace\n',
        )],
    ),
    (
        "server/core/composer.ts",
        "56ad96e50c5e577cc41d59c050e92dd59c4e28c12a75e5f572fc6de39ddfc1c2",
        [
            (
                'import { userHome } from "../user-home.ts";\n',
                'import { userHome } from "../user-home.ts";\n'
                '// codex-workspace: hold a send until the pane\'s Codex has its session thread (patches/ovis)\n'
                'import { waitForCodexReady } from "../codex-ready.ts";\n',
            ),
            (
                '        const identity = pane ? pendingIdentity(pane) : null;\n',
                '        // codex-workspace: text typed before Codex\'s session thread is up loses the session\n'
                '        // ("No active thread is available"); the time spent waiting does not count against the deadline\n'
                '        if (pane && resolveAgent(pane).agent === "codex") arrivedAt += await waitForCodexReady(paneId, authorize);\n'
                '        const identity = pane ? pendingIdentity(pane) : null;\n',
            ),
        ],
    ),
    (
        "server/core/pending.ts",
        "d9623e7aa58f72553bc62d2366d4325ab724e9c3722a76224c39cb92929bea96",
        [
            (
                'import { herdrSkillAttachIo, type PromptPiece, typeWithSkills } from "../opencode-skill-attach.ts";\n',
                'import { herdrSkillAttachIo, type PromptPiece, typeWithSkills } from "../opencode-skill-attach.ts";\n'
                '// codex-workspace: hold a delivery until the pane\'s Codex has its session thread (patches/ovis)\n'
                'import { waitForCodexReady } from "../codex-ready.ts";\n',
            ),
            (
                '      const context = await pendingContext(owner, paneId, lease, identity);\n',
                '      const context = await pendingContext(owner, paneId, lease, identity);\n'
                '      // codex-workspace: as in core/composer.ts, never type into a Codex whose session thread is not up\n'
                '      if (context.identity.agent === "codex") arrivedAt += await waitForCodexReady(paneId, () => authorizePending(owner, paneId, lease));\n',
            ),
        ],
    ),
    (
        "dist/index.html",
        "31220cd3ace10ea556b085bedfe1fa529f272e05a70e48add5d4cacbba4295e6",
        [(
            '    <meta charset="UTF-8" />\n',
            '    <meta charset="UTF-8" />\n'
            '    <!-- codex-workspace: the New workspace / New tab picker defaults to Codex (patches/ovis) -->\n'
            '    <script src="/codex-workspace-defaults.js"></script>\n'
            '    <!-- codex-workspace: speak agent replies through voiceapi; before the app, to see its socket -->\n'
            f'    <script src="/voice-narration.js?v={NARRATION_VERSION}"></script>\n'
            '    <!-- codex-workspace: the DeepSeek balance beside the composer model pill -->\n'
            f'    <script src="/deepseek-balance.js?v={BALANCE_VERSION}"></script>\n',
        )],
    ),
    (
        "server/pane-routes.ts",
        "2148ed95a61766b4ede91a10bfbd10dc08906b363f7f8d7ee2b69248e9adb9ba",
        [
            (
                'import { conversationImage, ConversationReadPending, ConversationUnavailable, forgetPaneTranscriptState, '
                'HistoryChanged, paneConversation, paneProcessRoots, paneTranscript, toolOutput } from "./conversation.ts";\n',
                'import { conversationImage, ConversationReadPending, ConversationUnavailable, forgetPaneTranscriptState, '
                'HistoryChanged, paneConversation, paneProcessRoots, paneTranscript, toolOutput } from "./conversation.ts";\n'
                '// codex-workspace: reply narration through voiceapi (patches/ovis/voice-narration.ts)\n'
                'import { voiceNarrationRoute } from "./voice-narration.ts";\n'
                '// codex-workspace: the DeepSeek balance for the composer pill (patches/ovis/deepseek-balance.ts)\n'
                'import { deepseekBalanceRoute, BALANCE_PATH } from "./deepseek-balance.ts";\n',
            ),
            (
                '  if (pathname === "/api/pane/conversation") {\n',
                '  // codex-workspace: /api/narration/* lists reply segments and streams their speech (patches/ovis)\n'
                '  if (pathname.startsWith("/api/narration/")) {\n'
                '    setRequestRoute(pathname);\n'
                '    return voiceNarrationRoute(pathname, url, request, {\n'
                '      readConversation: (paneId) => paneConversation(paneId, options.codexHome, {}, '
                'options.devinDbPath, options.opencodeDb, options.hermesHome, options.grokHome, options.museData),\n'
                '    });\n'
                '  }\n'
                '  if (pathname === BALANCE_PATH) {\n'
                '    setRequestRoute(pathname);\n'
                '    return deepseekBalanceRoute(pathname, request);\n'
                '  }\n'
                '\n'
                '  if (pathname === "/api/pane/conversation") {\n',
            ),
        ],
    ),
]


class Refused(Exception):
    pass


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write(path: str, text: str) -> None:
    tmp = f"{path}.codex-workspace.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def base_text(path: str, rel: str, original: str, edits: list[tuple[str, str]]) -> str | None:
    """What to patch: the file itself, or its .orig when it carries an older
    version of these edits. None when it is already current."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise Refused(f"{rel}: {exc.strerror}") from exc
    if all(new in text for _, new in edits):
        return None
    if sha256(text) == original:
        return text
    backup = f"{path}.orig"
    if MARK in text and os.path.exists(backup):
        with open(backup, encoding="utf-8") as fh:
            saved = fh.read()
        if sha256(saved) == original:
            return saved
    raise Refused(f"{rel} is neither the bundle's original nor patched by us (sha256 {sha256(text)[:12]})")


def patched_text(rel: str, text: str, edits: list[tuple[str, str]]) -> str:
    for old, new in edits:
        if text.count(old) != 1:
            raise Refused(f"{rel}: anchor found {text.count(old)} times: {old[:60]!r}")
        text = text.replace(old, new)
    return text


def patch_file(ovis: str, rel: str, original: str, edits: list[tuple[str, str]]) -> bool:
    path = os.path.join(ovis, rel)
    text = base_text(path, rel, original, edits)
    if text is None:
        return False
    text = patched_text(rel, text, edits)
    backup = f"{path}.orig"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    write(path, text)
    return True


def copy_new(ovis: str, name: str, rel: str) -> bool:
    with open(os.path.join(PATCH_DIR, name), encoding="utf-8") as fh:
        want = fh.read()
    dst = os.path.join(ovis, rel)
    if os.path.exists(dst):
        with open(dst, encoding="utf-8") as fh:
            if fh.read() == want:
                return False
    with open(dst + ".codex-workspace.tmp", "w", encoding="utf-8") as fh:
        fh.write(want)
    os.replace(dst + ".codex-workspace.tmp", dst)
    return True


def apply(ovis: str) -> bool:
    try:
        with open(os.path.join(ovis, "bundle.json"), encoding="utf-8") as fh:
            version = str(json.load(fh).get("version"))
    except (OSError, ValueError) as exc:
        raise Refused(f"no readable bundle.json in {ovis}: {exc}") from exc
    if version != BUNDLE_VERSION:
        raise Refused(f"Ovis bundle {version}, these edits are for {BUNDLE_VERSION}")
    # Check every file, anchors included, before writing any, so a refusal changes nothing.
    for rel, original, edits in EDITS:
        text = base_text(os.path.join(ovis, rel), rel, original, edits)
        if text is not None:
            patched_text(rel, text, edits)
    changed = False
    for name, rel in NEW_FILES:
        changed |= copy_new(ovis, name, rel)
    for rel, original, edits in EDITS:
        changed |= patch_file(ovis, rel, original, edits)
    stamp = os.path.join(ovis, STAMP)
    if changed or not os.path.exists(stamp):
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write("patched by scripts/ovis_patch.py\n")
    return changed


def main(argv: list[str]) -> int:
    ovis = argv[1] if len(argv) > 1 else os.path.join(REPO, "tools", "ovis")
    try:
        print("changed" if apply(ovis) else "unchanged")
    except Refused as exc:
        print(f"ovis_patch: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
