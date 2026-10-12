"""Deterministic extractor for the weekly skill run: what repeats in job records and transcripts, redacted."""

import json
import math
import re
import string
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
_BASIC = re.compile(r"\bBasic\s+([A-Za-z0-9+/_-]{8,}={0,2})(?![\w+/])")
# the password is capped at 256 chars: uncapped, every `scheme://` in a long run of them rescans to the run's end
_URL_CREDS = re.compile(r"((?<![\w+.-])[a-z][\w+.-]*://[^\s/:@]*):\S{1,256}@", re.I)
_PREFIXED = re.compile(r"\b(?:sk-[\w-]{16,}|ghp_\w{20,}|xox[a-z]-[\w-]{10,}|AKIA[0-9A-Z]{16})")
# a secret name contains one of `_LONG_WORDS` (`PGPASSWORD`, `SECRETKEY`, `api_key`), or a part (split on `_`, `-` or a
# camelCase hump) equals or ends with one of `_PART_WORDS` (`DB_PASS`, `apiKey`, `adminpass`). A `key` ending needs four
# letters before it, so `monkey` stays a word; `Design`, `author`, `keyboard`, `signal` and `passage` do not match
_LONG_WORDS = "password|passwd|passphrase|secret|token|credential|apikey|api_key|privatekey|private_key"
_PART_WORDS = "pass|pwd|auth|authorization|cookie|session|salt|sig|signature"
_PART_START = r"(?:(?<![\w-])|(?<=[_-])|(?<=(?-i:[a-z0-9]))(?=(?-i:[A-Z])))"
_PART_END = r"(?=[_-]|(?![\w-])|(?<=(?-i:[a-z0-9]))(?=(?-i:[A-Z])))"
_SECRET_NAME = (
    rf"(?=[\w-]*?(?:{_LONG_WORDS})"
    rf"|[\w-]*?(?:{_PART_WORDS}){_PART_END}"
    rf"|[\w-]*?(?:[a-z]{{4}}|{_PART_START})key{_PART_END})"
)
_NAME = rf"{_SECRET_NAME}[\w-]+"
_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s\"',;]+)"
_KV = re.compile(rf"(?i)(?<![\w-])({_NAME})(\s*[=:]\s*){_VALUE}")
_KV_SPACE = re.compile(rf"(?i)(?<![\w-])({_NAME})(\s+)(?!-|<redacted>){_VALUE}")
_KV_JSON = re.compile(rf"(?i)(\"{_NAME}\"\s*:\s*)\"[^\"]*\"")
_BARE_SECRET_WORDS = {"password", "passwd", "pwd", "passphrase"}
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HOME = re.compile(r"(?:^|(?<=[\s\"'`(=:/\[]))(?:/home/[^/\s]+|/Users/[^/\s]+|/root(?![\w.-]))")

