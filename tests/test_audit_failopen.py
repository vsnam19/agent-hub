import fcntl
import json
import re
import time
from pathlib import Path

import pytest

from agenthub.audit import writer
from agenthub.audit.writer import (
    audit_root,
    record_pending,
    write,
)


def _all_lines(state_dir: Path) -> list[str]:
    """All audit lines in chain order; the day files are found from disk, never from a fixed date."""
    files = sorted(audit_root(state_dir).glob("*/*.jsonl"))
    assert files
    return [line for f in files for line in f.read_text(encoding="utf-8").splitlines()]


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

    lines = _all_lines(tmp_path)
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

    lines = _all_lines(tmp_path)
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

    lines = _all_lines(tmp_path)
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

    root = audit_root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    lock_file = root / "chain.lock"
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
    assert 0.18 <= elapsed < 2.0

    pending_file = tmp_path / "audit-pending-gap.jsonl"
    assert pending_file.is_file()
    lines = pending_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ts"] == ts
    assert "timed out acquiring audit lock" in record["reason"]


def test_pending_claim_keeps_entry_appended_during_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = tmp_path / "audit-pending-gap.jsonl"
    pending.write_text('{"ts":"2026-01-01T00:00:00.000Z","reason":"old"}\n', encoding="utf-8")
    real = writer._append_locked
    calls: list[str] = []

    def spy(event, root):  # type: ignore[no-untyped-def]
        if event.get("kind") == "gap" and not calls:
            calls.append("gap")
            record_pending({"ts": "2026-01-02T00:00:00.000Z"}, "during-gap", state_dir=tmp_path)
        return real(event, root)

    monkeypatch.setattr(writer, "_append_locked", spy)
    assert write({"kind": "tool", "target": "x"}, state_dir=tmp_path) is True
    assert calls == ["gap"]
    kept = [json.loads(line) for line in pending.read_text(encoding="utf-8").splitlines()]
    assert [k["reason"] for k in kept] == ["during-gap"]
    assert not list(tmp_path.glob("*.claimed"))
    events = [json.loads(line) for line in _all_lines(tmp_path)]
    assert [e["kind"] for e in events] == ["gap", "tool"]
    assert events[0]["detail"]["lost"] == 1


def test_unreadable_pending_still_reports_gap(tmp_path: Path) -> None:
    pending = tmp_path / "audit-pending-gap.jsonl"
    pending.write_text("not json\n{broken\n", encoding="utf-8")
    assert write({"kind": "tool", "target": "x"}, state_dir=tmp_path) is True
    assert not pending.exists()
    events = [json.loads(line) for line in _all_lines(tmp_path)]
    assert [e["kind"] for e in events] == ["gap", "tool"]
    assert events[0]["detail"]["lost"] == 2
    assert events[0]["detail"]["unparsable"] == 2


def test_gap_written_then_error_does_not_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = tmp_path / "audit-pending-gap.jsonl"
    pending.write_text('{"ts":"2026-01-01T00:00:00.000Z","reason":"old"}\n', encoding="utf-8")
    real = writer._write_record_to_file

    def boom_after_gap(record, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real(record, *args, **kwargs)
        if record.get("kind") == "gap":
            raise RuntimeError("after gap line")
        return result

    monkeypatch.setattr(writer, "_write_record_to_file", boom_after_gap)
    assert write({"kind": "tool", "target": "x"}, state_dir=tmp_path) is False
    events = [json.loads(line) for line in _all_lines(tmp_path)]
    assert [e["kind"] for e in events] == ["gap"]
    kept = [json.loads(line) for line in pending.read_text(encoding="utf-8").splitlines()]
    assert [k["reason"] for k in kept] == ["after gap line"]
    assert not list(tmp_path.glob("*.claimed"))


def _make_unreadable(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    real_read = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self == target:
            raise PermissionError("unreadable claim")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


def test_unreadable_claim_left_when_gap_append_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = tmp_path / "audit-pending-gap.jsonl"
    bad = tmp_path / "audit-pending-gap.999999991.claimed"
    good = tmp_path / "audit-pending-gap.999999992.claimed"
    bad.write_text("secret\n", encoding="utf-8")
    good.write_text('{"ts":"2026-01-01T00:00:00.000Z","reason":"old"}\n', encoding="utf-8")
    _make_unreadable(monkeypatch, bad)
    real = writer._write_record_to_file

    def boom_on_gap(record, *args, **kwargs):  # type: ignore[no-untyped-def]
        if record.get("kind") == "gap":
            raise RuntimeError("gap append failed")
        return real(record, *args, **kwargs)

    monkeypatch.setattr(writer, "_write_record_to_file", boom_on_gap)
    assert write({"kind": "tool", "target": "x"}, state_dir=tmp_path) is False
    assert bad.exists()
    assert not good.exists()
    reasons = [json.loads(line).get("reason") for line in pending.read_text(encoding="utf-8").splitlines()]
    assert "old" in reasons


def test_unreadable_claim_unlinked_after_gap_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "audit-pending-gap.999999991.claimed"
    good = tmp_path / "audit-pending-gap.999999992.claimed"
    bad.write_text("secret\n", encoding="utf-8")
    good.write_text('{"ts":"2026-01-01T00:00:00.000Z","reason":"old"}\n', encoding="utf-8")
    _make_unreadable(monkeypatch, bad)
    assert write({"kind": "tool", "target": "x"}, state_dir=tmp_path) is True
    assert not bad.exists()
    assert not good.exists()
    events = [json.loads(line) for line in _all_lines(tmp_path)]
    assert [e["kind"] for e in events] == ["gap", "tool"]
    assert events[0]["detail"]["lost"] == "unknown"


def test_unparsable_pending_lines_counted(tmp_path: Path) -> None:
    pending = tmp_path / "audit-pending-gap.jsonl"
    pending.write_text(
        '{"ts":"2026-01-01T00:00:00.000Z","reason":"old"}\n{broken\n\nnot json\n',
        encoding="utf-8",
    )
    assert write({"kind": "tool", "target": "x"}, state_dir=tmp_path) is True
    gap = json.loads(_all_lines(tmp_path)[0])
    assert gap["kind"] == "gap"
    assert gap["detail"]["lost"] == 3
    assert gap["detail"]["unparsable"] == 2
