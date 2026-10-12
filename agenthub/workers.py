import tomllib
from pathlib import Path

from agenthub.config import ConfigError

USER_FILE = "workers.toml"
REPO_FILE = Path(".agent-hub") / "config.toml"


def worker_plugins(config_dir: Path, repo_root: Path) -> tuple[Path, ...]:
    """Plugin directories that per-run worker hosts load for a run in `repo_root`, in list order.

    `<config_dir>/workers.toml` names the plugins (`[plugins]`: name -> plugin directory) and the `default` list.
    `<repo_root>/.agent-hub/config.toml` may set `worker_plugins`, which replaces the default and can only name
    plugins from the user's table. Without a user file there are no worker plugins.
    """
    user_file = config_dir / USER_FILE
    if not user_file.is_file():
        return ()

    user = _load(user_file)
    plugins = _plugins(user, user_file)
    names = _names(user.get("default", []), "default", user_file)

    repo_file = repo_root / REPO_FILE
    if repo_file.is_file():
        repo = _load(repo_file)
        if "worker_plugins" in repo:
            names = _names(repo["worker_plugins"], "worker_plugins", repo_file)

    return tuple(_plugin_dir(name, plugins, user_file) for name in dict.fromkeys(names))


def _load(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path}: {e}") from e


def _plugins(user: dict, path: Path) -> dict[str, str]:
    plugins = user.get("plugins", {})
    if not isinstance(plugins, dict) or not all(isinstance(d, str) for d in plugins.values()):
        raise ConfigError(f"{path}: plugins must be a table of name = directory strings")
    return plugins


def _names(value: object, key: str, path: Path) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ConfigError(f"{path}: {key} must be a list of plugin names")
    return value


def _plugin_dir(name: str, plugins: dict[str, str], user_file: Path) -> Path:
    if name not in plugins:
        raise ConfigError(f"{user_file}: worker plugin {name!r} is not in [plugins]")
    directory = Path(plugins[name]).expanduser().resolve()
    if not (directory / "skills").is_dir():
        raise ConfigError(f"{user_file}: worker plugin {name!r} has no skills/ directory at {directory}")
    return directory
