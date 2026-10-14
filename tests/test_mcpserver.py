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
from agenthub.jobs import get_job, list_jobs, new_job
from agenthub.mcpserver import (
    _dead_runner_pid,
    _plan_stalled,
    _reap_review,
    _spawn_detached,
    build_server,
    clamp_timeout,
    gate_start,
    jobs_data,
    plan_close,
    plan_merge,
    plan_review,
    plan_review_wait,
    plan_review_wait_async,
    plan_run,
    plan_status,
    plan_triage,
    plan_wait,
    pool_data,
    ready,
    run_job,
    show_job_data,
    validate_run_args,
    wait_job,
    wait_job_async,
)
from agenthub.plan import PlanError
from tests.gitrepo import make_repo
from tests.test_plan_cli import fake_review, make_gated_plan_run, make_plan_file, run_cli_plan

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
    assert set(tool_map.keys()) == {
        "pool",
        "jobs",
        "show_job",
        "run_job",
        "wait_job",
        "gate_start",
        "ready",
        "plan_run",
        "plan_status",
        "plan_wait",
        "plan_review",
        "plan_review_wait",
        "plan_merge",
        "plan_triage",
        "plan_close",
    }
    for name in ["pool", "jobs", "show_job", "wait_job", "ready", "plan_status", "plan_wait", "plan_review_wait"]:
        tool = tool_map[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    assert tool_map["run_job"].annotations is None or tool_map["run_job"].annotations.read_only_hint is not True
    assert tool_map["gate_start"].annotations is None or tool_map["gate_start"].annotations.read_only_hint is not True
    assert tool_map["plan_run"].annotations is None or tool_map["plan_run"].annotations.read_only_hint is not True
    assert tool_map["plan_review"].annotations is None or tool_map["plan_review"].annotations.read_only_hint is not True
    assert tool_map["plan_merge"].annotations is not None
    assert tool_map["plan_merge"].annotations.destructive_hint is True

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


def test_wait_job_reports_dead_process(env_sandbox, repo):
    proc = subprocess.Popen(["true"])
    proc.wait()

    p = paths()
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json("result.json", {"id": job.id, "status": "running", "workdir": str(repo)})
    (job.dir / "pid").write_text(json.dumps({"pid": proc.pid, "start_time": 123456}))

    t0 = time.monotonic()
    res = wait_job(job_id=job.id, timeout_s=5)
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0
    assert res["status"] == "failed"
    assert res["state"] == "failed"
    assert res["exit_code"] == 1
    assert str(proc.pid) in res["reason"]
    assert res["reason"] == f"job process {proc.pid} is not running; result.json was never finished"
    assert job.read_json("result.json")["status"] == "running"

    res_async = asyncio.run(wait_job_async(job_id=job.id, timeout_s=5))
    assert res_async["status"] == "failed"
    assert res_async["state"] == "failed"
    assert res_async["exit_code"] == 1
    assert res_async["reason"] == f"job process {proc.pid} is not running; result.json was never finished"


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


def test_announced_job_has_running_record(env_sandbox, repo, tmp_path, monkeypatch):
    import agenthub.cli as cli

    brief = write_brief(tmp_path)
    announce_run = tmp_path / "announce-run.txt"

    seen_at_announce = []
    orig_atomic_write = cli._atomic_write

    def checking_atomic_write(path, content):
        job_id = content.strip()
        job = get_job(paths(), job_id)
        assert (job.dir / "result.json").is_file(), f"result.json must exist when job {job_id} is announced"
        rec = job.read_json("result.json")
        assert rec["status"] == "running", f"status must be running when announced, got {rec['status']}"
        assert rec["id"] == job_id
        assert rec["workdir"] == str(repo.resolve())
        assert rec["started"] is not None
        seen_at_announce.append(rec)
        orig_atomic_write(path, content)

    monkeypatch.setattr(cli, "_atomic_write", checking_atomic_write)

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
    assert len(seen_at_announce) == 1
    assert seen_at_announce[0]["role"] == "implement"
    assert seen_at_announce[0]["kind"] == "implement"
    run_job_obj = get_job(paths(), seen_at_announce[0]["id"])
    assert (run_job_obj.dir / "pid").is_file()

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
    assert len(seen_at_announce) == 2
    assert seen_at_announce[1]["role"] == "gate"
    assert seen_at_announce[1]["kind"] == "gate"


def test_gate_job_running_record_before_gates(env_sandbox, repo, tmp_path):
    record_file = tmp_path / "seen_record.json"
    gate_cmd = (
        f'{sys.executable} -c "'
        f"import json, pathlib; "
        f"from agenthub.config import paths; "
        f"from agenthub.jobs import list_jobs; "
        f"jobs = list_jobs(paths(), 10); "
        f"assert len(jobs) >= 1, 'no jobs found during gate run'; "
        f"g = jobs[0]; "
        f"assert g['kind'] == 'gate', f'unexpected kind: {{g[\"kind\"]}}'; "
        f"assert g['status'] == 'running', f'unexpected status: {{g[\"status\"]}}'; "
        f"pathlib.Path({str(record_file)!r}).write_text(json.dumps(g)); "
        f'"'
    )
    set_repo_gates(repo, [gate_cmd])
    rc = main(["gate", "-d", str(repo), "--json"])
    assert rc == 0
    assert record_file.is_file()
    seen = json.loads(record_file.read_text())
    assert seen["kind"] == "gate"
    assert seen["status"] == "running"
    assert seen["workdir"] == str(repo.resolve())
    assert seen["started"] is not None
    assert seen["ended"] is None

    final_job = get_job(paths(), seen["id"]).read_json("result.json")
    assert final_job["status"] == "ok"
    assert final_job["ended"] is not None


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


def test_timeout_message_log_bounded(env_sandbox, repo, tmp_path, monkeypatch):
    """On spawn timeout, error message contains at most the last 40 lines of spawn log."""
    p = paths()

    pid_file = tmp_path / "child_pid.txt"
    # Child writes 100 lines to stdout, writes its PID to file, then sleeps
    script = (
        "import os, time, pathlib, sys; "
        "sys.stdout.write(''.join(f'log line {i}\\n' for i in range(100))); sys.stdout.flush(); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(300)"
    )

    real_monotonic = time.monotonic
    real_sleep = time.sleep

    def fast_monotonic():
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

    assert "error" in res
    assert "log" in res
    err = res["error"]

    # Verify that only the last 40 lines (lines 60 to 99) are kept
    assert "log line 99" in err
    assert "log line 60" in err
    assert "log line 59" not in err
    assert "log line 0" not in err

    # The prefix before " (log: " has exactly 40 lines
    log_content = err.rsplit(" (log: ", 1)[0]
    assert len(log_content.splitlines()) == 40


def test_job_id_traversal_rejected(env_sandbox, tmp_path):
    p = paths()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = {"id": "escaped-outside-job", "status": "ok", "role": "implement"}
    (outside / "result.json").write_text(json.dumps(secret))

    # Path traversal: ../../x, relative escape to outside dir, a/b, ..
    for traversal_id in ["../../x", f"../../{outside.name}", "a/b", ".."]:
        with pytest.raises(KeyError):
            show_job_data(traversal_id)
        with pytest.raises(KeyError):
            wait_job(traversal_id, timeout_s=1)

    # Absolute path
    for abs_id in [str(outside), "/etc", "/tmp", "/abs"]:
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


def test_job_id_with_user_role_names_accepted(env_sandbox):
    p = paths()
    now = datetime.now(timezone.utc)
    for role in ["review-mvp", "implement2", "Haiku_Impl", "a.b", "ROLE", "日本語"]:
        job = new_job(p, role, now)
        job.write_json("result.json", {"id": job.id, "status": "ok", "role": role})
        data = show_job_data(job.id)
        assert data["id"] == job.id
        assert data["role"] == role
        wait_res = wait_job(job.id, timeout_s=1)
        assert wait_res["state"] == "ok"


def test_spawn_early_exit_reported(env_sandbox, repo):
    p = paths()
    # Child exits with code 0 before announcing
    res0 = _spawn_detached(
        cmd=[sys.executable, "-c", "import sys; sys.stdout.write('clean exit\\n'); sys.exit(0)"],
        workdir_path=repo,
        p=p,
    )
    assert "error" in res0
    assert "child exited with code 0 before announcing a job" in res0["error"]
    assert "clean exit" in res0["error"]
    assert "log" in res0
    assert Path(res0["log"]).is_file()

    # Child exits with non-zero code before announcing
    res1 = _spawn_detached(
        cmd=[sys.executable, "-c", "import sys; sys.stderr.write('early error\\n'); sys.exit(2)"],
        workdir_path=repo,
        p=p,
    )
    assert "error" in res1
    assert "child exited with code 2 before announcing a job" in res1["error"]
    assert "early error" in res1["error"]
    assert "log" in res1
    assert Path(res1["log"]).is_file()


def test_clamp_rejects_nan_inf_bool():
    for bad in [True, False, float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf"]:
        with pytest.raises(UsageError):
            clamp_timeout(bad)
        with pytest.raises(UsageError):
            wait_job("20260101-000000-implement-0001", timeout_s=bad)


def test_spawn_child_stdin_is_devnull(env_sandbox, repo, tmp_path):
    p = paths()
    fake_id = "20260101-000000-gate-9999"
    out_file = tmp_path / "stdin_check.json"

    # Set up a pipe on parent's fd 0 so that if child inherits stdin, fd 0 is a pipe and reading blocks
    r_fd, w_fd = os.pipe()
    real_dup_stdin = os.dup(0)
    os.dup2(r_fd, 0)
    os.close(r_fd)

    script = (
        "import os, sys, pathlib, json; "
        "idx = sys.argv.index('--announce'); "
        "announce = pathlib.Path(sys.argv[idx + 1]); "
        f"announce.write_text('{fake_id}\\n'); "
        "stdin_data = sys.stdin.read(); "
        "fd0 = os.readlink('/proc/self/fd/0') if os.path.exists('/proc/self/fd/0') else '/dev/null'; "
        f"pathlib.Path({str(out_file)!r}).write_text(json.dumps({{'stdin_data': stdin_data, 'fd0': fd0}})); "
    )
    try:
        res = _spawn_detached(
            cmd=[sys.executable, "-c", script],
            workdir_path=repo,
            p=p,
        )
        assert res["job_id"] == fake_id
        assert out_file.is_file()
        data = json.loads(out_file.read_text())
        assert data["stdin_data"] == ""
        assert data["fd0"] == "/dev/null"
    finally:
        os.dup2(real_dup_stdin, 0)
        os.close(real_dup_stdin)
        os.close(w_fd)


def test_brief_path_must_be_absolute_file(env_sandbox, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel_brief.md").write_text(BRIEF)
    (tmp_path / "-").write_text(BRIEF)

    # Relative path is rejected even if it exists in cwd
    with pytest.raises(UsageError):
        run_job(workdir=str(repo), role="implement", brief_path="rel_brief.md")

    # "-" is rejected even if a file named "-" exists in cwd
    with pytest.raises(UsageError):
        run_job(workdir=str(repo), role="implement", brief_path="-")

    # Nonexistent absolute file is rejected
    with pytest.raises(UsageError):
        run_job(workdir=str(repo), role="implement", brief_path=str(tmp_path / "does_not_exist.md"))

    # Directory is rejected
    with pytest.raises(UsageError):
        run_job(workdir=str(repo), role="implement", brief_path=str(tmp_path))

    # Existing regular file given as absolute path is accepted
    valid_brief = write_brief(tmp_path)
    wpath, kind, text = validate_run_args(
        workdir=str(repo),
        role="implement",
        brief_path=str(valid_brief),
    )
    assert wpath == repo.resolve()
    assert kind == "implement"
    assert text == BRIEF


def test_child_does_not_import_cwd_agenthub(env_sandbox, repo, tmp_path, monkeypatch):
    decoy_dir = tmp_path / "server_cwd"
    decoy_dir.mkdir()
    decoy_pkg = decoy_dir / "agenthub"
    decoy_pkg.mkdir()
    (decoy_pkg / "__init__.py").write_text("")
    (decoy_pkg / "__main__.py").write_text("import sys; sys.exit(42)\n")

    monkeypatch.chdir(decoy_dir)
    brief = write_brief(tmp_path)

    res = run_job(workdir=str(repo), role="implement", brief_path=str(brief))
    assert "error" not in res
    assert "job_id" in res

    set_repo_gates(repo, ["true"])
    gate_res = gate_start(workdir=str(repo))
    assert "error" not in gate_res
    assert "job_id" in gate_res


def test_spawn_child_cwd_is_state_dir(env_sandbox, repo, tmp_path):
    p = paths()
    fake_id = "20260101-000000-gate-9999"
    out_file = tmp_path / "cwd_check.txt"
    script = (
        "import os, sys, pathlib; "
        "idx = sys.argv.index('--announce'); "
        "announce = pathlib.Path(sys.argv[idx + 1]); "
        f"announce.write_text('{fake_id}\\n'); "
        f"pathlib.Path({str(out_file)!r}).write_text(os.getcwd()); "
    )
    res = _spawn_detached(
        cmd=[sys.executable, "-c", script],
        workdir_path=repo,
        p=p,
    )
    assert res["job_id"] == fake_id
    assert out_file.is_file()
    assert Path(out_file.read_text()).resolve() == p.state_dir.resolve()


def test_timeout_escalates_to_sigkill(env_sandbox, repo, monkeypatch):
    """Child that ignores SIGTERM is escalated to SIGKILL on timeout."""
    p = paths()

    pid_file = repo / "child_pid.txt"
    # Child ignores SIGTERM and sleeps
    script = (
        "import os, signal, time, pathlib; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(300)"
    )

    real_monotonic = time.monotonic
    real_sleep = time.sleep

    def fast_monotonic():
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

    assert "error" in res
    assert "log" in res
    assert list(p.state_dir.glob("*announce*")) == []

    assert pid_file.is_file()
    child_pid = int(pid_file.read_text().strip())

    try:
        os.kill(child_pid, 0)
        alive = True
    except OSError:
        alive = False

    if alive:
        try:
            os.kill(child_pid, 9)
        except OSError:
            pass
        raise AssertionError(f"child process {child_pid} was not killed by SIGKILL escalation")


def test_announce_write_failure_does_not_stop_job(env_sandbox, repo, tmp_path, capsys):
    brief = write_brief(tmp_path)
    # A directory path cannot be overwritten as a regular file by _atomic_write
    bad_announce = tmp_path / "announce_dir"
    bad_announce.mkdir()

    rc = main(
        [
            "run",
            "-d",
            str(repo),
            "--role",
            "implement",
            "--brief",
            str(brief),
            "--announce",
            str(bad_announce),
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "announce" in err.lower()

    # The job finished normally (status: "ok")
    p = paths()
    jobs = list_jobs(p, 10)
    assert len(jobs) >= 1
    assert jobs[0]["status"] == "ok"


def test_wait_job_async_returns_result(env_sandbox, repo):
    p = paths()
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json("result.json", {"id": job.id, "status": "ok", "role": "implement"})

    res = asyncio.run(wait_job_async(job_id=job.id, timeout_s=1))
    assert res["status"] == "ok"
    assert res["state"] == "ok"


def test_wait_job_tool_does_not_block_event_loop(env_sandbox, repo):
    p = paths()
    job = new_job(p, "implement", datetime.now(timezone.utc))
    job.write_json("result.json", {"id": job.id, "status": "running"})

    server = build_server()

    async def run_test():
        ticks = 0
        stop = False

        async def heartbeat():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.01)

        hb_task = asyncio.create_task(heartbeat())
        try:
            tool_res = await server.call_tool("wait_job", {"job_id": job.id, "timeout_s": 1})
            assert not tool_res.is_error
            data = json.loads(tool_res.content[0].text)
            assert data.get("state") == "running"
            assert ticks >= 5
        finally:
            stop = True
            await hb_task

    asyncio.run(run_test())


def test_plan_run_returns_at_once(env_sandbox, repo, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("AGENT_HUB_PLAN_POLL_S", "0.1")

    # Invalid plan fails validation like CLI
    bad_plan = tmp_path / "bad_plan.json"
    bad_plan.write_text("{}")
    with pytest.raises(PlanError):
        plan_run(str(bad_plan))

    plan_file = make_plan_file(tmp_path, repo)
    res = plan_run(str(plan_file))
    assert "plan" in res
    assert res["plan"] == "plan-1"
    assert "runner_pid" in res
    assert isinstance(res["runner_pid"], int)
    assert res["runner_pid"] > 0

    runner_pid = res["runner_pid"]
    try:
        os.kill(runner_pid, 0)
    except OSError:
        pytest.fail(f"runner process {runner_pid} is not running")
    finally:
        main(["plan", "cancel", str(plan_file), "-d", str(repo)])
        try:
            os.kill(runner_pid, 9)
        except OSError:
            pass


def test_plan_run_records_runner_pid_before_return(env_sandbox, repo, tmp_path, monkeypatch):
    from agenthub.procinfo import start_time

    real_popen = subprocess.Popen
    children = []

    def fake_popen(cmd, **kwargs):
        if "agenthub" not in cmd:
            return real_popen(cmd, **kwargs)
        child = real_popen([sys.executable, "-c", "import time; time.sleep(5)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    plan_file = make_plan_file(tmp_path, repo)
    plan_dir = plan_file.parent
    dead = real_popen(["true"])
    dead.wait()
    (plan_dir / "runner.pid").write_text(json.dumps({"pid": dead.pid, "start_time": 1}))
    assert _dead_runner_pid(plan_dir, {}) == dead.pid

    try:
        res = plan_run(str(plan_file))
        runner_pid = res["runner_pid"]
        assert runner_pid == children[0].pid
        recorded = json.loads((plan_dir / "runner.pid").read_text())
        assert recorded == {"pid": runner_pid, "start_time": start_time(runner_pid)}
        assert _dead_runner_pid(plan_dir, {}) is None
    finally:
        for child in children:
            child.kill()
            child.wait()


def test_plan_status_matches_cli(env_sandbox, repo, tmp_path):
    plan_file = make_plan_file(tmp_path, repo)

    # Before run, state.json not found
    with pytest.raises(UsageError, match="state.json not found"):
        plan_status(str(plan_file))

    env = {"FAKE_CLAUDE_SCENARIO": "handoff", "FAKE_AGY_SCENARIO": "handoff", "FAKE_CODEX_SCENARIO": "handoff"}
    res_run = run_cli_plan(env_sandbox, plan_file, "run", extra_env=env)
    assert res_run.returncode == 0

    # CLI status output
    res_cli = run_cli_plan(env_sandbox, plan_file, "status")
    assert res_cli.returncode == 0

    res_status = plan_status(str(plan_file))
    assert res_status["plan"] == "plan-1"
    assert "tasks" in res_status
    assert len(res_status["tasks"]) == 2

    # Verify per-task {name, status, attempts, branch, handoff}
    tasks_by_name = {t["name"]: t for t in res_status["tasks"]}
    for name in ["t1", "t2"]:
        assert name in tasks_by_name
        t = tasks_by_name[name]
        assert t["name"] == name
        assert t["status"] == "done"
        assert isinstance(t["attempts"], list)
        assert len(t["attempts"]) >= 1
        assert t["branch"] == f"orch/plan-1/{name}"
        assert t["handoff"] == f"handoffs/{name}.md"
        assert f"{name}: done" in res_cli.stdout
        assert f"branch=orch/plan-1/{name}" in res_cli.stdout

    # With trail=True
    res_trail = plan_status(str(plan_file), trail=True)
    assert "trail" in res_trail
    assert len(res_trail["trail"]) > 0
    first_row = res_trail["trail"][0]
    for field in ["time", "task", "phase", "decision", "reason", "evidence", "result"]:
        assert field in first_row
    assert any(r["decision"] == "task started" for r in res_trail["trail"])

    # Trail matches CLI trail output
    res_cli_trail = run_cli_plan(env_sandbox, plan_file, "status", extra_args=["--trail"])
    assert res_cli_trail.returncode == 0
    assert "task started" in res_cli_trail.stdout


def _stalled_plan(env_sandbox, repo, tmp_path, runner):
    plan_file = make_plan_file(tmp_path, repo)
    plan_dir = plan_file.parent
    (plan_dir / "state.json").write_text(json.dumps({"tasks": {"t1": {"status": "running"}}}))
    (plan_dir / "runner.pid").write_text(json.dumps(runner))
    return plan_file


def test_plan_wait_stalled_when_runner_dead(env_sandbox, repo, tmp_path):
    proc = subprocess.Popen(["true"])
    proc.wait()
    plan_file = _stalled_plan(env_sandbox, repo, tmp_path, {"pid": proc.pid, "start_time": 1})
    t0 = time.monotonic()
    res = plan_wait(str(plan_file), timeout_s=5)
    assert time.monotonic() - t0 < 3
    assert res["state"] == "stalled"
    assert str(proc.pid) in res["reason"]


def test_plan_wait_running_when_runner_alive(env_sandbox, repo, tmp_path):
    from agenthub.procinfo import start_time

    plan_file = _stalled_plan(env_sandbox, repo, tmp_path, {"pid": os.getpid(), "start_time": start_time(os.getpid())})
    assert plan_wait(str(plan_file), timeout_s=1)["state"] == "running"


def test_plan_wait_stalled_when_pid_reused(env_sandbox, repo, tmp_path):
    from agenthub.procinfo import start_time

    plan_file = _stalled_plan(
        env_sandbox, repo, tmp_path, {"pid": os.getpid(), "start_time": start_time(os.getpid()) + 1}
    )
    res = plan_wait(str(plan_file), timeout_s=5)
    assert res["state"] == "stalled"
    assert str(os.getpid()) in res["reason"]


@pytest.mark.parametrize("runner", [{"pid": os.getpid()}, {"pid": os.getpid(), "start_time": None}])
def test_plan_wait_running_when_start_time_missing(env_sandbox, repo, tmp_path, runner):
    plan_file = _stalled_plan(env_sandbox, repo, tmp_path, runner)
    assert plan_wait(str(plan_file), timeout_s=1)["state"] == "running"


def test_plan_stalled_when_tasks_empty(env_sandbox, repo, tmp_path):
    proc = subprocess.Popen(["true"])
    proc.wait()
    plan_file = make_plan_file(tmp_path, repo)
    plan_dir = plan_file.parent
    state_file = plan_dir / "state.json"
    state_file.write_text(json.dumps({"tasks": {}}))
    runner = {"pid": proc.pid, "start_time": 1}
    (plan_dir / "runner.pid").write_text(json.dumps(runner))
    res = _plan_stalled(plan_file, plan_dir, state_file, {"tasks": {}}, paths())
    assert res is not None
    assert res["state"] == "stalled"


def test_plan_status_includes_report_text(env_sandbox, repo, tmp_path):
    plan_file = make_plan_file(tmp_path, repo)
    plan_dir = plan_file.parent
    (plan_dir / "state.json").write_text(json.dumps({"tasks": {}}))
    report_text = "# Plan report: plan-1\n\n## Tasks\n- t1: done\n"
    (plan_dir / "report.md").write_text(report_text, encoding="utf-8")

    res = plan_status(str(plan_file))

    assert res["report"] == report_text
    assert res["plan"] == "plan-1"
    assert "trail" not in res


def test_plan_status_report_empty_when_absent(env_sandbox, repo, tmp_path):
    plan_file = make_plan_file(tmp_path, repo)
    plan_dir = plan_file.parent
    (plan_dir / "state.json").write_text(json.dumps({"tasks": {}}))
    assert not (plan_dir / "report.md").exists()

    res = plan_status(str(plan_file))

    assert res["report"] == ""
    assert res["plan"] == "plan-1"
    assert len(res["tasks"]) == 2


def test_plan_wait_bounded(env_sandbox, repo, tmp_path, monkeypatch):
    assert clamp_timeout(0) == 1
    assert clamp_timeout(-10) == 1
    assert clamp_timeout(50) == 50
    assert clamp_timeout(300) == 300
    assert clamp_timeout(500) == 300

    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("AGENT_HUB_PLAN_POLL_S", "0.1")

    plan_file = make_plan_file(tmp_path, repo)
    run_res = plan_run(str(plan_file))
    runner_pid = run_res["runner_pid"]

    try:
        # Bounded wait while running returns {"state": "running"}
        wait_res = plan_wait(str(plan_file), timeout_s=1)
        assert wait_res == {"state": "running"}
    finally:
        main(["plan", "cancel", str(plan_file), "-d", str(repo)])
        try:
            os.kill(runner_pid, 9)
        except OSError:
            pass

    # Now run to completion and test plan_wait returns final per-task statuses
    completed_dir = tmp_path / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    plan_file2 = make_plan_file(completed_dir, repo)
    env = {"FAKE_CLAUDE_SCENARIO": "handoff", "FAKE_AGY_SCENARIO": "handoff", "FAKE_CODEX_SCENARIO": "handoff"}
    res_run = run_cli_plan(env_sandbox, plan_file2, "run", extra_env=env)
    assert res_run.returncode == 0

    wait_done = plan_wait(str(plan_file2), timeout_s=10)
    assert wait_done["plan"] == "plan-1"
    assert "tasks" in wait_done
    assert all(t["status"] == "done" for t in wait_done["tasks"])


def test_plan_wait_tool_does_not_block_event_loop(env_sandbox, repo, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("AGENT_HUB_PLAN_POLL_S", "0.1")

    plan_file = make_plan_file(tmp_path, repo)
    run_res = plan_run(str(plan_file))
    runner_pid = run_res["runner_pid"]

    server = build_server()

    async def run_test():
        ticks = 0
        stop = False

        async def heartbeat():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.01)

        hb_task = asyncio.create_task(heartbeat())
        try:
            tool_res = await server.call_tool("plan_wait", {"plan_path": str(plan_file), "timeout_s": 1})
            assert not tool_res.is_error
            data = json.loads(tool_res.content[0].text)
            assert data.get("state") == "running"
            assert ticks >= 5
        finally:
            stop = True
            await hb_task

    try:
        asyncio.run(run_test())
    finally:
        main(["plan", "cancel", str(plan_file), "-d", str(repo)])
        try:
            os.kill(runner_pid, 9)
        except OSError:
            pass


def test_plan_review_cli_result_file(env_sandbox, tmp_path):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    result_file = tmp_path / "review-result.json"
    rc = main(["plan", "review", str(plan_file), "--result", str(result_file)])
    assert result_file.is_file()
    data = json.loads(result_file.read_text())
    assert data["exit_code"] == rc
    assert data["status"] == ("ok" if rc == 0 else "failed")
    assert isinstance(data["lines"], list)
    assert len(data["lines"]) == 2
    assert "stderr_tail" in data
    assert isinstance(data["stderr_tail"], str)

    # Test error exit (e.g. no done task named nope)
    err_result = tmp_path / "err-result.json"
    err_rc = main(["plan", "review", str(plan_file), "--task", "nope", "--result", str(err_result)])
    assert err_rc == 2
    assert err_result.is_file()
    err_data = json.loads(err_result.read_text())
    assert err_data["exit_code"] == 2
    assert err_data["status"] == "failed"
    assert "no done implement task named nope" in err_data["stderr_tail"]


def test_plan_review_tool_returns_handle(env_sandbox, tmp_path):
    with pytest.raises(UsageError):
        plan_review("")

    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)

    with pytest.raises(UsageError, match="no done implement task named nope"):
        plan_review(str(plan_file), task="nope")

    res = plan_review(str(plan_file))
    assert "handle" in res
    handle = res["handle"]
    assert isinstance(handle, str)
    assert len(handle) > 0
    assert "plan" in res
    assert res["plan"] == "plan-1"

    wait_res = plan_review_wait(handle, timeout_s=30)
    assert "lines" in wait_res
    assert len(wait_res["lines"]) == 2
    for line in wait_res["lines"]:
        assert "gate=" in line and "review=" in line and "job=" in line

    wait_res_async = asyncio.run(plan_review_wait_async(handle, timeout_s=30))
    assert wait_res_async["lines"] == wait_res["lines"]
    assert wait_res_async["state"] == wait_res["state"]

    server = build_server()
    tool_res = asyncio.run(server.call_tool("plan_review", {"plan_path": str(plan_file), "task": "t1"}))
    assert not tool_res.is_error
    data = json.loads(tool_res.content[0].text)
    assert "handle" in data
    wait_data = asyncio.run(server.call_tool("plan_review_wait", {"handle": data["handle"], "timeout_s": 30}))
    assert not wait_data.is_error
    wdata = json.loads(wait_data.content[0].text)
    assert "lines" in wdata
    assert len(wdata["lines"]) == 1
    assert "t1" in wdata["lines"][0]


def test_plan_review_result_survives_server_exit(env_sandbox, tmp_path):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    res = plan_review(str(plan_file))
    assert "handle" in res
    handle = res["handle"]

    p = paths()
    review_file = p.state_dir / "mcp" / "plan-review" / f"{handle}.json"
    assert review_file.is_file()

    # The child writes the result file directly; wait for it
    wait_res = plan_review_wait(handle, timeout_s=30)
    assert wait_res["state"] in ("ok", "failed")
    assert "lines" in wait_res
    assert len(wait_res["lines"]) == 2

    # A separate/new call reads the result directly without needing any daemon thread
    wait_res2 = plan_review_wait(handle, timeout_s=5)
    assert wait_res2["lines"] == wait_res["lines"]
    assert wait_res2["state"] == wait_res["state"]
    assert "stderr_tail" in wait_res2


def test_plan_review_handle_traversal_rejected(env_sandbox, tmp_path):
    for bad_handle in ["../../x", "a/b", "..", "/etc/passwd", "/tmp/x"]:
        with pytest.raises(KeyError):
            plan_review_wait(bad_handle)

    for empty_handle in ["", "   "]:
        with pytest.raises(UsageError):
            plan_review_wait(empty_handle)


def test_plan_review_no_done_tasks_reason(env_sandbox, tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    plan_file = make_plan_file(tmp_path, repo)
    plan_dir = plan_file.parent
    (plan_dir / "state.json").write_text(
        json.dumps({"tasks": {"t1": {"status": "pending"}, "t2": {"status": "pending"}}})
    )

    orig_popen = subprocess.Popen

    # Ensure review subprocess is NOT spawned
    def fail_popen(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if any("review" in str(c) for c in cmd):
            raise AssertionError("should not spawn when no tasks are done")
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    res = plan_review(str(plan_file))
    assert res.get("reason") == "no done tasks to review"
    assert "runner_pid" not in res
    assert "handle" not in res

    # Also test via MCP tool call
    server = build_server()
    tool_res = asyncio.run(server.call_tool("plan_review", {"plan_path": str(plan_file)}))
    assert not tool_res.is_error
    data = json.loads(tool_res.content[0].text)
    assert data.get("reason") == "no done tasks to review"
    assert "runner_pid" not in data
    assert "handle" not in data


def test_plan_wait_ignores_review_prefix(env_sandbox, tmp_path):
    # plan_wait treats review-* as a plan path and does not attempt review handle resolution
    with pytest.raises((UsageError, PlanError)):
        plan_wait("review-nonexistent.json")

    p = paths()
    review_dir = p.state_dir / "mcp" / "plan-review"
    review_dir.mkdir(parents=True, exist_ok=True)
    fake_review = review_dir / "review-fake.json"
    fake_review.write_text(json.dumps({"status": "ok", "lines": ["fake"]}))

    with pytest.raises((UsageError, PlanError)):
        plan_wait("review-fake")


def test_plan_review_spawn_failure_closes_files(env_sandbox, tmp_path, monkeypatch):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)

    opened = []
    orig_open = open

    def tracking_open(*args, **kwargs):
        f = orig_open(*args, **kwargs)
        opened.append(f)
        return f

    orig_popen = subprocess.Popen

    def fail_popen(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if any("review" in str(c) for c in cmd):
            raise OSError("mocked spawn failure")
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking_open)
    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    res = plan_review(str(plan_file))
    assert "spawn failed" in res.get("error", "")

    assert len(opened) > 0
    for f in opened:
        assert f.closed


def test_plan_merge_tool_refuses_dirty_checkout(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()
    repo = tmp_path / "repo"
    head_before = subprocess.run(
        ["git", "rev-parse", "master"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "stray.txt").write_text("uncommitted\n")

    with pytest.raises(UsageError, match="main checkout is not clean"):
        plan_merge(str(plan_file))

    head_after = subprocess.run(
        ["git", "rev-parse", "master"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_before == head_after


def test_plan_merge_tool_is_destructive_and_merges_ready(env_sandbox, tmp_path, monkeypatch, capsys):
    server = build_server()
    tools = asyncio.run(server.list_tools())
    tool_map = {t.name: t for t in tools}
    assert tool_map["plan_merge"].annotations is not None
    assert tool_map["plan_merge"].annotations.destructive_hint is True

    plan_file, state = make_gated_plan_run(env_sandbox, tmp_path)
    t2_file = Path(state["tasks"]["t2"]["worktree"]) / "t2.txt"
    t2_file.write_text("t2 work\n")
    subprocess.run(["git", "add", "t2.txt"], cwd=t2_file.parent, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "t2 work"], cwd=t2_file.parent, check=True)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file)]) == 0
    capsys.readouterr()
    repo = tmp_path / "repo"

    res = plan_merge(str(plan_file))
    assert res["merged"] == ["t1", "t2"]
    assert res["stopped_at"] is None

    log_res = subprocess.run(
        ["git", "log", "--format=%s", "-n", "2"], cwd=repo, capture_output=True, text=True, check=True
    )
    subjects = log_res.stdout.splitlines()
    assert len(subjects) == 2
    assert "Merge orch/plan-1/t2" in subjects[0]
    assert "Merge orch/plan-1/t1" in subjects[1]
    assert (repo / "agent_wrote.txt").is_file()
    assert (repo / "t2.txt").is_file()

    for name in ("t1", "t2"):
        assert not Path(state["tasks"][name]["worktree"]).exists()
        assert subprocess.run(["git", "show-ref", f"refs/heads/orch/plan-1/{name}"], cwd=repo).returncode != 0

    p2 = tmp_path / "p2"
    p2.mkdir()
    plan_file2, state2 = make_gated_plan_run(env_sandbox, p2)
    fake_review(monkeypatch, "approve")
    assert main(["plan", "review", str(plan_file2)]) == 0
    capsys.readouterr()
    t1_worktree = state2["tasks"]["t1"]["worktree"]
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "after review"], cwd=t1_worktree, check=True)

    res2 = plan_merge(str(plan_file2))
    assert res2["merged"] == []
    assert res2["stopped_at"] is not None
    assert res2["stopped_at"]["task"] == "t1"
    assert res2["stopped_at"]["status"] == "not-ready"
    assert len(res2["stopped_at"]["problems"]) > 0


def _review_files(handle):
    d = paths().state_dir / "mcp" / "plan-review"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{handle}.json", d / f"{handle}.stderr"


def test_reap_review_marks_dead_runner_failed(env_sandbox):
    handle = "review-x-1"
    result_file, stderr_file = _review_files(handle)
    result_file.write_text(json.dumps({"handle": handle, "plan": "x", "pid": 1, "status": "running"}))
    with open(stderr_file, "w") as err:
        proc = subprocess.Popen(["sh", "-c", "echo boom >&2; exit 3"], stderr=err)
    _reap_review(proc, result_file, stderr_file)
    data = json.loads(result_file.read_text())
    assert data["status"] == "failed"
    assert data["exit_code"] == 3
    assert data["lines"] == []
    assert "boom" in data["stderr_tail"]
    assert data["handle"] == handle
    res = plan_review_wait(handle, timeout_s=2)
    assert res["state"] == "failed"


def test_reap_review_keeps_finished_result(env_sandbox):
    handle = "review-x-2"
    result_file, stderr_file = _review_files(handle)
    original = {"status": "ok", "exit_code": 0, "lines": ["x"], "stderr_tail": ""}
    result_file.write_text(json.dumps(original))
    stderr_file.write_text("")
    proc = subprocess.Popen(["true"])
    _reap_review(proc, result_file, stderr_file)
    assert json.loads(result_file.read_text()) == original


def test_plan_review_wait_detects_zombie_runner(env_sandbox):
    handle = "review-x-3"
    result_file, _ = _review_files(handle)
    proc = subprocess.Popen(["true"])
    try:
        result_file.write_text(json.dumps({"handle": handle, "plan": "x", "pid": proc.pid, "status": "running"}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            text = Path(f"/proc/{proc.pid}/stat").read_text()
            if text[text.rfind(")") + 1 :].split()[0] == "Z":
                break
            time.sleep(0.02)
        res = plan_review_wait(handle, timeout_s=3)
        assert res["state"] == "failed"
    finally:
        proc.wait()


def test_plan_triage_spawns_cli_detached(env_sandbox, repo, tmp_path, monkeypatch):
    plan_file = make_plan_file(tmp_path, repo)

    calls = []
    orig_popen = subprocess.Popen

    class DummyProc:
        def __init__(self, pid=42424):
            self.pid = pid

        def wait(self):
            return 0

    def fake_popen(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if any("triage" in str(c) for c in cmd):
            calls.append({"cmd": cmd, "kwargs": kwargs})
            return DummyProc()
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    bad_plan = tmp_path / "bad_plan.json"
    bad_plan.write_text("{}")
    with pytest.raises(PlanError):
        plan_triage(str(bad_plan))

    res = plan_triage(str(plan_file))
    assert res == {"plan": "plan-1", "runner_pid": 42424}
    assert len(calls) == 1
    call = calls[0]
    expected_cmd = [
        sys.executable,
        "-P",
        "-m",
        "agenthub",
        "plan",
        "triage",
        str(plan_file),
    ]
    assert call["cmd"] == expected_cmd
    assert call["kwargs"]["start_new_session"] is True
    assert call["kwargs"]["stdin"] == subprocess.DEVNULL
    assert call["kwargs"]["stderr"] == subprocess.STDOUT
    assert "PYTHONPATH" in call["kwargs"]["env"]

    def fail_popen(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if any("triage" in str(c) for c in cmd):
            raise OSError("failed to exec")
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    err_res = plan_triage(str(plan_file))
    assert "error" in err_res
    assert "spawn failed: failed to exec" in err_res["error"]
    assert "log" in err_res


def test_plan_close_passes_rounds(env_sandbox, repo, tmp_path, monkeypatch):
    plan_file = make_plan_file(tmp_path, repo)

    calls = []
    orig_popen = subprocess.Popen

    class DummyProc:
        def __init__(self, pid=42425):
            self.pid = pid

        def wait(self):
            return 0

    def fake_popen(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if any("close" in str(c) for c in cmd):
            calls.append({"cmd": cmd, "kwargs": kwargs})
            return DummyProc()
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    bad_plan = tmp_path / "bad_plan.json"
    bad_plan.write_text("{}")
    with pytest.raises(PlanError):
        plan_close(str(bad_plan))

    res = plan_close(str(plan_file))
    assert res == {"plan": "plan-1", "runner_pid": 42425}
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        sys.executable,
        "-P",
        "-m",
        "agenthub",
        "plan",
        "close",
        str(plan_file),
        "--rounds",
        "3",
    ]
    assert calls[0]["kwargs"]["start_new_session"] is True

    res2 = plan_close(str(plan_file), rounds=5)
    assert res2 == {"plan": "plan-1", "runner_pid": 42425}
    assert len(calls) == 2
    assert calls[1]["cmd"] == [
        sys.executable,
        "-P",
        "-m",
        "agenthub",
        "plan",
        "close",
        str(plan_file),
        "--rounds",
        "5",
    ]
    assert calls[1]["kwargs"]["start_new_session"] is True

    def fail_popen(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if any("close" in str(c) for c in cmd):
            raise OSError("exec format error")
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    err_res = plan_close(str(plan_file))
    assert "error" in err_res
    assert "spawn failed: exec format error" in err_res["error"]
    assert "log" in err_res


def test_plan_close_rejects_bad_rounds(env_sandbox, repo, tmp_path):
    plan_file = make_plan_file(tmp_path, repo)

    for bad in [0, -1, -5, "3", "bad", True, False, None, 2.5]:
        with pytest.raises(UsageError):
            plan_close(str(plan_file), rounds=bad)


def test_new_tools_are_registered_and_not_read_only(env_sandbox):
    server = build_server()
    tools = asyncio.run(server.list_tools())
    tool_map = {t.name: t for t in tools}

    assert "plan_triage" in tool_map
    assert "plan_close" in tool_map

    triage_tool = tool_map["plan_triage"]
    assert triage_tool.annotations is None or triage_tool.annotations.read_only_hint is not True

    close_tool = tool_map["plan_close"]
    assert close_tool.annotations is None or close_tool.annotations.read_only_hint is not True
