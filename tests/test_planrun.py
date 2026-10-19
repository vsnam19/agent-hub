import json
import subprocess
from pathlib import Path

import agenthub.planrun
from agenthub.config import load_pool, paths
from agenthub.guards import _git_argv
from agenthub.plan import Plan, Task
from agenthub.planrun import _reviewers_besides, compose_brief, run_plan
from agenthub.planstate import load_or_init
from tests.gitrepo import make_repo

# Mock PLAN_POLL_S to be very fast for tests
agenthub.planrun.PLAN_POLL_S = 0.001


class FakePopen:
    def __init__(self, returncode=0):
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class FakeSpawner:
    def __init__(self, results):
        self.results = results
        self.spawns = []

    def __call__(self, argv, log_path):
        self.spawns.append((argv, log_path))
        name = log_path.stem.rsplit("-", 1)[0]
        res_list = self.results.get(name, [])
        res = res_list.pop(0) if res_list else {"status": "failed", "exit_code": 1, "id": "job_default"}

        if res is not None:
            log_path.write_text(json.dumps(res))
            return FakePopen(res.get("exit_code", 0))
        return FakePopen(0)


def make_plan(tmp_path, tasks):
    repo_dir = tmp_path / "repo"
    if not repo_dir.exists():
        make_repo(repo_dir)
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

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
        goal="goal", repo=repo_dir, base="master", slug="testslug", tasks=plan_tasks, max_parallel=2, path=plan_file
    )
    return plan


def test_single_task_done(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}])
    state = load_or_init(plan)

    spawn = FakeSpawner(
        {
            "t1": [
                {
                    "status": "ok",
                    "exit_code": 0,
                    "id": "j1",
                    "handoff": {"status": "success"},
                    "reply_path": str(tmp_path / "reply.md"),
                }
            ]
        }
    )
    (tmp_path / "reply.md").write_text("my reply")

    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert state.task("t1").status == "done"


def test_dependency_order(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1", "depends_on": ["t2"]}, {"name": "t2"}])
    state = load_or_init(plan)

    spawn = FakeSpawner({"t2": [{"status": "ok"}], "t1": [{"status": "ok"}]})

    rc = run_plan(plan, state, spawn)
    assert rc == 0
    # verify t2 spawned before t1
    assert spawn.spawns[0][1].stem.startswith("t2-")
    assert spawn.spawns[1][1].stem.startswith("t1-")


def test_parallel_limit(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}, {"name": "t2"}, {"name": "t3"}])
    state = load_or_init(plan)

    # We want to check max_parallel=1
    spawn = FakeSpawner({"t1": [{"status": "ok"}], "t2": [{"status": "ok"}], "t3": [{"status": "ok"}]})

    rc = run_plan(plan, state, spawn, max_parallel=1)
    assert rc == 0
    # It would be better to check that only 1 was running at a time, but this is a simple check
    assert len(spawn.spawns) == 3


def test_dependent_starts_from_upstream_branch(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}, {"name": "t2", "depends_on": ["t1"]}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "ok"}], "t2": [{"status": "ok"}]})

    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert state.task("t2").start_ref == state.task("t1").branch


def test_two_dependencies_merged(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}, {"name": "t2"}, {"name": "t3", "depends_on": ["t1", "t2"]}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "ok"}], "t2": [{"status": "ok"}], "t3": [{"status": "ok"}]})

    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert state.task("t3").status == "done"


def test_merge_ignores_commit_signing(tmp_path):
    repo_dir = make_repo(tmp_path / "repo")
    import subprocess

    from agenthub.guards import _git_argv

    subprocess.run(_git_argv("config", "commit.gpgsign", "true"), cwd=repo_dir)
    subprocess.run(_git_argv("config", "gpg.program", "/bin/false"), cwd=repo_dir)

    plan = make_plan(tmp_path, [{"name": "t1"}, {"name": "t2"}, {"name": "t3", "depends_on": ["t1", "t2"]}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "ok"}], "t2": [{"status": "ok"}], "t3": [{"status": "ok"}]})

    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert state.task("t3").status == "done"


