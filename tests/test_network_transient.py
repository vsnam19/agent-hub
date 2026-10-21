import json
from datetime import timedelta

from agenthub import process
from agenthub.agents import NETWORK_MARKERS, Outcome, is_network_failure
from agenthub.agents.agy import AgyAdapter
from agenthub.agents.claude import ClaudeAdapter
from agenthub.agents.codex import CodexAdapter
from agenthub.config import paths
from agenthub.pool import Cooldowns, Link
from tests.test_run import go, now


def test_network_outage_is_transient(tmp_path):
    adapter = ClaudeAdapter()
    adapter.config = type("C", (), {"binary": "claude"})()

    # Case 1: stdout JSON has "API Error: Can't reach the API server"
    attempt1 = tmp_path / "attempt1"
    attempt1.mkdir()
    (attempt1 / "stdout").write_text(json.dumps({"is_error": True, "result": "API Error: Can't reach the API server"}))
    c1 = adapter.classify(1, attempt1)
    assert c1.outcome == Outcome.TRANSIENT
    assert "can't reach the api server" in c1.detail

    # Case 2: stderr has "getaddrinfo EAI_AGAIN api.anthropic.com"
    attempt2 = tmp_path / "attempt2"
    attempt2.mkdir()
    (attempt2 / "stderr").write_text("getaddrinfo EAI_AGAIN api.anthropic.com")
    c2 = adapter.classify(1, attempt2)
    assert c2.outcome == Outcome.TRANSIENT
    assert "eai_again" in c2.detail

    # Additional network error substrings from brief
    for err in ["ENOTFOUND", "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "network error"]:
        att = tmp_path / f"attempt_{err}"
        att.mkdir()
        (att / "stderr").write_text(f"Error: {err} connecting to server")
        c = adapter.classify(1, att)
        assert c.outcome == Outcome.TRANSIENT


def test_transient_falls_through_with_short_cooldown(env_sandbox, repo, monkeypatch):
    orig_run = process.run

    def fake_run(inv, cwd, env, attempt_dir, timeout, on_start=lambda pgid: None):
        if attempt_dir.name == "1-claude-m1":
            (attempt_dir / "stdout").write_text(
                json.dumps({"is_error": True, "result": "API Error: Can't reach the API server"})
            )
            (attempt_dir / "stderr").write_text("")
            on_start(12345)
            return process.Ran(rc=1, timed_out=False, duration_s=0.1, pgid=12345)
        return orig_run(inv, cwd, env, attempt_dir, timeout, on_start=on_start)

    monkeypatch.setattr(process, "run", fake_run)

    result = go(repo, ["claude:m1", "agy:m2"])
    assert result["status"] == "ok"
    assert [a["outcome"] for a in result["attempts"]] == ["transient", "ok"]
    assert result["model"] == "m2"

    until = Cooldowns(paths().cooldowns_file).until(Link("claude", "m1"), now())
    assert until is not None
    assert now() < until <= now() + timedelta(minutes=3)
    assert until - now() > timedelta(minutes=2)


def test_network_words_on_success_stay_ok(tmp_path):
    adapter = ClaudeAdapter()
    adapter.config = type("C", (), {"binary": "claude"})()

    attempt = tmp_path / "attempt"
    attempt.mkdir()
    (attempt / "stdout").write_text(json.dumps({"result": "done"}))
    (attempt / "stderr").write_text("ECONNREFUSED from mcp server")
    c = adapter.classify(0, attempt)
    assert c.outcome == Outcome.OK


def _agy_attempt(tmp_path, name, stderr, response=""):
    d = tmp_path / name
    d.mkdir()
    (d / "stdout").write_text(json.dumps({"response": response}))
    (d / "stderr").write_text(stderr)
    return d


def _codex_attempt(tmp_path, name, stderr, reply=""):
    d = tmp_path / name
    d.mkdir()
    (d / "stdout").write_text("")
    (d / "stderr").write_text(stderr)
    if reply:
        (d / "reply.md").write_text(reply)
    return d


def test_agy_network_failure_is_transient(tmp_path):
    adapter = AgyAdapter()
    adapter.config = type("C", (), {"binary": "agy"})()
    stderr = "getaddrinfo EAI_AGAIN generativelanguage.googleapis.com"

    c = adapter.classify(1, _agy_attempt(tmp_path, "a1", stderr))
    assert c.outcome == Outcome.TRANSIENT
    assert "eai_again" in c.detail

    c = adapter.classify(0, _agy_attempt(tmp_path, "a2", stderr, response="done"))
    assert c.outcome != Outcome.TRANSIENT


def test_codex_network_failure_is_transient(tmp_path):
    adapter = CodexAdapter()
    adapter.config = type("C", (), {"binary": "codex"})()
    stderr = "getaddrinfo EAI_AGAIN api.openai.com"

    c = adapter.classify(1, _codex_attempt(tmp_path, "c1", stderr))
    assert c.outcome == Outcome.TRANSIENT
    assert "eai_again" in c.detail

    c = adapter.classify(0, _codex_attempt(tmp_path, "c2", stderr, reply="done"))
    assert c.outcome != Outcome.TRANSIENT


def test_agy_quota_beats_network(tmp_path):
    adapter = AgyAdapter()
    adapter.config = type("C", (), {"binary": "agy"})()
    c = adapter.classify(1, _agy_attempt(tmp_path, "q", "quota exceeded; ECONNRESET"))
    assert c.outcome == Outcome.QUOTA


def test_is_network_failure_markers():
    assert NETWORK_MARKERS
    for marker in NETWORK_MARKERS:
        assert is_network_failure(marker)
        assert is_network_failure(f"Error: {marker.upper()} while connecting")
    assert not is_network_failure("syntax error")
