import os
from datetime import datetime, timedelta, timezone

import pytest

from agenthub.agents import Outcome, Request
from agenthub.agents.codex import CodexAdapter
from agenthub.process import run


def test_codex_build_implement_argv(tmp_path):
    adapter = CodexAdapter()
    adapter.config = type("C", (), {"binary": "codex"})()

    brief = tmp_path / "b.md"
    brief.write_text("hello")
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    req = Request(
        role="implement",
        model="m",
        workdir=tmp_path,
        brief_path=brief,
        attempt_dir=attempt,
        git_common_dir=tmp_path / ".git",
    )

    inv = adapter.build(req)
    assert inv.argv == [
        "codex",
        "exec",
        "-m",
        "m",
        "-C",
        str(tmp_path),
        "--json",
        "-o",
        str(attempt / "reply.md"),
        "--skip-git-repo-check",
        "-s",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "--add-dir",
        str(tmp_path / ".git"),
    ]
    assert inv.stdin_path == brief


def test_codex_build_review_argv(tmp_path):
    adapter = CodexAdapter()
    adapter.config = type("C", (), {"binary": "codex"})()

    brief = tmp_path / "b.md"
    brief.write_text("hello")
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    req = Request(
        role="review",
        model="m",
        workdir=tmp_path,
        brief_path=brief,
        attempt_dir=attempt,
        git_common_dir=tmp_path / ".git",
    )

    inv = adapter.build(req)
    assert inv.argv == [
        "codex",
        "exec",
        "-m",
        "m",
        "-C",
        str(tmp_path),
        "--json",
        "-o",
        str(attempt / "reply.md"),
        "--skip-git-repo-check",
        "-s",
        "read-only",
    ]
    assert inv.stdin_path == brief


def run_fake(env_sandbox, adapter, scenario, req):
    os.environ["FAKE_CODEX_SCENARIO"] = scenario
    inv = adapter.build(req)

    ran = run(
        inv=inv, cwd=req.workdir, env=os.environ.copy(), attempt_dir=req.attempt_dir, timeout=timedelta(seconds=5)
    )
    return adapter.classify(ran.rc, req.attempt_dir)


@pytest.fixture
def req_codex(tmp_path):
    brief = tmp_path / "b.md"
    brief.write_text("hello")
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    return Request(
        role="implement",
        model="m",
        workdir=tmp_path,
        brief_path=brief,
        attempt_dir=attempt,
        git_common_dir=tmp_path / ".git",
    )


@pytest.fixture
def adapter_codex():
    a = CodexAdapter()
    a.config = type("C", (), {"binary": "codex"})()
    return a


def test_codex_classify_ok(env_sandbox, adapter_codex, req_codex):
    c = run_fake(env_sandbox, adapter_codex, "ok", req_codex)
    assert c.outcome == Outcome.OK
    assert c.reply == "done"


def test_codex_classify_quota(env_sandbox, adapter_codex, req_codex):
    c = run_fake(env_sandbox, adapter_codex, "quota", req_codex)
    assert c.outcome == Outcome.QUOTA


def test_codex_quota_reset_time_parsed(env_sandbox, adapter_codex, req_codex):
    now = datetime.now(timezone.utc)
    c = run_fake(env_sandbox, adapter_codex, "quota-reset", req_codex)
    assert c.outcome == Outcome.QUOTA
    assert c.reset_at is not None
    diff = c.reset_at - now
    # 3600s
    assert abs(diff.total_seconds() - 3600) < 10


def test_codex_classify_auth(env_sandbox, adapter_codex, req_codex):
    c = run_fake(env_sandbox, adapter_codex, "auth", req_codex)
    assert c.outcome == Outcome.AUTH


def test_codex_classify_model_unavailable(env_sandbox, adapter_codex, req_codex):
    c = run_fake(env_sandbox, adapter_codex, "model", req_codex)
    assert c.outcome == Outcome.MODEL_UNAVAILABLE


def test_codex_classify_empty(env_sandbox, adapter_codex, req_codex):
    c = run_fake(env_sandbox, adapter_codex, "empty", req_codex)
    assert c.outcome == Outcome.EMPTY


def test_codex_classify_fail(env_sandbox, adapter_codex, req_codex):
    c = run_fake(env_sandbox, adapter_codex, "fail", req_codex)
    assert c.outcome == Outcome.FAILED


def test_codex_reply_mentioning_quota_is_ok(env_sandbox, adapter_codex, req_codex):
    c = run_fake(env_sandbox, adapter_codex, "ok-quota-text", req_codex)
    assert c.outcome == Outcome.OK
    assert c.reply == "quota exceeded is mentioned here"


def test_codex_file_not_found_is_not_model(tmp_path, adapter_codex):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    (attempt / "stderr").write_text("config file not found; using default model")
    c = adapter_codex.classify(2, attempt)
    assert c.outcome == Outcome.FAILED
