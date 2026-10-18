import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from agenthub.config import ConfigError
from agenthub.workers import USER_FILE, _load, _plugins, worker_plugins

HOSTS = ("codex", "agy")
INSTALLABLE = ("missing", "stale")
MCP_ROW = "mcp:agent-hub"
MCP_SERVER_NAME = "agent-hub"


def _mcp_bin_path() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / "agent-hub"


def _mcp_command() -> list[str]:
    return [str(_mcp_bin_path()), "mcp"]


@dataclass(frozen=True)
class Row:
    host: str
    plugin: str
    # current | stale | missing | error | disabled | not-a-worker-plugin | host-not-installed | per-run | stray
    state: str
    installed_version: str | None = None
    source_version: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class HubConfig:
    plugins: dict[str, Path]
    marketplace_dir: Path | None
    marketplace_name: str | None
    marketplace_registered: str | None  # "root" | "name" | "failed" | None
    marketplace_failure: str | None = None


def load_config(config_dir: Path) -> HubConfig:
    """Union of `[plugins]` and the codex marketplace from `[hub]` in workers.toml."""
    user_file = config_dir / USER_FILE
    if not user_file.is_file():
        return HubConfig({}, None, None, None)
    user = _load(user_file)
    plugins = {n: Path(d).expanduser().resolve() for n, d in _plugins(user, user_file).items()}
    hub = user.get("hub", {})
    if not isinstance(hub, dict) or not isinstance(hub.get("codex_marketplace", ""), str):
        raise ConfigError(f"{user_file}: hub must be a table with codex_marketplace = directory string")
    directory = Path(hub["codex_marketplace"]).expanduser().resolve() if "codex_marketplace" in hub else None
    name = None
    registered = None
    failure = None
    if directory is not None:
        name = _marketplace_names(directory)[0]
        if name is not None and shutil.which("codex"):
            registered, failure = _registered_checked(directory, name)
    return HubConfig(plugins, directory, name, registered, failure)


def status(config_dir: Path, repo_root: Path | None = None) -> list[Row]:
    cfg = load_config(config_dir)
    rows: list[Row] = []
    listed = _marketplace_names(cfg.marketplace_dir)[1] if cfg.marketplace_dir else set()
    errors: list[Row] = []
    codex_installed: dict[str, str] = {}
    if shutil.which("codex") and cfg.marketplace_name:
        codex_installed, failure = _codex_installed(cfg)
        if failure:
            errors.append(Row("codex", "-", "error", detail=failure))
    if cfg.marketplace_failure:
        errors.append(Row("codex", "-", "error", detail=f"marketplace list failed: {cfg.marketplace_failure}"))
    agy_installed: set[str] = set()
    if shutil.which("agy"):
        agy_installed, failure = _agy_installed()
        if failure:
            errors.append(Row("agy", "-", "error", detail=failure))
    for name, directory in cfg.plugins.items():
        row = _codex_row(cfg, name, directory, listed, codex_installed)
        if cfg.marketplace_registered == "name" and row.state in ("current", "stale", "missing"):
            note = f"marketplace {cfg.marketplace_name} is registered from another root"
            row = replace(row, detail=f"{row.detail}; {note}" if row.detail else note)
        rows.append(row)
        rows.append(_agy_row(name, directory, agy_installed))
    try:
        resolved = worker_plugins(config_dir, repo_root or _repo_root())
    except ConfigError as exc:
        rows.append(Row("claude+gwen", "-", "error", detail=str(exc)))
        resolved = []
    by_dir = {d: n for n, d in cfg.plugins.items()}
    for host in ("claude", "gwen"):
        for directory in resolved:
            rows.append(Row(host, by_dir.get(directory, directory.name), "per-run", detail=str(directory)))
    rows.extend(errors)
    rows.extend(_strays(cfg))
    rows.append(_codex_mcp_row())
    rows.append(_agy_mcp_row())
    return rows


def _repo_root() -> Path:
    proc = _run("git", "rev-parse", "--show-toplevel")
    top = proc.stdout.strip()
    return Path(top) if proc.returncode == 0 and top else Path.cwd()


