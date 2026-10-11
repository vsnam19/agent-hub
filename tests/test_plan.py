import json

import pytest

from agenthub.plan import PlanError, load
from tests.gitrepo import make_repo


def test_load_valid_plan_defaults(tmp_path):
    repo_dir = make_repo(tmp_path / "repo")

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    brief_path = plan_dir / "briefs" / "core.md"
    brief_path.parent.mkdir()
    brief_path.write_text("my brief")

    plan_file = plan_dir / "plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "goal": "do it",
                "repo": str(repo_dir),
                "base": "master",
                "slug": "do-it",
                "tasks": [{"name": "core", "role": "implement", "brief": "briefs/core.md"}],
            }
        )
    )

    plan = load(plan_file)
    assert plan.goal == "do it"
    assert plan.repo == repo_dir.resolve()
    assert plan.base == "master"
    assert plan.slug == "do-it"
    assert plan.max_parallel == 2
    assert len(plan.tasks) == 1
    t = plan.tasks[0]
    assert t.name == "core"
    assert t.role == "implement"
    assert t.brief == brief_path
    assert t.max_attempts == 2


def test_load_reports_all_problems(tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "goal": "a",
                "repo": "/does/not/exist",
                "base": "nonexistent",
                "slug": "bad_slug!",
                "tasks": [{"name": "bad_name!", "role": "magic"}],
            }
        )
    )

    with pytest.raises(PlanError) as exc:
        load(plan_file)

    msg = str(exc.value)
    assert "slug must be [a-z0-9-]+" in msg
    assert "repo is not an existing git work tree" in msg
    assert "task 0: name must be [a-z0-9-]+" in msg
    assert "unknown role 'magic'" in msg


def test_load_rejects_cycle(tmp_path):
    repo_dir = make_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    plan_file = plan_dir / "plan.json"
    (plan_dir / "a.md").write_text("a")
    (plan_dir / "b.md").write_text("b")

    plan_file.write_text(
        json.dumps(
            {
                "goal": "cycle",
                "repo": str(repo_dir),
                "base": "master",
                "slug": "cycle",
                "tasks": [
                    {"name": "t1", "role": "implement", "brief": "a.md", "depends_on": ["t2"]},
                    {"name": "t2", "role": "implement", "brief": "b.md", "depends_on": ["t1"]},
                ],
            }
        )
    )

    with pytest.raises(PlanError) as exc:
        load(plan_file)
    assert "cycle detected" in str(exc.value)


def test_load_review_needs_verifies(tmp_path):
    repo_dir = make_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    plan_file = plan_dir / "plan.json"

    plan_file.write_text(
        json.dumps(
            {
                "goal": "rev",
                "repo": str(repo_dir),
                "base": "master",
                "slug": "rev",
                "tasks": [{"name": "r1", "role": "review"}],
            }
        )
    )

    with pytest.raises(PlanError) as exc:
        load(plan_file)
    assert "review needs verifies" in str(exc.value)


def test_load_brief_relative_to_plan_dir(tmp_path):
    repo_dir = make_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "b.md").write_text("brief")
    plan_file = plan_dir / "plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "goal": "x",
                "repo": str(repo_dir),
                "base": "master",
                "slug": "x",
                "tasks": [{"name": "t1", "role": "implement", "brief": "b.md"}],
            }
        )
    )

    plan = load(plan_file)
    assert plan.tasks[0].brief == plan_dir / "b.md"


def test_load_bad_slug_and_names(tmp_path):
    repo_dir = make_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "b.md").write_text("brief")
    plan_file = plan_dir / "plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "goal": "x",
                "repo": str(repo_dir),
                "base": "master",
                "slug": "inv_alid",
                "tasks": [{"name": "bad_name!", "role": "implement", "brief": "b.md"}],
            }
        )
    )
    with pytest.raises(PlanError) as exc:
        load(plan_file)
    assert "slug must be [a-z0-9-]+" in str(exc.value)
    assert "task 0: name must be [a-z0-9-]+" in str(exc.value)


def test_plan_task_custom_role(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text('[roles.quick]\nkind = "implement"\nchain = ["claude:haiku"]\n')
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))
    repo_dir = make_repo(tmp_path / "repo")
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "brief.md").write_text("brief")
    plan_file = plan_dir / "plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "goal": "custom role",
                "repo": str(repo_dir),
                "base": "master",
                "slug": "custom-role",
                "tasks": [{"name": "fast", "role": "quick", "brief": "brief.md"}],
            }
        )
    )

    task = load(plan_file).tasks[0]
    assert task.role == "quick"
    assert task.kind == "implement"
