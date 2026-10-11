import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agenthub.config import ConfigError, load_pool, role_kind
from agenthub.guards import _run_git


class PlanError(Exception):
    pass


@dataclass
class Task:
    name: str
    role: str
    kind: str | None = None
    brief: Path | None = None
    verifies: str | None = None
    gate: list[str] = field(default_factory=list)
    paths_allowed: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    agent: str | None = None
    max_attempts: int = 2
    timeout: str | None = None


@dataclass
class Plan:
    goal: str
    repo: Path
    base: str
    slug: str
    tasks: list[Task]
    max_parallel: int = 2
    path: Path | None = None
    jev: bool = False


def load(path: Path) -> Plan:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise PlanError(f"failed to read/parse json: {e}") from e

    errors = []
    try:
        pool = load_pool()
    except ConfigError as e:
        raise PlanError(str(e)) from e

    for k in ["goal", "repo", "base", "slug", "tasks"]:
        if k not in data:
            errors.append(f"missing required field: {k}")

    if errors:
        raise PlanError("\n".join(errors))

    repo = Path(data["repo"]).resolve()
    base = str(data["base"])
    slug = str(data["slug"])

    if not slug or not re.fullmatch(r"[a-z0-9-]+", slug):
        errors.append("slug must be [a-z0-9-]+")

    try:
        # Check if it's a valid git repo
        _run_git(repo, "rev-parse", "--is-inside-work-tree")
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        errors.append("repo is not an existing git work tree")

    try:
        if not errors or "repo" not in errors[-1]:
            _run_git(repo, "rev-parse", "--verify", "--quiet", base)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        errors.append("base is not an existing branch")

    tasks_data = data["tasks"]
    if not isinstance(tasks_data, list) or not tasks_data:
        errors.append("tasks must be a non-empty list")
        raise PlanError("\n".join(errors))

    tasks = []
    task_names = set()
    for i, tdata in enumerate(tasks_data):
        name = tdata.get("name")
        if not name or not re.fullmatch(r"[a-z0-9-]+", str(name)):
            errors.append(f"task {i}: name must be [a-z0-9-]+")
            name = str(name) if name else f"__idx_{i}"

        if name in task_names:
            errors.append(f"task {i}: duplicate name {name}")
        task_names.add(name)

        role = tdata.get("role")
        try:
            kind = role_kind(pool, str(role))
        except ConfigError as e:
            errors.append(f"task {name}: {e}")
            kind = None

        brief_path = None
        verifies = None

        if kind == "implement":
            brief = tdata.get("brief")
            if not brief:
                errors.append(f"task {name}: implement needs brief")
            else:
                brief_path = path.parent / brief
                if not brief_path.exists():
                    errors.append(f"task {name}: brief file {brief_path} must exist")
        elif kind == "review":
            verifies = tdata.get("verifies")
            if not verifies:
                errors.append(f"task {name}: review needs verifies")
            else:
                if verifies not in task_names and verifies not in [t.get("name") for t in tasks_data]:
                    errors.append(f"task {name}: verifies names a non-existent task")

        depends_on = tdata.get("depends_on", [])
        for dep in depends_on:
            if dep not in [t.get("name") for t in tasks_data]:
                errors.append(f"task {name}: depends_on names non-existent task {dep}")

        agent = tdata.get("agent")
        if agent is not None and not isinstance(agent, str):
            errors.append(f"task {name}: agent must be null or string")

        tasks.append(
            Task(
                name=name,
                role=str(role),
                kind=kind,
                brief=brief_path,
                verifies=verifies,
                gate=tdata.get("gate", []),
                paths_allowed=tdata.get("paths_allowed", []),
                depends_on=depends_on,
                agent=agent,
                max_attempts=int(tdata.get("max_attempts", 2)),
                timeout=tdata.get("timeout"),
            )
        )

    # Review implicitly depends on what it verifies
    for t in tasks:
        if t.kind == "review" and t.verifies and t.verifies not in t.depends_on:
            t.depends_on.append(t.verifies)

    # Cycle detection
    # a simple DFS to detect cycles
    visited = {}

    def dfs(node: str, path_nodes: list[str]) -> bool:
        if visited.get(node) == -1:
            return True  # cycle
        if visited.get(node) == 1:
            return False

        visited[node] = -1
        path_nodes.append(node)

        task_obj = next((t for t in tasks if t.name == node), None)
        if task_obj:
            for dep in task_obj.depends_on:
                if dfs(dep, path_nodes):
                    return True

        path_nodes.pop()
        visited[node] = 1
        return False

    for t in tasks:
        if t.name not in visited:
            cycle_path = []
            if dfs(t.name, cycle_path):
                errors.append(f"cycle detected: {' -> '.join(cycle_path + [cycle_path[-1]])}")
                break

    if errors:
        raise PlanError("\n".join(errors))

    return Plan(
        goal=str(data["goal"]),
        repo=repo,
        base=base,
        slug=slug,
        tasks=tasks,
        max_parallel=int(data.get("max_parallel", 2)),
        path=path,
        jev=bool(data.get("jev", False)),
    )
