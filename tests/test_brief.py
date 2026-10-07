from pathlib import Path

import pytest

from agenthub.brief import BriefError, review_text, validate


def test_implement_brief_ok():
    assert validate("implement", "## Input\ntext\n## Expected output\nmore text") is None


def test_implement_brief_missing_both_lists_both():
    with pytest.raises(BriefError, match="Brief missing headings: Input, Expected output"):
        validate("implement", "no headings here")


def test_brief_headings_any_case_and_level():
    assert validate("implement", "###   iNpUt  :\r\nblah\r\n#### EXPECTED OUTPUT \r\nblah") is None


def test_h1_does_not_count():
    with pytest.raises(BriefError, match="Brief missing headings: Input, Expected output"):
        validate("implement", "# Input\n# Expected output")


def test_review_brief_optional():
    assert validate("review", "no headings at all") is None
    assert validate("review", "") is None


def test_unknown_role():
    with pytest.raises(BriefError, match="Unknown role: xyz"):
        validate("xyz", "## Input\n## Expected output")


def test_review_text_contains_base_and_notes():
    text = review_text("master", "check security", Path("/j/diff.patch"))
    assert "`master`" in text
    assert "/j/diff.patch" in text
    assert "## Orchestrator notes" in text
    assert "check security" in text


def test_review_text_without_notes_has_no_notes_section():
    text = review_text("main", None, Path("/j/diff.patch"))
    assert "`main`" in text
    assert "## Orchestrator notes" not in text

    text2 = review_text("main", "   \n  ", Path("/j/diff.patch"))
    assert "## Orchestrator notes" not in text2


def test_review_text_points_at_diff_file_and_has_no_git_command():
    text = review_text("master", None, Path("/jobs/j1/diff.patch"))
    assert "/jobs/j1/diff.patch" in text
    assert "git diff" not in text
    assert "git log" not in text
    assert "Do not modify any file" in text
