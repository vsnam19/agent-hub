import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agenthub.plan import Plan, Task
from agenthub.planrun import apply_retry, escalate, review_task, run_plan
from agenthub.planstate import AttemptState, TaskState, load_or_init
from tests.test_planrun import FakeSpawner, make_plan


def test_gate_failure_retries_after_fix_round():
    task = Task(name="t", role="implement", max_attempts=2)
    tstate = TaskState(
        status="running",
        attempts=[
            AttemptState(job="j1", status="gate-failed", exit_code=1),
            AttemptState(job="j2", status="gate-failed", exit_code=1),
            AttemptState(job="j3", status="gate-failed", exit_code=1),
        ],
        attempts_base=2,
    )
    apply_retry(tstate, task, "gate-failed", {"gate": [{"cmd": "pytest", "rc": 1, "tail": "failed"}]})
    assert tstate.status == "pending"


def test_no_retry_when_attempts_since_fix_reach_max():
    task = Task(name="t", role="implement", max_attempts=2)
    tstate = TaskState(
        status="running",
        attempts=[
            AttemptState(job="j1", status="gate-failed", exit_code=1),
            AttemptState(job="j2", status="gate-failed", exit_code=1),
            AttemptState(job="j3", status="gate-failed", exit_code=1),
        ],
        attempts_base=1,
    )
    apply_retry(tstate, task, "gate-failed", {"gate": [{"cmd": "pytest", "rc": 1, "tail": "failed"}]})
    assert tstate.status == "failed"


def test_failed_job_retries_once_per_fix_round():
    task = Task(name="t", role="implement", max_attempts=2)
    tstate = TaskState(
        status="running",
        attempts=[
            AttemptState(job="j1", status="failed", exit_code=1),
            AttemptState(job="j2", status="failed", exit_code=1),
        ],
        attempts_base=1,
    )
    apply_retry(tstate, task, "failed", {})
    assert tstate.status == "pending"


def test_retry_in_fix_round_keeps_fix_brief():
    task = Task(name="t", role="implement", max_attempts=2)
    tstate = TaskState(
        status="running",
        fix_rounds=1,
        reason="Please fix the timeout handling in task execution.\nEnsure tests pass.",
        attempts=[
            AttemptState(job="j1", status="gate-failed", exit_code=1),
        ],
        attempts_base=0,
    )
    apply_retry(tstate, task, "gate-failed", {"gate": [{"cmd": "pytest", "rc": 1, "tail": "assertion error"}]})
    assert tstate.status == "pending"
    assert tstate.reason == (
        "Please fix the timeout handling in task execution.\nEnsure tests pass.\n\n"
        "## Previous attempt failed the gate\npytest\nassertion error"
    )


def test_second_retry_keeps_only_newest_note():
    task = Task(name="t", role="implement", max_attempts=3)
    tstate = TaskState(
        status="running",
        fix_rounds=1,
        reason=(
            "Please fix the timeout handling in task execution.\nEnsure tests pass.\n\n"
            "## Previous attempt failed the gate\npytest\nassertion error"
        ),
        attempts=[
            AttemptState(job="j1", status="gate-failed", exit_code=1),
            AttemptState(job="j2", status="timeout", exit_code=124),
        ],
        attempts_base=0,
    )
    apply_retry(tstate, task, "timeout", {})
    assert tstate.status == "pending"
    assert "Previous attempt failed the gate" not in tstate.reason
    assert tstate.reason == (
        "Please fix the timeout handling in task execution.\nEnsure tests pass.\n\n"
        "## Previous attempt timed out — work in smaller steps and commit as you go"
    )


def test_escalation_in_fix_round_keeps_fix_brief(monkeypatch, tmp_path):
    monkeypatch.setattr("agenthub.planrun.subprocess.run", lambda *a, **kw: None)
    task = Task(name="t", role="implement-small", max_attempts=2)
    tstate = TaskState(
        status="running",
        role="implement-small",
        worktree=str(tmp_path),
        start_ref="HEAD",
        fix_rounds=1,
        reason=(
            "Please fix the timeout handling in task execution.\nEnsure tests pass.\n\n"
            "## Previous attempt timed out — work in smaller steps and commit as you go"
        ),
    )
    escalate(tstate, task, {"gate": [{"cmd": "pytest", "rc": 1, "tail": "failed test"}]}, "gate-failed")
    assert tstate.status == "pending"
    assert tstate.role == "implement"
    assert "Previous attempt timed out" not in tstate.reason
    assert tstate.reason == (
        "Please fix the timeout handling in task execution.\nEnsure tests pass.\n\n"
        "## Previous attempt failed\npytest\nfailed test"
    )


def test_retry_outside_fix_round_unchanged():
    task = Task(name="t", role="implement", max_attempts=2)
    tstate = TaskState(
        status="running",
        fix_rounds=0,
        reason="Initial instruction before fix rounds",
        attempts=[
            AttemptState(job="j1", status="gate-failed", exit_code=1),
        ],
        attempts_base=0,
    )
    apply_retry(tstate, task, "gate-failed", {"gate": [{"cmd": "pytest", "rc": 1, "tail": "failed"}]})
    assert tstate.status == "pending"
    assert tstate.reason == "## Previous attempt failed the gate\npytest\nfailed"


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Tester"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    return path


