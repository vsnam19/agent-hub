"""Size check for a task brief: is it small enough for one haiku run? (Phase E, D-094)"""

import re

MAX_FILES = 3
MAX_DELIVERABLES = 4
MAX_TESTS = 6

HEADINGS = ("Goal", "Input", "Expected output", "Constraints", "Gate")
DELIVERABLE = re.compile(r"^\d+\.\s+\S")
TEST_NAME = re.compile(r"`(?:[^`\s]+::)?(test_\w+)`")
BACKTICKED = re.compile(r"`([^`]+)`")
FILE_EXTENSIONS = (
    ".py",
    ".md",
    ".toml",
    ".json",
    ".sh",
    ".yml",
    ".yaml",
    ".txt",
    ".cpp",
    ".h",
    ".hpp",
    ".c",
    ".js",
    ".ts",
    ".cfg",
    ".ini",
)
CHANGE_WORD = re.compile(r"\bchanges?\b|\bchanged\b")
WHITESPACE = re.compile(r"\s")


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif current is not None:
            current.append(line)
    return sections


def _section(sections: dict[str, list[str]], name: str) -> list[str] | None:
    """Body of the first heading whose text starts with name, case-insensitively; None if absent."""
    for heading, body in sections.items():
        if heading.lower().startswith(name.lower()):
            return body
    return None


def _only_bullets(lines: list[str]) -> list[str]:
    """Each `- Only …` bullet joined with its indented continuation lines."""
    bullets: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("- Only "):
            current = [line]
            bullets.append(current)
        elif current is not None and line[:1].isspace() and line.strip():
            current.append(line)
        else:
            current = None
    return ["\n".join(bullet) for bullet in bullets]


def _paths(text: str) -> set[str]:
    """Backticked spans naming a file: no whitespace, has '/' or allowed extension; `path::test` skipped."""
    return {
        span
        for span in BACKTICKED.findall(text)
        if not WHITESPACE.search(span) and "::" not in span and ("/" in span or span.endswith(FILE_EXTENSIONS))
    }


def measure(text: str) -> dict[str, int]:
    """Counts of files, deliverables and tests in a brief, as the limits in check() compare them."""
    sections = _sections(text)
    expected = _section(sections, "Expected output") or []

    deliverables = sum(1 for line in expected if DELIVERABLE.match(line))

    files = set()
    for bullet in _only_bullets(_section(sections, "Constraints") or []):
        if CHANGE_WORD.search(bullet):
            files |= _paths(bullet)
    files |= _paths("\n".join(expected))

    tests = {match.group(1) for match in TEST_NAME.finditer(text)}
    return {"files": len(files), "deliverables": deliverables, "tests": len(tests)}


def check(text: str) -> list[str]:
    """One problem string per violated rule; an empty list means the brief is small enough."""
    sections = _sections(text)
    problems = [f"missing heading: ## {name}" for name in HEADINGS if _section(sections, name) is None]
    counts = measure(text)

    if counts["deliverables"] > MAX_DELIVERABLES:
        problems.append(f"too many deliverables: {counts['deliverables']} (max {MAX_DELIVERABLES})")
    if counts["files"] > MAX_FILES:
        problems.append(f"too many files: {counts['files']} (max {MAX_FILES})")
    if counts["tests"] > MAX_TESTS:
        problems.append(f"too many tests: {counts['tests']} (max {MAX_TESTS})")

    return problems
