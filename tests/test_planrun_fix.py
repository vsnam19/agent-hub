from agenthub.plan import Task
from agenthub.planrun import apply_retry, escalate
from agenthub.planstate import AttemptState, TaskState


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
