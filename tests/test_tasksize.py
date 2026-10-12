from agenthub.tasksize import MAX_DELIVERABLES, MAX_FILES, MAX_TESTS, check


def make_brief(
    deliverables: int = 2,
    files: list[str] | None = None,
    tests: list[str] | None = None,
    skip: str | None = None,
) -> str:
    files = ["agenthub/tasksize.py", "tests/test_tasksize.py"] if files is None else files
    tests = ["test_small_brief_has_no_problems"] if tests is None else tests
    sections = {
        "Goal": ["Check that a brief is small enough.", ""],
        "Input": ["- `agenthub/brief.py`", ""],
        "Expected output": [f"{number}. Deliverable {number}." for number in range(1, deliverables + 1)]
        + [f"   Covered by `{name}`." for name in tests]
        + ["", "Done means: `just lint` and `uv run pytest -q` pass.", ""],
        "Constraints": [f"- Only {', '.join(f'`{path}`' for path in files)} change. Stdlib only.", ""],
        "Gate": ["`just lint` and `uv run pytest -q`.", ""],
    }
    lines = ["# Brief: sample", ""]
    for name, body in sections.items():
        if name != skip:
            lines += [f"## {name}", "", *body]
    return "\n".join(lines)


def test_small_brief_has_no_problems():
    assert check(make_brief()) == []


def test_missing_heading():
    assert check(make_brief(skip="Gate")) == ["missing heading: ## Gate"]


def test_too_many_deliverables():
    assert check(make_brief(deliverables=MAX_DELIVERABLES + 1)) == [
        f"too many deliverables: {MAX_DELIVERABLES + 1} (max {MAX_DELIVERABLES})"
    ]


def test_too_many_files():
    files = [f"agenthub/module_{index}.py" for index in range(MAX_FILES + 1)]
    assert check(make_brief(files=files)) == [f"too many files: {MAX_FILES + 1} (max {MAX_FILES})"]


def test_too_many_tests():
    tests = [f"test_case_{index}" for index in range(MAX_TESTS + 1)]
    assert check(make_brief(tests=tests)) == [f"too many tests: {MAX_TESTS + 1} (max {MAX_TESTS})"]


def test_counts_unique_tests_only():
    tests = [f"test_case_{index}" for index in range(MAX_TESTS)]
    # Six unique names, each mentioned again: 6 + 6 = 12 mentions, but still within the limit.
    assert check(make_brief(tests=tests + tests + [f"tests/test_x.py::{name}" for name in tests])) == []
