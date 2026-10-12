import json
import shutil
import sys

import pytest

from agenthub import hub
from agenthub.cli import main

FAKE_CODEX = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
state = Path(os.environ["FAKE_HUB_STATE"])
args = sys.argv[1:]
with (state / "calls.log").open("a") as f:
    f.write("codex " + " ".join(args) + "\\n")
print("WARNING: noisy", file=sys.stderr)
inst = state / "codex_installed.json"
mps = state / "codex_marketplaces.txt"
if args[:3] == ["plugin", "marketplace", "list"]:
    print("MARKETPLACE  ROOT")
    if mps.exists():
        print(mps.read_text(), end="")
elif args[:3] == ["plugin", "marketplace", "add"]:
    with mps.open("a") as f:
        f.write(f"agent-plugins  {{args[3]}}\\n")
elif args[:2] == ["plugin", "list"]:
    print(json.dumps({{"installed": json.loads(inst.read_text()) if inst.exists() else [], "available": []}}))
elif args[:2] == ["plugin", "add"]:
    if os.environ.get("FAKE_CODEX_FAIL"):
        print(os.environ.get("FAKE_CODEX_STDERR", "boom\\nlast line"), file=sys.stderr)
        sys.exit(3)
    cur = json.loads(inst.read_text()) if inst.exists() else []
    cur.append({{"name": args[2].split("@")[0], "version": "1.0.0"}})
    inst.write_text(json.dumps(cur))
else:
    sys.exit(64)
"""

FAKE_AGY = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
state = Path(os.environ["FAKE_HUB_STATE"])
args = sys.argv[1:]
with (state / "calls.log").open("a") as f:
    f.write("agy " + " ".join(args) + "\\n")
lst = state / "agy_list.json"
if args[:2] == ["plugin", "list"]:
    print(json.dumps({{"imports": json.loads(lst.read_text()) if lst.exists() else []}}))
elif args[:2] == ["plugin", "validate"]:
    if os.environ.get("FAKE_AGY_VALIDATE_FAIL"):
        print("invalid", file=sys.stderr)
        sys.exit(2)
    print("[ok]")
elif args[:2] == ["plugin", "install"]:
    cur = json.loads(lst.read_text()) if lst.exists() else []
    cur.append({{"name": Path(args[2]).name}})
    lst.write_text(json.dumps(cur))
else:
    sys.exit(64)
"""


class Env:
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
        self.market = tmp_path / "market"
        self.src = self.market / "plugins" / "telaf"
        (self.src / "skills" / "a").mkdir(parents=True)
        (self.src / "skills" / "a" / "SKILL.md").write_text("alpha")
        (self.src / ".codex-plugin").mkdir()
        (self.src / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "telaf", "version": "1.0.0"}))
        (self.src / "plugin.json").write_text(json.dumps({"name": "telaf", "version": "1.0.0"}))
        (self.market / ".agents" / "plugins").mkdir(parents=True)
        (self.market / ".agents" / "plugins" / "marketplace.json").write_text(
            json.dumps({"name": "agent-plugins", "plugins": [{"name": "telaf"}]})
        )
        self.write_config()
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("CODEX_HOME", str(self.codex_home))
        monkeypatch.setenv("AGENT_HUB_CONFIG", str(self.config))
        monkeypatch.setenv("FAKE_HUB_STATE", str(self.state))
        for tool in ("git", "python3"):
            real = shutil.which(tool) if tool == "git" else sys.executable
            (self.bin / tool).symlink_to(real)
        monkeypatch.setenv("PATH", str(self.bin))
        self.install_fake("codex", FAKE_CODEX)
        self.install_fake("agy", FAKE_AGY)

    def write_config(self):
        (self.config / "workers.toml").write_text(
            f'default = []\n[plugins]\ntelaf = "{self.src}"\n[hub]\ncodex_marketplace = "{self.market}"\n'
        )

    def install_fake(self, name, text):
        f = self.bin / name
        f.write_text(text)
        f.chmod(0o755)

    def calls(self):
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def mutating_calls(self):
        return [c for c in self.calls() if " list" not in c]

    def install_codex_snapshot(self, version="1.0.0"):
        (self.state / "codex_installed.json").write_text(json.dumps([{"name": "telaf", "version": version}]))
        snap = self.codex_home / "plugins" / "cache" / "agent-plugins" / "telaf" / version
        shutil.copytree(self.src / "skills", snap / "skills")
        return snap

    def install_agy_snapshot(self):
        (self.state / "agy_list.json").write_text(json.dumps([{"name": "telaf"}]))
        snap = self.home / ".gemini" / "config" / "plugins" / "telaf"
        shutil.copytree(self.src / "skills", snap / "skills")
        shutil.copy(self.src / "plugin.json", snap / "plugin.json")
        return snap


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def _state(rows, host, plugin="telaf"):
    return next(r for r in rows if r.host == host and r.plugin == plugin).state


