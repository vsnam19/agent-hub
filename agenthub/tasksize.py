"""Size check for a task brief: is it small enough for one haiku run? (Phase E, D-094)"""

import re

MAX_FILES = 3
MAX_DELIVERABLES = 4
MAX_TESTS = 6

HEADINGS = ("Goal", "Input", "Expected output", "Constraints", "Gate")
DELIVERABLE = re.compile(r"^\d+\.\s+\S")
TEST_NAME = re.compile(r"`(?:[^`\s]+::)?(test_\w+)`")
BACKTICKED = re.compile(r"`([^`]+)`")


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif current is not None:
            current.append(line)
    return sections


def check(text: str) -> list[str]:
    """One problem string per violated rule; an empty list means the brief is small enough."""
    sections = _sections(text)
    problems = [f"missing heading: ## {name}" for name in HEADINGS if name not in sections]

    deliverables = sum(1 for line in sections.get("Expected output", []) if DELIVERABLE.match(line))
    if deliverables > MAX_DELIVERABLES:
        problems.append(f"too many deliverables: {deliverables} (max {MAX_DELIVERABLES})")

    files = {
        path
        for line in sections.get("Constraints", [])
        if line.startswith("- Only ") and "change" in line
        for path in BACKTICKED.findall(line)
    }
    if len(files) > MAX_FILES:
        problems.append(f"too many files: {len(files)} (max {MAX_FILES})")

    tests = {match.group(1) for match in TEST_NAME.finditer(text)}
    if len(tests) > MAX_TESTS:
        problems.append(f"too many tests: {len(tests)} (max {MAX_TESTS})")

    return problems
