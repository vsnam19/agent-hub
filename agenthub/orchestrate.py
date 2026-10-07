"""The run loop: walk a role's chain, attempt, classify, fall back, write the result record."""

import dataclasses
import os
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from agenthub import brief as briefmod
from agenthub import env, guards, process
from agenthub.agents import Classified, Outcome, Request, registry
from agenthub.config import ConfigError, Paths, Pool
from agenthub.jobs import Job, new_job
from agenthub.pool import Cooldowns, Link

EXIT = {
    "ok": 0,
    "failed": 1,
    "refused": 3,
    "exhausted": 4,
    "modified-tree": 5,
    "timeout": 6,
    "empty": 7,
    "interrupted": 8,
    "cancelled": 130,
}

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
    adapters = registry()
    for link in args.links:
        if link.agent not in adapters:
            raise ConfigError(f"unknown agent in chain: {link.agent}")
    briefmod.validate(args.role, args.brief)

    started = now()
    workdir = args.workdir
    repo = None
    refusal = None
    base = None
    diff = ""
    try:
        repo = guards.inspect(workdir)
        if args.role == "implement":
            guards.check_implement(repo)
        else:
            base = args.base or repo.default_branch
            diff = guards.review_diff(repo, base)
    except guards.GuardError as e:
        refusal = str(e)

    if args.role == "review" and base is None:
        base = args.base or (repo.default_branch if repo else "HEAD")

    removed: set[str] = set()
    for link in args.links:
        removed.update(env.child_env(os.environ, adapters[link.agent].config.env_passthrough)[1])

    job = new_job(p, args.role, started)
    job.write_json(
        "request.json",
        {
            "role": args.role,
            "links": [str(link) for link in args.links],
            "workdir": str(workdir),
            "base": base,
            "timeout_s": int(args.timeout.total_seconds()),
            "removed_env": sorted(removed),
        },
    )
    text = args.brief
    if args.role == "review":
        diff_path = job.dir / "diff.patch"
        diff_path.write_text(diff)
        text = briefmod.review_text(base, args.brief, diff_path)
    (job.dir / "brief.md").write_text(text)
    (job.dir / "pid").write_text(f"{os.getpid()}\n")

    record: dict = {
        "id": job.id,
        "role": args.role,
        "status": "running",
        "exit_code": None,
        "agent": None,
        "model": None,
        "workdir": str(workdir),
        "base": base,
        "started": started.isoformat(),
        "ended": None,
        "duration_s": None,
        "attempts": [],
        "changed_files": [],
        "diffstat": "",
        "reply_path": None,
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
        for n, link in enumerate(args.links, start=1):
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
                role=args.role,
                model=link.model,
                workdir=workdir,
                brief_path=job.dir / "brief.md",
                attempt_dir=attempt_dir,
                git_common_dir=repo.common_dir,
            )
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
            entry.update(outcome=str(cl.outcome), rc=ran.rc, duration_s=round(ran.duration_s, 3))
            record.update(agent=link.agent, model=link.model, reply_path=str(attempt_dir / "reply.md"))

            if cancel.cancelled:
                entry["outcome"] = "cancelled"
                status = "cancelled"
                break
            if cl.outcome == Outcome.QUOTA:
                cooldowns.set(link, cl.reset_at or (now() + pool.quota_cooldown))

            if args.role == "review" and guards.fingerprint(repo) != before:
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

    files, stat = guards.changes(repo, start_head)
    record.update(changed_files=files, diffstat=stat)
    return _finish(job, record, status or "exhausted", now, started)


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
