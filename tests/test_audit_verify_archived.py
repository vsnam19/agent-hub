from __future__ import annotations

import hashlib
import json
import tarfile
from datetime import date
from pathlib import Path

from agenthub.audit.archive import archive_month
from agenthub.audit.verify import VerifyResult, verify
from agenthub.audit.writer import append, canonical_json


def test_archived_month_verifies_ok(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    append({"ts": "2026-09-01T10:00:01.000Z", "kind": "command", "target": "c2"}, state_dir=state_dir)
    append({"ts": "2026-09-15T14:30:00.000Z", "kind": "command", "target": "c3"}, state_dir=state_dir)

    res_archive = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert res_archive.status == "ok"

    # Verify using explicit vault
    res = verify(state_dir=state_dir, month="2026-09", vault=vault_dir)
    assert isinstance(res, VerifyResult)
    assert res.ok is True
    assert res.message == "ok: 3 events"

    # Verify using config_dir with audit.toml
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "audit.toml").write_text(f'vault = "{vault_dir}"\n', encoding="utf-8")

    res_cfg = verify(state_dir=state_dir, month="2026-09", config_dir=config_dir)
    assert isinstance(res_cfg, VerifyResult)
    assert res_cfg.ok is True
    assert res_cfg.message == "ok: 3 events"

    # Boundary check: manifest count mismatch fails
    local_manifest = state_dir / "audit" / "manifests" / "2026-09.json"
    manifest_orig = local_manifest.read_text(encoding="utf-8")
    data_wrong_count = json.loads(manifest_orig)
    data_wrong_count["count"] = 99
    local_manifest.write_text(json.dumps(data_wrong_count), encoding="utf-8")
    res_bad_count = verify(state_dir=state_dir, month="2026-09", vault=vault_dir)
    assert res_bad_count.ok is False
    assert "count mismatch" in res_bad_count.message

    # Boundary check: manifest last_hash mismatch fails
    data_wrong_last = json.loads(manifest_orig)
    data_wrong_last["last_hash"] = "f" * 64
    local_manifest.write_text(json.dumps(data_wrong_last), encoding="utf-8")
    res_bad_last = verify(state_dir=state_dir, month="2026-09", vault=vault_dir)
    assert res_bad_last.ok is False
    assert "last_hash mismatch" in res_bad_last.message

    # Boundary check: manifest first_prev_hash mismatch fails
    data_wrong_first = json.loads(manifest_orig)
    data_wrong_first["first_prev_hash"] = "e" * 64
    local_manifest.write_text(json.dumps(data_wrong_first), encoding="utf-8")
    res_bad_first = verify(state_dir=state_dir, month="2026-09", vault=vault_dir)
    assert res_bad_first.ok is False
    assert any(m in res_bad_first.message for m in ("prev_hash mismatch", "first_prev_hash mismatch"))

    # Restore valid manifest
    local_manifest.write_text(manifest_orig, encoding="utf-8")


def test_tampered_archive_names_the_break(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "first"}, state_dir=state_dir)
    append({"ts": "2026-09-01T10:00:01.000Z", "kind": "command", "target": "second"}, state_dir=state_dir)
    append({"ts": "2026-09-01T10:00:02.000Z", "kind": "command", "target": "third"}, state_dir=state_dir)

    res_archive = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert res_archive.status == "ok"

    vault_tar = vault_dir / "agent-hub-audit" / "2026-09.tar.xz"
    assert vault_tar.is_file()

    # Extract, tamper second event, repack tarball
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(vault_tar, "r:xz") as tar:
        tar.extractall(extract_dir, filter="data")

    day_file = extract_dir / "2026-09" / "2026-09-01.jsonl"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3

    record = json.loads(lines[1])
    assert record["seq"] == 2
    record["target"] = "tampered"
    lines[1] = canonical_json(record)
    day_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Repack into vault_tar
    with tarfile.open(vault_tar, "w:xz") as tar:
        tar.add(day_file, arcname="2026-09/2026-09-01.jsonl")

    # Update sha256 in local manifest so it passes sha256 check and reaches per-line verification
    repacked_sha = hashlib.sha256(vault_tar.read_bytes()).hexdigest()
    local_manifest = state_dir / "audit" / "manifests" / "2026-09.json"
    manifest_data = json.loads(local_manifest.read_text(encoding="utf-8"))
    manifest_data["sha256"] = repacked_sha
    local_manifest.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    res = verify(state_dir=state_dir, month="2026-09", vault=vault_dir)
    assert res.ok is False
    assert "2026-09/2026-09-01.jsonl:2" in res.message
    assert "seq=2" in res.message
    assert "hash mismatch" in res.message


