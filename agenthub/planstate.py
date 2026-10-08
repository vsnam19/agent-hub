import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agenthub.config import paths
from agenthub.plan import Plan


@dataclass
class AttemptState:
    job: str
    status: str
    exit_code: int


@dataclass
class TaskState:
    status: str = "pending"
    attempts: list[AttemptState] = field(default_factory=list)
    worktree: str = ""
    branch: str = ""
    start_ref: str = ""
    reason: str = ""
    handoff: str = ""


@dataclass
class State:
    slug: str
    started: str
    updated: str
    tasks: dict[str, TaskState] = field(default_factory=dict)

    _path: Path = field(default=None, repr=False, init=False)

    def save(self) -> None:
        if not getattr(self, "_path", None):
            return

        self.updated = datetime.now(timezone.utc).astimezone().isoformat()

        data = {
            "slug": self.slug,
            "started": self.started,
            "updated": self.updated,
            "tasks": {
                name: {
                    "status": t.status,
                    "attempts": [asdict(a) for a in t.attempts],
                    "worktree": t.worktree,
                    "branch": t.branch,
                    "start_ref": t.start_ref,
                    "reason": t.reason,
                    "handoff": t.handoff,
                }
                for name, t in self.tasks.items()
            },
        }

        d = self._path.parent
        d.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=d, prefix="state.json.tmp", text=True)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(temp_path, self._path)
        except Exception:
            os.unlink(temp_path)
            raise

    def task(self, name: str) -> TaskState:
        return self.tasks[name]

    def resume(self) -> None:
        p = paths()
        changed = False
        for _name, task_state in self.tasks.items():
            if task_state.status == "running" and task_state.attempts:
                last_attempt = task_state.attempts[-1]
                job_result_path = p.jobs_dir / last_attempt.job / "result.json"
                if job_result_path.is_file():
                    # Has result, we leave it as running but the planrun loop must pick it up.
                    # Wait, the spec says "finishes the attempt".
                    # Let's just update the attempt status/exit_code so we know it has finished.
                    try:
                        with open(job_result_path, "r") as f:
                            result = json.load(f)
                        last_attempt.status = result.get("status", last_attempt.status)
                        last_attempt.exit_code = result.get("exit_code", last_attempt.exit_code)
                        changed = True
                    except Exception:
                        pass
                else:
                    # No result, job is dead
                    task_state.status = "pending"
                    task_state.attempts.pop()
                    changed = True

        if changed:
            self.save()


def load_or_init(plan: Plan) -> State:
    state_file = plan.path.parent / "state.json"
    if state_file.is_file():
        with open(state_file, "r") as f:
            data = json.load(f)

        tasks_state = {}
        for k, v in data.get("tasks", {}).items():
            attempts = [AttemptState(**a) for a in v.get("attempts", [])]
            tasks_state[k] = TaskState(
                status=v.get("status", "pending"),
                attempts=attempts,
                worktree=v.get("worktree", ""),
                branch=v.get("branch", ""),
                start_ref=v.get("start_ref", ""),
                reason=v.get("reason", ""),
                handoff=v.get("handoff", ""),
            )

        state = State(
            slug=data.get("slug", plan.slug),
            started=data.get("started", datetime.now(timezone.utc).astimezone().isoformat()),
            updated=data.get("updated", datetime.now(timezone.utc).astimezone().isoformat()),
            tasks=tasks_state,
        )
        state._path = state_file

        for t in plan.tasks:
            if t.name not in state.tasks:
                state.tasks[t.name] = TaskState()

        state.resume()
        return state

    state = State(
        slug=plan.slug,
        started=datetime.now(timezone.utc).astimezone().isoformat(),
        updated=datetime.now(timezone.utc).astimezone().isoformat(),
        tasks={t.name: TaskState() for t in plan.tasks},
    )
    state._path = state_file
    state.save()
    return state
