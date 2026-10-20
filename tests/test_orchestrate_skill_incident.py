from pathlib import Path

SKILL = Path(__file__).parent.parent / "skills" / "orchestrate" / "SKILL.md"


def _non_negotiables():
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("## Non-negotiables")
    end = text.index("## Routing")
    return text[start:end]


def test_non_negotiables_has_incident_rule():
    section = _non_negotiables()
    assert "agent-hub incident add" in section
    assert "not the implementer's" in section


def test_incident_rule_is_one_bullet():
    bullets = [line for line in _non_negotiables().splitlines() if line.startswith("- ")]
    matches = [line for line in bullets if "incident add" in line]
    assert len(matches) == 1
