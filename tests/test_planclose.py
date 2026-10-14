from pathlib import Path

from agenthub.plan import Plan, Task
from agenthub.planclose import CloseResult, close
from agenthub.planstate import load_or_init


def _make_plan(tmp_path: Path, tasks: list[Task] | None = None):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    if tasks is None:
        tasks = [Task(name="task-1", role="implement")]
    plan = Plan(
        goal="Test plan close loop",
        repo=tmp_path,
        base="main",
        slug="test-close",
        tasks=tasks,
        path=plan_file,
    )
    state = load_or_init(plan)
    return plan, state


def test_all_approved_on_first_pass(tmp_path: Path):
    plan, state = _make_plan(tmp_path)

    run_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        return {"gate": "ok", "review": "approve", "job": "job-rev-1", "reason": ""}

    def fake_triage(task: Task) -> dict | None:
        raise AssertionError("triage should not be called when task is approved")

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

    assert isinstance(res, CloseResult)
    assert res.ok is True
    assert res.report == tmp_path / "report.md"
    assert res.report.is_file()
    assert run_calls == 1
    assert state.task("task-1").status == "done"


def test_request_changes_triages_then_approves(tmp_path: Path):
    plan, state = _make_plan(tmp_path)

    run_calls = 0
    review_calls = 0
    triage_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        nonlocal review_calls
        review_calls += 1
        if review_calls == 1:
            return {"gate": "ok", "review": "request-changes", "job": "job-rev-1", "reason": "fix needed"}
        return {"gate": "ok", "review": "approve", "job": "job-rev-2", "reason": ""}

    def fake_triage(task: Task) -> dict | None:
        nonlocal triage_calls
        triage_calls += 1
        tstate = state.task(task.name)
        tstate.status = "pending"
        tstate.fix_rounds += 1
        return {"status": "fix", "fix_path": "/tmp/fix.md"}

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

    assert isinstance(res, CloseResult)
    assert res.ok is True
    assert res.report == tmp_path / "report.md"
    assert run_calls == 2
    assert review_calls == 2
    assert triage_calls == 1
    assert state.task("task-1").status == "done"
    assert state.task("task-1").fix_rounds == 1

    report_content = res.report.read_text(encoding="utf-8")
    assert "- task-1: done, fix rounds 1/3, review approve, review job job-rev-2" in report_content
    assert "## Needs the director\n(nothing)" in report_content


def test_blocked_triage_stops_the_task(tmp_path: Path):
    plan, state = _make_plan(tmp_path)

    run_calls = 0
    triage_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        return {"gate": "ok", "review": "request-changes", "job": "job-rev-1", "reason": "fix needed"}

    def fake_triage(task: Task) -> dict | None:
        nonlocal triage_calls
        triage_calls += 1
        tstate = state.task(task.name)
        tstate.status = "blocked"
        tstate.reason = "spec conflict"
        return {"status": "blocked", "reason": "spec conflict"}

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

    assert isinstance(res, CloseResult)
    assert res.ok is False
    assert run_calls == 1
    assert triage_calls == 1
    assert state.task("task-1").status == "blocked"
    assert state.task("task-1").reason == "spec conflict"

    report_content = res.report.read_text(encoding="utf-8")
    assert "- task-1: blocked, fix rounds 0/3, review request-changes, review job job-rev-1" in report_content
    assert "- task-1: spec conflict" in report_content


def test_triage_none_blocks_task(tmp_path: Path):
    plan, state = _make_plan(tmp_path)

    def fake_run() -> int:
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        return {"gate": "failed", "review": "failed", "job": "job-gate-1", "reason": "gate failed"}

    def fake_triage(task: Task) -> dict | None:
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

    assert res.ok is False
    assert state.task("task-1").status == "blocked"
    assert state.task("task-1").reason == "triage found nothing to act on"


def test_rounds_exhausted_blocks(tmp_path: Path):
    plan, state = _make_plan(tmp_path)
    state.task("task-1").fix_rounds = 2
    state.save()

    run_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        return {"gate": "ok", "review": "request-changes", "job": "job-rev-1", "reason": "still broken"}

    def fake_triage(task: Task) -> dict | None:
        raise AssertionError("triage must not be called when rounds are exhausted")

    def fake_ready(task: Task) -> list[str]:
        return []

    res = close(
        plan,
        state,
        run=fake_run,
        review=fake_review,
        triage=fake_triage,
        ready=fake_ready,
        max_rounds=2,
    )

    assert isinstance(res, CloseResult)
    assert res.ok is False
    assert run_calls == 1
    assert state.task("task-1").status == "blocked"
    assert state.task("task-1").reason == "fix rounds exhausted"

    report_content = res.report.read_text(encoding="utf-8")
    assert "- task-1: blocked, fix rounds 2/2, review request-changes, review job job-rev-1" in report_content
    assert "- task-1: fix rounds exhausted" in report_content


def test_review_failed_blocks(tmp_path: Path):
    plan, state = _make_plan(tmp_path)

    run_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        return {"gate": "failed", "review": "failed", "job": "-", "reason": "no work tree recorded"}

    def fake_triage(task: Task) -> dict | None:
        raise AssertionError("triage must not be called when review fails with job -")

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

    assert isinstance(res, CloseResult)
    assert res.ok is False
    assert run_calls == 1
    assert state.task("task-1").status == "blocked"
    assert state.task("task-1").reason == "review failed: no work tree recorded"

    report_content = res.report.read_text(encoding="utf-8")
    assert "- task-1: blocked, fix rounds 0/3, review failed, review job -" in report_content
    assert "- task-1: review failed: no work tree recorded" in report_content


