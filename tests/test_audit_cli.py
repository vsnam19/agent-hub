from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthub.audit.show import format_line
from agenthub.audit.writer import append, audit_root
from agenthub.cli import main


def test_show_prints_timeline_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    e1 = append(
        {
            "ts": "2026-10-18T10:00:00.000Z",
            "kind": "session",
            "target": "start",
            "actor": {"role": "director", "host": "claude"},
        },
        state_dir=state_dir,
    )
    e2 = append(
        {
            "ts": "2026-10-18T10:01:00.000Z",
            "kind": "command",
            "target": "git status",
            "actor": {"role": "director", "host": "claude"},
            "outcome": {"status": "ok", "exit_code": 0},
        },
        state_dir=state_dir,
    )

    rc = main(["audit", "show"])
    assert rc == 0

    out, err = capsys.readouterr()
    assert err == ""
    lines = out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0] == format_line(e1)
    assert lines[1] == format_line(e2)


def test_show_json_one_object_per_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    e1 = append(
        {"ts": "2026-10-18T10:00:00.000Z", "kind": "session", "target": "start"},
        state_dir=state_dir,
    )
    e2 = append(
        {"ts": "2026-10-18T10:01:00.000Z", "kind": "command", "target": "ls"},
        state_dir=state_dir,
    )

    rc = main(["audit", "show", "--json"])
    assert rc == 0

    out, err = capsys.readouterr()
    assert err == ""
    lines = out.strip().splitlines()
    assert len(lines) == 2
    obj1 = json.loads(lines[0])
    obj2 = json.loads(lines[1])
    assert obj1 == e1
    assert obj2 == e2


def test_verify_clean_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "cmd1"}, state_dir=state_dir)
    append({"ts": "2026-10-18T10:00:01.000Z", "kind": "command", "target": "cmd2"}, state_dir=state_dir)

    rc = main(["audit", "verify"])
    assert rc == 0

    out, err = capsys.readouterr()
    assert err == ""
    assert "ok: 2 events" in out

    rc_month = main(["audit", "verify", "--month", "2026-10"])
    assert rc_month == 0
    out_m, _ = capsys.readouterr()
    assert "ok: 2 events" in out_m


def test_verify_break_exits_one_and_names_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    append({"ts": "2026-10-18T10:00:00.000Z", "kind": "command", "target": "one"}, state_dir=state_dir)
    append({"ts": "2026-10-18T10:00:01.000Z", "kind": "command", "target": "two"}, state_dir=state_dir)

    day_file = audit_root(state_dir) / "2026-10" / "2026-10-18.jsonl"
    lines = day_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    # Tamper the second event
    record = json.loads(lines[1])
    record["target"] = "tampered"
    lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    day_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = main(["audit", "verify"])
    assert rc == 1

    out, _ = capsys.readouterr()
    assert "2026-10/2026-10-18.jsonl:2" in out
    assert "seq=2" in out
    assert "hash mismatch" in out


def test_outward_lists_only_outward_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    append({"ts": "2026-10-18T09:00:00.000Z", "kind": "command", "target": "git fetch"}, state_dir=state_dir)
    e2 = append(
        {"ts": "2026-10-18T10:00:00.000Z", "kind": "outward", "target": "git push origin main"},
        state_dir=state_dir,
    )
    append({"ts": "2026-10-18T11:00:00.000Z", "kind": "tool", "target": "edit"}, state_dir=state_dir)
    e4 = append({"ts": "2026-10-18T12:00:00.000Z", "kind": "outward", "target": "gh pr create"}, state_dir=state_dir)

    rc = main(["audit", "outward"])
    assert rc == 0

    out, err = capsys.readouterr()
    assert err == ""
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0] == format_line(e2)
    assert lines[1] == format_line(e4)


def test_show_skipped_months_go_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    manifests_dir = audit_root(state_dir) / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / "2026-08.json").write_text(json.dumps({"month": "2026-08"}), encoding="utf-8")
    (manifests_dir / "2026-09.json").write_text(json.dumps({"month": "2026-09"}), encoding="utf-8")

    e_oct = append(
        {"ts": "2026-10-01T10:00:00.000Z", "kind": "command", "target": "c1"},
        state_dir=state_dir,
    )

    empty_cfg = tmp_path / "empty_cfg"
    empty_cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(empty_cfg))

    rc = main(["audit", "show"])
    assert rc == 0

    out, err = capsys.readouterr()
    assert out.splitlines() == [format_line(e_oct)]
    assert "agent-hub: skipped archived months (vault unreachable): 2026-08, 2026-09" in err


def test_bad_since_is_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    rc = main(["audit", "show", "--since", "junk"])
    assert rc == 2
    _, err = capsys.readouterr()
    assert "agent-hub: " in err
    assert "since" in err

    rc_outward = main(["audit", "outward", "--since", "invalid-date"])
    assert rc_outward == 2
    _, err_outward = capsys.readouterr()
    assert "agent-hub: " in err_outward
    assert "since" in err_outward


def test_show_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    e1 = append(
        {
            "ts": "2026-10-18T10:00:00.000Z",
            "kind": "session",
            "actor": {"role": "director", "host": "claude", "session_id": "sess-alpha"},
        },
        state_dir=state_dir,
    )
    e2 = append(
        {
            "ts": "2026-10-18T10:01:00.000Z",
            "kind": "command",
            "target": "cargo check",
            "parent": {"plan": "plan-xyz", "job": "job-abc"},
        },
        state_dir=state_dir,
    )
    e3 = append(
        {"ts": "2026-10-18T10:02:00.000Z", "kind": "gap", "target": "network failure"},
        state_dir=state_dir,
    )

    # Filter by plan
    rc = main(["audit", "show", "--plan", "plan-xyz"])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert out.splitlines() == [format_line(e2)]

    # Filter by job
    rc = main(["audit", "show", "--job", "job-abc"])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert out.splitlines() == [format_line(e2)]

    # Filter by session
    rc = main(["audit", "show", "--session", "sess-alpha"])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert out.splitlines() == [format_line(e1)]

    # Filter by kind
    rc = main(["audit", "show", "--kind", "gap"])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert out.splitlines() == [format_line(e3)]

    # Filter by since
    rc = main(["audit", "show", "--since", "2026-10-18T10:01:30.000Z"])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert out.splitlines() == [format_line(e3)]


def test_verify_invalid_month_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    rc = main(["audit", "verify", "--month", "not-a-month"])
    assert rc == 2
    _, err = capsys.readouterr()
    assert "agent-hub: " in err
    assert "invalid month format" in err


def test_outward_with_since_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    append(
        {"ts": "2026-10-18T10:00:00.000Z", "kind": "outward", "target": "git push 1"},
        state_dir=state_dir,
    )
    e2 = append(
        {"ts": "2026-10-18T12:00:00.000Z", "kind": "outward", "target": "git push 2"},
        state_dir=state_dir,
    )

    rc = main(["audit", "outward", "--since", "2026-10-18T11:00:00.000Z", "--json"])
    assert rc == 0
    out, _ = capsys.readouterr()
    lines = out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == e2


def test_audit_missing_subcommand_exit_two(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["audit"])
    assert rc == 2
    _, err = capsys.readouterr()
    assert "agent-hub: " in err
