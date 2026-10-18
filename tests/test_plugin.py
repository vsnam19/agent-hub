import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from agenthub.mcpserver import build_server

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
ORCHESTRATE_SKILL = REPO_ROOT / "skills" / "orchestrate" / "SKILL.md"
MODELS = {"haiku", "sonnet", "opus"}


def _split_agent(text):
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert match, "agent file must start with YAML frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, match.group(2)


def _agent_files():
    files = sorted(AGENTS_DIR.glob("*.md"))
    assert files, "agents/ must ship at least one agent"
    return files


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
        "mcpServers": {
            "agent-hub": {
                "command": "${CLAUDE_PLUGIN_ROOT}/bin/agent-hub",
                "args": ["mcp"],
            }
        },
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


def test_plugin_manifest_declares_mcp_server():
    assert not (REPO_ROOT / ".mcp.json").exists(), ".mcp.json must not exist at repo root"
    manifest_path = REPO_ROOT / ".claude-plugin" / "plugin.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    expected = {
        "agent-hub": {
            "command": "${CLAUDE_PLUGIN_ROOT}/bin/agent-hub",
            "args": ["mcp"],
        }
    }
    assert manifest.get("mcpServers") == expected


def test_skill_names_every_mcp_tool():
    server = build_server()
    server_tools = {t.name for t in asyncio.run(server.list_tools())}
    text = ORCHESTRATE_SKILL.read_text(encoding="utf-8")
    for tool_name in server_tools:
        assert tool_name in text, f"orchestrate skill must name MCP tool {tool_name}"


def test_readme_mcp_tools_match_server():
    server = build_server()
    server_tools = {t.name for t in asyncio.run(server.list_tools())}

    readme_path = REPO_ROOT / "README.md"
    readme_text = readme_path.read_text(encoding="utf-8")
    tools_section = re.search(r"### Tools\n\n(.*?)(?:\n\n###|\Z)", readme_text, re.DOTALL)
    assert tools_section, "README must have a '### Tools' section"
    readme_tools = re.findall(r"^- `([^`]+)`:", tools_section.group(1), re.MULTILINE)

    assert len(readme_tools) == len(set(readme_tools)), "Duplicate tools in README"
    assert set(readme_tools) == server_tools


def test_readme_exit_codes_table():
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "\n| Exit | Status | Meaning |\n|---|---|---|\n" in readme_text


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


def test_agents_have_valid_frontmatter():
    for path in _agent_files():
        fields, _ = _split_agent(path.read_text(encoding="utf-8"))
        assert fields["name"] == path.stem, f"{path.name} name must match its file stem"
        assert fields["description"].startswith("Use when"), f"{path.name} description must start with 'Use when'"
        assert fields["model"] in MODELS, f"{path.name} model must be one of {sorted(MODELS)}"
        assert fields["tools"], f"{path.name} must list tools"


def test_reviewers_cannot_edit():
    read_only = [p for p in _agent_files() if p.stem == "explorer" or p.stem.startswith("reviewer-")]
    assert {p.stem for p in read_only} == {
        "explorer",
        "reviewer-correctness",
        "reviewer-evidence",
        "reviewer-design",
    }
    for path in read_only:
        fields, _ = _split_agent(path.read_text(encoding="utf-8"))
        tools = {tool.strip() for tool in fields["tools"].split(",")}
        assert not tools & {"Edit", "Write"}, f"{path.name} must not be able to edit files"


def test_orchestrate_mentions_every_agent():
    text = ORCHESTRATE_SKILL.read_text(encoding="utf-8")
    for path in _agent_files():
        assert f"agent-hub:{path.stem}" in text, f"orchestrate skill must mention agent-hub:{path.stem}"


def test_implementer_has_handoff_headings():
    path = AGENTS_DIR / "implementer.md"
    _, body = _split_agent(path.read_text(encoding="utf-8"))
    for heading in ("## Status", "## What I did", "## Verification"):
        assert heading in body, f"implementer must end with {heading}"
    assert "inconclusive" in body


def test_plugin_hooks_manifest():
    hooks_path = REPO_ROOT / "hooks" / "hooks.json"
    assert hooks_path.exists(), "hooks/hooks.json must exist"
    with open(hooks_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "hooks" in data
    hooks = data["hooks"]

    # SessionStart hook
    assert "SessionStart" in hooks
    session_start_entries = hooks["SessionStart"]
    assert len(session_start_entries) == 1
    session_start = session_start_entries[0]
    assert session_start.get("matcher") == "startup|resume|clear|compact"
    assert len(session_start.get("hooks", [])) == 1
    ss_hook = session_start["hooks"][0]
    assert ss_hook.get("type") == "command"
    assert ss_hook.get("command") == "${CLAUDE_PLUGIN_ROOT}/bin/agent-hub hook session-start"
    assert ss_hook.get("timeout") == 10

    # Stop hook
    assert "Stop" in hooks
    stop_entries = hooks["Stop"]
    assert len(stop_entries) == 1
    stop = stop_entries[0]
    assert len(stop.get("hooks", [])) == 1
    stop_hook = stop["hooks"][0]
    assert stop_hook.get("type") == "command"
    assert stop_hook.get("command") == "${CLAUDE_PLUGIN_ROOT}/bin/agent-hub hook stop"
    assert stop_hook.get("timeout") == 10

    # README documentation
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Claude Code hooks" in readme_text
    assert "SessionStart" in readme_text
    assert "Stop" in readme_text
    assert "20 blocks" in readme_text
    assert "disableAllHooks" in readme_text
    assert "/plugin" in readme_text
