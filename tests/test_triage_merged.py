import shutil
import subprocess
from pathlib import Path

import agenthub.planclose
import agenthub.triagerun
from agenthub.cli import _merged_or_gone, main
from agenthub.plan import load as load_plan
from agenthub.planstate import TaskState, load_or_init
from tests.gitrepo import make_repo
from tests.test_plan_cli import fake_review, make_gated_plan_run, make_plan_file


def test_triage_ignores_merged_tasks(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    plan = load_plan(plan_file)
    repo = Path(plan.repo)
    t1_branch = state["tasks"]["t1"]["branch"]
    t2_branch = state["tasks"]["t2"]["branch"]
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    t2_worktree = Path(state["tasks"]["t2"]["worktree"])

    # Add a commit to t2 so it has its own commit
    (t2_worktree / "t2.txt").write_text("t2")
    subprocess.run(["git", "add", "."], cwd=t2_worktree, check=True)
    subprocess.run(["git", "commit", "-m", "t2 commit"], cwd=t2_worktree, check=True)

    # Merge t1 and t2 branches into master (plan.base)
    subprocess.run(["git", "merge", "--ff-only", t1_branch], cwd=repo, check=True)
    subprocess.run(["git", "merge", "--ff-only", t2_branch], cwd=repo, check=True)

    # Remove the work trees
    shutil.rmtree(t1_worktree)
    shutil.rmtree(t2_worktree)

    rc = main(["plan", "triage", str(plan_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipped" not in out
    assert "nothing to triage" in out


def test_triage_ignores_deleted_branch(env_sandbox, tmp_path, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    plan = load_plan(plan_file)
    repo = Path(plan.repo)
    t1_branch = state["tasks"]["t1"]["branch"]
    t2_branch = state["tasks"]["t2"]["branch"]
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    t2_worktree = Path(state["tasks"]["t2"]["worktree"])

    # Remove the work trees first, prune git worktree metadata, then delete branches
    shutil.rmtree(t1_worktree)
    shutil.rmtree(t2_worktree)
    subprocess.run(["git", "worktree", "prune"], cwd=repo, check=True)

    subprocess.run(["git", "branch", "-D", t1_branch], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-D", t2_branch], cwd=repo, check=True)

    rc = main(["plan", "triage", str(plan_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipped" not in out
    assert "nothing to triage" in out


def test_triage_still_skips_unmerged_missing_worktree(env_sandbox, tmp_path, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    t2_worktree = Path(state["tasks"]["t2"]["worktree"])

    # Branches exist and are not merged, but worktrees are removed
    shutil.rmtree(t1_worktree)
    shutil.rmtree(t2_worktree)

    rc = main(["plan", "triage", str(plan_file)])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"t1 skipped: work tree missing: {t1_worktree}" in out
    assert f"t2 skipped: work tree missing: {t2_worktree}" in out
    assert "skipped 2 task(s)" in out


def test_triage_ignores_merged_task_while_triaging_active(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    plan = load_plan(plan_file)
    repo = Path(plan.repo)
    t1_branch = state["tasks"]["t1"]["branch"]
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    t2_worktree = Path(state["tasks"]["t2"]["worktree"])

    # Add a commit to t2 so it is ahead of t1 and not merged when t1 merges
    (t2_worktree / "t2.txt").write_text("t2")
    subprocess.run(["git", "add", "."], cwd=t2_worktree, check=True)
    subprocess.run(["git", "commit", "-m", "t2 commit"], cwd=t2_worktree, check=True)

    # Merge only t1 into base and delete its worktree; leave t2 active
    subprocess.run(["git", "merge", "--ff-only", t1_branch], cwd=repo, check=True)
    shutil.rmtree(t1_worktree)

    calls = []

    def fake_triage(plan, task, tstate, jobs, head, now):
        calls.append(task.name)
        return {
            "task": task.name,
            "status": "fix",
            "counts": {"fix": 1, "reject": 0, "blocked": 0},
            "fix_path": "/path/to/fix.md",
            "rejected_path": "",
            "reason": "",
        }

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipped" not in out
    assert calls == ["t2"]
    assert "t2 fix=1 reject=0 blocked=0" in out
    assert "t1" not in out


def test_close_keeps_merged_tasks_in_plan(env_sandbox, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "handoff")
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "handoff")
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "handoff")
    fake_review(monkeypatch, "approve")

    repo = tmp_path / "repo"
    make_repo(repo, branch=None)
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["test -f README.md"]\n')
    (repo / ".gitignore").write_text(".worktrees/\n")
    subprocess.run(["git", "add", ".agent-hub", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "repo gates"], cwd=repo, check=True)

    plan_file = make_plan_file(tmp_path, repo)
    plan = load_plan(plan_file)

    # Create t1 branch with a commit and merge it into master
    t1_branch = "orch/plan-1/t1"
    subprocess.run(["git", "checkout", "-b", t1_branch], cwd=repo, check=True)
    (repo / "t1.txt").write_text("t1")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "t1 commit"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "master"], cwd=repo, check=True)
    subprocess.run(["git", "merge", "--ff-only", t1_branch], cwd=repo, check=True)

    # Setup state: t1 is done (and merged), t2 is pending (depending on t1)
    pstate = load_or_init(plan)
    pstate.tasks["t1"].status = "done"
    pstate.tasks["t1"].branch = t1_branch
    pstate.tasks["t1"].start_ref = "master"
    pstate.tasks["t2"].status = "pending"
    pstate.save()

    rc = main(["plan", "close", str(plan_file)])
    assert rc == 0
    report_file = plan_file.parent / "report.md"
    assert report_file.is_file()
    report_content = report_file.read_text()
    assert "t1" in report_content
    assert "t2" in report_content


def test_merged_or_gone_false_on_git_error(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo, branch=None)
    plan_file = make_plan_file(tmp_path, repo)
    plan = load_plan(plan_file)
    shutil.rmtree(repo)
    tstate = TaskState(status="done", branch="feature", start_ref="master")
    assert _merged_or_gone(plan, tstate) is False


def test_branch_without_own_commits_not_merged(env_sandbox, tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo, branch=None)
    plan_file = make_plan_file(tmp_path, repo)
    plan = load_plan(plan_file)

    # t1 is merged into master
    t1_branch = "orch/plan-1/t1"
    subprocess.run(["git", "checkout", "-b", t1_branch], cwd=repo, check=True)
    (repo / "t1.txt").write_text("t1")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "t1 commit"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "master"], cwd=repo, check=True)
    subprocess.run(["git", "merge", "--ff-only", t1_branch], cwd=repo, check=True)

    # t2 was started from t1 (start_ref = t1_branch) but made no commits of its own
    t2_branch = "orch/plan-1/t2"
    subprocess.run(["git", "branch", t2_branch, t1_branch], cwd=repo, check=True)
    t2_state = TaskState(status="done", branch=t2_branch, start_ref=t1_branch)

    # t2 tip is an ancestor of plan.base (master) because t1 is in master,
    # but t2 has no commits of its own (tip == start_ref).
    assert _merged_or_gone(plan, t2_state) is False


def test_merged_or_gone_helper(env_sandbox, tmp_path):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    plan = load_plan(plan_file)
    repo = Path(plan.repo)
    pstate = load_or_init(plan)
    t1_state = pstate.task("t1")
    t2_state = pstate.task("t2")
    t2_worktree = Path(t2_state.worktree)

    # Add a commit to t2 so it is ahead of t1
    (t2_worktree / "t2.txt").write_text("t2")
    subprocess.run(["git", "add", "."], cwd=t2_worktree, check=True)
    subprocess.run(["git", "commit", "-m", "t2 commit"], cwd=t2_worktree, check=True)

    # Before merge or deletion: both branches exist and are unmerged
    assert _merged_or_gone(plan, t1_state) is False
    assert _merged_or_gone(plan, t2_state) is False

    # After merging t1 and removing its worktree:
    subprocess.run(["git", "merge", "--ff-only", t1_state.branch], cwd=repo, check=True)
    t1_worktree = Path(t1_state.worktree)
    shutil.rmtree(t1_worktree)
    assert _merged_or_gone(plan, t1_state) is True
    assert _merged_or_gone(plan, t2_state) is False

    # After deleting t2 branch (prune worktree first):
    shutil.rmtree(t2_worktree)
    subprocess.run(["git", "worktree", "prune"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-D", t2_state.branch], cwd=repo, check=True)
    assert _merged_or_gone(plan, t2_state) is True

    # Empty branch or task without branch:
    t1_state.branch = ""
    assert _merged_or_gone(plan, t1_state) is False
