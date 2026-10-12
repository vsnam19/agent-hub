import argparse
import json
import os
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from agenthub import guards
from agenthub.agents import registry
from agenthub.brief import BriefError, order_table
from agenthub.config import ConfigError, load_pool, parse_duration, paths, role_kind
from agenthub.jobs import get_job, list_jobs
from agenthub.orchestrate import EXIT, RunArgs, readiness, run_gate_job, run_job
from agenthub.plan import PlanError
from agenthub.plan import load as load_plan
from agenthub.planrun import run_plan
from agenthub.pool import Cooldowns, parse_link
from agenthub.procinfo import start_time

WAIT_POLL_S = 1.0
WAIT_APPEAR_S = 10.0
PLAN_WAIT_POLL_S = 1.0
PLAN_WAIT_APPEAR_S = float(os.environ.get("AGENT_HUB_PLAN_WAIT_APPEAR_S", "10.0"))
ALL_JOBS = 100000  # every job record, for filters that must see past any --limit


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise UsageError(message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="agent-hub", description="Run coding agents by role with quota fallback")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    run = sub.add_parser("run", help="run one job")
    run.add_argument("--role", required=True)
    run.add_argument("--brief", help="brief file, or - for stdin (optional for review)")
    run.add_argument("-d", "--dir", required=True, dest="workdir")
    run.add_argument("--agent", help="use this single agent:model instead of the role's chain")
    run.add_argument("--base", help="review only: ref to compare against (default: the default branch)")
    run.add_argument("--timeout", help="e.g. 30m (default: the role's timeout)")
    run.add_argument("--gate", action="append", dest="gates", help="gate command to run (repeatable)")
    run.add_argument(
        "--allow", action="append", default=[], metavar="GLOB", help="only these paths may change (repeatable)"
    )
    run.add_argument(
        "--exclude", action="append", default=[], metavar="AGENT:MODEL", help="skip this link (repeatable)"
    )
    run.add_argument(
        "--skill", action="append", default=[], dest="skills", metavar="NAME", help="worker skill to load (repeatable)"
    )
    run.add_argument("--jev", action="store_true", help="use jev fallback for missing handoffs")
    run.add_argument("--json", action="store_true")

    pool = sub.add_parser("pool", help="show roles, chains and cooldowns")
    pool.add_argument("--json", action="store_true")
    pool.add_argument("--clear", metavar="AGENT:MODEL")

    profile = sub.add_parser("profile", help="print an agent's profile")
    profile.add_argument("agent")

    jobs = sub.add_parser("jobs", help="list recent jobs of this repository")
    jobs.add_argument("-d", "--dir", default=".", dest="workdir", help="repository whose jobs to list")
    jobs.add_argument("--all", action="store_true", help="list the jobs of every repository")
    jobs.add_argument("--limit", type=int, default=20)

    show = sub.add_parser("show", help="show a job")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")

    cancel = sub.add_parser("cancel", help="cancel a running job of this repository (exit 3 if it is another's)")
    cancel.add_argument("id", nargs="?")
    cancel.add_argument("--all", action="store_true", help="cancel every running job of this repository")
    cancel.add_argument("-d", "--dir", default=".", dest="workdir", help="repository whose jobs to cancel")

    wait = sub.add_parser("wait", help="wait for a job to finish")
    wait.add_argument("-d", "--dir", required=True, dest="workdir")
    wait.add_argument("--timeout", help="e.g. 9m")
    wait.add_argument("--json", action="store_true")

    plan = sub.add_parser("plan", help="run a multi-task plan")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True, parser_class=_Parser)
    plan_run = plan_sub.add_parser("run")
    plan_run.add_argument("plan_file", metavar="<plan.json>")
    plan_run.add_argument("--max-parallel", type=int, metavar="N")
    plan_status = plan_sub.add_parser("status")
    plan_status.add_argument("plan_file", metavar="<plan.json>")
    plan_status.add_argument("--json", action="store_true")
    plan_status.add_argument("--trail", action="store_true", help="print the decision trail as a table")
    plan_wait = plan_sub.add_parser("wait")
    plan_wait.add_argument("plan_file", metavar="<plan.json>")
    plan_wait.add_argument("--timeout", help="e.g. 9m")
    plan_wait.add_argument("--json", action="store_true")
    plan_cancel = plan_sub.add_parser("cancel", help="stop the plan's runner and its running jobs")
    plan_cancel.add_argument("plan_file", metavar="<plan.json>")
    plan_cancel.add_argument("-d", "--dir", default=".", dest="workdir", help="repository the caller runs from")

    orders = sub.add_parser("orders", help="list standing orders and what enforces each (exit 1 if one has none)")
    orders.add_argument("-d", "--dir", default=".", dest="workdir", help="repo whose standing orders to list")
    orders.add_argument("--json", action="store_true")

    ready = sub.add_parser(
        "ready", help="is this work tree's HEAD gated and approved by another model? (exit 1 if not)"
    )
    ready.add_argument("-d", "--dir", default=".", dest="workdir", help="work tree to check")
    ready.add_argument("--json", action="store_true")

    gate = sub.add_parser("gate", help="run the gates on the committed HEAD and record them (exit 9 if one fails)")
    gate.add_argument("-d", "--dir", default=".", dest="workdir", help="clean work tree to gate")
    gate.add_argument("--gate", action="append", dest="gates", help="gate command to run after the repo gates")
    gate.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        handler = {
            "run": _cmd_run,
            "pool": _cmd_pool,
            "profile": _cmd_profile,
            "jobs": _cmd_jobs,
            "show": _cmd_show,
            "cancel": _cmd_cancel,
            "wait": _cmd_wait,
            "plan": _cmd_plan,
            "orders": _cmd_orders,
            "ready": _cmd_ready,
            "gate": _cmd_gate,
        }[args.command]
        return handler(args)
    except SystemExit as e:  # --help
        return e.code if isinstance(e.code, int) else 0
    except (UsageError, ConfigError, BriefError, PlanError) as e:
        print(_safe(f"agent-hub: {e}", sys.stderr), file=sys.stderr)
        return 2


