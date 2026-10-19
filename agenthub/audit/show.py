from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agenthub.audit.writer import audit_root

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_RELATIVE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
}


def _format_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_since(text: str, now: datetime | None = None) -> str:
    if not isinstance(text, str):
        raise ValueError(f"invalid since expression: {text!r}")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("empty since expression")

    m = _RELATIVE_RE.match(cleaned)
    if m:
        val = float(m.group(1))
        unit = m.group(2).lower()
        sec = val * _UNIT_SECONDS[unit]
        base_now = datetime.now(timezone.utc) if now is None else now
        if base_now.tzinfo is None:
            base_now = base_now.replace(tzinfo=timezone.utc)
        else:
            base_now = base_now.astimezone(timezone.utc)
        target_dt = base_now - timedelta(seconds=sec)
        return _format_utc_iso(target_dt)

    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as err:
        raise ValueError(f"invalid since expression: {text!r}") from err

    return _format_utc_iso(dt)


def format_line(event: Mapping[str, Any]) -> str:
    ts = str(event.get("ts", ""))
    actor_data = event.get("actor")
    if not isinstance(actor_data, Mapping):
        actor_data = {}
    agent = actor_data.get("agent")
    if agent:
        actor_str = str(agent)
    else:
        role = actor_data.get("role", "")
        host = actor_data.get("host", "")
        actor_str = f"{role}:{host}"

    kind = str(event.get("kind", ""))
    target = str(event.get("target", ""))

    outcome_data = event.get("outcome")
    if not isinstance(outcome_data, Mapping):
        outcome_data = {}
    status = str(outcome_data.get("status", ""))
    exit_code = outcome_data.get("exit_code")
    if exit_code is not None:
        outcome_str = f"{status} exit={exit_code}"
    else:
        outcome_str = status

    parts = [ts, actor_str, kind, target, outcome_str]
    quote = event.get("quote")
    if quote:
        parts.append(str(quote))
    return " ".join(parts)


def read_events(state_dir: Path | str | None = None) -> Iterator[dict[str, Any]]:
    root = audit_root(state_dir)
    if not root.is_dir():
        return

    month_dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and _MONTH_RE.match(p.name)),
        key=lambda p: p.name,
    )
    for m_dir in month_dirs:
        day_files = sorted(
            (p for p in m_dir.iterdir() if p.is_file() and _DAY_FILE_RE.match(p.name)),
            key=lambda p: p.name,
        )
        for day_file in day_files:
            try:
                with day_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if not isinstance(record, dict):
                            continue
                        yield record
            except OSError:
                continue


def select(
    events: Iterable[Mapping[str, Any]],
    plan: str | None = None,
    job: str | None = None,
    session: str | None = None,
    since: str | None = None,
    kinds: Iterable[str] | str = (),
) -> list[dict[str, Any]]:
    allowed_kinds = {kinds} if isinstance(kinds, str) else set(kinds)
    results: list[dict[str, Any]] = []

    for event in events:
        if not isinstance(event, Mapping):
            continue

        parent = event.get("parent")
        if not isinstance(parent, Mapping):
            parent = {}

        if plan is not None and parent.get("plan") != plan:
            continue

        actor = event.get("actor")
        if not isinstance(actor, Mapping):
            actor = {}

        if job is not None:
            if parent.get("job") != job and actor.get("job_id") != job:
                continue

        if session is not None:
            if actor.get("session_id") != session and parent.get("director_session") != session:
                continue

        if since is not None:
            ts = event.get("ts")
            if ts is None or str(ts) < since:
                continue

        if allowed_kinds and event.get("kind") not in allowed_kinds:
            continue

        results.append(dict(event))

    return results


def outward(
    events: Iterable[Mapping[str, Any]],
    since: str | None = None,
) -> list[dict[str, Any]]:
    return select(events, since=since, kinds=("outward",))
