import json
import re

import pytest

from agenthub import tasksize
from agenthub.plandraft import Draft, DraftError, DraftTask, ordered, parse, techlead_brief, write


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
    with pytest.raises(DraftError, match="missing field: brief"):
        parse(_reply({"goal": "g", "tasks": [{"name": "a", "role": "implement", "depends_on": [], "reason": "r"}]}))


@pytest.mark.parametrize(
    "fence",
    [
        "```json",
        "```JSON",
        "```Json",
        "  ```json",
        "\t```json",
    ],
)
def test_fence_variants(fence):
    body = json.dumps({"goal": "g", "tasks": [_task("a")]})
    for reply in (
        f"{fence}\n{body}\n```\n",
        f"{fence}\r\n{body}\r\n```\r\n",
        f"{fence}\n{body}```\n",
        f"{fence}\n{body}\n  ```  \n",
    ):
        draft = parse(reply)

        assert draft.goal == "g"
        assert [t.name for t in draft.tasks] == ["a"]


def test_role_defaults_and_reason_rule():
    reply = _reply({"goal": "g", "tasks": [{"name": "a", "depends_on": [], "brief": "b"}]})

    draft = parse(reply)

    assert draft.tasks[0].role == "implement"
    assert draft.tasks[0].reason == ""

    for role in ("implement-small", "implement-hard"):
        with pytest.raises(DraftError, match="needs a non-empty reason"):
            parse(_reply({"goal": "g", "tasks": [{"name": "a", "role": role, "depends_on": [], "brief": "b"}]}))
        with pytest.raises(DraftError, match="needs a non-empty reason"):
            parse(
                _reply(
                    {
                        "goal": "g",
                        "tasks": [{"name": "a", "role": role, "depends_on": [], "reason": "   ", "brief": "b"}],
                    }
                )
            )

    kept = parse(_reply({"goal": "g", "tasks": [_task("a", "implement-small")]}))
    assert kept.tasks[0].reason == "a reason"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"tasks": [_task("a")]}, "missing field: goal"),
        ({"goal": "g", "tasks": []}, "tasks must be a non-empty list"),
        ({"goal": "g", "tasks": "a"}, "tasks must be a non-empty list"),
        ([{"goal": "g", "tasks": [_task("a")]}], "techlead json must be an object"),
        ({"goal": "g", "tasks": [{**_task("a"), "depends_on": "core"}]}, "depends_on must be a list of strings"),
        ({"goal": "g", "tasks": [_task("a", depends_on=["a"])]}, "dependency cycle: a -> a"),
    ],
)
def test_shape_errors(data, message):
    with pytest.raises(DraftError, match=message):
        parse(_reply(data))


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


def test_long_chain_no_recursion_error():
    count = 5000
    tasks = [_task(f"t{i}", depends_on=[f"t{i - 1}"] if i else []) for i in range(count)]

    draft = parse(_reply({"goal": "g", "tasks": tasks}))

    assert len(draft.tasks) == count
    assert draft.tasks[-1].name == f"t{count - 1}"
    assert draft.tasks[-1].depends_on == [f"t{count - 2}"]

    tasks[0]["depends_on"] = [f"t{count - 1}"]
    with pytest.raises(DraftError, match="dependency cycle: t0 -> "):
        parse(_reply({"goal": "g", "tasks": tasks}))


@pytest.mark.parametrize("name", ["a", "0", "a-b", "a1-2", "9-"])
def test_name_and_goal_rules(name):
    draft = parse(_reply({"goal": "g", "tasks": [_task(name)]}))

    assert draft.tasks[0].name == name

    for bad in ["-a", "--", "-", "A", "a_b", "a b", "a.b", ""]:
        with pytest.raises(DraftError, match="must match"):
            parse(_reply({"goal": "g", "tasks": [_task(bad)]}))

    for goal in ["", "   ", 5, None]:
        with pytest.raises(DraftError, match="goal must be a non-empty string"):
            parse(_reply({"goal": goal, "tasks": [_task("a")]}))


def test_unclosed_fence():
    body = json.dumps({"goal": "g", "tasks": [_task("a")]})

    for reply in (f"Plan:\n```json\n{body}\n", f"```JSON\n{body}"):
        with pytest.raises(DraftError, match="the ```json block is not closed"):
            parse(reply)


