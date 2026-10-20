import subprocess

from agenthub.agents import registry


def test_claude_agent_toml_lists_known_models():
    assert registry()["claude"].config.known_models == ["haiku", "sonnet", "opus", "fable"]


def test_claude_list_models_returns_known_models():
    assert registry()["claude"].list_models() == ["haiku", "sonnet", "opus", "fable"]


def test_claude_list_models_runs_no_command(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("list_models must not run a command")

    monkeypatch.setattr(subprocess, "run", boom)
    assert registry()["claude"].list_models() == ["haiku", "sonnet", "opus", "fable"]


def test_gwen_has_no_catalog():
    assert registry()["gwen"].list_models() is None
