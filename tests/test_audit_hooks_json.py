from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON_PATH = REPO_ROOT / "hooks" / "hooks.json"
AUDIT_EVENTS = ("SessionStart", "Stop", "PostToolUse", "PostToolUseFailure")


def _load_hooks_json() -> dict[str, Any]:
    assert HOOKS_JSON_PATH.is_file(), "hooks/hooks.json must exist"
    raw = HOOKS_JSON_PATH.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert isinstance(data, dict)
    # Check 2-space indent formatting
    assert raw == json.dumps(data, indent=2) + "\n"
    return data


def _audit_group_for_event(hooks: dict[str, Any], event: str) -> dict[str, Any]:
    groups = hooks[event]
    assert isinstance(groups, list)
    if event in ("SessionStart", "Stop"):
        assert len(groups) == 2
        return groups[1]
    assert len(groups) == 1
    return groups[0]


def test_existing_entries_unchanged() -> None:
    data = _load_hooks_json()
    hooks = data["hooks"]

    session_start_groups = hooks["SessionStart"]
    assert len(session_start_groups) >= 1
    assert session_start_groups[0] == {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
            {
                "type": "command",
                "command": "${CLAUDE_PLUGIN_ROOT}/bin/agent-hub hook session-start",
                "timeout": 10,
            }
        ],
    }

    stop_groups = hooks["Stop"]
    assert len(stop_groups) >= 1
    assert stop_groups[0] == {
        "hooks": [
            {
                "type": "command",
                "command": "${CLAUDE_PLUGIN_ROOT}/bin/agent-hub hook stop",
                "timeout": 10,
            }
        ],
    }


def test_audit_events_registered() -> None:
    data = _load_hooks_json()
    hooks = data["hooks"]
    assert set(hooks.keys()) == set(AUDIT_EVENTS)

    for event in AUDIT_EVENTS:
        group = _audit_group_for_event(hooks, event)
        group_hooks = group.get("hooks")
        assert isinstance(group_hooks, list)
        assert len(group_hooks) == 1
        cmd = group_hooks[0].get("command", "")
        assert cmd.endswith(f"--host claude {event}")


def test_audit_groups_use_star_matcher_and_timeout_5() -> None:
    data = _load_hooks_json()
    hooks = data["hooks"]

    for event in AUDIT_EVENTS:
        group = _audit_group_for_event(hooks, event)
        assert group.get("matcher") == "*"
        group_hooks = group.get("hooks")
        assert isinstance(group_hooks, list)
        assert len(group_hooks) == 1
        entry = group_hooks[0]
        assert entry.get("type") == "command"
        assert entry.get("timeout") == 5


def test_audit_commands_use_isolated_python_not_agent_hub_bin() -> None:
    data = _load_hooks_json()
    hooks = data["hooks"]

    for event in AUDIT_EVENTS:
        group = _audit_group_for_event(hooks, event)
        entry = group["hooks"][0]
        cmd = entry["command"]
        expected = f"python3 -I ${{CLAUDE_PLUGIN_ROOT}}/hooks/audit-hook.py --host claude {event}"
        assert cmd == expected
        assert "python3 -I " in cmd
        assert "bin/agent-hub" not in cmd


def test_audit_script_exists() -> None:
    data = _load_hooks_json()
    hooks = data["hooks"]

    for event in AUDIT_EVENTS:
        group = _audit_group_for_event(hooks, event)
        cmd = group["hooks"][0]["command"]
        tokens = cmd.split()
        script_token = next(t for t in tokens if "${CLAUDE_PLUGIN_ROOT}" in t)
        rel_path = script_token.replace("${CLAUDE_PLUGIN_ROOT}/", "")
        script_path = REPO_ROOT / rel_path
        assert script_path.is_file(), f"Audit script {script_path} must exist"
