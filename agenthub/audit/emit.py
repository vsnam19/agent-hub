from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agenthub.audit.trace import trace_context
from agenthub.audit.writer import write


def _to_json_safe(val: Any, seen: set[int] | None = None) -> Any:
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (Path, os.PathLike)):
        return str(val)

    if seen is None:
        seen = set()
    val_id = id(val)
    if val_id in seen:
        return str(val)

    if isinstance(val, Mapping):
        seen.add(val_id)
        try:
            return {str(k): _to_json_safe(v, seen) for k, v in val.items()}
        finally:
            seen.remove(val_id)

    if isinstance(val, (list, tuple)):
        seen.add(val_id)
        try:
            return [_to_json_safe(item, seen) for item in val]
        finally:
            seen.remove(val_id)

    if isinstance(val, (set, frozenset)):
        seen.add(val_id)
        try:
            converted = [_to_json_safe(item, seen) for item in val]
            try:
                return sorted(converted)
            except TypeError:
                return sorted(converted, key=str)
        finally:
            seen.remove(val_id)

    try:
        return str(val)
    except Exception:
        return repr(val)


def emit(
    kind: str,
    event: str,
    *,
    state_dir: Path | str,
    parent: Mapping[str, Any] | None = None,
    target: str = "",
    status: str = "ok",
    exit_code: int | None = None,
    duration_ms: int | float | None = None,
    quote: str | None = None,
    evidence: Any = None,
    detail: Mapping[str, Any] | None = None,
    actor_role: str = "agent-hub",
    job_id: str | None = None,
) -> None:
    try:
        if parent is None:
            resolved_parent = dict(trace_context(os.environ).parent)
        else:
            resolved_parent = dict(parent)

        actor: dict[str, Any] = {
            "role": actor_role,
            "host": "agent-hub",
        }
        if job_id is not None:
            actor["job_id"] = job_id

        outcome: dict[str, Any] = {"status": status}
        if exit_code is not None:
            outcome["exit_code"] = exit_code
        if duration_ms is not None:
            outcome["duration_ms"] = duration_ms

        detail_dict: dict[str, Any] = {}
        if detail is not None:
            if isinstance(detail, Mapping):
                raw_detail = dict(detail)
            else:
                try:
                    raw_detail = dict(detail)
                except Exception:
                    raw_detail = {"value": detail}
            if "event" in raw_detail:
                caller_ev = raw_detail.pop("event")
                if caller_ev != event:
                    raw_detail["caller_event"] = caller_ev
            safe_detail = _to_json_safe(raw_detail)
            if isinstance(safe_detail, dict):
                detail_dict = safe_detail
            else:
                detail_dict = {"value": safe_detail}
        detail_dict["event"] = event

        record: dict[str, Any] = {
            "kind": kind,
            "actor": actor,
            "parent": resolved_parent,
            "target": target,
            "outcome": outcome,
            "detail": detail_dict,
        }
        if quote is not None:
            record["quote"] = quote
        if evidence is not None:
            try:
                record["evidence"] = str(evidence)
            except Exception:
                record["evidence"] = repr(evidence)

        write(record, state_dir=state_dir)
    except Exception:
        pass
