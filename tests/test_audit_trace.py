from __future__ import annotations

import pytest

from agenthub.audit.trace import (
    ENV_TRACE_ATTEMPT,
    ENV_TRACE_JOB,
    ENV_TRACE_PLAN,
    ENV_TRACE_ROLE,
    ENV_TRACE_TASK,
    TraceContext,
    trace_context,
    trace_env,
)


def test_trace_env_only_given_keys() -> None:
    assert trace_env() == {}
    assert trace_env(job="job-42") == {ENV_TRACE_JOB: "job-42"}
    assert trace_env(plan="p-1", task="t-1") == {
        ENV_TRACE_PLAN: "p-1",
        ENV_TRACE_TASK: "t-1",
    }
    all_keys = trace_env(
        plan="p-1",
        task="t-1",
        job="job-42",
        attempt=1,
        role="implement-small",
    )
    assert all_keys == {
        ENV_TRACE_PLAN: "p-1",
        ENV_TRACE_TASK: "t-1",
        ENV_TRACE_JOB: "job-42",
        ENV_TRACE_ATTEMPT: "1",
        ENV_TRACE_ROLE: "implement-small",
    }
    assert isinstance(all_keys[ENV_TRACE_ATTEMPT], str)


def test_context_without_job_is_director() -> None:
    ctx = trace_context({})
    assert isinstance(ctx, TraceContext)
    assert ctx.actor_role == "director"
    assert ctx.pool_role is None
    assert ctx.parent == {}

    ctx_with_others = trace_context(
        {
            ENV_TRACE_PLAN: "p-1",
            ENV_TRACE_TASK: "t-1",
            ENV_TRACE_ROLE: "orchestrator",
        }
    )
    assert ctx_with_others.actor_role == "director"
    assert ctx_with_others.pool_role == "orchestrator"
    assert ctx_with_others.parent == {"plan": "p-1", "task": "t-1"}
    assert "job" not in ctx_with_others.parent


def test_context_with_job_is_worker_with_parents() -> None:
    env = trace_env(
        plan="phase-g",
        task="audit-trace",
        job="job-100",
        attempt=2,
        role="implement-small",
    )
    ctx = trace_context(env)
    assert ctx.actor_role == "worker"
    assert ctx.pool_role == "implement-small"
    assert ctx.parent == {
        "plan": "phase-g",
        "task": "audit-trace",
        "job": "job-100",
        "attempt": 2,
    }

    ctx_job_only = trace_context({ENV_TRACE_JOB: "job-100"})
    assert ctx_job_only.actor_role == "worker"
    assert ctx_job_only.pool_role is None
    assert ctx_job_only.parent == {"job": "job-100"}


def test_attempt_is_int() -> None:
    ctx = trace_context({ENV_TRACE_JOB: "job-1", ENV_TRACE_ATTEMPT: "42"})
    assert ctx.parent["attempt"] == 42
    assert isinstance(ctx.parent["attempt"], int)
    assert not isinstance(ctx.parent["attempt"], bool)

    ctx_zero = trace_context({ENV_TRACE_JOB: "job-1", ENV_TRACE_ATTEMPT: "0"})
    assert ctx_zero.parent["attempt"] == 0
    assert isinstance(ctx_zero.parent["attempt"], int)


def test_non_numeric_attempt_ignored() -> None:
    for invalid in ("abc", "1.5", "None", "-"):
        ctx = trace_context({ENV_TRACE_JOB: "job-1", ENV_TRACE_ATTEMPT: invalid})
        assert "attempt" not in ctx.parent
        assert ctx.parent == {"job": "job-1"}


def test_empty_values_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    empty_env_args = trace_env(
        plan="",
        task="   ",
        job="",
        attempt="",
        role="",
    )
    assert empty_env_args == {}

    env_with_empty_strings = {
        ENV_TRACE_PLAN: "",
        ENV_TRACE_TASK: "   ",
        ENV_TRACE_JOB: "",
        ENV_TRACE_ATTEMPT: "",
        ENV_TRACE_ROLE: "   ",
    }
    ctx = trace_context(env_with_empty_strings)
    assert ctx.actor_role == "director"
    assert ctx.pool_role is None
    assert ctx.parent == {}

    # Verify default os.environ fallback when env is None
    monkeypatch.delenv(ENV_TRACE_JOB, raising=False)
    monkeypatch.delenv(ENV_TRACE_PLAN, raising=False)
    monkeypatch.delenv(ENV_TRACE_TASK, raising=False)
    monkeypatch.delenv(ENV_TRACE_ATTEMPT, raising=False)
    monkeypatch.delenv(ENV_TRACE_ROLE, raising=False)
    ctx_default = trace_context()
    assert ctx_default.actor_role == "director"
    assert ctx_default.pool_role is None
    assert ctx_default.parent == {}
