from __future__ import annotations

import hashlib
import json
import os
import tarfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

import agenthub.audit.archive as archive_mod
from agenthub.audit.archive import ArchiveResult, archive_month
from agenthub.audit.verify import verify
from agenthub.audit.writer import append, audit_root


def _read_all_events(state_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    root = audit_root(state_dir)
    if not root.is_dir():
        return events
    for p in sorted(root.glob("*/*.jsonl")):
        if p.name.endswith(".lock"):
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def test_archive_success_publishes_manifest_then_deletes_local(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    e1 = append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    e2 = append({"ts": "2026-09-01T10:00:01.000Z", "kind": "command", "target": "c2"}, state_dir=state_dir)
    e3 = append({"ts": "2026-09-15T14:30:00.000Z", "kind": "command", "target": "c3"}, state_dir=state_dir)
    append({"ts": "2026-10-01T08:00:00.000Z", "kind": "command", "target": "oct1"}, state_dir=state_dir)

    root = audit_root(state_dir)
    sep_dir = root / "2026-09"
    day1_bytes = (sep_dir / "2026-09-01.jsonl").read_bytes()
    day2_bytes = (sep_dir / "2026-09-15.jsonl").read_bytes()

    res = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert isinstance(res, ArchiveResult)
    assert res.status == "ok"
    assert res.exit_code == 0
    assert "2026-09" in res.message

    # Vault layout checks
    vault_sub = vault_dir / "agent-hub-audit"
    vault_tar = vault_sub / "2026-09.tar.xz"
    vault_manifest = vault_sub / "2026-09.json"
    assert vault_tar.is_file()
    assert vault_manifest.is_file()
    assert not (vault_sub / "2026-09.tar.xz.part").exists()

    # Check tarball contents
    with tarfile.open(vault_tar, "r:xz") as tar:
        assert tar.getnames() == ["2026-09/2026-09-01.jsonl", "2026-09/2026-09-15.jsonl"]
        f1 = tar.extractfile("2026-09/2026-09-01.jsonl")
        f2 = tar.extractfile("2026-09/2026-09-15.jsonl")
        assert f1 is not None and f1.read() == day1_bytes
        assert f2 is not None and f2.read() == day2_bytes

    actual_sha256 = hashlib.sha256(vault_tar.read_bytes()).hexdigest()

    # Check manifest contents in vault and local state
    manifest_data = json.loads(vault_manifest.read_text(encoding="utf-8"))
    assert set(manifest_data.keys()) == {
        "month",
        "files",
        "count",
        "first_hash",
        "first_prev_hash",
        "last_hash",
        "sha256",
        "created",
    }
    assert manifest_data["month"] == "2026-09"
    assert manifest_data["files"] == ["2026-09-01.jsonl", "2026-09-15.jsonl"]
    assert manifest_data["count"] == 3
    assert manifest_data["first_hash"] == e1["hash"]
    assert manifest_data["first_prev_hash"] == e1["prev_hash"]
    assert manifest_data["last_hash"] == e3["hash"]
    assert manifest_data["sha256"] == actual_sha256
    assert manifest_data["created"].endswith("Z")
    datetime.fromisoformat(manifest_data["created"])
    assert e2["prev_hash"] == e1["hash"]

    local_manifest = root / "manifests" / "2026-09.json"
    assert local_manifest.is_file()
    assert json.loads(local_manifest.read_text(encoding="utf-8")) == manifest_data

    # Local 2026-09 directory deleted, tmp archive cleaned up, 2026-10 preserved
    assert not sep_dir.exists()
    assert not (root / "tmp" / "2026-09.tar.xz").exists()
    assert (root / "2026-10" / "2026-10-01.jsonl").is_file()

    # Chain verification across archived month via local manifest succeeds
    v_res = verify(state_dir=state_dir)
    assert v_res.ok is True


def test_vault_missing_keeps_local_and_reports_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    append({"ts": "2026-09-05T12:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)

    root = audit_root(state_dir)
    day_file = root / "2026-09" / "2026-09-05.jsonl"
    original_bytes = day_file.read_bytes()

    missing_vault = tmp_path / "does_not_exist"
    res = archive_month("2026-09", state_dir=state_dir, vault=missing_vault, today=date(2026, 10, 19))
    assert res.status == "error"
    assert res.exit_code == 1
    assert "does not exist" in res.message
    assert not missing_vault.exists()
    assert day_file.is_file()
    assert day_file.read_bytes() == original_bytes
    assert not (root / "manifests" / "2026-09.json").exists()

    # Missing config file when vault is None also reports error and keeps local files
    empty_cfg = tmp_path / "empty_config"
    empty_cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(empty_cfg))
    res_cfg = archive_month("2026-09", state_dir=state_dir, vault=None, today=date(2026, 10, 19))
    assert res_cfg.status == "error"
    assert res_cfg.exit_code == 1
    assert "missing audit config" in res_cfg.message
    assert day_file.is_file()
    assert day_file.read_bytes() == original_bytes


def test_vault_unwritable_keeps_local(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    append({"ts": "2026-09-10T09:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)

    root = audit_root(state_dir)
    day_file = root / "2026-09" / "2026-09-10.jsonl"
    original_bytes = day_file.read_bytes()

    # 1. File in place of vault directory
    vault_as_file = tmp_path / "vault_file"
    vault_as_file.write_text("not a directory", encoding="utf-8")
    res_file = archive_month("2026-09", state_dir=state_dir, vault=vault_as_file, today=date(2026, 10, 19))
    assert res_file.status == "error"
    assert res_file.exit_code == 1
    assert "not a directory" in res_file.message
    assert day_file.is_file()
    assert day_file.read_bytes() == original_bytes

    # 2. File in place of <vault>/agent-hub-audit subdirectory
    vault_sub_file = tmp_path / "vault_sub_file"
    vault_sub_file.mkdir(parents=True, exist_ok=True)
    (vault_sub_file / "agent-hub-audit").write_text("blocking file", encoding="utf-8")
    res_sub = archive_month("2026-09", state_dir=state_dir, vault=vault_sub_file, today=date(2026, 10, 19))
    assert res_sub.status == "error"
    assert res_sub.exit_code == 1
    assert "not a directory" in res_sub.message
    assert day_file.is_file()
    assert day_file.read_bytes() == original_bytes

    # 3. Read-only vault directory
    ro_vault = tmp_path / "ro_vault"
    ro_vault.mkdir(parents=True, exist_ok=True)
    os.chmod(ro_vault, 0o500)
    try:
        res_ro = archive_month("2026-09", state_dir=state_dir, vault=ro_vault, today=date(2026, 10, 19))
        assert res_ro.status == "error"
        assert res_ro.exit_code == 1
        assert day_file.is_file()
        assert day_file.read_bytes() == original_bytes
        assert not (root / "manifests" / "2026-09.json").exists()
    finally:
        os.chmod(ro_vault, 0o700)

    # 4. Manifest write error (file in place of <state>/audit/manifests directory)
    ok_vault = tmp_path / "ok_vault"
    ok_vault.mkdir(parents=True, exist_ok=True)
    manifests_blocker = root / "manifests"
    manifests_blocker.write_text("not a dir", encoding="utf-8")
    try:
        res_mf = archive_month("2026-09", state_dir=state_dir, vault=ok_vault, today=date(2026, 10, 19))
        assert res_mf.status == "error"
        assert res_mf.exit_code == 1
        assert day_file.is_file()
        assert day_file.read_bytes() == original_bytes
        assert not (ok_vault / "agent-hub-audit" / "2026-09.json").exists()
        assert not (ok_vault / "agent-hub-audit" / "2026-09.json.part").exists()
    finally:
        manifests_blocker.unlink(missing_ok=True)


def test_checksum_mismatch_keeps_local_and_removes_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_sub = vault_dir / "agent-hub-audit"
    vault_sub.mkdir(parents=True, exist_ok=True)

    # Unrelated files in vault and tmp must not be deleted
    unrelated_vault_file = vault_sub / "2026-08.tar.xz"
    unrelated_vault_file.write_bytes(b"existing-archive")

    append({"ts": "2026-09-12T11:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    root = audit_root(state_dir)
    tmp_dir = root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    unrelated_tmp_file = tmp_dir / "keep_me.tmp"
    unrelated_tmp_file.write_text("unrelated", encoding="utf-8")

    day_file = root / "2026-09" / "2026-09-12.jsonl"
    original_bytes = day_file.read_bytes()

    def corrupt_copy(src: Path, dst: Path) -> None:
        dst.write_bytes(b"corrupted archive bytes")

    monkeypatch.setattr(archive_mod, "_copy_file", corrupt_copy)

    res = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert res.status == "error"
    assert res.exit_code == 1
    assert "checksum mismatch" in res.message

    # Local month file kept intact
    assert day_file.is_file()
    assert day_file.read_bytes() == original_bytes

    # Own .part and tmp files removed, no archive or manifest published
    assert not (vault_sub / "2026-09.tar.xz.part").exists()
    assert not (vault_sub / "2026-09.tar.xz").exists()
    assert not (vault_sub / "2026-09.json").exists()
    assert not (root / "manifests" / "2026-09.json").exists()
    assert not (tmp_dir / "2026-09.tar.xz").exists()

    # Unrelated files untouched
    assert unrelated_vault_file.read_bytes() == b"existing-archive"
    assert unrelated_tmp_file.read_text(encoding="utf-8") == "unrelated"


def test_rerun_is_noop_and_local_untouched(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    append({"ts": "2026-09-20T15:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)

    res1 = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert res1.status == "ok"
    assert res1.exit_code == 0

    vault_sub = vault_dir / "agent-hub-audit"
    vault_tar = vault_sub / "2026-09.tar.xz"
    vault_manifest = vault_sub / "2026-09.json"
    tar_bytes_before = vault_tar.read_bytes()
    manifest_bytes_before = vault_manifest.read_bytes()

    # Place a local file in 2026-09 to verify a noop rerun does not touch or delete local files
    root = audit_root(state_dir)
    sep_dir = root / "2026-09"
    sep_dir.mkdir(parents=True, exist_ok=True)
    sentinel_file = sep_dir / "2026-09-20.jsonl"
    sentinel_file.write_text('{"sentinel": true}\n', encoding="utf-8")

    res2 = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert res2.status == "noop"
    assert res2.exit_code == 0
    assert "already archived" in res2.message

    # Local sentinel file and vault files are completely untouched
    assert sentinel_file.is_file()
    assert sentinel_file.read_text(encoding="utf-8") == '{"sentinel": true}\n'
    assert vault_tar.read_bytes() == tar_bytes_before
    assert vault_manifest.read_bytes() == manifest_bytes_before


def test_archive_events_written_for_ok_noop_and_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    append({"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"}, state_dir=state_dir)
    append({"ts": "2026-09-02T10:00:00.000Z", "kind": "command", "target": "c2"}, state_dir=state_dir)

    emitted_calls: list[dict[str, Any]] = []
    real_emit = archive_mod.emit

    def spy_emit(kind: str, event: str, **kwargs: Any) -> None:
        emitted_calls.append({"kind": kind, "event": event, **kwargs})
        real_emit(kind, event, **kwargs)

    monkeypatch.setattr(archive_mod, "emit", spy_emit)

    # 1. Error run (missing vault)
    res_err = archive_month(
        "2026-09",
        state_dir=state_dir,
        vault=tmp_path / "missing_vault",
        today=date(2026, 10, 19),
    )
    assert res_err.status == "error"
    assert res_err.exit_code == 1

    # 2. OK run (valid vault)
    res_ok = archive_month(
        "2026-09",
        state_dir=state_dir,
        vault=vault_dir,
        today=date(2026, 10, 19),
    )
    assert res_ok.status == "ok"
    assert res_ok.exit_code == 0

    # 3. Noop run (already archived in vault)
    res_noop = archive_month(
        "2026-09",
        state_dir=state_dir,
        vault=vault_dir,
        today=date(2026, 10, 19),
    )
    assert res_noop.status == "noop"
    assert res_noop.exit_code == 0

    vault_tar = vault_dir / "agent-hub-audit" / "2026-09.tar.xz"
    expected_sha256 = hashlib.sha256(vault_tar.read_bytes()).hexdigest()

    # Check emit calls carry exact actor_role and detail (month, count, sha256, reason)
    assert len(emitted_calls) == 3
    call_err, call_ok, call_noop = emitted_calls

    assert call_err["kind"] == "archive"
    assert call_err["actor_role"] == "archiver"
    assert call_err["status"] == "error"
    assert "does not exist" in call_err["detail"]["reason"]

    assert call_ok["kind"] == "archive"
    assert call_ok["actor_role"] == "archiver"
    assert call_ok["status"] == "ok"
    assert call_ok["detail"]["month"] == "2026-09"
    assert call_ok["detail"]["count"] == 2
    assert call_ok["detail"]["sha256"] == expected_sha256

    assert call_noop["kind"] == "archive"
    assert call_noop["actor_role"] == "archiver"
    assert call_noop["status"] == "noop"
    assert call_noop["detail"]["month"] == "2026-09"
    assert call_noop["detail"]["sha256"] == expected_sha256

    # Check persisted events on disk
    all_events = _read_all_events(state_dir)
    archive_events = [ev for ev in all_events if ev.get("kind") == "archive"]
    assert len(archive_events) == 3

    ev_err, ev_ok, ev_noop = archive_events

    assert ev_err["actor"] == {"role": "archiver", "host": "agent-hub"}
    assert ev_err["outcome"]["status"] == "error"
    assert ev_err["outcome"]["exit_code"] == 1
    assert ev_err["detail"]["month"] == "2026-09"
    assert "does not exist" in ev_err["detail"]["reason"]

    assert ev_ok["actor"] == {"role": "archiver", "host": "agent-hub"}
    assert ev_ok["outcome"]["status"] == "ok"
    assert ev_ok["outcome"]["exit_code"] == 0
    assert ev_ok["detail"]["month"] == "2026-09"
    assert ev_ok["detail"]["count"] == 2
    assert "sha256" in ev_ok["detail"]
    assert ev_ok["evidence"] == f"sha256:{expected_sha256}"

    assert ev_noop["actor"] == {"role": "archiver", "host": "agent-hub"}
    assert ev_noop["outcome"]["status"] == "noop"
    assert ev_noop["outcome"]["exit_code"] == 0
    assert ev_noop["detail"]["month"] == "2026-09"
    assert "sha256" in ev_noop["detail"]
    assert ev_noop["evidence"] == f"sha256:{expected_sha256}"

    # Verify the audit chain is completely valid
    v_res = verify(state_dir=state_dir)
    assert v_res.ok is True
