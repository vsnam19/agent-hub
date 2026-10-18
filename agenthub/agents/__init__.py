import importlib
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class Outcome(StrEnum):
    OK = "ok"
    QUOTA = "quota"
    AUTH = "auth"
    MODEL_UNAVAILABLE = "model-unavailable"
    TRANSIENT = "transient"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    FAILED = "failed"


@dataclass(frozen=True)
class AgentConfig:
    binary: str
    version_args: list[str]
    env_passthrough: list[str]
    read_only: str


@dataclass(frozen=True)
class Request:
    role: str
    model: str
    workdir: Path
    brief_path: Path
    attempt_dir: Path
    git_common_dir: Path
    plugin_dirs: tuple[Path, ...] = ()
    effort: str = ""


@dataclass(frozen=True)
class Invocation:
    argv: list[str]
    stdin_path: Path | None
    env: dict[str, str] | None = None  # extra child variables over the filtered env; never recorded


@dataclass(frozen=True)
class Classified:
    outcome: Outcome
    reply: str
    reset_at: datetime | None = None
    detail: str = ""


class Adapter:
    name: str
    config: AgentConfig
    profile_path: Path

    def preflight(self, req: Request) -> Classified | None:
        return None

    def build(self, req: Request) -> Invocation:
        raise NotImplementedError

    def classify(self, rc: int, attempt_dir: Path) -> Classified:
        raise NotImplementedError

    def installed(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def version(self) -> str | None:
        if not self.installed():
            return None
        try:
            res = subprocess.run(
                [self.config.binary] + self.config.version_args,
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
            if res.returncode == 0 and res.stdout:
                return res.stdout.splitlines()[0]
        except Exception:
            pass
        return None


def discover(package_dir: Path, package_name: str) -> dict[str, Adapter]:
    adapters = {}
    if not package_dir.is_dir():
        return adapters

    for item in package_dir.iterdir():
        if not item.is_dir() or item.name.startswith("__"):
            continue

        agent_toml = item / "agent.toml"
        if not agent_toml.is_file():
            continue

        with agent_toml.open("rb") as f:
            cfg_data = tomllib.load(f)

        config = AgentConfig(
            binary=cfg_data.get("binary", ""),
            version_args=cfg_data.get("version_args", []),
            env_passthrough=cfg_data.get("env_passthrough", []),
            read_only=cfg_data.get("read_only", "best-effort"),
        )

        profile_path = item / "profile.md"

        mod_name = f"{package_name}.{item.name}"
        mod = importlib.import_module(mod_name)

        if not hasattr(mod, "ADAPTER"):
            raise TypeError(f"Package {mod_name} must define ADAPTER")

        adapter_cls = mod.ADAPTER
        adapter = adapter_cls()
        adapter.name = item.name
        adapter.config = config
        adapter.profile_path = profile_path

        adapters[adapter.name] = adapter

    return adapters


_REGISTRY_CACHE: dict[str, Adapter] | None = None


def registry() -> dict[str, Adapter]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        agents_dir = Path(__file__).parent
        _REGISTRY_CACHE = discover(agents_dir, "agenthub.agents")
    return _REGISTRY_CACHE


def common_stderr_text(attempt_dir: Path) -> str:
    stderr_file = attempt_dir / "stderr"
    if stderr_file.is_file():
        return stderr_file.read_text()
    return ""
