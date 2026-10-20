from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import agenthub.planrun
from agenthub.audit.trace import ENV_TRACE_PLAN, ENV_TRACE_TASK
from agenthub.plan import Plan, Task
from agenthub.planrun import _trace_environ, run_plan
from agenthub.planstate import load_or_init
from tests.gitrepo import make_repo

# Mock PLAN_POLL_S to be fast in tests
agenthub.planrun.PLAN_POLL_S = 0.001


class FakePopen:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class FakeTraceSpawner:
    def __init__(
        self,
        results: dict[str, list[dict]] | None = None,
        raise_on_task: str | None = None,
    ) -> None:
        self.results = results or {}
        self.raise_on_task = raise_on_task
        self.spawns: list[tuple[list[str], Path, dict[str, str | None]]] = []

    def __call__(self, argv: list[str], log_path: Path) -> FakePopen:
        name = log_path.stem.rsplit("-", 1)[0]
        recorded_env = {
            ENV_TRACE_PLAN: os.environ.get(ENV_TRACE_PLAN),
            ENV_TRACE_TASK: os.environ.get(ENV_TRACE_TASK),
        }
        self.spawns.append((argv, log_path, recorded_env))

        if self.raise_on_task == name:
            raise RuntimeError(f"Simulated spawn failure on {name}")

        res_list = self.results.get(name, [])
        res = res_list.pop(0) if res_list else {"status": "ok", "exit_code": 0, "id": f"job_{name}"}
        if res is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps(res))
            return FakePopen(res.get("exit_code", 0))
        return FakePopen(0)


def make_plan(
    tmp_path: Path,
    tasks: list[dict],
    plan_name: str = "plan",
) -> Plan:
    repo_dir = tmp_path / "repo"
    if not repo_dir.exists():
        make_repo(repo_dir)
    plan_dir = tmp_path / plan_name
    plan_dir.mkdir(parents=True, exist_ok=True)

    plan_tasks = []
    for t in tasks:
        brief_path = plan_dir / f"{t['name']}.md"
        brief_path.write_text(f"brief for {t['name']}")
        pt = Task(
            name=t["name"],
            role=t.get("role", "implement"),
            kind=t.get("kind"),
            agent=t.get("agent"),
            brief=brief_path if t.get("kind", t.get("role", "implement")) == "implement" else None,
            verifies=t.get("verifies"),
            depends_on=t.get("depends_on", []),
            paths_allowed=t.get("paths_allowed", []),
            gate=t.get("gate", []),
            skills=t.get("skills", []),
            max_attempts=t.get("max_attempts", 2),
        )
        plan_tasks.append(pt)

    plan_file = plan_dir / "plan.json"
    plan = Plan(
        goal="goal",
        repo=repo_dir,
        base="master",
        slug=plan_name,
        tasks=plan_tasks,
        max_parallel=2,
        path=plan_file,
    )
    return plan


def test_spawn_sees_plan_and_task_env(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, [{"name": "task-alpha"}], plan_name="my-plan-42")
    state = load_or_init(plan)
    spawn = FakeTraceSpawner()

    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert len(spawn.spawns) == 1
    argv, log_path, env_seen = spawn.spawns[0]
    assert env_seen[ENV_TRACE_PLAN] == "my-plan-42"
    assert env_seen[ENV_TRACE_TASK] == "task-alpha"