def test_status_missing_when_not_installed(env, tmp_path):
    rows = hub.status(env.config, tmp_path / "repo")
    assert _state(rows, "codex") == "missing"
    assert _state(rows, "agy") == "missing"


def test_status_current_when_hash_and_version_match(env, tmp_path):
    env.install_codex_snapshot()
    env.install_agy_snapshot()
    rows = hub.status(env.config, tmp_path / "repo")
    assert _state(rows, "codex") == "current"
    assert _state(rows, "agy") == "current"


def test_status_stale_when_skills_differ(env, tmp_path):
    snap = env.install_codex_snapshot()
    (snap / "skills" / "a" / "SKILL.md").write_text("changed")
    agy = env.install_agy_snapshot()
    (agy / "skills" / "b.md").write_text("extra")
    rows = hub.status(env.config, tmp_path / "repo")
    assert _state(rows, "codex") == "stale"
    assert _state(rows, "agy") == "stale"


def test_status_stale_when_version_differs(env, tmp_path):
    env.install_codex_snapshot(version="0.9.0")
    agy = env.install_agy_snapshot()
    (agy / "plugin.json").write_text(json.dumps({"name": "telaf", "version": "0.1.0"}))
    rows = hub.status(env.config, tmp_path / "repo")
    codex = next(r for r in rows if r.host == "codex")
    assert codex.state == "stale"
    assert (codex.installed_version, codex.source_version) == ("0.9.0", "1.0.0")
    assert _state(rows, "agy") == "stale"


def test_status_per_run_rows_for_claude_and_gwen(env, tmp_path):
    (env.config / "workers.toml").write_text(
        (env.config / "workers.toml").read_text().replace("default = []", 'default = ["telaf"]')
    )
    rows = hub.status(env.config, tmp_path)
    assert _state(rows, "claude") == "per-run"
    assert _state(rows, "gwen") == "per-run"


def test_status_reports_stray_codex_skills(env, tmp_path):
    for name in ("telaf-api", "telaf-legato", ".system", "other"):
        (env.codex_home / "skills" / name).mkdir(parents=True)
    rows = [r for r in hub.status(env.config, tmp_path / "repo") if r.state == "stray"]
    assert sorted(r.plugin for r in rows) == ["telaf-api", "telaf-legato"]
    assert all(r.detail.startswith(str(env.codex_home / "skills")) for r in rows)
    assert (env.codex_home / "skills" / "telaf-api").is_dir()


def test_status_host_not_installed(env, tmp_path):
    (env.bin / "codex").unlink()
    (env.bin / "agy").unlink()
    rows = hub.status(env.config, tmp_path / "repo")
    assert _state(rows, "codex") == "host-not-installed"
    assert _state(rows, "agy") == "host-not-installed"


def test_install_dry_run_runs_nothing(env, capsys):
    assert hub.install(env.config, apply=False) == 0
    out = capsys.readouterr().out
    assert "codex plugin add telaf@agent-plugins" in out
    assert "agy plugin install" in out
    assert env.mutating_calls() == []


def test_install_apply_registers_marketplace_once(env, tmp_path):
    assert hub.install(env.config, apply=True) == 0
    adds = [c for c in env.calls() if c.startswith("codex plugin marketplace add")]
    assert adds == [f"codex plugin marketplace add {env.market}"]
    assert "codex plugin add telaf@agent-plugins" in env.calls()
    assert hub.install(env.config, apply=True) == 0
    assert len([c for c in env.calls() if c.startswith("codex plugin marketplace add")]) == 1
    # fake snapshot is not copied; refresh stays planned
    assert _state(hub.status(env.config, tmp_path / "repo"), "codex") == "stale"