# Token rule. Text is split on whitespace, quotes, backticks and `=:,;()<>[]{}?&`; each token is then split on
# `. / @ # *` (Markdown, paths, extensions, `...`) and only a piece is redacted, when ALL hold:
#   - >= 16 chars: shorter strings are mostly words or short ids, and name rules cover short passwords;
#   - >= 2 of {lower, upper, digit}: a single-class string is a word or a number (`_-+` do not count);
#   - Shannon entropy >= 3.0 bits/char: random keys sit at 4+, repeated or patterned text well below;
#   - not an identifier: a piece split on `_`, `-` or `+` whose parts are all words (letters only, each upper-case or
#     camel/lower with >= 3 letter pieces, or a single letter) or digit groups of <= 8 digits (`FastAPI+SQLite+React`,
#     `2026-10-11-model-routing`, `phase-b`), and not a job id (`YYYYMMDD-HHMMSS-role-xxxx`, role = one or more
#     lower-case words joined by `-`): names the extract needs to keep.
_TOKEN = re.compile(r"[^\s\"'`=:,;()<>\[\]{}?&]+")
_PIECE_SPLIT = re.compile(r"([./@#*]+)")
_REDACTED_RUN = re.compile(r"<redacted>(?:[./@#*]+<redacted>)+")
_TOKEN_MIN_LEN = 16
_CONTINUATION_MIN_LEN = 8
_TOKEN_MIN_ENTROPY = 3.0
_DIGIT_GROUP_MAX = 8
_PART_SPLIT = re.compile(r"[_+-]+")
# a readable snake chunk: a word of >= 2 letters with a short digit run (`sha256`, `base64url`, `uuid4`), a letter and
# digits (`t1`), digits and a word (`24h`), or a plain number; random chunks like `k3f9x2m8q7z1` match none of these
_SNAKE_CHUNK = re.compile(r"[a-z]{2,}(?:\d{1,3}[a-z]*)?|[a-z]\d{1,3}|\d{1,3}[a-z]+|\d{1,8}")
_SNAKE_CHUNK_MAX = 12
_ASCII_RUN = re.compile(r"[\x00-\x7f]+")
_WORD_PIECE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
_IDENT_WORD = re.compile(r"(?:[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]{3,})+")
_JOB_ID = re.compile(r"\d{8}-\d{6}-[a-z]+(?:-[a-z]+)*-[0-9a-f]{4}(?:\.\w+)?")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

_CORRECTION = re.compile(
    r"không được|\bphải\b|\bđừng\b|\bsai\b|\bdon[’']?t\b|\bnever\b|\bwrong\b|\bmust\b",
    re.I,
)
_FINDING = re.compile(
    r"^\s*[-*]\s+(?:\*\*|`)?(high|medium|low|critical|important|minor)(?:\*\*|`)?\s+(?:—|–|--|-)\s+(.+)$", re.I
)
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_SEVERITY = {"critical": "high", "important": "medium", "minor": "low"}
_BULLET = re.compile(r"^\s*[-*]\s")
_DASH = re.compile(r"\s+(?:—|–|--|-)\s+")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_FIX_BRIEF = re.compile(r"\bround\s+\d+\s*(?::|fix(?:es)?\b|—|–|-)", re.I)
_AGENT_HUB = re.compile(r"(?<![\w-])agent-hub\s+([a-z][\w-]*)")

_CAT_BACKTICK = re.compile(r"`[^`]*`")
_CAT_QUOTED = re.compile(r"\"[^\"]*\"|(?<!\w)'[^']*'(?!\w)")
_CAT_PATH = re.compile(
    r"[\w.~-]*(?:/[\w.~-]+)+|\b[\w-]+\.(?:py|md|json|toml|sh|ya?ml|txt|cfg|lock|jsonl|cpp|hpp|h|c|js|ts)\b"
)
_CAT_NUMBER = re.compile(r"\b\d+(?:\.\d+)*\b")


def _is_word(text: str) -> bool:
    """Whether `text` is a camel-case word run: capital runs and lowercase runs of 3+ letters, nothing else.

    Splitting into pieces in one linear pass keeps it fast; a nested `(?:...)+` pattern backtracks 2^(n-1) ways over
    a run of n capitals.
    """
    pieces = _WORD_PIECE.findall(text)
    return (
        bool(pieces)
        and "".join(pieces) == text
        and all(p.isupper() or len(p.lstrip(string.ascii_uppercase)) >= 3 for p in pieces)
    )


def redact(text: str) -> str:
    text = _PEM.sub("<redacted-key>", text)
    text = _JWT.sub("<redacted>", text)
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _BASIC.sub(lambda m: m.group(0) if _is_word(m.group(1)) else "Basic <redacted>", text)
    text = _URL_CREDS.sub(r"\1:<redacted>@", text)
    text = _PREFIXED.sub("<redacted>", text)
    text = _KV.sub(r"\1\2<redacted>", text)
    text = _KV_SPACE.sub(_redact_space_value, text)
    text = _KV_JSON.sub(r'\1"<redacted>"', text)
    text = _EMAIL.sub("<email>", text)
    text = _TOKEN.sub(lambda m: _redact_token(m.group(0)), text)
    return _HOME.sub("~", text)


