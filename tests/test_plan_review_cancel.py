import os
import signal

import agenthub.planrun
from agenthub.cli import main
from tests.test_plan_cli import make_gated_plan_run


def test_plan_review_stops_on_cancel(env_sandbox, tmp_path, monkeypatch, capsys):
    plan_file, _state = make_gated_plan_run(env_sandbox, tmp_path)

    calls = []

    def fake_review_task(plan, task, tstate, now):
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

    def fake_review_task(plan, task, tstate, now):
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

    def fake_review_task(plan, task, tstate, now):
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
