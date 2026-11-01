#!/usr/bin/env python3
"""Summarize Codex Security session tokens and context-source growth."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass
class Totals:
    input: int = 0
    cached: int = 0
    output: int = 0
    reasoning: int = 0
    total: int = 0


@dataclass
class SessionUsage:
    path: str
    thread_id: str
    parent_thread_id: str | None
    role: str
    depth: int
    model: str | None
    calls: int = 0
    first_input: int = 0
    last_input: int = 0
    max_input: int = 0
    tokens: Totals = field(default_factory=Totals)
    command_chars: dict[str, int] = field(
        default_factory=lambda: {"total": 0, "references": 0, "generated": 0, "target": 0}
    )

    @property
    def growth(self) -> float:
        return self.last_input / self.first_input if self.first_input else 0.0


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _command_text(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _source_category(command: str) -> str:
    if "CODEX_SECURITY_SCAN_DIR" in command:
        return "generated"
    if "CODEX_SECURITY_PLUGIN_ROOT" in command or "/codex-security/" in command:
        if "/references/" in command or "/skills/" in command or "/schemas/" in command:
            return "references"
    return "target"


def inspect_session(path: Path) -> SessionUsage:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if isinstance(record, dict):
                records.append(record)

    meta = next(
        (_object(record.get("payload")) for record in records if record.get("type") == "session_meta"),
        {},
    )
    source = meta.get("source")
    role = meta.get("agent_role") or "root"
    depth = 0
    if isinstance(source, dict):
        subagent = _object(source.get("subagent"))
        spawn = _object(subagent.get("thread_spawn"))
        role = role if role != "root" else spawn.get("agent_role", role)
        if role == "root" and isinstance(spawn.get("agent_path"), str):
            role = spawn["agent_path"].rstrip("/").rsplit("/", 1)[-1]
        depth = _int(spawn.get("depth"))

    usage = SessionUsage(
        path=str(path),
        thread_id=str(meta.get("id") or path.stem),
        parent_thread_id=(
            str(meta["parent_thread_id"]) if isinstance(meta.get("parent_thread_id"), str) else None
        ),
        role=str(role),
        depth=depth,
        model=str(meta["model"]) if isinstance(meta.get("model"), str) else None,
    )

    for record in records:
        payload = _object(record.get("payload"))
        if record.get("type") == "token_usage_record":
            token_usage = _object(payload.get("usage"))
            input_tokens = _int(token_usage.get("input_tokens"))
            usage.calls += 1
            usage.tokens.input += input_tokens
            usage.tokens.cached += _int(token_usage.get("cached_input_tokens"))
            usage.tokens.output += _int(token_usage.get("output_tokens"))
            usage.tokens.reasoning += _int(token_usage.get("reasoning_output_tokens"))
            usage.tokens.total += _int(token_usage.get("total_tokens"))
            if usage.first_input == 0:
                usage.first_input = input_tokens
            usage.last_input = input_tokens
            usage.max_input = max(usage.max_input, input_tokens)

        if record.get("type") != "event_msg" or payload.get("type") != "item_completed":
            continue
        item = _object(payload.get("item"))
        if item.get("type") != "CommandExecution":
            continue
        output_chars = len(item.get("aggregated_output") or "")
        category = _source_category(_command_text(item.get("command")))
        usage.command_chars["total"] += output_chars
        usage.command_chars[category] += output_chars

    return usage


def _contains_scan_id(path: Path, scan_id: str) -> bool:
    needle = scan_id.encode("utf-8")
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            if needle in chunk:
                return True
    return False


def _descendants(sessions: list[SessionUsage], roots: set[str]) -> set[str]:
    selected = set(roots)
    changed = True
    while changed:
        changed = False
        for session in sessions:
            if session.parent_thread_id in selected and session.thread_id not in selected:
                selected.add(session.thread_id)
                changed = True
    return selected


def _sum(sessions: Iterable[SessionUsage], field_name: str) -> int:
    return sum(getattr(session.tokens, field_name) for session in sessions)


def _summary(sessions: list[SessionUsage]) -> dict[str, Any]:
    tokens = {name: _sum(sessions, name) for name in ("input", "cached", "output", "reasoning", "total")}
    uncached = tokens["input"] - tokens["cached"]
    command = {
        name: sum(session.command_chars[name] for session in sessions)
        for name in ("total", "references", "generated", "target")
    }
    return {
        "sessions": len(sessions),
        "tokens": tokens,
        "uncachedInput": uncached,
        "cachedInputPercent": round(tokens["cached"] * 100 / tokens["input"], 2)
        if tokens["input"]
        else 0.0,
        "commandChars": command,
        "referenceCommandPercent": round(command["references"] * 100 / command["total"], 2)
        if command["total"]
        else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=Path(".codex/state/cs/codex-home/sessions"),
        help="Codex Security session root (default: %(default)s)",
    )
    parser.add_argument("--scan-id", help="Include sessions mentioning this scan ID and descendants")
    parser.add_argument("--root-session", action="append", default=[], help="Include a thread ID and descendants")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    paths = sorted(args.sessions_dir.rglob("*.jsonl"))
    sessions = [inspect_session(path) for path in paths]

    selected_ids = set(args.root_session)
    if args.scan_id:
        selected_ids.update(
            session.thread_id for session, path in zip(sessions, paths) if _contains_scan_id(path, args.scan_id)
        )
    if selected_ids:
        selected_ids = _descendants(sessions, selected_ids)
        sessions = [session for session in sessions if session.thread_id in selected_ids]

    sessions.sort(key=lambda session: (session.parent_thread_id is not None, session.thread_id))
    result = {
        "summary": _summary(sessions),
        "sessions": [
            {
                **asdict(session),
                "growth": round(session.growth, 2),
            }
            for session in sessions
        ],
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    summary = result["summary"]
    tokens = summary["tokens"]
    command = summary["commandChars"]
    print(
        f"Sessions {summary['sessions']} | calls {sum(s.calls for s in sessions)} | "
        f"input {tokens['input']:,} | cached {tokens['cached']:,} "
        f"({summary['cachedInputPercent']:.1f}%) | uncached {summary['uncachedInput']:,} | "
        f"output {tokens['output']:,} | total {tokens['total']:,}"
    )
    print(
        f"Command output {command['total']:,} chars | references {command['references']:,} "
        f"({summary['referenceCommandPercent']:.1f}%) | generated {command['generated']:,} | "
        f"target/other {command['target']:,}"
    )
    for session in sessions:
        print(
            f"{session.role:<12} depth={session.depth} calls={session.calls:<3} "
            f"input={session.tokens.input:>9,} cached={session.tokens.cached:>9,} "
            f"context={session.first_input:,}->{session.last_input:,} "
            f"({session.growth:.1f}x) refs={session.command_chars['references']:,} "
            f"{session.thread_id}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