def _read_brief(args, kind: str) -> str:
    if args.brief is None:
        if kind == "implement":
            raise UsageError("--brief is required for implement")
        return ""
    if args.brief == "-":
        return sys.stdin.read()
    try:
        return Path(args.brief).read_text()
    except OSError as e:
        raise UsageError(f"cannot read brief: {e}") from None


def _safe(text: str, stream=None) -> str:
    """On a terminal, drop control/format characters (except newline and tab)."""
    if stream is None:
        stream = sys.stdout
    if not stream.isatty():
        return text
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf"))


def _summary(result: dict) -> str:
    lines = [f"status: {_safe(str(result['status']))}"]
    if result.get("agent"):
        lines.append(f"agent: {_safe(str(result['agent']))}:{_safe(str(result['model']))}")
    lines.append(f"changed: {len(result.get('changed_files') or [])} files")
    lines.append(f"job: {_safe(str(result['id']))}")

    if result.get("handoff"):
        h = result["handoff"]
        lines.append(f"handoff: {_safe(h['status'])} · {_safe(h['verification'])}")

    if "gate" in result and result["gate"]:
        gates = result["gate"]
        failed = next((g for g in gates if g["rc"] != 0), None)
        if failed:
            lines.append(f"gate: FAILED: {_safe(failed['cmd'])} (rc {failed['rc']})")
            if failed.get("tail"):
                lines.append(_safe(failed["tail"]))
        else:
            lines.append(f"gate: passed ({len(gates)})")

    if result.get("error"):
        lines.append(f"error: {_safe(str(result['error']))}")
    reply_path = result.get("reply_path")
    reply = ""
    if reply_path and Path(reply_path).is_file():
        reply = _safe(Path(reply_path).read_text())
    return "\n".join(lines) + "\n\n" + reply


def _emit(text: str) -> None:
    print(text, end="" if text.endswith("\n") else "\n")


