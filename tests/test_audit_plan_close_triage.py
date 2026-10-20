from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agenthub.audit.writer import audit_root
from agenthub.config import paths
from agenthub.plan import Plan, Task
from agenthub.planclose import CloseResult, close
from agenthub.planstate import TaskState, load_or_init
from agenthub.triagerun import triage_task

NOW = lambda: datetime(2026, 10, 14, tzinfo=timezone.utc)  # noqa: E731


def _read_audit_events(state_dir: Path | None = None) -> list[dict[str, Any]]:
    if state_dir is None:
        state_dir = paths().state_dir
    root = audit_root(state_dir)
    events: list[dict[str, Any]] = []
    for p in sorted(root.glob("*/*.jsonl")):
        if p.name.endswith(".lock"):
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _make_plan(tmp_path: Path, tasks: list[Task] | None = None) -> tuple[Plan, Any]:
    plan_dir = tmp_path / "plan-alpha"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    if tasks is None:
        tasks = [Task(name="task-1", role="implement")]
    plan = Plan(
        goal="Test plan close loop",
        repo=tmp_path,
        base="main",
        slug="plan-alpha",
        tasks=tasks,
        path=plan_file,
    )
    state = load_or_init(plan)
    return plan, state


def _setup_triage(tmp_path: Path) -> tuple[Plan, Task, TaskState, list[dict[str, Any]]]:
    plan_dir = tmp_path / "plan-beta"
    plan_dir.mkdir(parents=True, exist_ok=True)
    wt = tmp_path / "wt"
    wt.mkdir(parents=True, exist_ok=True)
    brief = plan_dir / "t.md"
    brief.write_text("task brief", encoding="utf-8")
    task = Task(name="task-t", role="implement", brief=brief, gate=["just lint"])
    plan = Plan(goal="g", repo=tmp_path, base="main", slug="plan-beta", tasks=[task], path=plan_dir / "plan.json")
    tstate = TaskState(status="done", worktree=str(wt))
    review_reply = tmp_path / "review" / "x" / "y" / "reply.md"
    review_reply.parent.mkdir(parents=True, exist_ok=True)
    review_reply.write_text(
        "## Findings\n- high — agenthub/a.py:1 — broken thing\n\n## Verdict\nrequest-changes\n",
        encoding="utf-8",
    )
    jobs: list[dict[str, Any]] = [
        {
            "id": "jr",
            "kind": "review",
            "status": "ok",
            "verdict": "request-changes",
            "head": "H",
            "workdir": str(wt),
            "reply_path": str(review_reply),
        },
        {"id": "ji", "kind": "implement", "status": "ok", "agent": "claude", "model": "haiku", "workdir": str(wt)},
    ]
    return plan, task, tstate, jobs


def _fake_run(tmp_path: Path, reply: str | None = None, status: str = "ok") -> Any:
    def run(args: Any, p: Any, pool: Any, now: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"status": status}
        if reply is not None:
            f = tmp_path / "reply.md"
            f.write_text(reply, encoding="utf-8")
            out["reply_path"] = str(f)
        return out

    return run


def _reply(
    decision: str = "fix",
    evidence: str = "e",
    fix_brief: str = "",
    findings: list[dict[str, Any]] | None = None,
) -> str:
    if findings is None:
        findings = [{"id": "F1", "severity": "high", "decision": decision, "evidence": evidence}]
    data = {
        "findings": findings,
        "fix_brief": fix_brief,
    }
    return "```json\n" + json.dumps(data) + "\n```\n"


def test_close_emits_plan_close_event(tmp_path: Path) -> None:
    plan, state = _make_plan(tmp_path)

    def fake_run() -> int:
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict[str, str]:
        return {"gate": "ok", "review": "approve", "job": "job-rev-1", "reason": ""}

    def fake_triage(task: Task) -> dict[str, Any] | None:
        return None

    def fake_ready(task: Task) -> list[str]:
        return []

    res = close(
        plan,
        state,
        run=fake_run,
        review=fake_review,
        triage=fake_triage,
        ready=fake_ready,
        max_rounds=3,
    )
    assert res.ok is True

    events = [
        e for e in _read_audit_events() if e.get("kind") == "plan" and e.get("detail", {}).get("event") == "close"
    ]
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "plan"
    assert ev["parent"] == {"plan": "plan-alpha"}
    assert ev["outcome"]["status"] == "ok"
    assert ev["detail"]["event"] == "close"
    assert ev["detail"]["ok"] is True

    # Also verify that a failed close outcome emits status: error
    plan2, state2 = _make_plan(tmp_path / "sub")
    res2 = close(
        plan2,
        state2,
        run=fake_run,
        review=fake_review,
        triage=fake_triage,
        ready=lambda task: ["work tree dirty"],
        max_rounds=3,
    )
    assert res2.ok is False
    events2 = [
        e for e in _read_audit_events() if e.get("kind") == "plan" and e.get("detail", {}).get("event") == "close"
    ]
    assert len(events2) == 2
    assert events2[1]["outcome"]["status"] == "error"
    assert events2[1]["detail"]["ok"] is False


def test_close_event_failure_does_not_change_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan, state = _make_plan(tmp_path)

    def fake_run() -> int:
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict[str, str]:
        return {"gate": "ok", "review": "approve", "job": "job-rev-1", "reason": ""}

    def _exploding_write(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("simulated writer failure in audit")

    monkeypatch.setattr("agenthub.audit.emit.write", _exploding_write)

    res = close(
        plan,
        state,
        run=fake_run,
        review=fake_review,
        triage=lambda t: None,
        ready=lambda t: [],
        max_rounds=3,
    )
    assert isinstance(res, CloseResult)
    assert res.ok is True
    assert plan.path is not None
    assert res.report == plan.path.parent / "report.md"


def test_triage_emits_plan_triage_event_with_task_parent(tmp_path: Path) -> None:
    plan, task, tstate, jobs = _setup_triage(tmp_path)
    fix_brief = "\n\n".join(
        [
            "## Goal\nfix it",
            "## Input\nx",
            "## Expected output\n1. `a.py`",
            "## Constraints\nnone",
            "## Gate\n`just lint`",
        ]
    )
    res = triage_task(
        plan,
        task,
        tstate,
        jobs,
        "H",
        NOW,
        run=_fake_run(tmp_path, _reply("fix", fix_brief=fix_brief)),
    )
    assert res is not None
    assert res["status"] == "fix"

    events = [
        e for e in _read_audit_events() if e.get("kind") == "plan" and e.get("detail", {}).get("event") == "triage"
    ]
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "plan"
    assert ev["parent"] == {"plan": "plan-beta", "task": "task-t"}
    assert ev["target"] == "fix"
    assert ev["outcome"]["status"] == "ok"
    assert ev["detail"]["event"] == "triage"


def test_triage_event_quote_is_reason(tmp_path: Path) -> None:
    plan, task, tstate, jobs = _setup_triage(tmp_path)
    res = triage_task(
        plan,
        task,
        tstate,
        jobs,
        "H",
        NOW,
        run=_fake_run(tmp_path, status="failed"),
    )
    assert res is not None
    assert res["status"] == "blocked"
    assert res["reason"] == "triage job failed"

    events = [
        e for e in _read_audit_events() if e.get("kind") == "plan" and e.get("detail", {}).get("event") == "triage"
    ]
    assert len(events) == 1
    ev = events[0]
    assert ev["target"] == "blocked"
    assert ev["quote"] == "triage job failed"
