import importlib.resources
import os
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    state_dir: Path

    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"

    @property
    def cooldowns_file(self) -> Path:
        return self.state_dir / "cooldowns.json"


def paths() -> Paths:
    home = Path(os.environ.get("HOME", "~")).expanduser()

    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        config_dir = Path(config_home) / "agent-hub"
    else:
        config_dir = home / ".config" / "agent-hub"

    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        state_dir = Path(state_home) / "agent-hub"
    else:
        state_dir = home / ".local" / "state" / "agent-hub"

    agent_hub_config = os.environ.get("AGENT_HUB_CONFIG")
    if agent_hub_config:
        config_dir = Path(agent_hub_config)

    agent_hub_state = os.environ.get("AGENT_HUB_STATE")
    if agent_hub_state:
        state_dir = Path(agent_hub_state)

    return Paths(config_dir=config_dir, state_dir=state_dir)


def parse_duration(text: str) -> timedelta:
    if not text:
        raise ConfigError(f"invalid duration: {text!r}")

    total_seconds = 0
    num_str = ""
    for char in text:
        if char.isdigit():
            num_str += char
        elif char in "smh":
            if not num_str:
                raise ConfigError(f"invalid duration: {text!r}")
            val = int(num_str)
            if char == "s":
                total_seconds += val
            elif char == "m":
                total_seconds += val * 60
            elif char == "h":
                total_seconds += val * 3600
            num_str = ""
        else:
            raise ConfigError(f"invalid duration: {text!r}")

    if num_str or total_seconds == 0:
        raise ConfigError(f"invalid duration: {text!r}")

    return timedelta(seconds=total_seconds)


@dataclass(frozen=True)
class Role:
    name: str
    kind: str
    chain: list[str]
    escalate: str | None = None


@dataclass(frozen=True)
class Pool:
    roles: dict[str, Role]
    quota_cooldown: timedelta
    timeouts: dict[str, timedelta]


def role_kind(pool: Pool, role_name: str) -> str:
    try:
        return pool.roles[role_name].kind
    except KeyError:
        available = ", ".join(pool.roles)
        raise ConfigError(f"unknown role {role_name!r}; available roles: {available}") from None


def load_pool(p: Paths | None = None) -> Pool:
    if p is None:
        p = paths()

    try:
        default_pool_text = importlib.resources.files("agenthub").joinpath("default_pool.toml").read_text()
        default_data = tomllib.loads(default_pool_text)
    except Exception as e:
        raise ConfigError(f"Failed to load default_pool.toml: {e}") from e

    user_data = {}
    user_file = p.config_dir / "pool.toml"
    if user_file.is_file():
        try:
            user_data = tomllib.loads(user_file.read_text())
        except Exception as e:
            raise ConfigError(f"{user_file}: {e}") from e

    # A user role replaces the corresponding built-in role.  Legacy roles infer
    # their kind from their well-known name so pre-kind pool.toml files work.
    role_data_by_name = {}
    for role, role_data in default_data.get("roles", {}).items():
        role_data_by_name[role] = role_data

    for role, role_data in user_data.get("roles", {}).items():
        role_data_by_name[role] = role_data

    roles: dict[str, Role] = {}
    for role_name, role_data in role_data_by_name.items():
        if not isinstance(role_data, dict):
            raise ConfigError(f"role {role_name!r} must be a table")
        kind = role_data.get("kind", role_name if role_name in ("implement", "review") else None)
        if kind not in ("implement", "review"):
            raise ConfigError(f'role {role_name!r} must declare kind = "implement" or "review"')
        chain = role_data.get("chain", [])
        if not isinstance(chain, list) or not all(isinstance(link, str) for link in chain):
            raise ConfigError(f"role {role_name!r} chain must be a list of agent:model strings")
        escalate = role_data.get("escalate")
        if escalate is not None and not isinstance(escalate, str):
            raise ConfigError(f"role {role_name!r} escalate must name a role")
        roles[role_name] = Role(name=role_name, kind=kind, chain=chain, escalate=escalate)

    for role in roles.values():
        if role.escalate is None:
            continue
        if role.kind != "implement":
            raise ConfigError(f"review role {role.name!r} cannot escalate")
        target = roles.get(role.escalate)
        if target is None:
            raise ConfigError(f"role {role.name!r} escalates to unknown role {role.escalate!r}")
        if target.kind != "implement":
            raise ConfigError(f"role {role.name!r} can only escalate to an implement role")

    defaults = default_data.get("defaults", {})
    user_defaults = user_data.get("defaults", {})

    quota_cooldown_str = user_defaults.get("quota_cooldown", defaults.get("quota_cooldown", "60m"))
    try:
        quota_cooldown = parse_duration(quota_cooldown_str)
    except ConfigError as e:
        raise ConfigError(f"{user_file}: {e}") from e

    timeouts_data = defaults.get("timeout", {}).copy()
    user_timeouts = user_defaults.get("timeout", {})
    timeouts_data.update(user_timeouts)

    timeouts = {}
    for role, to_str in timeouts_data.items():
        try:
            timeouts[role] = parse_duration(to_str)
        except ConfigError as e:
            raise ConfigError(f"{user_file}: {e}") from e

    # A role-specific timeout wins; otherwise its kind's timeout applies.
    for role_name, role in roles.items():
        if role_name not in timeouts:
            timeouts[role_name] = timeouts.get(role.kind, timedelta(minutes=60))

    return Pool(roles=roles, quota_cooldown=quota_cooldown, timeouts=timeouts)
