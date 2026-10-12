"""The run loop: walk a role's chain, attempt, classify, fall back, write the result record."""

import dataclasses
import fnmatch
import json
import os
import signal
import subprocess
import threading
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from agenthub import brief as briefmod
from agenthub import env, gateguard, guards, handoff, process
from agenthub.agents import Classified, Outcome, Request, registry
from agenthub.config import ConfigError, Paths, Pool
from agenthub.jobs import Job, list_jobs, new_job
from agenthub.pool import Cooldowns, Link
from agenthub.procinfo import start_time
from agenthub.workers import worker_plugins

EXIT = {
    "ok": 0,
    "failed": 1,
    "refused": 3,
    "exhausted": 4,
    "modified-tree": 5,
    "timeout": 6,
    "empty": 7,
    "interrupted": 8,
    "gate-failed": 9,
    "scope-violation": 10,
    "cancelled": 130,
}

GATE_TIMEOUT_S = 600
REPO_CONFIG = Path(".agent-hub") / "config.toml"

FALLBACK = (Outcome.QUOTA, Outcome.AUTH, Outcome.MODEL_UNAVAILABLE)
TERMINAL = {Outcome.EMPTY: "empty", Outcome.TIMEOUT: "timeout", Outcome.FAILED: "failed"}


@dataclass
class RunArgs:
    role: str
    brief: str
    workdir: Path
    links: list[Link]
    timeout: timedelta
    base: str | None
    gates: list[str] = dataclasses.field(default_factory=list)
    jev: bool = False
    kind: str = ""
    exclude: list[str] = dataclasses.field(default_factory=list)  # agent:model links to skip (e.g. the implementer)
    allow: list[str] = dataclasses.field(default_factory=list)  # repo-relative globs the run may change


def repo_gates(root: Path) -> list[str]:
    """Default gates from `<repo root>/.agent-hub/config.toml`; none when the file is absent."""
    path = root / REPO_CONFIG
    if not path.is_file():
        return []
    return _parse_gates(path.read_text(), path)


def base_repo_gates(repo: guards.Repo, since: str) -> tuple[list[str], str]:
    """Default gates as committed at `since` (the merge base with the base branch), so a HEAD that edits its own
    gate config cannot change the gates it is judged by. Falls back to the work tree's config when the base has none.
    Returns the gates and their source: the merge base sha, or "worktree"."""
    spec = f"{since}:{REPO_CONFIG.as_posix()}"
    try:
        text = guards._run_git(repo.root, "show", spec)
    except subprocess.CalledProcessError:
        text = None
    if text is not None:
        gates = _parse_gates(text, spec)
        if gates:
            return gates, since
    return repo_gates(repo.root), "worktree"


def base_repo_config(repo: guards.Repo, base: str) -> str:
    """Repo config text as committed at `base`, so a review's branch cannot choose its own worker plugins.
    Empty (no override) when the base has no config file."""
    try:
        return guards._run_git(repo.root, "show", f"{base}:{REPO_CONFIG.as_posix()}")
    except subprocess.CalledProcessError:
        return ""


