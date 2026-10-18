from pathlib import Path

from agenthub.triage import ReviewFinding
from agenthub.triageinputs import TriageInputs, collect


def test_request_changes_review_gives_inputs(tmp_path: Path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    head = "sha-rev-1"

    job_dir = tmp_path / "jobs" / "job-rev-1"
    attempt_dir = job_dir / "attempts" / "1-agy-m"
    attempt_dir.mkdir(parents=True)
    reply_file = attempt_dir / "reply.md"
    reply_file.write_text(
        """## Findings
- high — agenthub/a.py:10 — fd leak — leaks files
- low — agenthub/b.py:20 — typo — doc typo

## Verdict
request-changes
"""
    )

    jobs = [
        {
            "id": "job-rev-1",
            "kind": "review",
            "status": "ok",
            "verdict": "request-changes",
            "reply_path": str(reply_file),
            "workdir": str(workdir),
            "head": head,
            "agent": "claude",
            "model": "sonnet",
        },
        {
            "id": "job-impl-1",
            "kind": "implement",
            "status": "ok",
            "workdir": str(workdir),
            "head": head,
            "agent": "agy",
            "model": "sonnet",
        },
    ]

    inputs = collect(jobs, workdir, head)
    assert inputs is not None
    assert isinstance(inputs, TriageInputs)
    assert inputs.review_job == "job-rev-1"
    assert inputs.review_reply == reply_file.read_text()
    assert inputs.findings == [
        ReviewFinding(id="F1", severity="high", text="high — agenthub/a.py:10 — fd leak — leaks files"),
        ReviewFinding(id="F2", severity="low", text="low — agenthub/b.py:20 — typo — doc typo"),
    ]
    assert inputs.gate_tail == ""
    assert inputs.diff_path == str(job_dir / "diff.patch")
    assert inputs.implementers == ["agy:sonnet"]
    assert inputs.job_ids == {"job-impl-1"}


def test_approve_gives_none(tmp_path: Path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    head = "sha-app-1"

    jobs = [
        {
            "id": "job-rev-app",
            "kind": "review",
            "status": "ok",
            "verdict": "approve",
            "workdir": str(workdir),
            "head": head,
            "agent": "claude",
            "model": "sonnet",
        },
        {
            "id": "job-impl-1",
            "kind": "implement",
            "status": "ok",
            "workdir": str(workdir),
            "head": head,
            "agent": "agy",
            "model": "sonnet",
        },
    ]

    inputs = collect(jobs, workdir, head)
    assert inputs is None


def test_failed_gate_gives_tail(tmp_path: Path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    head = "sha-gate-fail"

    jobs = [
        {
            "id": "job-gate-1",
            "kind": "gate",
            "status": "gate-failed",
            "workdir": str(workdir),
            "head": head,
            "gate": [
                {"cmd": "uv run pytest -q", "rc": 0, "tail": "10 passed"},
                {"cmd": "just lint", "rc": 1, "tail": "error: unused import in a.py"},
                {"cmd": "just typecheck", "rc": 2, "tail": "type error in b.py"},
            ],
        },
        {
            "id": "job-impl-1",
            "kind": "implement",
            "status": "ok",
            "workdir": str(workdir),
            "head": head,
            "agent": "codex",
            "model": "gpt-5",
        },
    ]

    inputs = collect(jobs, workdir, head)
    assert inputs is not None
    assert isinstance(inputs, TriageInputs)
    assert inputs.review_job == ""
    assert inputs.review_reply == ""
    assert inputs.findings == []
    assert inputs.gate_tail == "just lint\nerror: unused import in a.py"
    assert inputs.diff_path == ""
    assert inputs.implementers == ["codex:gpt-5"]
    assert inputs.job_ids == {"job-gate-1", "job-impl-1"}


def test_other_workdir_is_ignored(tmp_path: Path):
    my_wt = tmp_path / "my_wt"
    my_wt.mkdir()
    other_wt = tmp_path / "other_wt"
    other_wt.mkdir()
    head = "sha-workdir-test"

    jobs = [
        {
            "id": "job-other-rev",
            "kind": "review",
            "status": "ok",
            "verdict": "request-changes",
            "workdir": str(other_wt),
            "head": head,
        },
        {
            "id": "job-other-gate",
            "kind": "gate",
            "status": "gate-failed",
            "workdir": str(other_wt),
            "head": head,
            "gate": [{"cmd": "false", "rc": 1, "tail": "fail"}],
        },
    ]

    inputs = collect(jobs, my_wt, head)
    assert inputs is None


def test_only_newest_job_counts(tmp_path: Path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    head = "sha-newest"

    # Scenario 1: Newest is approve, older is request-changes -> None
    jobs_approved_newest = [
        {
            "id": "job-rev-approve",
            "kind": "review",
            "status": "ok",
            "verdict": "approve",
            "workdir": str(workdir),
            "head": head,
        },
        {
            "id": "job-rev-rc",
            "kind": "review",
            "status": "ok",
            "verdict": "request-changes",
            "workdir": str(workdir),
            "head": head,
        },
    ]
    assert collect(jobs_approved_newest, workdir, head) is None

    # Scenario 2: Newest is request-changes, older is approve -> TriageInputs
    reply_dir = tmp_path / "jobs" / "job-rev-rc2" / "attempts" / "1-agy-m"
    reply_dir.mkdir(parents=True)
    reply_file = reply_dir / "reply.md"
    reply_file.write_text("## Findings\n(none)\n\n## Verdict\nrequest-changes\n")

    jobs_rc_newest = [
        {
            "id": "job-rev-rc2",
            "kind": "review",
            "status": "ok",
            "verdict": "request-changes",
            "reply_path": str(reply_file),
            "workdir": str(workdir),
            "head": head,
        },
        {
            "id": "job-rev-approve2",
            "kind": "review",
            "status": "ok",
            "verdict": "approve",
            "workdir": str(workdir),
            "head": head,
        },
    ]
    res_rc = collect(jobs_rc_newest, workdir, head)
    assert res_rc is not None
    assert res_rc.review_job == "job-rev-rc2"

    # Scenario 3: Newest is gate ok, older is gate-failed -> None
    jobs_gate_ok_newest = [
        {
            "id": "job-gate-ok",
            "kind": "gate",
            "status": "ok",
            "workdir": str(workdir),
            "head": head,
            "gate": [{"cmd": "true", "rc": 0, "tail": ""}],
        },
        {
            "id": "job-gate-failed",
            "kind": "gate",
            "status": "gate-failed",
            "workdir": str(workdir),
            "head": head,
            "gate": [{"cmd": "false", "rc": 1, "tail": "failed"}],
        },
    ]
    assert collect(jobs_gate_ok_newest, workdir, head) is None

    # Scenario 4: Newest is gate-failed, older is gate ok -> TriageInputs
    jobs_gate_fail_newest = [
        {
            "id": "job-gate-failed2",
            "kind": "gate",
            "status": "gate-failed",
            "workdir": str(workdir),
            "head": head,
            "gate": [{"cmd": "false", "rc": 1, "tail": "failed"}],
        },
        {
            "id": "job-gate-ok2",
            "kind": "gate",
            "status": "ok",
            "workdir": str(workdir),
            "head": head,
            "gate": [{"cmd": "true", "rc": 0, "tail": ""}],
        },
    ]
    res_gate = collect(jobs_gate_fail_newest, workdir, head)
    assert res_gate is not None
    assert res_gate.gate_tail == "false\nfailed"


def test_collects_implementers(tmp_path: Path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    other_wt = tmp_path / "other"
    other_wt.mkdir()
    head = "sha-impl"

    jobs = [
        {
            "id": "j-gate",
            "kind": "gate",
            "status": "gate-failed",
            "workdir": str(workdir),
            "head": head,
            "gate": [{"cmd": "test", "rc": 1, "tail": "fail"}],
        },
        # Valid implementers in workdir:
        {
            "id": "j-impl-1",
            "kind": "implement",
            "status": "ok",
            "agent": "agy",
            "model": "sonnet",
            "workdir": str(workdir),
        },
        {
            "id": "j-impl-2",
            "kind": "implement",
            "status": "gate-failed",
            "agent": "claude",
            "model": "haiku",
            "workdir": str(workdir),
        },
        # Duplicate implementer:
        {
            "id": "j-impl-3",
            "kind": "implement",
            "status": "ok",
            "agent": "agy",
            "model": "sonnet",
            "workdir": str(workdir),
        },
        # Excluded by status:
        {
            "id": "j-impl-4",
            "kind": "implement",
            "status": "failed",
            "agent": "codex",
            "model": "gpt-5",
            "workdir": str(workdir),
        },
        {
            "id": "j-impl-5",
            "kind": "implement",
            "status": "cancelled",
            "agent": "codex",
            "model": "gpt-4",
            "workdir": str(workdir),
        },
        # Excluded by workdir:
        {
            "id": "j-impl-6",
            "kind": "implement",
            "status": "ok",
            "agent": "other",
            "model": "model",
            "workdir": str(other_wt),
        },
        # Excluded by kind:
        {
            "id": "j-rev",
            "kind": "review",
            "status": "ok",
            "agent": "reviewer",
            "model": "expert",
            "workdir": str(workdir),
        },
    ]

    inputs = collect(jobs, workdir, head)
    assert inputs is not None
    assert inputs.implementers == ["agy:sonnet", "claude:haiku"]
    assert inputs.job_ids == {
        "j-gate",
        "j-impl-1",
        "j-impl-2",
        "j-impl-3",
        "j-impl-4",
        "j-impl-5",
        "j-rev",
    }


def test_job_ids_only_this_worktree(tmp_path: Path):
    my_wt = tmp_path / "my_wt"
    my_wt.mkdir()
    other_wt = tmp_path / "other_wt"
    other_wt.mkdir()
    head = "sha-worktree-test"

    jobs = [
        {
            "id": "job-gate-my",
            "kind": "gate",
            "status": "gate-failed",
            "workdir": str(my_wt),
            "head": head,
            "gate": [{"cmd": "false", "rc": 1, "tail": "failed"}],
        },
        {
            "id": "job-impl-my",
            "kind": "implement",
            "status": "ok",
            "workdir": str(my_wt),
            "head": head,
        },
        {
            "id": "job-impl-other",
            "kind": "implement",
            "status": "ok",
            "workdir": str(other_wt),
            "head": head,
        },
        {
            "id": "job-rev-other",
            "kind": "review",
            "status": "ok",
            "verdict": "request-changes",
            "workdir": str(other_wt),
            "head": head,
        },
    ]

    inputs = collect(jobs, my_wt, head)
    assert inputs is not None
    assert "job-impl-other" not in inputs.job_ids
    assert "job-rev-other" not in inputs.job_ids
    assert inputs.job_ids == {"job-gate-my", "job-impl-my"}


def test_job_ids_exclude_triaged_review(tmp_path: Path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    head = "sha-exclude-rev"

    job_dir = tmp_path / "jobs" / "job-rev-triaged"
    attempt_dir = job_dir / "attempts" / "1-agy-m"
    attempt_dir.mkdir(parents=True)
    reply_file = attempt_dir / "reply.md"
    reply_file.write_text("## Findings\n- high — a.py:1 — err\n\n## Verdict\nrequest-changes\n")

    jobs = [
        {
            "id": "job-rev-triaged",
            "kind": "review",
            "status": "ok",
            "verdict": "request-changes",
            "reply_path": str(reply_file),
            "workdir": str(workdir),
            "head": head,
        },
        {
            "id": "job-impl-1",
            "kind": "implement",
            "status": "ok",
            "workdir": str(workdir),
            "head": head,
        },
        {
            "id": "job-impl-2",
            "kind": "implement",
            "status": "ok",
            "workdir": str(workdir),
            "head": head,
        },
        {
            "id": "job-rev-older",
            "kind": "review",
            "status": "ok",
            "verdict": "approve",
            "workdir": str(workdir),
            "head": "sha-old",
        },
    ]

    inputs = collect(jobs, workdir, head)
    assert inputs is not None
    assert inputs.review_job == "job-rev-triaged"
    assert "job-rev-triaged" not in inputs.job_ids
    assert "job-rev-older" in inputs.job_ids
    assert inputs.job_ids == {"job-impl-1", "job-impl-2", "job-rev-older"}


def test_running_gate_not_triaged(tmp_path: Path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    head = "sha-gate-running"

    jobs = [
        {
            "id": "job-gate-running",
            "kind": "gate",
            "status": "running",
            "workdir": str(workdir),
            "head": head,
        },
        {
            "id": "job-impl-1",
            "kind": "implement",
            "status": "ok",
            "workdir": str(workdir),
            "head": head,
            "agent": "codex",
            "model": "gpt-5",
        },
    ]

    inputs = collect(jobs, workdir, head)
    assert inputs is None


def test_cancelled_gate_not_triaged(tmp_path: Path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    head = "sha-gate-cancelled"

    jobs = [
        {
            "id": "job-gate-cancelled",
            "kind": "gate",
            "status": "cancelled",
            "workdir": str(workdir),
            "head": head,
        },
        {
            "id": "job-impl-1",
            "kind": "implement",
            "status": "ok",
            "workdir": str(workdir),
            "head": head,
            "agent": "codex",
            "model": "gpt-5",
        },
    ]

    inputs = collect(jobs, workdir, head)
    assert inputs is None


def test_undecodable_reply_does_not_crash(tmp_path: Path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    head = "sha-rev-undecodable"

    job_dir = tmp_path / "jobs" / "job-rev-undecodable"
    attempt_dir = job_dir / "attempts" / "1-agy-m"
    attempt_dir.mkdir(parents=True)
    reply_file = attempt_dir / "reply.md"
    reply_file.write_bytes(b"## Findings\n- high \x80\xff invalid utf-8\n\n## Verdict\nrequest-changes\n")

    jobs = [
        {
            "id": "job-rev-undecodable",
            "kind": "review",
            "status": "ok",
            "verdict": "request-changes",
            "reply_path": str(reply_file),
            "workdir": str(workdir),
            "head": head,
            "agent": "claude",
            "model": "sonnet",
        },
        {
            "id": "job-impl-1",
            "kind": "implement",
            "status": "ok",
            "workdir": str(workdir),
            "head": head,
            "agent": "agy",
            "model": "sonnet",
        },
    ]

    inputs = collect(jobs, workdir, head)
    assert inputs is not None
    assert isinstance(inputs, TriageInputs)
    assert inputs.review_job == "job-rev-undecodable"
    assert "\ufffd" in inputs.review_reply
