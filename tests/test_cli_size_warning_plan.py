import agenthub.cli
from agenthub.cli import main
from tests.gitrepo import make_repo
from tests.test_plan_cli import make_plan_file

LARGE_BRIEF = """## Goal
do many things
## Input
stuff
## Expected output
1. first thing
2. second thing
3. third thing
4. fourth thing
5. fifth thing
## Constraints
stuff
## Gate
stuff
"""

SMALL_BRIEF = """## Goal
do one thing
## Input
stuff
## Expected output
1. the thing
## Constraints
- Only `agenthub/cli.py` may change.
## Gate
stuff
"""


def make_size_plan(env_sandbox, tmp_path, tasks):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_content = {"goal": "do it", "repo": str(repo), "base": "master", "slug": "plan-1", "tasks": tasks}
    plan_file = make_plan_file(tmp_path, repo, plan_content)
    briefs = plan_file.parent / "briefs"
    briefs.mkdir(exist_ok=True)
    (briefs / "big.md").write_text(LARGE_BRIEF)
    (briefs / "small.md").write_text(SMALL_BRIEF)
    return plan_file


def test_plan_run_warns_per_oversized_task(env_sandbox, tmp_path, monkeypatch, capsys):
    tasks = [
        {"name": "big", "role": "implement", "brief": "briefs/big.md"},
        {"name": "small", "role": "implement", "brief": "briefs/small.md"},
    ]
    plan_file = make_size_plan(env_sandbox, tmp_path, tasks)
    monkeypatch.setattr(agenthub.cli, "run_plan", lambda *a, **k: 0)

    rc = main(["plan", "run", str(plan_file)])

    assert rc == 0
    err = capsys.readouterr().err
    assert "agent-hub: warning: task big: brief size:" in err
    assert err.count("agent-hub: warning: task big: brief size:") == 1


def test_plan_run_small_task_no_warning(env_sandbox, tmp_path, monkeypatch, capsys):
    tasks = [{"name": "small", "role": "implement", "brief": "briefs/small.md"}]
    plan_file = make_size_plan(env_sandbox, tmp_path, tasks)
    monkeypatch.setattr(agenthub.cli, "run_plan", lambda *a, **k: 0)

    rc = main(["plan", "run", str(plan_file)])

    assert rc == 0
    err = capsys.readouterr().err
    assert "task small:" not in err
    assert "brief size" not in err


def test_plan_run_review_task_not_checked(env_sandbox, tmp_path, monkeypatch, capsys):
    tasks = [
        {"name": "small", "role": "implement", "brief": "briefs/small.md"},
        {"name": "rev", "role": "review", "verifies": "small", "depends_on": ["small"], "brief": "briefs/big.md"},
    ]
    plan_file = make_size_plan(env_sandbox, tmp_path, tasks)
    monkeypatch.setattr(agenthub.cli, "run_plan", lambda *a, **k: 0)

    rc = main(["plan", "run", str(plan_file)])

    assert rc == 0
    err = capsys.readouterr().err
    assert "task rev:" not in err
    assert "brief size" not in err
