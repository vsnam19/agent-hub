import fcntl
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

import agenthub.triagerun
from agenthub.cli import UsageError, _claim_runner, _live_runner, _task_head, main
from agenthub.guards import GuardError, inspect
from agenthub.plan import load as load_plan
from agenthub.planstate import load_or_init
from tests.gitrepo import make_repo
from tests.test_plan_cli import make_gated_plan_run, make_plan_file


def test_triage_prints_fix_line_and_exits_zero(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    t1_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=t1_worktree, capture_output=True, text=True, check=True
    ).stdout.strip()

    p = load_plan(plan_file)
    s = load_or_init(p)
    assert _task_head(s.task("t1")) == (t1_head, None)

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
    assert rc == 1
    assert f"t1 skipped: work tree missing: {t1_worktree}" in out
    assert calls == ["t2"]
    assert "t2 fix=1 reject=0 blocked=0 brief=/path/to/fixes/t2-1.md" in out
    assert "skipped 1 task(s)" in out

    # When all worktrees are missing, both are skipped and exit code is 1
    t2_worktree = Path(state["tasks"]["t2"]["worktree"])
    shutil.rmtree(t2_worktree)
    calls.clear()
    rc2 = main(["plan", "triage", str(plan_file)])
    out2 = capsys.readouterr().out
    assert rc2 == 1
    assert f"t1 skipped: work tree missing: {t1_worktree}" in out2
    assert f"t2 skipped: work tree missing: {t2_worktree}" in out2
    assert "skipped 2 task(s)" in out2
    assert "nothing to triage" not in out2
    assert calls == []


def test_triage_refuses_live_runner(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    state_file = plan_file.parent / "state.json"
    data = json.loads(state_file.read_text())
    other_pid = os.getpid() + 1000
    data["runner"] = {"pid": other_pid, "start_time": 12345}
    state_file.write_text(json.dumps(data))

    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: 12345 if pid == other_pid else None)
    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: True if pid == other_pid else False)

    calls = []
    monkeypatch.setattr(agenthub.triagerun, "triage_task", lambda *args, **kwargs: calls.append(1))

    rc = main(["plan", "triage", str(plan_file)])
    assert rc == 2
    err = capsys.readouterr().err
    assert f"agent-hub: plan runner {other_pid} is still running; wait or cancel it first" in err
    assert len(calls) == 0

    saved_state = json.loads(state_file.read_text())
    assert saved_state["runner"] == {"pid": other_pid, "start_time": 12345}


def test_triage_does_not_claim_runner(env_sandbox, tmp_path, monkeypatch):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)
    state_file = plan_file.parent / "state.json"
    plan_dir = plan_file.parent

    data = json.loads(state_file.read_text())
    data["runner"] = {"pid": 99999, "start_time": 12345}
    state_file.write_text(json.dumps(data))

    recorded_pid = os.getpid()
    recorded_time = 77777
    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: recorded_time if pid == recorded_pid else None)
    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: False if pid == 99999 else True)

    runner_pid_file = plan_file.parent / "runner.pid"
    runner_pid_file.unlink(missing_ok=True)

    triage_calls = 0

    def fake_triage(plan, task, tstate, jobs, head, now):
        nonlocal triage_calls
        triage_calls += 1
        saved_state = json.loads(state_file.read_text())
        assert saved_state.get("runner") != {"pid": recorded_pid, "start_time": recorded_time}
        runner_pid_file = plan_file.parent / "runner.pid"
        assert not runner_pid_file.is_file()
        return None

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])
    assert rc == 0
    assert triage_calls > 0

    saved_state = json.loads(state_file.read_text())
    assert saved_state.get("runner") != {"pid": recorded_pid, "start_time": recorded_time}
    assert not (plan_dir / "runner.pid").is_file()