def _cmd_run(args) -> int:
    p = paths()
    pool = load_pool(p)
    kind = role_kind(pool, args.role)
    if args.agent:
        links = [parse_link(args.agent)]
    else:
        links = [parse_link(x) for x in pool.roles[args.role].chain]
    timeout = parse_duration(args.timeout) if args.timeout else pool.timeouts[args.role]
    text = _read_brief(args, kind)
    run_args = RunArgs(
        role=args.role,
        kind=kind,
        brief=text,
        workdir=Path(args.workdir).expanduser().resolve(),
        links=links,
        timeout=timeout,
        base=args.base,
        gates=args.gates or [],
        jev=args.jev or os.environ.get("AGENT_HUB_JEV") == "1",
        exclude=args.exclude,
        allow=args.allow,
        skills=args.skills,
    )
    result = run_job(run_args, p, pool, _now)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _emit(_summary(result))
    return EXIT[result["status"]]


def _cmd_pool(args) -> int:
    p = paths()
    pool = load_pool(p)
    cooldowns = Cooldowns(p.cooldowns_file)
    adapters = registry()
    now = _now()

    if args.clear:
        link = parse_link(args.clear)
        if cooldowns.clear(link):
            print(_safe(f"cleared {link}"))
        else:
            print(_safe(f"no cooldown for {link}"))
        return 0

    roles: dict[str, dict] = {}
    for role, config in pool.roles.items():
        entries = []
        for text in config.chain:
            link = parse_link(text)
            adapter = adapters.get(link.agent)
            installed = adapter is not None and adapter.installed()
            until = cooldowns.until(link, now)
            entries.append(
                {
                    "link": str(link),
                    "installed": installed,
                    "version": adapter.version() if installed else None,
                    "cooldown_until": until.isoformat() if until else None,
                }
            )
        roles[role] = {"kind": config.kind, "escalate": config.escalate, "chain": entries}

    if args.json:
        print(json.dumps({"roles": roles}, indent=2))
        return 0
    for role, config in roles.items():
        escalate = f", escalate → {config['escalate']}" if config["escalate"] else ""
        print(_safe(f"{role} ({config['kind']}{escalate}):"))
        for e in config["chain"]:
            parts = [e["link"], "installed" if e["installed"] else "missing"]
            if e["version"]:
                parts.append(e["version"])
            if e["cooldown_until"]:
                parts.append(f"cooldown until {e['cooldown_until']}")
            print(_safe("  " + "  ".join(parts)))
    return 0


def _cmd_orders(args) -> int:
    try:
        repo_root = guards.inspect(Path(args.workdir)).root
    except guards.GuardError:
        repo_root = None
        note = f"agent-hub: {args.workdir} is not a git work tree; no repo standing orders"
        print(_safe(note, sys.stderr), file=sys.stderr)

    orders = order_table(paths().config_dir, repo_root)
    missing = [o for o in orders if o.enforced_by is None]
    for o in missing:
        warning = f"agent-hub: no '— enforced by:' on order {o.number} in {o.source}: {o.rule}"
        print(_safe(warning, sys.stderr), file=sys.stderr)

    if args.json:
        rows = [
            {"number": o.number, "rule": o.rule, "enforced_by": o.enforced_by, "source": str(o.source)} for o in orders
        ]
        print(json.dumps({"orders": rows}, indent=2))
        return 1 if missing else 0

    if not orders:
        print("no standing orders")
        return 0
    width = max(len(o.enforced_by or "MISSING") for o in orders)
    print(_safe(f"{'#':>3}  {'enforced by':<{width}}  rule  (source)"))
    for o in orders:
        print(_safe(f"{o.number:>3}  {o.enforced_by or 'MISSING':<{width}}  {o.rule}  ({o.source})"))
    return 1 if missing else 0


def _cmd_ready(args) -> int:
    try:
        repo = guards.inspect(Path(args.workdir))
    except guards.GuardError as e:
        raise UsageError(str(e)) from None
    head = guards.head(repo)
    jobs = list_jobs(paths(), 100000)
    problems = readiness(jobs, repo.root, head)
    if args.json:
        print(json.dumps({"ready": not problems, "head": head, "problems": problems}, indent=2))
    else:
        if problems:
            for problem in problems:
                print(_safe(f"not ready: {problem}"))
        else:
            print(f"ready: {head}")
    return 1 if problems else 0


