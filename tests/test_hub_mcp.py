import json
import shutil
import sys
from pathlib import Path

import pytest

from agenthub import hub

FAKE_CODEX_MCP = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
state = Path(os.environ["FAKE_HUB_STATE"])
args = sys.argv[1:]
with (state / "calls.log").open("a") as f:
    f.write("codex " + " ".join(args) + "\\n")
mcp_file = state / "codex_mcp.json"
if args[:3] == ["mcp", "list", "--json"]:
    if os.environ.get("FAKE_CODEX_MCP_FAIL"):
        print(os.environ.get("FAKE_CODEX_MCP_STDERR", "codex mcp error"), file=sys.stderr)
        sys.exit(int(os.environ.get("FAKE_CODEX_MCP_RC", "1")))
    if os.environ.get("FAKE_CODEX_MCP_BAD_JSON"):
        print("not json")
        sys.exit(0)
    print(mcp_file.read_text() if mcp_file.exists() else "[]")
elif args[:3] == ["mcp", "add", "agent-hub"]:
    pass
elif args[:3] == ["mcp", "remove", "agent-hub"]:
    pass
else:
    sys.exit(64)
"""

FAKE_AGY_MCP = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
state = Path(os.environ["FAKE_HUB_STATE"])
args = sys.argv[1:]
with (state / "calls.log").open("a") as f:
    f.write("agy " + " ".join(args) + "\\n")
mcp_file = state / "agy_mcp.txt"
if args[:2] == ["mcp", "list"]:
    if os.environ.get("FAKE_AGY_MCP_FAIL"):
        print(os.environ.get("FAKE_AGY_MCP_STDERR", "agy mcp error"), file=sys.stderr)
        sys.exit(int(os.environ.get("FAKE_AGY_MCP_RC", "1")))
    if os.environ.get("FAKE_AGY_MCP_BAD_TABLE"):
        print("not a table")
        sys.exit(0)
    print(mcp_file.read_text() if mcp_file.exists() else "No MCP servers configured.")
elif args[:3] == ["mcp", "add", "agent-hub"]:
    pass
else:
    sys.exit(64)
"""


class McpEnv:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.bin = tmp_path / "fakebin"
        self.bin.mkdir()
        self.state = tmp_path / "fakestate"
        self.state.mkdir()
        self.home = tmp_path / "hubhome"
        self.codex_home = tmp_path / "codexhome"
        self.home.mkdir()
        self.codex_home.mkdir()
        self.config = tmp_path / "hubconfig"
        self.config.mkdir()
        (self.config / "workers.toml").write_text("default = []\n")
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("CODEX_HOME", str(self.codex_home))
        monkeypatch.setenv("FAKE_HUB_STATE", str(self.state))
        monkeypatch.setenv("AGENT_HUB_CONFIG", str(self.config))
        for tool in ("git", "python3"):
            real = shutil.which(tool) if tool == "git" else sys.executable
            (self.bin / tool).symlink_to(real)
        monkeypatch.setenv("PATH", str(self.bin))
        self.install_fake("codex", FAKE_CODEX_MCP)
        self.install_fake("agy", FAKE_AGY_MCP)

    def install_fake(self, name, text):
        f = self.bin / name
        f.write_text(text)
        f.chmod(0o755)

    def calls(self):
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def set_codex_mcp(self, servers: list[dict]):
        (self.state / "codex_mcp.json").write_text(json.dumps(servers))

    def set_agy_mcp(self, table_text: str):
        (self.state / "agy_mcp.txt").write_text(table_text)


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    return McpEnv(tmp_path, monkeypatch)


def test_mcp_command_uses_package_bin():
    cmd = hub._mcp_command()
    assert isinstance(cmd, list)
    assert len(cmd) >= 2
    assert cmd[0].endswith("bin/agent-hub")
    assert Path(cmd[0]).is_absolute()
    assert cmd[1:] == ["mcp"]


def test_mcp_rows_missing_then_install_commands(mcp_env, tmp_path):
    rows = hub.status(mcp_env.config, tmp_path / "repo")
    mcp_rows = [r for r in rows if r.plugin == hub.MCP_ROW]
    assert len(mcp_rows) == 2
    codex_row = next(r for r in mcp_rows if r.host == "codex")
    agy_row = next(r for r in mcp_rows if r.host == "agy")
    assert codex_row.state == "missing"
    assert agy_row.state == "missing"

    cfg = hub.load_config(mcp_env.config)
    cmds = hub.plan_install(rows, cfg)
    bin_cmd = hub._mcp_command()[0]
    assert cmds == [
        ["codex", "mcp", "add", "agent-hub", "--", bin_cmd, "mcp"],
        ["agy", "mcp", "add", "agent-hub", bin_cmd, "mcp"],
    ]


