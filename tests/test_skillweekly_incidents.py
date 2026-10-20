import json

from agenthub import skillweekly
from agenthub.skillweekly import Proc
from tests.test_skillweekly import NOW, Env


def seed_incident(env: Env) -> None:
    """Write one recent incident row into the state dir's incidents.jsonl."""
    row = {
        "ts": "2026-10-10T10:00:00Z",
        "kind": "gate-failure",
        "what": "gate failed on a draft branch",
        "source": "director",
    }
    (env.state / "incidents.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_only_incidents_does_not_stop_as_nothing_to_learn(tmp_path):
    env = Env(tmp_path, findings=0)
    seed_incident(env)
    report = env.run(dry_run=True)
    assert report.exit_code == 0
    assert not any(step.name == "stop" for step in report.steps)
    assert any("draft" in step.name for step in report.steps)


def test_dry_run_continues_when_only_incidents_exist(tmp_path):
    env = Env(tmp_path, findings=0)
    seed_incident(env)
    report = env.run(dry_run=True)
    names = [step.name for step in report.steps]
    assert "stop" not in names
    assert "draft" in names
    assert "report" in names
    assert env.fake.calls == []


def test_real_run_with_only_incidents_does_not_stop_as_nothing_to_learn(tmp_path):
    env = Env(tmp_path, findings=0)
    seed_incident(env)
    calls: list[list[str]] = []

    def failing_first(argv, cwd, timeout) -> Proc:
        calls.append(list(argv))
        if len(calls) == 1:
            return Proc(1, "", "boom\n")
        return env.fake(argv, cwd, timeout)

    report = skillweekly.weekly(env.cfg, NOW, failing_first, False)
    assert calls
    assert report.outcome != "nothing to learn"


def test_no_findings_corrections_or_incidents_still_stops(tmp_path):
    env = Env(tmp_path, findings=0)
    report = env.run()
    assert report.exit_code == 0
    assert "nothing to learn" in (env.out / "report.md").read_text()
    assert env.last_run.read_text().strip() == NOW.isoformat()
    assert env.fake.calls == []
