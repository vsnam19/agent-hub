import json
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from agenthub.audit.archive import archive_month
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
    assert "seq=3: seq mismatch: expected 2, got 3" in res.message


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
    assert "seq=3: seq mismatch: expected 2, got 3" in res.message


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


def test_ts_backwards_is_break_strict(tmp_path: Path) -> None:
    # Monotonic timestamps (equal ts) pass
    append({"ts": "2026-10-18T10:00:05.000Z", "kind": "command", "target": "first"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:05.000Z", "kind": "command", "target": "second"}, state_dir=tmp_path)

    res_ok = verify(state_dir=tmp_path)
    assert res_ok.ok is True
    assert res_ok.message == "ok: 2 events"

    # Later timestamp passes
    append({"ts": "2026-10-18T10:00:06.000Z", "kind": "command", "target": "third"}, state_dir=tmp_path)
    res_ok2 = verify(state_dir=tmp_path)
    assert res_ok2.ok is True
    assert res_ok2.message == "ok: 3 events"

    # Under strict D-123 rule, any earlier timestamp fails with ts went backwards
    _append_raw(tmp_path, {"ts": "2026-10-18T10:00:04.000Z", "kind": "command", "target": "fourth"})
    res_fail = verify(state_dir=tmp_path)
    assert res_fail.ok is False
    assert "2026-10/2026-10-18.jsonl:4 seq=4:" in res_fail.message
    assert "ts went backwards" in res_fail.message


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
    assert "2026-10/2026-10-18.jsonl" in res.message


def test_idle_month_between_months_ok(tmp_path: Path) -> None:
    root = audit_root(tmp_path)
    m1_dir = root / "2026-08"
    m1_dir.mkdir(parents=True, exist_ok=True)
    ev1 = {
        "schema_version": 1,
        "ts": "2026-08-15T10:00:00.000Z",
        "seq": 1,
        "event_id": "e1",
        "kind": "command",
        "target": "aug",
        "prev_hash": ZERO_HASH,
    }
    ev1["hash"] = event_hash(ZERO_HASH, ev1)
    (m1_dir / "2026-08-15.jsonl").write_text(canonical_json(ev1) + "\n", encoding="utf-8")

    # 2026-09 is idle (no directory, no manifest)

    m2_dir = root / "2026-10"
    m2_dir.mkdir(parents=True, exist_ok=True)
    ev2 = {
        "schema_version": 1,
        "ts": "2026-10-01T10:00:00.000Z",
        "seq": 1,
        "event_id": "e2",
        "kind": "command",
        "target": "oct",
        "prev_hash": ev1["hash"],
    }
    ev2["hash"] = event_hash(ev1["hash"], ev2)
    (m2_dir / "2026-10-01.jsonl").write_text(canonical_json(ev2) + "\n", encoding="utf-8")

    res_all = verify(state_dir=tmp_path)
    assert res_all.ok is True
    assert res_all.message == "ok: 2 events"

    res_oct = verify(state_dir=tmp_path, month="2026-10")
    assert res_oct.ok is True
    assert res_oct.message == "ok: 1 events"


def test_empty_day_file_skipped(tmp_path: Path) -> None:
    root = audit_root(tmp_path)
    oct_dir = root / "2026-10"
    oct_dir.mkdir(parents=True, exist_ok=True)

    ev1 = {
        "schema_version": 1,
        "ts": "2026-10-01T10:00:00.000Z",
        "seq": 1,
        "event_id": "e1",
        "kind": "command",
        "target": "first",
        "prev_hash": ZERO_HASH,
    }
    ev1["hash"] = event_hash(ZERO_HASH, ev1)
    (oct_dir / "2026-10-01.jsonl").write_text(canonical_json(ev1) + "\n", encoding="utf-8")

    # Empty day file on 2026-10-02
    (oct_dir / "2026-10-02.jsonl").write_text("", encoding="utf-8")

    # Day file on 2026-10-03 chains from 2026-10-01
    ev2 = {
        "schema_version": 1,
        "ts": "2026-10-03T10:00:00.000Z",
        "seq": 1,
        "event_id": "e2",
        "kind": "command",
        "target": "second",
        "prev_hash": ev1["hash"],
    }
    ev2["hash"] = event_hash(ev1["hash"], ev2)
    (oct_dir / "2026-10-03.jsonl").write_text(canonical_json(ev2) + "\n", encoding="utf-8")

    res = verify(state_dir=tmp_path)
    assert res.ok is True
    assert res.message == "ok: 2 events"

    res_month = verify(state_dir=tmp_path, month="2026-10")
    assert res_month.ok is True
    assert res_month.message == "ok: 2 events"


def test_month_not_local_message(tmp_path: Path) -> None:
    root = audit_root(tmp_path)
    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_data = {"month": "2026-09", "last_hash": "a" * 64}
    (manifests_dir / "2026-09.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    res = verify(state_dir=tmp_path, month="2026-09")
    assert res.ok is True
    assert "not local" in res.message
    assert "not checked" in res.message


def test_link_source_named_in_message(tmp_path: Path) -> None:
    root = audit_root(tmp_path)
    oct_dir = root / "2026-10"
    oct_dir.mkdir(parents=True, exist_ok=True)

    ev1 = {
        "schema_version": 1,
        "ts": "2026-10-18T10:00:00.000Z",
        "seq": 1,
        "event_id": "e1",
        "kind": "command",
        "target": "oct",
        "prev_hash": ZERO_HASH,
    }
    ev1["hash"] = event_hash(ZERO_HASH, ev1)
    (oct_dir / "2026-10-18.jsonl").write_text(canonical_json(ev1) + "\n", encoding="utf-8")

    dec_dir = root / "2026-12"
    dec_dir.mkdir(parents=True, exist_ok=True)
    wrong_prev = "b" * 64
    ev2 = {
        "schema_version": 1,
        "ts": "2026-12-01T10:00:00.000Z",
        "seq": 1,
        "event_id": "e2",
        "kind": "command",
        "target": "dec",
        "prev_hash": wrong_prev,
    }
    ev2["hash"] = event_hash(wrong_prev, ev2)
    (dec_dir / "2026-12-01.jsonl").write_text(canonical_json(ev2) + "\n", encoding="utf-8")

    res = verify(state_dir=tmp_path)
    assert res.ok is False
    assert "2026-12/2026-12-01.jsonl:1 seq=1:" in res.message
    assert "prev_hash mismatch" in res.message
    assert "2026-10/2026-10-18.jsonl" in res.message
    assert ev1["hash"][:8] in res.message


def test_unreadable_earlier_tail_is_break(tmp_path: Path) -> None:
    root = audit_root(tmp_path)
    sep_dir = root / "2026-09"
    sep_dir.mkdir(parents=True, exist_ok=True)

    # 2026-09-17 has a valid event
    ev0 = {
        "schema_version": 1,
        "ts": "2026-09-17T10:00:00.000Z",
        "seq": 1,
        "event_id": "e0",
        "kind": "command",
        "target": "old",
        "prev_hash": ZERO_HASH,
    }
    ev0["hash"] = event_hash(ZERO_HASH, ev0)
    (sep_dir / "2026-09-17.jsonl").write_text(canonical_json(ev0) + "\n", encoding="utf-8")

    # 2026-09-18 is non-empty, but its last line is unreadable / corrupt JSON
    (sep_dir / "2026-09-18.jsonl").write_text("corrupt tail line\n", encoding="utf-8")

    # 2026-10-01 chains from 2026-09-17 (an older file)
    oct_dir = root / "2026-10"
    oct_dir.mkdir(parents=True, exist_ok=True)
    ev1 = {
        "schema_version": 1,
        "ts": "2026-10-01T10:00:00.000Z",
        "seq": 1,
        "event_id": "e1",
        "kind": "command",
        "target": "new",
        "prev_hash": ev0["hash"],
    }
    ev1["hash"] = event_hash(ev0["hash"], ev1)
    (oct_dir / "2026-10-01.jsonl").write_text(canonical_json(ev1) + "\n", encoding="utf-8")

    # Verifying month 2026-10 must not skip 2026-09-18 to use 2026-09-17; it must report 2026-09-18 as break
    res = verify(state_dir=tmp_path, month="2026-10")
    assert res.ok is False
    assert "2026-09/2026-09-18.jsonl" in res.message


def test_ts_backwards_is_break(tmp_path: Path) -> None:
    root = audit_root(tmp_path)
    oct_dir = root / "2026-10"
    oct_dir.mkdir(parents=True, exist_ok=True)

    ev1 = {
        "schema_version": 1,
        "ts": "2026-10-18T10:00:05.000Z",
        "seq": 1,
        "event_id": "e1",
        "kind": "command",
        "target": "first",
        "prev_hash": ZERO_HASH,
    }
    ev1["hash"] = event_hash(ZERO_HASH, ev1)

    # Event 2 has an earlier timestamp (even within 2 seconds)
    ev2 = {
        "schema_version": 1,
        "ts": "2026-10-18T10:00:04.000Z",
        "seq": 2,
        "event_id": "e2",
        "kind": "command",
        "target": "second",
        "prev_hash": ev1["hash"],
    }
    ev2["hash"] = event_hash(ev1["hash"], ev2)

    (oct_dir / "2026-10-18.jsonl").write_text(
        canonical_json(ev1) + "\n" + canonical_json(ev2) + "\n",
        encoding="utf-8",
    )

    res = verify(state_dir=tmp_path)
    assert res.ok is False
    assert "2026-10/2026-10-18.jsonl:2 seq=2:" in res.message
    assert "ts went backwards" in res.message


def test_earlier_tail_not_json_object_derives_seq(tmp_path: Path) -> None:
    root = audit_root(tmp_path)
    sep_dir = root / "2026-09"
    sep_dir.mkdir(parents=True, exist_ok=True)

    ev0 = {
        "schema_version": 1,
        "ts": "2026-09-18T10:00:00.000Z",
        "seq": 1,
        "event_id": "e0",
        "kind": "command",
        "target": "valid",
        "prev_hash": ZERO_HASH,
    }
    ev0["hash"] = event_hash(ZERO_HASH, ev0)

    # Line 1: blank
    # Line 2: valid event with seq=1
    # Line 3: blank
    # Line 4: JSON that is not an object (e.g. "[1, 2, 3]")
    # Line number is 4, but seq should be derived from previous event (seq=1) + 1 = 2
    day_content = "\n" + canonical_json(ev0) + "\n\n[1, 2, 3]\n"
    (sep_dir / "2026-09-18.jsonl").write_text(day_content, encoding="utf-8")

    oct_dir = root / "2026-10"
    oct_dir.mkdir(parents=True, exist_ok=True)
    ev1 = {
        "schema_version": 1,
        "ts": "2026-10-01T10:00:00.000Z",
        "seq": 1,
        "event_id": "e1",
        "kind": "command",
        "target": "oct",
        "prev_hash": ev0["hash"],
    }
    ev1["hash"] = event_hash(ev0["hash"], ev1)
    (oct_dir / "2026-10-01.jsonl").write_text(canonical_json(ev1) + "\n", encoding="utf-8")

    res = verify(state_dir=tmp_path, month="2026-10")
    assert res.ok is False
    assert "2026-09/2026-09-18.jsonl:4" in res.message
    assert "seq=2:" in res.message
    assert "event is not a JSON object" in res.message


def test_month_with_only_empty_day_files_verifies_archive(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    append({"ts": "2026-09-02T10:00:00.000Z", "kind": "command", "target": "c2"}, state_dir=state_dir)

    res_archive = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert res_archive.status == "ok"

    # Recreate the 2026-09 directory with only an empty day file (blank lines)
    sep_dir = audit_root(state_dir) / "2026-09"
    sep_dir.mkdir(parents=True, exist_ok=True)
    (sep_dir / "2026-09-01.jsonl").write_text("\n\n", encoding="utf-8")

    # A month with only empty day files must not count as local to hide its manifest/archive
    res = verify(state_dir=state_dir, month="2026-09", vault=vault_dir)
    assert res.ok is True
    assert res.message == "ok: 2 events"


def test_verify_archived_month_loads_default_vault(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    append({"ts": "2026-09-01T10:00:01.000Z", "kind": "command", "target": "c2"}, state_dir=state_dir)

    res_archive = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert res_archive.status == "ok"

    # Configure default owner config (via AGENT_HUB_CONFIG) to point to vault_dir
    config_dir = tmp_path / "owner_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "audit.toml").write_text(f'vault = "{vault_dir}"\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(config_dir))

    # verify(month=M) called with neither vault nor config_dir
    res = verify(state_dir=state_dir, month="2026-09")
    assert res.ok is True
    assert res.message == "ok: 2 events"


def test_blank_line_is_break_everywhere(tmp_path: Path) -> None:
    # 1. In _verify_day_lines: blank line in the middle of a day file is reported as break with reason "blank line"
    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "first"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:01.000Z", "kind": "command", "target": "second"}, state_dir=tmp_path)

    day_file = audit_root(tmp_path) / "2026-10" / "2026-10-18.jsonl"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # Insert blank line between line 1 and line 2
    day_file.write_text(lines[0] + "\n\n" + lines[1] + "\n", encoding="utf-8")

    res = verify(state_dir=tmp_path)
    assert res.ok is False
    assert "2026-10/2026-10-18.jsonl:2 seq=2:" in res.message
    assert "blank line" in res.message
    assert "invalid JSON" not in res.message

    # 2. In _verify_day_lines: blank line at line 1 of a day file
    day_file.write_text("\n" + lines[0] + "\n", encoding="utf-8")
    res1 = verify(state_dir=tmp_path)
    assert res1.ok is False
    assert "2026-10/2026-10-18.jsonl:1 seq=1:" in res1.message
    assert "blank line" in res1.message

    # 3. In _verify_day_lines: day file with only blank lines
    day_file.write_text("\n\n", encoding="utf-8")
    res_only_blank = verify(state_dir=tmp_path)
    assert res_only_blank.ok is False
    assert "2026-10/2026-10-18.jsonl:1 seq=1:" in res_only_blank.message
    assert "blank line" in res_only_blank.message

    # 4. In _latest_earlier_local_day_hash: earlier day file with only blank lines stops at that file and names it
    tmp2 = tmp_path / "case_earlier_blank"
    root2 = audit_root(tmp2)
    sep_dir = root2 / "2026-09"
    sep_dir.mkdir(parents=True, exist_ok=True)
    ev0 = {
        "schema_version": 1,
        "ts": "2026-09-17T10:00:00.000Z",
        "seq": 1,
        "event_id": "e0",
        "kind": "command",
        "target": "old",
        "prev_hash": ZERO_HASH,
    }
    ev0["hash"] = event_hash(ZERO_HASH, ev0)
    (sep_dir / "2026-09-17.jsonl").write_text(canonical_json(ev0) + "\n", encoding="utf-8")

    # 2026-09-18 has only blank lines
    (sep_dir / "2026-09-18.jsonl").write_text("\n\n", encoding="utf-8")

    oct_dir = root2 / "2026-10"
    oct_dir.mkdir(parents=True, exist_ok=True)
    ev_oct = {
        "schema_version": 1,
        "ts": "2026-10-01T10:00:00.000Z",
        "seq": 1,
        "event_id": "e1",
        "kind": "command",
        "target": "new",
        "prev_hash": ev0["hash"],
    }
    ev_oct["hash"] = event_hash(ev0["hash"], ev_oct)
    (oct_dir / "2026-10-01.jsonl").write_text(canonical_json(ev_oct) + "\n", encoding="utf-8")

    # Verifying month 2026-10 must NOT skip 2026-09-18 to use 2026-09-17;
    # it must stop and name 2026-09-18 with blank line
    res_earlier = verify(state_dir=tmp2, month="2026-10")
    assert res_earlier.ok is False
    assert "2026-09/2026-09-18.jsonl:1 seq=1:" in res_earlier.message
    assert "blank line" in res_earlier.message

    # 5. In _latest_earlier_local_day_hash: earlier day file ending in a blank line stops at that file and names it
    (sep_dir / "2026-09-18.jsonl").write_text(canonical_json(ev0) + "\n\n", encoding="utf-8")
    res_earlier_tail = verify(state_dir=tmp2, month="2026-10")
    assert res_earlier_tail.ok is False
    assert "2026-09/2026-09-18.jsonl:2 seq=2:" in res_earlier_tail.message
    assert "blank line" in res_earlier_tail.message
