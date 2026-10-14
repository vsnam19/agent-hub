import re
from dataclasses import dataclass


@dataclass
class ReviewFinding:
    id: str
    severity: str
    text: str


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