def test_merge_conflict_blocks(tmp_path):
    repo_dir = make_repo(tmp_path / "repo")

    # Make t1 and t2 branches with conflicting changes
    import subprocess

    from agenthub.guards import _git_argv

    subprocess.run(_git_argv("branch", "orch/testslug/t1"), cwd=repo_dir)
    subprocess.run(_git_argv("branch", "orch/testslug/t2"), cwd=repo_dir)

    subprocess.run(_git_argv("checkout", "orch/testslug/t1"), cwd=repo_dir)
    (repo_dir / "conflict.txt").write_text("t1")
    subprocess.run(_git_argv("add", "conflict.txt"), cwd=repo_dir)
    subprocess.run(_git_argv("commit", "-m", "t1"), cwd=repo_dir)

    subprocess.run(_git_argv("checkout", "orch/testslug/t2"), cwd=repo_dir)
    (repo_dir / "conflict.txt").write_text("t2")
    subprocess.run(_git_argv("add", "conflict.txt"), cwd=repo_dir)
    subprocess.run(_git_argv("commit", "-m", "t2"), cwd=repo_dir)

    subprocess.run(_git_argv("checkout", "master"), cwd=repo_dir)

    plan = make_plan(tmp_path, [{"name": "t1"}, {"name": "t2"}, {"name": "t3", "depends_on": ["t1", "t2"]}])
    # Manually set t1 and t2 to done with branches
    state = load_or_init(plan)
    state.task("t1").status = "done"
    state.task("t1").branch = "orch/testslug/t1"
    state.task("t2").status = "done"
    state.task("t2").branch = "orch/testslug/t2"

    spawn = FakeSpawner({"t3": [{"status": "ok"}]})

    rc = run_plan(plan, state, spawn)
    assert rc == 1
    assert state.task("t3").status == "blocked"
    assert "merge conflict" in state.task("t3").reason


def test_failed_dependency_skips_downstream(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}, {"name": "t2", "depends_on": ["t1"]}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "failed"}]})
    rc = run_plan(plan, state, spawn)
    assert rc == 1
    assert state.task("t1").status == "failed"
    assert state.task("t2").status == "skipped"


def test_gate_failed_retried_with_tail(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}])
    state = load_or_init(plan)
    spawn = FakeSpawner(
        {"t1": [{"status": "gate-failed", "gate": [{"cmd": "test", "rc": 1, "tail": "error!"}]}, {"status": "ok"}]}
    )
    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert len(state.task("t1").attempts) == 2
    assert state.task("t1").status == "done"


def test_timeout_retry_note(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "timeout"}, {"status": "ok"}]})
    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert len(state.task("t1").attempts) == 2


def test_failed_retried_only_once(tmp_path):
    # legacy pool without escalate links: a failed task stays failed (escalation is covered separately)
    config_dir = paths().config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "pool.toml").write_text('[roles.implement]\nkind = "implement"\nchain = ["claude:haiku"]\n')
    plan = make_plan(tmp_path, [{"name": "t1", "max_attempts": 5}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "failed"}, {"status": "failed"}, {"status": "ok"}]})
    rc = run_plan(plan, state, spawn)
    assert rc == 1
    assert len(state.task("t1").attempts) == 2
    assert state.task("t1").status == "failed"


def test_exhausted_not_retried_blocked(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "exhausted"}]})
    rc = run_plan(plan, state, spawn)
    assert rc == 1
    assert len(state.task("t1").attempts) == 1
    assert state.task("t1").status == "blocked"


