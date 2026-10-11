import json
import subprocess
from datetime import datetime, timedelta, timezone

from agenthub.cli import main
from agenthub.config import Pool, paths
from agenthub.jobs import new_job
from agenthub.orchestrate import RunArgs, run_job
from agenthub.pool import parse_link

BRIEF = "## Input\n\nfoo\n\n## Expected output\n\nbar\n"


def git_head(repo):
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def commit(repo, name):
    (repo / name).write_text("x\n")
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", name], cwd=repo, check=True)


def add_job(repo, kind, head, *, model="m1", agent="codex", verdict=None, gate_sha=None, n=0):
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
    }
    job.write_json("result.json", record)


def ready(repo, capsys):
    rc = main(["ready", "-d", str(repo), "--json"])
    return rc, json.loads(capsys.readouterr().out)


def test_ready_when_gated_and_approved_on_head(env_sandbox, repo, capsys):
    head = git_head(repo)
    add_job(repo, "implement", head, model="m1", n=1)
    add_job(repo, "review", head, model="m2", verdict="approve", n=2)
    rc, out = ready(repo, capsys)
    assert rc == 0
    assert out == {"ready": True, "head": head, "problems": []}


def test_not_ready_review_stale_after_commit(env_sandbox, repo, capsys):
    old = git_head(repo)
    add_job(repo, "implement", old, model="m1", n=1)
    add_job(repo, "review", old, model="m2", verdict="approve", n=2)
    commit(repo, "late.txt")
    new = git_head(repo)
    rc, out = ready(repo, capsys)
    assert rc == 1
    assert f"no gated implement job on {new}" in out["problems"]
    assert f"review is stale (reviewed {old})" in out["problems"]


def test_not_ready_request_changes(env_sandbox, repo, capsys):
    head = git_head(repo)
    add_job(repo, "implement", head, model="m1", n=1)
    add_job(repo, "review", head, model="m2", verdict="request-changes", n=2)
    rc, out = ready(repo, capsys)
    assert rc == 1
    assert out["problems"] == ["review verdict request-changes"]


def test_not_ready_reviewer_is_implementer(env_sandbox, repo, capsys):
    head = git_head(repo)
    add_job(repo, "implement", head, model="m1", n=1)
    add_job(repo, "review", head, model="m1", verdict="approve", n=2)
    rc, out = ready(repo, capsys)
    assert rc == 1
    assert out["problems"] == ["reviewer is the implementer"]


def test_result_records_head_and_gate_sha(env_sandbox, repo, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "write")
    pool = Pool(roles={}, quota_cooldown=timedelta(minutes=60), timeouts={})
    args = RunArgs(
        role="implement",
        brief=BRIEF,
        workdir=repo,
        links=[parse_link("agy:m1")],
        timeout=timedelta(seconds=30),
        base=None,
        gates=["test -f agent_wrote.txt"],
        jev=False,
        allow=[],
    )
    result = run_job(args, paths(), pool, lambda: datetime.now(timezone.utc))
    assert result["status"] == "ok"
    assert result["head"] == git_head(repo)
    assert result["gate"][0]["sha"] == result["head"]


def test_ready_with_gate_job_after_commit(env_sandbox, repo, capsys):
    old = git_head(repo)
    add_job(repo, "implement", old, model="m1", n=1)
    commit(repo, "late.txt")
    new = git_head(repo)
    add_job(repo, "gate", new, model="gate", agent="agent-hub", n=2)
    add_job(repo, "review", new, model="m2", verdict="approve", n=3)
    rc, out = ready(repo, capsys)
    assert rc == 0
    assert out == {"ready": True, "head": new, "problems": []}


def test_ready_reviewer_is_implementer_with_gate_job(env_sandbox, repo, capsys):
    old = git_head(repo)
    add_job(repo, "implement", old, model="m1", n=1)
    commit(repo, "late.txt")
    new = git_head(repo)
    add_job(repo, "gate", new, model="gate", agent="agent-hub", n=2)
    add_job(repo, "review", new, model="m1", verdict="approve", n=3)
    rc, out = ready(repo, capsys)
    assert rc == 1
    assert out["problems"] == ["reviewer is the implementer"]
