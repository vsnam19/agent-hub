import json
from datetime import datetime, timezone
from pathlib import Path

from agenthub.plan import Plan, Task
from agenthub.planstate import TaskState
from agenthub.triagerun import triage_task

NOW = lambda: datetime(2026, 10, 14, tzinfo=timezone.utc)  # noqa: E731


def _setup(tmp_path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    brief = plan_dir / "t.md"
    brief.write_text("task brief")
    task = Task(name="t", role="implement", brief=brief, gate=["just lint"])
    plan = Plan(goal="g", repo=tmp_path, base="main", slug="s", tasks=[task], path=plan_dir / "plan.json")
    tstate = TaskState(status="done", worktree=str(wt))
    review_reply = tmp_path / "review" / "x" / "y" / "reply.md"
    review_reply.parent.mkdir(parents=True)
    review_reply.write_text("## Findings\n- high — agenthub/a.py:1 — broken thing\n\n## Verdict\nrequest-changes\n")
    jobs = [
        {"id": "jr", "kind": "review", "status": "ok", "verdict": "request-changes", "head": "H",
         "workdir": str(wt), "reply_path": str(review_reply)},
        {"id": "ji", "kind": "implement", "status": "ok", "agent": "claude", "model": "haiku", "workdir": str(wt)},
    ]  # fmt: skip
    return plan, task, tstate, jobs


def _fake(tmp_path, reply=None, status="ok", calls=None):
    def run(args, p, pool, now):
        if calls is not None:
            calls.append(args)
        out = {"status": status}
        if reply is not None:
            f = tmp_path / "reply.md"
            f.write_text(reply)
            out["reply_path"] = str(f)
        return out

    return run


def _reply(decision="fix", evidence="e", fix_brief="", findings=None):
    if findings is None:
        findings = [{"id": "F1", "severity": "high", "decision": decision, "evidence": evidence}]
    data = {
        "findings": findings,
        "fix_brief": fix_brief,
    }
    return "```json\n" + json.dumps(data) + "\n```\n"


def test_no_triage_needed_returns_none(tmp_path):
    plan, task, tstate, jobs = _setup(tmp_path)
    assert triage_task(plan, task, tstate, [], "H", NOW, run=_fake(tmp_path)) is None


def test_fix_reply_applies_and_returns_summary(tmp_path):
    plan, task, tstate, jobs = _setup(tmp_path)
    fix = "\n\n".join(
        [
            "## Goal\nfix it",
            "## Input\nx",
            "## Expected output\n1. `a.py`",
            "## Constraints\nnone",
            "## Gate\n`just lint`",
        ]
    )
    res = triage_task(plan, task, tstate, jobs, "H", NOW, run=_fake(tmp_path, _reply("fix", fix_brief=fix)))
    assert res is not None
    assert res["task"] == "t"
    assert res["status"] == "fix"
    assert res["counts"]["fix"] == 1
    assert tstate.fix_rounds == 1
    assert tstate.status == "pending"
    assert (plan.path.parent / "fixes" / "t-1.md").is_file()


def test_job_failure_blocks_task(tmp_path):
    plan, task, tstate, jobs = _setup(tmp_path)
    res = triage_task(plan, task, tstate, jobs, "H", NOW, run=_fake(tmp_path, status="failed"))
    assert res["status"] == "blocked"
    assert res["reason"] == "triage job failed"
    assert tstate.status == "blocked"
    assert res["counts"] == {"fix": 0, "reject": 0, "blocked": 0}


def test_invalid_reply_blocks_task(tmp_path):
    plan, task, tstate, jobs = _setup(tmp_path)
    res = triage_task(plan, task, tstate, jobs, "H", NOW, run=_fake(tmp_path, "no json here"))
    assert res["status"] == "blocked"
    assert res["reason"].startswith("triage reply invalid: ")
    assert tstate.reason == res["reason"]


def test_run_args_use_techlead_and_exclude_implementers(tmp_path):
    plan, task, tstate, jobs = _setup(tmp_path)
    calls = []
    triage_task(plan, task, tstate, jobs, "H", NOW, run=_fake(tmp_path, "x", calls=calls))
    assert len(calls) == 1
    args = calls[0]
    assert args.role == "techlead"
    assert args.kind == "plan"
    assert args.exclude == ["claude:haiku"]
    assert args.workdir == tmp_path / "wt"
    assert "task brief" in args.brief


def test_blocked_decision_blocks_task(tmp_path):
    plan, task, tstate, jobs = _setup(tmp_path)
    res = triage_task(plan, task, tstate, jobs, "H", NOW, run=_fake(tmp_path, _reply("blocked", "needs human")))
    assert res["status"] == "blocked"
    assert res["task"] == "t"
    assert res["reason"] == "blocked: needs human"
    assert tstate.status == "blocked"


def test_unlisted_review_finding_blocks(tmp_path):
    plan, task, tstate, jobs = _setup(tmp_path)
    res = triage_task(plan, task, tstate, jobs, "H", NOW, run=_fake(tmp_path, _reply(findings=[])))
    assert res["status"] == "blocked"
    assert res["reason"] == "triage incomplete: F1"
    assert tstate.status == "blocked"
    assert tstate.reason == "triage incomplete: F1"


def test_unparsed_review_bullet_blocks(tmp_path):
    plan, task, tstate, jobs = _setup(tmp_path)
    review_reply = Path(jobs[0]["reply_path"])
    review_reply.write_text(
        "## Findings\n"
        "- high — agenthub/a.py:1 — broken thing\n"
        "- unknown — agenthub/b.py:2 — weird thing\n\n"
        "## Verdict\nrequest-changes\n"
    )
    res = triage_task(plan, task, tstate, jobs, "H", NOW, run=_fake(tmp_path, _reply("blocked", "human needed")))
    assert res["status"] == "blocked"
    assert res["reason"] == "review findings unparsed: 1"
    assert tstate.status == "blocked"
    assert tstate.reason == "review findings unparsed: 1"


def test_rejected_context_in_round_order(tmp_path):
    plan, task, tstate, jobs = _setup(tmp_path)
    fixes_dir = plan.path.parent / "fixes"
    fixes_dir.mkdir(parents=True, exist_ok=True)
    (fixes_dir / f"{task.name}-10-rejected.md").write_text("round 10 rejection text")
    (fixes_dir / f"{task.name}-2-rejected.md").write_text("round 2 rejection text")
    (fixes_dir / f"{task.name}-z-rejected.md").write_text("non-int z rejection text")
    (fixes_dir / f"{task.name}-a-rejected.md").write_text("non-int a rejection text")
    calls = []
    triage_task(plan, task, tstate, jobs, "H", NOW, run=_fake(tmp_path, "x", calls=calls))
    assert len(calls) == 1
    brief = calls[0].brief
    pos2 = brief.find("round 2 rejection text")
    pos10 = brief.find("round 10 rejection text")
    pos_a = brief.find("non-int a rejection text")
    pos_z = brief.find("non-int z rejection text")
    assert pos2 != -1
    assert pos10 != -1
    assert pos_a != -1
    assert pos_z != -1
    assert pos2 < pos10 < pos_a < pos_z


def test_gate_failure_without_fix_brief_blocks(tmp_path):
    plan, task, tstate, _ = _setup(tmp_path)
    wt = Path(tstate.worktree)
    jobs = [
        {
            "id": "jg",
            "kind": "gate",
            "status": "failed",
            "head": "H",
            "workdir": str(wt),
            "gate": [{"cmd": "just lint", "rc": 1, "tail": "lint failed"}],
        },
        {"id": "ji", "kind": "implement", "status": "ok", "agent": "claude", "model": "haiku", "workdir": str(wt)},
    ]
    for empty in ("", "   \n  "):
        res = triage_task(plan, task, tstate, jobs, "H", NOW, run=_fake(tmp_path, _reply(findings=[], fix_brief=empty)))
        assert res is not None
        assert res["status"] == "blocked"
        assert res["task"] == "t"
        assert res["reason"] == "gate failed and the triage wrote no fix brief"
        assert tstate.status == "blocked"
        assert tstate.reason == "gate failed and the triage wrote no fix brief"
        assert res["counts"] == {"fix": 0, "reject": 0, "blocked": 0}
        assert not (plan.path.parent / "triage.jsonl").exists()


def test_gate_failure_with_fix_brief_starts_round(tmp_path):
    plan, task, tstate, _ = _setup(tmp_path)
    wt = Path(tstate.worktree)
    jobs = [
        {
            "id": "jg",
            "kind": "gate",
            "status": "failed",
            "head": "H",
            "workdir": str(wt),
            "gate": [{"cmd": "just lint", "rc": 1, "tail": "lint failed"}],
        },
        {"id": "ji", "kind": "implement", "status": "ok", "agent": "claude", "model": "haiku", "workdir": str(wt)},
    ]
    fix = "\n\n".join(
        [
            "## Goal\nfix lint",
            "## Input\nx",
            "## Expected output\n1. `a.py`",
            "## Constraints\nnone",
            "## Gate\n`just lint`",
        ]
    )
    res = triage_task(plan, task, tstate, jobs, "H", NOW, run=_fake(tmp_path, _reply(findings=[], fix_brief=fix)))
    assert res is not None
    assert res["status"] == "fix"
    assert res["task"] == "t"
    assert tstate.status == "pending"
    assert tstate.fix_rounds == 1
    assert (plan.path.parent / "fixes" / "t-1.md").is_file()
    assert (plan.path.parent / "fixes" / "t-1.md").read_text(encoding="utf-8") == fix
    assert (plan.path.parent / "triage.jsonl").is_file()
