from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agenthub.audit.normalize import file_sha256, normalize, scrub
from agenthub.audit.trace import trace_env
from agenthub.audit.writer import sanitize

# Real captured payload from:
# claude -p 'run: echo hi' --allowedTools Bash
# with a PostToolUse hook `cat > <scratch>/payload.json` (home path replaced with /tmp/x).
REAL_CLAUDE_POST_TOOL_USE_PAYLOAD: dict[str, Any] = {
    "session_id": "d376e73f-f8af-4eb8-b73d-29d2672d7737",
    "transcript_path": "/tmp/x/.claude/projects/-tmp-tmpqnq7nmth/d376e73f-f8af-4eb8-b73d-29d2672d7737.jsonl",
    "cwd": "/tmp/tmpqnq7nmth",
    "prompt_id": "74eec515-af83-4fae-850a-84f371e9bb86",
    "permission_mode": "auto",
    "effort": {"level": "medium"},
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "echo hi", "description": "Print hi"},
    "tool_response": {
        "stdout": "hi",
        "stderr": "",
        "interrupted": False,
        "isImage": False,
        "noOutputExpected": False,
    },
    "tool_use_id": "toolu_015uMxJsQW9kE6HKBFMR1Tir",
    "duration_ms": 232,
}


def test_claude_bash_real_payload_becomes_command_event(tmp_path: Path) -> None:
    events = normalize("claude", "PostToolUse", REAL_CLAUDE_POST_TOOL_USE_PAYLOAD, {})
    assert len(events) == 1
    ev = events[0]

    assert ev["actor"] == {
        "role": "director",
        "host": "claude",
        "session_id": "d376e73f-f8af-4eb8-b73d-29d2672d7737",
    }
    assert ev["parent"] == {}
    assert ev["kind"] == "command"
    assert ev["target"] == "echo hi"
    assert ev["detail"] == {"argv": ["echo", "hi"], "cwd": "/tmp/tmpqnq7nmth"}
    assert ev["outcome"] == {"status": "ok"}
    assert ev["quote"] == "Print hi"

    for chain_key in ("seq", "prev_hash", "hash", "ts"):
        assert chain_key not in ev
    assert "stderr_tail" not in ev["detail"]
    assert "tool_response" not in ev

    # Other hosts return empty list here
    assert normalize("codex", "PostToolUse", REAL_CLAUDE_POST_TOOL_USE_PAYLOAD, {}) == []
    assert normalize("agy", "PostToolUse", REAL_CLAUDE_POST_TOOL_USE_PAYLOAD, {}) == []

    # Outward command produces both command and outward events sharing actor and parent
    outward_payload = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "tool_input": {"command": "cd /tmp/x && git push origin main", "description": "Push main"},
    }
    outward_events = normalize("claude", "PostToolUse", outward_payload, {})
    assert len(outward_events) == 2
    assert outward_events[0]["kind"] == "command"
    assert outward_events[1]["kind"] == "outward"
    assert outward_events[1]["target"] == "git push origin main"
    assert outward_events[1]["actor"] == outward_events[0]["actor"]
    assert outward_events[1]["parent"] == outward_events[0]["parent"]

    # File event computes sha256_after within budget, never sha256_before, never file contents
    target_file = tmp_path / "sample.txt"
    file_bytes = b"confidential file body 12345"
    target_file.write_bytes(file_bytes)
    expected_sha = hashlib.sha256(file_bytes).hexdigest()

    assert file_sha256(target_file) == expected_sha
    assert file_sha256(tmp_path / "missing.txt") is None
    assert file_sha256(target_file, budget_ms=0) is None
    assert file_sha256(tmp_path) is None

    write_payload = {
        "session_id": "sess-file",
        "cwd": str(tmp_path),
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(target_file),
            "content": "confidential file body 12345",
        },
    }
    file_events = normalize("claude", "PostToolUse", write_payload, {})
    assert len(file_events) == 1
    fev = file_events[0]
    assert fev["kind"] == "file_write"
    assert fev["target"] == str(target_file)
    assert fev["detail"] == {
        "path": str(target_file),
        "op": "write",
        "sha256_after": expected_sha,
    }
    assert "sha256_before" not in fev["detail"]
    assert "confidential file body 12345" not in json.dumps(fev)