def _cmd_gate(args) -> int:
    try:
        repo = guards.inspect(Path(args.workdir))
    except guards.GuardError as e:
        raise UsageError(str(e)) from None
    if guards._run_git(repo.root, "status", "--porcelain"):
        raise UsageError("commit first; gate runs on HEAD")
    base = repo.default_branch
    try:
        since = guards._run_git(repo.root, "merge-base", "HEAD", base)
    except subprocess.CalledProcessError:
        raise UsageError(f"no merge base of HEAD and {base}") from None
    result = run_gate_job(paths(), repo, args.gates or [], base, since, _now)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _emit(_summary(result))
    return EXIT[result["status"]]


def _cmd_profile(args) -> int:
    adapter = registry().get(args.agent)
    if adapter is None:
        raise UsageError(f"unknown agent: {args.agent}")
    print(_safe(adapter.profile_path.read_text()), end="")
    return 0


def _repo_of(workdir: str) -> str:
    """The git common dir of a work tree: the identity of its repository, shared by all of its worktrees."""
    try:
        return str(guards.inspect(Path(workdir).expanduser().resolve()).common_dir)
    except guards.GuardError as e:
        raise UsageError(str(e)) from None


def _job_repo(record: dict) -> str | None:
    """The repository a job belongs to; a record without `repo` derives it from its work tree, if that still exists."""
    if record.get("repo"):
        return record["repo"]
    if not record.get("workdir"):
        return None
    try:
        return str(guards.inspect(Path(record["workdir"])).common_dir)
    except guards.GuardError:
        return None


def _cmd_jobs(args) -> int:
    mine = None if args.all else _repo_of(args.workdir)
    rows = [r for r in list_jobs(paths(), ALL_JOBS) if args.all or _job_repo(r) == mine][: args.limit]
    for r in rows:
        agent = f"{r['agent']}:{r['model']}" if r.get("agent") else "-"
        dur = f"{r['duration_s']:.0f}s" if r.get("duration_s") is not None else "-"
        print(_safe("  ".join([r["id"], r["role"], r["status"], agent, dur, r["workdir"]])))
    return 0


def _repo_or_none(workdir: str) -> str | None:
    """The repository identity of a work tree, or None when the path is not a git work tree."""
    try:
        return _repo_of(workdir)
    except UsageError:
        return None


def _load_job(job_id: str):
    try:
        return get_job(paths(), job_id)
    except KeyError:
        raise UsageError(f"unknown job: {job_id}") from None


def _cmd_show(args) -> int:
    job = _load_job(args.id)
    try:
        result = job.read_json("result.json")
    except (OSError, ValueError):
        raise UsageError(f"job has no result: {args.id}") from None
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _emit(_summary(result))
    return 0


def _read_pid_file(path: Path) -> tuple[int | None, int | None]:
    """Return (pid, start_time) from the JSON pid file; a legacy plain int is unverifiable."""
    try:
        data = json.loads(path.read_text())
        return int(data["pid"]), int(data["start_time"])
    except (OSError, ValueError, TypeError, KeyError):
        return None, None


def _read_result(job) -> dict:
    try:
        return job.read_json("result.json")
    except (OSError, ValueError):
        return {}


def _terminate_verified(pid: int | None, recorded: int | None) -> bool:
    """SIGTERM the pid only while its start time still matches the recorded one; a reused pid never matches."""
    if pid is None or recorded is None or start_time(pid) != recorded:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def _cancel_job(job) -> str:
    """Cancel one job of the caller's repository; returns the line to print about it."""
    result = _read_result(job)
    if result.get("status") not in (None, "running"):
        return f"job already finished: {result['status']}"
    pid, recorded = _read_pid_file(job.dir / "pid")
    if _terminate_verified(pid, recorded):
        return f"sent SIGTERM to {pid}"
    result.update(status="cancelled", exit_code=EXIT["cancelled"], ended=_now().isoformat())
    job.write_json("result.json", result)
    return f"cancelled {job.id} (process not verified; nothing signalled)"


