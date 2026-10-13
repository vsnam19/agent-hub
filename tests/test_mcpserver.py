import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenthub import guards
from agenthub.brief import BriefError
from agenthub.cli import UsageError, main
from agenthub.config import ConfigError, Paths, paths
from agenthub.jobs import get_job, new_job
from agenthub.mcpserver import (
    _spawn_detached,
    build_server,
    clamp_timeout,
    gate_start,
    jobs_data,
    pool_data,
    ready,
    run_job,
    show_job_data,
    wait_job,
)
from tests.gitrepo import make_repo

BRIEF = "## Input\n\nfoo\n\n## Expected output\n\nbar\n"


def write_brief(tmp_path, text=BRIEF):
    f = tmp_path / "brief.md"
    f.write_text(text)
    return f


def test_pool_data_matches_cli_json(env_sandbox, capsys):
    assert main(["pool", "--json"]) == 0
    cli_out = json.loads(capsys.readouterr().out)
    data = pool_data()
    assert data == cli_out
    assert "roles" in data


def test_jobs_data_lists_repo_jobs(env_sandbox, repo, tmp_path):
    repo2 = make_repo(tmp_path / "repo2")
    p = paths()
    now = datetime.now(timezone.utc)

    job1 = new_job(p, "implement", now)
    job1.write_json(
        "result.json",
        {
            "id": job1.id,
            "role": "implement",
            "status": "ok",
            "repo": str(guards.inspect(repo).common_dir),
            "workdir": str(repo),
        },
    )

    job2 = new_job(p, "implement", now)
    job2.write_json(
        "result.json",
        {
            "id": job2.id,
            "role": "implement",
            "status": "ok",
            "repo": str(guards.inspect(repo2).common_dir),
            "workdir": str(repo2),
        },
    )

    # Scoped to repo
    res_repo = jobs_data(str(repo))
    assert "jobs" in res_repo
    repo_ids = [j["id"] for j in res_repo["jobs"]]
    assert job1.id in repo_ids
    assert job2.id not in repo_ids

    # All jobs across repos
    res_all = jobs_data(str(repo), all=True)
    assert "jobs" in res_all
    all_ids = [j["id"] for j in res_all["jobs"]]
    assert job1.id in all_ids
    assert job2.id in all_ids


def test_show_job_data_unknown_id(env_sandbox):
    with pytest.raises(KeyError):
        show_job_data("nonexistent-job-id")

    p = paths()
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json("result.json", {"id": job.id, "status": "ok", "role": "implement"})
    data = show_job_data(job.id)
    assert data["id"] == job.id
    assert data["status"] == "ok"
    assert data["role"] == "implement"


def test_mcp_command_without_sdk_exit_2(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setitem(sys.modules, "mcp.server", None)
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)
    assert main(["mcp"]) == 2
    err = capsys.readouterr().err
    assert "install with: uv tool install 'agent-hub[mcp]'" in err


def test_mcp_runtime_import_error_not_masked(monkeypatch):
    def fake_run_stdio():
        raise ImportError("boom")

    monkeypatch.setattr("agenthub.mcpserver.run_stdio", fake_run_stdio)
    with pytest.raises(ImportError, match="boom"):
        main(["mcp"])


