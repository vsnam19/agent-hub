import heapq
import json
import re
import string
from dataclasses import dataclass, field
from pathlib import Path

from agenthub import tasksize

ROLES = ("implement-small", "implement", "implement-hard")
TEMPLATE = Path(__file__).parent / "templates" / "techlead-brief.md"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
BLOCK_RE = re.compile(r"^[ \t]*```json[ \t]*\r?\n(.*?)```[ \t]*\r?$", re.DOTALL | re.MULTILINE | re.IGNORECASE)
OPEN_RE = re.compile(r"^[ \t]*```json[ \t]*\r?$", re.MULTILINE | re.IGNORECASE)
DEFAULT_ROLE = "implement"


class DraftError(Exception):
    pass


def techlead_brief(spec_path: str, gates: list[str]) -> str:
    """The techlead job's brief: the spec path, the repository gates and the tasksize limits filled in."""
    if not gates:
        raise ValueError("techlead brief needs at least one gate")
    template = string.Template(TEMPLATE.read_text(encoding="utf-8"))
    return template.substitute(
        spec_path=spec_path,
        gates=_join_gates(gates),
        max_files=tasksize.MAX_FILES,
        max_deliverables=tasksize.MAX_DELIVERABLES,
        max_tests=tasksize.MAX_TESTS,
    )


def _join_gates(gates: list[str]) -> str:
    """`a`, `b` and `c`: each gate backticked, joined the way the example brief joins its gates."""
    names = [f"`{gate}`" for gate in gates]
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


@dataclass
class DraftTask:
    name: str
    role: str
    depends_on: list[str] = field(default_factory=list)
    reason: str = ""
    brief: str = ""


@dataclass
class Draft:
    goal: str
    tasks: list[DraftTask]


def parse(reply: str) -> Draft:
    blocks = BLOCK_RE.findall(reply)
    if len(blocks) < len(OPEN_RE.findall(reply)):
        raise DraftError("the ```json block is not closed")
    if not blocks:
        raise DraftError("techlead reply has no ```json block")
    if len(blocks) > 1:
        raise DraftError(f"techlead reply has {len(blocks)} ```json blocks, expected exactly one")

    try:
        data = json.loads(blocks[0])
    except json.JSONDecodeError as e:
        raise DraftError(f"techlead json block is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise DraftError("techlead json must be an object")

    for key in ("goal", "tasks"):
        if key not in data:
            raise DraftError(f"techlead json missing field: {key}")
    if not isinstance(data["goal"], str) or not data["goal"].strip():
        raise DraftError("techlead goal must be a non-empty string")
    if not isinstance(data["tasks"], list) or not data["tasks"]:
        raise DraftError("techlead tasks must be a non-empty list")

    tasks = [_parse_task(i, raw) for i, raw in enumerate(data["tasks"])]
    names = [t.name for t in tasks]
    seen = set()
    for name in names:
        if name in seen:
            raise DraftError(f"duplicate task name: {name}")
        seen.add(name)
    for t in tasks:
        for dep in t.depends_on:
            if dep not in seen:
                raise DraftError(f"task {t.name} depends on unknown task: {dep}")
    _check_cycles(tasks)
    return Draft(goal=data["goal"], tasks=tasks)


def _parse_task(index: int, raw: object) -> DraftTask:
    if not isinstance(raw, dict):
        raise DraftError(f"task {index} must be an object")
    for key in ("name", "depends_on", "brief"):
        if key not in raw:
            raise DraftError(f"task {index} missing field: {key}")
    name = raw["name"]
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise DraftError(f"task {index} name must match [a-z0-9][a-z0-9-]*: {name!r}")
    role = raw.get("role", DEFAULT_ROLE)
    if role not in ROLES:
        raise DraftError(f"task {name} has bad role {role!r}, expected one of {', '.join(ROLES)}")
    deps = raw["depends_on"]
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        raise DraftError(f"task {name} depends_on must be a list of strings")
    reason = raw.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        raise DraftError(f"task {name} reason must be a string")
    if role != DEFAULT_ROLE and not reason.strip():
        raise DraftError(f"task {name} has role {role!r} and needs a non-empty reason")
    if not isinstance(raw["brief"], str):
        raise DraftError(f"task {name} brief must be a string")
    return DraftTask(name=name, role=role, depends_on=list(deps), reason=reason, brief=raw["brief"])


def _check_cycles(tasks: list[DraftTask]) -> None:
    # Iterative DFS with an explicit stack, so a long dependency chain cannot hit the recursion limit.
    deps = {t.name: t.depends_on for t in tasks}
    done: set[str] = set()

    for root in deps:
        if root in done:
            continue
        path = [root]
        on_path = {root}
        stack = [iter(deps[root])]
        while stack:
            dep = next(stack[-1], None)
            if dep is None:
                stack.pop()
                name = path.pop()
                on_path.discard(name)
                done.add(name)
            elif dep in on_path:
                cycle = path[path.index(dep) :] + [dep]
                raise DraftError(f"dependency cycle: {' -> '.join(cycle)}")
            elif dep not in done:
                path.append(dep)
                on_path.add(dep)
                stack.append(iter(deps[dep]))


def ordered(tasks: list[DraftTask]) -> list[DraftTask]:
    """Topological order: every task after its dependencies, ties kept in the original order.

    Assumes no cycles; `parse` already rejects them.
    """
    waiting = [len(t.depends_on) for t in tasks]
    dependents: dict[str, list[int]] = {t.name: [] for t in tasks}
    for i, t in enumerate(tasks):
        for dep in t.depends_on:
            dependents[dep].append(i)

    ready = [i for i, count in enumerate(waiting) if count == 0]
    heapq.heapify(ready)
    out: list[DraftTask] = []
    while ready:
        i = heapq.heappop(ready)
        out.append(tasks[i])
        for j in dependents[tasks[i].name]:
            waiting[j] -= 1
            if waiting[j] == 0:
                heapq.heappush(ready, j)
    return out


def write(
    draft: Draft, plan_dir: Path, *, repo: Path, slug: str, base: str, gates: list[str], max_parallel: int = 3
) -> Path:
    """Write `plan_dir/plan.json` and one `briefs/<name>.md` per task; return the plan.json path."""
    plan_path = plan_dir / "plan.json"
    if plan_path.exists():
        raise DraftError(f"plan already exists: {plan_path}")

    (plan_dir / "briefs").mkdir(parents=True, exist_ok=True)
    tasks = []
    for t in ordered(draft.tasks):
        brief_rel = f"briefs/{t.name}.md"
        (plan_dir / brief_rel).write_text(t.brief.rstrip("\n") + "\n", encoding="utf-8")
        entry = {
            "name": t.name,
            "role": t.role,
            "depends_on": list(t.depends_on),
            "gate": list(gates),
            "brief": brief_rel,
        }
        if t.reason:
            entry["reason"] = t.reason
        tasks.append(entry)

    data = {
        "goal": draft.goal,
        "repo": str(repo.resolve()),
        "base": base,
        "slug": slug,
        "max_parallel": max_parallel,
        "tasks": tasks,
    }
    plan_path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return plan_path
