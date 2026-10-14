import json
from pathlib import Path

from agenthub.plan import Plan, Task
from agenthub.planreport import write_report
from agenthub.planstate import State, TaskState


def _make_plan(tmp_path: Path, tasks: list[Task] | None = None) -> Plan:
    plan_file = tmp_path / "plan.json"
    if tasks is None:
        tasks = [Task(name="task-a", role="implement")]
    return Plan(
        goal="Deliver the one-page plan report",
        repo=tmp_path,
        base="main",
        slug="e8-report-feature",
        tasks=tasks,
        path=plan_file,
    )


def test_report_lists_tasks_with_rounds(tmp_path: Path):
    tasks = [
        Task(name="task-1", role="implement"),
        Task(name="task-2", role="implement"),
        Task(name="task-review", role="review", kind="review"),
    ]
    plan = _make_plan(tmp_path, tasks=tasks)
    state = State(
        slug=plan.slug,
        started="2026-10-14T00:00:00+00:00",
        updated="2026-10-14T00:00:00+00:00",
        tasks={
            "task-1": TaskState(status="done", fix_rounds=1),
            "task-2": TaskState(status="pending", fix_rounds=0),
            "task-review": TaskState(status="done"),
        },
    )
    reviews = {
        "task-1": {"gate": "ok", "review": "approve", "job": "job-101", "reason": ""},
    }

    report_path = write_report(plan, state, reviews, rounds=3)

    assert report_path == tmp_path / "report.md"
    assert report_path.is_file()

    content = report_path.read_text(encoding="utf-8")
    assert "# Plan report: e8-report-feature" in content
    assert "Deliver the one-page plan report" in content
    assert "- task-1: done, fix rounds 1/3, review approve, review job job-101" in content
    assert "- task-2: pending, fix rounds 0/3, review -, review job -" in content
    assert "task-review" not in content