def test_archive_sha_mismatch_with_manifest_fails(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    res_archive = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert res_archive.status == "ok"

    vault_tar = vault_dir / "agent-hub-audit" / "2026-09.tar.xz"
    vault_tar.write_bytes(vault_tar.read_bytes() + b"\x00")

    res = verify(state_dir=state_dir, month="2026-09", vault=vault_dir)
    assert res.ok is False
    assert "sha256 mismatch" in res.message.lower()
    assert "2026-09" in res.message


def test_missing_vault_reports_needs_vault(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    res_archive = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert res_archive.status == "ok"

    # Case 1: Vault directory does not exist
    missing_vault = tmp_path / "does_not_exist"
    res1 = verify(state_dir=state_dir, month="2026-09", vault=missing_vault)
    assert res1.ok is False
    assert res1.message.startswith("archive for 2026-09 needs the vault:")

    # Case 2: Archive file in vault is missing
    vault_tar = vault_dir / "agent-hub-audit" / "2026-09.tar.xz"
    vault_tar.unlink()
    res2 = verify(state_dir=state_dir, month="2026-09", vault=vault_dir)
    assert res2.ok is False
    assert res2.message.startswith("archive for 2026-09 needs the vault:")

    # Case 3: Missing config file when vault is None
    empty_cfg = tmp_path / "empty_config"
    empty_cfg.mkdir(parents=True, exist_ok=True)
    res3 = verify(state_dir=state_dir, month="2026-09", config_dir=empty_cfg)
    assert res3.ok is False
    assert res3.message.startswith("archive for 2026-09 needs the vault:")


def test_local_month_results_unchanged(tmp_path: Path) -> None:
    # 1. Empty audit root
    res_empty = verify(state_dir=tmp_path)
    assert res_empty == VerifyResult(ok=True, message="ok: 0 events")

    # 2. Clean local events
    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "one"}, state_dir=tmp_path)
    append({"ts": "2026-10-18T10:00:01.000Z", "kind": "command", "target": "two"}, state_dir=tmp_path)

    res_all = verify(state_dir=tmp_path)
    assert res_all == VerifyResult(ok=True, message="ok: 2 events")

    res_month = verify(state_dir=tmp_path, month="2026-10")
    assert res_month == VerifyResult(ok=True, message="ok: 2 events")

    # 3. Non-existent month without manifest
    res_nonexistent = verify(state_dir=tmp_path, month="2026-11")
    assert res_nonexistent == VerifyResult(ok=True, message="ok: 0 events")

    # 4. Tampered local event
    day_file = tmp_path / "audit" / "2026-10" / "2026-10-18.jsonl"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["target"] = "tampered"
    lines[1] = canonical_json(record)
    day_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res_tampered = verify(state_dir=tmp_path)
    assert res_tampered.ok is False
    assert "2026-10/2026-10-18.jsonl:2" in res_tampered.message
    assert "seq=2" in res_tampered.message
    assert "hash mismatch" in res_tampered.message


def test_archived_later_day_names_previous_file_as_source(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    append({"ts": "2026-09-02T10:00:00.000Z", "kind": "command", "target": "c2"}, state_dir=state_dir)

    res_archive = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert res_archive.status == "ok"

    vault_tar = vault_dir / "agent-hub-audit" / "2026-09.tar.xz"
    assert vault_tar.is_file()

    # Extract archive
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(vault_tar, "r:xz") as tar:
        tar.extractall(extract_dir, filter="data")

    # Day 2 line 1 prev_hash mismatch
    day2_file = extract_dir / "2026-09" / "2026-09-02.jsonl"
    lines = day2_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    record["prev_hash"] = "0" * 64
    lines[0] = canonical_json(record)
    day2_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    day1_file = extract_dir / "2026-09" / "2026-09-01.jsonl"

    # Repack into vault_tar
    with tarfile.open(vault_tar, "w:xz") as tar:
        tar.add(day1_file, arcname="2026-09/2026-09-01.jsonl")
        tar.add(day2_file, arcname="2026-09/2026-09-02.jsonl")

    # Update sha256 in local manifest so it passes sha256 check and reaches day lines verification
    repacked_sha = hashlib.sha256(vault_tar.read_bytes()).hexdigest()
    local_manifest = state_dir / "audit" / "manifests" / "2026-09.json"
    manifest_data = json.loads(local_manifest.read_text(encoding="utf-8"))
    manifest_data["sha256"] = repacked_sha
    local_manifest.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    res = verify(state_dir=state_dir, month="2026-09", vault=vault_dir)
    assert res.ok is False
    assert "2026-09/2026-09-02.jsonl:1 seq=1:" in res.message
    assert "prev_hash mismatch" in res.message
    assert "expected from 2026-09/2026-09-01.jsonl" in res.message
    assert "manifests/2026-09.json" not in res.message
