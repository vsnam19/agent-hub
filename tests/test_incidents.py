from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenthub import incidents
from agenthub.audit.show import read_events

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _raw_lines(state_dir: Path) -> list[str]:
    return (state_dir / "incidents.jsonl").read_text(encoding="utf-8").splitlines()


def test_add_writes_line_with_mode_600(tmp_path: Path) -> None:
    record = incidents.add(
        tmp_path,
        kind="review-stall",
        what="reviewer hung on plan",
        source="techlead",
        plan="p-1",
        task="t-2",
        job="job-9",
        attempts=2,
        rounds=3,
        now=NOW,
    )

    path = tmp_path / "incidents.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = _raw_lines(tmp_path)
    assert len(lines) == 1
    assert json.loads(lines[0]) == record
    assert record == {
        "ts": "2026-10-20T12:00:00.000Z",
        "kind": "review-stall",
        "source": "techlead",
        "plan": "p-1",
        "task": "t-2",
        "job": "job-9",
        "cost": {"attempts": 2, "rounds": 3},
        "what": "reviewer hung on plan",
    }

    incidents.add(tmp_path, kind="k", what="second", now=NOW)
    assert len(_raw_lines(tmp_path)) == 2
    assert "cost" not in json.loads(_raw_lines(tmp_path)[1])


def test_add_redacts_and_truncates_text(tmp_path: Path) -> None:
    ghp = "ghp_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"
    record = incidents.add(
        tmp_path,
        kind="leak",
        what=f"token was {ghp} " + "x" * 600,
        lesson="never paste " + ghp,
        now=NOW,
    )

    assert ghp not in _raw_lines(tmp_path)[0]
    assert ghp not in record["what"]
    assert ghp not in record["lesson"]
    assert len(record["what"]) == 500
    assert len(incidents.add(tmp_path, kind="long", what="y" * 900, now=NOW)["what"]) == 500


def test_add_emits_audit_event(tmp_path: Path) -> None:
    incidents.add(
        tmp_path,
        kind="quota",
        what="agy quota ran out",
        lesson="switch chain early",
        source="tool",
        job="job-7",
        minutes=5,
        now=NOW,
    )

    events = list(read_events(state_dir=tmp_path))
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "incident"
    assert ev["target"] == "quota"
    assert ev["quote"] == "agy quota ran out"
    assert ev["detail"]["event"] == "add"
    assert ev["detail"]["source"] == "tool"
    assert ev["detail"]["job"] == "job-7"
    assert ev["detail"]["cost"] == {"minutes": 5}
    assert "what" not in ev["detail"]
    assert "lesson" not in ev["detail"]


def test_add_rejects_empty_kind_what_and_bad_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        incidents.add(tmp_path, kind="", what="x", now=NOW)
    with pytest.raises(ValueError):
        incidents.add(tmp_path, kind="   ", what="x", now=NOW)
    with pytest.raises(ValueError):
        incidents.add(tmp_path, kind="k", what="", now=NOW)
    with pytest.raises(ValueError):
        incidents.add(tmp_path, kind="k", what="x", source="robot", now=NOW)

    assert not (tmp_path / "incidents.jsonl").exists()
    assert incidents.read(tmp_path) == []


def test_read_accepts_summary_as_what(tmp_path: Path) -> None:
    (tmp_path / "incidents.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-10-01T00:00:00Z", "kind": "old", "source": "director", "summary": "old text"}),
                "not json at all",
                json.dumps(["a", "list"]),
                json.dumps({"ts": "2026-10-02T00:00:00Z", "kind": "new", "source": "owner", "what": "new text"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows = incidents.read(tmp_path)
    assert [row["kind"] for row in rows] == ["old", "new"]
    assert rows[0]["what"] == "old text"
    assert rows[1]["what"] == "new text"
    assert incidents.read(tmp_path / "missing") == []


def test_read_filters_since_and_kind(tmp_path: Path) -> None:
    (tmp_path / "incidents.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-10-01T00:00:00Z", "kind": "a", "source": "tool", "what": "early a"}),
                json.dumps({"ts": "2026-10-19T08:00:00+00:00", "kind": "b", "source": "tool", "what": "b"}),
                json.dumps({"ts": "garbage", "kind": "a", "source": "tool", "what": "bad ts"}),
                json.dumps({"ts": "2026-10-20T10:00:00Z", "kind": "a", "source": "tool", "what": "late a"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert [r["what"] for r in incidents.read(tmp_path)] == ["early a", "b", "bad ts", "late a"]
    since = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    assert [r["what"] for r in incidents.read(tmp_path, since=since)] == ["b", "late a"]
    assert [r["what"] for r in incidents.read(tmp_path, kind="a")] == ["early a", "bad ts", "late a"]
    assert [r["what"] for r in incidents.read(tmp_path, since=since, kind="a")] == ["late a"]
