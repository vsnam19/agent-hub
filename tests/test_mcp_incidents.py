from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agenthub.cli import UsageError
from agenthub.config import paths
from agenthub.mcpserver import build_server, incidents_data


def _seed(state_dir: Path, rows: list[dict]) -> None:
    lines = [json.dumps(row) for row in rows]
    (state_dir / "incidents.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _row(n: int, kind: str = "review-stall", ts: str = "2026-10-19T10:00:00.000Z") -> dict:
    return {"ts": ts, "kind": kind, "source": "director", "what": f"incident {n}"}


def test_incidents_data_returns_records(env_sandbox):
    state_dir = paths().state_dir
    rows = [_row(1), _row(2, kind="quota"), _row(3)]
    _seed(state_dir, rows)

    data = incidents_data()
    assert data == {"incidents": rows, "truncated": False}

    many = [_row(n) for n in range(501)]
    _seed(state_dir, many)

    bounded = incidents_data()
    assert len(bounded["incidents"]) == 500
    assert bounded["incidents"] == many[-500:]
    assert bounded["truncated"] is True


def test_incidents_data_filters_kind_and_since(env_sandbox):
    state_dir = paths().state_dir
    old_quota = _row(1, kind="quota", ts="2026-10-01T00:00:00.000Z")
    new_stall = _row(2, kind="review-stall", ts="2026-10-19T10:00:00.000Z")
    new_quota = _row(3, kind="quota", ts="2026-10-20T08:00:00.000Z")
    _seed(state_dir, [old_quota, new_stall, new_quota])

    by_kind = incidents_data(kind="quota")
    assert by_kind["incidents"] == [old_quota, new_quota]
    assert by_kind["truncated"] is False

    by_since = incidents_data(since="2026-10-15T00:00:00Z")
    assert by_since["incidents"] == [new_stall, new_quota]

    both = incidents_data(since="2026-10-15T00:00:00Z", kind="quota")
    assert both["incidents"] == [new_quota]


def test_incidents_data_bad_since_raises_usage_error(env_sandbox):
    with pytest.raises(UsageError, match="empty since expression"):
        incidents_data(since="   ")
    with pytest.raises(UsageError, match="invalid since expression"):
        incidents_data(since="not-a-time")


def test_incidents_tool_is_read_only(env_sandbox):
    server = build_server(audit=True)
    tool_map = {t.name: t for t in asyncio.run(server.list_tools())}

    assert "incidents" in tool_map
    tool = tool_map["incidents"]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True

    _seed(paths().state_dir, [_row(1)])
    result = asyncio.run(server.call_tool("incidents", {}))
    assert not result.is_error
    assert "incident 1" in result.content[0].text


def test_no_incident_write_tool(env_sandbox):
    server = build_server(audit=True)
    names = {t.name for t in asyncio.run(server.list_tools())}

    assert {name for name in names if "incident" in name} == {"incidents"}
