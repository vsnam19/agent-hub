import fcntl
import json
import re
import time
from pathlib import Path

from agenthub.audit.writer import (
    audit_root,
    write,
)


def test_clean_write_emits_no_gap(tmp_path: Path) -> None:
    event = {
        "ts": "2026-10-19T12:00:00.000Z",
        "actor": {"role": "director", "host": "agy", "session_id": "conv-1"},
        "kind": "tool",
        "target": "read",
        "outcome": {"status": "ok"},
    }
    ok = write(event, state_dir=tmp_path)
    assert ok is True

    pending_file = tmp_path / "audit-pending-gap.jsonl"
    assert not pending_file.exists()

    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-19.jsonl"
    assert day_file.is_file()
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    recorded = json.loads(lines[0])
    assert recorded["kind"] == "tool"
    assert recorded["seq"] == 1
    assert recorded["actor"]["session_id"] == "conv-1"


def test_write_never_raises_when_audit_dir_unwritable(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit"
    audit_file.write_text("blocked", encoding="utf-8")

    event = {
        "ts": "2026-10-19T12:00:00.000Z",
        "actor": {"role": "director", "host": "agy", "session_id": "conv-1"},
        "kind": "tool",
        "target": "read",
        "outcome": {"status": "ok"},
    }
    ok = write(event, state_dir=tmp_path)
    assert ok is False


def test_failure_records_pending_file_outside_audit_dir(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit"
    audit_file.write_text("blocked", encoding="utf-8")

    ts1 = "2026-10-19T10:00:00.000Z"
    event1 = {
        "ts": ts1,
        "actor": {"role": "director", "host": "agy", "session_id": "conv-1"},
        "kind": "tool",
        "target": "read",
        "outcome": {"status": "ok"},
    }
    ok1 = write(event1, state_dir=tmp_path)
    assert ok1 is False

    pending_file = tmp_path / "audit-pending-gap.jsonl"
    assert pending_file.is_file()

    lines = pending_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record1 = json.loads(lines[0])
    assert record1["ts"] == ts1
    assert "reason" in record1
    assert record1["reason"]

    # Subsequent failure appends a second line to pending file
    ts2 = "2026-10-19T10:01:00.000Z"
    event2 = {
        "ts": ts2,
        "actor": {"role": "director", "host": "agy", "session_id": "conv-1"},
        "kind": "tool",
        "target": "write",
        "outcome": {"status": "ok"},
    }
    ok2 = write(event2, state_dir=tmp_path)
    assert ok2 is False

    lines2 = pending_file.read_text(encoding="utf-8").splitlines()
    assert len(lines2) == 2
    record2 = json.loads(lines2[1])
    assert record2["ts"] == ts2
    assert record2["reason"]


def test_next_write_emits_gap_before_event_and_removes_pending(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit"
    audit_file.write_text("blocked", encoding="utf-8")

    failed_event = {
        "ts": "2026-10-19T09:00:00.000Z",
        "actor": {"role": "director", "host": "agy", "session_id": "conv-1"},
        "kind": "tool",
        "target": "failing_cmd",
        "outcome": {"status": "ok"},
    }
    assert write(failed_event, state_dir=tmp_path) is False

    pending_file = tmp_path / "audit-pending-gap.jsonl"
    assert pending_file.is_file()

    # Recover by removing the blocking file
    audit_file.unlink()

    success_event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "director", "host": "agy", "session_id": "conv-1"},
        "kind": "tool",
        "target": "successful_cmd",
        "outcome": {"status": "ok"},
    }
    assert write(success_event, state_dir=tmp_path) is True

    # Pending file must be removed
    assert not pending_file.exists()

    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-19.jsonl"
    assert day_file.is_file()
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    gap_ev = json.loads(lines[0])
    ev2 = json.loads(lines[1])

    assert gap_ev["kind"] == "gap"
    assert gap_ev["actor"] == {"role": "agent-hub", "host": "agent-hub"}
    assert gap_ev["target"] == ""
    assert gap_ev["outcome"] == {"status": "ok"}
    assert gap_ev["seq"] == 1

    assert ev2["kind"] == "tool"
    assert ev2["target"] == "successful_cmd"
    assert ev2["seq"] == 2
    assert ev2["prev_hash"] == gap_ev["hash"]


def test_gap_has_from_first_pending_and_lost_count(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit"
    audit_file.write_text("blocked", encoding="utf-8")

    ts1 = "2026-10-19T00:01:00.000Z"
    ts2 = "2026-10-19T00:02:00.000Z"
    ts3 = "2026-10-19T00:03:00.000Z"

    assert write({"ts": ts1, "kind": "tool", "target": "1"}, state_dir=tmp_path) is False
    assert write({"ts": ts2, "kind": "tool", "target": "2"}, state_dir=tmp_path) is False
    assert write({"ts": ts3, "kind": "tool", "target": "3"}, state_dir=tmp_path) is False

    pending_file = tmp_path / "audit-pending-gap.jsonl"
    assert pending_file.is_file()
    lines = pending_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    first_pending = json.loads(lines[0])

    # Recover
    audit_file.unlink()

    event = {
        "ts": "2026-10-19T10:00:00.000Z",
        "actor": {"role": "director", "host": "agy", "session_id": "conv-1"},
        "kind": "tool",
        "target": "resume",
        "outcome": {"status": "ok"},
    }
    assert write(event, state_dir=tmp_path) is True
    assert not pending_file.exists()

    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-19.jsonl"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    gap_ev = json.loads(lines[0])

    assert gap_ev["kind"] == "gap"
    assert gap_ev["detail"]["from"] == ts1
    assert gap_ev["detail"]["reason"] == first_pending["reason"]
    assert gap_ev["detail"]["lost"] == 3
    assert gap_ev["detail"]["to"] >= ts1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", gap_ev["detail"]["to"])


def test_lock_timeout_returns_false_and_records_pending(tmp_path: Path) -> None:
    ts = "2026-10-19T00:10:00.000Z"
    day = "2026-10-19"
    month = "2026-10"

    month_dir = audit_root(tmp_path) / month
    month_dir.mkdir(parents=True, exist_ok=True)
    lock_file = month_dir / f"{day}.jsonl.lock"
    lock_file.touch()

    with lock_file.open("r+b") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            start = time.monotonic()
            ok = write(
                {
                    "ts": ts,
                    "actor": {"role": "director", "host": "agy", "session_id": "conv-1"},
                    "kind": "tool",
                    "target": "search",
                },
                state_dir=tmp_path,
            )
            elapsed = time.monotonic() - start
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    assert ok is False
    assert 0.18 <= elapsed <= 0.35

    pending_file = tmp_path / "audit-pending-gap.jsonl"
    assert pending_file.is_file()
    lines = pending_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ts"] == ts
    assert "timed out acquiring audit lock" in record["reason"]
