from agenthub.plan import Task
from agenthub.planrun import apply_retry
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
