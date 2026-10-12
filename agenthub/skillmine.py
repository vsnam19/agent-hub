"""Deterministic extractor for the weekly skill run: what repeats in job records and transcripts, redacted."""

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CAP = 300
MAX_CORRECTIONS = 500
MAX_CORRECTION_CHARS = 600
CATEGORY_WORDS = 12

_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[\w.~+/=-]+")
_PREFIXED = re.compile(r"\b(?:sk-[\w-]{16,}|ghp_\w{20,}|xox[a-z]-[\w-]{10,}|AKIA[0-9A-Z]{16})")
_KV = re.compile(r"(?i)\b(\w*(?:key|token|secret|password|passwd))(\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s\"',;]+)")
_KV_JSON = re.compile(r"(?i)(\"\w*(?:key|token|secret|password|passwd)\"\s*:\s*)\"[^\"]*\"")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")
_B64 = re.compile(r"(?<![\w+/=-])[A-Za-z0-9+/]{40,}={0,2}(?![\w+/])")
_HOME = re.compile(r"/home/[^/\s]+")

_CORRECTION = re.compile(
    r"không được|\bphải\b|\bđừng\b|\bsai\b|\bdon[’']?t\b|\bnever\b|\bwrong\b|\bmust\b",
    re.I,
)
_FINDING = re.compile(r"^\s*[-*]\s+(high|medium|low)\s+[—–-]\s+(.+)$", re.I)
_DASH = re.compile(r"\s+[—–-]\s+")
_FIX_BRIEF = re.compile(r"review round \d+\s*(?:fix(?:es)?|—|–|-)", re.I)
_AGENT_HUB = re.compile(r"(?<![\w-])agent-hub\s+([a-z][\w-]*)")

_CAT_BACKTICK = re.compile(r"`[^`]*`")
_CAT_QUOTED = re.compile(r"\"[^\"]*\"|(?<!\w)'[^']*'(?!\w)")
_CAT_PATH = re.compile(
    r"[\w.~-]*(?:/[\w.~-]+)+|\b[\w-]+\.(?:py|md|json|toml|sh|ya?ml|txt|cfg|lock|jsonl|cpp|hpp|h|c|js|ts)\b"
)
_CAT_NUMBER = re.compile(r"\b\d+(?:\.\d+)*\b")


def redact(text: str) -> str:
    text = _PEM.sub("<redacted-key>", text)
    text = _JWT.sub("<redacted>", text)
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _PREFIXED.sub("<redacted>", text)
    text = _KV.sub(r"\1\2<redacted>", text)
    text = _KV_JSON.sub(r'\1"<redacted>"', text)
    text = _EMAIL.sub("<email>", text)
    text = _HEX.sub("<redacted>", text)
    text = _B64.sub(lambda m: "<redacted>" if _looks_random(m.group(0)) else m.group(0), text)
    return _HOME.sub("~", text)


def _looks_random(blob: str) -> bool:
    return any(c.isdigit() for c in blob) and any(c.isalpha() for c in blob)


def clean(text: str) -> str:
    return redact(str(text))[:CAP]


def category(claim: str) -> str:
    text = _CAT_BACKTICK.sub("<id>", claim.lower().replace("**", ""))
    text = _CAT_QUOTED.sub("<str>", text)
    text = _CAT_PATH.sub("<path>", text)
    text = _CAT_NUMBER.sub("<n>", text)
    return clean(" ".join(text.split()[:CATEGORY_WORDS]))


@dataclass
class Extract:
    since: str
    until: str
    findings: list[dict] = field(default_factory=list)
    fix_rounds: dict[str, dict] = field(default_factory=dict)
    corrections: list[dict] = field(default_factory=list)
    corrections_total: int = 0
    agenthub_commands: dict[str, dict] = field(default_factory=dict)
    outcomes: dict[str, dict] = field(default_factory=lambda: {"by_role": {}, "by_agent": {}})

    def to_json(self) -> dict:
        return asdict(self)


