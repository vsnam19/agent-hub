import asyncio
import json
import sys
from datetime import datetime, timezone

import pytest

from agenthub import guards
from agenthub.cli import main
from agenthub.config import paths
from agenthub.jobs import new_job
from agenthub.mcpserver import build_server, jobs_data, pool_data, show_job_data
from tests.gitrepo import make_repo


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
    assert set(tool_map.keys()) == {"pool", "jobs", "show_job"}
    for name in ["pool", "jobs", "show_job"]:
        tool = tool_map[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True

    pool_res = asyncio.run(server.call_tool("pool", {}))
    assert not pool_res.is_error
    assert len(pool_res.content) == 1