def test_not_ready_blocks(tmp_path: Path):
    plan, state = _make_plan(tmp_path)

    run_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        return {"gate": "ok", "review": "approve", "job": "job-rev-1", "reason": ""}

    def fake_triage(task: Task) -> dict | None:
        raise AssertionError("triage must not be called when review is approve")

    def fake_ready(task: Task) -> list[str]:
        return ["work tree dirty", "HEAD uncommitted"]

    res = close(
        plan,
        state,
        run=fake_run,
        review=fake_review,
        triage=fake_triage,
        ready=fake_ready,
        max_rounds=3,
    )

    assert isinstance(res, CloseResult)
    assert res.ok is False
    assert run_calls == 1
    assert state.task("task-1").status == "blocked"
    assert state.task("task-1").reason == "not ready: work tree dirty; HEAD uncommitted"

    report_content = res.report.read_text(encoding="utf-8")
    assert "- task-1: blocked, fix rounds 0/3, review approve, review job job-rev-1" in report_content
    assert "- task-1: not ready: work tree dirty; HEAD uncommitted" in report_content


def test_multiple_tasks_with_non_implement_task(tmp_path: Path):
    tasks = [
        Task(name="task-1", role="implement"),
        Task(name="task-2", role="implement"),
        Task(name="task-rev", role="review", kind="review"),
    ]
    plan, state = _make_plan(tmp_path, tasks=tasks)

    run_calls = 0
    reviewed_tasks = []

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        if run_calls == 1:
            state.task("task-1").status = "done"
            state.task("task-2").status = "done"
        else:
            state.task("task-2").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        reviewed_tasks.append(task.name)
        if task.name == "task-1":
            return {"gate": "ok", "review": "approve", "job": "job-1", "reason": ""}
        if task.name == "task-2":
            if run_calls == 1:
                return {"gate": "ok", "review": "request-changes", "job": "job-2a", "reason": "need change"}
            return {"gate": "ok", "review": "approve", "job": "job-2b", "reason": ""}
        raise AssertionError(f"Unexpected review for {task.name}")

    def fake_triage(task: Task) -> dict | None:
        assert task.name == "task-2"
        state.task("task-2").status = "pending"
        state.task("task-2").fix_rounds += 1
        return {"status": "fix"}

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
    assert run_calls == 2
    # In pass 1, task-1 and task-2 are reviewed. In pass 2, only task-2 is reviewed (task-1 is already approved).
    assert reviewed_tasks == ["task-1", "task-2", "task-2"]
    assert state.task("task-1").status == "done"
    assert state.task("task-2").status == "done"
    assert state.task("task-2").fix_rounds == 1


def test_cancelled_run_stops_close(tmp_path: Path):
    plan, state = _make_plan(tmp_path)
    state.task("task-1").status = "done"

    run_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        return 130

    def fake_review(task: Task) -> dict:
        raise AssertionError("review must not be called when run is cancelled")

    def fake_triage(task: Task) -> dict | None:
        raise AssertionError("triage must not be called when run is cancelled")

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

    assert isinstance(res, CloseResult)
    assert res.ok is False
    assert run_calls == 1
    assert res.report == tmp_path / "report.md"
    assert res.report.is_file()


def test_rejected_only_gets_one_rereview(tmp_path: Path):
    plan, state = _make_plan(tmp_path)

    run_calls = 0
    review_calls = 0
    triage_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        nonlocal review_calls
        review_calls += 1
        if review_calls == 1:
            return {"gate": "ok", "review": "request-changes", "job": "job-rev-1", "reason": "unnecessary change"}
        return {"gate": "ok", "review": "approve", "job": "job-rev-2", "reason": ""}

    def fake_triage(task: Task) -> dict | None:
        nonlocal triage_calls
        triage_calls += 1
        # rejected-only keeps task status done
        return {
            "status": "rejected-only",
            "counts": {"total": 1, "rejected": 1, "fixed": 0},
            "rejected_path": "/tmp/rej.md",
        }

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

    assert isinstance(res, CloseResult)
    assert res.ok is True
    assert run_calls == 2
    assert review_calls == 2
    assert triage_calls == 1
    assert state.task("task-1").status == "done"
    assert res.report.is_file()


def test_rejected_only_twice_blocks(tmp_path: Path):
    plan, state = _make_plan(tmp_path)

    run_calls = 0
    review_calls = 0
    triage_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        state.task("task-1").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        nonlocal review_calls
        review_calls += 1
        return {
            "gate": "ok",
            "review": "request-changes",
            "job": f"job-rev-{review_calls}",
            "reason": "unnecessary change",
        }

    def fake_triage(task: Task) -> dict | None:
        nonlocal triage_calls
        triage_calls += 1
        return {
            "status": "rejected-only",
            "counts": {"total": 1, "rejected": 1, "fixed": 0},
            "rejected_path": f"/tmp/rej-{triage_calls}.md",
        }

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

    assert isinstance(res, CloseResult)
    assert res.ok is False
    assert run_calls == 2
    assert review_calls == 2
    assert triage_calls == 2
    assert state.task("task-1").status == "blocked"
    assert state.task("task-1").reason == "triage rejected every finding twice; director decides"
    assert res.report.is_file()

    report_content = res.report.read_text(encoding="utf-8")
    assert "- task-1: triage rejected every finding twice; director decides" in report_content
