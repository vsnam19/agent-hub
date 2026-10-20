"""Per-agent model catalog cache.

Each host CLI reports the model ids it accepts.  Those lists are kept in
models.json in the state directory for FRESH_FOR, so the pool display and the
chains can flag or skip unknown models without calling the CLIs every time.
Any doubt (no entry, stale entry, entry with an error) answers None, so callers
fail open.
"""

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

FRESH_FOR = timedelta(hours=24)


def read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def is_fresh(entry: object, now: datetime) -> bool:
    if not isinstance(entry, dict) or entry.get("error") is not None:
        return False
    try:
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
        return now - fetched_at < FRESH_FOR
    except (KeyError, TypeError, ValueError):
        return False


def model_known(cache: Mapping, agent: str, model: str, now: datetime) -> bool | None:
    if agent == "claude" and model.startswith("claude-"):
        return True
    entry = cache.get(agent)
    if not is_fresh(entry, now):
        return None
    return model in entry["models"]


def _write(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix="models_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cache, f)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def refresh(adapters: Mapping, path: Path, now: datetime, force: bool = False) -> dict:
    cache = read(path)
    changed = False
    for name, adapter in adapters.items():
        if not adapter.installed():
            continue
        if not force and is_fresh(cache.get(name), now):
            continue
        models = adapter.list_models()
        if models is not None:
            cache[name] = {"fetched_at": now.isoformat(), "models": list(models), "error": None}
            changed = True
        elif adapter.models_error:
            cache[name] = {"fetched_at": now.isoformat(), "models": [], "error": adapter.models_error}
            changed = True
    if changed:
        _write(path, cache)
    return cache
