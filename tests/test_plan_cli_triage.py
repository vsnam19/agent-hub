import json
import os
import shutil
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


def test_triage_skips_missing_worktree(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    shutil.rmtree(t1_worktree)

    calls = []

    def fake_triage(plan, task, tstate, jobs, head, now):
        calls.append(task.name)
        return {
            "task": task.name,
            "status": "fix",
            "counts": {"fix": 1, "reject": 0, "blocked": 0},
            "fix_path": "/path/to/fixes/t2-1.md",
            "rejected_path": "",
            "reason": "",
        }

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])

    out = capsys.readouterr().out
    assert rc == 0
    assert f"t1 skipped: work tree missing: {t1_worktree}" in out
    assert calls == ["t2"]
    assert "t2 fix=1 reject=0 blocked=0 brief=/path/to/fixes/t2-1.md" in out

    # When all worktrees are missing, both are skipped and nothing to triage is printed
    t2_worktree = Path(state["tasks"]["t2"]["worktree"])
    shutil.rmtree(t2_worktree)
    calls.clear()
    rc2 = main(["plan", "triage", str(plan_file)])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert f"t1 skipped: work tree missing: {t1_worktree}" in out2
    assert f"t2 skipped: work tree missing: {t2_worktree}" in out2
    assert "nothing to triage" in out2
    assert calls == []


def test_triage_refuses_live_runner(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    state_file = plan_file.parent / "state.json"
    data = json.loads(state_file.read_text())
    other_pid = os.getpid() + 1000
    data["runner"] = {"pid": other_pid, "start_time": 12345}
    state_file.write_text(json.dumps(data))

    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: 12345 if pid == other_pid else None)

    calls = []
    monkeypatch.setattr(agenthub.triagerun, "triage_task", lambda *args, **kwargs: calls.append(1))

    rc = main(["plan", "triage", str(plan_file)])
    assert rc == 2
    err = capsys.readouterr().err
    assert f"agent-hub: plan runner {other_pid} is still running; wait or cancel it first" in err
    assert len(calls) == 0

    saved_state = json.loads(state_file.read_text())
    assert saved_state["runner"] == {"pid": other_pid, "start_time": 12345}


def test_triage_claims_runner(env_sandbox, tmp_path, monkeypatch):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    state_file = plan_file.parent / "state.json"

    data = json.loads(state_file.read_text())
    data["runner"] = {"pid": 99999, "start_time": 12345}
    state_file.write_text(json.dumps(data))

    recorded_pid = os.getpid()
    recorded_time = 77777
    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: recorded_time if pid == recorded_pid else None)

    monkeypatch.setattr(agenthub.triagerun, "triage_task", lambda *args, **kwargs: None)

    rc = main(["plan", "triage", str(plan_file)])
    assert rc == 0

    saved_state = json.loads(state_file.read_text())
    assert saved_state["runner"] == {"pid": recorded_pid, "start_time": recorded_time}
    runner_pid_file = plan_file.parent / "runner.pid"
    assert runner_pid_file.is_file()
    assert json.loads(runner_pid_file.read_text()) == {"pid": recorded_pid, "start_time": recorded_time}

    # If the recorded runner is already this process, triage succeeds and claims it again
    rc2 = main(["plan", "triage", str(plan_file)])
    assert rc2 == 0
