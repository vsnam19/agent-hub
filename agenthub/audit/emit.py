from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agenthub.audit.trace import trace_context
from agenthub.audit.writer import write


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

        detail_dict: dict[str, Any] = dict(detail) if detail is not None else {}
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
            record["evidence"] = evidence

        write(record, state_dir=state_dir)
    except Exception:
        pass