def mine(state_dir: Path, transcripts_dir: Path, since: datetime, now: datetime) -> Extract:
    extract = Extract(since=since.isoformat(), until=now.isoformat())
    _mine_jobs(Path(state_dir), _plan_branches(Path(state_dir)), extract, since)
    _mine_transcripts(Path(transcripts_dir), extract, since)
    return extract


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _plan_branches(state_dir: Path) -> dict[str, str]:
    """Worktree directory name `<slug>-<task>` -> branch `orch/<slug>/<task>`."""
    names: dict[str, str] = {}
    try:
        plan_files = sorted((state_dir / "plans").glob("*/plan.json"))
    except OSError:
        return names
    for plan_file in plan_files:
        plan = _read_json(plan_file)
        if not isinstance(plan, dict) or not isinstance(plan.get("slug"), str):
            continue
        for task in plan.get("tasks") or []:
            if isinstance(task, dict) and isinstance(task.get("name"), str):
                names[f"{plan['slug']}-{task['name']}"] = f"orch/{plan['slug']}/{task['name']}"
    return names


def _mine_jobs(state_dir: Path, branches: dict[str, str], extract: Extract, since: datetime) -> None:
    try:
        job_dirs = sorted(p for p in (state_dir / "jobs").iterdir() if p.is_dir())
    except OSError:
        return
    jobs = []
    for job_dir in job_dirs:
        result = _read_json(job_dir / "result.json")
        started = _parse_time(result.get("started")) if isinstance(result, dict) else None
        if started is None or started < since:
            continue
        jobs.append((started, job_dir, result))
    jobs.sort(key=lambda j: j[0])

    per_branch: dict[str, dict] = {}
    for _, job_dir, result in jobs:
        kind = result.get("kind") or result.get("role")
        role = clean(result.get("role") or "unknown")
        agent = clean(f"{result.get('agent')}:{result.get('model')}")
        status = clean(result.get("status") or "unknown")
        for table, key in (("by_role", role), ("by_agent", agent)):
            counts = extract.outcomes[table].setdefault(key, {})
            counts[status] = counts.get(status, 0) + 1

        workdir = result.get("workdir") if isinstance(result.get("workdir"), str) else ""
        dirname = Path(workdir).name
        branch = clean(branches.get(dirname, dirname) or "unknown")
        entry = per_branch.setdefault(branch, {"fix_briefs": 0, "reviews": 0, "verdicts": []})
        reply = _read_reply(job_dir)
        if kind == "review":
            entry["reviews"] += 1
            verdict = result.get("verdict") or _verdict_of(reply)
            if verdict:
                entry["verdicts"].append(clean(verdict))
            repo = Path(str(result.get("repo") or "")).parent.name if result.get("repo") else ""
            for severity, path, claim in _findings_of(reply):
                extract.findings.append(
                    {
                        "job": clean(job_dir.name),
                        "branch": branch,
                        "repo": clean(repo),
                        "severity": severity,
                        "file": clean(path),
                        "claim": clean(claim),
                        "category": category(claim),
                    }
                )
        elif _FIX_BRIEF.search(_read_text(job_dir / "brief.md").split("\n", 1)[0]):
            entry["fix_briefs"] += 1
    extract.fix_rounds = per_branch


def _read_reply(job_dir: Path) -> str:
    try:
        replies = sorted((job_dir / "attempts").glob("*/reply.md"))
    except OSError:
        return ""
    return _read_text(replies[-1]) if replies else ""


def _section(reply: str, title: str) -> list[str]:
    lines: list[str] = []
    inside = False
    for line in reply.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == title
            continue
        if inside:
            lines.append(line)
    return lines


def _verdict_of(reply: str) -> str | None:
    for line in _section(reply, "verdict"):
        word = line.strip().strip("*`").lower()
        if word in ("approve", "request-changes"):
            return word
    return None


def _findings_of(reply: str):
    for line in _section(reply, "findings"):
        match = _FINDING.match(line)
        if not match:
            continue
        parts = _DASH.split(match.group(2), maxsplit=1)
        if len(parts) != 2:
            continue
        where = re.match(r"[^\s:,]+", parts[0].strip().strip("`*"))
        yield match.group(1).lower(), where.group(0).strip("`") if where else "", parts[1].strip()


