from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agenthub.audit.writer import (
    append,
    audit_root,
    event_hash,
    sanitize,
    write,
)


def _get_single_day_file(root: Path) -> Path:
    jsonl_files = list(root.glob("*/*.jsonl"))
    assert len(jsonl_files) == 1, f"Expected 1 day file, found {jsonl_files}"
    return jsonl_files[0]


def test_secret_absent_from_file_bytes(tmp_path: Path) -> None:
    raw_secret = "sec-tok-9876543210fedcba"
    bearer_token = f"Bearer {raw_secret}"
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": f"api-call {bearer_token}",
        "quote": f"Executing with {bearer_token} inside",
        "detail": {
            "argv": ["curl", "-H", f"Authorization: {bearer_token}", "https://api.example.com"],
            "stderr_tail": f"Failed authentication: {bearer_token}",
            "nested": {
                "auth_header": bearer_token,
                "items": [f"item with {bearer_token}", 123],
            },
        },
        "outcome": {"status": "ok"},
    }

    append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    file_bytes = day_file.read_bytes()

    assert raw_secret.encode("utf-8") not in file_bytes
    assert b"Bearer <redacted>" in file_bytes
    assert b"<redacted>" in file_bytes


def test_quote_truncated_to_200_with_ellipsis(tmp_path: Path) -> None:
    long_quote = "A" * 250
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": "echo test",
        "quote": long_quote,
        "outcome": {"status": "ok"},
    }

    append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    stored_event = json.loads(day_file.read_text(encoding="utf-8").strip())

    assert len(stored_event["quote"]) == 200
    assert stored_event["quote"].endswith("…")
    assert stored_event["quote"] == ("A" * 199) + "…"


def test_target_truncated_to_500(tmp_path: Path) -> None:
    long_target = "B" * 600
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": long_target,
        "outcome": {"status": "ok"},
    }

    append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    stored_event = json.loads(day_file.read_text(encoding="utf-8").strip())

    assert len(stored_event["target"]) == 500
    assert stored_event["target"] == "B" * 500


def test_stderr_tail_keeps_last_2000_chars(tmp_path: Path) -> None:
    long_tail = "HEAD_" + ("C" * 2500) + "_TAIL"
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": "cat error.log",
        "detail": {
            "stderr_tail": long_tail,
        },
        "outcome": {"status": "error", "exit_code": 1},
    }

    append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    stored_event = json.loads(day_file.read_text(encoding="utf-8").strip())

    stored_tail = stored_event["detail"]["stderr_tail"]
    assert len(stored_tail) == 2000
    assert stored_tail.endswith("_TAIL")
    assert not stored_tail.startswith("HEAD_")
    assert stored_tail == long_tail[-2000:]


def test_hash_covers_redacted_text(tmp_path: Path) -> None:
    raw_secret = "sec-tok-1122334455667788"
    bearer_token = f"Bearer {raw_secret}"
    long_tail = "ERROR: " + bearer_token + " " + ("X" * 2200)
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": f"run {bearer_token}",
        "quote": f"Running with {bearer_token} " + ("Q" * 250),
        "detail": {
            "argv": ["run", bearer_token],
            "stderr_tail": long_tail,
            "extra": f"more {bearer_token}",
        },
        "outcome": {"status": "ok"},
    }

    res = append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    stored_event = json.loads(day_file.read_text(encoding="utf-8").strip())

    assert res["hash"] == stored_event["hash"]
    assert stored_event["hash"] == event_hash(stored_event["prev_hash"], stored_event)
    assert stored_event["hash"] != event_hash(stored_event["prev_hash"], event)


def test_short_clean_fields_unchanged(tmp_path: Path) -> None:
    actor = {"role": "director", "host": "agy", "session_id": "s-123"}
    parent = {"plan": "plan-1", "task": "task-2"}
    outcome = {"status": "ok", "exit_code": 0, "duration_ms": 120}
    evidence = "sha256:abcd1234ef567890abcd1234ef567890abcd1234ef567890abcd1234ef567890"
    detail = {
        "argv": ["git", "status", "--short"],
        "stderr_tail": "short error message",
        "count": 5,
        "is_ok": True,
        "tags": ["alpha", "beta"],
    }
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": actor,
        "parent": parent,
        "kind": "command",
        "target": "git status --short",
        "quote": "Short clean quote without secrets",
        "evidence": evidence,
        "detail": detail,
        "outcome": outcome,
    }

    res = append(event, state_dir=tmp_path)

    day_file = _get_single_day_file(audit_root(tmp_path))
    stored_event = json.loads(day_file.read_text(encoding="utf-8").strip())

    assert res["hash"] == stored_event["hash"]
    assert stored_event["actor"] == actor
    assert stored_event["parent"] == parent
    assert stored_event["outcome"] == outcome
    assert stored_event["evidence"] == evidence
    assert stored_event["target"] == "git status --short"
    assert stored_event["quote"] == "Short clean quote without secrets"
    assert stored_event["detail"] == detail


def test_sanitize_returns_new_dict_and_does_not_mutate() -> None:
    raw_secret = "sec-tok-abcdefabcdef1234"
    detail: dict[str, Any] = {
        "argv": ["test", f"Bearer {raw_secret}"],
        "nested": {"key": f"Bearer {raw_secret}"},
    }
    event: dict[str, Any] = {
        "target": f"cmd {raw_secret}",
        "quote": f"Bearer {raw_secret}",
        "detail": detail,
    }

    sanitized = sanitize(event)

    assert sanitized is not event
    assert sanitized["detail"] is not detail
    assert sanitized["detail"]["nested"] is not detail["nested"]
    assert raw_secret in event["target"]
    assert raw_secret in event["detail"]["nested"]["key"]
    assert raw_secret not in sanitized["target"]
    assert raw_secret not in sanitized["detail"]["nested"]["key"]


def test_write_entrypoint_applies_sanitization(tmp_path: Path) -> None:
    raw_secret = "sec-tok-writeentrypoint1"
    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "worker", "host": "claude"},
        "kind": "command",
        "target": f"run Bearer {raw_secret}",
        "outcome": {"status": "ok"},
    }

    ok = write(event, state_dir=tmp_path)
    assert ok is True

    day_file = _get_single_day_file(audit_root(tmp_path))
    file_bytes = day_file.read_bytes()
    assert raw_secret.encode("utf-8") not in file_bytes
    assert b"Bearer <redacted>" in file_bytes


def test_argv_tuple_and_nested_sanitization() -> None:
    raw_secret = "sec-tok-argvtuple123456"
    event = {
        "detail": {
            "argv": ("app", "--token", raw_secret),
            "other_argv": ["run", {"token": f"--token={raw_secret}"}],
        }
    }
    sanitized = sanitize(event)
    assert sanitized["detail"]["argv"] == ("app", "--token", "<redacted>")
    assert sanitized["detail"]["other_argv"] == ["run", {"token": "--token=<redacted>"}]
