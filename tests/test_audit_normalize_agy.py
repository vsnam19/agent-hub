from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agenthub.audit.normalize import normalize, scrub
from agenthub.audit.trace import trace_env

# Real captured payload from agy 1.4.2:
# agy -p "run: echo hi" --dangerously-skip-permissions
# with a PostToolUse hook `cat > <scratch>/payload.json` (home paths replaced with /tmp/x).
REAL_AGY_POST_TOOL_USE_PAYLOAD: dict[str, Any] = {
    "artifactDirectoryPath": "/tmp/x/.gemini/antigravity-cli/brain/4f99fb2c-c829-4ded-a556-16d5e0bf6021",
    "conversationId": "4f99fb2c-c829-4ded-a556-16d5e0bf6021",
    "error": "",
    "modelName": "gemini-4-argon-medium",
    "stepIdx": 2,
    "toolCall": {
        "args": {
            "CommandLine": "echo hi",
            "Cwd": "/tmp/x/Workspaces/tools/agent-hub",
            "WaitMsBeforeAsync": 5000,
            "toolAction": "Running echo hi",
            "toolSummary": "Run echo hi",
        },
        "name": "run_command",
    },
    "transcriptPath": (
        "/tmp/x/.gemini/antigravity-cli/brain/4f99fb2c-c829-4ded-a556-16d5e0bf6021"
        "/.system_generated/logs/transcript_full.jsonl"
    ),
    "workspacePaths": ["/tmp/x/Workspaces/tools/agent-hub"],
}


def test_agy_real_payload_becomes_command_event() -> None:
    events = normalize("agy", "PostToolUse", REAL_AGY_POST_TOOL_USE_PAYLOAD, {})
    assert len(events) == 1
    ev = events[0]

    assert ev["actor"] == {
        "role": "director",
        "host": "agy",
        "session_id": "4f99fb2c-c829-4ded-a556-16d5e0bf6021",
        "model": "gemini-4-argon-medium",
        "agent": "agy:gemini-4-argon-medium",
    }
    assert ev["parent"] == {}
    assert ev["kind"] == "command"
    assert ev["target"] == "echo hi"
    assert ev["detail"] == {"argv": ["echo", "hi"], "cwd": "/tmp/x/Workspaces/tools/agent-hub"}
    assert ev["outcome"] == {"status": "ok"}
    assert "quote" not in ev

    for chain_key in ("seq", "prev_hash", "hash", "ts"):
        assert chain_key not in ev
    assert "stderr_tail" not in ev["detail"]
    assert "toolCall" not in ev
    assert "tool_response" not in ev

    # Other hosts return empty list for agy payload
    assert normalize("claude", "PostToolUse", REAL_AGY_POST_TOOL_USE_PAYLOAD, {}) == []
    assert normalize("codex", "PostToolUse", REAL_AGY_POST_TOOL_USE_PAYLOAD, {}) == []

    # Outward command produces both command and outward events sharing actor and parent
    outward_payload = {
        **REAL_AGY_POST_TOOL_USE_PAYLOAD,
        "toolCall": {
            "name": "run_command",
            "args": {
                "CommandLine": "git push origin main",
                "Cwd": "/tmp/x/Workspaces/tools/agent-hub",
            },
        },
    }
    outward_events = normalize("agy", "PostToolUse", outward_payload, {})
    assert len(outward_events) == 2
    assert outward_events[0]["kind"] == "command"
    assert outward_events[1]["kind"] == "outward"
    assert outward_events[1]["target"] == "git push origin main"
    assert outward_events[1]["actor"] == outward_events[0]["actor"]
    assert outward_events[1]["parent"] == outward_events[0]["parent"]

    # Non-command tool produces kind="tool"
    tool_payload = {
        **REAL_AGY_POST_TOOL_USE_PAYLOAD,
        "toolCall": {
            "name": "view_file",
            "args": {"AbsolutePath": "/tmp/x/file.txt"},
        },
    }
    tool_events = normalize("agy", "PostToolUse", tool_payload, {})
    assert len(tool_events) == 1
    tev = tool_events[0]
    assert tev["kind"] == "tool"
    assert tev["target"] == "view_file"
    assert tev["detail"] == {"tool": "view_file"}
    assert tev["outcome"] == {"status": "ok"}
    assert "quote" not in tev


