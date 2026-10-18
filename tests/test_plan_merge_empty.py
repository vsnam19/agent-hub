import subprocess
from pathlib import Path

from agenthub.cli import main
from agenthub.mcpserver import plan_merge
from agenthub.plan import load as load_plan
from agenthub.planstate import load_or_init
from tests.gitrepo import make_repo
from tests.test_plan_cli import fake_review, make_gated_plan_run, make_plan_file


def test_plan_merge_no_done_tasks(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo, branch=None)
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["test -f README.md"]\n')
    (repo / ".gitignore").write_text(".worktrees/\n")
    subprocess.run(["git", "add", ".agent-hub", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "repo gates"], cwd=repo, check=True)
    plan_file = make_plan_file(tmp_path, repo)
    plan = load_plan(plan_file)
    load_or_init(plan)

    res = plan_merge(str(plan_file))
    assert res == {"merged": [], "stopped_at": None, "error": "no done tasks to merge"}
    assert res["error"] == "no done tasks to merge"


def test_plan_merge_all_merged_has_no_error(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t2_file = Path(state["tasks"]["t2"]["worktree"]) / "t2.txt"
    t2_file.write_text("t2 work\n")
    subprocess.run(["git", "add", "t2.txt"], cwd=t2_file.parent, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "t2 work"], cwd=t2_file.parent, check=True)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()

    res1 = plan_merge(str(plan_file))
    assert res1["merged"] == ["t1", "t2"]
    assert res1["stopped_at"] is None
    assert "error" not in res1

    res2 = plan_merge(str(plan_file))
    assert res2["merged"] == ["t1", "t2"]
    assert res2["stopped_at"] is None
    assert "error" not in res2
