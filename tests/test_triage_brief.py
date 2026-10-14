from agenthub import tasksize
from agenthub.brief import validate
from agenthub.triage import ReviewFinding, triage_brief


def test_brief_has_required_headings():
    brief = triage_brief(
        task_brief="## Goal\nDo task\n\n## Input\nInput details",
        findings=[ReviewFinding(id="F1", severity="high", text="high — agenthub/a.py:1 — issue")],
        review_reply="## Findings\n- high — agenthub/a.py:1 — issue",
        gate_tail="",
        diff_path="job/diff.patch",
        worktree="/tmp/wt",
        gates=["just lint", "uv run pytest -q"],
    )
    validate("plan", brief)
    assert "## Goal" in brief
    assert "## Input" in brief
    assert "$" not in brief


def test_brief_lists_findings_and_paths():
    findings = [
        ReviewFinding(id="F1", severity="high", text="agenthub/a.py:12 — fd leak — fds leak over time"),
        ReviewFinding(id="F2", severity="medium", text="agenthub/b.py:44 — missing check — crash on None"),
    ]
    brief = triage_brief(
        task_brief="## Goal\nImplement feature X\n\n## Input\nTask inputs",
        findings=findings,
        review_reply="Review findings summary:\n- F1 fd leak\n- F2 missing check",
        gate_tail="",
        diff_path="review/diff.patch",
        worktree="/path/to/worktree",
        gates=["uv run pytest -q"],
        answered="F0 (low): already rejected with proof job-123",
    )
    assert "Implement feature X" in brief
    assert "/path/to/worktree" in brief
    assert "review/diff.patch" in brief
    assert "Review findings summary:" in brief
    assert "F1 (high): agenthub/a.py:12 — fd leak — fds leak over time" in brief
    assert "F2 (medium): agenthub/b.py:44 — missing check — crash on None" in brief
    assert "F0 (low): already rejected with proof job-123" in brief


def test_brief_states_schema_and_limits():
    brief = triage_brief(
        task_brief="## Goal\nTask\n\n## Input\nDetails",
        findings=[],
        review_reply="no findings",
        gate_tail="",
        diff_path="diff.patch",
        worktree="/wt",
        gates=["just lint", "uv run pytest -q"],
    )
    assert f"at most {tasksize.MAX_FILES} files" in brief
    assert f"at most {tasksize.MAX_DELIVERABLES} numbered deliverables" in brief
    assert f"at most {tasksize.MAX_TESTS} named tests" in brief
    assert "`just lint` and `uv run pytest -q`" in brief
    assert "```json" in brief
    assert '"findings"' in brief
    assert '"fix_brief"' in brief
    assert '"id"' in brief
    assert '"severity"' in brief
    assert '"decision"' in brief
    assert '"evidence"' in brief
    assert "fix" in brief
    assert "reject" in brief
    assert "blocked" in brief
    assert "path:line" in brief
    for heading in ("Goal", "Input", "Expected output", "Constraints", "Gate"):
        assert heading in brief


def test_gate_tail_section_only_when_present():
    with_tail = triage_brief(
        task_brief="## Goal\nTask\n\n## Input\nDetails",
        findings=[],
        review_reply="review",
        gate_tail="FAILED tests/test_foo.py::test_bar - AssertionError: false",
        diff_path="diff.patch",
        worktree="/wt",
        gates=["just test"],
    )
    assert "FAILED tests/test_foo.py::test_bar - AssertionError: false" in with_tail
    assert "Gate failure" in with_tail

    without_tail = triage_brief(
        task_brief="## Goal\nTask\n\n## Input\nDetails",
        findings=[],
        review_reply="review",
        gate_tail="",
        diff_path="diff.patch",
        worktree="/wt",
        gates=["just test"],
    )
    assert "Gate failure" not in without_tail
    assert "FAILED" not in without_tail
    assert "Already-answered" not in without_tail
