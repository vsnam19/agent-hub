import json
from pathlib import Path

from agenthub.plan import Plan
from agenthub.planstate import State, TaskState


def write_report(plan: Plan, state: State, reviews: dict[str, dict], rounds: int) -> Path:
    plan_dir = plan.path.parent if plan.path else Path(".")
    plan_dir.mkdir(parents=True, exist_ok=True)
    report_file = plan_dir / "report.md"

    lines: list[str] = [
        f"# Plan report: {plan.slug}",
        "",
        plan.goal.strip(),
        "",
        "## Tasks",
    ]

    for t in plan.tasks:
        if (t.kind or t.role) != "implement":
            continue
        tstate = state.tasks.get(t.name) or TaskState()
        rev = reviews.get(t.name) or {}
        review_val = rev.get("review") or "-"
        job_val = rev.get("job") or "-"
        lines.append(
            f"- {t.name}: {tstate.status}, fix rounds {tstate.fix_rounds}/{rounds}, "
            f"review {review_val}, review job {job_val}"
        )

    lines.append("")
    lines.append("## Findings")

    findings_lines: list[str] = []
    triage_path = plan_dir / "triage.jsonl"
    skipped = 0
    if triage_path.is_file():
        for raw_line in triage_path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except Exception:
                skipped += 1
                continue
            if not isinstance(entry, dict):
                skipped += 1
                continue
            findings = entry.get("findings")
            if not isinstance(findings, list):
                skipped += 1
                continue
            task_name = entry.get("task", "")
            round_num = entry.get("round", "")
            for f in findings:
                if not isinstance(f, dict):
                    skipped += 1
                    continue
                fid = f.get("id", "")
                sev = f.get("severity", "")
                decision = f.get("decision", "")
                evidence = f.get("evidence", "")
                findings_lines.append(f"- {task_name} round {round_num} {fid} ({sev}) {decision}: {evidence}")

    if skipped > 0:
        findings_lines.append(f"- triage.jsonl: {skipped} unreadable line(s) skipped")

    if findings_lines:
        lines.extend(findings_lines)
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("## Needs the director")

    director_lines: list[str] = []
    for t in plan.tasks:
        if (t.kind or t.role) != "implement":
            continue
        tstate = state.tasks.get(t.name) or TaskState()
        rev = reviews.get(t.name) or {}
        rev_review = rev.get("review")

        needs_director = tstate.status in ("blocked", "failed", "skipped", "scope-violation") or rev_review != "approve"
        if needs_director:
            if tstate.status in ("blocked", "failed", "skipped", "scope-violation"):
                reason = tstate.reason or rev.get("reason") or tstate.status
            else:
                reason = (
                    rev.get("reason") or tstate.reason or (f"review {rev_review}" if rev_review else "not approved")
                )
            reason_str = str(reason).replace("\n", " ").strip() if reason else "-"
            director_lines.append(f"- {t.name}: {reason_str}")

    if director_lines:
        lines.extend(director_lines)
    else:
        lines.append("(nothing)")

    report_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_file