def test_zombie_runner_is_not_alive(env_sandbox, tmp_path, monkeypatch):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    state_file = plan_file.parent / "state.json"

    zombie_pid = os.getpid() + 1000
    recorded_time = 12345
    data = json.loads(state_file.read_text())
    data["runner"] = {"pid": zombie_pid, "start_time": recorded_time}
    state_file.write_text(json.dumps(data))

    my_pid = os.getpid()
    my_start_time = 77777

    monkeypatch.setattr(
        agenthub.cli,
        "start_time",
        lambda pid: recorded_time if pid == zombie_pid else (my_start_time if pid == my_pid else None),
    )
    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: False if pid == zombie_pid else True)

    triage_calls = 0

    def fake_triage(plan, task, tstate, jobs, head, now):
        nonlocal triage_calls
        triage_calls += 1
        saved_state = json.loads(state_file.read_text())
        assert saved_state.get("runner") != {"pid": my_pid, "start_time": my_start_time}
        return None

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])
    assert rc == 0
    assert triage_calls > 0


def test_claim_uses_lock_file(env_sandbox, tmp_path, monkeypatch):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    plan_dir = plan_file.parent

    flock_calls = []
    orig_flock = fcntl.flock

    def spy_flock(fd, op):
        flock_calls.append((fd, op))
        return orig_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", spy_flock)
    monkeypatch.setattr(agenthub.triagerun, "triage_task", lambda *args, **kwargs: None)

    rc = main(["plan", "triage", str(plan_file)])
    assert rc == 0

    lock_file = plan_dir / "runner.lock"
    assert lock_file.is_file()
    assert any(op == (fcntl.LOCK_EX | fcntl.LOCK_NB) for _, op in flock_calls)


def test_empty_worktree_counts_as_skipped(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    state_file = plan_file.parent / "state.json"
    data = json.loads(state_file.read_text())
    data["tasks"]["t1"]["worktree"] = ""
    state_file.write_text(json.dumps(data))

    calls = []

    def fake_triage(plan, task, tstate, jobs, head, now):
        calls.append(task.name)
        return None

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "t1 skipped: no work tree recorded" in out
    assert calls == ["t2"]
    assert "skipped 1 task(s)" in out


def test_triage_reads_head_without_default_branch(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo, default="develop", branch=None)
    wt = tmp_path / "wt1"
    subprocess.run(["git", "worktree", "add", "-b", "task/t1", str(wt)], cwd=repo, check=True, capture_output=True)
    t1_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout.strip()

    # Verify that guards.inspect raises GuardError on this repo because there is no main/master/origin
    with pytest.raises(GuardError, match="No default branch found"):
        inspect(wt)

    content = {
        "goal": "do it",
        "repo": str(repo),
        "base": "develop",
        "slug": "plan-develop",
        "tasks": [{"name": "t1", "role": "implement", "brief": "b1.md"}],
    }
    plan_file = make_plan_file(tmp_path, repo, content=content)
    state_file = plan_file.parent / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "status": "running",
                "tasks": {
                    "t1": {
                        "status": "done",
                        "worktree": str(wt),
                        "branch": "task/t1",
                        "attempts": [],
                    }
                },
            }
        )
    )

    calls = []

    def fake_triage(plan, task, tstate, jobs, head, now):
        calls.append((task.name, head))
        return {
            "task": "t1",
            "status": "fix",
            "counts": {"fix": 1, "reject": 0, "blocked": 0},
            "fix_path": "/path/to/fixes/t1-1.md",
            "rejected_path": "",
            "reason": "",
        }

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [("t1", t1_head)]
    assert "t1 fix=1 reject=0 blocked=0 brief=/path/to/fixes/t1-1.md" in out
    assert "skipped" not in out


