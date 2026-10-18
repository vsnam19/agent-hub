import json
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenthub import guards, orchestrate
from agenthub.config import Pool, paths
from agenthub.jobs import Job
from agenthub.orchestrate import RunArgs, _Cancel, _finish, run_gate_job, run_job
from agenthub.pool import parse_link

BRIEF = "## Input\n\nfoo\n\n## Expected output\n\nbar\n"


def now_fn():
    return datetime.now(timezone.utc)


def test_finish_writes_atomically(env_sandbox, monkeypatch, tmp_path):
    job = Job("20261018-120000-implement-abcd", tmp_path / "job")
    job.dir.mkdir(parents=True)

    replace_calls = []
    real_replace = os.replace

    def recording_replace(src, dst):
        replace_calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)

    started = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

    def finish_now():
        return datetime(2026, 10, 18, 12, 0, 5, tzinfo=timezone.utc)

    record = {"id": job.id, "status": "running"}

    # Initial write before finish
    job.write_json("result.json", {"id": job.id, "status": "running"})
    assert len(replace_calls) == 1
    replace_calls.clear()

    res = _finish(job, record, "ok", finish_now, started)
    assert res["status"] == "ok"
    assert res["exit_code"] == 0

    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert dst == job.dir / "result.json"
    assert src.parent == job.dir
    assert src.name.startswith("result.json.tmp")
    assert src != dst
    assert job.read_json("result.json")["status"] == "ok"


@pytest.mark.parametrize("job_type", ["run_job", "run_gate_job"])
def test_cancel_installed_before_announce(env_sandbox, repo, monkeypatch, job_type):
    repo_guard = guards.inspect(repo)
    # Ensure repo config has gates for run_gate_job
    (repo / ".agent-hub").mkdir(exist_ok=True)
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["true"]\n')

    seen = {}

    def announce_hook(job_id: str) -> None:
        handler_term = signal.getsignal(signal.SIGTERM)
        handler_int = signal.getsignal(signal.SIGINT)
        seen["sigterm"] = handler_term
        seen["sigint"] = handler_int

        assert isinstance(getattr(handler_term, "__self__", None), _Cancel), (
            f"Expected SIGTERM handler to be an instance of _Cancel at announce, got {handler_term}"
        )
        assert getattr(handler_term, "__name__", None) == "_handle"

        assert isinstance(getattr(handler_int, "__self__", None), _Cancel), (
            f"Expected SIGINT handler to be an instance of _Cancel at announce, got {handler_int}"
        )
        assert getattr(handler_int, "__name__", None) == "_handle"

    orig_sigterm = signal.getsignal(signal.SIGTERM)
    orig_sigint = signal.getsignal(signal.SIGINT)

    if job_type == "run_job":
        pool = Pool(roles={}, quota_cooldown=timedelta(minutes=60), timeouts={"implement": timedelta(seconds=10)})
        args = RunArgs(
            role="implement",
            brief=BRIEF,
            workdir=repo,
            links=[parse_link("agy:m1")],
            timeout=timedelta(seconds=10),
            base=None,
        )
        res = run_job(args, paths(), pool, now_fn, on_started=announce_hook)
        assert res["status"] == "ok"
    else:
        res = run_gate_job(
            paths(),
            repo_guard,
            cli_gates=["true"],
            base="HEAD",
            since="HEAD",
            now=now_fn,
            on_started=announce_hook,
        )
        assert res["status"] == "ok"

    assert "sigterm" in seen
    assert "sigint" in seen

    # Handlers restored after completion
    assert signal.getsignal(signal.SIGTERM) == orig_sigterm
    assert signal.getsignal(signal.SIGINT) == orig_sigint


def test_cancel_installed_before_announce_module_hook(env_sandbox, repo, monkeypatch):
    """When on_started argument is omitted, orchestrate.on_started hook still sees _Cancel installed."""
    repo_guard = guards.inspect(repo)
    (repo / ".agent-hub").mkdir(exist_ok=True)
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["true"]\n')

    seen = {}

    def hook(job_id: str) -> None:
        handler = signal.getsignal(signal.SIGTERM)
        seen["sigterm"] = handler
        assert isinstance(getattr(handler, "__self__", None), _Cancel)

    monkeypatch.setattr(orchestrate, "on_started", hook)
    res = run_gate_job(
        paths(),
        repo_guard,
        cli_gates=["true"],
        base="HEAD",
        since="HEAD",
        now=now_fn,
    )
    assert res["status"] == "ok"
    assert "sigterm" in seen


@pytest.mark.parametrize("job_type", ["run_job", "run_gate_job"])
def test_cancel_during_announce_ends_in_finish(env_sandbox, repo, job_type):
    """A SIGTERM delivered during announce is caught by _Cancel and ends cleanly in _finish as cancelled."""
    repo_guard = guards.inspect(repo)
    (repo / ".agent-hub").mkdir(exist_ok=True)
    (repo / ".agent-hub" / "config.toml").write_text('gates = ["true"]\n')

    def cancel_on_started(job_id: str) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    if job_type == "run_job":
        pool = Pool(roles={}, quota_cooldown=timedelta(minutes=60), timeouts={"implement": timedelta(seconds=10)})
        args = RunArgs(
            role="implement",
            brief=BRIEF,
            workdir=repo,
            links=[parse_link("agy:m1")],
            timeout=timedelta(seconds=10),
            base=None,
        )
        res = run_job(args, paths(), pool, now_fn, on_started=cancel_on_started)
    else:
        res = run_gate_job(
            paths(),
            repo_guard,
            cli_gates=["true"],
            base="HEAD",
            since="HEAD",
            now=now_fn,
            on_started=cancel_on_started,
        )

    assert res["status"] == "cancelled"
    assert res["exit_code"] == 130
    recorded = paths().jobs_dir / res["id"] / "result.json"
    assert json.loads(recorded.read_text())["status"] == "cancelled"