def test_env_restored_after_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. When pre-existing environment variables exist
    monkeypatch.setenv(ENV_TRACE_PLAN, "preexisting-plan")
    monkeypatch.setenv(ENV_TRACE_TASK, "preexisting-task")

    plan = make_plan(tmp_path, [{"name": "task-bravo"}], plan_name="plan-bravo")
    state = load_or_init(plan)
    spawn = FakeTraceSpawner()

    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert len(spawn.spawns) == 1
    _, _, env_seen = spawn.spawns[0]
    assert env_seen[ENV_TRACE_PLAN] == "plan-bravo"
    assert env_seen[ENV_TRACE_TASK] == "task-bravo"

    assert os.environ.get(ENV_TRACE_PLAN) == "preexisting-plan"
    assert os.environ.get(ENV_TRACE_TASK) == "preexisting-task"

    # 2. When environment variables were unset prior to spawn
    monkeypatch.delenv(ENV_TRACE_PLAN, raising=False)
    monkeypatch.delenv(ENV_TRACE_TASK, raising=False)

    plan2 = make_plan(tmp_path, [{"name": "task-charlie"}], plan_name="plan-charlie")
    state2 = load_or_init(plan2)
    spawn2 = FakeTraceSpawner()

    rc2 = run_plan(plan2, state2, spawn2)
    assert rc2 == 0
    assert ENV_TRACE_PLAN not in os.environ
    assert ENV_TRACE_TASK not in os.environ


def test_env_restored_when_spawn_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. When pre-existing environment variables exist
    monkeypatch.setenv(ENV_TRACE_PLAN, "orig-plan")
    monkeypatch.setenv(ENV_TRACE_TASK, "orig-task")

    plan = make_plan(tmp_path, [{"name": "failing-task"}], plan_name="raise-plan")
    state = load_or_init(plan)
    spawn = FakeTraceSpawner(raise_on_task="failing-task")

    with pytest.raises(RuntimeError, match="Simulated spawn failure on failing-task"):
        run_plan(plan, state, spawn)

    assert os.environ.get(ENV_TRACE_PLAN) == "orig-plan"
    assert os.environ.get(ENV_TRACE_TASK) == "orig-task"

    # 2. When originally unset
    monkeypatch.delenv(ENV_TRACE_PLAN, raising=False)
    monkeypatch.delenv(ENV_TRACE_TASK, raising=False)

    plan2 = make_plan(tmp_path, [{"name": "failing-task-2"}], plan_name="raise-plan-2")
    state2 = load_or_init(plan2)
    spawn2 = FakeTraceSpawner(raise_on_task="failing-task-2")

    with pytest.raises(RuntimeError, match="Simulated spawn failure on failing-task-2"):
        run_plan(plan2, state2, spawn2)

    assert ENV_TRACE_PLAN not in os.environ
    assert ENV_TRACE_TASK not in os.environ


def test_each_task_gets_its_own_values(tmp_path: Path) -> None:
    plan = make_plan(
        tmp_path,
        [
            {"name": "step-1"},
            {"name": "step-2", "depends_on": ["step-1"]},
        ],
        plan_name="sequential-plan",
    )
    state = load_or_init(plan)
    spawn = FakeTraceSpawner()

    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert len(spawn.spawns) == 2

    # Check first task spawn
    _, log_path_1, env_1 = spawn.spawns[0]
    assert log_path_1.stem.startswith("step-1-")
    assert env_1[ENV_TRACE_PLAN] == "sequential-plan"
    assert env_1[ENV_TRACE_TASK] == "step-1"

    # Check second task spawn
    _, log_path_2, env_2 = spawn.spawns[1]
    assert log_path_2.stem.startswith("step-2-")
    assert env_2[ENV_TRACE_PLAN] == "sequential-plan"
    assert env_2[ENV_TRACE_TASK] == "step-2"


def test_trace_environ_context_manager_standalone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_TRACE_PLAN, raising=False)
    monkeypatch.delenv(ENV_TRACE_TASK, raising=False)

    plan = make_plan(tmp_path, [{"name": "t1"}], plan_name="standalone-plan")

    with _trace_environ(plan, "standalone-task"):
        assert os.environ.get(ENV_TRACE_PLAN) == "standalone-plan"
        assert os.environ.get(ENV_TRACE_TASK) == "standalone-task"

    assert ENV_TRACE_PLAN not in os.environ
    assert ENV_TRACE_TASK not in os.environ
