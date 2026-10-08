import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agenthub import guards


@dataclass
class Change:
    path: str
    kind: str
    line_num: int
    content: str


def _parse_diff(diff_text: str) -> list[Change]:
    changes = []
    current_file = None
    old_line = 0
    new_line = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("--- a/") and current_file is None:
            pass
        elif line.startswith("--- a/"):
            current_file = line[6:]
        elif line.startswith("+++ /dev/null"):
            pass
        elif line.startswith("--- /dev/null"):
            pass
        elif line.startswith("@@ "):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(2))
        elif line.startswith("+") and not line.startswith("+++"):
            if current_file:
                changes.append(Change(current_file, "added", new_line, line[1:]))
                new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            if current_file:
                changes.append(Change(current_file, "removed", old_line, line[1:]))
                old_line += 1
    return changes


def _is_test_file(path_str: str) -> bool:
    p = Path(path_str)
    if p.name.startswith("test_") and p.name.endswith(".py"):
        return True
    if p.name.endswith("_test.py"):
        return True
    if p.name == "conftest.py":
        return True
    if "tests" in p.parts:
        return True
    return False


def _is_gate_config(path_str: str) -> bool:
    name = Path(path_str).name
    return name in {
        "justfile",
        "Makefile",
        "pyproject.toml",
        "setup.cfg",
        ".ruff.toml",
        "ruff.toml",
        "pytest.ini",
        "tox.ini",
        "conftest.py",
    }


def _get_sections(content: str) -> dict[int, str]:
    mapping = {}
    current_section = None
    for i, line in enumerate(content.splitlines(), start=1):
        line_s = line.strip()
        if line_s.startswith("[") and line_s.endswith("]"):
            current_section = line_s[1:-1].strip()
        mapping[i] = current_section
    return mapping


def inspect_diff(repo: guards.Repo, since: str) -> list[dict]:
    # 1. Tracked changes
    diff_res = subprocess.run(
        guards._git_argv("diff", "-U0", "--no-ext-diff", "--no-textconv", since),
        cwd=repo.root,
        capture_output=True,
        text=True,
        env=guards._git_env(),
    )
    changes = _parse_diff(diff_res.stdout)

    # 2. Untracked changes
    untracked_res = subprocess.run(
        guards._git_argv("ls-files", "--others", "--exclude-standard"),
        cwd=repo.root,
        capture_output=True,
        text=True,
        env=guards._git_env(),
    )
    for path in untracked_res.stdout.strip().splitlines():
        file_path = repo.root / path
        if file_path.is_file():
            try:
                content = file_path.read_text()
                for i, line in enumerate(content.splitlines(), start=1):
                    changes.append(Change(path, "added", i, line))
            except UnicodeDecodeError:
                pass

    findings = []

    # Process changes
    for c in changes:
        if _is_test_file(c.path):
            if c.kind == "removed" and "assert" in c.content:
                findings.append({"file": c.path, "kind": "assert-removed", "line": c.line_num})
            if c.kind == "added" and re.search(r"\b(?:skip|xfail)\b|--ignore|--deselect", c.content):
                findings.append({"file": c.path, "kind": "skip-added", "line": c.line_num})
            if c.kind == "removed" and re.search(r"def\s+test_", c.content):
                findings.append({"file": c.path, "kind": "test-deleted", "line": c.line_num})

        if _is_gate_config(c.path):
            if c.path == "pyproject.toml":
                pass  # Handled separately below
            else:
                findings.append({"file": c.path, "kind": "gate-config-changed", "line": c.line_num})

    # Process pyproject.toml separately
    pyproject_changes = [c for c in changes if c.path == "pyproject.toml"]
    if pyproject_changes:
        added_changes = [c for c in pyproject_changes if c.kind == "added"]
        removed_changes = [c for c in pyproject_changes if c.kind == "removed"]

        file_path = repo.root / "pyproject.toml"
        if added_changes and file_path.exists():
            content = file_path.read_text()
            mapping = _get_sections(content)
            for c in added_changes:
                sec = mapping.get(c.line_num)
                if sec and (sec.startswith("tool.ruff") or sec.startswith("tool.pytest")):
                    findings.append({"file": "pyproject.toml", "kind": "gate-config-changed", "line": c.line_num})

        if removed_changes:
            res = subprocess.run(
                guards._git_argv("show", f"{since}:pyproject.toml"),
                cwd=repo.root,
                capture_output=True,
                text=True,
                env=guards._git_env(),
            )
            if res.returncode == 0:
                mapping = _get_sections(res.stdout)
                for c in removed_changes:
                    sec = mapping.get(c.line_num)
                    if sec and (sec.startswith("tool.ruff") or sec.startswith("tool.pytest")):
                        findings.append({"file": "pyproject.toml", "kind": "gate-config-changed", "line": c.line_num})

    # Group findings: {"file", "kind", "lines": [sorted line numbers]}
    grouped = {}
    for f in findings:
        k = (f["file"], f["kind"])
        if k not in grouped:
            grouped[k] = set()
        grouped[k].add(f["line"])

    unique_findings = []
    for (file, kind), lines in grouped.items():
        unique_findings.append({"file": file, "kind": kind, "lines": sorted(list(lines))})

    return unique_findings