def test_triage_skips_unborn_head(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t1_worktree = Path(state["tasks"]["t1"]["worktree"])
    shutil.rmtree(t1_worktree)
    t1_worktree.mkdir()
    subprocess.run(["git", "init"], cwd=t1_worktree, check=True, capture_output=True)

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
    assert rc == 1
    assert f"t1 skipped: cannot read HEAD: {t1_worktree}" in out
    assert calls == ["t2"]
    assert "t2 fix=1 reject=0 blocked=0 brief=/path/to/fixes/t2-1.md" in out
    assert "skipped 1 task(s)" in out


def test_skipped_tasks_exit_1(env_sandbox, tmp_path, monkeypatch, capsys):
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
    assert rc == 1
    assert f"t1 skipped: work tree missing: {t1_worktree}" in out
    assert calls == ["t2"]
    assert "t2 fix=1 reject=0 blocked=0 brief=/path/to/fixes/t2-1.md" in out
    assert "skipped 1 task(s)" in out


def test_plan_run_refuses_live_runner(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    state_file = plan_file.parent / "state.json"
    data = json.loads(state_file.read_text())
    other_pid = os.getpid() + 1000
    data["runner"] = {"pid": other_pid, "start_time": 12345}
    state_file.write_text(json.dumps(data))

    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: 12345 if pid == other_pid else None)
    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: True if pid == other_pid else False)

    run_calls = []
    monkeypatch.setattr(agenthub.cli, "run_plan", lambda *args, **kwargs: run_calls.append(1))

    rc = main(["plan", "run", str(plan_file)])
    assert rc == 2
    err = capsys.readouterr().err
    assert f"agent-hub: plan runner {other_pid} is still running; wait or cancel it first" in err
    assert len(run_calls) == 0

    saved_state = json.loads(state_file.read_text())
    assert saved_state["runner"] == {"pid": other_pid, "start_time": 12345}


def test_claim_rereads_runner_under_lock(env_sandbox, tmp_path, monkeypatch):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)
    plan_dir = plan_file.parent
    state_file = plan_dir / "state.json"

    p = load_plan(plan_file)
    s = load_or_init(p)

    # In-memory state object holds a dead runner
    s.runner = {"pid": 99999, "start_time": 11111}

    # On disk, state.json names a live verified runner
    live_pid = os.getpid() + 1000
    live_start = 22222
    disk_data = json.loads(state_file.read_text())
    disk_data["runner"] = {"pid": live_pid, "start_time": live_start}
    state_file.write_text(json.dumps(disk_data))

    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: True if pid == live_pid else False)
    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: live_start if pid == live_pid else None)

    with pytest.raises(UsageError, match=f"plan runner {live_pid} is still running; wait or cancel it first"):
        _claim_runner(s, plan_dir, plan=p)


def test_claim_refreshes_tasks_from_disk(env_sandbox, tmp_path, monkeypatch):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)
    plan_dir = plan_file.parent
    state_file = plan_dir / "state.json"

    p = load_plan(plan_file)
    s = load_or_init(p)

    # In-memory state has task t1 with fix_rounds=0
    assert s.tasks["t1"].fix_rounds == 0

    # On disk, update task t1 to fix_rounds=3 and status="pending"
    disk_data = json.loads(state_file.read_text())
    disk_data["tasks"]["t1"]["fix_rounds"] = 3
    disk_data["tasks"]["t1"]["status"] = "pending"
    disk_data["runner"] = {}
    state_file.write_text(json.dumps(disk_data))

    my_pid = os.getpid()
    my_time = 77777
    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: my_time if pid == my_pid else None)
    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: True if pid == my_pid else False)

    _claim_runner(s, plan_dir, plan=p)

    assert s.tasks["t1"].fix_rounds == 3
    assert s.tasks["t1"].status == "pending"

    # Verify state on disk has the refreshed tasks and the claimed runner
    saved = json.loads(state_file.read_text())
    assert saved["tasks"]["t1"]["fix_rounds"] == 3
    assert saved["runner"] == {"pid": my_pid, "start_time": my_time}


def test_claim_without_plan_checks_live_runner_on_disk(env_sandbox, tmp_path, monkeypatch):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)
    plan_dir = plan_file.parent
    state_file = plan_dir / "state.json"

    p = load_plan(plan_file)
    s = load_or_init(p)

    # In-memory state object holds an empty runner
    s.runner = {}

    # On disk, state.json names a live runner
    live_pid = os.getpid() + 1000
    live_start = 22222
    disk_data = json.loads(state_file.read_text())
    disk_data["runner"] = {"pid": live_pid, "start_time": live_start}
    state_file.write_text(json.dumps(disk_data))

    my_pid = os.getpid()
    my_time = 77777
    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: True)
    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: my_time if pid == my_pid else live_start)

    # Even without plan=..., _live_runner checks disk and refuses
    with pytest.raises(UsageError, match=f"plan runner {live_pid} is still running; wait or cancel it first"):
        _claim_runner(s, plan_dir)