def test_techlead_brief_has_limits_and_spec():
    text = techlead_brief("docs/spec.md", ["just lint", "uv run pytest -q"])

    assert "`docs/spec.md`" in text
    assert "`just lint` and `uv run pytest -q`" in text
    assert f"at most {tasksize.MAX_FILES} files" in text
    assert f"at most {tasksize.MAX_DELIVERABLES} numbered deliverables" in text
    assert f"at most {tasksize.MAX_TESTS} named tests" in text
    assert "$" not in text
    assert "```json" in text


def test_techlead_brief_example_passes_tasksize():
    text = techlead_brief("docs/spec.md", ["just test"])
    example = re.search(r"````markdown\n(.*?)\n````", text, re.DOTALL)

    assert example is not None
    assert "## Gate" in example.group(1)
    assert "~/" not in example.group(1)
    assert "/home/" not in example.group(1)
    assert tasksize.check(example.group(1)) == []


def test_techlead_brief_states_file_counting_and_repo_paths():
    text = techlead_brief("docs/spec.md", ["just test"])

    assert "counts toward the" in text
    assert "`## Expected output`" in text
    assert "relative to the repository root" in text
    assert "home-directory path" in text


def test_techlead_brief_asks_for_tests_of_old_behaviour():
    text = techlead_brief("docs/spec.md", ["just test"])

    assert "grep `tests/` for assertions of the old behaviour" in text
    assert "list every such test file in the task's changed files" in text
    assert "$" not in text


def test_null_reason_is_empty():
    draft = parse(_reply({"goal": "g", "tasks": [{**_task("a"), "reason": None}]}))

    assert draft.tasks[0].reason == ""

    with pytest.raises(DraftError, match="needs a non-empty reason"):
        parse(_reply({"goal": "g", "tasks": [{**_task("a", "implement-small"), "reason": None}]}))


def test_ordered_puts_dependencies_first():
    tasks = [
        DraftTask(name="c", role="implement", depends_on=["a"]),
        DraftTask(name="a", role="implement"),
        DraftTask(name="b", role="implement", depends_on=["c"]),
    ]

    assert [t.name for t in ordered(tasks)] == ["a", "c", "b"]


def test_write_creates_plan_and_briefs(tmp_path):
    draft = Draft(
        goal="ship it",
        tasks=[
            DraftTask(name="b", role="implement-hard", depends_on=["a"], reason="hard part", brief="B brief\n\n"),
            DraftTask(name="a", role="implement", brief="A brief"),
        ],
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_dir = tmp_path / "plan"

    plan_path = write(draft, plan_dir, repo=repo, slug="s", base="master", gates=["just lint", "uv run pytest -q"])

    assert plan_path == plan_dir / "plan.json"
    assert (plan_dir / "briefs" / "a.md").read_text(encoding="utf-8") == "A brief\n"
    assert (plan_dir / "briefs" / "b.md").read_text(encoding="utf-8") == "B brief\n"
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    assert data == {
        "goal": "ship it",
        "repo": str(repo.resolve()),
        "base": "master",
        "slug": "s",
        "max_parallel": 3,
        "tasks": [
            {
                "name": "a",
                "role": "implement",
                "depends_on": [],
                "gate": ["just lint", "uv run pytest -q"],
                "brief": "briefs/a.md",
            },
            {
                "name": "b",
                "role": "implement-hard",
                "depends_on": ["a"],
                "gate": ["just lint", "uv run pytest -q"],
                "brief": "briefs/b.md",
                "reason": "hard part",
            },
        ],
    }


def test_write_refuses_existing_plan(tmp_path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "plan.json").write_text("{}", encoding="utf-8")
    draft = Draft(goal="g", tasks=[DraftTask(name="a", role="implement", brief="b")])

    with pytest.raises(DraftError, match="plan already exists"):
        write(draft, plan_dir, repo=tmp_path, slug="s", base="master", gates=["just lint"])

    assert (plan_dir / "plan.json").read_text(encoding="utf-8") == "{}"
    assert not (plan_dir / "briefs").exists()
