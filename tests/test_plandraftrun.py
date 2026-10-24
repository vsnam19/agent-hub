import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenthub import tasksize
from agenthub.plandraft import DraftError
from agenthub.plandraftrun import draft

NOW = lambda: datetime(2026, 10, 24, tzinfo=timezone.utc)  # noqa: E731

SMALL = "\n\n".join(
    [
        "## Goal\nAdd a thing.",
        "## Input\n`agenthub/a.py`",
        "## Expected output\n1. Change `agenthub/a.py`.",
        "## Constraints\n- Only `agenthub/a.py` changes.",
        "## Gate\n`true`",
    ]
)
LARGE = SMALL.replace(
    "1. Change `agenthub/a.py`.",
    "1. Change `agenthub/a.py`.\n2. Add a.\n3. Add b.\n4. Add c.\n5. Add d.",
)


def _tasks(*briefs: tuple[str, str]) -> str:
    tasks = [{"name": name, "role": "implement", "depends_on": [], "brief": brief} for name, brief in briefs]
    data = {"goal": "ship it", "tasks": tasks}
    return f"Here is the plan.\n\n```json\n{json.dumps(data)}\n```\n"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".agent-hub").mkdir(parents=True)
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["true"]\n')
    return repo


def _spec(tmp_path: Path) -> Path:
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n")
    return spec


def _fake(tmp_path: Path, replies: list[str], calls: list | None = None):
    def run(args, p, pool, now):
        if calls is not None:
            calls.append(args)
        if not replies:
            return {"status": "ok"}
        reply = tmp_path / f"reply-{len(calls or [])}.md"
        reply.write_text(replies.pop(0))
        return {"status": "ok", "reply_path": str(reply)}

    return run


def _status_only(status: str):
    def run(args, p, pool, now):
        return {"status": status}

    return run


def test_draft_passes_first_time(tmp_path):
    assert tasksize.check(SMALL) == []
    repo = _repo(tmp_path)
    calls = []
    res = draft(
        _spec(tmp_path),
        "s",
        "main",
        repo,
        run=_fake(tmp_path, [_tasks(("a", SMALL), ("b", SMALL))], calls),
        now=NOW,
        state_dir=tmp_path,
    )
    assert len(calls) == 1
    assert res["resplit"] is False
    assert res["tasks"] == ["a", "b"]
    plan_path = Path(res["plan_path"])
    assert plan_path == tmp_path / "plans" / "s" / "plan.json"
    assert plan_path.is_file()
    assert (tmp_path / "plans" / "s" / "briefs" / "a.md").is_file()


def test_draft_resplits_once(tmp_path):
    assert tasksize.check(LARGE)
    repo = _repo(tmp_path)
    calls = []
    res = draft(
        _spec(tmp_path),
        "s",
        "main",
        repo,
        run=_fake(tmp_path, [_tasks(("a", LARGE)), _tasks(("a", SMALL))], calls),
        now=NOW,
        state_dir=tmp_path,
    )
    assert len(calls) == 2
    assert "## Re-split" in calls[1].brief
    assert f"{tasksize.check(LARGE)[0]}" in calls[1].brief
    assert res["resplit"] is True
    assert res["tasks"] == ["a"]
    assert Path(res["plan_path"]).is_file()


def test_draft_gives_up_after_one_resplit(tmp_path):
    repo = _repo(tmp_path)
    calls = []
    with pytest.raises(DraftError) as err:
        draft(
            _spec(tmp_path),
            "s",
            "main",
            repo,
            run=_fake(tmp_path, [_tasks(("a", LARGE)), _tasks(("a", LARGE))], calls),
            now=NOW,
            state_dir=tmp_path,
        )
    assert len(calls) == 2
    assert f"a: {tasksize.check(LARGE)[0]}" in str(err.value)
    assert not (tmp_path / "plans" / "s" / "plan.json").exists()


def test_draft_job_not_ok(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(DraftError, match="failed"):
        draft(_spec(tmp_path), "s", "main", repo, run=_status_only("failed"), now=NOW, state_dir=tmp_path)
    assert not (tmp_path / "plans" / "s" / "plan.json").exists()


def test_draft_needs_repo_gates(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    with pytest.raises(DraftError, match="no repo gates"):
        draft(_spec(tmp_path), "s", "main", repo, run=_fake(tmp_path, []), now=NOW, state_dir=tmp_path)
