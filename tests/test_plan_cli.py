import json
import os
import subprocess
import sys
from pathlib import Path

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
