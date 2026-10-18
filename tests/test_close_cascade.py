import ast
import subprocess
from pathlib import Path

from agenthub.cascade import cascade
from agenthub.plan import Plan, Task
from agenthub.planclose import CloseResult, close
from agenthub.planstate import load_or_init
from tests.gitrepo import make_repo


def _setup_repo_and_worktrees(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)

    wt_dir = tmp_path / "worktrees"
    wt_dir.mkdir()

    wt_a = wt_dir / "task-a"
    wt_b = wt_dir / "task-b"

    subprocess.run(
        ["git", "worktree", "add", "-b", "orch/testslug/task-a", str(wt_a), "master"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "orch/testslug/task-b", str(wt_b), "master"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    tasks = [
        Task(name="task-a", role="implement"),
        Task(name="task-b", role="implement", depends_on=["task-a"]),
    ]
    plan_file = tmp_path / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    plan = Plan(
        goal="Test cascade",
        repo=repo_dir,
        base="master",
        slug="testslug",
        tasks=tasks,
        path=plan_file,
    )
    state = load_or_init(plan)
    state.task("task-a").status = "done"
    state.task("task-a").branch = "orch/testslug/task-a"
    state.task("task-a").worktree = str(wt_a)

    state.task("task-b").status = "done"
    state.task("task-b").branch = "orch/testslug/task-b"
    state.task("task-b").worktree = str(wt_b)
    state.save()

    return repo_dir, wt_a, wt_b, plan, state


def test_fix_round_cascades_to_dependents(tmp_path: Path):
    repo_dir, wt_a, wt_b, plan, state = _setup_repo_and_worktrees(tmp_path)

    # Initial commit in task-a
    (wt_a / "a.py").write_text("def a(): return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=wt_a, check=True)
    subprocess.run(["git", "commit", "-m", "init a"], cwd=wt_a, check=True)

    # In task-b, merge task-a initial work and add b's work
    subprocess.run(["git", "merge", "--no-edit", "orch/testslug/task-a"], cwd=wt_b, check=True)
    (wt_b / "b.py").write_text("def b(): return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "b.py"], cwd=wt_b, check=True)
    subprocess.run(["git", "commit", "-m", "init b"], cwd=wt_b, check=True)

    run_calls = 0
    review_calls: list[str] = []

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        if run_calls == 2:
            # A gets a fix commit
            (wt_a / "fix.txt").write_text("fix from task-a\n", encoding="utf-8")
            subprocess.run(["git", "add", "fix.txt"], cwd=wt_a, check=True)
            subprocess.run(["git", "commit", "-m", "fix in a"], cwd=wt_a, check=True)
            state.task("task-a").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        review_calls.append(task.name)
        if task.name == "task-b":
            return {"gate": "ok", "review": "approve", "job": f"job-b-{len(review_calls)}", "reason": ""}
        if task.name == "task-a":
            if run_calls == 1:
                return {"gate": "ok", "review": "request-changes", "job": "job-a-1", "reason": "fix needed"}
            return {"gate": "ok", "review": "approve", "job": "job-a-2", "reason": ""}
        raise AssertionError(f"unexpected task {task.name}")

    def fake_triage(task: Task) -> dict | None:
        assert task.name == "task-a"
        tstate = state.task("task-a")
        tstate.status = "pending"
        tstate.fix_rounds += 1
        return {"status": "fix"}

    def fake_ready(task: Task) -> list[str]:
        return []

    res = close(
        plan,
        state,
        run=fake_run,
        review=fake_review,
        triage=fake_triage,
        ready=fake_ready,
        max_rounds=3,
    )

    assert isinstance(res, CloseResult)
    assert res.ok is True
    assert res.stale == {}
    assert (wt_b / "fix.txt").read_text(encoding="utf-8") == "fix from task-a\n"
    assert review_calls.count("task-b") == 2
    assert state.task("task-a").status == "done"
    assert state.task("task-b").status == "done"


def test_cascade_conflict_blocks_dependent(tmp_path: Path):
    repo_dir, wt_a, wt_b, plan, state = _setup_repo_and_worktrees(tmp_path)

    # Initial function in base repo
    (repo_dir / "calc.py").write_text("def compute():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init calc"], cwd=repo_dir, check=True)

    # Update branches with master
    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_a, check=True)
    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_b, check=True)

    # Task B edits compute to return 2
    (wt_b / "calc.py").write_text("def compute():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "b edit"], cwd=wt_b, check=True)

    run_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        if run_calls == 2:
            # Task A edits compute to return 1 (conflict!)
            (wt_a / "calc.py").write_text("def compute():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "a edit"], cwd=wt_a, check=True)
            state.task("task-a").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        if task.name == "task-b":
            return {"gate": "ok", "review": "approve", "job": "job-b", "reason": ""}
        if task.name == "task-a":
            if run_calls == 1:
                return {"gate": "ok", "review": "request-changes", "job": "job-a-1", "reason": "fix needed"}
            return {"gate": "ok", "review": "approve", "job": "job-a-2", "reason": ""}
        raise AssertionError(f"unexpected task {task.name}")

    def fake_triage(task: Task) -> dict | None:
        assert task.name == "task-a"
        tstate = state.task("task-a")
        tstate.status = "pending"
        tstate.fix_rounds += 1
        return {"status": "fix"}

    def fake_ready(task: Task) -> list[str]:
        return []

    res = close(
        plan,
        state,
        run=fake_run,
        review=fake_review,
        triage=fake_triage,
        ready=fake_ready,
        max_rounds=3,
    )

    assert isinstance(res, CloseResult)
    assert res.ok is False
    assert state.task("task-b").status == "blocked"
    assert state.task("task-b").reason == "cascade conflict from task-a: calc.py"
    # Verify worktree b is left in a clean state (merge aborted)
    clean_check = subprocess.run(["git", "status", "--porcelain"], cwd=wt_b, capture_output=True, text=True)
    assert clean_check.stdout.strip() == ""


def test_cascade_merges_disjoint_definitions(tmp_path: Path):
    repo_dir, wt_a, wt_b, plan, state = _setup_repo_and_worktrees(tmp_path)

    (repo_dir / "mod.py").write_text("def base():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_a, check=True)
    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_b, check=True)

    # Task A appends func_a
    (wt_a / "mod.py").write_text(
        "def base():\n    return 0\n\n\ndef func_a():\n    return 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "commit", "-am", "a adds func_a"], cwd=wt_a, check=True)

    # Task B appends func_b
    (wt_b / "mod.py").write_text(
        "def base():\n    return 0\n\n\ndef func_b():\n    return 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "commit", "-am", "b adds func_b"], cwd=wt_b, check=True)

    outcome = cascade(plan, state, "task-a")
    assert outcome == {"task-b": "merged"}

    merged_content = (wt_b / "mod.py").read_text(encoding="utf-8")
    assert "def base():" in merged_content
    assert "def func_a():" in merged_content
    assert "def func_b():" in merged_content

    # Validate AST
    tree = ast.parse(merged_content)
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert "base" in names
    assert "func_a" in names
    assert "func_b" in names

    # Verify worktree b is clean and merge commit was made
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt_b, capture_output=True, text=True)
    assert status.stdout.strip() == ""
