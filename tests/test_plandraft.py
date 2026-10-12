import json

import pytest

from agenthub.plandraft import DraftError, parse


def _reply(data: object, prefix: str = "Here is the plan.\n") -> str:
    return f"{prefix}\n```json\n{json.dumps(data)}\n```\n"


def _task(name: str, role: str = "implement", depends_on: list[str] | None = None) -> dict:
    return {
        "name": name,
        "role": role,
        "depends_on": depends_on or [],
        "reason": f"{name} reason",
        "brief": f"{name} brief",
    }


def test_valid_reply():
    reply = _reply(
        {
            "goal": "ship it",
            "tasks": [
                _task("core", "implement-small"),
                _task("api-2", "implement-hard", ["core"]),
            ],
        }
    )

    draft = parse(reply)

    assert draft.goal == "ship it"
    assert [t.name for t in draft.tasks] == ["core", "api-2"]
    core, api = draft.tasks
    assert core.role == "implement-small"
    assert core.depends_on == []
    assert core.reason == "core reason"
    assert core.brief == "core brief"
    assert api.role == "implement-hard"
    assert api.depends_on == ["core"]


def test_no_block():
    with pytest.raises(DraftError, match="no ```json block"):
        parse("I could not split this into tasks.")


def test_two_blocks():
    block = json.dumps({"goal": "g", "tasks": [_task("a")]})
    reply = f"```json\n{block}\n```\n\nand again\n\n```json\n{block}\n```\n"

    with pytest.raises(DraftError, match="2 ```json blocks"):
        parse(reply)


def test_invalid_json():
    with pytest.raises(DraftError, match="not valid JSON"):
        parse("```json\n{not json}\n```")


def test_missing_field():
    with pytest.raises(DraftError, match="missing field: reason"):
        parse(_reply({"goal": "g", "tasks": [{"name": "a", "role": "implement", "depends_on": [], "brief": "b"}]}))


def test_bad_role():
    with pytest.raises(DraftError, match="bad role 'implement-huge'"):
        parse(_reply({"goal": "g", "tasks": [_task("a", "implement-huge")]}))


def test_bad_name():
    with pytest.raises(DraftError, match="must match"):
        parse(_reply({"goal": "g", "tasks": [_task("Bad_Name")]}))


def test_duplicate_name():
    with pytest.raises(DraftError, match="duplicate task name: a"):
        parse(_reply({"goal": "g", "tasks": [_task("a"), _task("a")]}))


def test_unknown_dependency():
    with pytest.raises(DraftError, match="depends on unknown task: ghost"):
        parse(_reply({"goal": "g", "tasks": [_task("a", depends_on=["ghost"])]}))


def test_cycle():
    tasks = [_task("a", depends_on=["b"]), _task("b", depends_on=["c"]), _task("c", depends_on=["a"])]

    with pytest.raises(DraftError, match="dependency cycle: a -> b -> c -> a"):
        parse(_reply({"goal": "g", "tasks": tasks}))
