import os
from datetime import timedelta

import pytest

from agenthub.agents import Outcome, Request, registry
from agenthub.agents.claude import ClaudeAdapter
from agenthub.process import run


def test_claude_build_implement_argv(tmp_path):
    adapter = ClaudeAdapter()
    adapter.config = type("C", (), {"binary": "claude"})()

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
        "claude",
        "-p",
        "--model",
        "m",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--permission-mode",
        "bypassPermissions",
    ]
    assert inv.stdin_path == brief


def test_claude_build_review_argv(tmp_path):
    adapter = ClaudeAdapter()
    adapter.config = type("C", (), {"binary": "claude"})()

    brief = tmp_path / "b.md"
    brief.write_text("hello")
    attempt = tmp_path / "attempts" / "1-claude-m"
    attempt.mkdir(parents=True)

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
        "claude",
        "-p",
        "--model",
        "m",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--allowedTools",
        "Read,Grep,Glob",
        "--add-dir",
        str(tmp_path),
    ]
    assert inv.stdin_path == brief
    for arg in inv.argv:
        assert '"' not in arg


def test_claude_review_has_no_shell_tool(tmp_path):
    adapter = ClaudeAdapter()
    adapter.config = type("C", (), {"binary": "claude"})()
    brief = tmp_path / "b.md"
    brief.write_text("hello")
    attempt = tmp_path / "attempts" / "1-claude-m"
    attempt.mkdir(parents=True)
    req = Request(
        role="review",
        model="m",
        workdir=tmp_path,
        brief_path=brief,
        attempt_dir=attempt,
        git_common_dir=tmp_path / ".git",
    )
    argv = adapter.build(req).argv
    assert argv
    assert not any("Bash" in a for a in argv)


def run_fake(env_sandbox, adapter, scenario, req):
    os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
    inv = adapter.build(req)

    ran = run(
        inv=inv, cwd=req.workdir, env=os.environ.copy(), attempt_dir=req.attempt_dir, timeout=timedelta(seconds=5)
    )
    return adapter.classify(ran.rc, req.attempt_dir)


@pytest.fixture
def req_claude(tmp_path):
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
def adapter_claude():
    a = ClaudeAdapter()
    a.config = type("C", (), {"binary": "claude"})()
    return a


def test_claude_classify_ok(env_sandbox, adapter_claude, req_claude):
    c = run_fake(env_sandbox, adapter_claude, "ok", req_claude)
    assert c.outcome == Outcome.OK
    assert c.reply == "done"


def test_claude_classify_quota(env_sandbox, adapter_claude, req_claude):
    c = run_fake(env_sandbox, adapter_claude, "quota", req_claude)
    assert c.outcome == Outcome.QUOTA


def test_claude_quota_reset_time_parsed(env_sandbox, adapter_claude, req_claude):
    c = run_fake(env_sandbox, adapter_claude, "quota-reset", req_claude)
    assert c.outcome == Outcome.QUOTA
    assert c.reset_at is not None
    assert c.reset_at.timestamp() == 1760000000


def test_claude_classify_auth(env_sandbox, adapter_claude, req_claude):
    c = run_fake(env_sandbox, adapter_claude, "auth", req_claude)
    assert c.outcome == Outcome.AUTH


def test_claude_classify_model_unavailable(env_sandbox, adapter_claude, req_claude):
    c = run_fake(env_sandbox, adapter_claude, "model", req_claude)
    assert c.outcome == Outcome.MODEL_UNAVAILABLE


def test_claude_classify_empty(env_sandbox, adapter_claude, req_claude):
    c = run_fake(env_sandbox, adapter_claude, "empty", req_claude)
    assert c.outcome == Outcome.EMPTY


def test_claude_classify_fail(env_sandbox, adapter_claude, req_claude):
    c = run_fake(env_sandbox, adapter_claude, "fail", req_claude)
    assert c.outcome == Outcome.FAILED


def test_claude_reply_mentioning_quota_is_ok(env_sandbox, adapter_claude, req_claude):
    c = run_fake(env_sandbox, adapter_claude, "ok-quota-text", req_claude)
    assert c.outcome == Outcome.OK
    assert c.reply == "quota exceeded is mentioned here"


def test_fake_records_call(env_sandbox, adapter_claude, req_claude):
    from tests.conftest import fake_calls

    os.environ["GH_TOKEN"] = "sekret"
    os.environ["CLAUDECODE"] = "1"
    run_fake(env_sandbox, adapter_claude, "ok", req_claude)

    calls = fake_calls(env_sandbox.fakelog, "claude")
    assert len(calls) == 1
    call = calls[0]
    assert call["stdin"] == "hello"
    assert call["cwd"] == str(req_claude.workdir)
    assert "GH_TOKEN" in call["env"]
    assert call["env"]["GH_TOKEN"] == "sekret"


def test_registry_finds_three_agents():
    import agenthub.agents

    agenthub.agents._REGISTRY_CACHE = None
    reg = registry()
    assert set(reg.keys()) == {"agy", "codex", "claude"}
    for _name, adapter in reg.items():
        assert adapter.profile_path.exists()
        lines = adapter.profile_path.read_text().splitlines()
        assert len(lines) <= 40


def test_claude_file_not_found_is_not_model(tmp_path, adapter_claude):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    (attempt / "stderr").write_text("config file not found; using default model")
    c = adapter_claude.classify(2, attempt)
    assert c.outcome == Outcome.FAILED
