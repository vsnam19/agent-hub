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
SKILLS = ROOT / "skills"
PRINCIPLES = sorted(p.name for p in SKILLS.glob("principle-*") if p.is_dir())


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


def test_brief_template_has_rationalizations_section():
    text = TEMPLATE.read_text()
    heading = "## Rationalizations to refuse"
    assert heading in text
    assert text.index("## Constraints") < text.index(heading) < text.index("## Gate")
    assert "| Excuse | Answer |" in text
    rows = [line for line in text.splitlines() if line.startswith("|") and "---" not in line]
    excuse_rows = [line for line in rows if line != "| Excuse | Answer |"]
    assert len(excuse_rows) == 4, "the example table has four excuse rows"


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


def test_principle_skills_have_frontmatter_and_are_model_invocation_disabled():
    assert len(PRINCIPLES) == 7
    for name in PRINCIPLES:
        text = (SKILLS / name / "SKILL.md").read_text()
        fields = _frontmatter(text)
        assert fields["name"] == name
        assert fields["description"].startswith("Apply when")
        assert fields["disable-model-invocation"] == "true"
        for marker in ("**Why:**", "**Pattern:**", "**Boundaries:**"):
            assert marker in text, f"{name} lacks {marker}"
        assert len(text.splitlines()) <= 32, f"{name} is too long"
        assert re.search(r"D-\d{3}", text), f"{name} cites no decision"


def test_orchestrate_lists_every_principle():
    text = SKILL.read_text()
    mentioned = set(re.findall(r"principle-[a-z-]+", text))
    assert mentioned == set(PRINCIPLES)
