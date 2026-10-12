import hashlib
import json
import os
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


@dataclass(frozen=True)
class Row:
    host: str
    plugin: str
    state: str  # current | stale | missing | error | not-a-worker-plugin | host-not-installed | per-run | stray
    installed_version: str | None = None
    source_version: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class HubConfig:
    plugins: dict[str, Path]
    marketplace_dir: Path | None
    marketplace_name: str | None
    marketplace_registered: str | None  # "root" | "name" | None


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
    if directory is not None:
        name = _marketplace_names(directory)[0]
        if name is not None and shutil.which("codex"):
            registered = _registered(directory, name)
    return HubConfig(plugins, directory, name, registered)


def status(config_dir: Path, repo_root: Path | None = None) -> list[Row]:
    cfg = load_config(config_dir)
    rows: list[Row] = []
    listed = _marketplace_names(cfg.marketplace_dir)[1] if cfg.marketplace_dir else set()
    errors: list[Row] = []
    codex_installed: dict[str, str] = {}
    if shutil.which("codex") and cfg.marketplace_name:
        codex_installed, failure = _codex_installed(cfg)
        if failure:
            errors.append(Row("codex", "-", "error", detail=f"codex list failed: {failure}"))
    agy_installed: set[str] = set()
    if shutil.which("agy"):
        agy_installed, failure = _agy_installed()
        if failure:
            errors.append(Row("agy", "-", "error", detail=f"agy list failed: {failure}"))
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
    return rows


def _repo_root() -> Path:
    proc = _run("git", "rev-parse", "--show-toplevel")
    top = proc.stdout.strip()
    return Path(top) if proc.returncode == 0 and top else Path.cwd()


def plan_install(rows: list[Row], config: HubConfig, hosts: tuple[str, ...] = HOSTS) -> list[list[str]]:
    cmds: list[list[str]] = []
    todo = [r for r in rows if r.state in INSTALLABLE]
    codex_rows = [r for r in todo if r.host == "codex" and "codex" in hosts]
    if codex_rows and config.marketplace_dir is not None:
        if config.marketplace_registered is None:
            cmds.append(["codex", "plugin", "marketplace", "add", str(config.marketplace_dir)])
        for r in codex_rows:
            manifest = config.plugins[r.plugin] / ".codex-plugin" / "plugin.json"
            cmds.append(["codex", "plugin", "add", f"{_manifest_name(manifest) or r.plugin}@{config.marketplace_name}"])
    if "agy" in hosts:
        for r in (r for r in todo if r.host == "agy"):
            directory = str(config.plugins[r.plugin])
            cmds.append(["agy", "plugin", "validate", directory])
            cmds.append(["agy", "plugin", "install", directory])
    return cmds


def install(config_dir: Path, apply: bool, hosts: tuple[str, ...] = HOSTS, emit: Callable[[str], None] = print) -> int:
    cfg = load_config(config_dir)
    cmds = plan_install(status(config_dir), cfg, hosts)
    if not cmds:
        emit("nothing to install")
    for cmd in cmds:
        emit("$ " + shlex.join(cmd))
    if not apply:
        return 0
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as exc:
            emit(f"failed: {shlex.join(cmd)}: {exc}")
            return 1
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr.strip() or proc.stdout.strip()).splitlines()[-10:])
            emit(f"failed (exit {proc.returncode}): {shlex.join(cmd)}\n{tail}")
            return 1
    emit("")
    emit(format_rows(status(config_dir)))
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


def _run(*argv: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "timed out")
    except OSError:
        return subprocess.CompletedProcess(argv, 127, "", "<error>")


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
    proc = _run("codex", "plugin", "marketplace", "list")
    by_name = False
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        if Path(parts[1].strip()).expanduser().resolve() == directory:
            return "root"
        by_name = by_name or (name is not None and parts[0] == name)
    return "name" if by_name else None


def _codex_installed(cfg: HubConfig) -> tuple[dict[str, str], str | None]:
    proc = _run("codex", "plugin", "list", "-m", str(cfg.marketplace_name), "--json")
    if failure := _failure(proc):
        return {}, failure
    try:
        data = json.loads(proc.stdout)
        return {p["name"]: str(p.get("version", "")) for p in data.get("installed", [])}, None
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}, None


def _agy_installed() -> tuple[set[str], str | None]:
    proc = _run("agy", "plugin", "list")
    if failure := _failure(proc):
        return set(), failure
    try:
        return {p["name"] for p in json.loads(proc.stdout).get("imports", [])}, None
    except (ValueError, KeyError, TypeError, AttributeError):
        return set(), None


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
    same = version == source and skills_hash(snapshot) == skills_hash(directory / "skills")
    return Row("codex", name, "current" if same else "stale", version, source, detail)


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
    same = version == source and skills_hash(snapshot / "skills") == skills_hash(directory / "skills")
    return Row("agy", name, "current" if same else "stale", version, source, detail)


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
