from agenthub.tasksize import check
from agenthub.triage import (
    Resolution,
    ReviewFinding,
    Triage,
    TriageFinding,
    resolve,
)

VALID_FIX_BRIEF = """## Goal
Fix the bug

## Input
agenthub/a.py

## Expected output
1. `agenthub/a.py`: fix the issue
2. `tests/test_a.py`: `test_something`

## Constraints
- Only `agenthub/a.py` and `tests/test_a.py` change.

## Gate
`uv run pytest`
"""


def test_reject_with_path_line_evidence_kept(tmp_path):
    foo = tmp_path / "foo.py"
    foo.write_text("print('hello')\n")

    finding = TriageFinding(id="F1", severity="high", decision="reject", evidence="foo.py:1")
    triage = Triage(findings=[finding], fix_brief="")
    reviewed = [ReviewFinding(id="F1", severity="high", text="error in foo.py")]

    res = resolve(triage, reviewed, tmp_path, set())

    assert isinstance(res, Resolution)
    assert len(res.findings) == 1
    assert res.findings[0].decision == "reject"
    assert res.findings[0].evidence == "foo.py:1"
    assert res.blocked_reason == ""
    assert res.fix_brief == ""


def test_reject_without_evidence_becomes_fix(tmp_path):
    finding = TriageFinding(id="F1", severity="high", decision="reject", evidence="no evidence here")
    triage = Triage(findings=[finding], fix_brief=VALID_FIX_BRIEF)
    reviewed = [ReviewFinding(id="F1", severity="high", text="error in foo.py")]

    res = resolve(triage, reviewed, tmp_path, set())

    assert isinstance(res, Resolution)
    assert len(res.findings) == 1
    assert res.findings[0].decision == "fix"
    assert res.blocked_reason == ""


def test_reject_with_job_id_kept(tmp_path):
    job_ids = {"20261014-010000-implement-abcd"}
    finding = TriageFinding(
        id="F1",
        severity="medium",
        decision="reject",
        evidence="covered by job 20261014-010000-implement-abcd in log",
    )
    triage = Triage(findings=[finding], fix_brief="")
    reviewed = [ReviewFinding(id="F1", severity="medium", text="issue in bar.py")]

    res = resolve(triage, reviewed, tmp_path, job_ids)

    assert isinstance(res, Resolution)
    assert res.findings[0].decision == "reject"
    assert res.blocked_reason == ""


def test_unlisted_high_finding_is_incomplete(tmp_path):
    reviewed = [
        ReviewFinding(id="F1", severity="high", text="high issue"),
        ReviewFinding(id="F2", severity="high", text="second high issue"),
    ]
    triage = Triage(
        findings=[TriageFinding(id="F1", severity="high", decision="fix", evidence="")],
        fix_brief=VALID_FIX_BRIEF,
    )

    res = resolve(triage, reviewed, tmp_path, set())

    assert res.blocked_reason == "triage incomplete: F2"


def test_blocked_finding_blocks_task(tmp_path):
    finding = TriageFinding(id="F1", severity="low", decision="blocked", evidence="needs director decision")
    triage = Triage(findings=[finding], fix_brief="")
    reviewed = [ReviewFinding(id="F1", severity="low", text="low issue")]

    res = resolve(triage, reviewed, tmp_path, set())

    assert res.blocked_reason == "blocked: needs director decision"


def test_oversized_fix_brief_blocks(tmp_path):
    finding = TriageFinding(id="F1", severity="high", decision="fix", evidence="")
    triage = Triage(findings=[finding], fix_brief="")
    reviewed = [ReviewFinding(id="F1", severity="high", text="high issue")]

    problems = check("")
    expected_reason = f"fix brief too big or malformed: {'; '.join(problems)}"

    res = resolve(triage, reviewed, tmp_path, set())

    assert res.blocked_reason == expected_reason


def test_unlisted_low_finding_is_not_incomplete(tmp_path):
    reviewed = [
        ReviewFinding(id="F1", severity="high", text="high issue"),
        ReviewFinding(id="F2", severity="low", text="low issue"),
    ]
    foo = tmp_path / "foo.py"
    foo.write_text("print('hello')\n")
    triage = Triage(
        findings=[TriageFinding(id="F1", severity="high", decision="reject", evidence="foo.py:1")],
        fix_brief="",
    )

    res = resolve(triage, reviewed, tmp_path, set())

    assert res.blocked_reason == ""


def test_multiple_unlisted_findings_sorted_and_comma_separated(tmp_path):
    reviewed = [
        ReviewFinding(id="F3", severity="high", text="finding 3"),
        ReviewFinding(id="F1", severity="medium", text="finding 1"),
        ReviewFinding(id="F2", severity="low", text="finding 2"),
    ]
    triage = Triage(findings=[], fix_brief="")

    res = resolve(triage, reviewed, tmp_path, set())

    assert res.blocked_reason == "triage incomplete: F1, F3"


