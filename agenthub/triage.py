import json
import re
from dataclasses import dataclass

from agenthub.plandraft import BLOCK_RE, OPEN_RE

SEVERITIES = ("high", "medium", "low")
DECISIONS = ("fix", "reject", "blocked")


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
