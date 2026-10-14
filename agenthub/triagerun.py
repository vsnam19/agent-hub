from datetime import datetime
from pathlib import Path
from typing import Callable

from agenthub import triage, triageapply, triageinputs
from agenthub.config import load_pool, paths
from agenthub.orchestrate import RunArgs, run_job
from agenthub.plan import Plan, Task
from agenthub.planstate import TaskState
from agenthub.pool import parse_link


def _blocked(task: Task, tstate: TaskState, reason: str) -> dict:
    tstate.status = "blocked"
    tstate.reason = reason
    counts = {"fix": 0, "reject": 0, "blocked": 0}
    return {
        "task": task.name,
        "status": "blocked",
        "counts": counts,
        "fix_path": "",
        "rejected_path": "",
        "reason": reason,
    }


def triage_task(
    plan: Plan,
    task: Task,
    tstate: TaskState,
    jobs: list[dict],
    head: str,
    now: Callable[[], datetime],
    run: Callable[..., dict] = run_job,
) -> dict | None:
    """Run the techlead triage job for a task whose latest review requested changes (or whose gate failed) and apply
    its decision. Returns None when no triage is needed; otherwise the apply summary plus the task name."""
    worktree = Path(tstate.worktree)
    inputs = triageinputs.collect(jobs, worktree, head)
    if inputs is None:
        return None

    plan_dir = plan.path.parent
    task_brief = task.brief.read_text() if task.brief and task.brief.is_file() else ""
    rejected = sorted((plan_dir / "fixes").glob(f"{task.name}-*-rejected.md"))
    answered = "\n".join(p.read_text() for p in rejected)
    brief = triage.triage_brief(
        task_brief,
        inputs.findings,
        inputs.review_reply,
        inputs.gate_tail,
        inputs.diff_path,
        str(worktree),
        task.gate,
        answered,
    )

    pool = load_pool()
    args = RunArgs(
        role="techlead",
        kind="plan",
        brief=brief,
        workdir=worktree,
        links=[parse_link(x) for x in pool.roles["techlead"].chain],
        timeout=pool.timeouts["techlead"],
        base=None,
        jev=plan.jev,
        exclude=inputs.implementers,
    )
    result = run(args, paths(), pool, now)
    if result.get("status") != "ok":
        return _blocked(task, tstate, f"triage job {result.get('status')}")
    reply_path = result.get("reply_path")
    if not reply_path:
        return _blocked(task, tstate, f"triage job {result.get('status')}: no reply")
    try:
        parsed = triage.parse(Path(reply_path).read_text())
    except (triage.TriageError, OSError) as e:
        return _blocked(task, tstate, f"triage reply invalid: {e}")

    resolution = triage.resolve(parsed, inputs.findings, worktree, inputs.job_ids)
    summary = triageapply.apply(plan_dir, task.name, tstate, resolution, inputs.review_job)
    return {**summary, "task": task.name}
