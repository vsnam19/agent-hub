"""Guard: tests must not hard-code real agy or codex model ids (D-121 section 2.4).

Real ids are renamed by the vendors; a literal in a test breaks the suite on
rename. Use fake ids (``agy:m1``, ``codex:m2``) or the default pool instead.
Claude aliases (``claude:haiku``) and fake ids stay allowed.
"""

import re
from pathlib import Path

# Built by concatenation so this file does not match its own pattern.
REAL_ID = re.compile("agy:(?:gemini|claude|gpt)-[A-Za-z0-9_.-]+" + "|" + "codex:gpt-[A-Za-z0-9_.-]+")


def find_offenders(root: Path, skip: Path) -> list[str]:
    """Return ``path:line`` for every real model id literal in ``root``'s .py files.

    Files under a ``fixtures`` directory and the ``skip`` file are ignored.
    Paths are reported relative to ``root``.
    """
    skip = skip.resolve()
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == skip:
            continue
        rel = path.relative_to(root)
        if "fixtures" in rel.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in REAL_ID.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{rel.as_posix()}:{line}")
    return offenders


def test_no_real_model_ids_in_tests():
    tests_dir = Path(__file__).parent
    offenders = find_offenders(tests_dir, Path(__file__))
    assert offenders == [], f"real model ids found in tests: {offenders}"


def test_guard_catches_planted_literal(tmp_path):
    agy_literal = "agy:" + "gemini-4-argon-medium"
    codex_literal = "codex:" + "gpt-5"
    (tmp_path / "a.py").write_text(f"x = 1\nmodel = '{agy_literal}'\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(f"y = 2\nz = 3\nmodel = '{codex_literal}'\n", encoding="utf-8")
    skip = tmp_path / "unrelated_guard.py"
    skip.write_text("", encoding="utf-8")

    offenders = find_offenders(tmp_path, skip)

    assert sorted(offenders) == ["a.py:2", "b.py:3"]


def test_guard_allows_claude_aliases_and_fake_ids(tmp_path):
    (tmp_path / "ok.py").write_text(
        "a = 'claude:haiku'\nb = 'claude:sonnet'\nc = 'agy:m1'\nd = 'codex:m2'\ne = 'fake:model-a'\n",
        encoding="utf-8",
    )
    skip = tmp_path / "unrelated_guard.py"
    skip.write_text("", encoding="utf-8")

    assert find_offenders(tmp_path, skip) == []


def test_guard_skips_fixtures_directory_and_itself(tmp_path):
    agy_literal = "agy:" + "gemini-3.8-flash-high"
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "data.py").write_text(f"m = '{agy_literal}'\n", encoding="utf-8")
    guard = tmp_path / "test_guard_self.py"
    guard.write_text(f"self_ref = '{agy_literal}'\n", encoding="utf-8")

    assert find_offenders(tmp_path, guard) == []
