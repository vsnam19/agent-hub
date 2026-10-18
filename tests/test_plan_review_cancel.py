import os
import signal

import agenthub.planrun
from agenthub.cli import main
from tests.test_plan_cli import make_gated_plan_run


def test_plan_review_stops_on_cancel(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    calls = []

    def fake_review_task(plan, task, tstate, now, *args, **kwargs):
        calls.append(task.name)
        return {
            "gate": "ok",
            "review": "failed",
            "job": "j1",
            "reason": "",
            "cancelled": True,
        }

    monkeypatch.setattr(agenthub.planrun, "review_task", fake_review_task)

    rc = main(["plan", "review", str(plan_file)])
    assert rc == 130
    assert calls == ["t1"]
    assert "cancelled" in capsys.readouterr().err


def test_plan_review_stops_on_sigterm(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    calls = []

    def fake_review_task(plan, task, tstate, now, *args, **kwargs):
        calls.append(task.name)
        os.kill(os.getpid(), signal.SIGTERM)
        return {
            "gate": "ok",
            "review": "failed",
            "job": "j1",
            "reason": "",
        }

    monkeypatch.setattr(agenthub.planrun, "review_task", fake_review_task)

    rc = main(["plan", "review", str(plan_file)])
    assert rc == 130
    assert calls == ["t1"]
    assert "cancelled" in capsys.readouterr().err


def test_plan_review_stops_on_sigint(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    calls = []

    def fake_review_task(plan, task, tstate, now, *args, **kwargs):
        calls.append(task.name)
        os.kill(os.getpid(), signal.SIGINT)
        return {
            "gate": "ok",
            "review": "failed",
            "job": "j1",
            "reason": "",
        }

    monkeypatch.setattr(agenthub.planrun, "review_task", fake_review_task)

    rc = main(["plan", "review", str(plan_file)])
    assert rc == 130
    assert calls == ["t1"]
    assert "cancelled" in capsys.readouterr().err


def test_plan_review_signal_before_gate_starts_no_job(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    gate_called = False

    def fail_gate(*args, **kwargs):
        nonlocal gate_called
        gate_called = True
        raise AssertionError("run_gate_job must not be called when cancelled before gate starts")

    monkeypatch.setattr(agenthub.planrun, "run_gate_job", fail_gate)

    real_list_jobs = agenthub.planrun.list_jobs
    signal_sent = False

    def fake_list_jobs(*args, **kwargs):
        nonlocal signal_sent
        if not signal_sent:
            signal_sent = True
            os.kill(os.getpid(), signal.SIGTERM)
        return real_list_jobs(*args, **kwargs)

    monkeypatch.setattr(agenthub.planrun, "list_jobs", fake_list_jobs)

    rc = main(["plan", "review", str(plan_file)])
    assert rc == 130
    assert signal_sent is True
    assert not gate_called
    captured = capsys.readouterr()
    assert "t1 gate=failed review=failed job=-" in captured.out
    assert "cancelled" in captured.err


def test_plan_review_cancel_prints_task_line(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    def fake_review_task(plan, task, tstate, now, *args, **kwargs):
        return {
            "gate": "failed",
            "review": "failed",
            "job": "j-cancelled-1",
            "reason": "gate cancelled",
            "cancelled": True,
        }

    monkeypatch.setattr(agenthub.planrun, "review_task", fake_review_task)

    rc = main(["plan", "review", str(plan_file)])
    assert rc == 130
    captured = capsys.readouterr()
    assert "t1 gate=failed review=failed job=j-cancelled-1" in captured.out
    assert "gate cancelled" in captured.err
    assert "cancelled" in captured.err


def test_plan_review_signal_before_review_starts_no_job(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    review_called = False

    def fake_run_gate_job(*args, **kwargs):
        return {"status": "ok", "id": "gate-job-ok"}

    def fail_run_job(*args, **kwargs):
        nonlocal review_called
        review_called = True
        raise AssertionError("run_job must not be called when cancelled before review starts")

    monkeypatch.setattr(agenthub.planrun, "run_gate_job", fake_run_gate_job)
    monkeypatch.setattr(agenthub.planrun, "run_job", fail_run_job)

    real_implementer = agenthub.planrun._implementer
    signal_sent = False

    def fake_implementer(*args, **kwargs):
        nonlocal signal_sent
        if not signal_sent:
            signal_sent = True
            os.kill(os.getpid(), signal.SIGTERM)
        return real_implementer(*args, **kwargs)

    monkeypatch.setattr(agenthub.planrun, "_implementer", fake_implementer)

    rc = main(["plan", "review", str(plan_file)])
    assert rc == 130
    assert signal_sent is True
    assert not review_called
    captured = capsys.readouterr()
    assert "t1 gate=ok review=failed job=gate-job-ok" in captured.out
    assert "cancelled" in captured.err


def test_review_task_stop_before_gate(env_sandbox, tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from agenthub.plan import load
    from agenthub.planstate import load_or_init

    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)
    plan = load(plan_file)
    pstate = load_or_init(plan)
    task = plan.tasks[0]
    tstate = pstate.task(task.name)

    def fail_gate(*args, **kwargs):
        raise AssertionError("run_gate_job must not be called")

    monkeypatch.setattr(agenthub.planrun, "run_gate_job", fail_gate)

    res = agenthub.planrun.review_task(plan, task, tstate, lambda: datetime.now(timezone.utc), stop=lambda: True)
    assert res == {
        "gate": "failed",
        "review": "failed",
        "job": "-",
        "reason": "cancelled",
        "cancelled": True,
    }


def test_review_task_stop_before_review_job(env_sandbox, tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from agenthub.plan import load
    from agenthub.planstate import load_or_init

    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)
    plan = load(plan_file)
    pstate = load_or_init(plan)
    task = plan.tasks[0]
    tstate = pstate.task(task.name)

    monkeypatch.setattr(
        agenthub.planrun,
        "run_gate_job",
        lambda *args, **kwargs: {"status": "ok", "id": "gate-123"},
    )

    def fail_run_job(*args, **kwargs):
        raise AssertionError("run_job must not be called")

    monkeypatch.setattr(agenthub.planrun, "run_job", fail_run_job)

    stopped = False

    def stop_check():
        return stopped

    real_implementer = agenthub.planrun._implementer

    def fake_implementer(*args, **kwargs):
        nonlocal stopped
        stopped = True
        return real_implementer(*args, **kwargs)

    monkeypatch.setattr(agenthub.planrun, "_implementer", fake_implementer)

    res = agenthub.planrun.review_task(plan, task, tstate, lambda: datetime.now(timezone.utc), stop=stop_check)
    assert res == {
        "gate": "ok",
        "review": "failed",
        "job": "gate-123",
        "reason": "cancelled",
        "cancelled": True,
    }
