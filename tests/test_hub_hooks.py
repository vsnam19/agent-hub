import json
import shutil
import sys
from pathlib import Path

import pytest

from agenthub import hub

REAL_CODEX_HERDR = {
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "command": "bash '/home/namvs/.codex/herdr-agent-state.sh' session",
                        "timeout": 10,
                        "type": "command",
                    }
                ]
            }
        ]
    }
}

REAL_AGY_HERDR = {
    "herdr": {
        "PreInvocation": [
            {
                "command": "bash '/home/namvs/.gemini/config/hooks/herdr-agent-state.sh' session",
                "timeout": 10,
                "type": "command",
            }
        ]
    }
}

FAKE_CODEX = f"""#!{sys.executable}
import sys
args = sys.argv[1:]
if args[:3] == ["mcp", "list", "--json"]:
    print("[]")
else:
    sys.exit(0)
"""

FAKE_AGY = f"""#!{sys.executable}
import sys
args = sys.argv[1:]
if args[:2] == ["plugin", "list"]:
    print("Installed plugins:")
elif args[:2] == ["mcp", "list"]:
    print("No MCP servers configured.")
else:
    sys.exit(0)
"""


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    codex_home = tmp_path / "codex_home"
    config = tmp_path / "config"
    repo_root = tmp_path / "repo"
    home.mkdir()
    codex_home.mkdir()
    config.mkdir()
    repo_root.mkdir()
    (config / "workers.toml").write_text("default = []\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(config))

    for tool in ("git", "python3"):
        real = shutil.which(tool) if tool == "git" else sys.executable
        (bin_dir / tool).symlink_to(real)
    for name, script in (("codex", FAKE_CODEX), ("agy", FAKE_AGY)):
        p = bin_dir / name
        p.write_text(script, encoding="utf-8")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return {
        "home": home,
        "codex_home": codex_home,
        "config": config,
        "repo_root": repo_root,
        "codex_hooks": codex_home / "hooks.json",
        "agy_hooks": home / ".gemini" / "config" / "hooks.json",
    }


def test_codex_keeps_herdr_entry_and_appends_last(tmp_path):
    hooks_path = tmp_path / "codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(json.dumps(REAL_CODEX_HERDR, indent=2) + "\n", encoding="utf-8")
    root = tmp_path / "agent-hub"

    changed = hub.register_codex_hooks(hooks_path, root)
    assert changed is True

    raw = hooks_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    data = json.loads(raw)
    assert raw == json.dumps(data, indent=2) + "\n"

    session_groups = data["hooks"]["SessionStart"]
    assert len(session_groups) == 2
    assert session_groups[0] == REAL_CODEX_HERDR["hooks"]["SessionStart"][0]
    assert session_groups[1] == {
        "hooks": [
            {
                "type": "command",
                "command": f"python3 -I {root / 'hooks' / 'audit-hook.py'} --host codex SessionStart",
                "timeout": 5,
            }
        ]
    }
    assert data["hooks"]["PostToolUse"] == [
        {
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": f"python3 -I {root / 'hooks' / 'audit-hook.py'} --host codex PostToolUse",
                    "timeout": 5,
                }
            ],
        }
    ]

    # Add a trailing group after audit-hook.py and update with a new root:
    # stale entry must be replaced in place (index 1), never moved to the end.
    trailing_group = {"hooks": [{"type": "command", "command": "echo trailing", "timeout": 3}]}
    data["hooks"]["SessionStart"].append(trailing_group)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    new_root = tmp_path / "new root"
    assert hub.register_codex_hooks(hooks_path, new_root) is True
    updated = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert len(updated["hooks"]["SessionStart"]) == 3
    assert updated["hooks"]["SessionStart"][0] == REAL_CODEX_HERDR["hooks"]["SessionStart"][0]
    assert updated["hooks"]["SessionStart"][1] == {
        "hooks": [
            {
                "type": "command",
                "command": f"python3 -I '{new_root / 'hooks' / 'audit-hook.py'}' --host codex SessionStart",
                "timeout": 5,
            }
        ]
    }
    assert updated["hooks"]["SessionStart"][2] == trailing_group


def test_registration_idempotent_both_hosts(tmp_path):
    codex_path = tmp_path / "codex" / "hooks.json"
    agy_path = tmp_path / "agy" / "hooks.json"
    root = tmp_path / "repo"

    assert hub.register_codex_hooks(codex_path, root) is True
    assert hub.register_agy_hooks(agy_path, root) is True

    codex_bytes = codex_path.read_bytes()
    agy_bytes = agy_path.read_bytes()

    assert hub.register_codex_hooks(codex_path, root) is False
    assert hub.register_agy_hooks(agy_path, root) is False
    assert codex_path.read_bytes() == codex_bytes
    assert agy_path.read_bytes() == agy_bytes

    # Stale command updates in place and then is idempotent again
    root2 = tmp_path / "repo2"
    assert hub.register_codex_hooks(codex_path, root2) is True
    assert hub.register_agy_hooks(agy_path, root2) is True
    assert hub.register_codex_hooks(codex_path, root2) is False
    assert hub.register_agy_hooks(agy_path, root2) is False


