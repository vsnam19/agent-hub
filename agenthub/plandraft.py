import json
import re
from dataclasses import dataclass, field

ROLES = ("implement-small", "implement", "implement-hard")
NAME_RE = re.compile(r"[a-z0-9-]+")
BLOCK_RE = re.compile(r"^[ \t]*```json[ \t]*\r?\n(.*?)```[ \t]*\r?$", re.DOTALL | re.MULTILINE | re.IGNORECASE)
DEFAULT_ROLE = "implement"


class DraftError(Exception):
    pass


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
    if not isinstance(data["goal"], str):
        raise DraftError("techlead goal must be a string")
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
        raise DraftError(f"task {index} name must match [a-z0-9-]+: {name!r}")
    role = raw.get("role", DEFAULT_ROLE)
    if role not in ROLES:
        raise DraftError(f"task {name} has bad role {role!r}, expected one of {', '.join(ROLES)}")
    deps = raw["depends_on"]
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        raise DraftError(f"task {name} depends_on must be a list of strings")
    reason = raw.get("reason", "")
    if not isinstance(reason, str):
        raise DraftError(f"task {name} reason must be a string")
    if role != DEFAULT_ROLE and not reason.strip():
        raise DraftError(f"task {name} has role {role!r} and needs a non-empty reason")
    if not isinstance(raw["brief"], str):
        raise DraftError(f"task {name} brief must be a string")
    return DraftTask(name=name, role=role, depends_on=list(deps), reason=reason, brief=raw["brief"])


def _check_cycles(tasks: list[DraftTask]) -> None:
    deps = {t.name: t.depends_on for t in tasks}
    done: set[str] = set()

    def visit(name: str, path: list[str]) -> None:
        if name in path:
            cycle = path[path.index(name) :] + [name]
            raise DraftError(f"dependency cycle: {' -> '.join(cycle)}")
        if name in done:
            return
        for dep in deps[name]:
            visit(dep, path + [name])
        done.add(name)

    for name in deps:
        visit(name, [])
