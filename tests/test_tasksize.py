from agenthub.tasksize import MAX_DELIVERABLES, MAX_FILES, MAX_TESTS, check


def make_brief(
    deliverables: int = 2,
    files: list[str] | None = None,
    tests: list[str] | None = None,
    skip: str | None = None,
    extra_expected: list[str] | None = None,
    only_lines: list[str] | None = None,
) -> str:
    files = ["agenthub/tasksize.py", "tests/test_tasksize.py"] if files is None else files
    tests = ["test_small_brief_has_no_problems"] if tests is None else tests
    if only_lines is None:
        only_lines = [f"- Only {', '.join(f'`{path}`' for path in files)} change. Stdlib only.", ""]
    sections = {
        "Goal": ["Check that a brief is small enough.", ""],
        "Input": ["- `agenthub/brief.py`", ""],
        "Expected output": [f"{number}. Deliverable {number}." for number in range(1, deliverables + 1)]
        + [f"   Covered by `{name}`." for name in tests]
        + (extra_expected or [])
        + ["", "Done means: `just lint` and `uv run pytest -q` pass.", ""],
        "Constraints": only_lines,
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
    # Six unique names, each mentioned three times: 6 + 6 + 6 = 18 mentions, but still within the limit.
    assert check(make_brief(tests=tests + tests + [f"tests/test_x.py::{name}" for name in tests])) == []


def test_files_under_expected_output_counted():
    # Two files in the Only line plus two more under Expected output: four in all, over the limit of three.
    extra = ["   Also edits `docs/notes.md`.", "   And `agenthub/extra.py`."]
    assert check(make_brief(extra_expected=extra)) == ["too many files: 4 (max 3)"]


def test_wrapped_only_line_counted():
    # The Only bullet wraps onto an indented continuation line that also names files and ends with "change".
    only_lines = ["- Only `agenthub/a.py`, `agenthub/b.py`,", "  `agenthub/c.py` and `agenthub/d.py` change.", ""]
    assert check(make_brief(only_lines=only_lines)) == ["too many files: 4 (max 3)"]


def test_commands_not_counted_as_files():
    # A command with '/' and spaces is not a path; the three Only files are still the whole file count.
    files = ["agenthub/a.py", "agenthub/b.py", "agenthub/c.py"]
    extra = ["   Run `grep -r foo agenthub/` first."]
    assert check(make_brief(files=files, extra_expected=extra)) == []


def test_unchanged_is_not_change():
    # "unchanged" and "changelog" are not the word "change", so their backticked paths are not Only files.
    files = ["agenthub/a.py", "agenthub/b.py", "agenthub/c.py", "agenthub/d.py"]
    unchanged = [f"- Only {', '.join(f'`{path}`' for path in files)} are unchanged.", ""]
    changelog = [f"- Only {', '.join(f'`{path}`' for path in files)} go in changelog.", ""]
    assert check(make_brief(only_lines=unchanged)) == []
    assert check(make_brief(only_lines=changelog)) == []


def test_heading_with_suffix():
    # Headings match by their leading name, case-insensitively, so suffixes such as "(E1)" or ":" are allowed.
    text = make_brief().replace("## Gate", "## Gate: final").replace("## Expected output", "## expected OUTPUT (E1)")
    assert check(text) == []


def test_non_path_backticks_ignored():
    # Commands, bare words and path::test names are not files, so the brief stays within the file limit.
    extra = [
        "   Run `uv run ruff check .` and `agent-hub run`.",
        "   Covers `agenthub/x.py::test_a` and `agenthub/y.py::test_b`.",
    ]
    assert check(make_brief(extra_expected=extra)) == []
