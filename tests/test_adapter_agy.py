import os
from datetime import datetime, timedelta, timezone

import pytest

from agenthub.agents import Outcome, Request
from agenthub.agents.agy import AgyAdapter
from agenthub.process import run


def test_agy_build_implement_argv(tmp_path):
    adapter = AgyAdapter()
    adapter.config = type("C", (), {"binary": "agy"})()

    brief = tmp_path / "b.md"
    brief.write_text("hello")
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    req = Request(
        role="implement",
        model="m",
        workdir=tmp_path,
        brief_path=brief,
        attempt_dir=attempt,
        git_common_dir=tmp_path,
    )

    inv = adapter.build(req)
    assert inv.argv == [
        "agy",
        "--model",
        "m",
        "--output-format",
        "json",
        "--add-dir",
        str(attempt),
        "--dangerously-skip-permissions",
        "-p",
        f"Read the file {attempt}/brief.md and carry out the task it describes. Work in the current directory.",
    ]
    assert inv.stdin_path is None
    assert (attempt / "brief.md").read_text() == "hello"


def test_agy_build_review_argv(tmp_path):
    adapter = AgyAdapter()
    adapter.config = type("C", (), {"binary": "agy"})()

    brief = tmp_path / "b.md"
    brief.write_text("hello")
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    req = Request(
        role="review",
        model="m",
        workdir=tmp_path,
        brief_path=brief,
        attempt_dir=attempt,
        git_common_dir=tmp_path,
    )

    inv = adapter.build(req)
    assert inv.argv == [
        "agy",
        "--model",
        "m",
        "--output-format",
        "json",
        "--add-dir",
        str(attempt),
        "--mode",
        "plan",
        "-p",
        f"Read the file {attempt}/brief.md and carry out the task it describes. Work in the current directory.",
    ]
    assert inv.stdin_path is None
    assert (attempt / "brief.md").read_text() == "hello"


def run_fake(env_sandbox, adapter, scenario, req):
    os.environ["FAKE_AGY_SCENARIO"] = scenario
    inv = adapter.build(req)

    ran = run(
        inv=inv, cwd=req.workdir, env=os.environ.copy(), attempt_dir=req.attempt_dir, timeout=timedelta(seconds=5)
    )
    return adapter.classify(ran.rc, req.attempt_dir)


@pytest.fixture
def req_agy(tmp_path):
    brief = tmp_path / "b.md"
    brief.write_text("hello")
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    return Request(
        role="implement",
        model="m",
        workdir=tmp_path,
        brief_path=brief,
        attempt_dir=attempt,
        git_common_dir=tmp_path,
    )


@pytest.fixture
def adapter_agy():
    a = AgyAdapter()
    a.config = type("C", (), {"binary": "agy"})()
    return a


def test_agy_classify_ok(env_sandbox, adapter_agy, req_agy):
    c = run_fake(env_sandbox, adapter_agy, "ok", req_agy)
    assert c.outcome == Outcome.OK
    assert c.reply.startswith("done: Read the file ")


def test_agy_classify_quota(env_sandbox, adapter_agy, req_agy):
    c = run_fake(env_sandbox, adapter_agy, "quota", req_agy)
    assert c.outcome == Outcome.QUOTA


def test_agy_quota_reset_time_parsed(env_sandbox, adapter_agy, req_agy):
    now = datetime.now(timezone.utc)
    c = run_fake(env_sandbox, adapter_agy, "quota-reset", req_agy)
    assert c.outcome == Outcome.QUOTA
    assert c.reset_at is not None
    diff = c.reset_at - now
    # 117h47m44s = 424064s
    assert abs(diff.total_seconds() - 424064) < 10


def test_agy_classify_auth(env_sandbox, adapter_agy, req_agy):
    c = run_fake(env_sandbox, adapter_agy, "auth", req_agy)
    assert c.outcome == Outcome.AUTH


def test_agy_classify_model_unavailable(env_sandbox, adapter_agy, req_agy):
    c = run_fake(env_sandbox, adapter_agy, "model", req_agy)
    assert c.outcome == Outcome.MODEL_UNAVAILABLE


def test_agy_classify_empty(env_sandbox, adapter_agy, req_agy):
    c = run_fake(env_sandbox, adapter_agy, "empty", req_agy)
    assert c.outcome == Outcome.EMPTY


def test_agy_classify_fail(env_sandbox, adapter_agy, req_agy):
    c = run_fake(env_sandbox, adapter_agy, "fail", req_agy)
    assert c.outcome == Outcome.FAILED


def test_agy_reply_mentioning_quota_is_ok(env_sandbox, adapter_agy, req_agy):
    c = run_fake(env_sandbox, adapter_agy, "ok-quota-text", req_agy)
    assert c.outcome == Outcome.OK
    assert c.reply == "quota exceeded is mentioned here"


def test_agy_reset_without_numbers_is_none(adapter_agy, tmp_path):
    attempt = tmp_path / "nonum"
    attempt.mkdir()
    (attempt / "stdout").write_text('{"status":"ERROR","error":"quota reached, resets in soon"}')
    (attempt / "stderr").write_text("")
    c = adapter_agy.classify(1, attempt)
    assert c.outcome == Outcome.QUOTA
    assert c.reset_at is None
