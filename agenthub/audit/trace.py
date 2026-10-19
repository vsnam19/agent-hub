from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

ENV_TRACE_PLAN = "AGENT_HUB_TRACE_PLAN"
ENV_TRACE_TASK = "AGENT_HUB_TRACE_TASK"
ENV_TRACE_JOB = "AGENT_HUB_TRACE_JOB"
ENV_TRACE_ATTEMPT = "AGENT_HUB_TRACE_ATTEMPT"
ENV_TRACE_ROLE = "AGENT_HUB_TRACE_ROLE"


@dataclass
class TraceContext:
    actor_role: str
    pool_role: str | None = None
    parent: dict[str, Any] = field(default_factory=dict)


def _is_non_empty(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    return True


def trace_env(
    plan: str | None = None,
    task: str | None = None,
    job: str | None = None,
    attempt: int | str | None = None,
    role: str | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {}
    if _is_non_empty(plan):
        env[ENV_TRACE_PLAN] = str(plan).strip()
    if _is_non_empty(task):
        env[ENV_TRACE_TASK] = str(task).strip()
    if _is_non_empty(job):
        env[ENV_TRACE_JOB] = str(job).strip()
    if _is_non_empty(attempt):
        env[ENV_TRACE_ATTEMPT] = str(attempt).strip()
    if _is_non_empty(role):
        env[ENV_TRACE_ROLE] = str(role).strip()
    return env


def trace_context(env: Mapping[str, Any] | None = None) -> TraceContext:
    if env is None:
        env = os.environ

    parent: dict[str, Any] = {}

    plan = env.get(ENV_TRACE_PLAN)
    if _is_non_empty(plan):
        parent["plan"] = str(plan).strip()

    task = env.get(ENV_TRACE_TASK)
    if _is_non_empty(task):
        parent["task"] = str(task).strip()

    job = env.get(ENV_TRACE_JOB)
    if _is_non_empty(job):
        parent["job"] = str(job).strip()

    raw_attempt = env.get(ENV_TRACE_ATTEMPT)
    if _is_non_empty(raw_attempt) and not isinstance(raw_attempt, bool):
        try:
            parent["attempt"] = int(str(raw_attempt).strip())
        except (ValueError, TypeError):
            pass

    role = env.get(ENV_TRACE_ROLE)
    pool_role = str(role).strip() if _is_non_empty(role) else None

    actor_role = "worker" if "job" in parent else "director"

    return TraceContext(actor_role=actor_role, pool_role=pool_role, parent=parent)