def _redact_space_value(match: re.Match) -> str:
    """`name value`: only for flags, identifier-like names and bare password words, so prose stays readable."""
    name = match.group(1)
    value = match.group(0)[len(name) + len(match.group(2)) :]
    flag = name.startswith("--") or name.lower() in _BARE_SECRET_WORDS
    identifier = ("_" in name or re.search(r"[a-z][A-Z]", name)) and not value.isalpha()
    if flag or identifier:
        return f"{name}{match.group(2)}<redacted>"
    return match.group(0)


def _entropy(token: str) -> float:
    counts = Counter(token)
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def _redact_token(token: str) -> str:
    parts = _PIECE_SPLIT.split(token)
    secret = [i % 2 == 0 and _looks_secret(part) for i, part in enumerate(parts)]
    # a base64 blob split by `/`: a mixed, non-word piece right next to a secret piece is part of it
    for i in range(0, len(parts), 2):
        near = (i >= 2 and secret[i - 2] and parts[i - 1] == "/") or (
            i + 2 < len(parts) and secret[i + 2] and parts[i + 1] == "/"
        )
        if near and not secret[i] and _looks_secret(parts[i], _CONTINUATION_MIN_LEN):
            secret[i] = True
    joined = "".join("<redacted>" if secret[i] else part for i, part in enumerate(parts))
    return _REDACTED_RUN.sub("<redacted>", joined)


def _looks_secret(piece: str, min_len: int = _TOKEN_MIN_LEN) -> bool:
    if not piece.isascii():
        # only the ASCII runs are judged: a symbol or accented letter must not hide an identifier, nor a secret
        return any(_looks_secret(run, min_len) for run in _ASCII_RUN.findall(piece))
    if len(piece) < min_len or _snake_identifier(piece):
        return False
    classes = (any(c.islower() for c in piece), any(c.isupper() for c in piece), any(c.isdigit() for c in piece))
    if sum(classes) < 2 or _entropy(piece) < _TOKEN_MIN_ENTROPY or _JOB_ID.fullmatch(piece):
        return False
    parts = [part for part in _PART_SPLIT.split(piece) if part]
    return not all(_identifier_part(part) for part in parts)


def _snake_identifier(piece: str) -> bool:
    parts = piece.split("_")
    return len(parts) >= 3 and all(len(part) <= _SNAKE_CHUNK_MAX and _SNAKE_CHUNK.fullmatch(part) for part in parts)


def _identifier_part(part: str) -> bool:
    if part.isdigit():
        return len(part) <= _DIGIT_GROUP_MAX
    return part.isalpha() and (len(part) == 1 or _IDENT_WORD.fullmatch(part) is not None)


def clean(text: str) -> str:
    return redact(str(text))[:CAP]


def category(claim: str) -> str:
    text = _CAT_BACKTICK.sub("<id>", redact(claim).lower().replace("**", ""))
    text = _CAT_QUOTED.sub("<str>", text)
    text = _CAT_PATH.sub("<path>", text)
    text = _CAT_NUMBER.sub("<n>", text)
    return clean(" ".join(text.split()[:CATEGORY_WORDS]))


