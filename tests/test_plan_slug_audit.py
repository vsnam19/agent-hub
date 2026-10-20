from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agenthub.audit.writer import audit_root
from agenthub.config import paths
from agenthub.plan import Plan, Task
from agenthub.planclose import close
from agenthub.planstate import TaskState, load_or_init
from agenthub.triagerun import triage_task

NOW = lambda: datetime(2026, 10, 20, tzinfo=timezone.utc)  # noqa: E731


def _read_audit_events() -> list[dict[str, Any]]:
    root = audit_root(paths().state_dir)
    events: list[dict[str, Any]] = []
    for p in sorted(root.glob("*/*.jsonl")):
        if p.name.endswith(".lock"):
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def test_close_event_plan_is_slug(tmp_path: Path) -> None:
    plan_dir = tmp_path / "x"
    plan_dir.mkdir(parents=True)
    plan_file = plan_dir / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    plan = Plan(
        goal="Test plan close slug",
        repo=tmp_path,
        base="main",
        slug="s",
        tasks=[Task(name="task-1", role="implement")],
        path=plan_file,
    )
    state = load_or_init(plan)

    def fake_run() -> int:
        state.task("task-1").status = "done"
        return 0

    res = close(
        plan,
        state,
        run=fake_run,
        review=lambda task: {"gate": "ok", "review": "approve", "job": "job-rev-1", "reason": ""},
        triage=lambda task: None,
        ready=lambda task: [],
        max_rounds=3,
    )
    assert res.ok is True

    events = [
        e for e in _read_audit_events() if e.get("kind") == "plan" and e.get("detail", {}).get("event") == "close"
    ]
    assert len(events) == 1
    assert events[0]["parent"] == {"plan": "s"}


def test_triage_event_plan_is_slug(tmp_path: Path) -> None:
    plan_dir = tmp_path / "x"
    plan_dir.mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    brief = plan_dir / "t.md"
    brief.write_text("task brief", encoding="utf-8")
    task = Task(name="task-t", role="implement", brief=brief, gate=["just lint"])
    plan = Plan(goal="g", repo=tmp_path, base="main", slug="s", tasks=[task], path=plan_dir / "plan.json")
    tstate = TaskState(status="done", worktree=str(wt))
    review_reply = tmp_path / "review" / "reply.md"
    review_reply.parent.mkdir(parents=True)
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

    def fake_run(args: Any, p: Any, pool: Any, now: Any) -> dict[str, Any]:
        return {"status": "failed"}

    res = triage_task(plan, task, tstate, jobs, "H", NOW, run=fake_run)
    assert res is not None
    assert res["status"] == "blocked"

    events = [
        e for e in _read_audit_events() if e.get("kind") == "plan" and e.get("detail", {}).get("event") == "triage"
    ]
    assert len(events) == 1
    assert events[0]["parent"] == {"plan": "s", "task": "task-t"}
