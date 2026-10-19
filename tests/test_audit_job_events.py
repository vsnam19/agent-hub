from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agenthub import guards
from agenthub.config import Pool, paths
from agenthub.orchestrate import EXIT, RunArgs, _finish, run_gate_job, run_job
from agenthub.pool import parse_link

BRIEF = "## Input\n\nfoo\n\n## Expected output\n\nbar\n"


def now() -> datetime:
    return datetime.now(timezone.utc)


def make_pool(**kw: Any) -> Pool:
    return Pool(
        roles={},
        quota_cooldown=kw.get("quota_cooldown", timedelta(minutes=60)),
        timeouts={"implement": timedelta(minutes=5), "review": timedelta(minutes=5)},
    )


def args_for(
    repo: Path,
    links: list[str],
    role: str = "implement",
    brief: str = BRIEF,
    timeout: timedelta = timedelta(seconds=30),
    base: str | None = None,
    gates: list[str] | None = None,
    jev: bool = False,
    allow: list[str] | None = None,
    skills: list[str] | None = None,
) -> RunArgs:
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


def go(repo: Path, links: list[str], **kw: Any) -> dict:
    return run_job(args_for(repo, links, **kw), paths(), make_pool(), now)


def read_audit_events(state_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    root = state_dir / "audit"
    for p in sorted(root.glob("*/*.jsonl")):
        if p.name.endswith(".lock"):
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def test_start_event_has_job_id_and_role(env_sandbox, repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HUB_TRACE_PLAN", "plan-42")
    monkeypatch.setenv("AGENT_HUB_TRACE_TASK", "task-99")

    result = go(repo, ["agy:m1"], role="implement")
    events = read_audit_events(env_sandbox.state)
    start_events = [
        e
        for e in events
        if e.get("detail", {}).get("event") == "start" and e.get("actor", {}).get("job_id") == result["id"]
    ]
    assert len(start_events) == 1
    ev = start_events[0]

    assert ev["kind"] == "job"
    assert ev["actor"]["job_id"] == result["id"]
    assert ev["actor"]["role"] == "agent-hub"
    assert ev["actor"]["host"] == "agent-hub"
    assert ev["parent"]["job"] == result["id"]
    assert ev["parent"]["plan"] == "plan-42"
    assert ev["parent"]["task"] == "task-99"
    assert ev["detail"]["role"] == "implement"
    assert ev["detail"]["kind"] == "implement"
    assert ev["detail"]["workdir"] == str(repo)
    assert ev["outcome"]["status"] == "ok"

    # Also verify run_gate_job emits start event with job_id and role='gate'
    (repo / ".agent-hub").mkdir(exist_ok=True)
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["true"]\n')
    repo_guard = guards.inspect(repo)
    gate_res = run_gate_job(paths(), repo_guard, [], "master", "master", now)

    events_after = read_audit_events(env_sandbox.state)
    gate_starts = [
        e
        for e in events_after
        if e.get("detail", {}).get("event") == "start" and e.get("actor", {}).get("job_id") == gate_res["id"]
    ]
    assert len(gate_starts) == 1
    gev = gate_starts[0]
    assert gev["kind"] == "job"
    assert gev["actor"]["job_id"] == gate_res["id"]
    assert gev["parent"]["job"] == gate_res["id"]
    assert gev["detail"]["role"] == "gate"
    assert gev["detail"]["kind"] == "gate"
    assert gev["detail"]["workdir"] == str(repo)


def test_attempt_event_per_link(env_sandbox, repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "quota,ok")
    result = go(repo, ["agy:m1", "agy:m2"])
    assert result["status"] == "ok"

    events = read_audit_events(env_sandbox.state)
    attempt_events = [
        e
        for e in events
        if e.get("detail", {}).get("event") == "attempt" and e.get("actor", {}).get("job_id") == result["id"]
    ]
    assert len(attempt_events) == 2

    # Link 1: quota failure
    att1 = attempt_events[0]
    assert att1["kind"] == "job"
    assert att1["actor"]["job_id"] == result["id"]
    assert att1["parent"]["job"] == result["id"]
    assert att1["outcome"]["status"] == "error"
    assert att1["detail"]["n"] == 1
    assert att1["detail"]["agent"] == "agy"
    assert att1["detail"]["model"] == "m1"
    assert att1["detail"]["outcome"] == "quota"
    assert "rc" in att1["detail"]
    assert "duration" in att1["detail"]

    # Link 2: success
    att2 = attempt_events[1]
    assert att2["kind"] == "job"
    assert att2["actor"]["job_id"] == result["id"]
    assert att2["parent"]["job"] == result["id"]
    assert att2["outcome"]["status"] == "ok"
    assert att2["detail"]["n"] == 2
    assert att2["detail"]["agent"] == "agy"
    assert att2["detail"]["model"] == "m2"
    assert att2["detail"]["outcome"] == "ok"
    assert att2["detail"]["rc"] == 0
    assert "duration" in att2["detail"]


def test_fallback_event_when_next_link_runs(env_sandbox, repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "quota,ok")
    result = go(repo, ["agy:m1", "agy:m2"])
    assert result["status"] == "ok"

    events = read_audit_events(env_sandbox.state)
    fallback_events = [
        e
        for e in events
        if e.get("detail", {}).get("event") == "fallback" and e.get("actor", {}).get("job_id") == result["id"]
    ]
    assert len(fallback_events) == 1
    fb = fallback_events[0]
    assert fb["kind"] == "job"
    assert fb["actor"]["job_id"] == result["id"]
    assert fb["parent"]["job"] == result["id"]
    assert fb["detail"]["from"] == 1
    assert fb["detail"]["outcome"] == "quota"

    # When there is only 1 link in chain, a fallback outcome does NOT emit fallback
    monkeypatch.setenv("FAKE_AGY_SCENARIO", "quota")
    res_single = go(repo, ["agy:m1"])
    events_single = read_audit_events(env_sandbox.state)
    single_fb = [
        e
        for e in events_single
        if e.get("detail", {}).get("event") == "fallback" and e.get("actor", {}).get("job_id") == res_single["id"]
    ]
    assert len(single_fb) == 0


def test_gate_event_has_summary_quote(env_sandbox, repo) -> None:
    # 1. Successful gates in run_job
    res_ok = go(repo, ["agy:m1"], gates=["true"])
    assert res_ok["status"] == "ok"
    events_ok = read_audit_events(env_sandbox.state)
    gate_ok = [
        e
        for e in events_ok
        if e.get("detail", {}).get("event") == "gate" and e.get("actor", {}).get("job_id") == res_ok["id"]
    ]
    assert len(gate_ok) == 1
    assert gate_ok[0]["outcome"]["status"] == "ok"
    assert gate_ok[0]["quote"] == "passed (1)"

    # 2. Failing gate in run_job
    res_fail = go(repo, ["agy:m1"], gates=["echo failed; exit 2"])
    assert res_fail["status"] == "gate-failed"
    events_fail = read_audit_events(env_sandbox.state)
    gate_fail = [
        e
        for e in events_fail
        if e.get("detail", {}).get("event") == "gate" and e.get("actor", {}).get("job_id") == res_fail["id"]
    ]
    assert len(gate_fail) == 1
    assert gate_fail[0]["outcome"]["status"] == "error"
    assert gate_fail[0]["quote"] == "FAILED: echo failed; exit 2 (rc 2)"

    # 3. Gate event in run_gate_job
    (repo / ".agent-hub").mkdir(exist_ok=True)
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["true"]\n')
    repo_guard = guards.inspect(repo)
    gate_job_res = run_gate_job(paths(), repo_guard, [], "master", "master", now)
    events_gj = read_audit_events(env_sandbox.state)
    gate_job_events = [
        e
        for e in events_gj
        if e.get("detail", {}).get("event") == "gate" and e.get("actor", {}).get("job_id") == gate_job_res["id"]
    ]
    assert len(gate_job_events) == 1
    assert gate_job_events[0]["outcome"]["status"] == "ok"
    assert gate_job_events[0]["quote"] == "passed (1)"


def test_finish_event_has_status_and_exit_code(env_sandbox, repo) -> None:
    res = go(repo, ["agy:m1"])
    events = read_audit_events(env_sandbox.state)
    finish_events = [
        e
        for e in events
        if e.get("detail", {}).get("event") == "finish" and e.get("actor", {}).get("job_id") == res["id"]
    ]
    assert len(finish_events) == 1
    ev = finish_events[0]
    assert ev["kind"] == "job"
    assert ev["actor"]["job_id"] == res["id"]
    assert ev["parent"]["job"] == res["id"]
    assert ev["outcome"]["status"] == "ok"
    assert ev["outcome"]["exit_code"] == 0
    assert isinstance(ev["outcome"]["duration_ms"], int)
    assert ev["outcome"]["duration_ms"] >= 0

    # Test quote when handoff is a dict with summary str
    fake_job_id = "20261019-120000-implement-abcd"
    fake_job_dir = env_sandbox.state / "jobs" / fake_job_id
    fake_job_dir.mkdir(parents=True)
    from agenthub.jobs import Job

    job = Job(id=fake_job_id, dir=fake_job_dir)
    rec = {
        "id": fake_job_id,
        "status": "running",
        "handoff": {"summary": "Completed successfully in 1 attempt"},
    }
    fin_rec = _finish(job, rec, "ok", now, now())
    assert fin_rec["status"] == "ok"

    events_after = read_audit_events(env_sandbox.state)
    custom_finish = [
        e
        for e in events_after
        if e.get("detail", {}).get("event") == "finish" and e.get("actor", {}).get("job_id") == fake_job_id
    ]
    assert len(custom_finish) == 1
    assert custom_finish[0]["quote"] == "Completed successfully in 1 attempt"
    assert custom_finish[0]["outcome"]["status"] == "ok"
    assert custom_finish[0]["outcome"]["exit_code"] == EXIT["ok"]


def test_emit_failure_does_not_change_job_result(env_sandbox, repo, monkeypatch: pytest.MonkeyPatch) -> None:
    def _exploding_emit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom from audit emit")

    monkeypatch.setattr("agenthub.orchestrate.emit", _exploding_emit)
    result = go(repo, ["agy:m1"])
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["agent"] == "agy"
    assert result["model"] == "m1"


def test_review_event_when_verdict_present(env_sandbox, repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agenthub.handoff.parse_verdict", lambda text: "approve")
    monkeypatch.setattr("agenthub.guards.review_diff", lambda repo, base: "diff --git a/f b/f\n")
    result = go(repo, ["codex:m1"], role="review", brief="", base="master")
    assert result["status"] == "ok"
    assert result["verdict"] == "approve"

    events = read_audit_events(env_sandbox.state)
    review_events = [
        e
        for e in events
        if e.get("detail", {}).get("event") == "review" and e.get("actor", {}).get("job_id") == result["id"]
    ]
    assert len(review_events) == 1
    ev = review_events[0]
    assert ev["kind"] == "job"
    assert ev["actor"]["job_id"] == result["id"]
    assert ev["parent"]["job"] == result["id"]
    assert ev["quote"] == "approve"
    assert ev["detail"]["verdict"] == "approve"