def _parse_gates(text: str, where: object) -> list[str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{where}: {e}") from e
    gates = data.get("gates", [])
    if not isinstance(gates, list) or not all(isinstance(g, str) for g in gates):
        raise ConfigError(f"{where}: gates must be a list of strings")
    return gates


def merge_gates(repo: list[str], cli: list[str]) -> list[str]:
    """Repo gates first, then the command-line ones; a command in both runs once, at its first place."""
    merged: list[str] = []
    for cmd in [*repo, *cli]:
        if cmd not in merged:
            merged.append(cmd)
    return merged


def run_gates(
    gates: list[str], workdir: Path, gate_dir: Path, sha: str, cancel: "_Cancel | None" = None
) -> tuple[list[dict], bool]:
    """Run gate commands in order in `workdir`, logs under `gate_dir`, stopping at the first failure or once `cancel`
    has been signalled. Returns the gate records, each stamped with `sha`, and whether every gate passed."""
    gate_env, _ = env.child_env(os.environ, ())
    gate_dir.mkdir(exist_ok=True)

    records: list[dict] = []
    for n, cmd in enumerate(gates, start=1):
        if cancel is not None and cancel.cancelled:
            return records, False
        attempt_dir = gate_dir / str(n)
        attempt_dir.mkdir(exist_ok=True)

        inv = process.Invocation(argv=["bash", "-c", cmd], stdin_path=None)

        def on_start(pgid: int) -> None:
            if cancel is not None:
                cancel.pgid = pgid

        ran = process.run(inv, workdir, gate_env, attempt_dir, timedelta(seconds=GATE_TIMEOUT_S), on_start=on_start)
        if cancel is not None:
            cancel.pgid = None

        stdout_text = (attempt_dir / "stdout").read_text(errors="replace")
        stderr_text = (attempt_dir / "stderr").read_text(errors="replace")
        log_content = stdout_text + stderr_text
        log_path = gate_dir / f"{n}.log"
        log_path.write_text(log_content)

        lines = log_content.splitlines()
        tail = "\n".join(lines[-40:])

        rc = ran.rc if not ran.timed_out else (ran.rc if ran.rc != 0 else -15)
        records.append(
            {
                "cmd": cmd,
                "rc": rc,
                "duration_s": round(ran.duration_s, 3),
                "tail": tail,
                "log": str(log_path),
                "sha": sha,
            }
        )

        if rc != 0:
            return records, False
    return records, True


def run_gate_job(
    p: Paths,
    repo: guards.Repo,
    cli_gates: list[str],
    base: str,
    since: str,
    now: Callable[[], datetime],
) -> dict:
    """Run the repo's default gates, then `cli_gates`, on the committed HEAD of a clean work tree and record them
    as a `gate` job, with what the diff from `since` (the merge base with `base`) touched.
    The default gates come from the merge base, falling back to the work tree's config when the base has none.
    Refuses (ConfigError, before any job is recorded) when neither has gates."""
    defaults, source = base_repo_gates(repo, since)
    if not defaults:
        raise ConfigError("no repo gates: set gates in .agent-hub/config.toml")
    started = now()
    job = new_job(p, "gate", started)
    head = guards.head(repo)
    record: dict = {
        "id": job.id,
        "role": "gate",
        "kind": "gate",
        "status": "running",
        "exit_code": None,
        "agent": "agent-hub",
        "model": "gate",
        "workdir": str(repo.root),
        "repo": str(repo.common_dir),
        "head": head,
        "base": base,
        "repo_gates": defaults,
        "repo_gates_source": source,
        "started": started.isoformat(),
        "ended": None,
        "duration_s": None,
        "changed_files": [],
        "diffstat": "",
        "reply_path": None,
    }
    with _Cancel() as cancel:
        try:
            record["gate"], passed = run_gates(
                merge_gates(defaults, cli_gates), repo.root, job.dir / "gate", head, cancel
            )
            record["gate_touched"] = [] if cancel.cancelled else gateguard.inspect_diff(repo, since)
        except BaseException:
            _finish(job, record, "failed", now, started)
            raise
    if cancel.cancelled:
        return _finish(job, record, "cancelled", now, started)
    return _finish(job, record, "ok" if passed else "gate-failed", now, started)


class _Cancel:
    """Tracks SIGTERM/SIGINT; stops the running attempt's process group when one arrives."""

    def __init__(self) -> None:
        self.cancelled = False
        self.pgid: int | None = None
        self._old: dict[int, object] = {}

    def _handle(self, signum, frame) -> None:
        self.cancelled = True
        if self.pgid is not None:
            process.terminate(self.pgid)

    def __enter__(self) -> "_Cancel":
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                self._old[sig] = signal.signal(sig, self._handle)
        return self

    def __exit__(self, *exc) -> None:
        for sig, old in self._old.items():
            signal.signal(sig, old)


def run_job(args: RunArgs, p: Paths, pool: Pool, now: Callable[[], datetime]) -> dict:
    kind = args.kind or args.role
    adapters = registry()
    excluded = set(args.exclude)
    links = [link for link in args.links if str(link) not in excluded]
    for link in links:
        if link.agent not in adapters:
            raise ConfigError(f"unknown agent in chain: {link.agent}")
    briefmod.validate(kind, args.brief)

    started = now()
    workdir = args.workdir
    repo = None
    refusal = None
    base = None
    diff = ""
    try:
        repo = guards.inspect(workdir)
        if kind == "implement":
            guards.check_implement(repo)
        else:
            base = args.base or repo.default_branch
            diff = guards.review_diff(repo, base)
    except guards.GuardError as e:
        refusal = str(e)

    gates = args.gates
    if kind == "implement" and repo is not None:
        gates = merge_gates(repo_gates(repo.root), args.gates)
    repo_config = None
    if kind == "review":
        repo_config = base_repo_config(repo, base) if repo is not None and base is not None else ""
    plugin_dirs = ()
    if refusal is None:
        plugin_dirs = worker_plugins(p.config_dir, repo.root if repo else workdir, repo_config)

    if kind == "review" and base is None:
        base = args.base or (repo.default_branch if repo else "HEAD")

    removed: set[str] = set()
    for link in args.links:
        removed.update(env.child_env(os.environ, adapters[link.agent].config.env_passthrough)[1])

    job = new_job(p, args.role, started)
    repo_id = str(repo.common_dir) if repo is not None else None
    job.write_json(
        "request.json",
        {
            "role": args.role,
            "links": [str(link) for link in links],
            "exclude": sorted(excluded),
            "workdir": str(workdir),
            "repo": repo_id,
            "base": base,
            "timeout_s": int(args.timeout.total_seconds()),
            "removed_env": sorted(removed),
        },
    )
    text = args.brief
    if kind == "review":
        diff_path = job.dir / "diff.patch"
        diff_path.write_text(diff)
        text = briefmod.review_text(base, args.brief, diff_path)
    standing = briefmod.standing_orders(p.config_dir, repo.root if repo else None)
    if standing:
        text += "\n\n" + standing
    if kind == "implement":
        text += "\n" + handoff.FOOTER
    else:
        judge = _touched_section(touched_findings(list_jobs(p, 100000), workdir, guards.head(repo))) if repo else ""
        text += "\n\n" + judge + handoff.REVIEW_FOOTER
    (job.dir / "brief.md").write_text(text)
    pid = os.getpid()
    (job.dir / "pid").write_text(json.dumps({"pid": pid, "start_time": start_time(pid)}))

    record: dict = {
        "id": job.id,
        "role": args.role,
        "kind": kind,
        "status": "running",
        "exit_code": None,
        "agent": None,
        "model": None,
        "workdir": str(workdir),
        "repo": repo_id,
        "base": base,
        "started": started.isoformat(),
        "ended": None,
        "duration_s": None,
        "attempts": [],
        "changed_files": [],
        "diffstat": "",
        "reply_path": None,
        "head": None,
        "verdict": None,
    }
    job.write_json("result.json", record)

    if refusal is not None:
        record["error"] = refusal
        return _finish(job, record, "refused", now, started)

    assert repo is not None
    start_head = guards.head(repo)
    before = guards.fingerprint(repo)
    cooldowns = Cooldowns(p.cooldowns_file)
    status: str | None = None

    with _Cancel() as cancel:
        for n, link in enumerate(links, start=1):
            if cancel.cancelled:
                status = "cancelled"
                break
            adapter = adapters[link.agent]
            entry: dict = {"n": n, "agent": link.agent, "model": link.model}
            record["attempts"].append(entry)

            if not adapter.installed():
                entry.update(outcome="skipped", reason="not-installed")
                continue
            until = cooldowns.until(link, now())
            if until is not None:
                entry.update(outcome="skipped", reason=f"cooldown until {until.isoformat()}")
                continue

            attempt_dir = job.attempt_dir(n, link.agent, link.model)
            req = Request(
                role=kind,
                model=link.model,
                workdir=workdir,
                brief_path=job.dir / "brief.md",
                attempt_dir=attempt_dir,
                git_common_dir=repo.common_dir,
                plugin_dirs=plugin_dirs,
            )
            cl = adapter.preflight(req)
            if cl is not None:
                ran = None
            else:
                inv = adapter.build(req)
                child_env, _ = env.child_env(os.environ, adapter.config.env_passthrough)

                def on_start(pgid: int, n=n, link=link, attempt_dir=attempt_dir) -> None:
                    cancel.pgid = pgid
                    job.write_json(
                        _rel(job, attempt_dir, "attempt.json"),
                        {"n": n, "agent": link.agent, "model": link.model, "pgid": pgid, "started": now().isoformat()},
                    )

                ran = process.run(inv, workdir, child_env, attempt_dir, args.timeout, on_start=on_start)
                cancel.pgid = None

                cl = adapter.classify(ran.rc, attempt_dir)
                if ran.timed_out:
                    cl = dataclasses.replace(cl, outcome=Outcome.TIMEOUT)
            _write_reply(attempt_dir, cl)
            if ran is None:
                entry.update(outcome=str(cl.outcome), reason=cl.detail)
            else:
                entry.update(outcome=str(cl.outcome), rc=ran.rc, duration_s=round(ran.duration_s, 3))
            record.update(agent=link.agent, model=link.model, reply_path=str(attempt_dir / "reply.md"))

            if cancel.cancelled:
                entry["outcome"] = "cancelled"
                status = "cancelled"
                break
            if cl.outcome == Outcome.QUOTA:
                cooldowns.set(link, cl.reset_at or (now() + pool.quota_cooldown))

            if kind == "review" and guards.fingerprint(repo) != before:
                status = "modified-tree"
                break
            if cl.outcome == Outcome.OK:
                status = "ok"
                break
            if cl.outcome in FALLBACK:
                if guards.fingerprint(repo) != before:
                    status = "interrupted"
                    break
                continue
            status = TERMINAL[cl.outcome]
            break

    record["handoff_inferred"] = None
    if kind == "implement":
        reply_path = record.get("reply_path")
        parsed = None
        if reply_path and Path(reply_path).is_file():
            reply_text = Path(reply_path).read_text()
            parsed = handoff.parse(reply_text)
            if parsed is None and args.jev:
                from agenthub import jevclient

                record["handoff_inferred"] = jevclient.classify_reply(reply_text)
        record["handoff"] = parsed
        record["handoff_missing"] = parsed is None
    else:
        record["handoff"] = None
        record["handoff_missing"] = False
        reply_path = record.get("reply_path")
        if status == "ok" and reply_path and Path(reply_path).is_file():
            reply_text = Path(reply_path).read_text()
            record["verdict"] = handoff.parse_verdict(reply_text)
            record["touched_review"] = handoff.parse_touched_entries(reply_text)

    record["gate"] = []
    if status == "ok" and kind == "implement" and gates:
        record["gate"], passed = run_gates(gates, workdir, job.dir / "gate", guards.head(repo))
        if not passed:
            status = "gate-failed"

    if kind == "implement" and status in ("ok", "gate-failed"):
        record["gate_touched"] = gateguard.inspect_diff(repo, start_head)
    else:
        record["gate_touched"] = []

    record["head"] = guards.head(repo)
    files, stat = guards.changes(repo, start_head)
    record.update(changed_files=files, diffstat=stat)
    if args.allow and kind == "implement" and status in ("ok", "gate-failed"):
        outside = [
            f
            for f in files
            if not any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(f, g.replace("**", "*")) for g in args.allow)
        ]
        if outside:
            record["scope_violation"] = outside
            if status == "ok":
                status = "scope-violation"
    return _finish(job, record, status or "exhausted", now, started)


