import json
import os
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_plugin_manifest_fields():
    manifest_path = REPO_ROOT / ".claude-plugin" / "plugin.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    expected = {
        "name": "agent-hub",
        "description": "Run coding agents from one orchestrator, by role, with quota fallback",
        "version": manifest["version"],
        "author": {"name": "vsnam19"},
        "license": "MIT",
        "keywords": ["agents", "orchestration", "delegation", "codex", "agy"],
    }

    assert manifest == expected


def test_plugin_version_matches_pyproject():
    manifest_path = REPO_ROOT / ".claude-plugin" / "plugin.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    pyproject_path = REPO_ROOT / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)

    assert manifest["version"] == pyproject["project"]["version"]


def test_bin_wrapper_is_executable_and_shellchecked():
    bin_path = REPO_ROOT / "bin" / "agent-hub"
    st = os.stat(bin_path)
    assert st.st_mode & stat.S_IXUSR, "Script is not executable"

    shellcheck_path = shutil.which("shellcheck")
    if not shellcheck_path:
        pytest.skip("shellcheck not found on PATH")

    result = subprocess.run([shellcheck_path, str(bin_path)], capture_output=True, text=True)
    assert result.returncode == 0, f"shellcheck failed: {result.stdout}\n{result.stderr}"


def test_bin_wrapper_runs_cli(tmp_path):
    bin_path = REPO_ROOT / "bin" / "agent-hub"
    result = subprocess.run([str(bin_path), "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_bin_wrapper_without_uv_exits_127(tmp_path):
    bin_path = REPO_ROOT / "bin" / "agent-hub"

    # Create fake PATH with only required commands but NO uv
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    for cmd in ["bash", "dirname", "readlink"]:
        real_cmd = shutil.which(cmd)
        if real_cmd:
            os.symlink(real_cmd, fake_bin / cmd)

    env = os.environ.copy()
    env["PATH"] = str(fake_bin)

    result = subprocess.run([str(bin_path), "--help"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 127
    assert "agent-hub: uv is required (https://docs.astral.sh/uv/)" in result.stderr