def test_claude_failure_event_has_error_status_and_stderr_tail() -> None:
    prefix = "HEAD_NOISE_" * 50
    tail = "ERR_TAIL_LINE_" + ("x" * 1986)
    assert len(tail) == 2000
    full_error = prefix + tail

    failure_payload = {
        "session_id": "e7ea0d0c-700d-4358-98fe-664acb1ed150",
        "cwd": "/tmp/x",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "false", "description": "Run the false command"},
        "error": full_error,
        "is_interrupt": False,
        "duration_ms": 167,
    }
    events = normalize("claude", "PostToolUseFailure", failure_payload, {})
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "command"
    assert ev["outcome"] == {"status": "error"}
    assert ev["detail"]["stderr_tail"] == tail
    assert len(ev["detail"]["stderr_tail"]) == 2000
    assert prefix not in ev["detail"]["stderr_tail"]

    for chain_key in ("seq", "prev_hash", "hash", "ts"):
        assert chain_key not in ev

    # Success event never includes stderr_tail even if error or stderr exists in payload
    success_with_stderr = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "error": "some leftover error",
        "tool_response": {"stdout": "hi", "stderr": "some warning on stderr"},
    }
    ok_events = normalize("claude", "PostToolUse", success_with_stderr, {})
    assert len(ok_events) == 1
    assert ok_events[0]["outcome"] == {"status": "ok"}
    assert "stderr_tail" not in ok_events[0]["detail"]


def test_claude_quote_from_description() -> None:
    long_desc = "D" * 260
    with_desc = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "tool_input": {"command": "echo hi", "description": long_desc},
    }
    events = normalize("claude", "PostToolUse", with_desc, {})
    assert len(events) == 1
    assert events[0]["quote"] == long_desc
    sanitized = sanitize(events[0])
    assert sanitized["quote"] == ("D" * 199) + "…"
    assert len(sanitized["quote"]) == 200

    without_desc = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "tool_input": {"command": "echo hi"},
    }
    events_no_desc = normalize("claude", "PostToolUse", without_desc, {})
    assert len(events_no_desc) == 1
    assert "quote" not in events_no_desc[0]

    empty_desc = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "tool_input": {"command": "echo hi", "description": ""},
    }
    events_empty = normalize("claude", "PostToolUse", empty_desc, {})
    assert len(events_empty) == 1
    assert "quote" not in events_empty[0]


def test_quote_cut_to_200_with_ellipsis() -> None:
    # 200 chars kept whole
    desc_200 = "A" * 200
    events_200 = normalize(
        "claude",
        "PostToolUse",
        {**REAL_CLAUDE_POST_TOOL_USE_PAYLOAD, "tool_input": {"command": "echo hi", "description": desc_200}},
        {},
    )
    assert len(events_200) == 1
    assert events_200[0]["quote"] == desc_200
    sanitized_200 = sanitize(events_200[0])
    assert sanitized_200["quote"] == desc_200
    assert len(sanitized_200["quote"]) == 200

    # 201 chars kept full in normalize, cut to 199 chars + ellipsis in writer sanitize
    desc_201 = ("B" * 199) + "XY"
    events_201 = normalize(
        "claude",
        "PostToolUse",
        {**REAL_CLAUDE_POST_TOOL_USE_PAYLOAD, "tool_input": {"command": "echo hi", "description": desc_201}},
        {},
    )
    assert len(events_201) == 1
    assert events_201[0]["quote"] == desc_201
    sanitized_201 = sanitize(events_201[0])
    assert sanitized_201["quote"] == ("B" * 199) + "…"
    assert len(sanitized_201["quote"]) == 200

    # 350 chars kept full in normalize, cut to 199 chars + ellipsis in writer sanitize
    desc_350 = "C" * 350
    events_350 = normalize(
        "claude",
        "PostToolUse",
        {**REAL_CLAUDE_POST_TOOL_USE_PAYLOAD, "tool_input": {"command": "echo hi", "description": desc_350}},
        {},
    )
    assert len(events_350) == 1
    assert events_350[0]["quote"] == desc_350
    sanitized_350 = sanitize(events_350[0])
    assert sanitized_350["quote"] == ("C" * 199) + "…"
    assert len(sanitized_350["quote"]) == 200


