from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agenthub.audit.writer import audit_root
from agenthub.cli import main
from agenthub.config import paths
from tests.test_plan_cli import fake_review, make_gated_plan_run


def _read_all_events(state_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    root = audit_root(state_dir)
    for p in sorted(root.glob("*/*.jsonl")):
        if p.name.endswith(".lock"):
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def test_merge_emits_plan_merge_event(
    env_sandbox: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t2_file = Path(state["tasks"]["t2"]["worktree"]) / "t2.txt"
    t2_file.write_text("t2 work\n")
    subprocess.run(["git", "add", "t2.txt"], cwd=t2_file.parent, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "t2 work"], cwd=t2_file.parent, check=True)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()

    rc = main(["plan", "merge", str(plan_file)])
    assert rc == 0

    events = [
        e
        for e in _read_all_events(paths().state_dir)
        if e.get("kind") == "plan" and e.get("detail", {}).get("event") == "merge"
    ]
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "plan"
    assert ev["actor"]["role"] == "agent-hub"
    assert ev["actor"]["host"] == "agent-hub"
    assert ev["parent"] == {"plan": plan_file.parent.name}
    assert ev["target"] == "master"
    assert ev["outcome"]["status"] == "ok"
    assert ev["outcome"]["exit_code"] == 0
    assert ev["detail"]["tasks"] == ["t1", "t2"]


def test_failed_merge_event_has_error_status(
    env_sandbox: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()

    # Commit after review makes t1 unreviewed and ungated -> merge stops at t1
    t1_worktree = state["tasks"]["t1"]["worktree"]
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "after review"], cwd=t1_worktree, check=True)

    rc = main(["plan", "merge", str(plan_file)])
    assert rc == 1

    events = [
        e
        for e in _read_all_events(paths().state_dir)
        if e.get("kind") == "plan" and e.get("detail", {}).get("event") == "merge"
    ]
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "plan"
    assert ev["actor"]["role"] == "agent-hub"
    assert ev["parent"] == {"plan": plan_file.parent.name}
    assert ev["target"] == "master"
    assert ev["outcome"]["status"] == "error"
    assert ev["outcome"]["exit_code"] == 1
    assert ev["detail"]["tasks"] == []


def test_emit_failure_keeps_merge_result(
    env_sandbox: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t2_file = Path(state["tasks"]["t2"]["worktree"]) / "t2.txt"
    t2_file.write_text("t2 work\n")
    subprocess.run(["git", "add", "t2.txt"], cwd=t2_file.parent, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "t2 work"], cwd=t2_file.parent, check=True)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()

    def _broken_emit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated emit failure")

    monkeypatch.setattr("agenthub.cli.emit", _broken_emit)

    rc = main(["plan", "merge", str(plan_file)])
    assert rc == 0

    lines = capsys.readouterr().out.splitlines()
    assert [line.split(" at ")[0] for line in lines] == ["t1 merged into master", "t2 merged into master"]

    repo = tmp_path / "repo"
    assert (repo / "agent_wrote.txt").is_file()
    assert (repo / "t2.txt").is_file()
