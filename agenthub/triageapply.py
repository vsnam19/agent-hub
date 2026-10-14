import json
from pathlib import Path

from agenthub.planstate import TaskState
from agenthub.triage import Resolution


def apply(
    plan_dir: Path,
    task: str,
    tstate: TaskState,
    resolution: Resolution,
    review_job: str,
) -> dict:
    plan_dir = Path(plan_dir)
    round_num = tstate.fix_rounds + 1

    # triage.jsonl
    plan_dir.mkdir(parents=True, exist_ok=True)
    log_entry = {
        "task": task,
        "round": round_num,
        "review_job": review_job,
        "findings": [
            {
                "id": f.id,
                "severity": f.severity,
                "decision": f.decision,
                "evidence": f.evidence,
            }
            for f in resolution.findings
        ],
    }
    with open(plan_dir / "triage.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")

    counts = {
        "fix": sum(1 for f in resolution.findings if f.decision == "fix"),
        "reject": sum(1 for f in resolution.findings if f.decision == "reject"),
        "blocked": sum(1 for f in resolution.findings if f.decision == "blocked"),
    }

    if resolution.blocked_reason:
        tstate.status = "blocked"
        tstate.reason = resolution.blocked_reason
        return {
            "status": "blocked",
            "counts": counts,
            "fix_path": "",
            "rejected_path": "",
            "reason": resolution.blocked_reason,
        }

    has_fix = any(f.decision == "fix" for f in resolution.findings)
    rejects = [f for f in resolution.findings if f.decision == "reject"]

    rejected_path = ""
    if rejects:
        fixes_dir = plan_dir / "fixes"
        fixes_dir.mkdir(parents=True, exist_ok=True)
        rej_file = fixes_dir / f"{task}-{round_num}-rejected.md"
        rej_lines = [f"- {f.id} ({f.severity}): {f.evidence}" for f in rejects]
        rej_file.write_text("\n".join(rej_lines) + "\n", encoding="utf-8")
        rejected_path = str(rej_file)

    if has_fix:
        fixes_dir = plan_dir / "fixes"
        fixes_dir.mkdir(parents=True, exist_ok=True)
        fix_file = fixes_dir / f"{task}-{round_num}.md"
        fix_file.write_text(resolution.fix_brief, encoding="utf-8")
        fix_path = str(fix_file)

        tstate.status = "pending"
        tstate.reason = resolution.fix_brief
        tstate.fix_rounds += 1
        tstate.attempts_base = len(tstate.attempts)

        return {
            "status": "fix",
            "counts": counts,
            "fix_path": fix_path,
            "rejected_path": rejected_path,
            "reason": "",
        }

    return {
        "status": "rejected-only",
        "counts": counts,
        "fix_path": "",
        "rejected_path": rejected_path,
        "reason": "",
    }
