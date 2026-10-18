from datetime import timedelta

import pytest

from agenthub.config import ConfigError, load_pool, parse_duration, paths, role_kind


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
    assert pool.roles["implement"].chain == [
        "claude:haiku",
        "agy:claude-sonnet-4-6",
        "agy:gemini-3.8-flash-high",
        "codex:gpt-5.6-terra",
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
    assert pool.roles["implement"].chain == ["x:y"]
    assert "review" in pool.roles
    assert pool.roles["review"].chain == [
        "agy:claude-opus-4-6-thinking",
        "codex:gpt-5.6-terra",
        "agy:gemini-3.8-flash-high",
        "claude:sonnet",
    ]


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
    import re

    with pytest.raises(ConfigError, match=re.escape(str(conf / "pool.toml"))):
        load_pool(p)


def test_legacy_pool_without_kind_still_works(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text('[roles.implement]\nchain = ["claude:legacy"]\n')
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))

    pool = load_pool(paths())

    assert pool.roles["implement"].kind == "implement"
    assert role_kind(pool, "implement") == "implement"
    assert pool.roles["implement"].chain == ["claude:legacy"]


def test_custom_role_requires_kind(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text('[roles.fast]\nchain = ["claude:haiku"]\n')
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))

    with pytest.raises(ConfigError, match="fast.*kind"):
        load_pool(paths())


def test_escalate_unknown_role_is_error(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text(
        '[roles.implement]\nkind = "implement"\nescalate = "missing"\nchain = ["claude:haiku"]\n'
    )
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))

    with pytest.raises(ConfigError, match="unknown role 'missing'"):
        load_pool(paths())


def test_review_role_cannot_escalate(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text(
        '[roles.review]\nkind = "review"\nescalate = "implement"\nchain = ["claude:sonnet"]\n'
    )
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))

    with pytest.raises(ConfigError, match="review role 'review' cannot escalate"):
        load_pool(paths())


def test_default_pool_roles(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path / "conf"))
    pool = load_pool(paths())

    assert {(name, role.kind, role.escalate) for name, role in pool.roles.items()} == {
        ("implement-small", "implement", "implement"),
        ("implement", "implement", "implement-hard"),
        ("implement-hard", "implement", None),
        ("review", "review", None),
        ("techlead", "plan", None),
    }


def test_plan_kind_accepted(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text('[roles.planner]\nkind = "plan"\nchain = ["claude:sonnet"]\n')
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))

    pool = load_pool(paths())

    assert pool.roles["planner"].kind == "plan"
    assert role_kind(pool, "planner") == "plan"
    assert pool.timeouts["planner"] == timedelta(minutes=20)


def test_unknown_kind_error_lists_three_kinds(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text('[roles.fast]\nkind = "chat"\nchain = ["claude:haiku"]\n')
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))

    with pytest.raises(ConfigError, match='"implement", "review" or "plan"'):
        load_pool(paths())


def test_plan_role_cannot_escalate(monkeypatch, tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "pool.toml").write_text(
        '[roles.planner]\nkind = "plan"\nescalate = "implement"\nchain = ["claude:sonnet"]\n'
    )
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(conf))

    with pytest.raises(ConfigError, match="plan role 'planner' cannot escalate"):
        load_pool(paths())


def test_default_pool_has_techlead(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path / "conf"))
    pool = load_pool(paths())

    assert pool.roles["techlead"].kind == "plan"
    assert pool.roles["techlead"].chain == ["claude:sonnet"]
    assert pool.roles["techlead"].escalate is None
    assert pool.timeouts["plan"] == timedelta(minutes=20)
    assert pool.timeouts["techlead"] == timedelta(minutes=20)
