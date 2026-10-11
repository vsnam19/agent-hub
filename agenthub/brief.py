import re
import sys
from pathlib import Path


class BriefError(Exception):
    pass


def standing_orders(config_dir: Path, repo_root: Path | None) -> str:
    """The `## Standing orders` section: user file, then project file; each only if non-empty after strip."""
    sources = [config_dir / "standing-orders.md"]
    if repo_root is not None:
        sources.append(repo_root / ".agent-hub" / "standing-orders.md")

    bodies = []
    for source in sources:
        if not source.is_file():
            continue
        try:
            body = source.read_text().strip()
        except (OSError, UnicodeDecodeError) as e:
            print(f"agent-hub: skipping standing orders {source}: {e}", file=sys.stderr)
            continue
        if body:
            bodies.append(body)

    if not bodies:
        return ""
    return "## Standing orders\n\n" + "\n\n".join(bodies)


def validate(kind: str, text: str) -> None:
    if kind == "review":
        return
    if kind != "implement":
        raise BriefError(f"Unknown role: {kind}")

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


def review_text(base: str, brief: str | None, diff_path: Path) -> str:
    preamble = (
        f"You are reviewing the changes on the current branch against `{base}` in this repository. The changes\n"
        f"between `{base}` and HEAD are in the file `{diff_path}`: read it, and read the files it touches in the\n"
        f"current directory. Do not modify any file. Report findings as a list, most severe\n"
        f"first; for each give: severity (critical/important/minor), file:line, the problem, and a concrete failure\n"
        f'scenario. Say "no findings" if there are none.'
    )
    if brief and brief.strip():
        return f"{preamble}\n\n## Orchestrator notes\n\n{brief}"
    return preamble
