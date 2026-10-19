from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agenthub import orchestrate
from agenthub.audit.trace import (
    ENV_TRACE_ATTEMPT,
    ENV_TRACE_JOB,
    ENV_TRACE_PLAN,
    ENV_TRACE_ROLE,
    ENV_TRACE_TASK,
)
from agenthub.config import Pool, paths
from agenthub.orchestrate import RunArgs, run_job
from agenthub.pool import parse_link

BRIEF = "## Input\n\nfoo\n\n## Expected output\n\nbar\n"


def now():
    return datetime.now(timezone.utc)


def make_pool(**kw):
    return Pool(
        roles={},
        quota_cooldown=kw.get("quota_cooldown", timedelta(minutes=60)),
        timeouts={"implement": timedelta(minutes=5), "review": timedelta(minutes=5)},
    )


def args_for(
    repo,
    links,
    role="implement",
    brief=BRIEF,
    timeout=timedelta(seconds=30),
    base=None,
    gates=None,
    jev=False,
    allow=None,
    skills=None,
):
    return RunArgs(
        role=role,
        brief=brief,
        workdir=repo,
        links=[parse_link(x) if isinstance(x, str) else x for x in links],
        timeout=timeout,
        base=base,
        gates=gates or [],
        jev=jev,
        allow=allow or [],
        skills=skills or [],
    )


def go(repo, links, **kw):
    return run_job(args_for(repo, links, **kw), paths(), make_pool(), now)


def scenario(monkeypatch, name, value):
    monkeypatch.setenv(f"FAKE_{name.upper()}_SCENARIO", value)


@pytest.fixture
def capture_process_run(monkeypatch):
    captured: list[dict[str, str]] = []
    real_run = orchestrate.process.run

    def fake_run(inv, cwd, env, attempt_dir, timeout, on_start=lambda pgid: None):
        captured.append(dict(env))
        return real_run(inv, cwd, env, attempt_dir, timeout, on_start=on_start)

    monkeypatch.setattr(orchestrate.process, "run", fake_run)
    return captured


def test_attempt_env_has_job_role_and_attempt_number(env_sandbox, repo, capture_process_run):
    result = go(repo, ["agy:m1"], role="implement")
    assert result["status"] == "ok"
    assert len(capture_process_run) == 1
    attempt_env = capture_process_run[0]
    assert attempt_env[ENV_TRACE_JOB] == result["id"]
    assert attempt_env[ENV_TRACE_ROLE] == "implement"
    assert attempt_env[ENV_TRACE_ATTEMPT] == "1"


def test_second_link_gets_attempt_two(env_sandbox, repo, monkeypatch, capture_process_run):
    scenario(monkeypatch, "agy", "quota,ok")
    result = go(repo, ["agy:m1", "agy:m2"], role="implement")
    assert result["status"] == "ok"
    assert len(capture_process_run) == 2

    first_env = capture_process_run[0]
    assert first_env[ENV_TRACE_JOB] == result["id"]
    assert first_env[ENV_TRACE_ROLE] == "implement"
    assert first_env[ENV_TRACE_ATTEMPT] == "1"

    second_env = capture_process_run[1]
    assert second_env[ENV_TRACE_JOB] == result["id"]
    assert second_env[ENV_TRACE_ROLE] == "implement"
    assert second_env[ENV_TRACE_ATTEMPT] == "2"


def test_inherited_plan_and_task_pass_through(env_sandbox, repo, monkeypatch, capture_process_run):
    monkeypatch.setenv(ENV_TRACE_PLAN, "plan-123")
    monkeypatch.setenv(ENV_TRACE_TASK, "task-456")
    result = go(repo, ["agy:m1"], role="implement")
    assert result["status"] == "ok"
    assert len(capture_process_run) == 1
    attempt_env = capture_process_run[0]
    assert attempt_env[ENV_TRACE_PLAN] == "plan-123"
    assert attempt_env[ENV_TRACE_TASK] == "task-456"
    assert attempt_env[ENV_TRACE_JOB] == result["id"]
    assert attempt_env[ENV_TRACE_ROLE] == "implement"
    assert attempt_env[ENV_TRACE_ATTEMPT] == "1"


def test_stale_inherited_job_is_overridden(env_sandbox, repo, monkeypatch, capture_process_run):
    monkeypatch.setenv(ENV_TRACE_JOB, "stale-job-999")
    monkeypatch.setenv(ENV_TRACE_ROLE, "stale-role")
    monkeypatch.setenv(ENV_TRACE_ATTEMPT, "99")
    result = go(repo, ["agy:m1"], role="implement")
    assert result["status"] == "ok"
    assert len(capture_process_run) == 1
    attempt_env = capture_process_run[0]
    assert attempt_env[ENV_TRACE_JOB] == result["id"]
    assert attempt_env[ENV_TRACE_ROLE] == "implement"
    assert attempt_env[ENV_TRACE_ATTEMPT] == "1"


def test_no_plan_vars_when_not_inherited(env_sandbox, repo, monkeypatch, capture_process_run):
    monkeypatch.delenv(ENV_TRACE_PLAN, raising=False)
    monkeypatch.delenv(ENV_TRACE_TASK, raising=False)
    result = go(repo, ["agy:m1"], role="implement")
    assert result["status"] == "ok"
    assert len(capture_process_run) == 1
    attempt_env = capture_process_run[0]
    assert ENV_TRACE_PLAN not in attempt_env
    assert ENV_TRACE_TASK not in attempt_env
    assert attempt_env[ENV_TRACE_JOB] == result["id"]
    assert attempt_env[ENV_TRACE_ROLE] == "implement"
    assert attempt_env[ENV_TRACE_ATTEMPT] == "1"
