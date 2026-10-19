from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import agenthub.planrun
from agenthub.audit.writer import audit_root
from agenthub.plan import Plan, Task
from agenthub.planrun import TRAIL_HEADER, _trail, run_plan
from agenthub.planstate import load_or_init
from tests.gitrepo import make_repo

agenthub.planrun.PLAN_POLL_S = 0.001


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


class FakePopen:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class FakeSpawner:
    def __init__(self, results: dict[str, list[dict]] | None = None) -> None:
        self.results = results or {}
        self.spawns: list[tuple[list[str], Path]] = []

    def __call__(self, argv: list[str], log_path: Path) -> FakePopen:
        name = log_path.stem.rsplit("-", 1)[0]
        self.spawns.append((argv, log_path))
        res_list = self.results.get(name, [])
        res = res_list.pop(0) if res_list else {"status": "ok", "exit_code": 0, "id": f"job_{name}"}
        if res is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps(res))
            return FakePopen(res.get("exit_code", 0))
        return FakePopen(0)


def _make_plan(
    tmp_path: Path,
    tasks: list[dict],
    plan_name: str = "test-plan",
) -> Plan:
    repo_dir = tmp_path / "repo"
    if not repo_dir.exists():
        make_repo(repo_dir)
    plan_dir = tmp_path / plan_name
    plan_dir.mkdir(parents=True, exist_ok=True)

    plan_tasks = []
    for t in tasks:
        brief_path = plan_dir / f"{t['name']}.md"
        brief_path.write_text(f"brief for {t['name']}")
        pt = Task(
            name=t["name"],
            role=t.get("role", "implement"),
            kind=t.get("kind"),
            agent=t.get("agent"),
            brief=brief_path if t.get("kind", t.get("role", "implement")) == "implement" else None,
            verifies=t.get("verifies"),
            depends_on=t.get("depends_on", []),
            paths_allowed=t.get("paths_allowed", []),
            gate=t.get("gate", []),
            skills=t.get("skills", []),
            max_attempts=t.get("max_attempts", 2),
        )
        plan_tasks.append(pt)

    plan_file = plan_dir / "plan.json"
    plan = Plan(
        goal="test plan goal",
        repo=repo_dir,
        base="master",
        slug="testslug",
        tasks=plan_tasks,
        max_parallel=2,
        path=plan_file,
    )
    return plan


def test_trail_row_becomes_plan_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    plan = _make_plan(tmp_path, [{"name": "task-alpha"}], plan_name="alpha-plan")

    _trail(
        plan,
        task="task-alpha",
        phase="start",
        decision="task started",
        reason="starting task",
        evidence="/path/to/evidence",
        result="running",
    )

    events = _read_all_events(state_dir)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "plan"
    assert ev["actor"] == {"role": "agent-hub", "host": "agent-hub"}
    assert ev["parent"] == {"plan": "alpha-plan", "task": "task-alpha"}
    assert ev["target"] == "task started"
    assert ev["outcome"] == {"status": "running"}
    assert ev["quote"] == "starting task"
    assert ev["evidence"] == "/path/to/evidence"
    assert ev["detail"] == {"event": "start", "phase": "start"}

    # Test with empty task: task parent key should be skipped, result defaults to 'ok'
    _trail(
        plan,
        task="",
        phase="final",
        decision="plan done",
        reason="",
        evidence="",
        result="",
    )

    events2 = _read_all_events(state_dir)
    assert len(events2) == 2
    ev2 = events2[1]
    assert ev2["kind"] == "plan"
    assert ev2["parent"] == {"plan": "alpha-plan"}
    assert "task" not in ev2["parent"]
    assert ev2["target"] == "plan done"
    assert ev2["outcome"] == {"status": "ok"}
    assert ev2["detail"] == {"event": "final", "phase": "final"}


def test_tsv_content_unchanged_by_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    plan = _make_plan(tmp_path, [{"name": "task-beta"}], plan_name="beta-plan")
    trail_file = plan.path.parent / "decisions.tsv"

    _trail(
        plan,
        task="task-beta",
        phase="start",
        decision="task started",
        reason="role implement",
        evidence="master",
        result="running",
    )
    _trail(
        plan,
        task="task-beta",
        phase="attempt",
        decision="attempt 1 ended",
        reason="reason\nwith\nnewlines",
        evidence="evidence\twith\ttab",
        result="failed",
    )

    assert trail_file.is_file()
    lines = trail_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert lines[0] == TRAIL_HEADER
    assert lines[0] == "time\ttask\tphase\tdecision\treason\tevidence\tresult"

    row1_cells = lines[1].split("\t")
    assert len(row1_cells) == 7
    # time column is ISO timestamp
    assert row1_cells[1] == "task-beta"
    assert row1_cells[2] == "start"
    assert row1_cells[3] == "task started"
    assert row1_cells[4] == "role implement"
    assert row1_cells[5] == "master"
    assert row1_cells[6] == "running"

    row2_cells = lines[2].split("\t")
    assert len(row2_cells) == 7
    assert row2_cells[1] == "task-beta"
    assert row2_cells[2] == "attempt"
    assert row2_cells[3] == "attempt 1 ended"
    # Newlines and tabs replaced with single spaces
    assert row2_cells[4] == "reason with newlines"
    assert row2_cells[5] == "evidence with tab"
    assert row2_cells[6] == "failed"


