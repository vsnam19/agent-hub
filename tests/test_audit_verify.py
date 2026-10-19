import json
import shutil
from pathlib import Path

from agenthub.audit.verify import VerifyResult, verify
from agenthub.audit.writer import append, audit_root, canonical_json


def test_clean_log_verifies(tmp_path: Path) -> None:
    # Empty audit directory
    res_empty = verify(state_dir=tmp_path)
    assert res_empty == VerifyResult(ok=True, message="ok: 0 events")

    # Clean log with events across days in the same month
    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "cmd1"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:01.000Z", "kind": "command", "target": "cmd2"}, state_dir=tmp_path)
    append({"ts": "2026-10-19T08:00:00.000Z", "kind": "command", "target": "cmd3"}, state_dir=tmp_path)

    res = verify(state_dir=tmp_path)
    assert res.ok is True
    assert res.message == "ok: 3 events"

    # Verifying specific month
    res_month = verify(state_dir=tmp_path, month="2026-10")
    assert res_month.ok is True
    assert res_month.message == "ok: 3 events"

    # Verifying non-existent month
    res_other = verify(state_dir=tmp_path, month="2026-11")
    assert res_other.ok is True
    assert res_other.message == "ok: 0 events"


def test_tampered_event_names_file_line_and_seq(tmp_path: Path) -> None:
    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "one"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:01.000Z", "kind": "command", "target": "two"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:02.000Z", "kind": "command", "target": "three"}, state_dir=tmp_path)

    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-18.jsonl"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3

    # Tamper the second event's target payload without updating its hash
    record = json.loads(lines[1])
    assert record["seq"] == 2
    record["target"] = "tampered"
    lines[1] = canonical_json(record)
    day_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = verify(state_dir=tmp_path)
    assert res.ok is False
    assert "2026-10/2026-10-18.jsonl:2" in res.message
    assert "seq=2" in res.message
    assert "hash mismatch" in res.message


def test_removed_event_names_seq(tmp_path: Path) -> None:
    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "one"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:01.000Z", "kind": "command", "target": "two"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:02.000Z", "kind": "command", "target": "three"}, state_dir=tmp_path)

    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-18.jsonl"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3

    # Remove the second event (seq=2)
    del lines[1]
    day_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = verify(state_dir=tmp_path)
    assert res.ok is False
    assert "2026-10/2026-10-18.jsonl:2" in res.message
    assert "seq=" in res.message
    assert "2" in res.message


def test_reordered_events_are_detected(tmp_path: Path) -> None:
    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "one"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:01.000Z", "kind": "command", "target": "two"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:02.000Z", "kind": "command", "target": "three"}, state_dir=tmp_path)

    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-18.jsonl"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3

    # Swap events 2 and 3
    lines[1], lines[2] = lines[2], lines[1]
    day_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = verify(state_dir=tmp_path)
    assert res.ok is False
    assert "2026-10/2026-10-18.jsonl:2" in res.message
    assert any(err in res.message for err in ("seq mismatch", "prev_hash mismatch", "hash mismatch"))


def test_clock_skew_over_two_seconds_fails(tmp_path: Path) -> None:
    # Within 2.0s skew tolerance (1.5s earlier) -> passes
    append({"ts": "2026-10-18T10:00:05.000Z", "kind": "command", "target": "first"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:03.500Z", "kind": "command", "target": "second"}, state_dir=tmp_path)

    res_ok = verify(state_dir=tmp_path)
    assert res_ok.ok is True
    assert res_ok.message == "ok: 2 events"

    # Exactly 2.0s earlier is within tolerance -> passes
    append({"ts": "2026-10-18T10:00:01.500Z", "kind": "command", "target": "third"}, state_dir=tmp_path)
    res_ok2 = verify(state_dir=tmp_path)
    assert res_ok2.ok is True
    assert res_ok2.message == "ok: 3 events"

    # More than 2.0s earlier (e.g. 2.1s earlier than 10:00:01.500Z) -> fails
    append({"ts": "2026-10-18T09:59:59.400Z", "kind": "command", "target": "fourth"}, state_dir=tmp_path)
    res_fail = verify(state_dir=tmp_path)
    assert res_fail.ok is False
    assert "2026-10/2026-10-18.jsonl:4 seq=4:" in res_fail.message
    assert "clock skew" in res_fail.message.lower()


def test_verify_spans_archived_month_via_local_manifest(tmp_path: Path) -> None:
    state = tmp_path / "audit_state"
    e1 = append({"ts": "2026-09-30T23:50:00.000Z", "kind": "command", "target": "sep"}, state_dir=state)
    e2 = append({"ts": "2026-10-01T00:10:00.000Z", "kind": "command", "target": "oct"}, state_dir=state)
    assert e2["prev_hash"] == e1["hash"]

    # Delete the September directory (simulating monthly archival)
    root = audit_root(state)
    sep_dir = root / "2026-09"
    shutil.rmtree(sep_dir)
    assert not sep_dir.exists()

    # Create local manifest for the archived month
    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = manifests_dir / "2026-09.json"
    manifest_file.write_text(json.dumps({"month": "2026-09", "last_hash": e1["hash"]}), encoding="utf-8")

    # Verification spans archived month via manifest
    res = verify(state_dir=state)
    assert res.ok is True
    assert res.message == "ok: 1 events"

    res_month = verify(state_dir=state, month="2026-10")
    assert res_month.ok is True
    assert res_month.message == "ok: 1 events"

    # Wrong manifest last_hash fails
    manifest_file.write_text(json.dumps({"month": "2026-09", "last_hash": "f" * 64}), encoding="utf-8")
    res_wrong = verify(state_dir=state)
    assert res_wrong.ok is False
    assert "2026-10/2026-10-01.jsonl:1 seq=1:" in res_wrong.message
    assert "prev_hash mismatch" in res_wrong.message

    # Missing manifest for deleted earlier month fails
    manifest_file.unlink()
    res_missing = verify(state_dir=state)
    assert res_missing.ok is False
    assert "2026-10/2026-10-01.jsonl:1 seq=1:" in res_missing.message