def readiness(jobs: list[dict], workdir: Path, head: str) -> list[str]:
    """What still blocks a merge of `workdir` at `head`; empty when ready. `jobs` are result records, newest first.
    Gate-touched findings are never waved through: an approving non-implementer review of `head` must judge each."""
    mine = [j for j in jobs if j.get("workdir") and Path(j["workdir"]).resolve() == workdir.resolve()]

    def who(j: dict) -> str:
        return f"{j.get('agent')}:{j.get('model')}"

    gated = [j for j in mine if _passed_gate(j, head)]
    implementers = {who(j) for j in mine if j.get("kind") == "implement" and j.get("status") in ("ok", "gate-failed")}
    reviews = [j for j in mine if j.get("kind") == "review" and j.get("status") == "ok" and j.get("verdict")]
    problems = []
    if not gated:
        problems.append(f"no passing gate on {head}")
    if not implementers:
        problems.append("no implementer recorded: cannot check reviewer independence")
    judges: list[dict] = []
    if not reviews:
        problems.append("no review")
    else:
        on_head = [r for r in reviews if r.get("head") == head]
        if not on_head:
            problems.append(f"review is stale (reviewed {reviews[0].get('head')})")
        else:
            approved = [r for r in on_head if r["verdict"] == "approve"]
            if not approved:
                problems.append(f"review verdict {on_head[0]['verdict']}")
            elif all(who(r) in implementers for r in approved):
                problems.append("reviewer is the implementer")
            judges = [r for r in on_head if who(r) not in implementers]
    if gated:
        problems.extend(_touched_problems(touched_findings(jobs, workdir, head), judges))
    return problems


