import json
import subprocess
from pathlib import Path

import agenthub.triagerun
from agenthub.cli import _head_of, main
from agenthub.plan import load as load_plan
from agenthub.planstate import load_or_init
from tests.test_plan_cli import make_gated_plan_run


def test_triage_prints_fix_line_and_exits_zero(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    t1_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=t1_worktree, capture_output=True, text=True, check=True
    ).stdout.strip()

    p = load_plan(plan_file)
    s = load_or_init(p)
    assert _head_of(s.task("t1")) == t1_head

    calls = []

    def fake_triage(plan, task, tstate, jobs, head, now):
        calls.append((task.name, head, jobs))
        if task.name == "t1":
            return {
                "task": "t1",
                "status": "fix",
                "counts": {"fix": 2, "reject": 1, "blocked": 0},
                "fix_path": "/path/to/fixes/t1-1.md",
                "rejected_path": "/path/to/fixes/t1-1-rejected.md",
                "reason": "",
            }
        return {
            "task": "t2",
            "status": "rejected-only",
            "counts": {"fix": 0, "reject": 3, "blocked": 0},
            "fix_path": "",
            "rejected_path": "/path/to/fixes/t2-1-rejected.md",
            "reason": "",
        }

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "t1 fix=2 reject=1 blocked=0 brief=/path/to/fixes/t1-1.md" in out
    assert "t2 fix=0 reject=3 blocked=0" in out
    t2_line = [line for line in out.splitlines() if line.startswith("t2")][0]
    assert "brief=" not in t2_line
    assert "nothing to triage" not in out
    assert len(calls) == 2
    assert calls[0][0] == "t1"
    assert calls[0][1] == t1_head
    assert isinstance(calls[0][2], list)


def test_triage_blocked_exits_one(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    def fake_triage(plan, task, tstate, jobs, head, now):
        if task.name == "t1":
            return {
                "task": "t1",
                "status": "fix",
                "counts": {"fix": 1, "reject": 0, "blocked": 0},
                "fix_path": "/path/to/fixes/t1-1.md",
                "rejected_path": "",
                "reason": "",
            }
        return {
            "task": "t2",
            "status": "blocked",
            "counts": {"fix": 0, "reject": 0, "blocked": 0},
            "fix_path": "",
            "rejected_path": "",
            "reason": "trade-off needs director",
        }

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])

    out = capsys.readouterr().out
    assert rc == 1
    assert "t1 fix=1 reject=0 blocked=0 brief=/path/to/fixes/t1-1.md" in out
    assert "t2 blocked: trade-off needs director" in out


def test_triage_nothing_to_do_exits_zero(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    calls = []

    def fake_triage(plan, task, tstate, jobs, head, now):
        calls.append(task.name)
        return None

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to triage" in out
    assert len(calls) == 2

    # A task with no worktree is skipped without calling triage_task
    state_file = plan_file.parent / "state.json"
    data = json.loads(state_file.read_text())
    data["tasks"]["t1"]["worktree"] = ""
    state_file.write_text(json.dumps(data))

    calls.clear()
    rc2 = main(["plan", "triage", str(plan_file)])
    assert rc2 == 0
    assert calls == ["t2"]


def test_triage_saves_state(env_sandbox, tmp_path, monkeypatch):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    saved_during_t2 = None

    def fake_triage(plan, task, tstate, jobs, head, now):
        nonlocal saved_during_t2
        if task.name == "t1":
            tstate.status = "pending"
            tstate.reason = "fix instructions"
            tstate.fix_rounds = 1
            return {
                "task": "t1",
                "status": "fix",
                "counts": {"fix": 1, "reject": 0, "blocked": 0},
                "fix_path": "/path/to/fixes/t1-1.md",
                "rejected_path": "",
                "reason": "",
            }
        elif task.name == "t2":
            # Verify state was already saved to disk after t1
            disk_state = json.loads((plan_file.parent / "state.json").read_text())
            saved_during_t2 = disk_state["tasks"]["t1"]["status"]
            return None
        return None

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])
    assert rc == 0
    assert saved_during_t2 == "pending"

    saved_state = json.loads((plan_file.parent / "state.json").read_text())
    assert saved_state["tasks"]["t1"]["status"] == "pending"
    assert saved_state["tasks"]["t1"]["reason"] == "fix instructions"
    assert saved_state["tasks"]["t1"]["fix_rounds"] == 1
