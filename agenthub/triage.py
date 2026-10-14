import re
from dataclasses import dataclass


@dataclass
class ReviewFinding:
    id: str
    severity: str
    text: str


_FINDINGS_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s+|\*\*\s*)findings(?::\s*|\s*)(?:\([^)]*\))?(?:\s*\*\*:?)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SECTION_END = re.compile(r"^\s*(?:#{1,6}\s+|\*\*[^*]+\*\*:?\s*$)")
_BULLET_PREFIX = re.compile(r"^(\s*[-*]\s+)(.*)$")
_SEVERITY_PATTERN = re.compile(
    r"^(?:\*\*|`|\[|<)?(high|medium|low|critical|important|minor)(?::\*\*|:`|:\]|:>|\*\*|`|\]|>)?(?::)?$",
    re.IGNORECASE,
)
_SEVERITY_MAP = {
    "high": "high",
    "medium": "medium",
    "low": "low",
    "critical": "high",
    "important": "medium",
    "minor": "low",
}
_NONE_LIKE = re.compile(
    r"^\(?\s*(?:none|no\s+findings?|n/?a)\s*\)?\.?(?:\s*[:—–-].*)?$",
    re.IGNORECASE,
)


def _findings_lines(reply: str) -> list[str]:
    matches = list(_FINDINGS_HEADING.finditer(reply))
    if not matches:
        return []
    lines: list[str] = []
    for line in reply[matches[-1].end() :].splitlines():
        if _SECTION_END.match(line):
            break
        lines.append(line)
    return lines


def review_findings(reply: str) -> list[ReviewFinding]:
    """Extract findings from a review reply as numbered ReviewFinding records."""
    findings: list[ReviewFinding] = []
    current: ReviewFinding | None = None

    for line in _findings_lines(reply):
        bullet_match = _BULLET_PREFIX.match(line)
        if bullet_match:
            bullet_prefix = bullet_match.group(1)
            rest = bullet_match.group(2).strip()
            words = rest.split()
            first_word = words[0] if words else ""
            sev_match = _SEVERITY_PATTERN.match(first_word)
            if sev_match:
                sev_word = sev_match.group(1).lower()
                current = ReviewFinding(
                    id=f"F{len(findings) + 1}",
                    severity=_SEVERITY_MAP[sev_word],
                    text=rest,
                )
                findings.append(current)
            elif not bullet_prefix[0].isspace():
                current = None
            else:
                if current is not None:
                    current.text += f" {line.strip()}"
        else:
            stripped = line.strip()
            if stripped and current is not None:
                current.text += f" {stripped}"

    return findings


def unparsed_findings(reply: str) -> int:
    """The number of bullets in the findings section with indentation 0 that are not findings and not (none)-like
    text."""
    count = 0
    for line in _findings_lines(reply):
        bullet_match = _BULLET_PREFIX.match(line)
        if bullet_match:
            bullet_prefix = bullet_match.group(1)
            if not bullet_prefix[0].isspace():
                rest = bullet_match.group(2).strip()
                words = rest.split()
                first_word = words[0] if words else ""
                if _SEVERITY_PATTERN.match(first_word):
                    continue
                if _NONE_LIKE.match(rest):
                    continue
                count += 1
    return count
