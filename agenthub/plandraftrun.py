from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agenthub import tasksize
from agenthub.config import load_pool, paths
from agenthub.orchestrate import RunArgs, repo_gates, run_job
from agenthub.plandraft import Draft, DraftError, DraftTask, ordered, parse, techlead_brief, write
from agenthub.pool import parse_link


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def resplit_brief(brief: str, problems: dict[str, list[str]]) -> str:
    """The first brief followed by a `## Re-split` section listing each oversized task and its problems."""
    lines = [
        "## Re-split",
        "",
        "The plan you wrote has tasks too large for one run. Write the whole plan again with smaller tasks.",
        "Each task below breaks a size rule:",
        "",
    ]
    for name, found in problems.items():
        lines.append(f"- {name}:")
        lines.extend(f"  - {problem}" for problem in found)
    lines += ["", "Reply with the whole plan in one ```json block, as before."]
    return brief.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n"


def _ask(brief: str, repo: Path, run: Callable[..., dict], now: Callable[[], datetime]) -> Draft:
    pool = load_pool()
    args = RunArgs(
        role="techlead",
        kind="plan",
        brief=brief,
        workdir=repo,
        links=[parse_link(x) for x in pool.roles["techlead"].chain],
        timeout=pool.timeouts["techlead"],
        base=None,
    )
    result = run(args, paths(), pool, now)
    status = result.get("status")
    if status != "ok":
        raise DraftError(f"techlead job {status}")
    reply_path = result.get("reply_path")
    if not reply_path:
        raise DraftError(f"techlead job {status}: no reply")
    return parse(Path(reply_path).read_text(encoding="utf-8"))


def _problems(tasks: list[DraftTask]) -> dict[str, list[str]]:
    found = {t.name: tasksize.check(t.brief) for t in tasks}
    return {name: problems for name, problems in found.items() if problems}


def draft(
    spec: Path,
    slug: str,
    base: str,
    repo: Path,
    run: Callable[..., dict] = run_job,
    now: Callable[[], datetime] = _utc_now,
    state_dir: Path | None = None,
) -> dict:
    """Ask the techlead for a plan from the spec, re-split once if a task is too large, and write it.

    Returns the plan path, whether a re-split happened, and the task names in plan order. Never runs the plan.
    """
    gates = repo_gates(repo)
    if not gates:
        raise DraftError("no repo gates: set gates in .agent-hub/config.toml")
    brief = techlead_brief(str(spec), gates)

    plan = _ask(brief, repo, run, now)
    problems = _problems(plan.tasks)
    resplit = bool(problems)
    if resplit:
        plan = _ask(resplit_brief(brief, problems), repo, run, now)
        problems = _problems(plan.tasks)
        if problems:
            lines = [f"{name}: {problem}" for name, found in problems.items() for problem in found]
            raise DraftError("techlead re-split still has oversized tasks:\n" + "\n".join(lines))

    plan_dir = (state_dir or paths().state_dir) / "plans" / slug
    plan_path = write(plan, plan_dir, repo=repo, slug=slug, base=base, gates=gates)
    return {
        "plan_path": str(plan_path),
        "resplit": resplit,
        "tasks": [t.name for t in ordered(plan.tasks)],
    }