def test_scope_violation(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1", "paths_allowed": ["agenthub/**"]}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "ok", "changed_files": ["tests/test_x.py"]}]})
    rc = run_plan(plan, state, spawn)
    assert rc == 1
    assert state.task("t1").status == "scope-violation"


def test_review_runs_in_verified_worktree_with_base(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}, {"name": "r1", "role": "review", "verifies": "t1"}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "ok"}], "r1": [{"status": "ok"}]})
    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert state.task("r1").status == "done"

    # Check argv for r1
    r1_argv = spawn.spawns[1][0]
    assert "--role" in r1_argv
    assert "review" in r1_argv
    assert "--base" in r1_argv


def test_plan_task_skills_reach_brief(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1", "skills": ["debug-playbook", "telaf"]}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "ok"}]})
    rc = run_plan(plan, state, spawn)
    assert rc == 0
    argv = spawn.spawns[0][0]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--skill"] == ["debug-playbook", "telaf"]


def test_upstream_handoff_in_brief(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}, {"name": "t2", "depends_on": ["t1"]}])
    state = load_or_init(plan)

    (tmp_path / "reply1.md").write_text("t1 completed")
    spawn = FakeSpawner({"t1": [{"status": "ok", "reply_path": str(tmp_path / "reply1.md")}], "t2": [{"status": "ok"}]})
    rc = run_plan(plan, state, spawn)
    assert rc == 0

    t2_brief_file = plan.path.parent / "briefs-sent" / "t2-1.md"
    content = t2_brief_file.read_text()
    assert "## Upstream handoff: t1" in content
    assert "t1 completed" in content


def test_resume_after_restart(tmp_path, monkeypatch):

    monkeypatch.setenv("AGENT_HUB_STATE", str(tmp_path))
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path))
    plan = make_plan(tmp_path, [{"name": "t1"}])
    state = load_or_init(plan)
    state.task("t1").status = "running"
    from agenthub.planstate import AttemptState

    state.task("t1").attempts.append(AttemptState("job1", "", -1))
    state.save()

    # write job1 result
    from agenthub.config import paths

    p = paths()
    (p.jobs_dir / "job1").mkdir(parents=True, exist_ok=True)
    (p.jobs_dir / "job1" / "result.json").write_text(json.dumps({"status": "ok", "exit_code": 0}))

    state2 = load_or_init(plan)
    # the task was running but job1 has a result, so load_or_init updates attempt
    # wait, load_or_init updates attempt, then run_plan loop picks up the running task and finishes it
    spawn = FakeSpawner({})
    rc = run_plan(plan, state2, spawn)
    assert rc == 0
    assert state2.task("t1").status == "done"


def test_compose_brief_sections(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}])
    state = load_or_init(plan)
    brief = compose_brief(plan, plan.tasks[0], state, "## Previous note\nhello")
    assert "## Goal (whole plan)" in brief
    assert "## Previous note" in brief


def test_exit_code_all_done_vs_not(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "failed"}]})
    rc = run_plan(plan, state, spawn)
    assert rc == 1


def test_gate_touched_in_task_state(tmp_path, monkeypatch):
    import json

    from agenthub.planstate import AttemptState, State, TaskState

    # We can just test that loading and saving state preserves gate_touched
    state = State(
        slug="test",
        started="now",
        updated="now",
        tasks={
            "t1": TaskState(
                attempts=[
                    AttemptState(
                        job="j1", status="ok", exit_code=0, gate_touched=[{"file": "x", "kind": "y", "lines": [1]}]
                    )
                ]
            )
        },
    )
    state._path = tmp_path / "state.json"
    state.save()

    with open(state._path) as f:
        data = json.load(f)

    assert data["tasks"]["t1"]["attempts"][0]["gate_touched"] == [{"file": "x", "kind": "y", "lines": [1]}]


def _record_job(job_id, agent, model):
    job_dir = paths().jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "result.json").write_text(json.dumps({"id": job_id, "status": "ok", "agent": agent, "model": model}))


def test_review_skips_implementing_model(tmp_path):
    implementer = "agy:gemini-4-argon-medium"
    assert implementer in load_pool().roles["review"].chain
    plan = make_plan(
        tmp_path, [{"name": "t1"}, {"name": "r1", "role": "review", "verifies": "t1", "depends_on": ["t1"]}]
    )
    state = load_or_init(plan)
    _record_job("j1", "agy", "gemini-4-argon-medium")
    spawn = FakeSpawner({"t1": [{"status": "ok", "id": "j1"}], "r1": [{"status": "ok"}]})

    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert state.task("r1").status == "done"
    r1_argv = spawn.spawns[1][0]
    assert r1_argv[r1_argv.index("--exclude") + 1] == implementer


def test_review_blocked_when_only_implementing_model_left(tmp_path):
    config_dir = paths().config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "pool.toml").write_text('[roles.review]\nkind = "review"\nchain = ["claude:haiku"]\n')
    plan = make_plan(
        tmp_path, [{"name": "t1"}, {"name": "r1", "role": "review", "verifies": "t1", "depends_on": ["t1"]}]
    )
    state = load_or_init(plan)
    _record_job("j1", "claude", "haiku")
    spawn = FakeSpawner({"t1": [{"status": "ok", "id": "j1"}]})

    rc = run_plan(plan, state, spawn)
    assert rc == 1
    assert state.task("r1").status == "blocked"
    assert state.task("r1").reason == "no reviewer different from claude:haiku"
    assert len(spawn.spawns) == 1


def test_reviewers_besides_ignores_effort(tmp_path):
    config_dir = paths().config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "pool.toml").write_text(
        '[roles.review]\nkind = "review"\nchain = ["claude:opus@low", "claude:haiku@high"]\n'
    )
    assert _reviewers_besides("review", "claude:opus") == ["claude:haiku@high"]
    assert _reviewers_besides("review", "claude:haiku") == ["claude:opus@low"]
    assert _reviewers_besides("review", "claude:sonnet") == ["claude:opus@low", "claude:haiku@high"]


def _escalating_plan(tmp_path, role="implement-small"):
    return make_plan(tmp_path, [{"name": "t1", "role": role, "kind": "implement"}])


def _role_of(argv):
    return argv[argv.index("--role") + 1]


def _gate_failed(tail="boom"):
    return {"status": "gate-failed", "gate": [{"cmd": "just test", "rc": 1, "tail": tail}]}


def test_gate_failed_task_escalates_once_with_gate_tail(tmp_path):
    plan = _escalating_plan(tmp_path)
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [_gate_failed("a"), _gate_failed("tail-xyz"), {"status": "ok"}]})
    rc = run_plan(plan, state, spawn)
    assert rc == 0
    assert [_role_of(a) for a, _ in spawn.spawns] == ["implement-small", "implement-small", "implement"]
    tstate = state.task("t1")
    assert tstate.status == "done"
    assert tstate.escalated_from == "implement-small"
    assert len(tstate.attempts) == 3
    sent = (plan.path.parent / "briefs-sent" / "t1-3.md").read_text()
    assert "## Previous attempt failed" in sent
    assert "tail-xyz" in sent


def test_escalation_resets_committed_work(tmp_path):
    plan = _escalating_plan(tmp_path)
    state = load_or_init(plan)

    class Committing(FakeSpawner):
        def __call__(self, argv, log_path):
            wt = argv[argv.index("-d") + 1]
            if _role_of(argv) == "implement-small":
                junk = f"junk{len(self.spawns)}.txt"
                (Path(wt) / junk).write_text("x")
                subprocess.run(["git", "add", junk], cwd=wt, check=True)
                subprocess.run(
                    ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-m", "junk"], cwd=wt, check=True
                )
                (Path(wt) / f"untracked{len(self.spawns)}.txt").write_text("y")
            else:
                self.seen = sorted(p.name for p in Path(wt).iterdir() if p.name != ".git")
            return super().__call__(argv, log_path)

    spawn = Committing({"t1": [_gate_failed(), _gate_failed(), {"status": "ok"}]})
    assert run_plan(plan, state, spawn) == 0
    assert spawn.seen == ["README.md"]
    wt = state.task("t1").worktree
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True).stdout.strip()
    start = subprocess.run(
        ["git", "rev-parse", state.task("t1").start_ref], cwd=wt, capture_output=True, text=True
    ).stdout.strip()
    assert head == start


def test_escalation_ignores_agent_pin(tmp_path):
    plan = make_plan(
        tmp_path, [{"name": "t1", "role": "implement-small", "kind": "implement", "agent": "claude:haiku"}]
    )
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [_gate_failed(), _gate_failed(), {"status": "ok"}]})
    assert run_plan(plan, state, spawn) == 0
    assert [_role_of(a) for a, _ in spawn.spawns] == ["implement-small", "implement-small", "implement"]
    pins = [a[a.index("--agent") + 1] if "--agent" in a else None for a, _ in spawn.spawns]
    assert pins == ["claude:haiku", "claude:haiku", None]


def test_pin_applies_before_escalation(tmp_path):
    plan = make_plan(
        tmp_path, [{"name": "t1", "role": "implement-small", "kind": "implement", "agent": "claude:haiku"}]
    )
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "ok"}]})
    assert run_plan(plan, state, spawn) == 0
    assert len(spawn.spawns) == 1
    argv, _ = spawn.spawns[0]
    assert argv[argv.index("--agent") + 1] == "claude:haiku"
    assert state.task("t1").escalated_from == ""


def test_exhausted_does_not_escalate(tmp_path):
    plan = _escalating_plan(tmp_path)
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "exhausted"}]})
    assert run_plan(plan, state, spawn) == 1
    assert len(spawn.spawns) == 1
    assert state.task("t1").status == "blocked"
    assert state.task("t1").escalated_from == ""


def test_escalation_chain_two_steps_then_stops(tmp_path):
    plan = _escalating_plan(tmp_path)
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [_gate_failed(), _gate_failed(), _gate_failed(), _gate_failed()]})
    assert run_plan(plan, state, spawn) == 1
    assert [_role_of(a) for a, _ in spawn.spawns] == ["implement-small"] * 2 + ["implement", "implement-hard"]
    tstate = state.task("t1")
    assert tstate.status == "failed"
    assert tstate.escalated_from == "implement"


def test_escalation_cycle_guard(tmp_path):
    config_dir = paths().config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "pool.toml").write_text(
        '[roles.a]\nkind = "implement"\nchain = ["claude:haiku"]\nescalate = "b"\n'
        '[roles.b]\nkind = "implement"\nchain = ["claude:haiku"]\nescalate = "a"\n'
    )
    plan = _escalating_plan(tmp_path, role="a")
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [_gate_failed()] * 10})
    assert run_plan(plan, state, spawn) == 1
    assert [_role_of(a) for a, _ in spawn.spawns] == ["a", "a", "b"]
    assert state.task("t1").status == "failed"


class WritingSpawner(FakeSpawner):
    """Writes one file into each named task's worktree before its fake job finishes, as an agent would."""

    def __init__(self, results, writes):
        super().__init__(results)
        self.writes = writes

    def __call__(self, argv, log_path):
        name = log_path.stem.rsplit("-", 1)[0]
        if name in self.writes:
            workdir = Path(argv[argv.index("-d") + 1])
            (workdir / f"{name}.txt").write_text(f"work of {name}\n")
        return super().__call__(argv, log_path)


def git_out(cwd, *args):
    return subprocess.run(_git_argv(*args), cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_done_task_with_uncommitted_work_is_committed(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}])
    state = load_or_init(plan)
    spawn = WritingSpawner(
        {"t1": [{"status": "ok", "exit_code": 0, "id": "j1", "agent": "codex", "model": "gpt-x"}]}, writes={"t1"}
    )

    rc = run_plan(plan, state, spawn)

    assert rc == 0
    assert state.task("t1").status == "done"
    worktree = Path(state.task("t1").worktree)
    assert git_out(worktree, "status", "--porcelain") == ""
    assert git_out(worktree, "rev-list", "--count", "master..HEAD") == "1"
    assert git_out(worktree, "log", "-1", "--format=%s") == (
        "orch: testslug/t1 — work left uncommitted by codex:gpt-x (gate passed)"
    )
    assert git_out(worktree, "show", "--name-only", "--format=", "HEAD") == "t1.txt"


def test_failed_gate_is_not_committed(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1", "max_attempts": 1}])
    state = load_or_init(plan)
    spawn = WritingSpawner(
        {"t1": [{"status": "gate-failed", "exit_code": 1, "id": "j1", "gate": [{"cmd": "pytest", "rc": 1}]}]},
        writes={"t1"},
    )

    rc = run_plan(plan, state, spawn)

    assert rc == 1
    assert state.task("t1").status == "failed"
    worktree = Path(state.task("t1").worktree)
    assert git_out(worktree, "rev-list", "--count", "master..HEAD") == "0"
    assert "?? t1.txt" in git_out(worktree, "status", "--porcelain")


def test_dependent_starts_from_autocommitted_work(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}, {"name": "t2", "depends_on": ["t1"]}])
    state = load_or_init(plan)
    spawn = WritingSpawner(
        {
            "t1": [{"status": "ok", "exit_code": 0, "id": "j1", "agent": "codex", "model": "gpt-x"}],
            "t2": [{"status": "ok", "exit_code": 0, "id": "j2"}],
        },
        writes={"t1"},
    )

    rc = run_plan(plan, state, spawn)

    assert rc == 0
    assert state.task("t2").start_ref == state.task("t1").branch
    assert (Path(state.task("t2").worktree) / "t1.txt").read_text() == "work of t1\n"


def test_autocommit_falls_back_to_agent_hub_identity(tmp_path, monkeypatch):
    repo_dir = make_repo(tmp_path / "repo")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    git_out(repo_dir, "config", "--unset", "user.name")
    git_out(repo_dir, "config", "--unset", "user.email")
    plan = make_plan(tmp_path, [{"name": "t1"}])
    state = load_or_init(plan)
    spawn = WritingSpawner({"t1": [{"status": "ok", "exit_code": 0, "id": "j1"}]}, writes={"t1"})

    rc = run_plan(plan, state, spawn)

    assert rc == 0
    worktree = Path(state.task("t1").worktree)
    assert git_out(worktree, "log", "-1", "--format=%an <%ae>") == "agent-hub <agent-hub@localhost>"


def _trail_lines(plan):
    return (plan.path.parent / "decisions.tsv").read_text().splitlines()


def test_trail_records_attempt_and_done(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}])
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [{"status": "ok", "id": "j1"}]})

    rc = run_plan(plan, state, spawn)

    assert rc == 0
    lines = _trail_lines(plan)
    assert lines[0] == "time\ttask\tphase\tdecision\treason\tevidence\tresult"
    rows = [line.split("\t") for line in lines[1:]]
    assert all(len(row) == 7 for row in rows)
    assert [row[1:4] for row in rows] == [
        ["t1", "start", "task started"],
        ["t1", "attempt", "attempt 1 ended"],
        ["t1", "final", "task done"],
    ]
    assert rows[1][4:] == ["ok", "j1", ""]
    assert rows[2][6] == "done"


def test_trail_records_retry_and_escalation(tmp_path):
    plan = _escalating_plan(tmp_path)
    state = load_or_init(plan)
    spawn = FakeSpawner({"t1": [_gate_failed("a"), _gate_failed("b"), {"status": "ok", "id": "j3"}]})

    rc = run_plan(plan, state, spawn)

    assert rc == 0
    rows = [line.split("\t") for line in _trail_lines(plan)[1:]]
    assert [row[1:4] for row in rows] == [
        ["t1", "start", "task started"],
        ["t1", "attempt", "attempt 1 ended"],
        ["t1", "retry", "retry"],
        ["t1", "attempt", "attempt 2 ended"],
        ["t1", "escalate", "escalated implement-small -> implement"],
        ["t1", "attempt", "attempt 3 ended"],
        ["t1", "final", "task done"],
    ]
    assert rows[2][4] == "gate-failed"
    assert rows[4][4] == "gate-failed"
    assert rows[4][6] == "pending"


def test_trail_header_written_once_across_resume(tmp_path):
    plan = make_plan(tmp_path, [{"name": "t1"}])
    run_plan(plan, load_or_init(plan), FakeSpawner({"t1": [{"status": "ok", "id": "j1"}]}))

    # resume: the task is pending again and the runner appends to the same trail
    state = load_or_init(plan)
    state.task("t1").status = "pending"
    rc = run_plan(plan, state, FakeSpawner({"t1": [{"status": "ok", "id": "j2"}]}))

    assert rc == 0
    lines = _trail_lines(plan)
    assert lines.count("time\ttask\tphase\tdecision\treason\tevidence\tresult") == 1
    assert [line.split("\t")[3] for line in lines[1:]] == [
        "task started",
        "attempt 1 ended",
        "task done",
        "attempt 2 ended",
        "task done",
    ]
