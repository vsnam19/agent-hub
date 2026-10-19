import asyncio
import json
from datetime import datetime, timezone

import pytest

from agenthub.audit.show import read_events
from agenthub.audit.writer import append
from agenthub.cli import UsageError
from agenthub.config import paths
from agenthub.mcpserver import (
    audit_archive_rerun_data,
    audit_archive_status_data,
    audit_show_data,
    audit_status_data,
    build_server,
)


def test_audit_status_reports_last_seq_hash_and_pending(env_sandbox, tmp_path):
    p = paths()
    state_dir = p.state_dir

    # 1. Clean state: no events, no pending gap, no archives
    status = audit_status_data()
    now_utc = datetime.now(timezone.utc)
    expected_day = now_utc.strftime("%Y-%m-%d")
    expected_month = expected_day[:7]
    expected_day_file = state_dir / "audit" / expected_month / f"{expected_day}.jsonl"

    assert status["day_file"] == str(expected_day_file)
    assert status["last_seq"] is None
    assert status["last_hash"] is None
    assert status["pending_gap_exists"] is False
    assert status["pending_gap_lines"] == 0
    assert status["latest_archive_month"] is None

    # 2. Append events to current day file
    append({"kind": "command", "target": "ls", "detail": {"argv": ["ls"]}})
    ev2 = append({"kind": "command", "target": "git", "detail": {"argv": ["git", "status"]}})

    status2 = audit_status_data()
    assert status2["day_file"] == str(expected_day_file)
    assert status2["last_seq"] == 2
    assert status2["last_hash"] == ev2["hash"]

    # 3. Pending gap file exists with lines
    pending_file = state_dir / "audit-pending-gap.jsonl"
    pending_file.write_text(
        '{"ts": "2026-10-19T00:00:00Z", "reason": "err1"}\n{"ts": "2026-10-19T00:01:00Z", "reason": "err2"}\n'
    )

    status3 = audit_status_data()
    assert status3["pending_gap_exists"] is True
    assert status3["pending_gap_lines"] == 2

    # 4. Manifests exist in local manifests directory
    manifests_dir = state_dir / "audit" / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / "2026-08.json").write_text(json.dumps({"month": "2026-08"}))
    (manifests_dir / "2026-09.json").write_text(json.dumps({"month": "2026-09"}))

    status4 = audit_status_data()
    assert status4["latest_archive_month"] == "2026-09"


def test_audit_show_bounded_to_500_and_filters(env_sandbox, tmp_path):
    state_dir = paths().state_dir

    # 1. Populate various events with different parents, actors, kinds, and timestamps
    for i in range(10):
        ts = f"2026-10-19T01:00:{i:02d}.000Z"
        append(
            {
                "ts": ts,
                "kind": "command" if i % 2 == 0 else "file_write",
                "target": f"target_{i}",
                "actor": {"job_id": "job_1" if i < 5 else "job_2", "session_id": "sess_1" if i % 3 == 0 else "sess_2"},
                "parent": {"plan": "plan_alpha" if i < 6 else "plan_beta"},
            }
        )

    # Filter by plan
    res_plan = audit_show_data(plan="plan_alpha")
    assert len(res_plan["events"]) == 6
    assert all(ev.get("parent", {}).get("plan") == "plan_alpha" for ev in res_plan["events"])
    assert res_plan["truncated"] is False

    # Filter by job
    res_job = audit_show_data(job="job_1")
    assert len(res_job["events"]) == 5
    assert all(ev.get("actor", {}).get("job_id") == "job_1" for ev in res_job["events"])

    # Filter by session
    res_sess = audit_show_data(session="sess_1")
    assert len(res_sess["events"]) == 4
    assert all(ev.get("actor", {}).get("session_id") == "sess_1" for ev in res_sess["events"])

    # Filter by kinds (single or list)
    res_kinds = audit_show_data(kinds=["file_write"])
    assert len(res_kinds["events"]) == 5
    assert all(ev.get("kind") == "file_write" for ev in res_kinds["events"])

    # Filter by since
    res_since = audit_show_data(since="2026-10-19T01:00:05.000Z")
    assert len(res_since["events"]) == 5
    assert all(ev.get("ts") >= "2026-10-19T01:00:05.000Z" for ev in res_since["events"])

    # 2. Test bounding to last 500 events
    # We append 520 events for plan_gamma
    for i in range(520):
        append(
            {
                "ts": f"2026-10-19T02:{i // 60:02d}:{i % 60:02d}.000Z",
                "kind": "tool",
                "target": f"t_{i}",
                "parent": {"plan": "plan_gamma"},
                "detail": {"index": i},
            }
        )

    res_gamma = audit_show_data(plan="plan_gamma")
    assert res_gamma["truncated"] is True
    assert len(res_gamma["events"]) == 500
    # Must be the LAST 500 events (indices 20 to 519)
    assert res_gamma["events"][0]["detail"]["index"] == 20
    assert res_gamma["events"][-1]["detail"]["index"] == 519

    # 3. Test skipped_months when archive manifest exists but vault archive is missing
    manifests_dir = state_dir / "audit" / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / "2026-08.json").write_text(json.dumps({"month": "2026-08"}))
    res_archive = audit_show_data()
    assert "2026-08" in res_archive["skipped_months"]