def test_agy_error_makes_error_status_and_stderr_tail() -> None:
    prefix = "HEAD_NOISE_" * 50
    tail = "ERR_TAIL_LINE_" + ("x" * 1986)
    assert len(tail) == 2000
    full_error = prefix + tail

    failure_payload = {
        **REAL_AGY_POST_TOOL_USE_PAYLOAD,
        "error": full_error,
    }
    events = normalize("agy", "PostToolUse", failure_payload, {})
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "command"
    assert ev["outcome"] == {"status": "error"}
    assert ev["detail"]["stderr_tail"] == tail
    assert len(ev["detail"]["stderr_tail"]) == 2000
    assert prefix not in ev["detail"]["stderr_tail"]

    for chain_key in ("seq", "prev_hash", "hash", "ts"):
        assert chain_key not in ev

    # Success event with empty string error never includes stderr_tail
    success_payload = {
        **REAL_AGY_POST_TOOL_USE_PAYLOAD,
        "error": "",
    }
    ok_events = normalize("agy", "PostToolUse", success_payload, {})
    assert len(ok_events) == 1
    assert ok_events[0]["outcome"] == {"status": "ok"}
    assert "stderr_tail" not in ok_events[0]["detail"]

    # Non-command tool with error has error status but no stderr_tail
    non_cmd_failure = {
        **REAL_AGY_POST_TOOL_USE_PAYLOAD,
        "toolCall": {
            "name": "view_file",
            "args": {"AbsolutePath": "/tmp/x/missing.txt"},
        },
        "error": "FileNotFoundError: [Errno 2] No such file or directory",
    }
    non_cmd_events = normalize("agy", "PostToolUse", non_cmd_failure, {})
    assert len(non_cmd_events) == 1
    assert non_cmd_events[0]["kind"] == "tool"
    assert non_cmd_events[0]["outcome"] == {"status": "error"}
    assert "stderr_tail" not in non_cmd_events[0]["detail"]


def test_agy_event_has_no_quote() -> None:
    payload_with_desc = {
        **REAL_AGY_POST_TOOL_USE_PAYLOAD,
        "description": "Some description text",
        "toolCall": {
            "name": "run_command",
            "args": {
                "CommandLine": "echo hi",
                "description": "Tool call description argument",
                "summary": "Summary text",
            },
        },
    }
    events = normalize("agy", "PostToolUse", payload_with_desc, {})
    assert len(events) == 1
    assert "quote" not in events[0]


def test_agy_thinking_and_transcript_path_ignored(tmp_path: Path) -> None:
    transcript_file = tmp_path / "transcript.jsonl"
    transcript_secret = "SECRET_FROM_AGY_TRANSCRIPT_FILE_99812"
    transcript_file.write_text(json.dumps({"secret": transcript_secret}) + "\n", encoding="utf-8")

    thinking_secret = "PRIVATE_AGY_THINKING_TEXT_7761"
    reasoning_secret = "PRIVATE_AGY_REASONING_TEXT_4432"
    signature_secret = "PRIVATE_AGY_SIGNATURE_2219"

    payload = {
        **REAL_AGY_POST_TOOL_USE_PAYLOAD,
        "transcriptPath": str(transcript_file),
        "transcript_path": str(transcript_file),
        "thinking": thinking_secret,
        "reasoning": reasoning_secret,
        "thought_signature": signature_secret,
        "nested": {
            "deep_thinking": thinking_secret,
            "deep_reasoning": reasoning_secret,
        },
        "toolCall": {
            "name": "run_command",
            "args": {
                "CommandLine": "echo hi",
                "thinking_field": thinking_secret,
                "reasoning_field": reasoning_secret,
            },
        },
    }

    scrubbed = scrub(payload)
    scrubbed_json = json.dumps(scrubbed)
    assert thinking_secret not in scrubbed_json
    assert reasoning_secret not in scrubbed_json
    assert signature_secret not in scrubbed_json
    assert "transcriptPath" not in scrubbed
    assert "transcript_path" not in scrubbed

    events = normalize("agy", "PostToolUse", payload, {})
    assert len(events) == 1
    events_json = json.dumps(events)

    assert transcript_secret not in events_json
    assert thinking_secret not in events_json
    assert reasoning_secret not in events_json
    assert signature_secret not in events_json
    assert str(transcript_file) not in events_json


