from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agenthub import planreport
from agenthub.plan import Plan, Task
from agenthub.planstate import State


@dataclass
class CloseResult:
    ok: bool
    report: Path


def close(
    plan: Plan,
    state: State,
    *,
    run: Callable[[], int],
    review: Callable[[Task], dict],
    triage: Callable[[Task], dict | None],
    ready: Callable[[Task], list[str]],
    max_rounds: int = 3,
) -> CloseResult:
    reviews: dict[str, dict] = {}
    approved: set[str] = set()

    implement_tasks = [t for t in plan.tasks if (t.kind or t.role) == "implement"]

    while True:
        run()

        made_pending = False
        for task in implement_tasks:
            tstate = state.task(task.name)
            if tstate.status != "done" or task.name in approved:
                continue

            r = review(task)
            reviews[task.name] = r

            if r.get("review") == "approve" and r.get("gate") == "ok":
                problems = ready(task)
                if problems:
                    tstate.status = "blocked"
                    tstate.reason = "not ready: " + "; ".join(problems)
                    state.save()
                else:
                    approved.add(task.name)
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
                    else:
                        state.save()
                        if tstate.status == "pending":
                            made_pending = True
            else:
                tstate.status = "blocked"
                reason = r.get("reason") or ""
                tstate.reason = f"review failed: {reason}"
                state.save()

        if not made_pending:
            break

    report_path = planreport.write_report(plan, state, reviews, max_rounds)
    ok = all(t.name in approved for t in implement_tasks)
    return CloseResult(ok=ok, report=report_path)