def test_mcp_row_current_codex_and_agy(mcp_env, tmp_path):
    bin_cmd = hub._mcp_command()[0]
    mcp_env.set_codex_mcp(
        [
            {
                "name": "agent-hub",
                "enabled": True,
                "transport": {
                    "type": "stdio",
                    "command": bin_cmd,
                    "args": ["mcp"],
                },
            }
        ]
    )
    mcp_env.set_agy_mcp(f"NAME            TYPE   STATUS   COMMAND/URL\nagent-hub       stdio  enabled  {bin_cmd} mcp\n")

    rows = hub.status(mcp_env.config, tmp_path / "repo")
    codex_row = next(r for r in rows if r.host == "codex" and r.plugin == hub.MCP_ROW)
    agy_row = next(r for r in rows if r.host == "agy" and r.plugin == hub.MCP_ROW)
    assert codex_row.state == "current"
    assert agy_row.state == "current"

    cmds = hub.plan_install(rows, hub.load_config(mcp_env.config))
    assert not any(c[0] in ("codex", "agy") and "mcp" in c for c in cmds)


def test_mcp_row_stale_codex_removes_then_adds(mcp_env, tmp_path):
    mcp_env.set_codex_mcp(
        [
            {
                "name": "agent-hub",
                "enabled": True,
                "transport": {
                    "type": "stdio",
                    "command": "/usr/local/bin/agent-hub",
                    "args": ["mcp"],
                },
            }
        ]
    )
    rows = hub.status(mcp_env.config, tmp_path / "repo")
    codex_row = next(r for r in rows if r.host == "codex" and r.plugin == hub.MCP_ROW)
    assert codex_row.state == "stale"
    assert codex_row.detail == "/usr/local/bin/agent-hub mcp"

    cmds = hub.plan_install(rows, hub.load_config(mcp_env.config), hosts=("codex",))
    bin_cmd = hub._mcp_command()[0]
    assert cmds == [
        ["codex", "mcp", "remove", "agent-hub"],
        ["codex", "mcp", "add", "agent-hub", "--", bin_cmd, "mcp"],
    ]


def test_codex_args_string_detail(mcp_env, tmp_path):
    bin_cmd = hub._mcp_command()[0]
    mcp_env.set_codex_mcp(
        [
            {
                "name": "agent-hub",
                "enabled": True,
                "transport": {
                    "type": "stdio",
                    "command": bin_cmd,
                    "args": "mcp x",
                },
            }
        ]
    )
    rows = hub.status(mcp_env.config, tmp_path / "repo")
    codex_row = next(r for r in rows if r.host == "codex" and r.plugin == hub.MCP_ROW)
    assert codex_row.state == "stale"
    assert codex_row.detail == f"{bin_cmd} mcp x"

    mcp_env.set_codex_mcp(
        [
            {
                "name": "agent-hub",
                "enabled": True,
                "transport": {
                    "type": "stdio",
                    "command": bin_cmd,
                    "args": "mcp",
                },
            }
        ]
    )
    rows_current = hub.status(mcp_env.config, tmp_path / "repo")
    codex_row_current = next(r for r in rows_current if r.host == "codex" and r.plugin == hub.MCP_ROW)
    assert codex_row_current.state == "current"


def test_mcp_row_stale_agy_updates(mcp_env, tmp_path):
    mcp_env.set_agy_mcp(
        "NAME            TYPE   STATUS   COMMAND/URL\nagent-hub       stdio  enabled  /usr/local/bin/agent-hub mcp\n"
    )
    rows = hub.status(mcp_env.config, tmp_path / "repo")
    agy_row = next(r for r in rows if r.host == "agy" and r.plugin == hub.MCP_ROW)
    assert agy_row.state == "stale"
    assert agy_row.detail == "/usr/local/bin/agent-hub mcp"

    cmds = hub.plan_install(rows, hub.load_config(mcp_env.config), hosts=("agy",))
    bin_cmd = hub._mcp_command()[0]
    assert cmds == [
        ["agy", "mcp", "add", "agent-hub", bin_cmd, "mcp"],
    ]


