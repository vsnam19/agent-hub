from datetime import datetime
from pathlib import Path
from typing import Callable

from agenthub import triage, triageapply, triageinputs
from agenthub.audit.emit import emit
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


def _rejected_sort_key(task_name: str, path: Path) -> tuple[int, int, str]:
    prefix = f"{task_name}-"
    suffix = "-rejected.md"
    name = path.name
    if name.startswith(prefix) and name.endswith(suffix):
        raw = name[len(prefix) : len(name) - len(suffix)]
        try:
            return (0, int(raw), name)
        except ValueError:
            pass
    return (1, 0, name)


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

    def _finish(res: dict) -> dict:
        plan_name = plan.slug
        decision = res.get("status", "")
        reason = res.get("reason", "")
        try:
            emit(
                "plan",
                "triage",
                state_dir=paths().state_dir,
                parent={"plan": plan_name, "task": task.name},
                target=decision,
                quote=reason if reason else None,
                detail={"counts": res.get("counts", {})},
            )
        except Exception:
            pass
        return res

    worktree = Path(tstate.worktree)
    inputs = triageinputs.collect(jobs, worktree, head)
    if inputs is None:
        return None
    if inputs.review_job and not inputs.findings:
        return _finish(_blocked(task, tstate, "review requested changes but no finding could be parsed"))

    plan_dir = plan.path.parent
    task_brief = task.brief.read_text() if task.brief and task.brief.is_file() else ""
    rejected = sorted(
        (plan_dir / "fixes").glob(f"{task.name}-*-rejected.md"),
        key=lambda p: _rejected_sort_key(task.name, p),
    )
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
        return _finish(_blocked(task, tstate, f"triage job {result.get('status')}"))
    reply_path = result.get("reply_path")
    if not reply_path:
        return _finish(_blocked(task, tstate, f"triage job {result.get('status')}: no reply"))
    try:
        parsed = triage.parse(Path(reply_path).read_text())
    except (triage.TriageError, OSError) as e:
        return _finish(_blocked(task, tstate, f"triage reply invalid: {e}"))

    if inputs.review_job == "" and not parsed.fix_brief.strip():
        return _finish(_blocked(task, tstate, "gate failed and the triage wrote no fix brief"))

    resolution = triage.resolve(
        parsed,
        inputs.findings,
        worktree,
        inputs.job_ids,
        unparsed=triage.unparsed_findings(inputs.review_reply),
    )
    summary = triageapply.apply(plan_dir, task.name, tstate, resolution, inputs.review_job)
    return _finish({**summary, "task": task.name})
