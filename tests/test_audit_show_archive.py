from __future__ import annotations

import io
import json
import os
import tarfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agenthub.audit.archive import ArchiveResult, archive_month
from agenthub.audit.show import (
    read_events,
    read_events_with_archives,
)
from agenthub.audit.writer import append, audit_root


def _make_tarball_with_bad_line(tar_path: Path, month: str, valid_events: list[dict[str, Any]]) -> None:
    lines = [
        json.dumps(valid_events[0]),
        "this is a corrupt json line {{{",
        "",
        "12345",
        json.dumps(valid_events[1]),
    ]
    content = ("\n".join(lines) + "\n").encode("utf-8")
    with tarfile.open(tar_path, "w:xz") as tar:
        ti = tarfile.TarInfo(name=f"{month}/{month}-01.jsonl")
        ti.size = len(content)
        ti.mtime = 1700000000
        tar.addfile(ti, io.BytesIO(content))


def test_archived_month_events_included_oldest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "audit.toml").write_text(f'vault = "{vault_dir}"\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(config_dir))

    # 1. Events in 2026-09 and 2026-10
    e_sep1 = append(
        {"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "ls"},
        state_dir=state_dir,
    )
    e_sep2 = append(
        {"ts": "2026-09-15T12:00:00.000Z", "kind": "tool", "target": "edit"},
        state_dir=state_dir,
    )
    e_oct1 = append(
        {"ts": "2026-10-01T08:00:00.000Z", "kind": "session", "target": "start"},
        state_dir=state_dir,
    )
    e_oct2 = append(
        {"ts": "2026-10-02T09:00:00.000Z", "kind": "outward", "target": "push"},
        state_dir=state_dir,
    )

    # 2. Archived month 2026-09 via archive_month
    res = archive_month("2026-09", state_dir=state_dir, vault=vault_dir, today=date(2026, 10, 19))
    assert isinstance(res, ArchiveResult)
    assert res.status == "ok"

    # Inject a bad line into the tarball to verify tolerating bad lines and in-memory extraction
    vault_audit_dir = vault_dir / "agent-hub-audit"
    tar_path = vault_audit_dir / "2026-09.tar.xz"
    assert tar_path.is_file()
    _make_tarball_with_bad_line(tar_path, "2026-09", [e_sep1, e_sep2])

    # 3. Call read_events_with_archives
    events, skipped = read_events_with_archives(state_dir=state_dir, since=None)

    assert skipped == []
    # 2 events from 2026-09 archive + 2 manual 2026-10 events + 1 archive event emitted by archive_month
    assert len(events) == 5
    assert events[:4] == [e_sep1, e_sep2, e_oct1, e_oct2]
    assert events[4]["kind"] == "archive"

    # Verify oldest first
    assert [ev["ts"] for ev in events[:4]] == [
        "2026-09-01T10:00:00.000Z",
        "2026-09-15T12:00:00.000Z",
        "2026-10-01T08:00:00.000Z",
        "2026-10-02T09:00:00.000Z",
    ]
    assert events[4]["ts"] >= events[3]["ts"]

    # Verify explicit vault argument works without relying on config
    events_explicit, skipped_explicit = read_events_with_archives(
        state_dir=state_dir,
        since=None,
        vault=vault_dir,
    )
    assert skipped_explicit == []
    assert events_explicit == events

    # Verify no files were extracted to disk
    extracted = list(tmp_path.rglob("*.jsonl"))
    # Only the local 2026-10 day files exist in state_dir, none from 2026-09
    assert all("2026-09" not in str(p) for p in extracted)


def test_since_skips_older_archives(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_audit_dir = vault_dir / "agent-hub-audit"
    vault_audit_dir.mkdir(parents=True, exist_ok=True)

    root = audit_root(state_dir)
    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    # Create manifests for 2026-07, 2026-08, 2026-09
    for m in ("2026-07", "2026-08", "2026-09"):
        (manifests_dir / f"{m}.json").write_text(json.dumps({"month": m}), encoding="utf-8")

    # Only 2026-09 archive exists in vault (2026-07 and 2026-08 do NOT exist in vault)
    e_sep = {"ts": "2026-09-15T10:00:00.000Z", "kind": "command", "target": "c_sep"}
    _make_tarball_with_bad_line(
        vault_audit_dir / "2026-09.tar.xz",
        "2026-09",
        [e_sep, {"ts": "2026-09-15T11:00:00.000Z", "kind": "command", "target": "c_sep2"}],
    )

    # Local events in 2026-10
    e_oct = append(
        {"ts": "2026-10-01T12:00:00.000Z", "kind": "command", "target": "c_oct"},
        state_dir=state_dir,
    )

    # Query with since in 2026-09:
    # 2026-07 and 2026-08 are older than 2026-09 so they must be SKIPPED without checking vault.
    # Because 2026-07 and 2026-08 are not accessed, their missing archives do NOT cause them to be in skipped list.
    events, skipped = read_events_with_archives(
        state_dir=state_dir,
        since="2026-09-01T00:00:00.000Z",
        vault=vault_dir,
    )

    assert skipped == []
    assert len(events) == 3
    assert events[0]["ts"] == "2026-09-15T10:00:00.000Z"
    assert events[1]["ts"] == "2026-09-15T11:00:00.000Z"
    assert events[2]["ts"] == "2026-10-01T12:00:00.000Z"

    # Query with since month form "2026-09"
    events_m, skipped_m = read_events_with_archives(
        state_dir=state_dir,
        since="2026-09",
        vault=vault_dir,
    )
    assert skipped_m == []
    assert len(events_m) == 3

    # Query with since in 2026-10 skips all archived months (2026-07, 2026-08, 2026-09)
    events_oct, skipped_oct = read_events_with_archives(
        state_dir=state_dir,
        since="2026-10-01T00:00:00.000Z",
        vault=vault_dir,
    )
    assert skipped_oct == []
    assert len(events_oct) == 1
    assert events_oct[0] == e_oct


def test_vault_unreachable_lists_skipped_months(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    root = audit_root(state_dir)
    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    # Two archived months recorded in manifests
    (manifests_dir / "2026-08.json").write_text(json.dumps({"month": "2026-08"}), encoding="utf-8")
    (manifests_dir / "2026-09.json").write_text(json.dumps({"month": "2026-09"}), encoding="utf-8")

    # Local events in 2026-10
    e_oct = append(
        {"ts": "2026-10-01T10:00:00.000Z", "kind": "command", "target": "c1"},
        state_dir=state_dir,
    )

    # 1. Config missing or invalid -> ArchiveError from load_vault()
    empty_cfg = tmp_path / "empty_cfg"
    empty_cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(empty_cfg))

    events_no_cfg, skipped_no_cfg = read_events_with_archives(state_dir=state_dir, since=None)
    assert skipped_no_cfg == ["2026-08", "2026-09"]
    assert events_no_cfg == [e_oct]

    # 2. Vault path does not exist
    missing_vault = tmp_path / "nonexistent_vault"
    events_miss, skipped_miss = read_events_with_archives(
        state_dir=state_dir,
        since=None,
        vault=missing_vault,
    )
    assert skipped_miss == ["2026-08", "2026-09"]
    assert events_miss == [e_oct]

    # 3. Vault exists, but 2026-08 archive exists and 2026-09 archive is missing
    partial_vault = tmp_path / "partial_vault"
    partial_audit = partial_vault / "agent-hub-audit"
    partial_audit.mkdir(parents=True, exist_ok=True)

    e_aug1 = {"ts": "2026-08-10T10:00:00.000Z", "kind": "command", "target": "c_aug1"}
    e_aug2 = {"ts": "2026-08-10T11:00:00.000Z", "kind": "command", "target": "c_aug2"}
    _make_tarball_with_bad_line(partial_audit / "2026-08.tar.xz", "2026-08", [e_aug1, e_aug2])

    events_part, skipped_part = read_events_with_archives(
        state_dir=state_dir,
        since=None,
        vault=partial_vault,
    )
    assert skipped_part == ["2026-09"]
    assert events_part == [e_aug1, e_aug2, e_oct]

    # 4. Corrupted archive file that cannot be opened as tar
    corrupt_tar = partial_audit / "2026-09.tar.xz"
    corrupt_tar.write_bytes(b"garbage not an xz archive")
    events_corrupt, skipped_corrupt = read_events_with_archives(
        state_dir=state_dir,
        since=None,
        vault=partial_vault,
    )
    assert skipped_corrupt == ["2026-09"]
    assert events_corrupt == [e_aug1, e_aug2, e_oct]


def test_local_only_behaviour_unchanged(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    root = audit_root(state_dir)
    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    # Manifest and vault archive exist for 2026-09
    (manifests_dir / "2026-09.json").write_text(json.dumps({"month": "2026-09"}), encoding="utf-8")
    vault_dir = tmp_path / "vault"
    vault_audit_dir = vault_dir / "agent-hub-audit"
    vault_audit_dir.mkdir(parents=True, exist_ok=True)
    _make_tarball_with_bad_line(
        vault_audit_dir / "2026-09.tar.xz",
        "2026-09",
        [
            {"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"},
            {"ts": "2026-09-01T11:00:00.000Z", "kind": "command", "target": "c2"},
        ],
    )

    # Local events in 2026-10
    e_oct1 = append(
        {"ts": "2026-10-01T08:00:00.000Z", "kind": "command", "target": "c_oct1"},
        state_dir=state_dir,
    )
    e_oct2 = append(
        {"ts": "2026-10-02T09:00:00.000Z", "kind": "command", "target": "c_oct2"},
        state_dir=state_dir,
    )

    # read_events returns an iterator
    events_iter = read_events(state_dir=state_dir)
    assert hasattr(events_iter, "__next__")
    assert hasattr(events_iter, "__iter__")

    events = list(events_iter)
    # read_events returns ONLY local events, ignoring manifests and vault archives
    assert len(events) == 2
    assert events == [e_oct1, e_oct2]


def test_corrupt_xz_archive_skipped(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_audit_dir = vault_dir / "agent-hub-audit"
    vault_audit_dir.mkdir(parents=True, exist_ok=True)

    root = audit_root(state_dir)
    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / "2026-08.json").write_text(json.dumps({"month": "2026-08"}), encoding="utf-8")

    # Build a valid .tar.xz archive with several thousand lines of incompressible data,
    # then corrupt bytes in its middle so the first tar header still reads but later data fails.
    lines = [
        f'{{"ts": "2026-08-01T{i % 24:02d}:00:00.000Z", "data": "{os.urandom(64).hex()}"}}\n'.encode("utf-8")
        for i in range(3000)
    ]
    content = b"".join(lines)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        ti = tarfile.TarInfo(name="2026-08/2026-08-01.jsonl")
        ti.size = len(content)
        tar.addfile(ti, io.BytesIO(content))

    compressed = bytearray(buf.getvalue())
    compressed[len(compressed) // 2] ^= 0xFF

    tar_path = vault_audit_dir / "2026-08.tar.xz"
    tar_path.write_bytes(compressed)

    # Local events in 2026-10
    e_oct = append(
        {"ts": "2026-10-01T10:00:00.000Z", "kind": "command", "target": "c1"},
        state_dir=state_dir,
    )

    events, skipped = read_events_with_archives(
        state_dir=state_dir,
        since=None,
        vault=vault_dir,
    )

    assert "2026-08" in skipped
    assert skipped == ["2026-08"]
    assert events == [e_oct]


def test_month_local_and_archived_deduped(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    vault_dir = tmp_path / "vault"
    vault_audit_dir = vault_dir / "agent-hub-audit"
    vault_audit_dir.mkdir(parents=True, exist_ok=True)

    root = audit_root(state_dir)
    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / "2026-09.json").write_text(json.dumps({"month": "2026-09"}), encoding="utf-8")

    # Local events in 2026-09
    e_sep1 = append(
        {"ts": "2026-09-01T10:00:00.000Z", "kind": "command", "target": "c1"},
        state_dir=state_dir,
    )
    e_sep2 = append(
        {"ts": "2026-09-02T10:00:00.000Z", "kind": "command", "target": "c2"},
        state_dir=state_dir,
    )

    # In archive, include e_sep1 and e_sep2 (with source='archive' to distinguish),
    # an archive-only event, and an event with no event_id
    e_arch1 = dict(e_sep1, source="archive")
    e_arch2 = dict(e_sep2, source="archive")
    e_arch_only = {
        "ts": "2026-09-03T10:00:00.000Z",
        "kind": "command",
        "target": "c3",
        "event_id": "arch-only",
        "source": "archive",
    }
    e_no_id_arch = {
        "ts": "2026-09-04T10:00:00.000Z",
        "kind": "command",
        "target": "c4_no_id_arch",
        "source": "archive",
    }

    arch_lines = [
        json.dumps(e_arch1),
        json.dumps(e_arch2),
        json.dumps(e_arch_only),
        json.dumps(e_no_id_arch),
    ]
    content = ("\n".join(arch_lines) + "\n").encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        ti = tarfile.TarInfo(name="2026-09/2026-09-01.jsonl")
        ti.size = len(content)
        tar.addfile(ti, io.BytesIO(content))
    (vault_audit_dir / "2026-09.tar.xz").write_bytes(buf.getvalue())

    # In local 2026-09: add an event with no event_id to 2026-09-02.jsonl
    no_id_local = {
        "ts": "2026-09-02T12:00:00.000Z",
        "kind": "command",
        "target": "c5_no_id_local",
        "hash": "0" * 64,
    }
    with open(root / "2026-09" / "2026-09-02.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(no_id_local) + "\n")

    # Local event in 2026-10
    e_oct = append(
        {"ts": "2026-10-01T10:00:00.000Z", "kind": "command", "target": "c_oct"},
        state_dir=state_dir,
    )

    events, skipped = read_events_with_archives(
        state_dir=state_dir,
        since=None,
        vault=vault_dir,
    )

    assert skipped == []

    # Assert that each shared event_id appears exactly once
    event_ids = [ev["event_id"] for ev in events if "event_id" in ev and ev["event_id"]]
    assert event_ids.count(e_sep1["event_id"]) == 1
    assert event_ids.count(e_sep2["event_id"]) == 1
    assert event_ids.count("arch-only") == 1
    assert event_ids.count(e_oct["event_id"]) == 1

    # Assert that for the shared event_ids, the local event was kept (local copy wins)
    shared_1 = next(ev for ev in events if ev.get("event_id") == e_sep1["event_id"])
    assert shared_1 == e_sep1
    assert "source" not in shared_1
    shared_2 = next(ev for ev in events if ev.get("event_id") == e_sep2["event_id"])
    assert shared_2 == e_sep2
    assert "source" not in shared_2

    # Assert events with no event_id are both preserved
    targets = [ev.get("target") for ev in events]
    assert "c4_no_id_arch" in targets
    assert "c5_no_id_local" in targets

    # Assert that the output is oldest first
    timestamps = [str(ev.get("ts", "")) for ev in events]
    assert timestamps == sorted(timestamps)
