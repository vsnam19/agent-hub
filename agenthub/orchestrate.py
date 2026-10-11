"""The run loop: walk a role's chain, attempt, classify, fall back, write the result record."""

import dataclasses
import json
import os
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from agenthub import brief as briefmod
from agenthub import env, guards, handoff, process
from agenthub.agents import Classified, Outcome, Request, registry
from agenthub.config import ConfigError, Paths, Pool
from agenthub.jobs import Job, new_job
from agenthub.pool import Cooldowns, Link
from agenthub.procinfo import start_time

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
    "cancelled": 130,
}

GATE_TIMEOUT_S = 600

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

    if kind == "review" and base is None:
        base = args.base or (repo.default_branch if repo else "HEAD")

    removed: set[str] = set()
    for link in args.links:
        removed.update(env.child_env(os.environ, adapters[link.agent].config.env_passthrough)[1])

    job = new_job(p, args.role, started)
    job.write_json(
        "request.json",
        {
            "role": args.role,
            "links": [str(link) for link in links],
            "exclude": sorted(excluded),
            "workdir": str(workdir),
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
    (job.dir / "brief.md").write_text(text)
    pid = os.getpid()
    (job.dir / "pid").write_text(json.dumps({"pid": pid, "start_time": start_time(pid)}))

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

    record["gate"] = []
    if status == "ok" and kind == "implement" and args.gates:
        gate_env, _ = env.child_env(os.environ, ())
        gate_dir = job.dir / "gate"
        gate_dir.mkdir(exist_ok=True)

        for n, cmd in enumerate(args.gates, start=1):
            attempt_dir = gate_dir / str(n)
            attempt_dir.mkdir(exist_ok=True)

            inv = process.Invocation(argv=["bash", "-c", cmd], stdin_path=None)
            ran = process.run(inv, workdir, gate_env, attempt_dir, timedelta(seconds=GATE_TIMEOUT_S))

            stdout_text = (attempt_dir / "stdout").read_text(errors="replace")
            stderr_text = (attempt_dir / "stderr").read_text(errors="replace")
            log_content = stdout_text + stderr_text
            log_path = gate_dir / f"{n}.log"
            log_path.write_text(log_content)

            lines = log_content.splitlines()
            tail = "\n".join(lines[-40:])

            rc = ran.rc if not ran.timed_out else (ran.rc if ran.rc != 0 else -15)
            record["gate"].append(
                {"cmd": cmd, "rc": rc, "duration_s": round(ran.duration_s, 3), "tail": tail, "log": str(log_path)}
            )

            if rc != 0:
                status = "gate-failed"
                break

    if kind == "implement" and status in ("ok", "gate-failed"):
        from agenthub import gateguard

        record["gate_touched"] = gateguard.inspect_diff(repo, start_head)
    else:
        record["gate_touched"] = []

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
