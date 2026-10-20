from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agenthub import planreport
from agenthub.audit.emit import emit
from agenthub.config import paths
from agenthub.plan import Plan, Task
from agenthub.planrun import merge_order
from agenthub.planstate import State


@dataclass
class CloseResult:
    ok: bool
    report: Path
    stale: dict[str, list[str]] = field(default_factory=dict)
    cancelled: bool = False


def close(
    plan: Plan,
    state: State,
    *,
    run: Callable[[], int],
    review: Callable[[Task], dict],
    triage: Callable[[Task], dict | None],
    ready: Callable[[Task], list[str]],
    stop: Callable[[], bool] = lambda: False,
    max_rounds: int = 3,
    cascade: Callable[[Plan, State, str], dict[str, str]] | None = None,
) -> CloseResult:
    if cascade is None:
        from agenthub.cascade import cascade as default_cascade

        cascade = default_cascade

    reviews: dict[str, dict] = {}
    approved: set[str] = set()
    rejected_once: set[str] = set()
    fixed_tasks: set[str] = set()
    pending_fixed: set[str] = set()
    cascaded: dict[str, set[str]] = {}

    implement_tasks = [t for t in plan.tasks if (t.kind or t.role) == "implement"]

    cancelled = False
    while True:
        rc = run()
        if rc == 130:
            cancelled = True
            break

        if pending_fixed:
            ordered_fixed = [t.name for t in merge_order(plan) if t.name in pending_fixed]
            for fixed_name in ordered_fixed:
                tstate = state.task(fixed_name)
                if tstate.status == "done":
                    cascade_results = cascade(plan, state, fixed_name)
                    for dep_name, outcome in cascade_results.items():
                        if outcome == "merged":
                            approved.discard(dep_name)
                            cascaded.setdefault(dep_name, set()).add(fixed_name)
                        elif outcome.startswith("conflict:"):
                            files = outcome.split("conflict:", 1)[1].strip()
                            dep_state = state.task(dep_name)
                            dep_state.status = "blocked"
                            dep_state.reason = f"cascade conflict from {fixed_name}: {files}"
                            approved.discard(dep_name)
                            state.save()
                        elif outcome.startswith("error:"):
                            err = outcome.split("error:", 1)[1].strip()
                            dep_state = state.task(dep_name)
                            dep_state.status = "blocked"
                            dep_state.reason = f"cascade failed from {fixed_name}: {err}"
                            approved.discard(dep_name)
                            state.save()
            pending_fixed.clear()

        made_pending = False
        needs_rereview = False
        for task in implement_tasks:
            tstate = state.task(task.name)
            if tstate.status != "done" or task.name in approved:
                continue

            if stop():
                cancelled = True
                break

            r = review(task)
            reviews[task.name] = r

            if r.get("cancelled"):
                cancelled = True
                tstate.status = "done"
                tstate.reason = ""
                state.save()
                break

            if r.get("review") == "approve" and r.get("gate") == "ok":
                problems = ready(task)
                if problems:
                    tstate.status = "blocked"
                    tstate.reason = "not ready: " + "; ".join(problems)
                    state.save()
                else:
                    approved.add(task.name)
                    rejected_once.discard(task.name)
            elif r.get("review") == "request-changes" or (r.get("gate") == "failed" and r.get("job") != "-"):
                if tstate.fix_rounds >= max_rounds:
                    tstate.status = "blocked"
                    tstate.reason = "fix rounds exhausted"
                    state.save()
                else:
                    tr = triage(task)
                    if tr is None:
                        tstate.status = "blocked"
                        tstate.reason = "triage found nothing to act on"
                        state.save()
                    elif tr.get("status") == "blocked" and tr.get("reason") == "triage job cancelled":
                        cancelled = True
                        tstate.status = "done"
                        tstate.reason = ""
                        state.save()
                        break
                    elif tr.get("status") == "rejected-only":
                        if task.name in rejected_once:
                            tstate.status = "blocked"
                            tstate.reason = "triage rejected every finding twice; director decides"
                            state.save()
                        else:
                            rejected_once.add(task.name)
                            needs_rereview = True
                            state.save()
                    else:
                        state.save()
                        if tstate.status == "pending":
                            made_pending = True
                            rejected_once.discard(task.name)
                            fixed_tasks.add(task.name)
                            pending_fixed.add(task.name)
                            for dep_name in cascaded:
                                cascaded[dep_name].discard(task.name)
            else:
                tstate.status = "blocked"
                reason = r.get("reason") or ""
                tstate.reason = f"review failed: {reason}"
                state.save()

        if cancelled:
            break
        if not (made_pending or needs_rereview):
            break

    task_map = {t.name: t for t in plan.tasks}

    def get_transitive_deps(name: str) -> set[str]:
        deps: set[str] = set()
        stack = list(task_map[name].depends_on) if name in task_map else []
        while stack:
            dep = stack.pop()
            if dep not in deps:
                deps.add(dep)
                if dep in task_map:
                    stack.extend(task_map[dep].depends_on)
        return deps

    stale: dict[str, list[str]] = {}
    for task in implement_tasks:
        if task.name in approved:
            upstream_fixed = get_transitive_deps(task.name) & fixed_tasks
            uncascaded = sorted(upstream_fixed - cascaded.get(task.name, set()))
            if uncascaded:
                stale[task.name] = uncascaded

    report_path = planreport.write_report(plan, state, reviews, max_rounds)
    if stale:
        stale_lines = [
            (
                f"- {task_name}: built before the fix rounds of {', '.join(upstream)} "
                "(merge carries both; re-review after merge)"
            )
            for task_name, upstream in stale.items()
        ]
        with open(report_path, "a", encoding="utf-8") as f:
            f.write("\n## Built before an upstream fix\n" + "\n".join(stale_lines) + "\n")

    cascade_blocked = any(
        state.task(t.name).status == "blocked"
        and state.task(t.name).reason.startswith(("cascade conflict from ", "cascade failed from "))
        for t in plan.tasks
    )
    ok = not cancelled and not stale and not cascade_blocked and all(t.name in approved for t in implement_tasks)
    plan_name = plan.slug
    try:
        emit(
            "plan",
            "close",
            state_dir=paths().state_dir,
            parent={"plan": plan_name},
            status="ok" if ok else "error",
            detail={
                "ok": ok,
                "cancelled": cancelled,
                "stale": stale,
                "approved": len(approved),
                "total": len(implement_tasks),
            },
        )
    except Exception:
        pass
    return CloseResult(ok=ok, report=report_path, stale=stale, cancelled=cancelled)