def test_server_lists_tools(env_sandbox):
    server = build_server()
    tools = asyncio.run(server.list_tools())
    tool_map = {t.name: t for t in tools}
    assert set(tool_map.keys()) == {"pool", "jobs", "show_job", "run_job", "wait_job", "gate_start", "ready"}
    for name in ["pool", "jobs", "show_job", "wait_job", "ready"]:
        tool = tool_map[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    assert tool_map["run_job"].annotations is None or tool_map["run_job"].annotations.read_only_hint is not True
    assert tool_map["gate_start"].annotations is None or tool_map["gate_start"].annotations.read_only_hint is not True

    pool_res = asyncio.run(server.call_tool("pool", {}))
    assert not pool_res.is_error
    assert len(pool_res.content) == 1


def test_run_job_returns_id_before_job_finishes(env_sandbox, repo, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "slow")
    brief = write_brief(repo)

    res = run_job(workdir=str(repo), role="implement", brief_path=str(brief))
    assert "job_id" in res
    assert res["workdir"] == str(repo.resolve())
    job_id = res["job_id"]

    try:
        job = get_job(paths(), job_id)
        record = job.read_json("result.json")
        assert record["status"] == "running"
    finally:
        main(["cancel", job_id, "-d", str(repo)])


def test_wait_job_times_out_running(env_sandbox, repo, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "slow")
    brief = write_brief(repo)

    res = run_job(workdir=str(repo), role="implement", brief_path=str(brief))
    job_id = res["job_id"]

    try:
        wait_res = wait_job(job_id=job_id, timeout_s=1)
        assert wait_res == {"state": "running"}
    finally:
        main(["cancel", job_id, "-d", str(repo)])


def test_wait_job_returns_result(env_sandbox, repo, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "ok")
    brief = write_brief(repo)

    res = run_job(
        workdir=str(repo),
        role="implement",
        agent="agy:claude-sonnet-4-6",
        brief_path=str(brief),
    )
    job_id = res["job_id"]

    wait_res = wait_job(job_id=job_id, timeout_s=30)
    assert wait_res["status"] == "ok"
    assert wait_res["exit_code"] == 0
    assert wait_res["agent"] == "agy"
    assert wait_res["model"] is not None
    assert "reply_path" in wait_res
    assert "gate" in wait_res


def test_wait_job_clamps_timeout(env_sandbox, repo, monkeypatch):
    assert clamp_timeout(0) == 1
    assert clamp_timeout(-10) == 1
    assert clamp_timeout(50) == 50
    assert clamp_timeout(300) == 300
    assert clamp_timeout(301) == 300
    assert clamp_timeout(1000) == 300
    assert clamp_timeout("2s") == 2
    assert clamp_timeout("10m") == 300

    p = paths()
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json("result.json", {"id": job.id, "status": "ok", "exit_code": 0, "workdir": str(repo)})

    res0 = wait_job(job_id=job.id, timeout_s=0)
    assert res0["status"] == "ok"

    res_large = wait_job(job_id=job.id, timeout_s=1000)
    assert res_large["status"] == "ok"

    job_running = new_job(p, "implement", datetime.now(timezone.utc))
    job_running.write_json("result.json", {"id": job_running.id, "status": "running", "workdir": str(repo)})

    times = [100.0]

    def fake_monotonic():
        return times[0]

    def fake_sleep(duration):
        times[0] += 50.0

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    res = wait_job(job_id=job_running.id, timeout_s=1000)
    assert res == {"state": "running"}
    assert times[0] <= 450.0


def test_run_job_validation_errors(env_sandbox, repo):
    brief = write_brief(repo)

    with pytest.raises(UsageError):
        run_job(workdir=str(repo), role=None, brief_path=str(brief))

    with pytest.raises(UsageError):
        run_job(workdir="", role="implement", brief_path=str(brief))

    with pytest.raises(ConfigError):
        run_job(workdir=str(repo), role="nonexistent", brief_path=str(brief))

    with pytest.raises(UsageError):
        run_job(workdir=str(repo), role="implement", brief_path=None)

    bad_brief = repo / "bad_brief.md"
    bad_brief.write_text("No headings here")
    with pytest.raises(BriefError):
        run_job(workdir=str(repo), role="implement", brief_path=str(bad_brief))

    with pytest.raises(ConfigError):
        run_job(workdir=str(repo), role="implement", agent="invalid_link", brief_path=str(brief))

    with pytest.raises(ConfigError):
        run_job(workdir=str(repo), role="implement", agent="nosuch:model", brief_path=str(brief))


def test_wait_job_errors(env_sandbox, repo):
    with pytest.raises(UsageError):
        wait_job("")

    with pytest.raises(KeyError):
        wait_job(job_id="nonexistent-id", timeout_s=1)


def test_wait_job_requires_id(env_sandbox):
    """wait_job must accept job_id and reject missing or empty ids."""
    with pytest.raises(TypeError):
        wait_job()  # type: ignore[call-arg]

    with pytest.raises(UsageError):
        wait_job("")

    with pytest.raises(UsageError):
        wait_job("   ")

    with pytest.raises(KeyError):
        wait_job(job_id="20260101-000000-implement-ffff", timeout_s=1)

    p = paths()
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json("result.json", {"id": job.id, "status": "ok", "role": "implement"})
    res = wait_job(job_id=job.id, timeout_s=5)
    assert res["status"] == "ok"
    assert res["job_id"] == job.id


def test_wait_job_running_before_result(env_sandbox, repo):
    p = paths()
    now = datetime.now(timezone.utc)
    job = new_job(p, "implement", now)
    assert job.dir.is_dir()
    assert not (job.dir / "result.json").is_file()

    # Returns running state on timeout while result.json is absent
    res = wait_job(job_id=job.id, timeout_s=1)
    assert res == {"state": "running"}

    # Unknown job id raises KeyError
    with pytest.raises(KeyError):
        wait_job(job_id="20260101-000000-implement-ffff", timeout_s=1)

    # When result.json appears before timeout, wait_job returns the completed result
    record = {
        "id": job.id,
        "status": "ok",
        "exit_code": 0,
        "workdir": str(repo),
        "role": "implement",
    }
    job.write_json("result.json", record)
    res_done = wait_job(job_id=job.id, timeout_s=2)
    assert res_done["status"] == "ok"
    assert res_done["job_id"] == job.id
    assert res_done["state"] == "ok"


def git_head(repo):
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def set_repo_gates(repo, gates):
    (repo / ".agent-hub").mkdir(exist_ok=True)
    (repo / ".agent-hub" / "config.toml").write_text(f"gates = {json.dumps(gates)}\n")
    subprocess.run(["git", "add", ".agent-hub"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "repo gates"], cwd=repo, check=True)


def add_job_record(
    repo, kind, head, *, model="m1", agent="codex", verdict=None, gate_sha=None, n=0, repo_gates=("true",), touched=()
):
    when = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=n)
    job = new_job(paths(), kind, when)
    record = {
        "id": job.id,
        "role": kind,
        "kind": kind,
        "status": "ok",
        "agent": agent,
        "model": model,
        "workdir": str(repo),
        "head": head,
        "verdict": verdict,
        "gate": [{"cmd": "true", "rc": 0, "sha": gate_sha or head}] if kind in ("implement", "gate") else [],
        "gate_touched": list(touched),
    }
    if kind == "gate":
        record["repo_gates"] = list(repo_gates)
    job.write_json("result.json", record)