def test_agy_session_start_and_stop_events() -> None:
    start_payload = {
        "conversationId": "4f99fb2c-c829-4ded-a556-16d5e0bf6021",
        "modelName": "gemini-4-argon-medium",
        "workspacePaths": ["/tmp/x/Workspaces/tools/agent-hub"],
        "artifactDirectoryPath": "/tmp/x/artifacts",
        "transcriptPath": "/tmp/x/transcript.jsonl",
    }
    start_events = normalize("agy", "SessionStart", start_payload, {})
    assert len(start_events) == 1
    sev = start_events[0]
    assert sev["kind"] == "session"
    assert sev["target"] == "start"
    assert sev["detail"] == {"event": "start"}
    assert sev["outcome"] == {"status": "ok"}
    assert sev["actor"] == {
        "role": "director",
        "host": "agy",
        "session_id": "4f99fb2c-c829-4ded-a556-16d5e0bf6021",
        "model": "gemini-4-argon-medium",
        "agent": "agy:gemini-4-argon-medium",
    }
    assert sev["parent"] == {}
    assert "quote" not in sev
    for chain_key in ("seq", "prev_hash", "hash", "ts"):
        assert chain_key not in sev

    stop_payload = {
        "conversationId": "4f99fb2c-c829-4ded-a556-16d5e0bf6021",
        "modelName": "gemini-4-argon-medium",
        "terminationReason": "NO_TOOL_CALL",
        "error": "",
        "executionNum": 1,
        "fullyIdle": True,
    }
    stop_events = normalize("agy", "Stop", stop_payload, {})
    assert len(stop_events) == 1
    tev = stop_events[0]
    assert tev["kind"] == "session"
    assert tev["target"] == "stop"
    assert tev["detail"] == {"event": "stop", "reason": "NO_TOOL_CALL"}
    assert tev["outcome"] == {"status": "ok"}
    assert tev["actor"] == {
        "role": "director",
        "host": "agy",
        "session_id": "4f99fb2c-c829-4ded-a556-16d5e0bf6021",
        "model": "gemini-4-argon-medium",
        "agent": "agy:gemini-4-argon-medium",
    }
    assert tev["parent"] == {}
    assert "quote" not in tev
    for chain_key in ("seq", "prev_hash", "hash", "ts"):
        assert chain_key not in tev

    # Stop event with error status
    stop_error_payload = {
        **stop_payload,
        "terminationReason": "ERROR",
        "error": "Agent process crashed",
    }
    stop_err_events = normalize("agy", "Stop", stop_error_payload, {})
    assert len(stop_err_events) == 1
    assert stop_err_events[0]["kind"] == "session"
    assert stop_err_events[0]["target"] == "stop"
    assert stop_err_events[0]["detail"] == {"event": "stop", "reason": "ERROR"}
    assert stop_err_events[0]["outcome"] == {"status": "error"}


def test_agy_model_and_session_in_actor() -> None:
    env = trace_env(
        plan="phase-g",
        task="audit-normalize-agy",
        job="20261019-020733-implement-8b37",
        attempt=1,
        role="implement",
    )
    payload = {
        **REAL_AGY_POST_TOOL_USE_PAYLOAD,
        "conversationId": "conv-worker-1234",
        "modelName": "gemini-3.8-flash-high",
    }
    events = normalize("agy", "PostToolUse", payload, env)
    assert len(events) == 1
    ev = events[0]
    assert ev["actor"] == {
        "role": "worker",
        "host": "agy",
        "session_id": "conv-worker-1234",
        "model": "gemini-3.8-flash-high",
        "agent": "agy:gemini-3.8-flash-high",
        "pool_role": "implement",
    }
    assert ev["parent"] == {
        "plan": "phase-g",
        "task": "audit-normalize-agy",
        "job": "20261019-020733-implement-8b37",
        "attempt": 1,
    }
