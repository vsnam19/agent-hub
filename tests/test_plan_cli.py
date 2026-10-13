import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agenthub.planrun
from agenthub.cli import main
from agenthub.config import paths
from agenthub.jobs import list_jobs, new_job
from agenthub.procinfo import start_time
from tests.gitrepo import make_repo


def make_plan_file(tmp_path, repo_dir, content=None):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    plan_file = plan_dir / "plan.json"
    if content is None:
        content = {
            "goal": "do it",
            "repo": str(repo_dir),
            "base": "master",
            "slug": "plan-1",
            "tasks": [
                {"name": "t1", "role": "implement", "brief": "b1.md", "gate": ["test -f agent_wrote.txt"]},
                {"name": "t2", "role": "implement", "brief": "b2.md", "depends_on": ["t1"]},
            ],
        }
    plan_file.write_text(json.dumps(content))
    (plan_dir / "b1.md").write_text("""## Input
stuff
## Expected output
stuff
""")
    (plan_dir / "b2.md").write_text("""## Input
stuff
## Expected output
stuff
""")
    return plan_file


def run_cli_plan(env_sandbox, plan_file, cmd, extra_args=None, extra_env=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["AGENT_HUB_PLAN_POLL_S"] = "0.1"
    if extra_env:
        env.update(extra_env)
    argv = [sys.executable, "-m", "agenthub", "plan", cmd, str(plan_file)]
    if extra_args:
        argv.extend(extra_args)
    return subprocess.run(argv, env=env, capture_output=True, text=True)


def test_plan_run_two_tasks_end_to_end(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)

    # We want both tasks to succeed with handoff scenario.
    env = {"FAKE_CLAUDE_SCENARIO": "handoff", "FAKE_AGY_SCENARIO": "handoff", "FAKE_CODEX_SCENARIO": "handoff"}

    res = run_cli_plan(env_sandbox, plan_file, "run", extra_env=env)
    print((plan_file.parent / "state.json").read_text())
    assert res.returncode == 0

    # Check that state.json has both done
    state_file = plan_file.parent / "state.json"
    assert state_file.is_file()
    state = json.loads(state_file.read_text())
    assert state["tasks"]["t1"]["status"] == "done"
    assert state["tasks"]["t2"]["status"] == "done"

    # Branches exist
    assert subprocess.run(["git", "show-ref", "refs/heads/orch/plan-1/t1"], cwd=repo).returncode == 0
    assert subprocess.run(["git", "show-ref", "refs/heads/orch/plan-1/t2"], cwd=repo).returncode == 0

    # Handoff files written
    assert (plan_file.parent / "handoffs" / "t1.md").is_file()
    assert (plan_file.parent / "handoffs" / "t2.md").is_file()


def test_plan_status_output_and_json(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    env = {"FAKE_CLAUDE_SCENARIO": "handoff", "FAKE_AGY_SCENARIO": "handoff", "FAKE_CODEX_SCENARIO": "handoff"}
    run_cli_plan(env_sandbox, plan_file, "run", extra_env=env)

    res = run_cli_plan(env_sandbox, plan_file, "status")
    print((plan_file.parent / "state.json").read_text())
    assert res.returncode == 0
    assert "t1: done" in res.stdout
    assert "branch=orch/plan-1/t1" in res.stdout
    assert "handoff=success" in res.stdout

    res_json = run_cli_plan(env_sandbox, plan_file, "status", extra_args=["--json"])
    assert res_json.returncode == 0
    data = json.loads(res_json.stdout)
    assert data["tasks"]["t1"]["status"] == "done"


def test_plan_wait_finished(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    env = {"FAKE_CLAUDE_SCENARIO": "handoff", "FAKE_AGY_SCENARIO": "handoff", "FAKE_CODEX_SCENARIO": "handoff"}
    run_cli_plan(env_sandbox, plan_file, "run", extra_env=env)

    res = run_cli_plan(env_sandbox, plan_file, "wait")
    print((plan_file.parent / "state.json").read_text())
    assert res.returncode == 0


def test_plan_wait_timeout_124(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)

    # Start the plan in background using popen
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["AGENT_HUB_PLAN_POLL_S"] = "0.1"
    env["FAKE_CLAUDE_SCENARIO"] = "slow"
    env["FAKE_AGY_SCENARIO"] = "slow"
    env["FAKE_CODEX_SCENARIO"] = "slow"

    proc = subprocess.Popen([sys.executable, "-m", "agenthub", "plan", "run", str(plan_file)], env=env)

    # wait with small timeout
    res = run_cli_plan(env_sandbox, plan_file, "wait", extra_args=["--timeout", "1s"])
    assert res.returncode == 124

    proc.terminate()
    proc.wait()


def test_plan_wait_no_state_exit_2(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["AGENT_HUB_PLAN_POLL_S"] = "0.1"
    # Overwrite PLAN_WAIT_APPEAR_S to be fast for the test
    env["AGENT_HUB_PLAN_WAIT_APPEAR_S"] = "0.1"
    res = run_cli_plan(env_sandbox, plan_file, "wait")
    assert res.returncode == 2


def test_plan_invalid_exit_2(env_sandbox, tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text("{}")
    res = run_cli_plan(env_sandbox, plan_file, "run")
    assert res.returncode == 2


def test_plan_review_task_end_to_end(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_content = {
        "goal": "do it",
        "repo": str(repo),
        "base": "master",
        "slug": "plan-1",
        "tasks": [
            {"name": "t1", "role": "implement", "brief": "b1.md"},
            {"name": "t2", "role": "review", "verifies": "t1"},
        ],
    }
    plan_file = make_plan_file(tmp_path, repo, plan_content)
    env = {"FAKE_CLAUDE_SCENARIO": "handoff", "FAKE_AGY_SCENARIO": "handoff", "FAKE_CODEX_SCENARIO": "handoff"}
    res = run_cli_plan(env_sandbox, plan_file, "run", extra_env=env)
    print((plan_file.parent / "state.json").read_text())
    assert res.returncode == 0


def test_plan_status_trail_prints(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    env = {"FAKE_CLAUDE_SCENARIO": "handoff", "FAKE_AGY_SCENARIO": "handoff", "FAKE_CODEX_SCENARIO": "handoff"}
    run_cli_plan(env_sandbox, plan_file, "run", extra_env=env)

    res = run_cli_plan(env_sandbox, plan_file, "status", extra_args=["--trail"])

    assert res.returncode == 0
    assert "\t" not in res.stdout
    lines = res.stdout.splitlines()
    assert lines[0].split()[:4] == ["time", "task", "phase", "decision"]
    assert any(line.split()[1:2] == ["t1"] and "task started" in line for line in lines[1:])
    assert any(line.split()[1:2] == ["t2"] and "task done" in line for line in lines[1:])
    # columns line up: the task names sit at the header's "task" offset in every row
    task_column = lines[0].index("task")
    assert {line[task_column:].split()[0] for line in lines[1:]} == {"t1", "t2"}


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)


def _write_state(plan_file, runner, tasks):
    state = {
        "slug": "plan-1",
        "started": "2026-10-12T10:00:00+00:00",
        "updated": "2026-10-12T10:00:00+00:00",
        "runner": runner,
        "tasks": tasks,
    }
    (plan_file.parent / "state.json").write_text(json.dumps(state))


def test_plan_cancel_stops_runner_and_jobs(env_sandbox, tmp_path, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    worktree = tmp_path / "wt-t1"
    worktree.mkdir()
    runner = _sleeper()
    job_proc = _sleeper()
    try:
        job = new_job(paths(), "implement", datetime.now(timezone.utc))
        record = {"id": job.id, "role": "implement", "status": "running", "workdir": str(worktree)}
        job.write_json("result.json", record)
        (job.dir / "pid").write_text(json.dumps({"pid": job_proc.pid, "start_time": start_time(job_proc.pid)}))
        _write_state(
            plan_file,
            {"pid": runner.pid, "start_time": start_time(runner.pid)},
            {
                "t1": {
                    "status": "running",
                    "attempts": [{"job": "", "status": "", "exit_code": -1}],
                    "worktree": str(worktree),
                },
                "t2": {"status": "pending", "attempts": []},
            },
        )

        assert main(["plan", "cancel", str(plan_file), "-d", str(repo)]) == 0

        assert runner.wait(timeout=10) == -15
        assert job_proc.wait(timeout=10) == -15
        state = json.loads((plan_file.parent / "state.json").read_text())
        assert state["tasks"]["t1"]["status"] == "cancelled"
        assert state["tasks"]["t2"]["status"] == "pending"
    finally:
        for proc in (runner, job_proc):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert "sent SIGTERM to runner" in capsys.readouterr().out


def test_plan_cancel_foreign_repo_refused_exit_3(env_sandbox, tmp_path, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    other = tmp_path / "other"
    make_repo(other)
    plan_file = make_plan_file(tmp_path, repo)
    runner = _sleeper()
    try:
        _write_state(plan_file, {"pid": runner.pid, "start_time": start_time(runner.pid)}, {})

        assert main(["plan", "cancel", str(plan_file), "-d", str(other)]) == 3

        assert runner.poll() is None
        assert f"plan plan-1 belongs to {repo.resolve()}; run plan cancel from that repository" in (
            capsys.readouterr().err
        )
    finally:
        runner.kill()
        runner.wait()


def test_plan_cancel_wrong_pid_start_time_not_signalled(env_sandbox, tmp_path, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    runner = _sleeper()
    try:
        _write_state(plan_file, {"pid": runner.pid, "start_time": start_time(runner.pid) + 1}, {})

        assert main(["plan", "cancel", str(plan_file), "-d", str(repo)]) == 0

        assert runner.poll() is None
        assert "runner not verified; nothing signalled" in capsys.readouterr().out
    finally:
        runner.kill()
        runner.wait()


def test_plan_cancel_outside_repo_exit_3(env_sandbox, tmp_path, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    runner = _sleeper()
    try:
        _write_state(plan_file, {"pid": runner.pid, "start_time": start_time(runner.pid)}, {})

        assert main(["plan", "cancel", str(plan_file), "-d", str(outside)]) == 3

        assert runner.poll() is None
        assert f"plan plan-1 belongs to {repo.resolve()}; run plan cancel from that repository" in (
            capsys.readouterr().err
        )
    finally:
        runner.kill()
        runner.wait()


def test_plan_cancel_missing_plan_repo(env_sandbox, tmp_path, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    missing = repo.resolve()
    shutil.rmtree(repo)
    runner = _sleeper()
    try:
        _write_state(plan_file, {"pid": runner.pid, "start_time": start_time(runner.pid)}, {})

        assert main(["plan", "cancel", str(plan_file), "-d", str(tmp_path)]) == 3

        assert runner.wait(timeout=10) == -15
        assert f"plan repository missing: {missing}" in capsys.readouterr().err
    finally:
        runner.kill()
        runner.wait()


def test_plan_cancel_corrupt_state(env_sandbox, tmp_path, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    state_file = plan_file.parent / "state.json"
    runner = _sleeper()
    try:
        (plan_file.parent / "runner.pid").write_text(
            json.dumps({"pid": runner.pid, "start_time": start_time(runner.pid)})
        )
        state_file.write_text('{"runner": ')

        assert main(["plan", "cancel", str(plan_file), "-d", str(repo)]) == 2

        assert runner.wait(timeout=10) == -15
        captured = capsys.readouterr()
        assert f"plan state unreadable: {state_file}" in captured.err
        assert f"sent SIGTERM to runner {runner.pid}" in captured.out
    finally:
        runner.kill()
        runner.wait()


def test_plan_run_rejects_unknown_skill_before_running(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_content = {
        "goal": "do it",
        "repo": str(repo),
        "base": "master",
        "slug": "plan-1",
        "tasks": [
            {"name": "t1", "role": "implement", "brief": "b1.md"},
            {"name": "t2", "role": "implement", "brief": "b2.md", "skills": ["nope"]},
        ],
    }
    plan_file = make_plan_file(tmp_path, repo, plan_content)
    res = run_cli_plan(env_sandbox, plan_file, "run")
    assert res.returncode == 2
    assert "task t2: unknown skill nope" in res.stderr
    assert not (plan_file.parent / "state.json").exists()
    assert list_jobs(paths(), 100) == []


def test_plan_unknown_skill_lists_available(env_sandbox, tmp_path):
    worker = tmp_path / "worker-plugin"
    (worker / "skills" / "debug-playbook").mkdir(parents=True)
    (worker / "skills" / "debug-playbook" / "SKILL.md").write_text("# debug\n")
    telaf = tmp_path / "telaf"
    (telaf / "skills" / "telaf").mkdir(parents=True)
    (telaf / "skills" / "telaf" / "SKILL.md").write_text("# telaf\n")
    (env_sandbox.config / "workers.toml").write_text(
        f'default = []\n[plugins]\nworker = "{worker}"\ntelaf = "{telaf}"\n'
    )
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_content = {
        "goal": "do it",
        "repo": str(repo),
        "base": "master",
        "slug": "plan-1",
        "tasks": [{"name": "t1", "role": "implement", "brief": "b1.md", "skills": ["nope"]}],
    }
    plan_file = make_plan_file(tmp_path, repo, plan_content)
    res = run_cli_plan(env_sandbox, plan_file, "run")
    assert res.returncode == 2
    assert "task t1: unknown skill nope: available skills are debug-playbook, telaf" in res.stderr
    assert not (plan_file.parent / "state.json").exists()


def make_gated_plan_run(env_sandbox, tmp_path):
    """Run a two-task plan with fake agents in a repo whose gates are committed on the plan base."""
    repo = tmp_path / "repo"
    make_repo(repo, branch=None)
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["test -f README.md"]\n')
    # worktrees live under .worktrees/, which the repo ignores, so the main checkout stays clean
    (repo / ".gitignore").write_text(".worktrees/\n")
    subprocess.run(["git", "add", ".agent-hub", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "repo gates"], cwd=repo, check=True)
    plan_file = make_plan_file(tmp_path, repo)
    env = {"FAKE_CLAUDE_SCENARIO": "handoff", "FAKE_AGY_SCENARIO": "handoff", "FAKE_CODEX_SCENARIO": "handoff"}
    run_cli_plan(env_sandbox, plan_file, "run", extra_env=env)
    state = json.loads((plan_file.parent / "state.json").read_text())
    assert state["tasks"]["t1"]["status"] == "done"
    assert state["tasks"]["t2"]["status"] == "done"
    return plan_file, state


def fake_review(monkeypatch, verdict):
    """Stand in for the review agent: record each review as a job whose verdict is `verdict`."""
    calls = []

    def run(args, p, pool, now):
        calls.append(args)
        job = new_job(p, "review", now())
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=args.workdir, capture_output=True, text=True, check=True
        ).stdout.strip()
        record = {
            "id": job.id,
            "role": "review",
            "kind": "review",
            "status": "ok",
            "verdict": verdict,
            "head": head,
            "workdir": str(args.workdir),
            "agent": "reviewer-agent",
            "model": "reviewer-model",
            "error": "",
        }
        job.write_json("result.json", record)
        return record

    monkeypatch.setattr(agenthub.planrun, "run_job", run)
    return calls


def test_plan_review_gates_and_reviews_done_tasks(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    calls = fake_review(monkeypatch, "approve")

    rc = main(["plan", "review", str(plan_file)])

    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert [line.split(" job=")[0] for line in lines] == ["t1 gate=ok review=approve", "t2 gate=ok review=approve"]
    gates = [j for j in list_jobs(paths(), 100) if j["kind"] == "gate"]
    assert len(gates) == 2
    assert [c.base for c in calls] == ["master", "orch/plan-1/t1"]
    assert [c.workdir.resolve() for c in calls] == [Path(state["tasks"][t]["worktree"]).resolve() for t in ("t1", "t2")]
    # The implementer of t1 is excluded from its review
    implement_job = state["tasks"]["t1"]["attempts"][-1]["job"]
    implementer = json.loads((paths().jobs_dir / implement_job / "result.json").read_text())
    assert calls[0].exclude == [f"{implementer['agent']}:{implementer['model']}"]


def test_plan_review_skips_approved_head(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)
    calls = fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()
    gate_jobs = len([j for j in list_jobs(paths(), 100) if j["kind"] == "gate"])

    rc = main(["plan", "review", str(plan_file)])

    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert [line.split(" (")[0] for line in lines] == ["t1 up to date", "t2 up to date"]
    assert len(calls) == 2
    assert len([j for j in list_jobs(paths(), 100) if j["kind"] == "gate"]) == gate_jobs


def test_plan_review_dirty_tree_never_up_to_date(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()

    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    (t1_worktree / "dirty.txt").write_text("uncommitted\n")

    rc = main(["plan", "review", str(plan_file), "--task", "t1"])

    out = capsys.readouterr()
    assert rc == 1
    assert "t1 gate=failed review=failed job=-" in out.out
    assert "work tree is not clean; commit before review" in out.err


def test_plan_review_self_review_not_up_to_date(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    t1_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=t1_worktree, capture_output=True, text=True, check=True
    ).stdout.strip()
    implement_job = state["tasks"]["t1"]["attempts"][-1]["job"]
    implementer = json.loads((paths().jobs_dir / implement_job / "result.json").read_text())

    job = new_job(paths(), "review", datetime.now(timezone.utc))
    job.write_json(
        "result.json",
        {
            "id": job.id,
            "role": "review",
            "kind": "review",
            "status": "ok",
            "verdict": "approve",
            "head": t1_head,
            "workdir": str(t1_worktree),
            "agent": implementer["agent"],
            "model": implementer["model"],
            "error": "",
        },
    )

    calls = fake_review(monkeypatch, "approve")
    rc = main(["plan", "review", str(plan_file), "--task", "t1"])

    out = capsys.readouterr()
    assert rc == 0
    assert "t1 up to date" not in out.out
    assert len(calls) == 1
    assert calls[0].exclude == [f"{implementer['agent']}:{implementer['model']}"]


def test_plan_review_ungated_approval_not_up_to_date(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    (t1_worktree / "ungated.txt").write_text("ungated\n")
    subprocess.run(["git", "add", "ungated.txt"], cwd=t1_worktree, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ungated work"], cwd=t1_worktree, check=True)
    new_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=t1_worktree, capture_output=True, text=True, check=True
    ).stdout.strip()

    job = new_job(paths(), "review", datetime.now(timezone.utc))
    job.write_json(
        "result.json",
        {
            "id": job.id,
            "role": "review",
            "kind": "review",
            "status": "ok",
            "verdict": "approve",
            "head": new_head,
            "workdir": str(t1_worktree),
            "agent": "reviewer-agent",
            "model": "reviewer-model",
            "error": "",
        },
    )

    calls = fake_review(monkeypatch, "approve")
    gate_jobs_before = len([j for j in list_jobs(paths(), 100) if j["kind"] == "gate"])

    rc = main(["plan", "review", str(plan_file), "--task", "t1"])

    out = capsys.readouterr()
    assert rc == 0
    assert "t1 up to date" not in out.out
    gate_jobs_after = [j for j in list_jobs(paths(), 100) if j["kind"] == "gate"]
    assert len(gate_jobs_after) == gate_jobs_before + 1
    assert gate_jobs_after[0]["head"] == new_head
    assert len(calls) == 1


def test_plan_review_newer_request_changes_wins(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    t1_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=t1_worktree, capture_output=True, text=True, check=True
    ).stdout.strip()

    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file), "--task", "t1"]) == 0
    capsys.readouterr()

    when = datetime.now(timezone.utc) + timedelta(seconds=10)
    job = new_job(paths(), "review", when)
    job.write_json(
        "result.json",
        {
            "id": job.id,
            "role": "review",
            "kind": "review",
            "status": "ok",
            "verdict": "request-changes",
            "head": t1_head,
            "workdir": str(t1_worktree),
            "agent": "reviewer-agent",
            "model": "reviewer-model",
            "error": "fix needed",
        },
    )

    calls = fake_review(monkeypatch, "request-changes")
    rc = main(["plan", "review", str(plan_file), "--task", "t1"])

    out = capsys.readouterr()
    assert rc == 1
    assert "t1 up to date" not in out.out
    assert len(calls) == 1
    assert "t1 gate=ok review=request-changes" in out.out


def _merge_subjects(repo):
    return subprocess.run(
        ["git", "log", "--merges", "--format=%s", "master"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.splitlines()


def test_plan_merge_merges_ready_tasks_in_order(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    # t2 adds its own commit, so each task has a head of its own to merge
    t2_file = Path(state["tasks"]["t2"]["worktree"]) / "t2.txt"
    t2_file.write_text("t2 work\n")
    subprocess.run(["git", "add", "t2.txt"], cwd=t2_file.parent, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "t2 work"], cwd=t2_file.parent, check=True)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()
    repo = tmp_path / "repo"

    rc = main(["plan", "merge", str(plan_file)])

    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert [line.split(" at ")[0] for line in lines] == ["t1 merged into master", "t2 merged into master"]
    # t2 is stacked on t1, so its merge comes second: the newest merge subject is t2's
    assert _merge_subjects(repo) == [
        "Merge orch/plan-1/t2 — merged through agent-hub ready (approve, reviewer-agent:reviewer-model)",
        "Merge orch/plan-1/t1 — merged through agent-hub ready (approve, reviewer-agent:reviewer-model)",
    ]
    assert (repo / "agent_wrote.txt").is_file()
    assert (repo / "t2.txt").is_file()
    for name in ("t1", "t2"):
        assert not Path(state["tasks"][name]["worktree"]).exists()
        assert subprocess.run(["git", "show-ref", f"refs/heads/orch/plan-1/{name}"], cwd=repo).returncode != 0


def test_plan_merge_stops_at_not_ready(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()
    repo = tmp_path / "repo"
    # A commit after review leaves t1's HEAD unreviewed and ungated, so it is not ready
    t1_worktree = state["tasks"]["t1"]["worktree"]
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "after review"], cwd=t1_worktree, check=True)

    rc = main(["plan", "merge", str(plan_file)])

    out = capsys.readouterr().out
    assert rc == 1
    assert "t1 not ready: no passing gate on " in out
    assert "t2" not in out
    assert _merge_subjects(repo) == []
    assert Path(t1_worktree).is_dir()
    assert subprocess.run(["git", "show-ref", "refs/heads/orch/plan-1/t2"], cwd=repo).returncode == 0


def test_plan_merge_refuses_dirty_checkout(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()
    repo = tmp_path / "repo"
    head_before = subprocess.run(
        ["git", "rev-parse", "master"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "stray.txt").write_text("uncommitted\n")

    rc = main(["plan", "merge", str(plan_file)])

    err = capsys.readouterr().err
    assert rc == 3
    assert "main checkout is not clean" in err
    assert (
        subprocess.run(
            ["git", "rev-parse", "master"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        == head_before
    )
    assert _merge_subjects(repo) == []
    assert subprocess.run(["git", "show-ref", "refs/heads/orch/plan-1/t1"], cwd=repo).returncode == 0
