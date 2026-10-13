from pathlib import Path

import pytest

from agenthub.brief import BriefError, order_table, plan_text, review_text, standing_orders, validate


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


def test_review_preamble_says_no_shell():
    text = review_text("main", None, Path("/j/diff.patch"))
    assert "no shell" in text
    assert "do not run commands" in text
    assert "file-reading tools" in text


def test_review_preamble_limits_reads_to_worktree_and_diff():
    text = review_text("main", None, Path("/j/diff.patch"))
    assert "inside the current directory" in text
    assert "not available" in text
    assert "unverified" in text


def test_review_preamble_says_no_urls():
    text = review_text("main", None, Path("/j/diff.patch"))
    assert "URLs and the web are not available" in text
    assert "do not fetch them" in text


def test_review_text_points_at_diff_file_and_has_no_git_command():
    text = review_text("master", None, Path("/jobs/j1/diff.patch"))
    assert "/jobs/j1/diff.patch" in text
    assert "git diff" not in text
    assert "git log" not in text
    assert "Do not modify any file" in text


def test_orders_suffix_stripped_in_brief(tmp_path):
    (tmp_path / "standing-orders.md").write_text(
        "1. Run the lint — enforced by: gate\n2. Never edit gate config -- enforced by: gate-guard\n"
    )
    text = standing_orders(tmp_path, None)
    assert text == "## Standing orders\n\n1. Run the lint\n2. Never edit gate config"


def test_orders_mixed_lines_all_sent(tmp_path):
    config = tmp_path / "config"
    repo = tmp_path / "repo"
    (repo / ".agent-hub").mkdir(parents=True)
    config.mkdir()
    (config / "standing-orders.md").write_text("1. Enforced rule — enforced by: gate\n2. Bare rule with no suffix\n")
    (repo / ".agent-hub" / "standing-orders.md").write_text("3. Repo rule without suffix\n")

    text = standing_orders(config, repo)
    assert "1. Enforced rule\n2. Bare rule with no suffix" in text
    assert text.index("2. Bare rule") < text.index("3. Repo rule without suffix")
    assert "enforced by" not in text

    orders = order_table(config, repo)
    assert [(o.number, o.enforced_by) for o in orders] == [(1, "gate"), (2, None), (3, None)]


def test_plan_preamble_states_read_only_rule():
    text = plan_text("## Input\nbody")
    assert "read-only" in text
    assert "do not modify, create or delete files" in text
    assert "do not fetch URLs" in text
    assert "final message" in text
    assert "no shell" not in text
