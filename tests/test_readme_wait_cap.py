from __future__ import annotations

from pathlib import Path

from agenthub.mcpserver import MAX_WAIT_S

README = Path(__file__).resolve().parent.parent / "README.md"


def test_readme_wait_cap_matches_max_wait_s() -> None:
    text = README.read_text(encoding="utf-8")
    assert f"at most {MAX_WAIT_S}s" in text
    assert "at most 300s" not in text