def test_audit_archive_status_lists_manifests(env_sandbox, tmp_path):
    p = paths()
    state_dir = p.state_dir

    # 1. Empty manifests directory
    manifests = audit_archive_status_data()
    assert manifests == []

    # 2. Populate manifests directory with multiple manifests
    manifests_dir = state_dir / "audit" / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    m1_data = {
        "month": "2026-07",
        "count": 142,
        "sha256": "aaaa" * 16,
        "created": "2026-08-01T00:00:00Z",
        "files": ["2026-07-01.jsonl"],
        "first_hash": "1111" * 16,
        "last_hash": "2222" * 16,
    }
    m2_data = {
        "month": "2026-08",
        "count": 289,
        "sha256": "bbbb" * 16,
        "created": "2026-09-01T00:00:00Z",
        "files": ["2026-08-01.jsonl"],
        "first_hash": "3333" * 16,
        "last_hash": "4444" * 16,
    }

    # Write out of order and include a non-manifest file
    (manifests_dir / "2026-08.json").write_text(json.dumps(m2_data))
    (manifests_dir / "2026-07.json").write_text(json.dumps(m1_data))
    (manifests_dir / "other.json").write_text(json.dumps({"foo": "bar"}))
    (manifests_dir / "corrupted.json").write_text("not json")

    results = audit_archive_status_data()
    assert len(results) == 2

    # Should be sorted by month
    assert results[0] == {
        "month": "2026-07",
        "count": 142,
        "sha256": "aaaa" * 16,
        "created": "2026-08-01T00:00:00Z",
    }
    assert results[1] == {
        "month": "2026-08",
        "count": 289,
        "sha256": "bbbb" * 16,
        "created": "2026-09-01T00:00:00Z",
    }


def test_archive_rerun_rejects_paths_and_bad_months(env_sandbox):
    bad_inputs = [
        None,
        123,
        True,
        "",
        "   ",
        "2026",
        "2026-8",
        "2026-08-01",
        "2026-13",
        "2026-00",
        "../2026-08",
        "2026-08/..",
        "2026/08",
        "/etc/passwd",
        "2026-08;rm -rf",
    ]
    for bad in bad_inputs:
        with pytest.raises((UsageError, TypeError, ValueError)):
            audit_archive_rerun_data(bad)

    # Check no events were emitted
    events = list(read_events())
    assert len(events) == 0


def test_archive_rerun_archives_given_month(env_sandbox, tmp_path):
    p = paths()
    state_dir = p.state_dir
    config_dir = p.config_dir

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    audit_toml = config_dir / "audit.toml"
    audit_toml.write_text(f'vault = "{vault_dir}"\n')

    # Append events for 2026-08
    append({"ts": "2026-08-01T10:00:00.000Z", "kind": "command", "target": "c1"})
    append({"ts": "2026-08-15T12:00:00.000Z", "kind": "command", "target": "c2"})

    # Check month directory exists locally
    assert (state_dir / "audit" / "2026-08").is_dir()

    # Run rerun
    res = audit_archive_rerun_data("2026-08")
    assert res["status"] == "ok"
    assert res["exit_code"] == 0

    # Local month dir is deleted
    assert not (state_dir / "audit" / "2026-08").exists()

    # Vault tarball and manifests exist
    vault_tar = vault_dir / "agent-hub-audit" / "2026-08.tar.xz"
    assert vault_tar.is_file()
    assert (vault_dir / "agent-hub-audit" / "2026-08.json").is_file()
    assert (state_dir / "audit" / "manifests" / "2026-08.json").is_file()

    # Re-run for already archived month is idempotent noop
    res_noop = audit_archive_rerun_data("2026-08")
    assert res_noop["status"] == "noop"
    assert res_noop["exit_code"] == 0


def test_tools_registered_with_expected_annotations(env_sandbox):
    server = build_server(audit=True)
    tools = asyncio.run(server.list_tools())
    tool_map = {t.name: t for t in tools}

    assert "audit_status" in tool_map
    assert "audit_show" in tool_map
    assert "audit_archive_status" in tool_map
    assert "audit_archive_rerun" in tool_map

    # Check annotations
    for name in ["audit_status", "audit_show", "audit_archive_status"]:
        tool = tool_map[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True

    rerun_tool = tool_map["audit_archive_rerun"]
    assert rerun_tool.annotations is not None
    assert rerun_tool.annotations.read_only_hint is False
    assert rerun_tool.annotations.destructive_hint is False
    assert rerun_tool.annotations.idempotent_hint is True

    # Check calling audit_status tool through the server
    status_res = asyncio.run(server.call_tool("audit_status", {}))
    assert not status_res.is_error
    assert len(status_res.content) == 1
    assert "day_file" in status_res.content[0].text

    # Check calling audit_archive_status tool through the server
    archive_status_res = asyncio.run(server.call_tool("audit_archive_status", {}))
    assert not archive_status_res.is_error

    # Check calling audit_show tool through the server
    show_res = asyncio.run(server.call_tool("audit_show", {}))
    assert not show_res.is_error
    assert "events" in show_res.content[0].text
