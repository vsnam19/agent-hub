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

    base_code = (
        "def top():\n"
        "    return 'original_top'\n\n\n"
        "def spacer_1():\n"
        "    pass\n\n\n"
        "def spacer_2():\n"
        "    pass\n\n\n"
        "def spacer_3():\n"
        "    pass\n\n\n"
        "def spacer_4():\n"
        "    pass\n\n\n"
        "def spacer_5():\n"
        "    pass\n\n\n"
        "def spacer_6():\n"
        "    pass\n\n\n"
        "def bottom():\n"
        "    return 'original_bottom'\n"
    )
    (repo_dir / "mod.py").write_text(base_code, encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_a, check=True)
    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_b, check=True)

    # Task A edits top function
    a_code = base_code.replace("return 'original_top'", "return 'top_from_a'")
    (wt_a / "mod.py").write_text(a_code, encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "a edits top"], cwd=wt_a, check=True)

    # Task B edits bottom function (far apart)
    b_code = base_code.replace("return 'original_bottom'", "return 'bottom_from_b'")
    (wt_b / "mod.py").write_text(b_code, encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "b edits bottom"], cwd=wt_b, check=True)

    outcome = cascade(plan, state, "task-a")
    assert outcome == {"task-b": "merged"}

    merged_content = (wt_b / "mod.py").read_text(encoding="utf-8")
    assert "top_from_a" in merged_content
    assert "bottom_from_b" in merged_content

    # Validate AST
    tree = ast.parse(merged_content)
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert "top" in names
    assert "bottom" in names

    # Verify worktree b is clean and merge commit was made
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt_b, capture_output=True, text=True)
    assert status.stdout.strip() == ""


def test_cascade_conflict_leaves_no_merge_in_progress(tmp_path: Path):
    repo_dir, wt_a, wt_b, plan, state = _setup_repo_and_worktrees(tmp_path)

    (repo_dir / "calc.py").write_text("def compute():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init calc"], cwd=repo_dir, check=True)

    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_a, check=True)
    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_b, check=True)

    # Conflicting edits to the same function
    (wt_b / "calc.py").write_text("def compute():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "b edit"], cwd=wt_b, check=True)

    (wt_a / "calc.py").write_text("def compute():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "a edit"], cwd=wt_a, check=True)

    outcome = cascade(plan, state, "task-a")
    assert outcome == {"task-b": "conflict: calc.py"}

    # No MERGE_HEAD
    merge_head = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=wt_b,
        capture_output=True,
    )
    assert merge_head.returncode != 0

    # Clean work tree
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt_b, capture_output=True, text=True)
    assert status.stdout.strip() == ""


def test_cascade_conflict_empty_conflicted_list_returns_unknown(tmp_path: Path, monkeypatch):
    repo_dir, wt_a, wt_b, plan, state = _setup_repo_and_worktrees(tmp_path)

    (repo_dir / "calc.py").write_text("def compute():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init calc"], cwd=repo_dir, check=True)

    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_a, check=True)
    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_b, check=True)

    (wt_b / "calc.py").write_text("def compute():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "b edit"], cwd=wt_b, check=True)

    (wt_a / "calc.py").write_text("def compute():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "a edit"], cwd=wt_a, check=True)

    orig_run = subprocess.run

    def fake_subprocess_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any("--diff-filter=U" in str(arg) for arg in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = cascade(plan, state, "task-a")
    assert outcome == {"task-b": "conflict: unknown"}

    # No MERGE_HEAD
    merge_head = orig_run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=wt_b,
        capture_output=True,
    )
    assert merge_head.returncode != 0

    # Clean work tree
    status = orig_run(["git", "status", "--porcelain"], cwd=wt_b, capture_output=True, text=True)
    assert status.stdout.strip() == ""


def test_cascade_reports_git_error_without_commit(tmp_path: Path):
    repo_dir, wt_a, wt_b, plan, state = _setup_repo_and_worktrees(tmp_path)

    (repo_dir / "file.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init file"], cwd=repo_dir, check=True)

    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_a, check=True)
    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_b, check=True)

    (wt_a / "file.txt").write_text("fix from a\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "a fix"], cwd=wt_a, check=True)

    # Dependent worktree b has an uncommitted change to the file the fix touches
    (wt_b / "file.txt").write_text("dirty in b\n", encoding="utf-8")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt_b, capture_output=True, text=True, check=True
    ).stdout.strip()

    outcome = cascade(plan, state, "task-a")
    assert "task-b" in outcome
    assert outcome["task-b"].startswith("error: ")
    assert "overwritten by merge" in outcome["task-b"]

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt_b, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_after == head_before

    # Verify no MERGE_HEAD
    merge_head = subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=wt_b, capture_output=True)
    assert merge_head.returncode != 0


