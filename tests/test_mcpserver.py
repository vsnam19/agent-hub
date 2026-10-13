import asyncio
import json
import sys
import time
from datetime import datetime, timezone

import pytest

from agenthub import guards
from agenthub.brief import BriefError
from agenthub.cli import UsageError, main
from agenthub.config import ConfigError, paths
from agenthub.jobs import get_job, new_job
from agenthub.mcpserver import (
    build_server,
    clamp_timeout,
    jobs_data,
    pool_data,
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


def test_server_lists_tools(env_sandbox):
    server = build_server()
    tools = asyncio.run(server.list_tools())
    tool_map = {t.name: t for t in tools}
    assert set(tool_map.keys()) == {"pool", "jobs", "show_job", "run_job", "wait_job"}
    for name in ["pool", "jobs", "show_job", "wait_job"]:
        tool = tool_map[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    assert tool_map["run_job"].annotations is None or tool_map["run_job"].annotations.read_only_hint is not True

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

        wait_dir_res = wait_job(workdir=str(repo), timeout_s=1)
        assert wait_dir_res == {"state": "running"}
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

    wait_dir_res = wait_job(workdir=str(repo), timeout_s=5)
    assert wait_dir_res["status"] == "ok"
    assert wait_dir_res["id"] == job_id


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
        wait_job()

    with pytest.raises(KeyError):
        wait_job(job_id="nonexistent-id", timeout_s=1)

    with pytest.raises(UsageError):
        wait_job(workdir=str(repo / "nonexistent-dir"), timeout_s=1)
