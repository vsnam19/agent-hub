import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agenthub import brief
from agenthub.orchestrate import EXIT

ROOT = Path(__file__).parent.parent
SKILL_DIR = ROOT / "skills" / "orchestrate"
SKILL = SKILL_DIR / "SKILL.md"
TEMPLATE = SKILL_DIR / "brief-template.md"
SMOKE = ROOT / "scripts" / "smoke.sh"


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def test_skill_frontmatter():
    fields = _frontmatter(SKILL.read_text())
    assert fields["name"] == "orchestrate"
    assert fields["description"]
    assert len(fields["description"]) < 1024


def test_skill_names_no_agent():
    text = (SKILL.read_text() + TEMPLATE.read_text()).lower()
    for word in ("agy", "codex", "gemini", "opus", "sonnet", "gpt"):
        assert word not in text, f"skill must not name {word}"


def test_skill_covers_every_status():
    text = SKILL.read_text()
    for status, code in EXIT.items():
        assert f"`{status}`" in text, f"status {status} missing from SKILL.md"
        assert str(code) in text


def test_template_has_required_headings():
    text = TEMPLATE.read_text()
    brief.validate("implement", text)
    for heading in ("## Goal", "## Input", "## Expected output", "## Constraints", "## Gate"):
        assert heading in text


def test_smoke_script_is_executable_and_shellchecked():
    assert SMOKE.exists()
    assert os.access(SMOKE, os.X_OK)
    assert SMOKE.read_text().startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in SMOKE.read_text()
    checker = shutil.which("shellcheck")
    if checker is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run([checker, str(SMOKE)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