def _touched_problems(findings: list[dict], judges: list[dict]) -> list[str]:
    """Problems with `findings` given the independent reviews of head `judges`: weakening or unjudged ones. A
    weakening judgement counts from any verdict and blocks whatever the other reviews say; a legitimate one only from
    an approval."""
    weakened: set[tuple[str, str, tuple[int, ...]]] = set()
    legitimate: set[tuple[str, str, tuple[int, ...]]] = set()
    for r in judges:
        for e in r.get("touched_review") or []:
            key = (e.get("file"), e.get("kind"), tuple(sorted(e.get("lines") or [])))
            if e.get("judgement") == "weakening":
                weakened.add(key)
            elif e.get("judgement") == "legitimate" and r["verdict"] == "approve":
                legitimate.add(key)
    problems = []
    unjudged = 0
    for f in findings:
        key = (f["file"], f["kind"], tuple(sorted(f["lines"])))
        if key in weakened:
            lines = ",".join(str(n) for n in f["lines"])
            problems.append(f"reviewer judged {f['file']} {f['kind']} lines {lines} as weakening")
        elif key not in legitimate:
            unjudged += 1
    if unjudged:
        problems.insert(0, f"gate touched: {unjudged} findings not judged by the reviewer")
    return problems


def _touched_section(findings: list[dict]) -> str:
    """The review-brief section asking the reviewer to judge each gate-touched finding; empty when there are none."""
    if not findings:
        return ""
    lines = "\n".join(f"- {f['file']} {f['kind']} lines {','.join(str(n) for n in f['lines'])}" for f in findings)
    return (
        "## Gate-touched findings to judge\n\n"
        "The gate guard flagged these changes to the checks themselves. Read each hunk and judge it independently.\n\n"
        f"{lines}\n\n"
        "In your final message add a `## Gate-touched` section with one line per finding:\n"
        "`<file> <kind> lines <l1,l2> — legitimate|weakening — <reason>`, naming the same lines as the finding.\n"
        "A judgement covers only the lines it names.\n\n"
    )


