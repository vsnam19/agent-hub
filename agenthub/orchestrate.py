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
from typing import Any, Callable

from agenthub import brief as briefmod
from agenthub import env, gateguard, guards, handoff, process
from agenthub import models as modelcat
from agenthub.agents import Classified, Outcome, Request, registry
from agenthub.audit.emit import emit
from agenthub.audit.trace import trace_context, trace_env
from agenthub.config import ConfigError, Paths, Pool
from agenthub.jobs import Job, list_jobs, new_job
from agenthub.pool import Cooldowns, Link
from agenthub.procinfo import start_time
from agenthub.workers import available_skills, skill_plugin_dirs, worker_plugins

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

FALLBACK = (Outcome.QUOTA, Outcome.AUTH, Outcome.MODEL_UNAVAILABLE, Outcome.TRANSIENT)
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
    skills: list[str] = dataclasses.field(default_factory=list)  # worker skills the brief tells the worker to load


def _gate_summary(gates: list[dict] | None) -> str | None:
    if not gates:
        return None
    failed = next((g for g in gates if g.get("rc") != 0), None)
    if failed:
        return f"FAILED: {failed.get('cmd')} (rc {failed.get('rc')})"
    return f"passed ({len(gates)})"


def _parent_for(job_id: str) -> dict[str, Any]:
    env_parent = trace_context(os.environ).parent
    parent: dict[str, Any] = {}
    for key in ("plan", "task"):
        if key in env_parent:
            parent[key] = env_parent[key]
    parent["job"] = job_id
    return parent