def test_live_runner(tmp_path, monkeypatch):
    # Non-existent file
    assert _live_runner(tmp_path) is None

    # Invalid JSON
    state_file = tmp_path / "state.json"
    state_file.write_text("not json")
    assert _live_runner(tmp_path) is None

    # Missing runner
    state_file.write_text(json.dumps({"tasks": {}}))
    assert _live_runner(tmp_path) is None

    # Runner is this process (should return None)
    my_pid = os.getpid()
    state_file.write_text(json.dumps({"runner": {"pid": my_pid, "start_time": 100}}))
    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: 100)
    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: True)
    assert _live_runner(tmp_path) is None

    # Runner is other dead process
    other_pid = os.getpid() + 1000
    state_file.write_text(json.dumps({"runner": {"pid": other_pid, "start_time": 200}}))
    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: False)
    assert _live_runner(tmp_path) is None

    # Runner start_time mismatch
    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: True)
    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: 300)
    assert _live_runner(tmp_path) is None

    # Runner verified alive
    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: 200)
    assert _live_runner(tmp_path) == other_pid


def test_triage_refusal_leaves_state_untouched(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)
    state_file = plan_file.parent / "state.json"
    data = json.loads(state_file.read_text())
    other_pid = os.getpid() + 1000
    data["runner"] = {"pid": other_pid, "start_time": 12345}
    data["tasks"]["t1"]["status"] = "running"
    data["tasks"]["t1"]["attempts"] = [{"job": "", "status": "running", "exit_code": None, "gate_touched": []}]
    raw_bytes = json.dumps(data, indent=2).encode("utf-8") + b"\n"
    state_file.write_bytes(raw_bytes)

    monkeypatch.setattr(agenthub.cli, "start_time", lambda pid: 12345 if pid == other_pid else None)
    monkeypatch.setattr(agenthub.cli, "is_running", lambda pid: True if pid == other_pid else False)

    rc = main(["plan", "triage", str(plan_file)])
    assert rc == 2
    err = capsys.readouterr().err
    assert f"agent-hub: plan runner {other_pid} is still running; wait or cancel it first" in err
    assert state_file.read_bytes() == raw_bytes


def test_run_refuses_while_triage_holds_lock(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    run_rc = None
    run_err = None

    def fake_triage(plan, task, tstate, jobs, head, now):
        nonlocal run_rc, run_err
        run_rc = main(["plan", "run", str(plan_file)])
        run_err = capsys.readouterr().err
        return None

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])
    assert rc == 0
    assert run_rc == 2
    assert "agent-hub: plan is busy (triage in progress); try again later" in run_err


def test_triage_stops_on_signal(env_sandbox, tmp_path, monkeypatch):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)
    state_file = plan_file.parent / "state.json"

    triage_calls = []

    def fake_triage(plan, task, tstate, jobs, head, now):
        triage_calls.append(task.name)
        if task.name == "t1":
            tstate.status = "blocked"
            tstate.reason = "triage job cancelled"
            os.kill(os.getpid(), signal.SIGTERM)
            return {
                "task": task.name,
                "status": "blocked",
                "reason": "triage job cancelled",
            }
        return None

    monkeypatch.setattr(agenthub.triagerun, "triage_task", fake_triage)

    rc = main(["plan", "triage", str(plan_file)])
    assert rc == 130
    assert triage_calls == ["t1"]

    saved = json.loads(state_file.read_text())
    assert saved["tasks"]["t1"]["status"] == "done"
    assert saved["tasks"]["t1"]["reason"] == ""
    assert saved["tasks"]["t2"]["status"] == "done"
