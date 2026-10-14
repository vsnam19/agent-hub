import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from agenthub import tasksize
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
                if split_severity(rest)[0] is not None:
                    continue
                if _NONE_LIKE.match(rest):
                    continue
                if len(_FINDING_SEPARATOR.findall(rest)) >= 2:
                    count += 1
    return count


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