def touched_findings(jobs: list[dict], workdir: Path, head: str) -> list[dict]:
    """The gate_touched findings of every passing gate on `head` for `workdir`, deduplicated by (file, kind, lines);
    empty when none. A later clean gate does not hide an earlier finding. `jobs` are result records, newest first."""
    mine = [j for j in jobs if j.get("workdir") and Path(j["workdir"]).resolve() == workdir.resolve()]
    found: dict[tuple[str, str, tuple[int, ...]], dict] = {}
    for j in mine:
        if not _passed_gate(j, head):
            continue
        for f in j.get("gate_touched") or []:
            found.setdefault((f["file"], f["kind"], tuple(sorted(f["lines"]))), f)
    return list(found.values())


def _passed_gate(j: dict, head: str) -> bool:
    """Whether job `j` is a gate that passed on `head`; a gate job must also have completed (status ok, not cancelled
    after some gates) and have run every repo default gate."""
    if j.get("kind") not in ("implement", "gate") or j.get("head") != head or not j.get("gate"):
        return False
    if not all(g.get("rc") == 0 and g.get("sha") == head for g in j["gate"]):
        return False
    if j["kind"] == "gate":
        ran = {g.get("cmd") for g in j["gate"]}
        return j.get("status") == "ok" and bool(j.get("repo_gates")) and set(j["repo_gates"]) <= ran
    return True


def _rel(job: Job, attempt_dir: Path, name: str) -> str:
    return str((attempt_dir / name).relative_to(job.dir))


def _write_reply(attempt_dir: Path, cl: Classified) -> None:
    reply_file = attempt_dir / "reply.md"
    if cl.reply or not reply_file.exists():
        reply_file.write_text(cl.reply)


def _finish(job: Job, record: dict, status: str, now, started: datetime) -> dict:
    ended = now()
    record.update(
        status=status,
        exit_code=EXIT[status],
        ended=ended.isoformat(),
        duration_s=round((ended - started).total_seconds(), 3),
    )
    job.write_json("result.json", record)
    return record
