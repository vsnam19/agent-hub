import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from agenthub.agents import Outcome, Request
from agenthub.agents.agy import AgyAdapter, _keyring_env, _secret_service_running
from agenthub.process import run


@pytest.fixture(autouse=True)
def mock_secret_service_running(monkeypatch):
    monkeypatch.setattr("agenthub.agents.agy._secret_service_running", lambda: False)


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


def test_agy_implement_has_no_job_dir(tmp_path):
    adapter = AgyAdapter()
    adapter.config = type("C", (), {"binary": "agy"})()
    job = tmp_path / "job"
    job.mkdir()
    brief = job / "b.md"
    brief.write_text("hello")
    attempt = job / "attempt"
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
    assert str(req.brief_path.parent) not in inv.argv
    assert str(attempt) in inv.argv


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
        "--add-dir",
        str(brief.parent),
        "--mode",
        "plan",
        "-p",
        f"Read the file {attempt}/brief.md and carry out the task it describes. Work in the current directory.",
    ]
    assert inv.stdin_path is None
    assert (attempt / "brief.md").read_text() == "hello"


BLANK_SSH = {"SSH_CONNECTION": "", "SSH_CLIENT": "", "SSH_TTY": ""}
SSH_DBUS = {"SSH_CONNECTION": "10.0.0.2 22 10.0.0.1 5000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}


SSH_ONLY = {"SSH_CONNECTION": "10.0.0.2 22 10.0.0.1 5000"}


def test_keyring_env_blanks_under_ssh_with_secret_service():
    assert _keyring_env(SSH_ONLY, secret_service=lambda: True) == BLANK_SSH


def test_keyring_env_none_under_ssh_without_secret_service():
    assert _keyring_env(SSH_DBUS, secret_service=lambda: False) is None


def test_keyring_env_none_without_ssh():
    assert (
        _keyring_env({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}, secret_service=lambda: True) is None
    )
    assert _keyring_env({}, secret_service=lambda: True) is None


def test_keyring_env_forced_on():
    assert _keyring_env({"AGENTHUB_AGY_KEYRING": "1"}, secret_service=lambda: False) == BLANK_SSH


def test_keyring_env_forced_off():
    assert _keyring_env({"AGENTHUB_AGY_KEYRING": "0", **SSH_DBUS}, secret_service=lambda: True) is None


def _fake_dbus(monkeypatch, returncode, stdout):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_secret_service_running_parses_true(monkeypatch):
    _secret_service_running.cache_clear()
    calls = _fake_dbus(monkeypatch, 0, "method return time=1 sender=org.freedesktop.DBus\n   boolean true\n")
    try:
        assert _secret_service_running() is True
        assert calls and "org.freedesktop.secrets" in calls[0][-1]
    finally:
        _secret_service_running.cache_clear()


def test_secret_service_running_false(monkeypatch):
    _secret_service_running.cache_clear()
    _fake_dbus(monkeypatch, 0, "method return time=1 sender=org.freedesktop.DBus\n   boolean false\n")
    try:
        assert _secret_service_running() is False
    finally:
        _secret_service_running.cache_clear()


def test_secret_service_running_missing_binary(monkeypatch):
    _secret_service_running.cache_clear()

    def missing(argv, **kwargs):
        raise FileNotFoundError("dbus-send")

    monkeypatch.setattr(subprocess, "run", missing)
    try:
        assert _secret_service_running() is False
    finally:
        _secret_service_running.cache_clear()


def test_build_passes_keyring_env(tmp_path, monkeypatch):
    adapter = AgyAdapter()
    adapter.config = type("C", (), {"binary": "agy"})()

    monkeypatch.delenv("AGENTHUB_AGY_KEYRING", raising=False)
    monkeypatch.setattr("agenthub.agents.agy._secret_service_running", lambda: True)
    for key, value in SSH_DBUS.items():
        monkeypatch.setenv(key, value)

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
    assert inv.env == BLANK_SSH


def test_build_never_calls_dbus_send(tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 22 10.0.0.1 5000")

    orig_run = subprocess.run

    def fail_on_dbus(cmd, *args, **kwargs):
        if (isinstance(cmd, (list, tuple)) and any("dbus-send" in str(arg) for arg in cmd)) or "dbus-send" in str(cmd):
            pytest.fail("build() must not call dbus-send")
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_on_dbus)
    _secret_service_running.cache_clear()

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
    assert inv.argv[0] == "agy"
    assert (attempt / "brief.md").read_text() == "hello"
    assert inv.env is None


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


def test_classify_auth_timeout_is_auth(adapter_agy, tmp_path):
    attempt = tmp_path / "auth-timeout"
    attempt.mkdir()
    (attempt / "stdout").write_text("")
    (attempt / "stderr").write_text(
        "Waiting for authentication (timeout 60s)...\n"
        "Error: authentication timed out.\n"
        "error: authentication failed or timed out\n"
    )
    c = adapter_agy.classify(1, attempt)
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


AUTO_DENIED_STDOUT = (
    '{"conversation_id":"c1","status":"SUCCESS","response":"","duration_seconds":65.3,"num_turns":1,'
    '"denied_actions":[{"action":"command","display_name":"RunCommand"}]}'
)
AUTO_DENIED_STDERR = (
    'jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for, '
    "so it was auto-denied."
)


def test_agy_denied_permission_is_failed_with_reason(adapter_agy, tmp_path):
    attempt = tmp_path / "denied"
    attempt.mkdir()
    (attempt / "stdout").write_text(AUTO_DENIED_STDOUT)
    (attempt / "stderr").write_text(AUTO_DENIED_STDERR)
    c = adapter_agy.classify(0, attempt)
    assert c.outcome == Outcome.FAILED
    assert c.detail.startswith("permission auto-denied: RunCommand")
    assert c.reply == ""


def test_agy_denied_permission_with_reply_is_ok(adapter_agy, tmp_path):
    attempt = tmp_path / "denied-reply"
    attempt.mkdir()
    stdout = AUTO_DENIED_STDOUT.replace('"response":""', '"response":"no findings"')
    (attempt / "stdout").write_text(stdout)
    (attempt / "stderr").write_text("")
    c = adapter_agy.classify(0, attempt)
    assert c.outcome == Outcome.OK
    assert c.reply == "no findings"


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