def test_secret_across_quote_cut_is_redacted() -> None:
    # A secret that spans the 200-character cut (e.g. at index 190-210)
    # If cut before redaction, the secret is truncated and its prefix leaks.
    # normalize() must pass the full description, and sanitize() redacts before cutting.
    secret = "AKIAABCDEFGHIJKLMNOP"
    prefix = "word " * 37  # 185 chars ending in space; secret spans 185..205 across the 200-char boundary
    full_desc = prefix + secret + " ending text exceeding limit"
    payload = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "tool_input": {"command": "echo hi", "description": full_desc},
    }
    events = normalize("claude", "PostToolUse", payload, {})
    assert len(events) == 1
    assert events[0]["quote"] == full_desc

    sanitized = sanitize(events[0])
    assert "AKIA" not in sanitized["quote"]
    assert "<redacted>" in sanitized["quote"]
    assert len(sanitized["quote"]) <= 200
    assert sanitized["quote"].endswith("…")


def test_non_string_command_does_not_raise() -> None:
    # 1. Null command
    null_payload = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "tool_input": {"command": None, "description": "Null command test"},
    }
    null_events = normalize("claude", "PostToolUse", null_payload, {})
    assert len(null_events) == 1
    ev_null = null_events[0]
    assert ev_null["kind"] == "command"
    assert ev_null["target"] == ""
    assert ev_null["detail"]["command_type"] == "NoneType"
    assert ev_null["outcome"] == {"status": "ok"}
    assert ev_null["quote"] == "Null command test"

    # 2. Integer command
    int_payload = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "tool_input": {"command": 12345},
    }
    int_events = normalize("claude", "PostToolUse", int_payload, {})
    assert len(int_events) == 1
    ev_int = int_events[0]
    assert ev_int["kind"] == "command"
    assert ev_int["target"] == ""
    assert ev_int["detail"]["command_type"] == "int"
    assert ev_int["outcome"] == {"status": "ok"}

    # 3. List command
    list_payload = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "tool_input": {"command": ["echo", "hello"]},
    }
    list_events = normalize("claude", "PostToolUse", list_payload, {})
    assert len(list_events) == 1
    ev_list = list_events[0]
    assert ev_list["kind"] == "command"
    assert ev_list["target"] == ""
    assert ev_list["detail"]["command_type"] == "list"
    assert ev_list["outcome"] == {"status": "ok"}

    # 4. Failure with null command carries error status and stderr_tail
    failure_null_payload = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "hook_event_name": "PostToolUseFailure",
        "tool_input": {"command": None},
        "error": "command is required",
    }
    fail_events = normalize("claude", "PostToolUseFailure", failure_null_payload, {})
    assert len(fail_events) == 1
    ev_fail = fail_events[0]
    assert ev_fail["kind"] == "command"
    assert ev_fail["target"] == ""
    assert ev_fail["detail"]["command_type"] == "NoneType"
    assert ev_fail["detail"]["stderr_tail"] == "command is required"
    assert ev_fail["outcome"] == {"status": "error"}


