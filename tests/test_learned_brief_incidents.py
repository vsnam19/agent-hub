import importlib.resources
import string


def _template() -> str:
    return importlib.resources.files("agenthub").joinpath("templates/learned-brief.md").read_text("utf-8")


def test_template_mentions_incidents_section():
    text = _template()
    assert "## Incidents" in text
    assert "incidents" in text


def test_template_names_three_incident_threshold():
    text = _template()
    assert "learned-director" in text
    assert "3 or more incidents" in text


def test_template_still_substitutes_known_placeholders():
    text = _template()
    values = {
        "date": "2026-10-20",
        "window": "2026-10-13..2026-10-20",
        "existing_worker": "(none)",
        "existing_director": "(none)",
        "existing_base": "(none)",
    }
    rendered = string.Template(text).safe_substitute(values)
    assert "$" not in rendered
    assert "2026-10-20" in rendered
    assert "2026-10-13..2026-10-20" in rendered
