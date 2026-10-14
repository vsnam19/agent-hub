import json
import re
import string
from dataclasses import dataclass, field
from pathlib import Path

from agenthub import tasksize
from agenthub.plandraft import BLOCK_RE, OPEN_RE

SEVERITIES = ("high", "medium", "low")
DECISIONS = ("fix", "reject", "blocked")
TRIAGE_TEMPLATE = Path(__file__).parent / "templates" / "triage-brief.md"


class TriageError(Exception):
    pass


@dataclass
class ReviewFinding:
    id: str
    severity: str
    text: str


@dataclass
class TriageFinding:
    id: str
    severity: str
    decision: str
    evidence: str = ""


@dataclass
class Triage:
    findings: list[TriageFinding]
    fix_brief: str


@dataclass
class Resolution:
    findings: list[TriageFinding]
    fix_brief: str
    blocked_reason: str
    review_text: dict[str, str] = field(default_factory=dict)


def review_findings(reply: str) -> list[ReviewFinding]:
    """Extract findings from a review reply as numbered ReviewFinding records."""
    matches = list(re.finditer(r"^#{2,}\s+findings(?::)?\s*$", reply, re.IGNORECASE | re.MULTILINE))
    if not matches:
        return []

    findings: list[ReviewFinding] = []
    for line in reply[matches[-1].end() :].splitlines():
        if re.match(r"^#+\s", line):
            break
        if not line.startswith("- "):
            continue
        text = line.removeprefix("- ")
        words = text.split()
        if not words:
            continue
        severity = words[0].strip("<>*").lower()
        if severity not in ("high", "medium", "low"):
            continue

        findings.append(
            ReviewFinding(
                id=f"F{len(findings) + 1}",
                severity=severity,
                text=text,
            )
        )

    return findings


