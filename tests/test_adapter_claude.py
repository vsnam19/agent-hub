import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from agenthub.agents import Outcome, Request, registry
from agenthub.agents.claude import WORKER_PROMPT, ClaudeAdapter
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
        "--setting-sources",
        "project,local",
        "--append-system-prompt",
        WORKER_PROMPT,
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
        "--setting-sources",
        "project,local",
        "--append-system-prompt",
        WORKER_PROMPT,
        "--tools",
        "Read,Grep,Glob,Skill",
        "--allowedTools",
        "Read,Grep,Glob,Skill",
        "--strict-mcp-config",
        "--add-dir",
        str(tmp_path),
    ]
    assert inv.stdin_path == brief
    for arg in inv.argv:
        assert '"' not in arg


def test_review_argv_allows_only_read_grep_glob_skill(tmp_path):
    argv = _build_for(tmp_path, "review")
    tools = argv[argv.index("--tools") + 1].split(",")
    assert tools == ["Read", "Grep", "Glob", "Skill"]
    assert "--permission-mode" not in argv


def test_review_argv_loads_no_mcp_server(tmp_path):
    argv = _build_for(tmp_path, "review")
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv


def test_implement_argv_unchanged_by_review_restriction(tmp_path):
    argv = _build_for(tmp_path, "implement")
    assert "--tools" not in argv
    assert "--allowedTools" not in argv
    assert "--strict-mcp-config" not in argv
    assert argv[-2:] == ["--permission-mode", "bypassPermissions"]


def _build_for(tmp_path, role):
    adapter = ClaudeAdapter()
    adapter.config = type("C", (), {"binary": "claude"})()
    brief = tmp_path / "b.md"
    brief.write_text("hello")
    attempt = tmp_path / "attempts" / "1-claude-m"
    attempt.mkdir(parents=True)
    req = Request(
        role=role,
        model="m",
        workdir=tmp_path,
        brief_path=brief,
        attempt_dir=attempt,
        git_common_dir=tmp_path / ".git",
    )
    return adapter.build(req).argv


@pytest.mark.parametrize("role", ["implement", "review"])
def test_claude_ignores_user_settings(tmp_path, role):
    argv = _build_for(tmp_path, role)
    i = argv.index("--setting-sources")
    assert argv[i + 1] == "project,local"


@pytest.mark.parametrize("role", ["implement", "review"])
def test_claude_worker_prompt_appended(tmp_path, role):
    argv = _build_for(tmp_path, role)
    i = argv.index("--append-system-prompt")
    assert argv[i + 1] == WORKER_PROMPT
    assert "English" in WORKER_PROMPT
    assert "background" in WORKER_PROMPT


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


def test_registry_finds_four_agents():
    import agenthub.agents

    agenthub.agents._REGISTRY_CACHE = None
    reg = registry()
    assert set(reg.keys()) == {"agy", "claude", "codex", "gwen"}
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


def test_claude_build_adds_plugin_dirs(tmp_path):
    adapter = ClaudeAdapter()
    adapter.config = type("C", (), {"binary": "claude"})()

    brief = tmp_path / "b.md"
    brief.write_text("hello")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    telaf = tmp_path / "telaf"
    aspice = tmp_path / "aspice"

    req = Request(
        role="review",
        model="m",
        workdir=tmp_path,
        brief_path=brief,
        attempt_dir=attempt,
        git_common_dir=tmp_path / ".git",
        plugin_dirs=(telaf, aspice),
    )

    argv = adapter.build(req).argv
    assert argv[-4:] == ["--plugin-dir", str(telaf), "--plugin-dir", str(aspice)]
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob,Skill"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,Skill"
    assert "--strict-mcp-config" in argv


def _classify_stdout(tmp_path, adapter, payload, rc=1):
    attempt = tmp_path / "attempt"
    attempt.mkdir(parents=True)
    (attempt / "stdout").write_text(json.dumps(payload))
    return adapter.classify(rc, attempt)


def _limit_payload(result, **extra):
    payload = {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error"}
    payload["result"] = result
    payload.update(extra)
    return payload


def test_claude_session_limit_is_quota(tmp_path, adapter_claude):
    c = _classify_stdout(
        tmp_path,
        adapter_claude,
        _limit_payload("You've hit your session limit · resets 12:20pm (Asia/Ho_Chi_Minh)", api_error_status=429),
    )
    assert c.outcome == Outcome.QUOTA

    c = _classify_stdout(tmp_path / "second", adapter_claude, _limit_payload("You've hit your weekly limit"))
    assert c.outcome == Outcome.QUOTA


def test_claude_session_limit_reset_time_parsed(tmp_path, adapter_claude, monkeypatch):
    text = "You've hit your session limit · resets 12:20pm (Asia/Ho_Chi_Minh)"

    # 09:00 in Asia/Ho_Chi_Minh (UTC+7): 12:20pm is still ahead today, i.e. 05:20 UTC.
    monkeypatch.setattr("agenthub.agents.claude._now", lambda: datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc))
    c = _classify_stdout(tmp_path / "before", adapter_claude, _limit_payload(text, api_error_status=429))
    assert c.outcome == Outcome.QUOTA
    assert c.reset_at == datetime(2026, 10, 12, 5, 20, tzinfo=timezone.utc)

    # 17:00 in Asia/Ho_Chi_Minh: 12:20pm has passed, so the reset is tomorrow at 05:20 UTC.
    monkeypatch.setattr("agenthub.agents.claude._now", lambda: datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc))
    c = _classify_stdout(tmp_path / "after", adapter_claude, _limit_payload(text, api_error_status=429))
    assert c.outcome == Outcome.QUOTA
    assert c.reset_at == datetime(2026, 10, 13, 5, 20, tzinfo=timezone.utc)


def test_claude_429_without_text_is_quota(tmp_path, adapter_claude):
    c = _classify_stdout(tmp_path, adapter_claude, {"is_error": True, "api_error_status": 429, "result": ""})
    assert c.outcome == Outcome.QUOTA
    assert c.reset_at is None


def test_claude_limit_reset_with_date_parsed(tmp_path, adapter_claude, monkeypatch):
    text = "You've hit your session limit · resets Oct 14, 9am (Asia/Ho_Chi_Minh)"
    monkeypatch.setattr("agenthub.agents.claude._now", lambda: datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc))
    c = _classify_stdout(tmp_path, adapter_claude, _limit_payload(text, api_error_status=429))
    assert c.outcome == Outcome.QUOTA
    # 09:00 local on 2026-10-14 is 02:00 UTC.
    assert c.reset_at == datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)


def test_claude_limit_unknown_zone_has_no_reset(tmp_path, adapter_claude):
    text = "You've hit your session limit · resets 12:20pm (Mars/Olympus)"
    c = _classify_stdout(tmp_path, adapter_claude, _limit_payload(text, api_error_status=429))
    assert c.outcome == Outcome.QUOTA
    assert c.reset_at is None
