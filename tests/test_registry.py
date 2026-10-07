import os
import sys
from pathlib import Path

import pytest

from agenthub.agents import (
    Adapter,
    AgentConfig,
    Classified,
    Invocation,
    Request,
    discover,
)


class DummyAdapter(Adapter):
    def build(self, req: Request) -> Invocation:
        return Invocation(argv=["dummy"], stdin_path=None)

    def classify(self, rc: int, attempt_dir: Path) -> Classified:
        raise NotImplementedError


def test_discover_loads_adapter_with_config_and_profile(tmp_path: Path):
    pkg = tmp_path / "agents"
    pkg.mkdir()
    (pkg / "__init__.py").touch()

    ag = pkg / "my_agent"
    ag.mkdir()

    (ag / "__init__.py").write_text("""
from agenthub.agents import Adapter, Invocation, Request, Classified

class MyAdapter(Adapter):
    def build(self, req: Request) -> Invocation: pass
    def classify(self, rc: int, attempt_dir) -> Classified: pass

ADAPTER = MyAdapter
""")
    (ag / "agent.toml").write_text("""
binary = "mybin"
version_args = ["--version"]
env_passthrough = []
read_only = "native"
""")
    (ag / "profile.md").write_text("my profile")

    # insert tmp_path to sys.path so 'agents' is importable
    sys.path.insert(0, str(tmp_path))
    try:
        adapters = discover(pkg, "agents")

        assert "my_agent" in adapters
        adapter = adapters["my_agent"]
        assert adapter.name == "my_agent"
        assert adapter.config == AgentConfig(
            binary="mybin", version_args=["--version"], env_passthrough=[], read_only="native"
        )
        assert adapter.profile_path == ag / "profile.md"
        assert type(adapter).__name__ == "MyAdapter"
    finally:
        sys.path.pop(0)


def test_discover_skips_package_without_agent_toml(tmp_path: Path):
    pkg = tmp_path / "agents"
    pkg.mkdir()
    (pkg / "__init__.py").touch()

    ag = pkg / "my_agent"
    ag.mkdir()

    (ag / "__init__.py").write_text("""
from agenthub.agents import Adapter
class MyAdapter(Adapter): pass
ADAPTER = MyAdapter
""")

    sys.path.insert(0, str(tmp_path))
    try:
        adapters = discover(pkg, "agents")
        assert "my_agent" not in adapters
    finally:
        sys.path.pop(0)


def test_discover_rejects_missing_ADAPTER(tmp_path: Path):
    pkg = tmp_path / "agents2"
    pkg.mkdir()
    (pkg / "__init__.py").touch()

    ag = pkg / "my_agent2"
    ag.mkdir()

    (ag / "__init__.py").write_text("x = 1")
    (ag / "agent.toml").write_text('binary = "mybin"\nversion_args = []\nenv_passthrough = []\nread_only = "native"')

    sys.path.insert(0, str(tmp_path))
    try:
        with pytest.raises(TypeError, match="my_agent2"):
            discover(pkg, "agents2")
    finally:
        sys.path.pop(0)


def test_installed_and_version(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bin_file = bin_dir / "mybin"
    bin_file.write_text("#!/usr/bin/env python3\nprint('mybin 1.2.3')\n")
    bin_file.chmod(0o755)

    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    adapter = DummyAdapter()
    adapter.config = AgentConfig(binary="mybin", version_args=["--version"], env_passthrough=[], read_only="native")

    assert adapter.installed()
    assert adapter.version() == "mybin 1.2.3"


def test_version_none_when_missing(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))

    adapter = DummyAdapter()
    adapter.config = AgentConfig(
        binary="missingbin", version_args=["--version"], env_passthrough=[], read_only="native"
    )

    assert not adapter.installed()
    assert adapter.version() is None
