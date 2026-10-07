import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agenthub.config import ConfigError


@dataclass(frozen=True)
class Link:
    agent: str
    model: str

    def __str__(self) -> str:
        return f"{self.agent}:{self.model}"


def parse_link(text: str) -> Link:
    parts = text.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ConfigError(f"Invalid link: {text!r}")
    return Link(agent=parts[0], model=parts[1])


class Cooldowns:
    def __init__(self, path: Path):
        self._path = path

    def _read(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _with_lock(self, callback):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(self._path.name + ".lock")
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                return callback()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def until(self, link: Link, now: datetime) -> datetime | None:
        if now.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")

        data = self._read()
        link_str = str(link)
        if link_str in data:
            try:
                dt = datetime.fromisoformat(data[link_str])
                if dt > now:
                    return dt
            except ValueError:
                pass
        return None

    def set(self, link: Link, until: datetime) -> None:
        if until.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")

        def _do_set():
            data = self._read()
            data[str(link)] = until.isoformat()

            fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, prefix="cooldowns_", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, self._path)

        self._with_lock(_do_set)

    def clear(self, link: Link) -> bool:
        def _do_clear():
            data = self._read()
            link_str = str(link)
            if link_str in data:
                del data[link_str]
                fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, prefix="cooldowns_", suffix=".json")
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.replace(tmp_path, self._path)
                return True
            return False

        return self._with_lock(_do_clear)

    def all(self, now: datetime) -> dict[str, datetime]:
        if now.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")

        data = self._read()
        res = {}
        for k, v in data.items():
            try:
                dt = datetime.fromisoformat(v)
                if dt > now:
                    res[k] = dt
            except ValueError:
                pass
        return res
