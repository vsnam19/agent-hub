from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenthub import incidents, skillmine

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=7)
RECENT = NOW - timedelta(days=2)
OLD = NOW - timedelta(days=60)


def _mine(tmp_path: Path) -> skillmine.Extract:
    return skillmine.mine(tmp_path / "state", tmp_path / "claude", SINCE, NOW)


def _write_raw(state_dir: Path, rows: list[dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    (state_dir / "incidents.jsonl").write_text(text, encoding="utf-8")


def test_mine_reads_incidents_in_window(tmp_path: Path) -> None:
    state = tmp_path / "state"
    incidents.add(state, kind="review-stall", what="reviewer hung", now=RECENT)
    incidents.add(state, kind="review-stall", what="too old", now=OLD)

    extract = _mine(tmp_path)

    assert [row["what"] for row in extract.incidents] == ["reviewer hung"]
    assert extract.incidents[0]["kind"] == "review-stall"


def test_mine_accepts_summary_rows(tmp_path: Path) -> None:
    _write_raw(
        tmp_path / "state",
        [{"ts": RECENT.isoformat(), "kind": "wrong-model", "summary": "chain picked an unknown model"}],
    )

    extract = _mine(tmp_path)

    assert len(extract.incidents) == 1
    assert extract.incidents[0]["what"] == "chain picked an unknown model"


def test_mine_counts_incidents_per_kind(tmp_path: Path) -> None:
    state = tmp_path / "state"
    incidents.add(state, kind="review-stall", what="a", now=RECENT)
    incidents.add(state, kind="review-stall", what="b", now=RECENT)
    incidents.add(state, kind="wrong-model", what="c", now=RECENT)
    incidents.add(state, kind="review-stall", what="old", now=OLD)

    extract = _mine(tmp_path)

    assert extract.incident_counts == {"review-stall": 2, "wrong-model": 1}


def test_mine_without_incident_file_is_empty(tmp_path: Path) -> None:
    assert not (tmp_path / "state" / "incidents.jsonl").exists()

    extract = _mine(tmp_path)

    assert extract.incidents == []
    assert extract.incident_counts == {}


def test_extract_json_includes_incidents(tmp_path: Path) -> None:
    incidents.add(tmp_path / "state", kind="review-stall", what="reviewer hung", now=RECENT)

    data = _mine(tmp_path).to_json()

    assert data["incident_counts"] == {"review-stall": 1}
    assert [row["what"] for row in data["incidents"]] == ["reviewer hung"]