def test_gate_start_returns_gate_job(env_sandbox, repo):
    set_repo_gates(repo, ["true"])
    res = gate_start(str(repo))
    assert "job_id" in res
    assert res["workdir"] == str(repo.resolve())
    job_id = res["job_id"]
    assert "-gate-" in job_id

    wait_res = wait_job(job_id=job_id, timeout_s=10)
    assert wait_res["status"] == "ok"
    assert wait_res["kind"] == "gate"
    assert wait_res["gate_summary"] == "passed (1)"


def test_ready_reports_problems(env_sandbox, repo):
    head = git_head(repo)
    res = ready(str(repo))
    assert res == {"ready": False, "problems": res["problems"]}
    assert f"no passing gate on {head}" in res["problems"]
    assert "no implementer recorded: cannot check reviewer independence" in res["problems"]
    assert "no review" in res["problems"]


def test_ready_true_on_approved_head(env_sandbox, repo):
    head = git_head(repo)
    add_job_record(repo, "implement", head, model="m1", n=1)
    add_job_record(repo, "review", head, model="m2", verdict="approve", n=2)
    res = ready(str(repo))
    assert res == {"ready": True, "sha": head}

    server = build_server()
    call_res = asyncio.run(server.call_tool("ready", {"workdir": str(repo)}))
    assert not call_res.is_error
    assert json.loads(call_res.content[0].text) == {"ready": True, "sha": head}