def _mine_transcripts(root: Path, extract: Extract, since: datetime) -> None:
    try:
        files = sorted(root.glob("*/*.jsonl"))
    except OSError:
        return
    sequences: dict[str, list[str]] = {}
    for path in files:
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since:
                continue
            with path.open(errors="replace") as handle:
                for line in handle:
                    _transcript_line(line, extract, since, sequences)
        except OSError:
            continue
    ngrams: Counter[str] = Counter()
    sessions: dict[str, set[str]] = {}
    for session, subs in sequences.items():
        for i in range(len(subs) - 2):
            gram = " > ".join(subs[i : i + 3])
            ngrams[gram] += 1
            sessions.setdefault(gram, set()).add(session)
    extract.agenthub_commands = {clean(g): {"count": n, "sessions": len(sessions[g])} for g, n in ngrams.most_common()}


def _transcript_line(line: str, extract: Extract, since: datetime, sequences: dict[str, list[str]]) -> None:
    try:
        record = json.loads(line)
    except ValueError:
        return
    if not isinstance(record, dict):
        return
    when = _parse_time(record.get("timestamp"))
    message = record.get("message")
    if when is None or when < since or not isinstance(message, dict):
        return
    content = message.get("content")
    session = str(record.get("sessionId") or "unknown")
    if record.get("type") == "user":
        if (
            isinstance(content, str)
            and not content.startswith("<")
            and record.get("isMeta") is not True
            and len(content) <= MAX_CORRECTION_CHARS
            and _CORRECTION.search(content)
        ):
            extract.corrections_total += 1
            if len(extract.corrections) < MAX_CORRECTIONS:
                extract.corrections.append(
                    {"session": clean(session), "time": when.isoformat(), "text": clean(content)}
                )
    elif record.get("type") == "assistant" and isinstance(content, list):
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash"):
                continue
            command = (block.get("input") or {}).get("command")
            if isinstance(command, str):
                sequences.setdefault(session, []).extend(_AGENT_HUB.findall(command))


def metrics(extract: Extract) -> dict:
    per_category = Counter(f["category"] for f in extract.findings)
    rounds = [b["reviews"] for b in extract.fix_rounds.values() if b["reviews"]]
    return {
        "findings_per_category": dict(per_category.most_common(20)),
        "mean_review_rounds": round(sum(rounds) / len(rounds), 2) if rounds else 0,
        "max_review_rounds": max(rounds, default=0),
        "fix_briefs_total": sum(b["fix_briefs"] for b in extract.fix_rounds.values()),
        "corrections_total": extract.corrections_total,
    }


def render_md(extract: Extract) -> str:
    lines = [f"# Skill mining extract ({extract.since[:10]} .. {extract.until[:10]})", ""]
    lines += [f"Findings: {len(extract.findings)}; corrections: {extract.corrections_total}", ""]
    lines.append("## Top finding categories")
    jobs: dict[str, list[str]] = {}
    for f in extract.findings:
        if f["job"] not in jobs.setdefault(f["category"], []):
            jobs[f["category"]].append(f["job"])
    top = Counter(f["category"] for f in extract.findings).most_common(10)
    lines += [f"- {n}× {cat} (e.g. {', '.join(jobs[cat][:3])})" for cat, n in top] or ["- (none)"]
    lines += ["", "## Branches with 2+ review rounds"]
    looped = sorted(
        ((b, d) for b, d in extract.fix_rounds.items() if d["reviews"] >= 2), key=lambda x: -x[1]["reviews"]
    )
    lines += [
        f"- {b}: {d['reviews']} reviews, {d['fix_briefs']} fix briefs ({' → '.join(d['verdicts'])})" for b, d in looped
    ] or ["- (none)"]
    lines += ["", "## Corrections"]
    lines += [f"- {c['text']}" for c in extract.corrections[:20]] or ["- (none)"]
    return "\n".join(lines) + "\n"


def write(extract: Extract, out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    summary = render_md(extract)
    (out / "extract.json").write_text(json.dumps(extract.to_json(), indent=2, ensure_ascii=False) + "\n")
    (out / "extract.md").write_text(summary)
    (out / "metrics.json").write_text(json.dumps(metrics(extract), indent=2, ensure_ascii=False) + "\n")
    return summary
