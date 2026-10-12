import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from agenthub.config import ConfigError
from agenthub.workers import USER_FILE, _load, _plugins, worker_plugins

HOSTS = ("codex", "agy")
INSTALLABLE = ("missing", "stale")


@dataclass(frozen=True)
class Row:
    host: str
    plugin: str
    state: str  # current | stale | missing | not-a-worker-plugin | host-not-installed | per-run | stray
    installed_version: str | None = None
    source_version: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class HubConfig:
    plugins: dict[str, Path]
    marketplace_dir: Path | None
    marketplace_name: str | None
    marketplace_registered: bool


def load_config(config_dir: Path) -> HubConfig:
    """Union of `[plugins]` and the codex marketplace from `[hub]` in workers.toml."""
    user_file = config_dir / USER_FILE
    if not user_file.is_file():
        return HubConfig({}, None, None, False)
    user = _load(user_file)
    plugins = {n: Path(d).expanduser().resolve() for n, d in _plugins(user, user_file).items()}
    hub = user.get("hub", {})
    if not isinstance(hub, dict) or not isinstance(hub.get("codex_marketplace", ""), str):
        raise ConfigError(f"{user_file}: hub must be a table with codex_marketplace = directory string")
    directory = Path(hub["codex_marketplace"]).expanduser().resolve() if "codex_marketplace" in hub else None
    name = None
    registered = False
    if directory is not None:
        name = _marketplace_names(directory)[0]
        if name is not None and shutil.which("codex"):
            registered = _registered(directory)
    return HubConfig(plugins, directory, name, registered)


def status(config_dir: Path, repo_root: Path | None = None) -> list[Row]:
    cfg = load_config(config_dir)
    rows: list[Row] = []
    listed = _marketplace_names(cfg.marketplace_dir)[1] if cfg.marketplace_dir else set()
    codex_installed = _codex_installed(cfg) if shutil.which("codex") and cfg.marketplace_name else {}
    agy_installed = _agy_installed() if shutil.which("agy") else {}
    for name, directory in cfg.plugins.items():
        rows.append(_codex_row(cfg, name, directory, listed, codex_installed))
        rows.append(_agy_row(name, directory, agy_installed))
    resolved = worker_plugins(config_dir, repo_root or Path.cwd())
    by_dir = {d: n for n, d in cfg.plugins.items()}
    for host in ("claude", "gwen"):
        for directory in resolved:
            rows.append(Row(host, by_dir.get(directory, directory.name), "per-run", detail=str(directory)))
    rows.extend(_strays(cfg))
    return rows


def plan_install(rows: list[Row], config: HubConfig, hosts: tuple[str, ...] = HOSTS) -> list[list[str]]:
    cmds: list[list[str]] = []
    todo = [r for r in rows if r.state in INSTALLABLE]
    codex_rows = [r for r in todo if r.host == "codex" and "codex" in hosts]
    if codex_rows and config.marketplace_dir is not None:
        if not config.marketplace_registered:
            cmds.append(["codex", "plugin", "marketplace", "add", str(config.marketplace_dir)])
        for r in codex_rows:
            cmds.append(["codex", "plugin", "add", f"{r.plugin}@{config.marketplace_name}"])
    if "agy" in hosts:
        for r in (r for r in todo if r.host == "agy"):
            directory = str(config.plugins[r.plugin])
            cmds.append(["agy", "plugin", "validate", directory])
            cmds.append(["agy", "plugin", "install", directory])
    return cmds


def install(config_dir: Path, apply: bool, hosts: tuple[str, ...] = HOSTS) -> int:
    cfg = load_config(config_dir)
    cmds = plan_install(status(config_dir), cfg, hosts)
    if not cmds:
        print("nothing to install")
    for cmd in cmds:
        print("$ " + " ".join(cmd))
    if not apply:
        return 0
    for cmd in cmds:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = "\n".join(proc.stderr.strip().splitlines()[-10:])
            print(f"failed (exit {proc.returncode}): {' '.join(cmd)}\n{tail}")
            return 1
    print()
    print(format_rows(status(config_dir)))
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


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


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


def _registered(directory: Path) -> bool:
    proc = _run("codex", "plugin", "marketplace", "list")
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and Path(parts[-1]).expanduser().resolve() == directory:
            return True
    return False


def _codex_installed(cfg: HubConfig) -> dict[str, str]:
    proc = _run("codex", "plugin", "list", "-m", str(cfg.marketplace_name), "--json")
    try:
        data = json.loads(proc.stdout)
        return {p["name"]: str(p.get("version", "")) for p in data.get("installed", [])}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}


def _agy_installed() -> set[str]:
    proc = _run("agy", "plugin", "list")
    try:
        return {p["name"] for p in json.loads(proc.stdout).get("imports", [])}
    except (ValueError, KeyError, TypeError, AttributeError):
        return set()


def _codex_row(cfg: HubConfig, name: str, directory: Path, listed: set[str], installed: dict[str, str]) -> Row:
    if not shutil.which("codex"):
        return Row("codex", name, "host-not-installed", detail="codex is not on PATH")
    manifest = directory / ".codex-plugin" / "plugin.json"
    if not manifest.is_file():
        return Row("codex", name, "not-a-worker-plugin", detail=f"no {manifest.relative_to(directory)}")
    if cfg.marketplace_dir is None or cfg.marketplace_name is None:
        return Row("codex", name, "not-a-worker-plugin", detail="no usable [hub] codex_marketplace")
    if name not in listed:
        return Row("codex", name, "not-a-worker-plugin", detail="not listed in the codex marketplace file")
    source = _version(manifest)
    if name not in installed:
        return Row("codex", name, "missing", source_version=source)
    version = installed[name]
    snapshot = _codex_home() / "plugins" / "cache" / cfg.marketplace_name / name / version / "skills"
    same = version == source and skills_hash(snapshot) == skills_hash(directory / "skills")
    return Row("codex", name, "current" if same else "stale", version, source)


def _agy_row(name: str, directory: Path, installed: set[str]) -> Row:
    if not shutil.which("agy"):
        return Row("agy", name, "host-not-installed", detail="agy is not on PATH")
    manifest = directory / "plugin.json"
    if not manifest.is_file():
        return Row("agy", name, "not-a-worker-plugin", detail="no plugin.json")
    source = _version(manifest)
    if name not in installed:
        return Row("agy", name, "missing", source_version=source)
    snapshot = Path(os.environ.get("HOME", "~")).expanduser() / ".gemini" / "config" / "plugins" / name
    version = _version(snapshot / "plugin.json")
    same = version == source and skills_hash(snapshot / "skills") == skills_hash(directory / "skills")
    return Row("agy", name, "current" if same else "stale", version, source)


def _strays(cfg: HubConfig) -> list[Row]:
    skills = _codex_home() / "skills"
    if not skills.is_dir():
        return []
    return [
        Row("codex", d.name, "stray", detail=str(d))
        for d in sorted(skills.iterdir())
        if d.is_dir() and d.name != ".system" and any(d.name.startswith(n) for n in cfg.plugins)
    ]