def test_mirror_failure_does_not_break_trail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    def _exploding_emit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated audit emit failure")

    monkeypatch.setattr("agenthub.planrun.emit", _exploding_emit)

    plan = _make_plan(tmp_path, [{"name": "task-gamma"}], plan_name="gamma-plan")
    trail_file = plan.path.parent / "decisions.tsv"

    # Should not raise any exception even though emit fails
    _trail(
        plan,
        task="task-gamma",
        phase="start",
        decision="task started",
        reason="testing failure tolerance",
    )

    assert trail_file.is_file()
    lines = trail_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == TRAIL_HEADER
    row_cells = lines[1].split("\t")
    assert row_cells[1] == "task-gamma"
    assert row_cells[2] == "start"
    assert row_cells[3] == "task started"


def test_commit_phase_is_autocommit_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    plan = _make_plan(tmp_path, [{"name": "task-delta"}], plan_name="delta-plan")
    trail_file = plan.path.parent / "decisions.tsv"

    _trail(
        plan,
        task="task-delta",
        phase="commit",
        decision="autocommit",
        reason="agent left work uncommitted",
        evidence="a1b2c3d4",
        result="",
    )

    events = _read_all_events(state_dir)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "plan"
    assert ev["parent"] == {"plan": "delta-plan", "task": "task-delta"}
    # Event name is autocommit instead of commit
    assert ev["detail"]["event"] == "autocommit"
    # Phase in detail preserves the original phase name
    assert ev["detail"]["phase"] == "commit"
    assert ev["target"] == "autocommit"
    assert ev["quote"] == "agent left work uncommitted"
    assert ev["evidence"] == "a1b2c3d4"
    assert ev["outcome"] == {"status": "ok"}

    # But decisions.tsv row still retains phase 'commit'
    lines = trail_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row_cells = lines[1].split("\t")
    assert row_cells[2] == "commit"


def test_run_start_and_finish_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir))

    # 1. Success case: returncode 0
    plan_success = _make_plan(tmp_path, [{"name": "task-one"}], plan_name="plan-success")
    state_success = load_or_init(plan_success)
    spawner_success = FakeSpawner({"task-one": [{"status": "ok", "exit_code": 0, "id": "job-1"}]})

    rc_success = run_plan(plan_success, state_success, spawner_success)
    assert rc_success == 0

    events = _read_all_events(state_dir)
    assert len(events) >= 2
    start_ev = events[0]
    assert start_ev["kind"] == "plan"
    assert start_ev["detail"]["event"] == "run-start"
    assert start_ev["parent"] == {"plan": "plan-success"}
    assert start_ev["outcome"] == {"status": "ok"}

    finish_ev = events[-1]
    assert finish_ev["kind"] == "plan"
    assert finish_ev["detail"]["event"] == "run-finish"
    assert finish_ev["parent"] == {"plan": "plan-success"}
    assert finish_ev["outcome"] == {"status": "ok"}
    assert finish_ev["detail"]["return_code"] == 0

    # 2. Failure case: returncode 1
    state_dir_fail = tmp_path / "state-fail"
    monkeypatch.setenv("AGENT_HUB_STATE", str(state_dir_fail))

    plan_fail = _make_plan(tmp_path, [{"name": "task-two", "max_attempts": 1}], plan_name="plan-fail")
    state_fail = load_or_init(plan_fail)
    spawner_fail = FakeSpawner(
        {
            "task-two": [
                {"status": "failed", "exit_code": 1, "id": "job-2-1"},
                {"status": "failed", "exit_code": 1, "id": "job-2-2"},
                {"status": "failed", "exit_code": 1, "id": "job-2-3"},
            ]
        }
    )

    rc_fail = run_plan(plan_fail, state_fail, spawner_fail)
    assert rc_fail == 1

    events_fail = _read_all_events(state_dir_fail)
    assert len(events_fail) >= 2
    fail_start = events_fail[0]
    assert fail_start["detail"]["event"] == "run-start"
    assert fail_start["parent"] == {"plan": "plan-fail"}
    assert fail_start["outcome"] == {"status": "ok"}

    fail_finish = events_fail[-1]
    assert fail_finish["detail"]["event"] == "run-finish"
    assert fail_finish["parent"] == {"plan": "plan-fail"}
    assert fail_finish["outcome"] == {"status": "error"}
    assert fail_finish["detail"]["return_code"] == 1
