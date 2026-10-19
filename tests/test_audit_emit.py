from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agenthub.audit.emit import emit
from agenthub.audit.trace import (
    ENV_TRACE_ATTEMPT,
    ENV_TRACE_JOB,
    ENV_TRACE_PLAN,
    ENV_TRACE_ROLE,
    ENV_TRACE_TASK,
)
from agenthub.audit.writer import audit_root


def _read_all_events(state_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    root = audit_root(state_dir)
    for p in sorted(root.glob("*/*.jsonl")):
        if p.name.endswith(".lock"):
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def test_emit_writes_job_event_with_parents(tmp_path: Path) -> None:
    emit(
        "job",
        "finish",
        state_dir=tmp_path,
        parent={"plan": "p-1", "task": "t-1", "job": "job-100", "attempt": 2},
        target="tests/test_foo.py",
        status="ok",
        exit_code=0,
        duration_ms=450,
        job_id="job-100",
        quote="Tests passed",
        evidence="/path/to/log",
    )

    events = _read_all_events(tmp_path)
    assert len(events) == 1
    ev = events[0]

    assert ev["kind"] == "job"
    assert ev["actor"] == {"role": "agent-hub", "host": "agent-hub", "job_id": "job-100"}
    assert ev["parent"] == {"plan": "p-1", "task": "t-1", "job": "job-100", "attempt": 2}
    assert ev["target"] == "tests/test_foo.py"
    assert ev["outcome"] == {"status": "ok", "exit_code": 0, "duration_ms": 450}
    assert ev["detail"] == {"event": "finish"}
    assert ev["quote"] == "Tests passed"
    assert ev["evidence"] == "/path/to/log"
    assert ev["seq"] == 1
    assert "ts" in ev
    assert "hash" in ev
    assert ev["schema_version"] == 1


def test_emit_plan_kind_and_detail_event(tmp_path: Path) -> None:
    emit(
        "plan",
        "triage",
        state_dir=tmp_path,
        detail={"decision": "retry", "score": 42},
    )

    events = _read_all_events(tmp_path)
    assert len(events) == 1
    ev = events[0]

    assert ev["kind"] == "plan"
    assert ev["actor"] == {"role": "agent-hub", "host": "agent-hub"}
    assert "job_id" not in ev["actor"]
    assert ev["target"] == ""
    assert ev["outcome"] == {"status": "ok"}
    assert ev["detail"] == {"event": "triage", "decision": "retry", "score": 42}
    assert "quote" not in ev
    assert "evidence" not in ev


def test_emit_never_raises_when_writer_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _exploding_write(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("simulated writer failure")

    monkeypatch.setattr("agenthub.audit.emit.write", _exploding_write)

    res = emit(
        "job",
        "start",
        state_dir=tmp_path,
        target="failing_action",
    )
    assert res is None


def test_emit_uses_given_state_dir_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other_dir = tmp_path / "global_state"
    other_dir.mkdir()
    monkeypatch.setenv("AGENT_HUB_STATE", str(other_dir))

    specific_state_dir = tmp_path / "custom_state"
    specific_state_dir.mkdir()

    emit(
        "job",
        "start",
        state_dir=specific_state_dir,
        job_id="job-custom",
    )

    events_specific = _read_all_events(specific_state_dir)
    assert len(events_specific) == 1
    assert events_specific[0]["actor"]["job_id"] == "job-custom"

    global_audit = other_dir / "audit"
    assert not global_audit.exists()


def test_emit_parent_defaults_to_env_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_TRACE_PLAN, "plan-alpha")
    monkeypatch.setenv(ENV_TRACE_TASK, "task-beta")
    monkeypatch.setenv(ENV_TRACE_JOB, "job-gamma")
    monkeypatch.setenv(ENV_TRACE_ATTEMPT, "5")
    monkeypatch.setenv(ENV_TRACE_ROLE, "implement-small")

    emit(
        "job",
        "start",
        state_dir=tmp_path,
    )

    events = _read_all_events(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["parent"] == {
        "plan": "plan-alpha",
        "task": "task-beta",
        "job": "job-gamma",
        "attempt": 5,
    }


def test_emit_archive_actor_role(tmp_path: Path) -> None:
    emit(
        "archive",
        "archived",
        state_dir=tmp_path,
        actor_role="archiver",
        target="2026-09",
        status="ok",
        detail={"files": 30, "size": 1024},
    )

    events = _read_all_events(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "archive"
    assert ev["actor"] == {"role": "archiver", "host": "agent-hub"}
    assert ev["detail"]["event"] == "archived"
    assert ev["detail"]["files"] == 30
    assert ev["detail"]["size"] == 1024
    assert ev["target"] == "2026-09"
