"""Storage for the incident log: one JSON line per process failure in incidents.jsonl."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agenthub.audit.emit import emit
from agenthub.config import Paths
from agenthub.redact import redact

SOURCES = ("director", "techlead", "tool", "owner")
MAX_TEXT = 500
COST_KEYS = ("attempts", "rounds", "minutes")


def _incidents_file(state_dir: Path | str) -> Path:
    return Paths(config_dir=Path(state_dir), state_dir=Path(state_dir)).incidents_file


def _clean(value: str) -> str:
    return redact(value)[:MAX_TEXT]


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ts(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def add(
    state_dir: Path | str,
    *,
    kind: str,
    what: str,
    source: str = "director",
    lesson: str | None = None,
    plan: str | None = None,
    task: str | None = None,
    job: str | None = None,
    attempts: int | None = None,
    rounds: int | None = None,
    minutes: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not kind or not kind.strip():
        raise ValueError("incident kind must not be empty")
    if not what or not what.strip():
        raise ValueError("incident what must not be empty")
    if source not in SOURCES:
        raise ValueError(f"incident source must be one of {', '.join(SOURCES)}: {source!r}")

    ts = _utc_iso(datetime.now(timezone.utc) if now is None else now)
    record: dict[str, Any] = {"ts": ts, "kind": _clean(kind), "source": source}
    for key, value in (("plan", plan), ("task", task), ("job", job)):
        if value is not None:
            record[key] = _clean(value)

    given_cost = {k: v for k, v in (("attempts", attempts), ("rounds", rounds), ("minutes", minutes)) if v is not None}
    if given_cost:
        record["cost"] = {k: int(given_cost[k]) for k in COST_KEYS if k in given_cost}

    record["what"] = _clean(what)
    if lesson is not None:
        record["lesson"] = _clean(lesson)

    path = _incidents_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)

    detail = {k: v for k, v in record.items() if k not in ("what", "lesson")}
    emit(
        "incident",
        "add",
        state_dir=state_dir,
        target=record["kind"],
        quote=record["what"],
        detail=detail,
    )
    return record


def read(
    state_dir: Path | str,
    since: datetime | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    path = _incidents_file(state_dir)
    if not path.is_file():
        return []

    since_dt = None
    if since is not None:
        since_dt = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)

    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if "what" not in row and "summary" in row:
            row["what"] = row["summary"]
        if kind is not None and row.get("kind") != kind:
            continue
        if since_dt is not None:
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts < since_dt:
                continue
        rows.append(row)
    return rows