def test_reject_with_parent_traversal_becomes_fix(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("outside\n")

    finding = TriageFinding(
        id="F1",
        severity="high",
        decision="reject",
        evidence="../outside.py:1",
    )
    triage = Triage(findings=[finding], fix_brief=VALID_FIX_BRIEF)
    reviewed = [ReviewFinding(id="F1", severity="high", text="high issue")]

    res = resolve(triage, reviewed, wt, set())

    assert res.findings[0].decision == "fix"


def test_reject_with_line_zero_or_negative_becomes_fix(tmp_path):
    foo = tmp_path / "foo.py"
    foo.write_text("print('hello')\n")

    finding0 = TriageFinding(id="F1", severity="high", decision="reject", evidence="foo.py:0")
    triage = Triage(findings=[finding0], fix_brief=VALID_FIX_BRIEF)
    reviewed = [ReviewFinding(id="F1", severity="high", text="high issue")]

    res = resolve(triage, reviewed, tmp_path, set())
    assert res.findings[0].decision == "fix"


def test_order_of_checks_incomplete_before_blocked(tmp_path):
    reviewed = [
        ReviewFinding(id="F1", severity="high", text="high issue"),
        ReviewFinding(id="F2", severity="high", text="unlisted high issue"),
    ]
    triage = Triage(
        findings=[TriageFinding(id="F1", severity="high", decision="blocked", evidence="director needed")],
        fix_brief="",
    )

    res = resolve(triage, reviewed, tmp_path, set())

    assert res.blocked_reason == "triage incomplete: F2"


def test_order_of_checks_blocked_before_fix_brief(tmp_path):
    reviewed = [
        ReviewFinding(id="F1", severity="high", text="high issue"),
        ReviewFinding(id="F2", severity="medium", text="medium issue"),
    ]
    triage = Triage(
        findings=[
            TriageFinding(id="F1", severity="high", decision="blocked", evidence="director needed"),
            TriageFinding(id="F2", severity="medium", decision="fix", evidence=""),
        ],
        fix_brief="bad brief with no headings",
    )

    res = resolve(triage, reviewed, tmp_path, set())

    assert res.blocked_reason == "blocked: director needed"


def test_no_fix_findings_skips_fix_brief_check(tmp_path):
    foo = tmp_path / "foo.py"
    foo.write_text("content\n")

    reviewed = [ReviewFinding(id="F1", severity="high", text="issue")]
    triage = Triage(
        findings=[TriageFinding(id="F1", severity="high", decision="reject", evidence="foo.py:1")],
        fix_brief="not a valid brief",
    )

    res = resolve(triage, reviewed, tmp_path, set())

    assert res.findings[0].decision == "reject"
    assert res.blocked_reason == ""


def test_reject_line_past_end_becomes_fix(tmp_path):
    foo = tmp_path / "foo.py"
    foo.write_text("line 1\nline 2\n")

    finding = TriageFinding(id="F1", severity="high", decision="reject", evidence="foo.py:3")
    triage = Triage(findings=[finding], fix_brief=VALID_FIX_BRIEF)
    reviewed = [ReviewFinding(id="F1", severity="high", text="high issue")]

    res = resolve(triage, reviewed, tmp_path, set())
    assert res.findings[0].decision == "fix"


def test_reject_negative_line_becomes_fix(tmp_path):
    foo = tmp_path / "foo.py"
    foo.write_text("line 1\nline 2\n")

    finding = TriageFinding(id="F1", severity="high", decision="reject", evidence="foo.py:-1")
    triage = Triage(findings=[finding], fix_brief=VALID_FIX_BRIEF)
    reviewed = [ReviewFinding(id="F1", severity="high", text="high issue")]

    res = resolve(triage, reviewed, tmp_path, set())
    assert res.findings[0].decision == "fix"


def test_reject_absolute_path_inside_worktree_kept(tmp_path):
    foo = tmp_path / "foo.py"
    foo.write_text("print('hello')\n")

    abs_path = str(foo.resolve())
    finding = TriageFinding(id="F1", severity="high", decision="reject", evidence=f"{abs_path}:1")
    triage = Triage(findings=[finding], fix_brief="")
    reviewed = [ReviewFinding(id="F1", severity="high", text="high issue")]

    res = resolve(triage, reviewed, tmp_path, set())
    assert res.findings[0].decision == "reject"
    assert res.findings[0].evidence == f"{abs_path}:1"
    assert res.blocked_reason == ""


def test_reject_symlink_outside_worktree_becomes_fix(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("line 1\n")

    link = wt / "link.py"
    link.symlink_to(outside_file)

    finding = TriageFinding(id="F1", severity="high", decision="reject", evidence="link.py:1")
    triage = Triage(findings=[finding], fix_brief=VALID_FIX_BRIEF)
    reviewed = [ReviewFinding(id="F1", severity="high", text="high issue")]

    res = resolve(triage, reviewed, wt, set())
    assert res.findings[0].decision == "fix"


def test_job_id_must_match_whole_token(tmp_path):
    job_ids = {"20261014-010000-implement-abcd"}
    reviewed = [ReviewFinding(id="F1", severity="medium", text="issue in bar.py")]

    # Substring followed by hyphen/alphanumeric
    finding_sub1 = TriageFinding(
        id="F1",
        severity="medium",
        decision="reject",
        evidence="covered by 20261014-010000-implement-abcd-extra",
    )
    triage1 = Triage(findings=[finding_sub1], fix_brief=VALID_FIX_BRIEF)
    res1 = resolve(triage1, reviewed, tmp_path, job_ids)
    assert res1.findings[0].decision == "fix"

    # Substring preceded by underscore/alphanumeric
    finding_sub2 = TriageFinding(
        id="F1",
        severity="medium",
        decision="reject",
        evidence="covered by job_20261014-010000-implement-abcd",
    )
    triage2 = Triage(findings=[finding_sub2], fix_brief=VALID_FIX_BRIEF)
    res2 = resolve(triage2, reviewed, tmp_path, job_ids)
    assert res2.findings[0].decision == "fix"

    # Whole token match surrounded by punctuation or space
    finding_exact = TriageFinding(
        id="F1",
        severity="medium",
        decision="reject",
        evidence="see [20261014-010000-implement-abcd] for fix",
    )
    triage3 = Triage(findings=[finding_exact], fix_brief="")
    res3 = resolve(triage3, reviewed, tmp_path, job_ids)
    assert res3.findings[0].decision == "reject"


def test_gate_failure_fix_brief_is_size_checked(tmp_path):
    bad_brief = "no sections here"
    problems = check(bad_brief)
    triage_bad = Triage(findings=[], fix_brief=bad_brief)
    res_bad = resolve(triage_bad, [], tmp_path, set())
    assert res_bad.blocked_reason == f"fix brief too big or malformed: {'; '.join(problems)}"

    triage_good = Triage(findings=[], fix_brief=VALID_FIX_BRIEF)
    res_good = resolve(triage_good, [], tmp_path, set())
    assert res_good.blocked_reason == ""

    triage_empty = Triage(findings=[], fix_brief="   \n")
    res_empty = resolve(triage_empty, [], tmp_path, set())
    assert res_empty.blocked_reason == ""


def test_review_text_kept(tmp_path):
    reviewed = [
        ReviewFinding(id="F1", severity="high", text="high issue text"),
        ReviewFinding(id="F2", severity="low", text="low issue text"),
    ]
    triage = Triage(
        findings=[
            TriageFinding(id="F1", severity="high", decision="fix", evidence=""),
            TriageFinding(id="F2", severity="low", decision="reject", evidence=""),
        ],
        fix_brief=VALID_FIX_BRIEF,
    )
    res = resolve(triage, reviewed, tmp_path, set())
    assert res.review_text == {
        "F1": "high issue text",
        "F2": "low issue text",
    }
    # Also verify default
    res_direct = Resolution(findings=[], fix_brief="", blocked_reason="")
    assert res_direct.review_text == {}


def test_unparsed_blocks(tmp_path):
    foo = tmp_path / "foo.py"
    foo.write_text("print('hello')\n")

    finding = TriageFinding(id="F1", severity="high", decision="reject", evidence="foo.py:1")
    triage = Triage(findings=[finding], fix_brief="")
    reviewed = [ReviewFinding(id="F1", severity="high", text="error")]

    # Default unparsed=0 does not block
    res0 = resolve(triage, reviewed, tmp_path, set())
    assert res0.blocked_reason == ""

    # unparsed > 0 blocks with "review findings unparsed: <n>"
    res1 = resolve(triage, reviewed, tmp_path, set(), unparsed=2)
    assert res1.blocked_reason == "review findings unparsed: 2"

    # triage incomplete takes precedence over unparsed
    reviewed_extra = [
        ReviewFinding(id="F1", severity="high", text="error"),
        ReviewFinding(id="F2", severity="high", text="unlisted error"),
    ]
    res2 = resolve(triage, reviewed_extra, tmp_path, set(), unparsed=2)
    assert res2.blocked_reason == "triage incomplete: F2"

    # unparsed takes precedence over blocked findings and fix brief checks
    triage_blocked = Triage(
        findings=[TriageFinding(id="F1", severity="high", decision="blocked", evidence="waiting")],
        fix_brief="bad brief",
    )
    res3 = resolve(triage_blocked, reviewed, tmp_path, set(), unparsed=1)
    assert res3.blocked_reason == "review findings unparsed: 1"