def test_gate_start_validation_and_errors(env_sandbox, repo):
    with pytest.raises(UsageError):
        gate_start("")

    (repo / "dirty.txt").write_text("uncommitted\n")
    res = gate_start(str(repo))
    assert "error" in res
    assert "commit first; gate runs on HEAD" in res["error"]


def test_ready_validation_errors(env_sandbox, repo):
    with pytest.raises(UsageError):
        ready("")

    with pytest.raises(UsageError):
        ready(str(repo / "nonexistent-dir"))


def test_spawn_env_does_not_add_state_or_config(env_sandbox, monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_HUB_STATE", raising=False)
    monkeypatch.delenv("AGENT_HUB_CONFIG", raising=False)
    seen_envs = []

    class FailedProc:
        returncode = 1

        def poll(self):
            return 1

    def fake_popen(cmd, **kwargs):
        seen_envs.append(kwargs["env"])
        return FailedProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    p = Paths(config_dir=tmp_path / "config", state_dir=tmp_path / "state")

    res = _spawn_detached(cmd=["true"], workdir_path=tmp_path, p=p)
    assert "error" in res
    assert "AGENT_HUB_STATE" not in seen_envs[0]
    assert "AGENT_HUB_CONFIG" not in seen_envs[0]
    assert seen_envs[0]["PATH"] == os.environ["PATH"]

    state_from_server = str(tmp_path / "server-state")
    monkeypatch.setenv("AGENT_HUB_STATE", state_from_server)
    _spawn_detached(cmd=["true"], workdir_path=tmp_path, p=p)
    assert seen_envs[1]["AGENT_HUB_STATE"] == state_from_server


def test_announce_writes_job_id(env_sandbox, repo, tmp_path):
    brief = write_brief(tmp_path)
    announce_run = tmp_path / "announce-run.txt"
    rc = main(
        [
            "run",
            "--role",
            "implement",
            "--brief",
            str(brief),
            "-d",
            str(repo),
            "--agent",
            "claude:haiku",
            "--announce",
            str(announce_run),
        ]
    )
    assert rc == 0
    assert announce_run.is_file()
    announced_run_id = announce_run.read_text().strip()
    assert announced_run_id
    run_record = get_job(paths(), announced_run_id).read_json("result.json")
    assert run_record["id"] == announced_run_id
    assert run_record["role"] == "implement"

    set_repo_gates(repo, ["true"])
    announce_gate = tmp_path / "announce-gate.txt"
    rc = main(
        [
            "gate",
            "-d",
            str(repo),
            "--announce",
            str(announce_gate),
        ]
    )
    assert rc == 0
    assert announce_gate.is_file()
    announced_gate_id = announce_gate.read_text().strip()
    assert announced_gate_id
    gate_record = get_job(paths(), announced_gate_id).read_json("result.json")
    assert gate_record["id"] == announced_gate_id
    assert gate_record["role"] == "gate"
    assert "-gate-" in announced_gate_id


def test_spawn_uses_announced_id_not_newest_job(env_sandbox, repo, tmp_path):
    repo2 = make_repo(tmp_path / "repo2")
    p = paths()
    future = datetime.now(timezone.utc) + timedelta(minutes=10)

    # Decoy job for the same workdir created first
    decoy_same_workdir = new_job(p, "implement", future)
    decoy_same_workdir.write_json(
        "request.json",
        {"role": "implement", "workdir": str(repo.resolve())},
    )
    decoy_same_workdir.write_json(
        "result.json",
        {"id": decoy_same_workdir.id, "status": "ok", "role": "implement", "workdir": str(repo.resolve())},
    )

    # Decoy gate job for another repo created first
    decoy_gate_other_repo = new_job(p, "gate", future)
    decoy_gate_other_repo.write_json(
        "request.json",
        {"role": "gate", "workdir": str(repo2.resolve())},
    )
    decoy_gate_other_repo.write_json(
        "result.json",
        {"id": decoy_gate_other_repo.id, "status": "ok", "role": "gate", "workdir": str(repo2.resolve())},
    )

    set_repo_gates(repo, ["true"])
    res_gate = gate_start(str(repo))
    assert "job_id" in res_gate
    assert res_gate["job_id"] != decoy_gate_other_repo.id
    assert res_gate["job_id"] != decoy_same_workdir.id
    assert "-gate-" in res_gate["job_id"]

    brief = write_brief(repo)
    res_run = run_job(workdir=str(repo), role="implement", brief_path=str(brief))
    assert "job_id" in res_run
    assert res_run["job_id"] != decoy_same_workdir.id
    assert res_run["job_id"] != decoy_gate_other_repo.id
    assert res_run["job_id"] != res_gate["job_id"]

    # Verify temp announce files and spawn logs were cleaned up from state dir
    assert list(p.state_dir.glob("*announce*")) == []
    assert list(p.state_dir.glob("mcp-*.log")) == []

    # Verify spawn logs were moved into the job dirs
    gate_job = get_job(p, res_gate["job_id"])
    assert (gate_job.dir / "spawn.log").is_file()
    run_job_record = get_job(p, res_run["job_id"])
    assert (run_job_record.dir / "spawn.log").is_file()


def test_spawn_log_moved_into_job_dir(env_sandbox, repo):
    p = paths()
    set_repo_gates(repo, ["true"])
    res = gate_start(str(repo))
    assert "job_id" in res
    job_id = res["job_id"]
    job = get_job(p, job_id)
    spawn_log = job.dir / "spawn.log"
    assert spawn_log.is_file()
    # State dir should not retain the spawn log or temp announce file
    assert list(p.state_dir.glob("mcp-*.log")) == []
    assert list(p.state_dir.glob("*announce*")) == []

    # Spawn that never announces keeps its log and returns error with log path
    bad_res = _spawn_detached(
        cmd=[sys.executable, "-c", "import sys; sys.stderr.write('fatal crash\\n'); sys.exit(1)"],
        workdir_path=repo,
        p=p,
    )
    assert "error" in bad_res
    assert "fatal crash" in bad_res["error"]
    assert "log" in bad_res
    log_path = Path(bad_res["log"])
    assert log_path.is_file()
    assert str(log_path) in bad_res["error"]
    assert log_path.parent == p.state_dir


def test_spawn_log_stays_if_job_dir_missing(env_sandbox, repo):
    p = paths()
    fake_id = "20260101-000000-gate-9999"
    # Announce an id, but do not create the job directory
    script = (
        "import sys, pathlib; "
        "idx = sys.argv.index('--announce'); "
        "announce = pathlib.Path(sys.argv[idx + 1]); "
        f"announce.write_text('{fake_id}\\n')"
    )
    res = _spawn_detached(
        cmd=[sys.executable, "-c", script],
        workdir_path=repo,
        p=p,
    )
    assert res["job_id"] == fake_id
    assert "log" in res
    log_path = Path(res["log"])
    assert log_path.is_file()
    assert log_path.parent == p.state_dir
    # Job directory was NOT created by _spawn_detached
    assert not (p.jobs_dir / fake_id).exists()


def test_spawn_timeout_kills_child_and_cleans_up(env_sandbox, repo, monkeypatch):
    """On announce timeout: child process group is terminated and announce file is removed."""
    p = paths()

    # Child writes its PID to a file then sleeps indefinitely without writing the announce file
    pid_file = repo / "child_pid.txt"
    script = f"import os, time, pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(300)"

    real_monotonic = time.monotonic
    real_sleep = time.sleep

    def fast_monotonic():
        # Only advance past 60s poll_deadline once child has started and written pid
        if pid_file.is_file():
            return real_monotonic() + 120.0
        return real_monotonic()

    monkeypatch.setattr(time, "monotonic", fast_monotonic)
    monkeypatch.setattr(time, "sleep", lambda _: real_sleep(0.01))

    res = _spawn_detached(
        cmd=[sys.executable, "-c", script],
        workdir_path=repo,
        p=p,
    )

    # Returns an error because no announce file appeared (timeout)
    assert "error" in res
    assert "log" in res
    log_path = Path(res["log"])
    assert log_path.is_file()
    assert str(log_path) in res["error"]

    # No temporary announce files should remain in the state dir
    assert list(p.state_dir.glob("*announce*")) == []

    # Verify the child process was actually killed
    assert pid_file.is_file(), "child process should have written its pid file"
    child_pid = int(pid_file.read_text().strip())

    # Process should no longer be running (SIGTERM was sent and proc.wait was called)
    deadline2 = real_monotonic() + 5.0
    while real_monotonic() < deadline2:
        try:
            os.kill(child_pid, 0)
        except OSError:
            break  # Process gone
        real_sleep(0.05)
    else:
        # Process still alive after 5s — clean up and fail
        try:
            os.kill(child_pid, 9)
        except OSError:
            pass
        raise AssertionError(f"child process {child_pid} was not killed by _spawn_detached timeout")


def test_job_id_traversal_rejected(env_sandbox, tmp_path):
    p = paths()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = {"id": "escaped-outside-job", "status": "ok", "role": "implement"}
    (outside / "result.json").write_text(json.dumps(secret))

    # Path traversal: ../../x and relative escape to outside dir
    for traversal_id in ["../../x", f"../../{outside.name}"]:
        with pytest.raises(KeyError):
            show_job_data(traversal_id)
        with pytest.raises(KeyError):
            wait_job(traversal_id, timeout_s=1)

    # Absolute path
    for abs_id in [str(outside), "/etc", "/tmp"]:
        with pytest.raises(KeyError):
            show_job_data(abs_id)
        with pytest.raises(KeyError):
            wait_job(abs_id, timeout_s=1)

    # Empty and whitespace
    for empty_id in ["", "   "]:
        with pytest.raises(UsageError):
            show_job_data(empty_id)
        with pytest.raises(UsageError):
            wait_job(empty_id)

    # Symlink matching job id pattern but escaping jobs dir is rejected
    symlink_job_id = "20260101-000000-implement-0000"
    p.jobs_dir.mkdir(parents=True, exist_ok=True)
    (p.jobs_dir / symlink_job_id).symlink_to(outside)
    with pytest.raises(KeyError):
        show_job_data(symlink_job_id)
    with pytest.raises(KeyError):
        wait_job(symlink_job_id, timeout_s=1)


def test_spawned_child_is_reaped(env_sandbox, repo, tmp_path):
    p = paths()
    fake_id = "20260101-000000-gate-9999"
    pid_file = tmp_path / "child_pid.txt"
    script = (
        "import os, sys, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "idx = sys.argv.index('--announce'); "
        "announce = pathlib.Path(sys.argv[idx + 1]); "
        f"announce.write_text('{fake_id}\\n'); "
        "time.sleep(0.1)"
    )
    res = _spawn_detached(
        cmd=[sys.executable, "-c", script],
        workdir_path=repo,
        p=p,
    )
    assert res["job_id"] == fake_id
    assert pid_file.is_file()
    child_pid = int(pid_file.read_text().strip())

    # Wait bounded time for daemon thread to reap the child
    deadline = time.monotonic() + 5.0
    reaped = False
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            reaped = True
            break
        time.sleep(0.05)

    assert reaped, f"Child process {child_pid} was not reaped by daemon thread"