def plan_install(rows: list[Row], config: HubConfig, hosts: tuple[str, ...] = HOSTS) -> list[list[str]]:
    cmds: list[list[str]] = []
    todo = [r for r in rows if r.state in INSTALLABLE and r.plugin != MCP_ROW]
    codex_rows = [r for r in todo if r.host == "codex" and "codex" in hosts]
    if codex_rows and config.marketplace_dir is not None and config.marketplace_registered != "failed":
        if config.marketplace_registered is None:
            cmds.append(["codex", "plugin", "marketplace", "add", str(config.marketplace_dir)])
        for r in codex_rows:
            manifest = config.plugins[r.plugin] / ".codex-plugin" / "plugin.json"
            cmds.append(["codex", "plugin", "add", f"{_manifest_name(manifest) or r.plugin}@{config.marketplace_name}"])
    mcp_todo = [r for r in rows if r.state in INSTALLABLE and r.plugin == MCP_ROW]
    bin_cmd = str(_mcp_bin_path())
    if "codex" in hosts:
        for r in (r for r in mcp_todo if r.host == "codex"):
            if r.state == "stale":
                cmds.append(["codex", "mcp", "remove", MCP_SERVER_NAME])
            cmds.append(["codex", "mcp", "add", MCP_SERVER_NAME, "--", bin_cmd, "mcp"])
    if "agy" in hosts:
        for r in (r for r in todo if r.host == "agy"):
            directory = str(config.plugins[r.plugin])
            cmds.append(["agy", "plugin", "validate", directory])
            cmds.append(["agy", "plugin", "install", directory])
        for _r in (r for r in mcp_todo if r.host == "agy"):
            cmds.append(["agy", "mcp", "add", MCP_SERVER_NAME, bin_cmd, "mcp"])
    return cmds


def install(
    config_dir: Path,
    apply: bool,
    hosts: tuple[str, ...] = HOSTS,
    emit: Callable[[str], None] = print,
    repo_root: Path | None = None,
) -> int:
    cfg = load_config(config_dir)
    cmds = plan_install(status(config_dir, repo_root), cfg, hosts)
    if not cmds:
        emit("nothing to install")
    for cmd in cmds:
        emit("$ " + shlex.join(cmd))
    if not apply:
        return 0
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            emit(f"failed (timeout): {shlex.join(cmd)}")
            return 1
        except OSError as exc:
            emit(f"failed: {shlex.join(cmd)}: {exc}")
            return 1
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr.strip() or proc.stdout.strip()).splitlines()[-10:])
            emit(f"failed (exit {proc.returncode}): {shlex.join(cmd)}\n{tail}")
            return 1
    emit("")
    emit(format_rows(status(config_dir, repo_root)))
    return 0


def format_rows(rows: list[Row]) -> str:
    table = [("HOST", "PLUGIN", "STATE", "INSTALLED", "SOURCE", "DETAIL")] + [
        (r.host, r.plugin, r.state, r.installed_version or "-", r.source_version or "-", r.detail) for r in rows
    ]
    widths = [max(len(line[i]) for line in table) for i in range(5)]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line[:5])) + ("  " + line[5] if line[5] else "")
        for line in table
    )


def rows_json(rows: list[Row]) -> str:
    return json.dumps([asdict(r) for r in rows], indent=2)


def skills_hash(skills: Path) -> str | None:
    """sha256 over relative path + bytes of every file under `skills/`."""
    if not skills.is_dir():
        return None
    digest = hashlib.sha256()
    for f in sorted(p for p in skills.rglob("*") if p.is_file()):
        digest.update(f.relative_to(skills).as_posix().encode() + b"\0")
        digest.update(f.read_bytes() + b"\0")
    return digest.hexdigest()


def _version(manifest: Path) -> str | None:
    try:
        v = json.loads(manifest.read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) else None


def _manifest_name(manifest: Path) -> str | None:
    try:
        n = json.loads(manifest.read_text()).get("name")
    except (OSError, ValueError, AttributeError):
        return None
    return n if isinstance(n, str) and n else None


def _name_detail(key: str, name: str) -> str:
    return "" if key == name else f"manifest name {name}"


def _no_version(detail: str, source: str | None) -> str:
    if source is not None:
        return detail
    return f"{detail}; no version in manifest" if detail else "no version in manifest"


