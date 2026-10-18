import agenthub.planclose
from agenthub.cli import main
from agenthub.planclose import CloseResult
from tests.gitrepo import make_repo
from tests.test_plan_cli import make_plan_file


def test_close_prints_report_path_and_exits_zero(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    report_file = plan_file.parent / "report.md"

    def fake_close(plan, state, *, run, review, triage, ready, max_rounds=3):
        return CloseResult(ok=True, report=report_file)

    monkeypatch.setattr(agenthub.planclose, "close", fake_close)

    rc = main(["plan", "close", str(plan_file)])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == str(report_file)


def test_close_exits_one_when_not_ok(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    report_file = plan_file.parent / "report.md"

    def fake_close(plan, state, *, run, review, triage, ready, max_rounds=3):
        return CloseResult(ok=False, report=report_file)

    monkeypatch.setattr(agenthub.planclose, "close", fake_close)

    rc = main(["plan", "close", str(plan_file)])

    out = capsys.readouterr().out
    assert rc == 1
    assert out.strip() == str(report_file)


def test_close_passes_rounds(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    report_file = plan_file.parent / "report.md"

    recorded = {}

    def fake_close(plan, state, *, run, review, triage, ready, max_rounds=3):
        recorded["plan"] = plan
        recorded["state"] = state
        recorded["run"] = run
        recorded["review"] = review
        recorded["triage"] = triage
        recorded["ready"] = ready
        recorded["max_rounds"] = max_rounds
        return CloseResult(ok=True, report=report_file)

    monkeypatch.setattr(agenthub.planclose, "close", fake_close)

    rc = main(["plan", "close", str(plan_file), "--rounds", "5"])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == str(report_file)
    assert recorded["max_rounds"] == 5
    assert callable(recorded["run"])
    assert callable(recorded["review"])
    assert callable(recorded["triage"])
    assert callable(recorded["ready"])
    assert recorded["state"].runner is not None
    assert (plan_file.parent / "runner.pid").is_file()

    # Also verify default rounds is 3
    rc_default = main(["plan", "close", str(plan_file)])
    assert rc_default == 0
    assert recorded["max_rounds"] == 3


def test_close_rejects_zero_rounds(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)

    rc = main(["plan", "close", str(plan_file), "--rounds", "0"])

    err = capsys.readouterr().err
    assert rc == 2
    assert "rounds" in err


def test_close_ready_missing_worktree_reports_problem(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    report_file = plan_file.parent / "report.md"

    recorded = {}

    def fake_close(plan, state, *, run, review, triage, ready, max_rounds=3):
        recorded["plan"] = plan
        recorded["state"] = state
        recorded["ready"] = ready
        return CloseResult(ok=True, report=report_file)

    monkeypatch.setattr(agenthub.planclose, "close", fake_close)

    rc = main(["plan", "close", str(plan_file)])
    assert rc == 0

    plan = recorded["plan"]
    state = recorded["state"]
    ready = recorded["ready"]
    task = plan.tasks[0]
    tstate = state.task(task.name)

    # Missing work tree directory
    missing_path = tmp_path / "missing-wt"
    tstate.worktree = str(missing_path)
    problems = ready(task)
    assert problems == [f"work tree missing: {missing_path}"]

    # Directory exists but cannot read HEAD
    unreadable_dir = tmp_path / "unreadable-wt"
    unreadable_dir.mkdir()
    tstate.worktree = str(unreadable_dir)
    problems = ready(task)
    assert problems == [f"cannot read HEAD: {unreadable_dir}"]


def test_close_triage_missing_worktree_blocks(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    report_file = plan_file.parent / "report.md"

    recorded = {}

    def fake_close(plan, state, *, run, review, triage, ready, max_rounds=3):
        recorded["plan"] = plan
        recorded["state"] = state
        recorded["triage"] = triage
        return CloseResult(ok=True, report=report_file)

    monkeypatch.setattr(agenthub.planclose, "close", fake_close)

    rc = main(["plan", "close", str(plan_file)])
    assert rc == 0

    plan = recorded["plan"]
    state = recorded["state"]
    triage = recorded["triage"]
    task = plan.tasks[0]
    tstate = state.task(task.name)

    # Missing work tree directory
    missing_path = tmp_path / "missing-wt"
    tstate.worktree = str(missing_path)
    res = triage(task)
    assert tstate.status == "blocked"
    assert tstate.reason == f"work tree missing: {missing_path}"
    assert res == {
        "task": task.name,
        "status": "blocked",
        "counts": {"fix": 0, "reject": 0, "blocked": 0},
        "fix_path": "",
        "rejected_path": "",
        "reason": f"work tree missing: {missing_path}",
    }

    # Directory exists but cannot read HEAD
    unreadable_dir = tmp_path / "unreadable-wt"
    unreadable_dir.mkdir()
    tstate.worktree = str(unreadable_dir)
    res = triage(task)
    assert tstate.status == "blocked"
    assert tstate.reason == f"cannot read HEAD: {unreadable_dir}"
    assert res == {
        "task": task.name,
        "status": "blocked",
        "counts": {"fix": 0, "reject": 0, "blocked": 0},
        "fix_path": "",
        "rejected_path": "",
        "reason": f"cannot read HEAD: {unreadable_dir}",
    }


def test_close_rejects_unknown_skill(env_sandbox, tmp_path, capsys):
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

    rc = main(["plan", "close", str(plan_file)])

    err = capsys.readouterr().err
    assert rc == 2
    assert "task t1: unknown skill nope" in err
    assert not (plan_file.parent / "runner.pid").exists()


def test_close_passes_max_parallel(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    report_file = plan_file.parent / "report.md"

    run_plan_args = []

    def fake_run_plan(plan, state, spawn, max_parallel=None):
        run_plan_args.append(max_parallel)
        return 0

    monkeypatch.setattr(agenthub.cli, "run_plan", fake_run_plan)

    def fake_close(plan, state, *, run, review, triage, ready, max_rounds=3):
        run()
        return CloseResult(ok=True, report=report_file)

    monkeypatch.setattr(agenthub.planclose, "close", fake_close)

    rc = main(["plan", "close", str(plan_file), "--max-parallel", "4"])
    assert rc == 0
    assert run_plan_args == [4]

    run_plan_args.clear()
    rc_default = main(["plan", "close", str(plan_file)])
    assert rc_default == 0
    assert run_plan_args == [None]
