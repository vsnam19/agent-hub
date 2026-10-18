import subprocess
from pathlib import Path

from agenthub.guards import _git_argv, _git_env
from agenthub.plan import Plan
from agenthub.planrun import merge_order
from agenthub.planstate import State


def _git_identity(worktree: Path) -> list[str]:
    identity = []
    for key, fallback in (("user.name", "agent-hub"), ("user.email", "agent-hub@localhost")):
        res = subprocess.run(
            _git_argv("config", "--get", key),
            cwd=worktree,
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if res.returncode != 0:
            identity += ["-c", f"{key}={fallback}"]
    return identity


def cascade(plan: Plan, state: State, task_name: str) -> dict[str, str]:
    """Merge the fixed branch of task_name into each done dependent in dependency order."""
    results: dict[str, str] = {}
    fixed_task_state = state.task(task_name)
    fixed_branch = fixed_task_state.branch or f"orch/{plan.slug}/{task_name}"

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

    ordered = merge_order(plan)
    dependents = [
        t
        for t in ordered
        if t.name != task_name and task_name in get_transitive_deps(t.name) and state.task(t.name).status == "done"
    ]

    for dep in dependents:
        dep_state = state.task(dep.name)
        if not dep_state.worktree:
            continue
        worktree = Path(dep_state.worktree)
        if not worktree.is_dir():
            continue

        merge_cmd = _git_argv(
            *_git_identity(worktree),
            "-c",
            "commit.gpgsign=false",
            "merge",
            "--no-edit",
            fixed_branch,
        )
        res = subprocess.run(merge_cmd, cwd=worktree, capture_output=True, text=True, env=_git_env())
        if res.returncode == 0:
            results[dep.name] = "merged"
            continue

        merge_head_proc = subprocess.run(
            _git_argv("rev-parse", "-q", "--verify", "MERGE_HEAD"),
            cwd=worktree,
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if merge_head_proc.returncode != 0:
            err_lines = (res.stderr or "").strip().splitlines()
            first_line = err_lines[0].strip() if err_lines else "merge failed"
            results[dep.name] = f"error: {first_line}"
            continue

        diff_cmd = _git_argv("diff", "--name-only", "--diff-filter=U")
        diff_res = subprocess.run(diff_cmd, cwd=worktree, capture_output=True, text=True, env=_git_env())
        conflicted = [f.strip() for f in diff_res.stdout.splitlines() if f.strip()]

        subprocess.run(
            _git_argv("merge", "--abort"),
            cwd=worktree,
            capture_output=True,
            text=True,
            env=_git_env(),
        )

        if conflicted:
            results[dep.name] = f"conflict: {', '.join(sorted(conflicted))}"
        else:
            results[dep.name] = "conflict: unknown"

    return results