def parse(reply: str) -> Triage:
    blocks = BLOCK_RE.findall(reply)
    if len(blocks) < len(OPEN_RE.findall(reply)):
        raise TriageError("the ```json block is not closed")
    if not blocks:
        raise TriageError("triage reply has no ```json block")
    if len(blocks) > 1:
        raise TriageError(f"triage reply has {len(blocks)} ```json blocks, expected exactly one")

    try:
        data = json.loads(blocks[0])
    except json.JSONDecodeError as e:
        raise TriageError(f"triage json block is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise TriageError("triage json must be an object")

    for key in ("findings", "fix_brief"):
        if key not in data:
            raise TriageError(f"triage json missing field: {key}")
    if not isinstance(data["findings"], list):
        raise TriageError("triage findings must be a list")
    if not isinstance(data["fix_brief"], str):
        raise TriageError("triage fix_brief must be a string")

    findings = [_parse_finding(i, raw) for i, raw in enumerate(data["findings"])]
    return Triage(findings=findings, fix_brief=data["fix_brief"])


def _parse_finding(index: int, raw: object) -> TriageFinding:
    if not isinstance(raw, dict):
        raise TriageError(f"finding {index} must be an object")
    for key in ("id", "severity", "decision", "evidence"):
        if key not in raw:
            raise TriageError(f"finding {index} missing field: {key}")
    finding_id = raw["id"]
    if not isinstance(finding_id, str):
        raise TriageError(f"finding {index} id must be a string")
    severity = raw["severity"]
    if isinstance(severity, str):
        severity = severity.strip().lower()
    if severity not in SEVERITIES:
        raise TriageError(f"finding {index} has bad severity {severity!r}, expected one of {', '.join(SEVERITIES)}")
    decision = raw["decision"]
    if isinstance(decision, str):
        decision = decision.strip().lower()
    if decision not in DECISIONS:
        raise TriageError(f"finding {index} has bad decision {decision!r}, expected one of {', '.join(DECISIONS)}")
    evidence = raw["evidence"]
    if not isinstance(evidence, str):
        raise TriageError(f"finding {index} evidence must be a string")
    return TriageFinding(id=finding_id, severity=severity, decision=decision, evidence=evidence)


def _has_valid_reject_evidence(evidence: str, worktree: Path, job_ids: set[str]) -> bool:
    if not evidence:
        return False

    for job_id in job_ids:
        if not job_id:
            continue
        pattern = rf"(?<![A-Za-z0-9_-]){re.escape(job_id)}(?![A-Za-z0-9_-])"
        if re.search(pattern, evidence):
            return True

    for match in re.finditer(r"([^\s:()\[\]{}`'\"<>]+):(\d+)", evidence):
        try:
            line_num = int(match.group(2))
        except ValueError:
            continue
        if line_num < 1:
            continue

        candidate = Path(match.group(1))
        if ".." in candidate.parts:
            continue

        try:
            wt_resolved = worktree.resolve()
            if candidate.is_absolute():
                candidate_resolved = candidate.resolve()
                if not candidate_resolved.is_relative_to(wt_resolved):
                    continue
                target = candidate_resolved
            else:
                target = (worktree / candidate).resolve()
                if not target.is_relative_to(wt_resolved):
                    continue
            if target.is_file():
                try:
                    num_lines = len(target.read_text(encoding="utf-8").splitlines())
                except (OSError, UnicodeDecodeError, ValueError):
                    continue
                if 1 <= line_num <= num_lines:
                    return True
        except (OSError, ValueError):
            continue

    return False


def resolve(
    triage: Triage,
    reviewed: list[ReviewFinding],
    worktree: Path,
    job_ids: set[str],
    unparsed: int = 0,
) -> Resolution:
    worktree = Path(worktree)
    job_ids = set(job_ids)

    resolved_findings = [
        TriageFinding(
            id=f.id,
            severity=f.severity,
            decision="fix"
            if (f.decision == "reject" and not _has_valid_reject_evidence(f.evidence, worktree, job_ids))
            else f.decision,
            evidence=f.evidence,
        )
        for f in triage.findings
    ]

    triage_ids = {f.id for f in triage.findings}
    missing = sorted(
        {rf.id for rf in reviewed if rf.severity.lower() in ("medium", "high") and rf.id not in triage_ids}
    )
    reviewed_ids = {rf.id for rf in reviewed}
    unknown = sorted({f.id for f in triage.findings if f.id not in reviewed_ids}) if reviewed else []
    fix_ids = sorted({f.id for f in resolved_findings if f.decision == "fix"})

    if missing:
        blocked_reason = f"triage incomplete: {', '.join(missing)}"
    elif unknown:
        blocked_reason = f"triage lists unknown findings: {', '.join(unknown)}"
    elif unparsed > 0:
        blocked_reason = f"review findings unparsed: {unparsed}"
    elif any(f.decision == "blocked" for f in resolved_findings):
        first_blocked = next(f for f in resolved_findings if f.decision == "blocked")
        evidence = first_blocked.evidence.strip() or "(no reason given)"
        blocked_reason = f"blocked: {evidence}"
    elif fix_ids and not triage.fix_brief.strip():
        blocked_reason = f"fix needed for {', '.join(fix_ids)} but the triage wrote no fix brief"
    elif any(f.decision == "fix" for f in resolved_findings) or triage.fix_brief.strip():
        problems = tasksize.check(triage.fix_brief)
        if problems:
            blocked_reason = f"fix brief too big or malformed: {'; '.join(problems)}"
        else:
            blocked_reason = ""
    else:
        blocked_reason = ""

    return Resolution(
        findings=resolved_findings,
        fix_brief=triage.fix_brief,
        blocked_reason=blocked_reason,
        review_text={rf.id: rf.text for rf in reviewed},
    )


def _join_gates(gates: list[str]) -> str:
    """`a`, `b` and `c`: each gate backticked, joined the way the example brief joins its gates."""
    if not gates:
        return ""
    names = [f"`{gate}`" for gate in gates]
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _format_findings(findings: list[ReviewFinding]) -> str:
    if not findings:
        return "(none)"
    return "\n".join(f"{f.id} ({f.severity}): {f.text}" for f in findings)


def _format_gate_tail(gate_tail: str) -> str:
    if not gate_tail or not gate_tail.strip():
        return ""
    return f"\n### Gate failure tail\n\n```\n{gate_tail.strip()}\n```\n"


def _format_answered(answered: str) -> str:
    if not answered or not answered.strip():
        return ""
    return f"\n### Already-answered rejected findings\n\n{answered.strip()}\n"


def triage_brief(
    task_brief: str,
    findings: list[ReviewFinding],
    review_reply: str,
    gate_tail: str,
    diff_path: str,
    worktree: str,
    gates: list[str],
    answered: str = "",
) -> str:
    """Render the triage job brief from the template."""
    template = string.Template(TRIAGE_TEMPLATE.read_text(encoding="utf-8"))
    return template.substitute(
        task_brief=task_brief,
        findings=_format_findings(findings),
        review_reply=review_reply,
        gate_tail=_format_gate_tail(gate_tail),
        diff_path=diff_path,
        worktree=worktree,
        gates=_join_gates(gates),
        answered=_format_answered(answered),
        max_files=tasksize.MAX_FILES,
        max_deliverables=tasksize.MAX_DELIVERABLES,
        max_tests=tasksize.MAX_TESTS,
    )
