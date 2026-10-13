import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.gitrepo import make_repo


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_STATE", str(tmp_path / "hub_state"))
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path / "hub_config"))


@pytest.fixture
def env_sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = tmp_path / "config"
    state = tmp_path / "state"
    fakelog = tmp_path / "fakelog"

    home.mkdir()
    config.mkdir()
    state.mkdir()
    fakelog.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(config))
    monkeypatch.setenv("AGENT_HUB_STATE", str(state))
    monkeypatch.setenv("FAKE_LOG_DIR", str(fakelog))

    fakes_dir = Path(__file__).parent / "fakes"
    git_dir = Path(shutil.which("git")).parent
    py_dir = Path(shutil.which("python3")).parent

    new_path = f"{fakes_dir}:{git_dir}:{py_dir}:/usr/bin:/bin"
    monkeypatch.setenv("PATH", new_path)

    ns = SimpleNamespace(
        home=home,
        config=config,
        state=state,
        fakelog=fakelog,
    )
    return ns


@pytest.fixture(autouse=True)
def check_fakes_sandbox(request):
    if "env_sandbox" in request.fixturenames:
        request.getfixturevalue("env_sandbox")
        fakes_dir = Path(__file__).parent / "fakes"
        for agent in ["agy", "codex", "claude"]:
            which_path = shutil.which(agent)
            if which_path is not None:
                assert Path(which_path).parent == fakes_dir, f"Real {agent} found on PATH instead of fake: {which_path}"


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


def fake_calls(fakelog: Path, name: str) -> list[dict]:
    calls = []
    # read sequentially
    i = 1
    while True:
        log_file = fakelog / f"{name}.{i}.json"
        if not log_file.exists():
            break
        calls.append(json.loads(log_file.read_text()))
        i += 1
    return calls


@pytest.fixture(autouse=True)
def _restore_environ():
    """Undo any direct os.environ edits a test makes, so nothing leaks into later tests."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
