import json

from scripts.codex_security_usage import inspect_session


def _write_jsonl(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_inspect_session_tracks_tokens_context_and_command_categories(tmp_path):
    session = tmp_path / "rollout.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "root",
                    "model": "deepseek-flash",
                    "agent_role": "root",
                },
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 4,
                        "total_tokens": 110,
                    }
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": ["sed", "$CODEX_SECURITY_PLUGIN_ROOT/references/core-scan.md"],
                        "aggregated_output": "reference",
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": ["cat", "app.py"],
                        "aggregated_output": "target-source",
                    },
                },
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "usage": {
                        "input_tokens": 300,
                        "cached_input_tokens": 250,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 320,
                    }
                },
            },
        ],
    )

    usage = inspect_session(session)

    assert usage.thread_id == "root"
    assert usage.calls == 2
    assert usage.first_input == 100
    assert usage.last_input == 300
    assert usage.max_input == 300
    assert usage.tokens.input == 400
    assert usage.tokens.cached == 330
    assert usage.tokens.output == 30
    assert usage.tokens.total == 430
    assert usage.command_chars == {
        "total": 22,
        "references": 9,
        "generated": 0,
        "target": 13,
    }


def test_inspect_session_handles_missing_usage_and_command_data(tmp_path):
    session = tmp_path / "minimal.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "child",
                    "parent_thread_id": "root",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "depth": 1,
                                "agent_path": "/root/baseline_auditor",
                            }
                        }
                    },
                },
            },
            {"type": "event_msg", "payload": "not-an-object"},
        ],
    )

    usage = inspect_session(session)

    assert usage.thread_id == "child"
    assert usage.parent_thread_id == "root"
    assert usage.role == "baseline_auditor"
    assert usage.calls == 0
    assert usage.tokens.total == 0
    assert usage.command_chars["total"] == 0
