import re
from dataclasses import dataclass, field


@dataclass
class ReviewFinding:
    id: str
    severity: str
    text: str
    head: str = field(default="", compare=False, repr=False)


_FINDINGS_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s+|\*\*\s*)findings(?::\s*|\s*)(?:\([^)]*\))?(?:\s*\*\*:?)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_HEADING_PREFIX = re.compile(r"^\s*(#{1,6})\s+")
_BOLD_SECTION_END = re.compile(r"^\*\*([^*]+)\*\*:?\s*$")
_SECTION_END_LABELS = frozenset({"verdict", "gate-touched", "notes", "summary"})
_BULLET_PREFIX = re.compile(r"^(\s*[-*]\s+)(.*)$")
_SEVERITY_PATTERN = re.compile(
    r"^(?:\*\*|`|\[|<)?(high|medium|low|critical|important|minor)(?::\*\*|:`|:\]|:>|\*\*|`|\]|>)?(?::)?$",
    re.IGNORECASE,
)
_SEVERITY_PREFIX_PATTERN = re.compile(
    r"^(?:\*\*|`)*severity:?(?:\*\*|`)*:?$",
    re.IGNORECASE,
)
_FINDING_SEPARATOR = re.compile(r"\s+(?:—|–|--)\s+")
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


_LEADING_SEP = re.compile(r"^(?:—|–|--|-|:)\s*")


def split_severity(line: str) -> tuple[str | None, str]:
    """Split the severity token(s) off a finding line.

    Returns the mapped severity (or None) and the rest of the line after the
    one or two severity words and any leading —, –, --, - or : plus spaces.
    """
    stripped = line.strip()
    words = stripped.split(maxsplit=2)
    if not words:
        return None, ""

    sev_match = _SEVERITY_PATTERN.match(words[0])
    if sev_match:
        sev = _SEVERITY_MAP[sev_match.group(1).lower()]
        parts = stripped.split(maxsplit=1)
        rest = parts[1] if len(parts) > 1 else ""
        return sev, _LEADING_SEP.sub("", rest)

    if len(words) > 1 and _SEVERITY_PREFIX_PATTERN.match(words[0]):
        sev_match2 = _SEVERITY_PATTERN.match(words[1])
        if sev_match2:
            sev = _SEVERITY_MAP[sev_match2.group(1).lower()]
            rest = words[2] if len(words) > 2 else ""
            return sev, _LEADING_SEP.sub("", rest)

    return None, stripped


def _extract_severity(words: list[str]) -> str | None:
    return split_severity(" ".join(words))[0]


def _findings_heading_level(heading_line: str) -> int:
    m = _HEADING_PREFIX.match(heading_line)
    if m:
        return len(m.group(1))
    return 2  # a bold **Findings** heading counts as level 2


def _is_section_end(line: str, findings_level: int) -> bool:
    heading_match = _HEADING_PREFIX.match(line)
    if heading_match:
        return len(heading_match.group(1)) <= findings_level

    if _FINDINGS_HEADING.match(line):
        return 2 <= findings_level

    if line.startswith("**"):
        bold_match = _BOLD_SECTION_END.match(line)
        if bold_match:
            inner = bold_match.group(1).strip()
            if inner.endswith(":"):
                inner = inner[:-1].strip()
            if inner.lower() in _SECTION_END_LABELS:
                return True

    return False


def _findings_lines(reply: str) -> list[str]:
    matches = list(_FINDINGS_HEADING.finditer(reply))
    if not matches:
        return []

    sections: list[list[str]] = []
    for i, match in enumerate(matches):
        findings_level = _findings_heading_level(match.group(0))
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(reply)
        section_text = reply[match.end() : next_start]
        lines: list[str] = []
        for line in section_text.splitlines():
            if _is_section_end(line, findings_level):
                break
            lines.append(line)
        sections.append(lines)

    for lines in reversed(sections):
        if any(_BULLET_PREFIX.match(line) for line in lines):
            return lines

    return sections[-1]


def review_findings(reply: str) -> list[ReviewFinding]:
    """Extract findings from a review reply as numbered ReviewFinding records."""
    findings: list[ReviewFinding] = []
    current: ReviewFinding | None = None

    for line in _findings_lines(reply):
        if _HEADING_PREFIX.match(line):
            current = None
            continue
        bullet_match = _BULLET_PREFIX.match(line)
        if bullet_match:
            bullet_prefix = bullet_match.group(1)
            rest = bullet_match.group(2).strip()
            sev, _ = split_severity(rest)
            if sev is not None:
                current = ReviewFinding(
                    id=f"F{len(findings) + 1}",
                    severity=sev,
                    text=rest,
                    head=rest,
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


def unparsed_findings(reply: str, *, any_shape: bool = False) -> int:
    """The number of bullets in the findings section with indentation 0 that are not findings and not (none)-like
    text."""
    count = 0
    for line in _findings_lines(reply):
        bullet_match = _BULLET_PREFIX.match(line)
        if bullet_match:
            bullet_prefix = bullet_match.group(1)
            if not bullet_prefix[0].isspace():
                rest = bullet_match.group(2).strip()
                if split_severity(rest)[0] is not None:
                    continue
                if _NONE_LIKE.match(rest):
                    continue
                if any_shape or len(_FINDING_SEPARATOR.findall(rest)) >= 2:
                    count += 1
    return count