def test_claude_thinking_blocks_and_transcript_content_absent(tmp_path: Path) -> None:
    transcript_file = tmp_path / "transcript.jsonl"
    transcript_secret = "SECRET_FROM_TRANSCRIPT_FILE_88271"
    transcript_file.write_text(json.dumps({"secret": transcript_secret}) + "\n", encoding="utf-8")

    thinking_secret = "PRIVATE_MODEL_THINKING_TEXT_1192"
    reasoning_secret = "PRIVATE_MODEL_REASONING_TEXT_3341"
    signature_secret = "PRIVATE_THOUGHT_SIGNATURE_5562"
    response_secret = "TOOL_RESPONSE_STDOUT_BODY_7783"

    payload = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "transcript_path": str(transcript_file),
        "transcriptPath": str(transcript_file),
        "thinking": thinking_secret,
        "ExtendedThinking": [{"text": thinking_secret, "thought_signature": signature_secret}],
        "reasoning_content": reasoning_secret,
        " nested ": {
            "my_Reasoning_block": reasoning_secret,
            "block_signature": signature_secret,
        },
        "tool_input": {
            "command": "echo hi",
            "description": "Print hi",
            "thinking_budget": thinking_secret,
            "reasoning": reasoning_secret,
            "signature": signature_secret,
        },
        "tool_response": {
            "stdout": response_secret,
            "stderr": response_secret,
            "thinking_trace": thinking_secret,
        },
    }

    scrubbed = scrub(payload)
    scrubbed_json = json.dumps(scrubbed)
    assert thinking_secret not in scrubbed_json
    assert reasoning_secret not in scrubbed_json
    assert signature_secret not in scrubbed_json
    assert "transcript_path" not in scrubbed
    assert "transcriptPath" not in scrubbed

    events = normalize("claude", "PostToolUse", payload, {})
    assert len(events) == 1
    events_json = json.dumps(events)

    assert transcript_secret not in events_json
    assert thinking_secret not in events_json
    assert reasoning_secret not in events_json
    assert signature_secret not in events_json
    assert response_secret not in events_json
    assert str(transcript_file) not in events_json


def test_claude_session_start_and_stop_events() -> None:
    start_payload = {
        "session_id": "e7ea0d0c-700d-4358-98fe-664acb1ed150",
        "transcript_path": "/tmp/x/.claude/projects/-tmp/e7ea0d0c.jsonl",
        "cwd": "/tmp/x",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    start_events = normalize("claude", "SessionStart", start_payload, {})
    assert len(start_events) == 1
    sev = start_events[0]
    assert sev["kind"] == "session"
    assert sev["detail"] == {"event": "start"}
    assert sev["outcome"] == {"status": "ok"}
    assert sev["actor"] == {
        "role": "director",
        "host": "claude",
        "session_id": "e7ea0d0c-700d-4358-98fe-664acb1ed150",
    }
    assert sev["parent"] == {}
    for chain_key in ("seq", "prev_hash", "hash", "ts"):
        assert chain_key not in sev

    stop_payload = {
        "session_id": "e7ea0d0c-700d-4358-98fe-664acb1ed150",
        "transcript_path": "/tmp/x/.claude/projects/-tmp/e7ea0d0c.jsonl",
        "cwd": "/tmp/x",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "Final assistant reply that should not be copied",
    }
    stop_events = normalize("claude", "Stop", stop_payload, {})
    assert len(stop_events) == 1
    tev = stop_events[0]
    assert tev["kind"] == "session"
    assert tev["detail"] == {"event": "stop"}
    assert tev["outcome"] == {"status": "ok"}
    assert tev["actor"] == {
        "role": "director",
        "host": "claude",
        "session_id": "e7ea0d0c-700d-4358-98fe-664acb1ed150",
    }
    assert tev["parent"] == {}
    assert "Final assistant reply" not in json.dumps(tev)
    for chain_key in ("seq", "prev_hash", "hash", "ts"):
        assert chain_key not in tev


def test_claude_worker_actor_from_trace_env() -> None:
    env = trace_env(
        plan="phase-g",
        task="audit-normalize-claude",
        job="20261019-015030-implement-hard-3d32",
        attempt=1,
        role="implement-hard",
    )
    payload_with_model = {
        **REAL_CLAUDE_POST_TOOL_USE_PAYLOAD,
        "model": "claude-sonnet-4-6",
    }
    events = normalize("claude", "PostToolUse", payload_with_model, env)
    assert len(events) == 1
    ev = events[0]
    assert ev["actor"] == {
        "role": "worker",
        "host": "claude",
        "session_id": "d376e73f-f8af-4eb8-b73d-29d2672d7737",
        "agent": "claude:claude-sonnet-4-6",
        "pool_role": "implement-hard",
    }
    assert ev["parent"] == {
        "plan": "phase-g",
        "task": "audit-normalize-claude",
        "job": "20261019-015030-implement-hard-3d32",
        "attempt": 1,
    }