def test_cascade_error_blocks_close(tmp_path: Path):
    repo_dir, wt_a, wt_b, plan, state = _setup_repo_and_worktrees(tmp_path)

    (repo_dir / "mod.py").write_text("def run(): return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_a, check=True)
    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_b, check=True)

    # Task b has uncommitted changes in mod.py so cascade merge will fail with git error
    (wt_b / "mod.py").write_text("def run(): return 'uncommitted'\n", encoding="utf-8")

    run_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        if run_calls == 2:
            (wt_a / "mod.py").write_text("def run(): return 1\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "fix in a"], cwd=wt_a, check=True)
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
    assert state.task("task-b").reason.startswith("cascade failed from task-a: ")


def test_cascade_blocks_close_when_non_implement_task_blocked(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)

    wt_dir = tmp_path / "worktrees"
    wt_dir.mkdir()

    wt_a = wt_dir / "task-a"
    wt_g = wt_dir / "gate-task"

    subprocess.run(
        ["git", "worktree", "add", "-b", "orch/testslug/task-a", str(wt_a), "master"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "orch/testslug/gate-task", str(wt_g), "master"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    tasks = [
        Task(name="task-a", role="implement"),
        Task(name="gate-task", role="gate", depends_on=["task-a"]),
    ]
    plan_file = tmp_path / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    plan = Plan(
        goal="Test non-implement cascade blocked",
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

    state.task("gate-task").status = "blocked"
    state.task("gate-task").reason = "cascade failed from task-a: merge error"
    state.task("gate-task").branch = "orch/testslug/gate-task"
    state.task("gate-task").worktree = str(wt_g)
    state.save()

    (repo_dir / "mod.py").write_text("def run(): return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init mod"], cwd=repo_dir, check=True)

    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_a, check=True)
    subprocess.run(["git", "merge", "--no-edit", "master"], cwd=wt_g, check=True)

    run_calls = 0

    def fake_run() -> int:
        nonlocal run_calls
        run_calls += 1
        if run_calls == 2:
            (wt_a / "mod.py").write_text("def run(): return 1\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "fix in a"], cwd=wt_a, check=True)
            state.task("task-a").status = "done"
        return 0

    def fake_review(task: Task) -> dict:
        assert task.name == "task-a"
        if run_calls == 1:
            return {"gate": "ok", "review": "request-changes", "job": "job-a-1", "reason": "fix needed"}
        return {"gate": "ok", "review": "approve", "job": "job-a-2", "reason": ""}

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
    # Even though task-a is approved and gate-task is not in implement_tasks,
    # gate-task being blocked by cascade MUST cause res.ok to be False!
    assert res.ok is False
    assert state.task("gate-task").status == "blocked"


def test_cascade_skips_non_implement_dependents(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    make_repo(repo_dir, default="master", branch=None)

    wt_dir = tmp_path / "worktrees"
    wt_dir.mkdir()

    tasks = [
        Task(name="task-a", role="implement"),
        Task(name="task-impl", role="implement", depends_on=["task-a"]),
        Task(name="task-gate", role="gate", depends_on=["task-a"]),
        Task(name="task-review", role="review", depends_on=["task-a"]),
        Task(name="task-kind-impl", role="custom", kind="implement", depends_on=["task-a"]),
        Task(name="task-kind-gate", role="implement", kind="gate", depends_on=["task-a"]),
    ]

    for t in tasks:
        wt = wt_dir / t.name
        subprocess.run(
            ["git", "worktree", "add", "-b", f"orch/testslug/{t.name}", str(wt), "master"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

    plan_file = tmp_path / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    plan = Plan(
        goal="Test cascade skips non-implement",
        repo=repo_dir,
        base="master",
        slug="testslug",
        tasks=tasks,
        path=plan_file,
    )
    state = load_or_init(plan)
    for t in tasks:
        state.task(t.name).status = "done"
        state.task(t.name).branch = f"orch/testslug/{t.name}"
        state.task(t.name).worktree = str(wt_dir / t.name)
    state.save()

    wt_a = wt_dir / "task-a"
    (wt_a / "a.txt").write_text("from a\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=wt_a, check=True)
    subprocess.run(["git", "commit", "-m", "commit in a"], cwd=wt_a, check=True)

    outcome = cascade(plan, state, "task-a")
    assert "task-impl" in outcome
    assert outcome["task-impl"] == "merged"
    assert "task-kind-impl" in outcome
    assert outcome["task-kind-impl"] == "merged"
    assert "task-gate" not in outcome
    assert "task-review" not in outcome
    assert "task-kind-gate" not in outcome
    assert (wt_dir / "task-impl" / "a.txt").read_text(encoding="utf-8") == "from a\n"
    assert (wt_dir / "task-kind-impl" / "a.txt").read_text(encoding="utf-8") == "from a\n"
    assert not (wt_dir / "task-gate" / "a.txt").exists()
    assert not (wt_dir / "task-review" / "a.txt").exists()
    assert not (wt_dir / "task-kind-gate" / "a.txt").exists()
