import json
import subprocess

import pytest

from agenthub.agents import AgentConfig
from agenthub.agents.codex import CodexAdapter

SAMPLE_CODEX_MODELS_OUTPUT = json.dumps(
    {
        "models": [
            {
                "slug": "gpt-reserve",
                "display_name": "GPT-Reserve",
                "description": "Fast and affordable agentic coding model.",
                "visibility": "hide",
                "priority": 4,
            },
            {
                "slug": "gpt-5.6-terra",
                "display_name": "GPT-5.6-Terra",
                "description": "Older balanced model for straightforward work.",
                "visibility": "list",
                "priority": 8,
            },
            {
                "slug": "gpt-5.6-luna",
                "display_name": "GPT-5.6-Luna",
                "description": "Older fast and efficient model.",
                "visibility": "list",
                "priority": 9,
            },
        ]
    }
)


def _make_codex_adapter() -> CodexAdapter:
    adapter = CodexAdapter()
    adapter.config = AgentConfig(
        binary="codex",
        version_args=["--version"],
        env_passthrough=[],
        read_only="native",
    )
    return adapter


def test_codex_list_models_reads_slugs(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_codex_adapter()
    monkeypatch.setattr(adapter, "installed", lambda: True)

    recorded_args = []

    def fake_run(args, **kwargs):
        recorded_args.append(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=SAMPLE_CODEX_MODELS_OUTPUT,
            stderr="",
        )

    monkeypatch.setattr("agenthub.agents.subprocess.run", fake_run)

    adapter.models_error = "stale error"
    models = adapter.list_models()
    assert recorded_args == [["codex", "debug", "models"]]
    assert models == [
        "gpt-reserve",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    assert adapter.models_error is None


def test_codex_list_models_bad_json_returns_none(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_codex_adapter()
    monkeypatch.setattr(adapter, "installed", lambda: True)

    fake_res = subprocess.CompletedProcess(
        args=["codex", "debug", "models"],
        returncode=0,
        stdout="not valid json {{{",
        stderr="",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_res)

    adapter.models_error = "stale error"
    result = adapter.list_models()
    assert result is None
    assert adapter.models_error == "bad json"


def test_codex_list_models_missing_models_key_returns_none(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_codex_adapter()
    monkeypatch.setattr(adapter, "installed", lambda: True)

    fake_res = subprocess.CompletedProcess(
        args=["codex", "debug", "models"],
        returncode=0,
        stdout=json.dumps({"wrong_key": []}),
        stderr="",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_res)

    result = adapter.list_models()
    assert result is None
    assert adapter.models_error == "no models key"

    fake_list = subprocess.CompletedProcess(
        args=["codex", "debug", "models"],
        returncode=0,
        stdout=json.dumps(["not", "a", "dict"]),
        stderr="",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_list)

    result = adapter.list_models()
    assert result is None
    assert adapter.models_error == "no models key"


def test_codex_list_models_empty_returns_none(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_codex_adapter()
    monkeypatch.setattr(adapter, "installed", lambda: True)

    fake_res = subprocess.CompletedProcess(
        args=["codex", "debug", "models"],
        returncode=0,
        stdout=json.dumps({"models": []}),
        stderr="",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_res)

    result = adapter.list_models()
    assert result is None
    assert adapter.models_error == "no models listed"

    fake_no_slugs = subprocess.CompletedProcess(
        args=["codex", "debug", "models"],
        returncode=0,
        stdout=json.dumps({"models": [{"display_name": "No Slug"}, {"slug": ""}]}),
        stderr="",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_no_slugs)

    result = adapter.list_models()
    assert result is None
    assert adapter.models_error == "no models listed"

    fake_not_list = subprocess.CompletedProcess(
        args=["codex", "debug", "models"],
        returncode=0,
        stdout=json.dumps({"models": "string instead of list"}),
        stderr="",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_not_list)

    result = adapter.list_models()
    assert result is None
    assert adapter.models_error == "no models listed"


def test_codex_list_models_not_installed(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_codex_adapter()
    monkeypatch.setattr(adapter, "installed", lambda: False)

    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess.run should not be called when not installed")

    monkeypatch.setattr("agenthub.agents.subprocess.run", fake_run)

    result = adapter.list_models()
    assert result is None
    assert adapter.models_error == "not installed"
    assert not called


def test_codex_list_models_subprocess_error(monkeypatch: pytest.MonkeyPatch):
    adapter = _make_codex_adapter()
    monkeypatch.setattr(adapter, "installed", lambda: True)

    fake_fail = subprocess.CompletedProcess(
        args=["codex", "debug", "models"],
        returncode=1,
        stdout="",
        stderr="error",
    )
    monkeypatch.setattr("agenthub.agents.subprocess.run", lambda *args, **kwargs: fake_fail)

    result = adapter.list_models()
    assert result is None
    assert adapter.models_error == "rc 1"