def test_agy_keeps_herdr_group_and_adds_agent_hub_group(tmp_path):
    agy_path = tmp_path / ".gemini" / "config" / "hooks.json"
    agy_path.parent.mkdir(parents=True)
    agy_path.write_text(json.dumps(REAL_AGY_HERDR, indent=2) + "\n", encoding="utf-8")
    root = tmp_path / "repo"

    assert hub.register_agy_hooks(agy_path, root) is True
    raw = agy_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    data = json.loads(raw)
    assert raw == json.dumps(data, indent=2) + "\n"

    assert list(data.keys()) == ["herdr", "agent-hub"]
    assert data["herdr"] == REAL_AGY_HERDR["herdr"]
    assert data["agent-hub"] == {
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"python3 -I {root / 'hooks' / 'audit-hook.py'} --host agy PostToolUse",
                        "timeout": 5,
                    }
                ],
            }
        ],
        "SessionStart": [
            {
                "type": "command",
                "command": f"python3 -I {root / 'hooks' / 'audit-hook.py'} --host agy SessionStart",
                "timeout": 5,
            }
        ],
        "Stop": [
            {
                "type": "command",
                "command": f"python3 -I {root / 'hooks' / 'audit-hook.py'} --host agy Stop",
                "timeout": 5,
            }
        ],
    }


def test_agy_disabled_group_left_alone(tmp_path):
    agy_path = tmp_path / ".gemini" / "config" / "hooks.json"
    agy_path.parent.mkdir(parents=True)
    root = tmp_path / "repo"

    # Case 1: agent-hub group itself has enabled: false -> left completely untouched
    disabled_agent_hub = {
        "herdr": REAL_AGY_HERDR["herdr"],
        "agent-hub": {
            "enabled": False,
            "SessionStart": [{"type": "command", "command": "stale", "timeout": 5}],
        },
    }
    initial_text = json.dumps(disabled_agent_hub, indent=2) + "\n"
    agy_path.write_text(initial_text, encoding="utf-8")

    assert hub.register_agy_hooks(agy_path, root) is False
    assert agy_path.read_text(encoding="utf-8") == initial_text

    # Case 2: another group has enabled: false -> kept untouched while agent-hub is added
    disabled_other = {
        "safety-gate": {
            "enabled": False,
            "PreToolUse": [{"matcher": "*", "hooks": [{"command": "./check.sh"}]}],
        }
    }
    agy_path.write_text(json.dumps(disabled_other, indent=2) + "\n", encoding="utf-8")
    assert hub.register_agy_hooks(agy_path, root) is True
    data = json.loads(agy_path.read_text(encoding="utf-8"))
    assert data["safety-gate"] == disabled_other["safety-gate"]
    assert "agent-hub" in data


def test_install_never_touches_codex_config_toml(hook_env):
    config_toml = hook_env["codex_home"] / "config.toml"
    original_toml = (
        '[features]\nhooks = true\n\n[hooks.state."/home/user/.codex/hooks.json:session_start:0:0"]\n'
        'trusted_hash = "deadbeef"\n'
    )
    config_toml.write_text(original_toml, encoding="utf-8")

    rc = hub.install(hook_env["config"], apply=True, repo_root=hook_env["repo_root"])
    assert rc == 0
    assert config_toml.read_text(encoding="utf-8") == original_toml
    assert "trusted_hash" not in hook_env["codex_hooks"].read_text(encoding="utf-8")
    assert "trusted_hash" not in hook_env["agy_hooks"].read_text(encoding="utf-8")


def test_dry_run_prints_and_writes_nothing(hook_env, capsys):
    rc = hub.install(hook_env["config"], apply=False, repo_root=hook_env["repo_root"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"would register audit hook in {hook_env['codex_hooks']}" in out
    assert f"would register audit hook in {hook_env['agy_hooks']}" in out
    assert not hook_env["codex_hooks"].exists()
    assert not hook_env["agy_hooks"].exists()

    # Host filtering on dry-run and apply
    rc_codex = hub.install(hook_env["config"], apply=False, hosts=("codex",), repo_root=hook_env["repo_root"])
    assert rc_codex == 0
    out_codex = capsys.readouterr().out
    assert f"would register audit hook in {hook_env['codex_hooks']}" in out_codex
    assert f"would register audit hook in {hook_env['agy_hooks']}" not in out_codex

    assert hub.install(hook_env["config"], apply=True, hosts=("codex",), repo_root=hook_env["repo_root"]) == 0
    assert hook_env["codex_hooks"].is_file()
    assert not hook_env["agy_hooks"].exists()


def test_install_invalid_json_in_hooks_file_emits_failure_and_returns_1(hook_env, capsys):
    assert (
        hub.audit_hook_command("codex", "PostToolUse", Path("/opt/agent-hub"))
        == "python3 -I /opt/agent-hub/hooks/audit-hook.py --host codex PostToolUse"
    )
    assert (
        hub.audit_hook_command("agy", "SessionStart", Path("/opt/my repo"))
        == "python3 -I '/opt/my repo/hooks/audit-hook.py' --host agy SessionStart"
    )

    bad_json = "{not valid json\n"
    hook_env["codex_hooks"].write_text(bad_json, encoding="utf-8")

    rc = hub.install(hook_env["config"], apply=True, repo_root=hook_env["repo_root"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "failed" in out
    assert str(hook_env["codex_hooks"]) in out
    assert hook_env["codex_hooks"].read_text(encoding="utf-8") == bad_json
    assert not hook_env["agy_hooks"].exists()

    hook_env["codex_hooks"].unlink()
    hook_env["agy_hooks"].parent.mkdir(parents=True, exist_ok=True)
    hook_env["agy_hooks"].write_text(bad_json, encoding="utf-8")

    rc_agy = hub.install(hook_env["config"], apply=True, hosts=("agy",), repo_root=hook_env["repo_root"])
    assert rc_agy == 1
    out_agy = capsys.readouterr().out
    assert "failed" in out_agy
    assert str(hook_env["agy_hooks"]) in out_agy
    assert hook_env["agy_hooks"].read_text(encoding="utf-8") == bad_json