def test_install_apply_stops_at_first_failure(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    assert hub.install(env.config, apply=True) == 1
    out = capsys.readouterr().out
    assert "exit 3" in out and "last line" in out
    assert not any(c.startswith("agy plugin") and " list" not in c for c in env.calls())


def test_install_validates_before_agy_install(env):
    assert hub.install(env.config, apply=True, hosts=("agy",)) == 0
    agy = [c for c in env.calls() if c.startswith("agy plugin") and "list" not in c]
    assert [c.split()[2] for c in agy] == ["validate", "install"]
    assert not any(c.startswith("codex plugin add") for c in env.calls())


def test_cli_hub_status_json(env, capsys):
    assert main(["hub", "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {(r["host"], r["plugin"], r["state"]) for r in data} >= {
        ("codex", "telaf", "missing"),
        ("agy", "telaf", "missing"),
    }
    assert env.mutating_calls() == []


def test_status_survives_broken_repo_worker_plugins(env, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".agent-hub").mkdir()
    (repo / ".agent-hub" / "config.toml").write_text('worker_plugins = ["nope"]')
    (env.codex_home / "skills" / "telaf-api").mkdir(parents=True)
    rows = hub.status(env.config, repo)
    assert _state(rows, "codex") == "missing"
    assert _state(rows, "agy") == "missing"
    assert _state(rows, "codex", "telaf-api") == "stray"
    err = next(r for r in rows if r.host == "claude+gwen")
    assert (err.plugin, err.state) == ("-", "error")
    assert err.detail


def test_status_matches_by_manifest_name(env, tmp_path):
    (env.config / "workers.toml").write_text((env.config / "workers.toml").read_text().replace("telaf =", "key ="))
    (env.src / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "telaf", "version": "1.0.0"}))
    env.install_codex_snapshot()
    env.install_agy_snapshot()
    rows = hub.status(env.config, tmp_path / "repo")
    codex = next(r for r in rows if r.host == "codex" and r.plugin == "key")
    agy = next(r for r in rows if r.host == "agy" and r.plugin == "key")
    assert (codex.state, agy.state) == ("current", "current")
    assert codex.detail == agy.detail == "manifest name telaf"
    cmds = hub.plan_install(rows, hub.load_config(env.config))
    assert cmds == []
    missing = [r for r in rows if r.state == "current"]
    stale = [hub.Row(r.host, r.plugin, "stale") for r in missing]
    planned = hub.plan_install(stale, hub.load_config(env.config))
    assert ["codex", "plugin", "add", "telaf@agent-plugins"] in planned


def test_strays_match_name_dash_prefix_and_skill_names_only(env, tmp_path):
    for name in ("telaf", "telaf-api", "a", "telafx", "other", ".system"):
        (env.codex_home / "skills" / name).mkdir(parents=True)
    rows = [r for r in hub.status(env.config, tmp_path / "repo") if r.state == "stray"]
    assert sorted(r.plugin for r in rows) == ["a", "telaf", "telaf-api"]


def test_registered_root_with_spaces(env, tmp_path):
    spaced = tmp_path / "my market"
    spaced.mkdir()
    (env.state / "codex_marketplaces.txt").write_text(f"agent-plugins  {spaced}\n")
    assert hub._registered(spaced.resolve()) == "root"
    assert hub._registered(tmp_path / "market") is None


def test_status_host_not_installed_ignores_system_path(env, tmp_path, monkeypatch):
    system = tmp_path / "system"
    system.mkdir()
    for name, text in (("codex", FAKE_CODEX), ("agy", FAKE_AGY)):
        (system / name).write_text(text)
        (system / name).chmod(0o755)
    (env.bin / "codex").unlink()
    (env.bin / "agy").unlink()
    assert not (env.bin / "codex").exists()
    rows = hub.status(env.config, tmp_path / "repo")
    assert _state(rows, "codex") == "host-not-installed"
    assert _state(rows, "agy") == "host-not-installed"
    assert env.calls() == []


