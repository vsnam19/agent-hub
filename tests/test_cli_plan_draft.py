import json

import agenthub.plandraftrun
from agenthub.cli import main
from agenthub.plandraft import DraftError
from tests.gitrepo import make_repo

SMALL_BRIEF = """## Goal
Add a thing.

## Input
`agenthub/a.py`

## Expected output
1. Change `agenthub/a.py`.

## Constraints
- Only `agenthub/a.py` changes.

## Gate
`true`
"""


def _write_plan(plan_dir, repo):
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "b1.md").write_text(SMALL_BRIEF)
    (plan_dir / "b2.md").write_text(SMALL_BRIEF)
    plan_file = plan_dir / "plan.json"
    data = {
        "goal": "ship it",
        "repo": str(repo),
        "base": "master",
        "slug": "draft-1",
        "tasks": [
            {"name": "t1", "role": "implement", "brief": "b1.md"},
            {"name": "t2", "role": "implement", "brief": "b2.md", "depends_on": ["t1"]},
        ],
    }
    plan_file.write_text(json.dumps(data))
    return plan_file


def test_plan_draft_prints_table(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path / "repo")
    plan_file = _write_plan(tmp_path / "plans" / "draft-1", repo)
    calls = []

    def fake_draft(spec, slug, base, repo_root, **kwargs):
        calls.append((spec, slug, base, repo_root))
        return {"plan_path": str(plan_file), "resplit": False, "tasks": ["t1", "t2"]}

    monkeypatch.setattr(agenthub.plandraftrun, "draft", fake_draft)
    rc = main(["plan", "draft", "--spec", "spec.md", "--slug", "draft-1", "-d", str(repo)])
    out = capsys.readouterr().out

    assert rc == 0
    assert calls and calls[0][1:3] == ("draft-1", "master")
    assert "re-split: no" in out
    assert "t1  implement  -  1  1  0" in out
    assert "t2  implement  t1  1  1  0" in out


def test_plan_draft_error_exits_1(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path / "repo")

    def fake_draft(spec, slug, base, repo_root, **kwargs):
        raise DraftError("too large")

    monkeypatch.setattr(agenthub.plandraftrun, "draft", fake_draft)
    rc = main(["plan", "draft", "--spec", "spec.md", "--slug", "draft-1", "-d", str(repo)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "too large" in captured.err
    assert captured.out == ""
