import json
import tomllib
from pathlib import Path


DEFAULTS = {
    "workers": 2,
    "subagents": 1,
    "stopAfterNoNew": 3,
    "stopAfterConsecutiveErrors": 2,
    "maxDiscoveryRuns": 10,
    "maxTimeHours": 1.5,
}


def test_runtime_defaults_and_local_addition_stay_in_sync():
    runtime = Path(".codex/skills/cxsec-security/runtime/scripts/deep_scan_defaults.json")
    asset = Path(
        ".codex/skills/cxsec-security/assets/local-additions/scripts/deep_scan_defaults.json"
    )

    assert runtime.read_bytes() == asset.read_bytes()
    assert json.loads(runtime.read_text(encoding="utf-8")) == DEFAULTS


def test_local_runtime_config_uses_cost_safe_defaults():
    with Path(".codex/codex-security/config.toml").open("rb") as source:
        document = tomllib.load(source)

    assert document["deep_scan"] == {
        "workers": 2,
        "subagents": 1,
        "stop_after_no_new": 3,
        "stop_after_consecutive_errors": 2,
        "max_discovery_runs": 10,
        "max_time_hours": 1.5,
    }