def _commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", filename], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", message], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_escalation_in_fix_round_keeps_round0_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agenthub.planrun.load_pool",
        lambda: SimpleNamespace(roles={"implement-small": SimpleNamespace(escalate="implement")}),
    )
    repo = _init_repo(tmp_path / "repo")
    commit_a = _commit(repo, "file.txt", "A", "commit A")
    commit_b = _commit(repo, "file.txt", "B", "commit B")
    commit_c = _commit(repo, "file.txt", "C", "commit C")

    assert commit_a != commit_b != commit_c
    head_c = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_c == commit_c

    task = Task(name="t", role="implement-small", max_attempts=2)
    tstate = TaskState(
        status="running",
        role="implement-small",
        worktree=str(repo),
        start_ref=commit_a,
        fix_start=commit_b,
        fix_rounds=1,
    )

    escalate(tstate, task, {"reason": "failed"}, "failed")

    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_after == commit_b


def test_escalation_outside_fix_round_resets_to_start_ref(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agenthub.planrun.load_pool",
        lambda: SimpleNamespace(roles={"implement-small": SimpleNamespace(escalate="implement")}),
    )
    repo = _init_repo(tmp_path / "repo")
    commit_a = _commit(repo, "file.txt", "A", "commit A")
    commit_b = _commit(repo, "file.txt", "B", "commit B")

    assert commit_a != commit_b
    head_b = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_b == commit_b

    task = Task(name="t", role="implement-small", max_attempts=2)
    tstate = TaskState(
        status="running",
        role="implement-small",
        worktree=str(repo),
        start_ref=commit_a,
        fix_start="",
        fix_rounds=0,
    )

    escalate(tstate, task, {"reason": "failed"}, "failed")

    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_after == commit_a


def test_no_agent_pin_after_escalated_role(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1", "role": "implement-small", "agent": "pinned-agent"}])
    state = load_or_init(plan)
    tstate = state.task("t1")
    tstate.role = "implement"  # escalated role
    tstate.escalated_from = ""  # fix round cleared escalated_from
    state.save()

    spawn = FakeSpawner({"t1": [{"status": "ok", "id": "job1"}]})
    run_plan(plan, state, spawn)

    assert len(spawn.spawns) == 1
    argv, _ = spawn.spawns[0]
    assert "--agent" not in argv
    assert "pinned-agent" not in argv

    # When role is not escalated, --agent should still be passed
    plan2 = make_plan(tmp_path / "p2", [{"name": "t2", "role": "implement-small", "agent": "pinned-agent"}])
    state2 = load_or_init(plan2)
    spawn2 = FakeSpawner({"t2": [{"status": "ok", "id": "job2"}]})
    run_plan(plan2, state2, spawn2)

    assert len(spawn2.spawns) == 1
    argv2, _ = spawn2.spawns[0]
    assert "--agent" in argv2
    assert "pinned-agent" in argv2


def test_review_task_cancelled_gate(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    commit_a = _commit(repo, "file.txt", "A", "init")

    task = Task(name="t1", role="implement", gate=["pytest"])
    tstate = TaskState(status="done", role="implement", worktree=str(repo), start_ref=commit_a)
    plan = Plan(goal="goal", repo=repo, base="main", slug="test", tasks=[task], path=tmp_path / "plan.json")

    monkeypatch.setattr("agenthub.planrun.list_jobs", lambda *a: [])
    monkeypatch.setattr(
        "agenthub.planrun.run_gate_job",
        lambda *a, **kw: {"status": "cancelled", "id": "gate-job-1"},
    )

    res = review_task(plan, task, tstate, lambda: datetime.now(timezone.utc))
    assert res.get("cancelled") is True
    assert res.get("gate") == "failed"
    assert res.get("review") == "failed"
    assert res.get("job") == "gate-job-1"
    assert res.get("reason") == "gate cancelled"


def test_review_task_cancelled_review(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    commit_a = _commit(repo, "file.txt", "A", "init")

    task = Task(name="t1", role="implement", gate=["pytest"])
    tstate = TaskState(status="done", role="implement", worktree=str(repo), start_ref=commit_a)
    plan = Plan(goal="goal", repo=repo, base="main", slug="test", tasks=[task], path=tmp_path / "plan.json")

    monkeypatch.setattr("agenthub.planrun.list_jobs", lambda *a: [])
    monkeypatch.setattr(
        "agenthub.planrun.run_gate_job",
        lambda *a, **kw: {"status": "ok", "id": "gate-job-1"},
    )
    monkeypatch.setattr(
        "agenthub.planrun.run_job",
        lambda *a, **kw: {"status": "cancelled", "id": "rev-job-1", "error": "interrupted"},
    )

    res = review_task(plan, task, tstate, lambda: datetime.now(timezone.utc))
    assert res.get("cancelled") is True
    assert res.get("gate") == "ok"
    assert res.get("review") == "failed"
    assert res.get("job") == "rev-job-1"
    assert res.get("reason") == "interrupted"


def test_review_task_not_cancelled_has_no_cancelled_key(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    commit_a = _commit(repo, "file.txt", "A", "init")

    task = Task(name="t1", role="implement", gate=["pytest"])
    tstate = TaskState(status="done", role="implement", worktree=str(repo), start_ref=commit_a)
    plan = Plan(goal="goal", repo=repo, base="main", slug="test", tasks=[task], path=tmp_path / "plan.json")

    monkeypatch.setattr("agenthub.planrun.list_jobs", lambda *a: [])
    monkeypatch.setattr(
        "agenthub.planrun.run_gate_job",
        lambda *a, **kw: {"status": "ok", "id": "gate-job-1"},
    )
    monkeypatch.setattr(
        "agenthub.planrun.run_job",
        lambda *a, **kw: {"status": "ok", "id": "rev-job-1", "verdict": "approve", "error": ""},
    )

    res = review_task(plan, task, tstate, lambda: datetime.now(timezone.utc))
    assert "cancelled" not in res
    assert res.get("gate") == "ok"
    assert res.get("review") == "approve"
