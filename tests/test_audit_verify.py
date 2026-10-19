import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from agenthub.audit.verify import VerifyResult, verify
from agenthub.audit.writer import ZERO_HASH, append, audit_root, canonical_json, event_hash


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
    assert "seq=3" in res.message
    assert "seq mismatch" in res.message


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


def _append_raw(state_dir: Path, event: dict[str, Any]) -> dict[str, Any]:
    record = dict(event)
    record.setdefault("schema_version", 1)
    record.setdefault("event_id", uuid.uuid4().hex)
    day = str(record["ts"])[:10]
    month = day[:7]
    month_dir = audit_root(state_dir) / month
    month_dir.mkdir(parents=True, exist_ok=True)
    day_file = month_dir / f"{day}.jsonl"
    if day_file.exists() and day_file.stat().st_size > 0:
        lines = [line.strip() for line in day_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        last_ev = json.loads(lines[-1])
        seq = int(last_ev["seq"]) + 1
        prev_hash = str(last_ev["hash"])
    else:
        seq = 1
        prev_hash = ZERO_HASH
    record["seq"] = seq
    record["prev_hash"] = prev_hash
    record["hash"] = event_hash(prev_hash, record)
    with day_file.open("a", encoding="utf-8") as f:
        f.write(canonical_json(record) + "\n")
    return record


def test_clock_skew_over_two_seconds_fails(tmp_path: Path) -> None:
    # Within 2.0s skew tolerance (1.5s earlier) -> passes
    _append_raw(tmp_path, {"ts": "2026-10-18T10:00:05.000Z", "kind": "command", "target": "first"})
    _append_raw(tmp_path, {"ts": "2026-10-18T10:00:03.500Z", "kind": "command", "target": "second"})

    res_ok = verify(state_dir=tmp_path)
    assert res_ok.ok is True
    assert res_ok.message == "ok: 2 events"

    # Exactly 2.0s earlier is within tolerance -> passes
    _append_raw(tmp_path, {"ts": "2026-10-18T10:00:01.500Z", "kind": "command", "target": "third"})
    res_ok2 = verify(state_dir=tmp_path)
    assert res_ok2.ok is True
    assert res_ok2.message == "ok: 3 events"

    # More than 2.0s earlier (e.g. 2.1s earlier than 10:00:01.500Z) -> fails
    _append_raw(tmp_path, {"ts": "2026-10-18T09:59:59.400Z", "kind": "command", "target": "fourth"})
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


def test_idle_month_between_local_months_verifies(tmp_path: Path) -> None:
    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "oct"}, state_dir=tmp_path)
    append({"ts": "2026-12-01T10:00:00.000Z", "kind": "command", "target": "dec"}, state_dir=tmp_path)

    res = verify(state_dir=tmp_path)
    assert res.ok is True
    assert res.message == "ok: 2 events"

    res_dec = verify(state_dir=tmp_path, month="2026-12")
    assert res_dec.ok is True
    assert res_dec.message == "ok: 1 events"


def test_idle_month_after_archived_manifest_verifies(tmp_path: Path) -> None:
    state = tmp_path / "audit_state"
    e1 = append({"ts": "2026-10-31T23:50:00.000Z", "kind": "command", "target": "oct"}, state_dir=state)

    # Delete 2026-10 directory (archived)
    root = audit_root(state)
    oct_dir = root / "2026-10"
    shutil.rmtree(oct_dir)
    assert not oct_dir.exists()

    # Create local manifest for the archived month
    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = manifests_dir / "2026-10.json"
    manifest_file.write_text(json.dumps({"month": "2026-10", "last_hash": e1["hash"]}), encoding="utf-8")

    # Event in 2026-12 (nothing in 2026-11)
    e2 = append({"ts": "2026-12-01T10:00:00.000Z", "kind": "command", "target": "dec"}, state_dir=state)
    assert e2["prev_hash"] == e1["hash"]

    res = verify(state_dir=state)
    assert res.ok is True
    assert res.message == "ok: 1 events"

    res_dec = verify(state_dir=state, month="2026-12")
    assert res_dec.ok is True
    assert res_dec.message == "ok: 1 events"

    # Manifest missing last_hash fails
    manifest_file.write_text(json.dumps({"month": "2026-10"}), encoding="utf-8")
    res_no_hash = verify(state_dir=state)
    assert res_no_hash.ok is False
    assert "2026-12/2026-12-01.jsonl:1 seq=1:" in res_no_hash.message
    assert "manifest for 2026-10 missing last_hash" in res_no_hash.message

    # Invalid manifest JSON fails
    manifest_file.write_text("invalid json", encoding="utf-8")
    res_bad_json = verify(state_dir=state)
    assert res_bad_json.ok is False
    assert "2026-12/2026-12-01.jsonl:1 seq=1:" in res_bad_json.message
    assert "invalid manifest for 2026-10" in res_bad_json.message


def test_deleted_month_with_events_still_fails(tmp_path: Path) -> None:
    state = tmp_path / "audit_state"
    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "oct"}, state_dir=state)
    append({"ts": "2026-11-15T10:00:00.000Z", "kind": "command", "target": "nov"}, state_dir=state)
    append({"ts": "2026-12-01T10:00:00.000Z", "kind": "command", "target": "dec"}, state_dir=state)

    # Delete 2026-11 directory without a manifest
    root = audit_root(state)
    nov_dir = root / "2026-11"
    shutil.rmtree(nov_dir)
    assert not nov_dir.exists()

    res = verify(state_dir=state)
    assert res.ok is False
    assert "2026-12/2026-12-01.jsonl:1" in res.message
    assert "seq=1" in res.message
    assert "prev_hash mismatch" in res.message
    assert "missing local month or manifest for 2026-11" in res.message