def _run(*argv: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "timed out")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _failure(proc: subprocess.CompletedProcess) -> str | None:
    if proc.returncode == 0:
        return None
    return "\n".join(proc.stderr.strip().splitlines()[-1:]) or f"exit {proc.returncode}"


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path(os.environ.get("HOME", "~")).expanduser() / ".codex")


def _marketplace_names(directory: Path | None) -> tuple[str | None, set[str]]:
    if directory is None:
        return None, set()
    try:
        data = json.loads((directory / ".agents" / "plugins" / "marketplace.json").read_text())
        return data["name"], {p["name"] for p in data["plugins"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None, set()


def _registered(directory: Path, name: str | None = None) -> str | None:
    """ "root" | "name" | None, or "failed" when the list command fails."""
    return _registered_checked(directory, name)[0]


def _registered_checked(directory: Path, name: str | None = None) -> tuple[str | None, str | None]:
    proc = _run("codex", "plugin", "marketplace", "list")
    if failure := _failure(proc):
        return "failed", failure
    by_name = False
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        if Path(parts[1].strip()).expanduser().resolve() == directory:
            return "root", None
        by_name = by_name or (name is not None and parts[0] == name)
    return ("name" if by_name else None), None


def _codex_installed(cfg: HubConfig) -> tuple[dict[str, str], str | None]:
    proc = _run("codex", "plugin", "list", "-m", str(cfg.marketplace_name), "--json")
    if failure := _failure(proc):
        return {}, f"codex list failed: {failure}"
    try:
        data = json.loads(proc.stdout)
        return {p["name"]: str(p.get("version", "")) for p in data["installed"]}, None
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}, "codex list output not understood"


def _agy_installed() -> tuple[set[str], str | None]:
    proc = _run("agy", "plugin", "list")
    if failure := _failure(proc):
        return set(), f"agy list failed: {failure}"
    try:
        return {p["name"] for p in json.loads(proc.stdout)["imports"]}, None
    except (ValueError, KeyError, TypeError, AttributeError):
        return set(), "agy list output not understood"


def _codex_row(cfg: HubConfig, name: str, directory: Path, listed: set[str], installed: dict[str, str]) -> Row:
    if not shutil.which("codex"):
        return Row("codex", name, "host-not-installed", detail="codex is not on PATH")
    manifest = directory / ".codex-plugin" / "plugin.json"
    if not manifest.is_file():
        return Row("codex", name, "not-a-worker-plugin", detail=f"no {manifest.relative_to(directory)}")
    if cfg.marketplace_dir is None or cfg.marketplace_name is None:
        return Row("codex", name, "not-a-worker-plugin", detail="no usable [hub] codex_marketplace")
    host_name = _manifest_name(manifest) or name
    detail = _name_detail(name, host_name)
    if host_name not in listed:
        return Row("codex", name, "not-a-worker-plugin", detail="not listed in the codex marketplace file")
    source = _version(manifest)
    if host_name not in installed:
        return Row("codex", name, "missing", source_version=source, detail=detail)
    version = installed[host_name]
    snapshot = _codex_home() / "plugins" / "cache" / cfg.marketplace_name / host_name / version / "skills"
    same = (source is None or version == source) and skills_hash(snapshot) == skills_hash(directory / "skills")
    return Row("codex", name, "current" if same else "stale", version, source, _no_version(detail, source))


def _agy_row(name: str, directory: Path, installed: set[str]) -> Row:
    if not shutil.which("agy"):
        return Row("agy", name, "host-not-installed", detail="agy is not on PATH")
    manifest = directory / "plugin.json"
    if not manifest.is_file():
        return Row("agy", name, "not-a-worker-plugin", detail="no plugin.json")
    host_name = _manifest_name(manifest) or name
    detail = _name_detail(name, host_name)
    source = _version(manifest)
    if host_name not in installed:
        return Row("agy", name, "missing", source_version=source, detail=detail)
    snapshot = Path(os.environ.get("HOME", "~")).expanduser() / ".gemini" / "config" / "plugins" / host_name
    version = _version(snapshot / "plugin.json")
    same_skills = skills_hash(snapshot / "skills") == skills_hash(directory / "skills")
    same = (source is None or version == source) and same_skills
    return Row("agy", name, "current" if same else "stale", version, source, _no_version(detail, source))


def _strays(cfg: HubConfig) -> list[Row]:
    skills = _codex_home() / "skills"
    if not skills.is_dir():
        return []
    exact: set[str] = set()
    prefixes: list[str] = []
    for key, directory in cfg.plugins.items():
        name = _manifest_name(directory / ".codex-plugin" / "plugin.json") or _manifest_name(directory / "plugin.json")
        name = name or key
        exact.add(name)
        prefixes.append(f"{name}-")
        plugin_skills = directory / "skills"
        if plugin_skills.is_dir():
            exact.update(s.name for s in plugin_skills.iterdir() if s.is_dir())
    return [
        Row("codex", d.name, "stray", detail=str(d))
        for d in sorted(skills.iterdir())
        if d.is_dir() and d.name != ".system" and (d.name in exact or d.name.startswith(tuple(prefixes)))
    ]


def _codex_mcp_row() -> Row:
    if not shutil.which("codex"):
        return Row("codex", MCP_ROW, "host-not-installed", detail="codex is not on PATH")
    bin_path = _mcp_bin_path()
    if not bin_path.is_file():
        return Row("codex", MCP_ROW, "error", detail="no bin/agent-hub next to the agenthub package")
    proc = _run("codex", "mcp", "list", "--json")
    if failure := _failure(proc):
        return Row("codex", MCP_ROW, "error", detail=f"codex mcp list failed: {failure}")
    try:
        data = json.loads(proc.stdout)
        if not isinstance(data, list):
            return Row("codex", MCP_ROW, "error", detail="codex mcp list output not understood")
    except (ValueError, TypeError):
        return Row("codex", MCP_ROW, "error", detail="codex mcp list output not understood")
    target = next((item for item in data if isinstance(item, dict) and item.get("name") == MCP_SERVER_NAME), None)
    if target is None:
        return Row("codex", MCP_ROW, "missing")
    if target.get("enabled") is False:
        return Row("codex", MCP_ROW, "disabled")
    transport = target.get("transport")
    if not isinstance(transport, dict):
        return Row("codex", MCP_ROW, "error", detail="codex mcp list output not understood")
    cmd = transport.get("command") or ""
    raw_args = transport.get("args")
    if raw_args is None:
        args: list[str] = []
    elif isinstance(raw_args, list):
        args = [str(a) for a in raw_args]
    else:
        args = [str(raw_args)]
    registered = " ".join([cmd] + args).strip()
    if cmd == str(bin_path) and args == ["mcp"]:
        return Row("codex", MCP_ROW, "current")
    return Row("codex", MCP_ROW, "stale", detail=registered)


def _agy_mcp_row() -> Row:
    if not shutil.which("agy"):
        return Row("agy", MCP_ROW, "host-not-installed", detail="agy is not on PATH")
    bin_path = _mcp_bin_path()
    if not bin_path.is_file():
        return Row("agy", MCP_ROW, "error", detail="no bin/agent-hub next to the agenthub package")
    proc = _run("agy", "mcp", "list")
    if failure := _failure(proc):
        return Row("agy", MCP_ROW, "error", detail=f"agy mcp list failed: {failure}")
    out = proc.stdout.strip()
    if not out or "No MCP servers configured" in out:
        return Row("agy", MCP_ROW, "missing")
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        return Row("agy", MCP_ROW, "missing")
    header_parts = re.split(r"\s{2,}", lines[0])
    if not header_parts or "NAME" not in header_parts[0]:
        return Row("agy", MCP_ROW, "error", detail="agy mcp list output not understood")
    target = None
    for line in lines[1:]:
        parts = re.split(r"\s{2,}", line, maxsplit=3)
        if len(parts) >= 4 and parts[0] == MCP_SERVER_NAME:
            target = parts
            break
    if target is None:
        return Row("agy", MCP_ROW, "missing")
    _name, _type, status_val, cmd_url = target
    if status_val == "disabled":
        return Row("agy", MCP_ROW, "disabled")
    expected = f"{bin_path} mcp"
    if cmd_url == expected:
        return Row("agy", MCP_ROW, "current")
    return Row("agy", MCP_ROW, "stale", detail=cmd_url)