def test_mcp_row_disabled_not_installed(mcp_env, tmp_path):
    bin_cmd = hub._mcp_command()[0]
    mcp_env.set_codex_mcp(
        [
            {
                "name": "agent-hub",
                "enabled": False,
                "transport": {
                    "type": "stdio",
                    "command": bin_cmd,
                    "args": ["mcp"],
                },
            }
        ]
    )
    mcp_env.set_agy_mcp(
        f"NAME            TYPE   STATUS    COMMAND/URL\nagent-hub       stdio  disabled  {bin_cmd} mcp\n"
    )
    rows = hub.status(mcp_env.config, tmp_path / "repo")
    codex_row = next(r for r in rows if r.host == "codex" and r.plugin == hub.MCP_ROW)
    agy_row = next(r for r in rows if r.host == "agy" and r.plugin == hub.MCP_ROW)
    assert codex_row.state == "disabled"
    assert agy_row.state == "disabled"

    cmds = hub.plan_install(rows, hub.load_config(mcp_env.config))
    assert not any("mcp" in c for c in cmds)


def test_mcp_row_list_failure_is_error(mcp_env, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MCP_FAIL", "1")
    monkeypatch.setenv("FAKE_AGY_MCP_FAIL", "1")
    rows = hub.status(mcp_env.config, tmp_path / "repo")
    codex_row = next(r for r in rows if r.host == "codex" and r.plugin == hub.MCP_ROW)
    agy_row = next(r for r in rows if r.host == "agy" and r.plugin == hub.MCP_ROW)
    assert codex_row.state == "error"
    assert "failed: codex mcp error" in codex_row.detail
    assert agy_row.state == "error"
    assert "failed: agy mcp error" in agy_row.detail

    cmds = hub.plan_install(rows, hub.load_config(mcp_env.config))
    assert not any("mcp" in c for c in cmds)

    monkeypatch.delenv("FAKE_CODEX_MCP_FAIL")
    monkeypatch.delenv("FAKE_AGY_MCP_FAIL")
    monkeypatch.setenv("FAKE_CODEX_MCP_BAD_JSON", "1")
    monkeypatch.setenv("FAKE_AGY_MCP_BAD_TABLE", "1")
    rows2 = hub.status(mcp_env.config, tmp_path / "repo")
    codex_row2 = next(r for r in rows2 if r.host == "codex" and r.plugin == hub.MCP_ROW)
    agy_row2 = next(r for r in rows2 if r.host == "agy" and r.plugin == hub.MCP_ROW)
    assert codex_row2.state == "error"
    assert codex_row2.detail == "codex mcp list output not understood"
    assert agy_row2.state == "error"
    assert agy_row2.detail == "agy mcp list output not understood"


def test_mcp_row_host_not_installed(mcp_env, tmp_path):
    (mcp_env.bin / "codex").unlink()
    (mcp_env.bin / "agy").unlink()
    rows = hub.status(mcp_env.config, tmp_path / "repo")
    codex_row = next(r for r in rows if r.host == "codex" and r.plugin == hub.MCP_ROW)
    agy_row = next(r for r in rows if r.host == "agy" and r.plugin == hub.MCP_ROW)
    assert codex_row.state == "host-not-installed"
    assert codex_row.detail == "codex is not on PATH"
    assert agy_row.state == "host-not-installed"
    assert agy_row.detail == "agy is not on PATH"


def test_mcp_rows_respect_hosts_filter(mcp_env, tmp_path):
    rows = hub.status(mcp_env.config, tmp_path / "repo")
    cfg = hub.load_config(mcp_env.config)
    bin_cmd = hub._mcp_command()[0]

    cmds_agy = hub.plan_install(rows, cfg, hosts=("agy",))
    assert cmds_agy == [["agy", "mcp", "add", "agent-hub", bin_cmd, "mcp"]]

    cmds_codex = hub.plan_install(rows, cfg, hosts=("codex",))
    assert cmds_codex == [["codex", "mcp", "add", "agent-hub", "--", bin_cmd, "mcp"]]


def test_mcp_row_missing_bin_is_error(mcp_env, tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "_mcp_bin_path", lambda: tmp_path / "nonexistent" / "agent-hub")
    rows = hub.status(mcp_env.config, tmp_path / "repo")
    codex_row = next(r for r in rows if r.host == "codex" and r.plugin == hub.MCP_ROW)
    agy_row = next(r for r in rows if r.host == "agy" and r.plugin == hub.MCP_ROW)
    assert codex_row.state == "error"
    assert codex_row.detail == "no bin/agent-hub next to the agenthub package"
    assert agy_row.state == "error"
    assert agy_row.detail == "no bin/agent-hub next to the agenthub package"