def _redact_all(value):
    """The one choke point: every string leaving the extract, keys included, is redacted once more."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_all(v) for v in value]
    if isinstance(value, dict):
        return {redact(k) if isinstance(k, str) else k: _redact_all(v) for k, v in value.items()}
    return value


@dataclass
class Extract:
    since: str
    until: str
    findings: list[dict] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)
    review_failures: int = 0
    unparsed_findings: int = 0
    fix_rounds: dict[str, dict] = field(default_factory=dict)
    corrections: list[dict] = field(default_factory=list)
    corrections_total: int = 0
    agenthub_commands: dict[str, dict] = field(default_factory=dict)
    outcomes: dict[str, dict] = field(default_factory=lambda: {"by_role": {}, "by_agent": {}})

    def to_json(self) -> dict:
        return _redact_all(asdict(self))


def mine(state_dir: Path, transcripts_dir: Path, since: datetime, now: datetime) -> Extract:
    extract = Extract(since=since.isoformat(), until=now.isoformat())
    _mine_jobs(Path(state_dir), _plan_branches(Path(state_dir)), extract, since)
    _mine_transcripts(Path(transcripts_dir), extract, since)
    extract.groups = group_findings(extract.findings)
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
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
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
        dirname = _dir_label(workdir)
        branch = clean(branches.get(dirname, dirname) or "unknown")
        entry = per_branch.setdefault(branch, {"fix_briefs": 0, "fix_brief_titles": [], "reviews": 0, "verdicts": []})
        reply = _read_reply(job_dir, result)
        if kind == "review" and result.get("status") != "ok":
            extract.review_failures += 1
        elif kind == "review":
            entry["reviews"] += 1
            verdict = result.get("verdict") or _verdict_of(reply)
            if verdict:
                entry["verdicts"].append(clean(verdict))
            repo = _repo_name(str(result.get("repo") or "")) if result.get("repo") else ""
            found, unparsed = _findings_of(reply)
            extract.unparsed_findings += unparsed
            for severity, path, claim in found:
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
        else:
            title = _first_line(_read_text(job_dir / "brief.md"))
            if _FIX_BRIEF.search(title):
                entry["fix_briefs"] += 1
                entry["fix_brief_titles"].append(clean(title))
    extract.fix_rounds = per_branch


def _dir_label(path: str) -> str:
    """Directory name; `~` at a home directory, so a user name is not taken for a repo or branch name."""
    directory = Path(path)
    return "~" if _HOME.fullmatch(str(directory)) else directory.name


def _repo_name(git_dir: str) -> str:
    return _dir_label(str(Path(git_dir).parent))


def _first_line(text: str) -> str:
    return next((line for line in text.splitlines() if line.strip()), "")


def module_of(path: str) -> str:
    return "/".join([part for part in path.split("/") if part and part != "."][:2])


def group_findings(findings: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for f in findings:
        grouped.setdefault((f["severity"], module_of(f["file"])), []).append(f)
    groups = []
    for (severity, module), items in grouped.items():
        examples = list(dict.fromkeys(f["claim"] for f in items))[:3]
        groups.append(
            {
                "severity": severity,
                "module": module,
                "count": len(items),
                "branches": len({f["branch"] for f in items}),
                "examples": examples,
            }
        )
    groups.sort(key=lambda g: (-g["count"], _SEVERITY_RANK.get(g["severity"], len(_SEVERITY_RANK)), g["module"]))
    return groups


def _read_reply(job_dir: Path, result: dict | None = None) -> str:
    reply_path = result.get("reply_path") if isinstance(result, dict) else None
    if isinstance(reply_path, str) and reply_path:
        try:
            path = Path(reply_path).resolve()
            if path.is_file() and path.is_relative_to(job_dir.resolve()):
                return _read_text(path)
        except (OSError, ValueError):
            pass
    try:
        replies = [p for p in (job_dir / "attempts").glob("*/reply.md") if p.parent.name.split("-")[0].isdigit()]
    except OSError:
        return ""
    replies.sort(key=lambda p: int(p.parent.name.split("-")[0]))
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


def _findings_of(reply: str) -> tuple[list[tuple[str, str, str]], int]:
    found: list[tuple[str, str, str]] = []
    unparsed = 0
    for line in _section(reply, "findings"):
        match = _FINDING.match(line)
        parts = _DASH.split(match.group(2), maxsplit=1) if match else []
        if len(parts) != 2:
            unparsed += bool(_BULLET.match(line))
            continue
        label = parts[0].strip().strip("`*")
        link = _LINK.match(label)
        if link:
            label = link.group(1).strip().strip("`*")
        where = re.match(r"[^\s:,]+", label)
        severity = match.group(1).lower()
        found.append((_SEVERITY.get(severity, severity), where.group(0).strip("`") if where else "", parts[1].strip()))
    return found, unparsed


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
            with path.open(encoding="utf-8", errors="replace") as handle:
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
    # two n-grams can clean to the same key (secret-looking command names): their counts and sessions are merged
    merged: dict[str, dict] = {}
    for gram, n in ngrams.most_common():
        entry = merged.setdefault(clean(gram), {"count": 0, "sessions": set()})
        entry["count"] += n
        entry["sessions"] |= sessions[gram]
    extract.agenthub_commands = {g: {"count": e["count"], "sessions": len(e["sessions"])} for g, e in merged.items()}


def _session_label(session: str) -> str:
    """A session UUID is not a secret, but the token rule would redact it; its first 8 hex digits still group."""
    return session[:8] if _UUID.fullmatch(session) else session


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
    session = _session_label(str(record.get("sessionId") or "unknown"))
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
            tool_input = block.get("input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if isinstance(command, str):
                sequences.setdefault(session, []).extend(_AGENT_HUB.findall(command))


def metrics(extract: Extract) -> dict:
    per_category = Counter(f["category"] for f in extract.findings)
    rounds = [b["reviews"] for b in extract.fix_rounds.values() if b["reviews"]]
    return {
        "findings_per_category": dict(per_category.most_common(20)),
        "mean_review_rounds": round(sum(rounds) / len(rounds), 2) if rounds else 0,
        "max_review_rounds": max(rounds, default=0),
        "review_failures": extract.review_failures,
        "unparsed_findings": extract.unparsed_findings,
        "fix_briefs_total": sum(b["fix_briefs"] for b in extract.fix_rounds.values()),
        "corrections_total": extract.corrections_total,
    }


def render_md(extract: Extract) -> str:
    lines = [f"# Skill mining extract ({extract.since[:10]} .. {extract.until[:10]})", ""]
    lines += [f"Findings: {len(extract.findings)}; corrections: {extract.corrections_total}", ""]
    lines.append("## Top finding groups (severity, module)")
    lines += [
        f"- {g['count']}× {g['severity']} in {g['module']} ({g['branches']} branches): " + "; ".join(g["examples"])
        for g in [g for g in extract.groups if g["count"] >= 2][:10]
    ] or ["- (none)"]
    lines += ["", "## Branches with 2+ review rounds"]
    looped = sorted(
        ((b, d) for b, d in extract.fix_rounds.items() if d["reviews"] >= 2), key=lambda x: -x[1]["reviews"]
    )
    lines += [
        f"- {b}: {d['reviews']} reviews, {d['fix_briefs']} fix briefs ({' → '.join(d['verdicts'])})" for b, d in looped
    ] or ["- (none)"]
    for b, d in looped:
        lines += [f"  - {b}: {t}" for t in d["fix_brief_titles"]]
    lines += ["", "## Corrections"]
    lines += [f"- {c['text']}" for c in extract.corrections[:20]] or ["- (none)"]
    return "\n".join(lines) + "\n"


def write(extract: Extract, out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    summary = redact(render_md(extract))
    dump = json.dumps(extract.to_json(), indent=2, ensure_ascii=False)
    (out / "extract.json").write_text(dump + "\n", encoding="utf-8")
    (out / "extract.md").write_text(summary, encoding="utf-8")
    stats = json.dumps(_redact_all(metrics(extract)), indent=2, ensure_ascii=False)
    (out / "metrics.json").write_text(stats + "\n", encoding="utf-8")
    return summary
