from datetime import timedelta

import pytest

from agenthub.config import ConfigError, load_pool, parse_duration, paths


def test_paths_honour_agent_hub_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path / "conf"))
    monkeypatch.setenv("AGENT_HUB_STATE", str(tmp_path / "st"))
    p = paths()
    assert p.config_dir == tmp_path / "conf"
    assert p.state_dir == tmp_path / "st"
    assert p.jobs_dir == tmp_path / "st" / "jobs"
    assert p.cooldowns_file == tmp_path / "st" / "cooldowns.json"


def test_paths_fall_back_to_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_HUB_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_HUB_STATE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xconf"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xst"))
    p = paths()
    assert p.config_dir == tmp_path / "xconf" / "agent-hub"
    assert p.state_dir == tmp_path / "xst" / "agent-hub"


def test_paths_fall_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_HUB_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_HUB_STATE", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    p = paths()
    assert p.config_dir == tmp_path / "home" / ".config" / "agent-hub"
    assert p.state_dir == tmp_path / "home" / ".local" / "state" / "agent-hub"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("90s", timedelta(seconds=90)),
        ("60m", timedelta(minutes=60)),
        ("2h", timedelta(hours=2)),
        ("1h30m", timedelta(hours=1, minutes=30)),
    ],
)
def test_parse_duration_valid(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "10", "5d", "m", "-5m"])
def test_parse_duration_invalid(text):
    with pytest.raises(ConfigError):
        parse_duration(text)


def test_load_pool_default_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path / "conf"))
    p = paths()
    pool = load_pool(p)
    assert pool.roles["implement"] == [
        "agy:claude-opus-4-6-thinking",
        "agy:claude-sonnet-4-6",
        "agy:gemini-3.1-pro-high",
        "codex:gpt-5.6-terra",
        "claude:sonnet",
    ]
    assert pool.timeouts["review"] == timedelta(minutes=20)
    assert pool.quota_cooldown == timedelta(minutes=60)


def test_load_pool_user_role_replaces_default_role(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text("""
[roles.implement]
chain = ["x:y"]
""")
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))
    p = paths()
    pool = load_pool(p)
    assert pool.roles["implement"] == ["x:y"]
    assert "review" in pool.roles
    assert pool.roles["review"] == ["codex:gpt-5.6-terra", "agy:gemini-3.1-pro-high", "claude:sonnet"]


def test_load_pool_user_defaults_merge(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text("""
[defaults]
quota_cooldown = "10m"
""")
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))
    p = paths()
    pool = load_pool(p)
    assert pool.quota_cooldown == timedelta(minutes=10)
    assert pool.timeouts["implement"] == timedelta(minutes=60)


def test_load_pool_invalid_toml_names_file(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text("[invalid")
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))
    p = paths()
    with pytest.raises(ConfigError, match="pool.toml"):
        load_pool(p)