def _cmd_cancel(args) -> int:
    if args.all == (args.id is not None):
        raise UsageError("give a job id, or --all")
    mine = _repo_of(args.workdir)
    if args.all:
        rows = [r for r in list_jobs(paths(), ALL_JOBS) if r.get("status") == "running" and _job_repo(r) == mine]
        if not rows:
            print("no running jobs")
        for r in rows:
            print(_safe(_cancel_job(get_job(paths(), r["id"]))))
        return 0

    job = _load_job(args.id)
    owner = _job_repo(_read_result(job))
    if owner != mine:
        where = owner or "an unknown repository"
        print(_safe(f"job {job.id} belongs to {where}; run cancel from that repository", sys.stderr), file=sys.stderr)
        return 3
    print(_safe(_cancel_job(job)))
    return 0


def _cmd_wait(args) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    timeout_td = parse_duration(args.timeout) if args.timeout else parse_duration("9m")
    timeout_s = timeout_td.total_seconds()

    start_time = time.monotonic()

    target_job = None
    while time.monotonic() - start_time <= WAIT_APPEAR_S:
        for r in list_jobs(paths(), 100):
            job_workdir = r.get("workdir")
            if not job_workdir:
                continue
            if Path(job_workdir).expanduser().resolve() == workdir:
                target_job = _load_job(r["id"])
                break
        if target_job is not None:
            break
        time.sleep(WAIT_POLL_S)

    if target_job is None:
        print(_safe(f"agent-hub: no job for {args.workdir}", sys.stderr), file=sys.stderr)
        return 2

    while time.monotonic() - start_time <= timeout_s:
        try:
            result = target_job.read_json("result.json")
        except (OSError, ValueError):
            result = {"status": "running"}

        if result.get("status") != "running":
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                _emit(_summary(result))
            return result.get("exit_code", 1)

        time.sleep(WAIT_POLL_S)

    print(_safe(f"still running: {target_job.id}"))
    return 124


