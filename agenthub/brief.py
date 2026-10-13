import re
import sys
from dataclasses import dataclass
from pathlib import Path

# `N. <rule> — enforced by: <mechanism>` (em dash or ` -- `); the mechanism is a named check or `none`.
ENFORCED_BY = re.compile(r"\s+(?:—|--)\s+enforced by:\s*(?P<mechanism>\S.*?)\s*$")
NUMBERED = re.compile(r"^\s*(?P<number>\d+)\.\s+(?P<text>\S.*)$")


class BriefError(Exception):
    pass


@dataclass(frozen=True)
class Order:
    number: int
    rule: str
    enforced_by: str | None  # None: the line has no `— enforced by:` suffix
    source: Path


def split_order(line: str) -> tuple[str, str | None]:
    """A standing-orders line as (rule text, mechanism); the mechanism is None when the suffix is missing."""
    match = ENFORCED_BY.search(line)
    if match is None:
        return line, None
    return line[: match.start()], match.group("mechanism")


def _sources(config_dir: Path, repo_root: Path | None) -> list[Path]:
    sources = [config_dir / "standing-orders.md"]
    if repo_root is not None:
        sources.append(repo_root / ".agent-hub" / "standing-orders.md")
    return sources


def _bodies(config_dir: Path, repo_root: Path | None) -> list[tuple[Path, str]]:
    """The non-empty standing-orders files, user first, each stripped."""
    bodies = []
    for source in _sources(config_dir, repo_root):
        if not source.is_file():
            continue
        try:
            body = source.read_text().strip()
        except (OSError, UnicodeDecodeError) as e:
            print(f"agent-hub: skipping standing orders {source}: {e}", file=sys.stderr)
            continue
        if body:
            bodies.append((source, body))
    return bodies


def standing_orders(config_dir: Path, repo_root: Path | None) -> str:
    """The `## Standing orders` section: user file, then project file; each only if non-empty after strip.

    The `— enforced by:` suffix is for our bookkeeping only, so it is stripped from every line sent.
    """
    bodies = []
    for _, body in _bodies(config_dir, repo_root):
        bodies.append("\n".join(split_order(line)[0] for line in body.splitlines()))

    if not bodies:
        return ""
    return "## Standing orders\n\n" + "\n\n".join(bodies)


def order_table(config_dir: Path, repo_root: Path | None) -> list[Order]:
    """Every numbered line of the standing-orders files, user file first; `enforced_by` is None when missing."""
    orders = []
    for source, body in _bodies(config_dir, repo_root):
        for line in body.splitlines():
            match = NUMBERED.match(line)
            if match is None:
                continue
            rule, enforced_by = split_order(match.group("text"))
            orders.append(Order(int(match.group("number")), rule.strip(), enforced_by, source))
    return orders


def validate(kind: str, text: str) -> None:
    if kind == "review":
        return
    if kind == "implement":
        required = ["Input", "Expected output"]
    elif kind == "plan":
        required = ["Goal", "Input"]
    else:
        raise BriefError(f"Unknown role: {kind}")

    # Headings: level >= 2 ('#' * n + space), text case-insensitive, trailing spaces/colon ignored.
    found = set()

    for line in text.splitlines():
        line = line.strip("\r\n")
        match = re.match(r"^(#{2,})\s+(.*)$", line)
        if match:
            found.add(match.group(2).strip(" \t:").lower())

    missing = [h for h in required if h.lower() not in found]
    if missing:
        raise BriefError(f"Brief missing headings: {', '.join(missing)}")


def plan_text(brief: str) -> str:
    preamble = (
        "You are planning work in the current directory. Read the files you need. This is a read-only job:\n"
        "do not modify, create or delete files, do not run commands, and do not fetch URLs.\n"
        "Write the plan in your final message; that message is the whole answer."
    )
    return f"{preamble}\n\n{brief}"


def review_text(base: str, brief: str | None, diff_path: Path) -> str:
    preamble = (
        f"You are reviewing the changes on the current branch against `{base}` in this repository. The changes\n"
        f"between `{base}` and HEAD are in the file `{diff_path}`: read it, and read the files it touches in the\n"
        f"current directory. Read only the diff file and files inside the current directory: paths outside them\n"
        f"(for example documents a README links to elsewhere on the machine) are not available, so note them as\n"
        f"unverified instead of opening them. URLs and the web are not available either: do not fetch them.\n"
        f"Do not modify any file. You have no shell: do not run commands\n"
        f"(git, tests, linters…);\n"
        f"use your file-reading tools only. Report findings as a list, most severe\n"
        f"first; for each give: severity (critical/important/minor), file:line, the problem, and a concrete failure\n"
        f'scenario. Say "no findings" if there are none.'
    )
    if brief and brief.strip():
        return f"{preamble}\n\n## Orchestrator notes\n\n{brief}"
    return preamble
