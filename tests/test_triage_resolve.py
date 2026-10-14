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
    outside_file = tmp_path.parent / "outside.py"
    outside_file.write_text("outside\n")
    try:
        finding = TriageFinding(
            id="F1",
            severity="high",
            decision="reject",
            evidence="../outside.py:1",
        )
        triage = Triage(findings=[finding], fix_brief=VALID_FIX_BRIEF)
        reviewed = [ReviewFinding(id="F1", severity="high", text="high issue")]

        res = resolve(triage, reviewed, tmp_path, set())

        assert res.findings[0].decision == "fix"
    finally:
        outside_file.unlink(missing_ok=True)


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
