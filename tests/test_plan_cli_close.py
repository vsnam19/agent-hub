import agenthub.planclose
from agenthub.cli import main
from agenthub.planclose import CloseResult
from tests.gitrepo import make_repo
from tests.test_plan_cli import make_plan_file


def test_close_prints_report_path_and_exits_zero(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    report_file = plan_file.parent / "report.md"

    def fake_close(plan, state, *, run, review, triage, ready, max_rounds=3):
        return CloseResult(ok=True, report=report_file)

    monkeypatch.setattr(agenthub.planclose, "close", fake_close)

    rc = main(["plan", "close", str(plan_file)])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == str(report_file)


def test_close_exits_one_when_not_ok(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    report_file = plan_file.parent / "report.md"

    def fake_close(plan, state, *, run, review, triage, ready, max_rounds=3):
        return CloseResult(ok=False, report=report_file)

    monkeypatch.setattr(agenthub.planclose, "close", fake_close)

    rc = main(["plan", "close", str(plan_file)])

    out = capsys.readouterr().out
    assert rc == 1
    assert out.strip() == str(report_file)


def test_close_passes_rounds(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)
    report_file = plan_file.parent / "report.md"

    recorded = {}

    def fake_close(plan, state, *, run, review, triage, ready, max_rounds=3):
        recorded["plan"] = plan
        recorded["state"] = state
        recorded["run"] = run
        recorded["review"] = review
        recorded["triage"] = triage
        recorded["ready"] = ready
        recorded["max_rounds"] = max_rounds
        return CloseResult(ok=True, report=report_file)

    monkeypatch.setattr(agenthub.planclose, "close", fake_close)

    rc = main(["plan", "close", str(plan_file), "--rounds", "5"])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == str(report_file)
    assert recorded["max_rounds"] == 5
    assert callable(recorded["run"])
    assert callable(recorded["review"])
    assert callable(recorded["triage"])
    assert callable(recorded["ready"])
    assert recorded["state"].runner is not None
    assert (plan_file.parent / "runner.pid").is_file()

    # Also verify default rounds is 3
    rc_default = main(["plan", "close", str(plan_file)])
    assert rc_default == 0
    assert recorded["max_rounds"] == 3


def test_close_rejects_zero_rounds(env_sandbox, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    make_repo(repo)
    plan_file = make_plan_file(tmp_path, repo)

    rc = main(["plan", "close", str(plan_file), "--rounds", "0"])

    err = capsys.readouterr().err
    assert rc == 2
    assert "rounds" in err