def test_dry_run_quotes_paths_with_spaces(env, tmp_path, capsys):
    spaced = tmp_path / "my market"
    shutil.copytree(env.market, spaced)
    (env.config / "workers.toml").write_text(
        f'default = []\n[plugins]\ntelaf = "{spaced / "plugins" / "telaf"}"\n[hub]\ncodex_marketplace = "{spaced}"\n'
    )
    assert hub.install(env.config, apply=False) == 0
    out = capsys.readouterr().out
    assert f"$ codex plugin marketplace add '{spaced.resolve()}'" in out
    assert f"$ agy plugin install '{(spaced / 'plugins' / 'telaf').resolve()}'" in out


def test_install_output_is_sanitised(env, monkeypatch, capsys):
    class Tty:
        def __init__(self, inner):
            self.inner = inner

        def isatty(self):
            return True

        def write(self, text):
            return self.inner.write(text)

        def flush(self):
            self.inner.flush()

    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    monkeypatch.setenv("FAKE_CODEX_STDERR", "bad \x1b[31mred\x07 end")
    monkeypatch.setattr(sys, "stdout", Tty(sys.stdout))
    assert main(["hub", "install", "--apply"]) == 1
    out = capsys.readouterr().out
    assert "bad [31mred end" in out
    assert "\x1b" not in out and "\x07" not in out


def test_install_apply_missing_tool_returns_1(env, monkeypatch, capsys):
    real_run = hub.subprocess.run

    def boom(cmd, **kwargs):
        if "list" in cmd:
            return real_run(cmd, **kwargs)
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(hub.subprocess, "run", boom)
    assert hub.install(env.config, apply=True) == 1
    out = capsys.readouterr().out
    assert out.count("failed: codex plugin marketplace add") == 1
    assert "No such file or directory" in out


def test_marketplace_registered_from_other_root_is_not_readded(env, tmp_path, capsys):
    (env.state / "codex_marketplaces.txt").write_text(f"agent-plugins  {tmp_path / 'elsewhere'}\n")
    cfg = hub.load_config(env.config)
    assert cfg.marketplace_registered == "name"
    assert hub._registered((tmp_path / "market").resolve(), "agent-plugins") == "name"
    rows = hub.status(env.config, tmp_path / "repo")
    codex = next(r for r in rows if r.host == "codex" and r.plugin == "telaf")
    assert "marketplace agent-plugins is registered from another root" in codex.detail
    assert hub.install(env.config, apply=False) == 0
    out = capsys.readouterr().out
    assert "marketplace add" not in out
    assert "codex plugin add telaf@agent-plugins" in out


def _fail_list(monkeypatch, exc):
    real_run = hub.subprocess.run

    def run(cmd, **kwargs):
        if "list" in cmd and cmd[0] in ("codex", "agy") and "marketplace" not in cmd:
            raise exc
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(hub.subprocess, "run", run)


def test_status_reports_list_timeout_as_error_row(env, tmp_path, monkeypatch):
    _fail_list(monkeypatch, hub.subprocess.TimeoutExpired(["x"], 30))
    rows = hub.status(env.config, tmp_path / "repo")
    errors = {r.host: r for r in rows if r.state == "error"}
    assert errors["codex"].detail == "codex list failed: timed out"
    assert errors["agy"].detail == "agy list failed: timed out"
    assert errors["codex"].plugin == "-"
    assert _state(rows, "codex") == "missing"


def test_status_reports_unexecutable_tool(env, tmp_path, monkeypatch):
    _fail_list(monkeypatch, PermissionError(13, "denied"))
    rows = hub.status(env.config, tmp_path / "repo")
    errors = {r.host: r for r in rows if r.state == "error"}
    assert errors["codex"].detail == "codex list failed: <error>"
    assert errors["agy"].detail == "agy list failed: <error>"


def test_install_failure_shows_stdout_when_stderr_empty(env, tmp_path, monkeypatch, capsys):
    env.install_fake(
        "codex",
        FAKE_CODEX.replace(
            'print("WARNING: noisy", file=sys.stderr)',
            'print("stdout reason") if args[:2] == ["plugin", "add"] else None',
        ).replace('print(os.environ.get("FAKE_CODEX_STDERR", "boom\\nlast line"), file=sys.stderr)', "pass"),
    )
    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    assert hub.install(env.config, apply=True) == 1
    out = capsys.readouterr().out
    assert "exit 3" in out
    assert "stdout reason" in out
