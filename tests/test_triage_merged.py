import shutil
import subprocess
from pathlib import Path

import agenthub.planclose
import agenthub.triagerun
from agenthub.cli import _merged_or_gone, main
from agenthub.plan import load as load_plan
from agenthub.planclose import CloseResult
from agenthub.planstate import load_or_init
from tests.test_plan_cli import make_gated_plan_run


def test_triage_ignores_merged_tasks(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    plan = load_plan(plan_file)
    repo = Path(plan.repo)
    t1_branch = state["tasks"]["t1"]["branch"]
    t2_branch = state["tasks"]["t2"]["branch"]
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    t2_worktree = Path(state["tasks"]["t2"]["worktree"])

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


def test_close_drops_merged_tasks_from_task_list(env_sandbox, tmp_path, monkeypatch):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    plan = load_plan(plan_file)
    repo = Path(plan.repo)
    t1_branch = state["tasks"]["t1"]["branch"]
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    t2_worktree = Path(state["tasks"]["t2"]["worktree"])

    # Add a commit to t2 so it is ahead of t1
    (t2_worktree / "t2.txt").write_text("t2")
    subprocess.run(["git", "add", "."], cwd=t2_worktree, check=True)
    subprocess.run(["git", "commit", "-m", "t2 commit"], cwd=t2_worktree, check=True)

    # Merge t1 into master (base) and remove worktree
    subprocess.run(["git", "merge", "--ff-only", t1_branch], cwd=repo, check=True)
    shutil.rmtree(t1_worktree)

    recorded = {}

    def fake_close(p, s, *, run, review, triage, ready, max_rounds=3):
        recorded["plan_tasks"] = [t.name for t in p.tasks]
        report_file = plan_file.parent / "report.md"
        report_file.write_text("done")
        return CloseResult(ok=True, report=report_file)

    monkeypatch.setattr(agenthub.planclose, "close", fake_close)

    rc = main(["plan", "close", str(plan_file)])
    assert rc == 0
    assert "t1" not in recorded["plan_tasks"]
    assert "t2" in recorded["plan_tasks"]


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
