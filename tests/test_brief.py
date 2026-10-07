import pytest

from agenthub.brief import BriefError, review_text, validate


def test_implement_brief_ok():
    validate("implement", "## Input\ntext\n## Expected output\nmore text")


def test_implement_brief_missing_both_lists_both():
    with pytest.raises(BriefError, match="Brief missing headings: Input, Expected output"):
        validate("implement", "no headings here")


def test_brief_headings_any_case_and_level():
    validate("implement", "###   iNpUt  :\r\nblah\r\n#### EXPECTED OUTPUT \r\nblah")


def test_h1_does_not_count():
    with pytest.raises(BriefError, match="Brief missing headings: Input, Expected output"):
        validate("implement", "# Input\n# Expected output")


def test_review_brief_optional():
    validate("review", "no headings at all")
    validate("review", "")


def test_unknown_role():
    with pytest.raises(BriefError, match="Unknown role: xyz"):
        validate("xyz", "## Input\n## Expected output")


def test_review_text_contains_base_and_notes():
    text = review_text("master", "check security")
    assert "`master`" in text
    assert "`git diff master...HEAD`" in text
    assert "## Orchestrator notes" in text
    assert "check security" in text


def test_review_text_without_notes_has_no_notes_section():
    text = review_text("main", None)
    assert "`main`" in text
    assert "## Orchestrator notes" not in text

    text2 = review_text("main", "   \n  ")
    assert "## Orchestrator notes" not in text2
