import json

from agenthub.plan import Plan, Task
from agenthub.planstate import TaskState, load_or_init


def _plan(tmp_path):
    return Plan(
        goal="g",
        repo=tmp_path,
        base="main",
        slug="s",
        tasks=[Task(name="t", role="implement")],
        path=tmp_path / "plan.json",
    )


def test_defaults_are_zero():
    t = TaskState()

    assert t.fix_rounds == 0
    assert t.attempts_base == 0


def test_roundtrip_through_state_file(tmp_path):
    plan = _plan(tmp_path)
    state = load_or_init(plan)
    state.tasks["t"].fix_rounds = 2
    state.tasks["t"].attempts_base = 3
    state.save()

    reloaded = load_or_init(plan)

    assert reloaded.tasks["t"].fix_rounds == 2
    assert reloaded.tasks["t"].attempts_base == 3
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["tasks"]["t"]["fix_rounds"] == 2
    assert saved["tasks"]["t"]["attempts_base"] == 3


def test_old_state_file_loads_with_defaults(tmp_path):
    plan = _plan(tmp_path)
    old = {
        "slug": "s",
        "started": "2026-10-14T00:00:00+00:00",
        "updated": "2026-10-14T00:00:00+00:00",
        "runner": {},
        "tasks": {
            "t": {
                "status": "pending",
                "attempts": [],
                "worktree": "",
                "branch": "",
                "start_ref": "",
                "reason": "",
                "handoff": "",
                "role": "",
                "escalated_from": "",
                "visited_roles": [],
            }
        },
    }
    (tmp_path / "state.json").write_text(json.dumps(old))

    state = load_or_init(plan)

    assert state.tasks["t"].fix_rounds == 0
    assert state.tasks["t"].attempts_base == 0


def test_fix_start_round_trips(tmp_path):
    assert TaskState().fix_start == ""

    plan = _plan(tmp_path)
    state = load_or_init(plan)
    state.tasks["t"].fix_start = "c0ffee1234567890abcdef"
    state.save()

    reloaded = load_or_init(plan)

    assert reloaded.tasks["t"].fix_start == "c0ffee1234567890abcdef"
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["tasks"]["t"]["fix_start"] == "c0ffee1234567890abcdef"


def test_old_state_without_fix_start_loads(tmp_path):
    plan = _plan(tmp_path)
    old = {
        "slug": "s",
        "started": "2026-10-14T00:00:00+00:00",
        "updated": "2026-10-14T00:00:00+00:00",
        "runner": {},
        "tasks": {
            "t": {
                "status": "pending",
                "attempts": [],
                "worktree": "",
                "branch": "",
                "start_ref": "",
                "reason": "",
                "handoff": "",
                "role": "",
                "escalated_from": "",
                "visited_roles": [],
                "fix_rounds": 1,
                "attempts_base": 2,
            }
        },
    }
    (tmp_path / "state.json").write_text(json.dumps(old))

    state = load_or_init(plan)

    assert state.tasks["t"].fix_start == ""
