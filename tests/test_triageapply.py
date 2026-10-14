from pathlib import Path

from agenthub.planstate import TaskState
from agenthub.triage import Resolution, TriageFinding
from agenthub.triageapply import apply


def test_fix_sets_pending_and_counts_round(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    tstate = TaskState(status="running", fix_rounds=0)
    finding = TriageFinding(id="F1", severity="high", decision="fix", evidence="foo.py:1")
    resolution = Resolution(findings=[finding], fix_brief="## Goal\nFix it", blocked_reason="")

    result = apply(plan_dir, "task-a", tstate, resolution, "job-123")

    assert result["status"] == "fix"
    assert tstate.status == "pending"
    assert tstate.fix_rounds == 1
    assert tstate.reason == "## Goal\nFix it"


def test_fix_brief_file_named_by_round(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    tstate = TaskState(status="running", fix_rounds=2)
    finding = TriageFinding(id="F1", severity="high", decision="fix", evidence="foo.py:1")
    resolution = Resolution(findings=[finding], fix_brief="## Goal\nFix round 3", blocked_reason="")

    result = apply(plan_dir, "task-b", tstate, resolution, "job-456")

    expected_path = plan_dir / "fixes" / "task-b-3.md"
    assert expected_path.is_file()
    assert expected_path.read_text() == "## Goal\nFix round 3"
    assert result["fix_path"] == str(expected_path)


def test_rejected_file_written_without_fix(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    tstate = TaskState(status="running", fix_rounds=0, reason="old reason")
    findings = [
        TriageFinding(id="F1", severity="high", decision="reject", evidence="a.py:10"),
        TriageFinding(id="F2", severity="medium", decision="reject", evidence="b.py:20"),
    ]
    resolution = Resolution(findings=findings, fix_brief="", blocked_reason="")

    result = apply(plan_dir, "task-c", tstate, resolution, "job-789")

    assert result["status"] == "rejected-only"
    assert result["counts"] == {"fix": 0, "reject": 2, "blocked": 0}
    assert result["fix_path"] == ""
    assert result["reason"] == ""

    # Task state unchanged
    assert tstate.status == "running"
    assert tstate.fix_rounds == 0
    assert tstate.reason == "old reason"

    # Fixes directory and rejected file
    rejected_path = plan_dir / "fixes" / "task-c-1-rejected.md"
    assert rejected_path.is_file()
    expected = (
        "## Review job-789\n"
        "- F1 (high): (text not recorded)\n"
        "  rejected: a.py:10\n"
        "- F2 (medium): (text not recorded)\n"
        "  rejected: b.py:20\n"
    )
    assert rejected_path.read_text() == expected
    assert result["rejected_path"] == str(rejected_path)


def test_blocked_sets_blocked_and_reason(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    tstate = TaskState(status="running", fix_rounds=1, attempts_base=0)
    findings = [
        TriageFinding(id="F1", severity="high", decision="reject", evidence="a.py:10"),
        TriageFinding(id="F2", severity="medium", decision="blocked", evidence="needs director"),
    ]
    resolution = Resolution(findings=findings, fix_brief="", blocked_reason="blocked: needs director")

    result = apply(plan_dir, "task-d", tstate, resolution, "job-999")

    assert result["status"] == "blocked"
    assert result["reason"] == "blocked: needs director"
    assert result["fix_path"] == ""
    assert result["rejected_path"] == ""
    assert result["counts"] == {"fix": 0, "reject": 1, "blocked": 1}

    # Task state
    assert tstate.status == "blocked"
    assert tstate.reason == "blocked: needs director"
    assert tstate.fix_rounds == 1  # unchanged
    assert tstate.attempts_base == 0  # unchanged

    # No files in fixes/
    fixes_dir = plan_dir / "fixes"
    assert not fixes_dir.exists()


def test_appends_triage_log_line(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    tstate = TaskState(status="running", fix_rounds=0)
    finding1 = TriageFinding(id="F1", severity="high", decision="fix", evidence="foo.py:10")
    finding2 = TriageFinding(id="F2", severity="low", decision="reject", evidence="bar.py:20")
    resolution1 = Resolution(findings=[finding1, finding2], fix_brief="## Goal\nFix", blocked_reason="")

    apply(plan_dir, "task-e", tstate, resolution1, "job-review-1")

    log_file = plan_dir / "triage.jsonl"
    assert log_file.is_file()
    lines = log_file.read_text().splitlines()
    assert len(lines) == 1

    import json

    data1 = json.loads(lines[0])
    assert data1 == {
        "task": "task-e",
        "round": 1,
        "review_job": "job-review-1",
        "findings": [
            {"id": "F1", "severity": "high", "decision": "fix", "evidence": "foo.py:10"},
            {"id": "F2", "severity": "low", "decision": "reject", "evidence": "bar.py:20"},
        ],
    }

    # Second apply appends a second line
    finding3 = TriageFinding(id="F3", severity="medium", decision="blocked", evidence="needs spec")
    resolution2 = Resolution(findings=[finding3], fix_brief="", blocked_reason="blocked: needs spec")
    apply(plan_dir, "task-e", tstate, resolution2, "job-review-2")

    lines = log_file.read_text().splitlines()
    assert len(lines) == 2
    data2 = json.loads(lines[1])
    assert data2 == {
        "task": "task-e",
        "round": 2,  # fix_rounds was incremented to 1 by previous fix
        "review_job": "job-review-2",
        "findings": [
            {"id": "F3", "severity": "medium", "decision": "blocked", "evidence": "needs spec"},
        ],
    }


def test_attempts_base_set_to_attempt_count(tmp_path: Path):
    from agenthub.planstate import AttemptState

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    attempts = [
        AttemptState(job="job-1", status="ok", exit_code=0),
        AttemptState(job="job-2", status="failed", exit_code=1),
        AttemptState(job="job-3", status="failed", exit_code=1),
    ]
    tstate = TaskState(status="running", attempts=attempts, fix_rounds=0, attempts_base=0)
    finding = TriageFinding(id="F1", severity="high", decision="fix", evidence="foo.py:1")
    resolution = Resolution(findings=[finding], fix_brief="## Goal\nFix", blocked_reason="")

    apply(plan_dir, "task-f", tstate, resolution, "job-review-3")

    assert tstate.attempts_base == 3


def test_fix_with_rejects_writes_both_files(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    tstate = TaskState(status="running", fix_rounds=0)
    findings = [
        TriageFinding(id="F1", severity="high", decision="fix", evidence="foo.py:1"),
        TriageFinding(id="F2", severity="medium", decision="reject", evidence="bar.py:2"),
    ]
    resolution = Resolution(findings=findings, fix_brief="## Goal\nFix both", blocked_reason="")

    result = apply(plan_dir, "task-g", tstate, resolution, "job-review-4")

    assert result["status"] == "fix"
    assert result["counts"] == {"fix": 1, "reject": 1, "blocked": 0}
    assert result["reason"] == ""

    fix_file = plan_dir / "fixes" / "task-g-1.md"
    assert fix_file.is_file()
    assert fix_file.read_text() == "## Goal\nFix both"
    assert result["fix_path"] == str(fix_file)

    rej_file = plan_dir / "fixes" / "task-g-1-rejected.md"
    assert rej_file.is_file()
    expected_rej = "## Review job-review-4\n- F2 (medium): (text not recorded)\n  rejected: bar.py:2\n"
    assert rej_file.read_text() == expected_rej
    assert result["rejected_path"] == str(rej_file)


def test_fix_without_rejects_has_no_rejected_file(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    tstate = TaskState(status="running", fix_rounds=0)
    findings = [
        TriageFinding(id="F1", severity="high", decision="fix", evidence="foo.py:1"),
    ]
    resolution = Resolution(findings=findings, fix_brief="## Goal\nFix only", blocked_reason="")

    result = apply(plan_dir, "task-h", tstate, resolution, "job-review-5")

    assert result["status"] == "fix"
    assert result["rejected_path"] == ""
    assert not (plan_dir / "fixes" / "task-h-1-rejected.md").exists()


def test_blocked_with_fix_findings_writes_no_files(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    tstate = TaskState(status="running", fix_rounds=1)
    findings = [
        TriageFinding(id="F1", severity="high", decision="fix", evidence="foo.py:1"),
        TriageFinding(id="F2", severity="medium", decision="reject", evidence="bar.py:2"),
    ]
    resolution = Resolution(
        findings=findings,
        fix_brief="## Goal\nToo big",
        blocked_reason="fix brief too big or malformed: brief exceeds 80 lines",
    )

    result = apply(plan_dir, "task-i", tstate, resolution, "job-review-6")

    assert result["status"] == "blocked"
    assert result["reason"] == "fix brief too big or malformed: brief exceeds 80 lines"
    assert result["fix_path"] == ""
    assert result["rejected_path"] == ""
    assert not (plan_dir / "fixes").exists()
    assert tstate.status == "blocked"
    assert tstate.reason == "fix brief too big or malformed: brief exceeds 80 lines"
    assert tstate.fix_rounds == 1


def test_rejected_file_has_review_text(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    tstate = TaskState(status="running", fix_rounds=0)
    findings = [
        TriageFinding(id="F1", severity="high", decision="reject", evidence="a.py:10"),
        TriageFinding(id="F2", severity="medium", decision="reject", evidence="b.py:20"),
    ]
    resolution = Resolution(
        findings=findings,
        fix_brief="",
        blocked_reason="",
        review_text={"F1": "high — a.py:10 missing check"},
    )

    result = apply(plan_dir, "task-rev", tstate, resolution, "job-rev-1")

    rejected_path = plan_dir / "fixes" / "task-rev-1-rejected.md"
    assert rejected_path.is_file()
    expected = (
        "## Review job-rev-1\n"
        "- F1 (high): high — a.py:10 missing check\n"
        "  rejected: a.py:10\n"
        "- F2 (medium): (text not recorded)\n"
        "  rejected: b.py:20\n"
    )
    assert rejected_path.read_text() == expected
    assert result["rejected_path"] == str(rejected_path)


def test_rejected_file_appends(tmp_path: Path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    tstate = TaskState(status="running", fix_rounds=0)
    finding1 = [TriageFinding(id="F1", severity="high", decision="reject", evidence="a.py:10")]
    res1 = Resolution(
        findings=finding1,
        fix_brief="",
        blocked_reason="",
        review_text={"F1": "finding 1 text"},
    )
    result1 = apply(plan_dir, "task-app", tstate, resolution=res1, review_job="job-1")

    rejected_path = plan_dir / "fixes" / "task-app-1-rejected.md"
    assert rejected_path.is_file()
    assert result1["rejected_path"] == str(rejected_path)

    finding2 = [TriageFinding(id="F2", severity="medium", decision="reject", evidence="b.py:20")]
    res2 = Resolution(
        findings=finding2,
        fix_brief="",
        blocked_reason="",
        review_text={"F2": "finding 2 text"},
    )
    result2 = apply(plan_dir, "task-app", tstate, resolution=res2, review_job="job-2")
    assert result2["rejected_path"] == str(rejected_path)

    expected = (
        "## Review job-1\n"
        "- F1 (high): finding 1 text\n"
        "  rejected: a.py:10\n"
        "## Review job-2\n"
        "- F2 (medium): finding 2 text\n"
        "  rejected: b.py:20\n"
    )
    assert rejected_path.read_text() == expected


def test_gate_failure_fix_brief_starts_round(tmp_path: Path):
    from agenthub.planstate import AttemptState

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    attempts = [AttemptState(job="job-1", status="failed", exit_code=1)]
    tstate = TaskState(status="running", attempts=attempts, fix_rounds=0, attempts_base=0)
    resolution = Resolution(
        findings=[],
        fix_brief="## Goal\nFix gate failure",
        blocked_reason="",
    )

    result = apply(plan_dir, "task-gate", tstate, resolution, "gate-review-job")

    assert result["status"] == "fix"
    assert result["counts"] == {"fix": 0, "reject": 0, "blocked": 0}
    assert result["reason"] == ""
    assert result["rejected_path"] == ""

    fix_file = plan_dir / "fixes" / "task-gate-1.md"
    assert fix_file.is_file()
    assert fix_file.read_text() == "## Goal\nFix gate failure"
    assert result["fix_path"] == str(fix_file)

    assert tstate.status == "pending"
    assert tstate.reason == "## Goal\nFix gate failure"
    assert tstate.fix_rounds == 1
    assert tstate.attempts_base == 1