def _emit_job(
    event: str,
    *,
    state_dir: Path | str,
    job_id: str,
    status: str = "ok",
    exit_code: int | None = None,
    duration_ms: int | float | None = None,
    quote: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        emit(
            "job",
            event,
            state_dir=state_dir,
            job_id=job_id,
            parent=_parent_for(job_id),
            status=status,
            exit_code=exit_code,
            duration_ms=duration_ms,
            quote=quote,
            detail=detail,
        )
    except Exception:
        pass


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


def on_started(job_id: str) -> None:
    """Hook called immediately after a job's initial running record (result.json) is written."""
    pass


def run_gate_job(
    p: Paths,
    repo: guards.Repo,
    cli_gates: list[str],
    base: str,
    since: str,
    now: Callable[[], datetime],
    *,
    on_started: Callable[[str], None] | None = None,
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
    pid = os.getpid()
    (job.dir / "pid").write_text(json.dumps({"pid": pid, "start_time": start_time(pid)}))
    with _Cancel() as cancel:
        try:
            job.write_json("result.json", record)
            (on_started or globals()["on_started"])(job.id)
            _emit_job(
                "start",
                state_dir=p.state_dir,
                job_id=job.id,
                detail={"role": "gate", "kind": "gate", "workdir": str(repo.root)},
            )
            record["gate"], passed = run_gates(
                merge_gates(defaults, cli_gates), repo.root, job.dir / "gate", head, cancel
            )
            _emit_job(
                "gate",
                state_dir=p.state_dir,
                job_id=job.id,
                status="ok" if passed else "error",
                quote=_gate_summary(record["gate"]),
            )
            record["gate_touched"] = [] if cancel.cancelled else gateguard.inspect_diff(repo, since)
        except BaseException:
            _finish(job, record, "cancelled" if cancel.cancelled else "failed", now, started, state_dir=p.state_dir)
            raise
    if cancel.cancelled:
        return _finish(job, record, "cancelled", now, started, state_dir=p.state_dir)
    return _finish(job, record, "ok" if passed else "gate-failed", now, started, state_dir=p.state_dir)


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


def run_job(
    args: RunArgs,
    p: Paths,
    pool: Pool,
    now: Callable[[], datetime],
    *,
    on_started: Callable[[str], None] | None = None,
) -> dict:
    kind = args.kind or args.role
    # read-only jobs must leave the tree as they found it; a review also gets a diff, a plan has no base
    read_only = kind in ("review", "plan")
    adapters = registry()
    started = now()
    workdir = Path(args.workdir).resolve()
    repo = None
    refusal = None
    base = None
    diff = ""
    try:
        repo = guards.inspect(workdir)
        if kind == "implement":
            guards.check_implement(repo)
        elif kind == "review":
            base = args.base or repo.default_branch
            diff = guards.review_diff(repo, base)
    except guards.GuardError as e:
        refusal = str(e)

    jobs = list_jobs(p, 100000) if kind == "review" else []
    # effort is ignored here: the same model at another effort is not an independent reviewer
    excluded = {text.partition("@")[0] for text in args.exclude}
    if kind == "review":
        excluded.update(implementers(jobs, workdir))
    links = [link for link in args.links if link.quota_key not in excluded]
    for link in links:
        if link.agent not in adapters:
            raise ConfigError(f"unknown agent in chain: {link.agent}")
    briefmod.validate(kind, args.brief)
    skills = list(dict.fromkeys(args.skills))
    known_skills = available_skills(p.config_dir) if skills else {}
    for skill in skills:
        if skill not in known_skills:
            raise ConfigError(f"unknown skill {skill!r}: {skills_hint(known_skills)}")

    gates = args.gates
    if kind == "implement" and repo is not None:
        gates = merge_gates(repo_gates(repo.root), args.gates)
    repo_config = None
    if kind == "review":
        repo_config = base_repo_config(repo, base) if repo is not None and base is not None else ""
    elif kind == "plan" and repo is not None:
        # a plan has no base to pin to: its worker plugins come from HEAD, not from uncommitted edits
        repo_config = base_repo_config(repo, "HEAD")
    plugin_dirs = ()
    if refusal is None:
        plugin_dirs = worker_plugins(p.config_dir, repo.root if repo else workdir, repo_config)
        if skills:
            # a named skill must be loadable even when its plugin is not in this run's default list
            skill_dirs = skill_plugin_dirs(p.config_dir, [known_skills[s] for s in skills])
            plugin_dirs = (*plugin_dirs, *(d for d in skill_dirs if d not in plugin_dirs))

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
    elif kind == "plan":
        text = briefmod.plan_text(args.brief)
    standing = briefmod.standing_orders(p.config_dir, repo.root if repo else None)
    if standing:
        text += "\n\n" + standing
    if skills:
        text += "\n\n" + _skills_section(skills, known_skills) + "\n"
    if kind == "implement":
        text += "\n" + handoff.FOOTER
    elif kind == "review":
        judge = _touched_section(touched_findings(jobs, workdir, guards.head(repo))) if repo else ""
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
    if not links:
        record["reason"] = f"all links excluded: {', '.join(sorted(excluded))}" if excluded else "no links"
    status: str | None = None
    denied_last = False

    with _Cancel() as cancel:
        try:
            job.write_json("result.json", record)
            (on_started or globals()["on_started"])(job.id)
            _emit_job(
                "start",
                state_dir=p.state_dir,
                job_id=job.id,
                detail={"role": args.role, "kind": kind, "workdir": str(workdir)},
            )

            if cancel.cancelled:
                return _finish(job, record, "cancelled", now, started)

            if refusal is not None:
                record["error"] = refusal
                return _finish(job, record, "refused", now, started)

            assert repo is not None
            start_head = guards.head(repo)
            before = guards.fingerprint(repo)
            cooldowns = Cooldowns(p.cooldowns_file)
            # read-only: job runs never fetch or write the catalog cache
            catalog = modelcat.read(p.models_file)

            for n, link in enumerate(links, start=1):
                if cancel.cancelled:
                    status = "cancelled"
                    break
                adapter = adapters[link.agent]
                entry: dict = {"n": n, "agent": link.agent, "model": link.model}
                if link.effort:
                    entry["effort"] = link.effort
                record["attempts"].append(entry)

                if link.effort and link.agent != "claude":
                    entry.update(outcome="config", reason="effort is only supported for claude links")
                    continue
                if not adapter.installed():
                    entry.update(outcome="skipped", reason="not-installed")
                    continue
                # D-121 2.3: only a fresh catalog that lacks the model skips; unknown or stale answers None
                if modelcat.model_known(catalog, link.agent, link.model, now()) is False:
                    entry.update(outcome="skipped", reason="unknown-model")
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
                    effort=link.effort,
                )
                cl = adapter.preflight(req)
                if cl is not None:
                    ran = None
                else:
                    inv = adapter.build(req)
                    child_env, _ = env.child_env(os.environ, adapter.config.env_passthrough)
                    child_env.update(trace_env(job=job.id, role=args.role, attempt=n))

                    def on_start(pgid: int, n=n, link=link, attempt_dir=attempt_dir) -> None:
                        cancel.pgid = pgid
                        meta = {
                            "n": n,
                            "agent": link.agent,
                            "model": link.model,
                            "pgid": pgid,
                            "started": now().isoformat(),
                        }
                        if link.effort:
                            meta["effort"] = link.effort
                        job.write_json(_rel(job, attempt_dir, "attempt.json"), meta)

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
                if link.effort:
                    record["effort"] = link.effort
                else:
                    record.pop("effort", None)
                _emit_job(
                    "attempt",
                    state_dir=p.state_dir,
                    job_id=job.id,
                    status="ok" if cl.outcome == Outcome.OK else "error",
                    exit_code=ran.rc if ran is not None else None,
                    duration_ms=int(round(ran.duration_s * 1000)) if ran is not None else None,
                    detail={
                        "agent": link.agent,
                        "model": link.model,
                        "n": n,
                        "outcome": entry.get("outcome"),
                        "rc": entry.get("rc"),
                        "duration": entry.get("duration_s"),
                    },
                )

                if cancel.cancelled:
                    entry["outcome"] = "cancelled"
                    status = "cancelled"
                    break
                denied_last = False
                if cl.outcome == Outcome.QUOTA:
                    cooldowns.set(link, cl.reset_at or (now() + pool.quota_cooldown))
                elif cl.outcome == Outcome.TRANSIENT:
                    cooldowns.set(link, now() + timedelta(minutes=3))

                if read_only and guards.fingerprint(repo) != before:
                    status = "modified-tree"
                    break
                if cl.outcome == Outcome.OK:
                    status = "ok"
                    break
                if cl.outcome in FALLBACK:
                    if guards.fingerprint(repo) != before:
                        status = "interrupted"
                        break
                    if n < len(links):
                        _emit_job(
                            "fallback",
                            state_dir=p.state_dir,
                            job_id=job.id,
                            detail={"from": n, "outcome": str(cl.outcome)},
                        )
                    continue
                if read_only and cl.outcome == Outcome.FAILED and cl.detail.startswith("permission auto-denied"):
                    entry["fallthrough"] = "permission auto-denied"
                    denied_last = True
                    continue
                status = TERMINAL[cl.outcome]
                break

            if cancel.cancelled:
                status = "cancelled"
        except BaseException:
            _finish(job, record, "cancelled" if cancel.cancelled else "failed", now, started)
            raise

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
        if kind == "review" and status == "ok" and reply_path and Path(reply_path).is_file():
            reply_text = Path(reply_path).read_text()
            record["verdict"] = handoff.parse_verdict(reply_text)
            record["touched_review"] = handoff.parse_touched_entries(reply_text)
            if record["verdict"]:
                _emit_job(
                    "review",
                    state_dir=p.state_dir,
                    job_id=job.id,
                    quote=str(record["verdict"]),
                    detail={"verdict": record["verdict"]},
                )

    record["gate"] = []
    if status == "ok" and kind == "implement" and gates:
        record["gate"], passed = run_gates(gates, workdir, job.dir / "gate", guards.head(repo))
        _emit_job(
            "gate",
            state_dir=p.state_dir,
            job_id=job.id,
            status="ok" if passed else "error",
            quote=_gate_summary(record["gate"]),
        )
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
    return _finish(job, record, status or ("failed" if denied_last else "exhausted"), now, started)


def implementers(jobs: list[dict], workdir: Path) -> set[str]:
    """The agent:model of every implement job on that resolved work tree with status ok or gate-failed."""
    target = Path(workdir).resolve()
    found: set[str] = set()
    for j in jobs:
        if (
            j.get("kind") == "implement"
            and j.get("status") in ("ok", "gate-failed")
            and j.get("agent")
            and j.get("model")
            and j.get("workdir")
        ):
            try:
                if Path(j["workdir"]).resolve() == target:
                    found.add(f"{j['agent']}:{j['model']}")
            except OSError:
                continue
    return found


def readiness(jobs: list[dict], workdir: Path, head: str) -> list[str]:
    """What still blocks a merge of `workdir` at `head`; empty when ready. `jobs` are result records, newest first.
    Gate-touched findings are never waved through: an approving non-implementer review of `head` must judge each."""
    mine = [j for j in jobs if j.get("workdir") and Path(j["workdir"]).resolve() == workdir.resolve()]

    def who(j: dict) -> str:
        return f"{j.get('agent')}:{j.get('model')}"

    gated = [j for j in mine if _passed_gate(j, head)]
    impl = implementers(jobs, workdir)
    reviews = [j for j in mine if j.get("kind") == "review" and j.get("status") == "ok" and j.get("verdict")]
    problems = []
    if not gated:
        problems.append(f"no passing gate on {head}")
    if not impl:
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
            elif all(who(r) in impl for r in approved):
                problems.append("reviewer is the implementer")
            judges = [r for r in on_head if who(r) not in impl]
    if gated:
        problems.extend(_touched_problems(touched_findings(jobs, workdir, head), judges))
    return problems


def _touched_problems(findings: list[dict], judges: list[dict]) -> list[str]:
    """Problems with `findings` given the independent reviews of head `judges`: weakening or unjudged ones. A
    weakening judgement counts from any verdict and blocks whatever the other reviews say; a legitimate one only from
    an approval."""
    weakened: dict[tuple[str, str], set[int]] = {}
    legitimate: dict[tuple[str, str], set[int]] = {}
    for r in judges:
        for e in r.get("touched_review") or []:
            key = (e.get("file"), e.get("kind"))
            lines = set(e.get("lines") or [])
            if e.get("judgement") == "weakening":
                weakened.setdefault(key, set()).update(lines)
            elif e.get("judgement") == "legitimate" and r.get("verdict") == "approve":
                legitimate.setdefault(key, set()).update(lines)
    problems = []
    unjudged = 0
    for f in findings:
        key = (f["file"], f["kind"])
        f_lines = set(f.get("lines") or [])
        if f_lines & weakened.get(key, set()):
            lines = ",".join(str(n) for n in f["lines"])
            problems.append(f"reviewer judged {f['file']} {f['kind']} lines {lines} as weakening")
        elif not (f_lines and f_lines.issubset(legitimate.get(key, set()))):
            unjudged += 1
    if unjudged:
        problems.insert(0, f"gate touched: {unjudged} findings not judged by the reviewer")
    return problems


def skills_hint(known: dict[str, str]) -> str:
    """The clause naming the skills that can be used, for an unknown-skill error."""
    return f"available skills are {', '.join(sorted(known))}" if known else "none configured"


def _skills_section(skills: list[str], known: dict[str, str]) -> str:
    """The `## Skills` brief section naming each skill with its plugin, in the order given."""
    listed = ", ".join(f"{s} ({known[s]})" for s in skills)
    return (
        f"## Skills\n\nLoad and follow these skills before you start (Claude lists them as <plugin>:<skill>): {listed}"
    )


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


def _finish(
    job: Job,
    record: dict,
    status: str,
    now: Callable[[], datetime],
    started: datetime,
    state_dir: Path | str | None = None,
) -> dict:
    try:
        current = job.read_json("result.json")
    except (OSError, ValueError):
        current = {}
    if current.get("status") == "cancelled" and status != "cancelled":
        return current
    ended = now()
    duration_s = (ended - started).total_seconds()
    record.update(
        status=status,
        exit_code=EXIT[status],
        ended=ended.isoformat(),
        duration_s=round(duration_s, 3),
    )
    job.write_json("result.json", record)

    handoff_data = record.get("handoff")
    quote = (
        handoff_data["summary"]
        if isinstance(handoff_data, dict) and isinstance(handoff_data.get("summary"), str)
        else None
    )
    try:
        resolved_state_dir = state_dir if state_dir is not None else job.dir.parent.parent
    except Exception:
        resolved_state_dir = ""

    _emit_job(
        "finish",
        state_dir=resolved_state_dir,
        job_id=job.id,
        status=status,
        exit_code=EXIT[status],
        duration_ms=int(round(duration_s * 1000)),
        quote=quote,
    )
    return record
