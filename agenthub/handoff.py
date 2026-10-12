import re

FOOTER = """---
If you cannot meet the brief's checks, stop: do not weaken a test, a gate or the brief's conditions to get green.
Report Status "blocked" and say exactly what stopped you.

When you are done, end with this handoff as your final message, exactly these headings:

## Status
success | partial | blocked

## What I did
- <one line per change>

## Verification
unit-test-verified | type-check-only | not-verified | inconclusive

## Notes
- <assumptions, surprises, anything the orchestrator must know; "(none)" if nothing>

## Follow-ups
- <tasks you suggest; "(none)" if nothing>
"""

REVIEW_FOOTER = (
    """---
Your final message is the only part of your answer that is kept, so put the whole review in it:

## Findings
- <severity high|medium|low> — <path:line> — <what is wrong> — <a concrete failure scenario> — """
    """<evidence: what you read or ran>
(or "(none)")

## Verdict
approve | request-changes

Use request-changes only for a high or medium finding.
"""
)


def parse_verdict(reply: str) -> str | None:
    """The word under the last `## Verdict` heading: "approve" or "request-changes"; None when absent or unknown."""
    matches = list(re.finditer(r"^#{2,}\s+verdict\s*$", reply, re.IGNORECASE | re.MULTILINE))
    if not matches:
        return None
    for line in reply[matches[-1].end() :].splitlines():
        line = line.strip()
        if line:
            word = line.split()[0].lower().rstrip(".,;:'\"!?")
            return word if word in ("approve", "request-changes") else None
    return None


def parse_touched_entries(reply: str) -> list[dict]:
    """The lines under the last `## Gate-touched` heading as {file, kind, judgement, reason}; bad lines are skipped."""
    matches = list(re.finditer(r"^#{2,}\s+gate-touched\s*$", reply, re.IGNORECASE | re.MULTILINE))
    if not matches:
        return []
    entries = []
    for line in reply[matches[-1].end() :].splitlines():
        if re.match(r"^#{2,}\s", line):
            break
        parts = re.split(r"\s+[—–]\s+", line.strip().removeprefix("- ").removeprefix("* ").strip("`"), maxsplit=2)
        if len(parts) < 2:
            continue
        target, judgement = parts[0].split(), parts[1].strip().lower().rstrip(".,;:")
        if len(target) != 2 or judgement not in ("legitimate", "weakening"):
            continue
        entries.append(
            {
                "file": target[0],
                "kind": target[1],
                "judgement": judgement,
                "reason": parts[2].strip() if len(parts) > 2 else "",
            }
        )
    return entries


def parse_touched_review(reply: str) -> dict[tuple[str, str], str]:
    """The reviewer's judgement per (file, kind) under `## Gate-touched`; weakening wins over legitimate."""
    out: dict[tuple[str, str], str] = {}
    for e in parse_touched_entries(reply):
        key = (e["file"], e["kind"])
        if out.get(key) != "weakening":
            out[key] = e["judgement"]
    return out


def parse(reply: str) -> dict | None:
    # find the last ## Status heading, case-insensitively, any level >= 2
    # e.g., ## status, ### Status
    matches = list(re.finditer(r"^(#{2,})\s+status\s*$", reply, re.IGNORECASE | re.MULTILINE))
    if not matches:
        return None

    last_match = matches[-1]
    text_to_parse = reply[last_match.start() :]

    # parse sections
    # valid headings
    heading_pattern = re.compile(r"^(#{2,})\s+(.*)$", re.MULTILINE)
    sections = {}
    current_section = None

    for line in text_to_parse.split("\n"):
        m = heading_pattern.match(line)
        if m:
            heading = m.group(2).strip().lower()
            current_section = heading
            sections[current_section] = []
        elif current_section:
            sections[current_section].append(line)

    # extract status
    status_lines = sections.get("status", [])
    status = ""
    for line in status_lines:
        line = line.strip()
        if line:
            status = line.split()[0].lower().rstrip(".,;:'\"!?")
            break

    if status not in ("success", "partial", "blocked"):
        return None

    # extract verification
    verification_lines = sections.get("verification", [])
    verification = "not-verified"
    for line in verification_lines:
        line = line.strip()
        if line:
            word = line.split()[0].lower().rstrip(".,;:'\"!?")
            if word in ("unit-test-verified", "type-check-only", "not-verified", "inconclusive"):
                verification = word
            break

    # helper for lists
    def parse_list(key):
        items = []
        for line in sections.get(key, []):
            line = line.strip()
            if not line:
                continue
            if line == "(none)":
                continue
            if line.startswith("- "):
                items.append(line[2:])
            elif line.startswith("* "):
                items.append(line[2:])
        return items

    did = parse_list("what i did")
    notes = parse_list("notes")
    followups = parse_list("follow-ups")

    return {"status": status, "verification": verification, "did": did, "notes": notes, "followups": followups}