def _print_trail(trail_file: Path) -> int:
    if not trail_file.is_file():
        raise UsageError("decisions.tsv not found")
    rows = [line.split("\t") for line in trail_file.read_text().splitlines()]
    if not rows:
        return 0
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print(_safe("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()))
    return 0


def _claim_runner(state, plan_dir: Path) -> None:
    """Record this process as the plan's runner with its start time, so `plan cancel` can verify it.
    The pid also goes to `runner.pid`, which still names the runner when state.json cannot be read."""
    pid = os.getpid()
    state.runner = {"pid": pid, "start_time": start_time(pid)}
    state.save()
    (plan_dir / "runner.pid").write_text(json.dumps(state.runner))


def _stop_runner(pid: int | None, recorded: int | None) -> None:
    """SIGTERM the plan's runner when its start time verifies; say so either way."""
    if _terminate_verified(pid, recorded):
        print(_safe(f"sent SIGTERM to runner {pid}"))
    else:
        print(_safe("runner not verified; nothing signalled"))


def _cmd_plan_cancel(args, plan_file: Path) -> int:
    """Stop the plan's runner and its running jobs. Reads only the plan's repo and slug, so cancel still works
    after the plan's repository has been removed."""
    try:
        data = json.loads(plan_file.read_text())
    except (OSError, ValueError) as e:
        raise UsageError(f"cannot read plan: {e}") from None
    if not isinstance(data, dict) or "repo" not in data or "slug" not in data:
        raise UsageError("plan needs repo and slug")
    repo = str(Path(str(data["repo"])).resolve())
    slug = str(data["slug"])
    state_file = plan_file.parent / "state.json"

    code = 0
    if _repo_or_none(repo) is None:
        print(_safe(f"plan repository missing: {repo}", sys.stderr), file=sys.stderr)
        code = 3
    elif _repo_or_none(args.workdir) != _repo_or_none(repo):
        print(
            _safe(f"plan {slug} belongs to {repo}; run plan cancel from that repository", sys.stderr), file=sys.stderr
        )
        return 3

    if not state_file.is_file():
        raise UsageError("state.json not found")
    try:
        state_data = json.loads(state_file.read_text())
        if not isinstance(state_data, dict):
            raise ValueError("state is not an object")
    except (OSError, ValueError):
        # Running jobs cannot be found without the state, so only the runner is stopped.
        _stop_runner(*_read_pid_file(plan_file.parent / "runner.pid"))
        print(_safe(f"plan state unreadable: {state_file}", sys.stderr), file=sys.stderr)
        return 2

    runner = state_data.get("runner") or {}
    _stop_runner(runner.get("pid"), runner.get("start_time"))

    # The runner cancels its own children on SIGTERM; cancel the jobs still running in the plan's work trees.
    tasks = state_data.get("tasks", {})
    running = [name for name, t in tasks.items() if t.get("status") == "running"]
    worktrees = {Path(tasks[name]["worktree"]).resolve() for name in running if tasks[name].get("worktree")}
    for r in list_jobs(paths(), ALL_JOBS):
        if r.get("status") == "running" and r.get("workdir") and Path(r["workdir"]).resolve() in worktrees:
            print(_safe(_cancel_job(get_job(paths(), r["id"]))))
    for name in running:
        tasks[name].update(status="cancelled", reason="cancelled")
    state_file.write_text(json.dumps(state_data, indent=2) + "\n")
    return code


def _cmd_plan(args) -> int:
    plan_file = Path(args.plan_file).resolve()
    if args.plan_command == "cancel":
        return _cmd_plan_cancel(args, plan_file)
    plan = load_plan(plan_file)
    plan_dir = plan.path.parent
    state_file = plan_dir / "state.json"

    if args.plan_command == "run":
        from agenthub.planstate import load_or_init

        state = load_or_init(plan)
        _claim_runner(state, plan_dir)

        def spawn(argv: list[str], log: Path) -> subprocess.Popen:
            f = open(log, "wb")
            return subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)

        return run_plan(plan, state, spawn, args.max_parallel)

    elif args.plan_command == "status":
        if args.trail:
            return _print_trail(plan_dir / "decisions.tsv")

        if not state_file.is_file():
            raise UsageError("state.json not found")

        with open(state_file, "r") as f:
            state_data = json.load(f)

        if args.json:
            print(json.dumps(state_data, indent=2))
            return 0

        tasks = state_data.get("tasks", {})
        for t in plan.tasks:
            ts = tasks.get(t.name, {})
            status = ts.get("status", "pending")
            attempts = ts.get("attempts", [])
            branch = ts.get("branch", "")
            handoff = ts.get("handoff", "")

            line = f"{t.name}: {status}"
            if attempts:
                last = attempts[-1]
                line += f" ({len(attempts)} attempts, last: {last.get('status', '')})"
                gate_touched = last.get("gate_touched", [])
                if gate_touched:
                    line += f" gate touched: {len(gate_touched)}"
            if ts.get("escalated_from"):
                line += f" escalated from {ts['escalated_from']}"
            if branch:
                line += f" branch={branch}"
            if handoff:
                from agenthub.handoff import parse as parse_handoff

                handoff_path = plan_dir / handoff
                if handoff_path.is_file():
                    parsed = parse_handoff(handoff_path.read_text())
                    if parsed:
                        line += f" handoff={parsed['status']} · {parsed['verification']}"
            print(_safe(line))
        return 0

    elif args.plan_command == "wait":
        import argparse

        timeout_td = parse_duration(args.timeout) if args.timeout else parse_duration("9m")
        timeout_s = timeout_td.total_seconds()

        start_time = time.monotonic()

        while time.monotonic() - start_time <= PLAN_WAIT_APPEAR_S:
            if state_file.is_file():
                break
            time.sleep(PLAN_WAIT_POLL_S)
        else:
            print(_safe("agent-hub: state.json did not appear", sys.stderr), file=sys.stderr)
            return 2

        while time.monotonic() - start_time <= timeout_s:
            res = subprocess.run(
                [sys.executable, "-m", "agenthub", "plan", "status", str(plan_file), "--json"], capture_output=True
            )
            if res.returncode != 0:
                return res.returncode
            state_data = json.loads(res.stdout)
            tasks = state_data.get("tasks", {})
            if not any(ts.get("status") in ("pending", "running") for ts in tasks.values()):
                _cmd_plan(
                    argparse.Namespace(plan_command="status", plan_file=args.plan_file, json=args.json, trail=False)
                )
                if all(ts.get("status") == "done" for t, ts in tasks.items() if any(pt.name == t for pt in plan.tasks)):
                    return 0
                return 1
            time.sleep(PLAN_WAIT_POLL_S)

        print(_safe(f"still running: {plan.slug}"))
        return 124
