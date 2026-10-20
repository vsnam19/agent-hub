import subprocess

import pytest

from agenthub.agents import AgentConfig
from agenthub.agents.agy import AgyAdapter

SAMPLE_AGY_MODELS_OUTPUT = (
    "Fetching available models...\n"
    "gemini-4-argon-medium\tGemini 4 Argon (Medium)\n"
    "gemini-4-argon-high\tGemini 4 Argon (High)\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
)


def _make_agy_adapter() -> AgyAdapter:
    adapter = AgyAdapter()
    adapter.config = AgentConfig(
        binary="agy",
        version_args=["--version"],
        env_passthrough=[],
        read_only="best-effort",
    )
    return adapter


def test_agentconfig_known_models_defaults_empty():
    cfg = AgentConfig(
        binary="agy",
        version_args=[],
        env_passthrough=[],
        read_only="best-effort",
    )
    assert cfg.known_models == []

    cfg_with_models = AgentConfig(
        binary="agy",
        version_args=[],
        env_passthrough=[],
        read_only="best-effort",
        known_models=["model-a", "model-b"],
    )
    assert cfg_with_models.known_models == ["model-a", "model-b"]


def test_agy_list_models_parses_tab_lines(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_agy_adapter()
    monkeypatch.setattr(adapter, "installed", lambda: True)

    fake_res = subprocess.CompletedProcess(
        args=["agy", "models"],
        returncode=0,
        stdout=SAMPLE_AGY_MODELS_OUTPUT,
        stderr="",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_res)

    adapter.models_error = "stale error"
    models = adapter.list_models()
    assert models == [
        "gemini-4-argon-medium",
        "gemini-4-argon-high",
        "gemini-3.8-flash-high",
    ]
    assert adapter.models_error is None


def test_agy_list_models_skips_lines_without_tab(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_agy_adapter()
    monkeypatch.setattr(adapter, "installed", lambda: True)

    mixed_output = (
        "Fetching available models...\n"
        "Some informative banner without tab\n"
        "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
        "Another line without tab\n"
    )
    fake_res = subprocess.CompletedProcess(
        args=["agy", "models"],
        returncode=0,
        stdout=mixed_output,
        stderr="",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_res)

    models = adapter.list_models()
    assert models == ["gemini-3.8-flash-high"]
    assert adapter.models_error is None

    # Empty parse case: output without any tabs
    no_tabs_output = "Fetching available models...\nOnly banners here\n"
    fake_empty_res = subprocess.CompletedProcess(
        args=["agy", "models"],
        returncode=0,
        stdout=no_tabs_output,
        stderr="",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_empty_res)
    models_none = adapter.list_models()
    assert models_none is None
    assert adapter.models_error == "no models listed"


def test_agy_list_models_drops_ssh_env(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_agy_adapter()
    monkeypatch.setattr(adapter, "installed", lambda: True)

    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5678 1.2.3.4 22")
    monkeypatch.setenv("SSH_CLIENT", "1.2.3.4 5678 22")
    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")
    monkeypatch.setenv("KEEP_VAR", "present")

    captured_env: dict[str, str] | None = None

    def fake_run(args, **kwargs):
        nonlocal captured_env
        captured_env = kwargs.get("env")
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=SAMPLE_AGY_MODELS_OUTPUT,
            stderr="",
        )

    monkeypatch.setattr("agenthub.agents.subprocess.run", fake_run)

    models = adapter.list_models()
    assert models is not None
    assert captured_env is not None
    assert "SSH_CONNECTION" not in captured_env
    assert "SSH_CLIENT" not in captured_env
    assert "SSH_TTY" not in captured_env
    assert captured_env.get("KEEP_VAR") == "present"


def test_list_models_failure_returns_none_with_reason(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_agy_adapter()

    # Case 1: not installed
    monkeypatch.setattr(adapter, "installed", lambda: False)
    res_not_installed = adapter.list_models()
    assert res_not_installed is None
    assert adapter.models_error == "not installed"

    # Case 2: non-zero return code (e.g. rc 2)
    monkeypatch.setattr(adapter, "installed", lambda: True)
    fake_fail_res = subprocess.CompletedProcess(
        args=["agy", "models"],
        returncode=2,
        stdout="",
        stderr="unknown option",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_fail_res)
    res_fail = adapter.list_models()
    assert res_fail is None
    assert adapter.models_error == "rc 2"


def test_list_models_timeout_returns_none(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_agy_adapter()
    monkeypatch.setattr(adapter, "installed", lambda: True)

    def fake_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=30)

    monkeypatch.setattr("agenthub.agents.subprocess.run", fake_timeout)

    res = adapter.list_models()
    assert res is None
    assert adapter.models_error == "timeout"
