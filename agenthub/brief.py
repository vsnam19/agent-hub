import re


class BriefError(Exception):
    pass


def validate(role: str, text: str) -> None:
    if role == "review":
        return
    if role != "implement":
        raise BriefError(f"Unknown role: {role}")

    # Headings: level >= 2 ('#' * n + space), text case-insensitive, trailing spaces/colon ignored.
    has_input = False
    has_expected_output = False

    for line in text.splitlines():
        line = line.strip("\r\n")
        match = re.match(r"^(#{2,})\s+(.*)$", line)
        if match:
            heading_text = match.group(2).strip(" \t:")
            heading_lower = heading_text.lower()
            if heading_lower == "input":
                has_input = True
            elif heading_lower == "expected output":
                has_expected_output = True

    missing = []
    if not has_input:
        missing.append("Input")
    if not has_expected_output:
        missing.append("Expected output")

    if missing:
        raise BriefError(f"Brief missing headings: {', '.join(missing)}")


def review_text(base: str, brief: str | None) -> str:
    preamble = (
        f"You are reviewing the changes on the current branch against `{base}` in this repository. Read them with\n"
        f"`git diff {base}...HEAD` and the files involved. Do not modify any file. "
        f"Report findings as a list, most severe\n"
        f"first; for each give: severity (critical/important/minor), file:line, the problem, and a concrete failure\n"
        f'scenario. Say "no findings" if there are none.'
    )
    if brief and brief.strip():
        return f"{preamble}\n\n## Orchestrator notes\n\n{brief}"
    return preamble