def test_report_lists_findings_by_decision(tmp_path: Path):
    plan = _make_plan(tmp_path)
    state = State(
        slug=plan.slug,
        started="2026-10-14T00:00:00+00:00",
        updated="2026-10-14T00:00:00+00:00",
        tasks={"task-a": TaskState(status="done")},
    )
    reviews = {"task-a": {"review": "approve", "job": "job-1"}}

    triage_file = tmp_path / "triage.jsonl"
    entry1 = {
        "task": "task-a",
        "round": 1,
        "review_job": "job-rev-1",
        "findings": [
            {"id": "F1", "severity": "high", "decision": "fix", "evidence": "file.py:10"},
            {"id": "F2", "severity": "low", "decision": "reject", "evidence": "test.py:5"},
        ],
    }
    entry2 = {
        "task": "task-b",
        "round": 2,
        "review_job": "job-rev-2",
        "findings": [
            {"id": "F3", "severity": "medium", "decision": "blocked", "evidence": "spec conflict"},
        ],
    }
    triage_file.write_text(json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n", encoding="utf-8")

    report_path = write_report(plan, state, reviews, rounds=3)
    content = report_path.read_text(encoding="utf-8")

    assert "- task-a round 1 F1 (high) fix: file.py:10" in content
    assert "- task-a round 1 F2 (low) reject: test.py:5" in content
    assert "- task-b round 2 F3 (medium) blocked: spec conflict" in content


def test_report_needs_director_for_blocked_task(tmp_path: Path):
    tasks = [
        Task(name="task-a", role="implement"),
        Task(name="task-b", role="implement"),
        Task(name="task-c", role="implement"),
    ]
    plan = _make_plan(tmp_path, tasks=tasks)
    state = State(
        slug=plan.slug,
        started="2026-10-14T00:00:00+00:00",
        updated="2026-10-14T00:00:00+00:00",
        tasks={
            "task-a": TaskState(status="blocked", reason="fix rounds exhausted"),
            "task-b": TaskState(status="failed", reason="tests failed with exit code 1"),
            "task-c": TaskState(status="done"),
        },
    )
    reviews = {
        "task-a": {"review": "failed", "job": "-", "reason": "broken"},
        "task-b": {"review": "-", "job": "-", "reason": ""},
        "task-c": {"review": "request-changes", "job": "job-5", "reason": "unhandled edge case"},
    }

    report_path = write_report(plan, state, reviews, rounds=2)
    content = report_path.read_text(encoding="utf-8")

    assert "## Needs the director" in content
    assert "- task-a: fix rounds exhausted" in content
    assert "- task-b: tests failed with exit code 1" in content
    assert "- task-c: unhandled edge case" in content
    assert "(nothing)" not in content


def test_report_nothing_when_all_approved(tmp_path: Path):
    tasks = [
        Task(name="task-1", role="implement"),
        Task(name="task-2", role="implement"),
    ]
    plan = _make_plan(tmp_path, tasks=tasks)
    state = State(
        slug=plan.slug,
        started="2026-10-14T00:00:00+00:00",
        updated="2026-10-14T00:00:00+00:00",
        tasks={
            "task-1": TaskState(status="done"),
            "task-2": TaskState(status="done"),
        },
    )
    reviews = {
        "task-1": {"review": "approve", "job": "job-1"},
        "task-2": {"review": "approve", "job": "job-2"},
    }

    report_path = write_report(plan, state, reviews, rounds=3)
    content = report_path.read_text(encoding="utf-8")

    assert "## Needs the director\n(nothing)" in content


def test_report_without_triage_log(tmp_path: Path):
    plan = _make_plan(tmp_path)
    state = State(
        slug=plan.slug,
        started="2026-10-14T00:00:00+00:00",
        updated="2026-10-14T00:00:00+00:00",
        tasks={"task-a": TaskState(status="done")},
    )
    reviews = {"task-a": {"review": "approve", "job": "job-1"}}

    report_path = write_report(plan, state, reviews, rounds=3)
    content = report_path.read_text(encoding="utf-8")

    assert "## Findings\n(none)" in content


def test_report_needs_director_for_skipped_and_scope_violation(tmp_path: Path):
    tasks = [
        Task(name="task-s", role="implement"),
        Task(name="task-v", role="implement"),
    ]
    plan = _make_plan(tmp_path, tasks=tasks)
    state = State(
        slug=plan.slug,
        started="2026-10-14T00:00:00+00:00",
        updated="2026-10-14T00:00:00+00:00",
        tasks={
            "task-s": TaskState(status="skipped", reason="dependency failed"),
            "task-v": TaskState(status="scope-violation", reason="modified outside paths_allowed"),
        },
    )
    reviews = {}

    report_path = write_report(plan, state, reviews, rounds=2)
    content = report_path.read_text(encoding="utf-8")

    assert "- task-s: dependency failed" in content
    assert "- task-v: modified outside paths_allowed" in content


def test_malformed_triage_line_skipped(tmp_path: Path):
    plan = _make_plan(tmp_path)
    state = State(
        slug=plan.slug,
        started="2026-10-14T00:00:00+00:00",
        updated="2026-10-14T00:00:00+00:00",
        tasks={"task-a": TaskState(status="done")},
    )
    reviews = {"task-a": {"review": "approve", "job": "job-1"}}

    triage_file = tmp_path / "triage.jsonl"
    entry1 = {
        "task": "task-a",
        "round": 1,
        "review_job": "job-rev-1",
        "findings": [
            {"id": "F1", "severity": "high", "decision": "fix", "evidence": "file.py:10"},
        ],
    }
    triage_file.write_text(
        json.dumps(entry1) + "\n" + '{"task": "task-a", half written\n',
        encoding="utf-8",
    )

    report_path = write_report(plan, state, reviews, rounds=3)

    assert report_path == tmp_path / "report.md"
    assert report_path.is_file()

    content = report_path.read_text(encoding="utf-8")
    assert "- task-a round 1 F1 (high) fix: file.py:10" in content
    assert "- triage.jsonl: 1 unreadable line(s) skipped" in content


def test_non_object_triage_line_skipped(tmp_path: Path):
    plan = _make_plan(tmp_path)
    state = State(
        slug=plan.slug,
        started="2026-10-14T00:00:00+00:00",
        updated="2026-10-14T00:00:00+00:00",
        tasks={"task-a": TaskState(status="done")},
    )
    reviews = {"task-a": {"review": "approve", "job": "job-1"}}

    triage_file = tmp_path / "triage.jsonl"
    entry1 = {
        "task": "task-a",
        "round": 1,
        "review_job": "job-rev-1",
        "findings": [
            {"id": "F1", "severity": "high", "decision": "fix", "evidence": "file.py:10"},
        ],
    }
    triage_file.write_text(
        json.dumps(entry1) + "\n" + '"not an object"\n' + "[1, 2, 3]\n",
        encoding="utf-8",
    )

    report_path = write_report(plan, state, reviews, rounds=3)

    assert report_path == tmp_path / "report.md"
    assert report_path.is_file()

    content = report_path.read_text(encoding="utf-8")
    assert "- task-a round 1 F1 (high) fix: file.py:10" in content
    assert "- triage.jsonl: 2 unreadable line(s) skipped" in content
