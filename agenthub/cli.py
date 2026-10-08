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

from agenthub.agents import registry
from agenthub.brief import BriefError
from agenthub.config import ConfigError, load_pool, parse_duration, paths
from agenthub.jobs import get_job, list_jobs
from agenthub.orchestrate import EXIT, RunArgs, run_job
from agenthub.plan import PlanError
from agenthub.plan import load as load_plan
from agenthub.planrun import run_plan
from agenthub.pool import Cooldowns, parse_link
from agenthub.procinfo import start_time

WAIT_POLL_S = 1.0
WAIT_APPEAR_S = 10.0
PLAN_WAIT_POLL_S = 1.0
PLAN_WAIT_APPEAR_S = float(os.environ.get("AGENT_HUB_PLAN_WAIT_APPEAR_S", "10.0"))


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
    run.add_argument("--role", required=True, choices=["implement", "review"])
    run.add_argument("--brief", help="brief file, or - for stdin (optional for review)")
    run.add_argument("-d", "--dir", required=True, dest="workdir")
    run.add_argument("--agent", help="use this single agent:model instead of the role's chain")
    run.add_argument("--base", help="review only: ref to compare against (default: the default branch)")
    run.add_argument("--timeout", help="e.g. 30m (default: the role's timeout)")
    run.add_argument("--gate", action="append", dest="gates", help="gate command to run (repeatable)")
    run.add_argument("--jev", action="store_true", help="use jev fallback for missing handoffs")
    run.add_argument("--json", action="store_true")

    pool = sub.add_parser("pool", help="show roles, chains and cooldowns")
    pool.add_argument("--json", action="store_true")
    pool.add_argument("--clear", metavar="AGENT:MODEL")

    profile = sub.add_parser("profile", help="print an agent's profile")
    profile.add_argument("agent")

    jobs = sub.add_parser("jobs", help="list recent jobs")
    jobs.add_argument("--limit", type=int, default=20)

    show = sub.add_parser("show", help="show a job")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")

    cancel = sub.add_parser("cancel", help="cancel a running job")
    cancel.add_argument("id")

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
    plan_wait = plan_sub.add_parser("wait")
    plan_wait.add_argument("plan_file", metavar="<plan.json>")
    plan_wait.add_argument("--timeout", help="e.g. 9m")
    plan_wait.add_argument("--json", action="store_true")

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
        }[args.command]
        return handler(args)
    except SystemExit as e:  # --help
        return e.code if isinstance(e.code, int) else 0
    except (UsageError, ConfigError, BriefError, PlanError) as e:
        print(_safe(f"agent-hub: {e}", sys.stderr), file=sys.stderr)
        return 2


def _read_brief(args) -> str:
    if args.brief is None:
        if args.role == "implement":
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
    if args.agent:
        links = [parse_link(args.agent)]
    else:
        if args.role not in pool.roles:
            raise ConfigError(f"no chain configured for role {args.role!r}")
        links = [parse_link(x) for x in pool.roles[args.role]]
    timeout = parse_duration(args.timeout) if args.timeout else pool.timeouts[args.role]
    text = _read_brief(args)
    run_args = RunArgs(
        role=args.role,
        brief=text,
        workdir=Path(args.workdir).expanduser().resolve(),
        links=links,
        timeout=timeout,
        base=args.base,
        gates=args.gates or [],
        jev=args.jev or os.environ.get("AGENT_HUB_JEV") == "1",
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

    roles: dict[str, list[dict]] = {}
    for role, chain in pool.roles.items():
        entries = []
        for text in chain:
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
        roles[role] = entries

    if args.json:
        print(json.dumps({"roles": roles}, indent=2))
        return 0
    for role, entries in roles.items():
        print(_safe(f"{role}:"))
        for e in entries:
            parts = [e["link"], "installed" if e["installed"] else "missing"]
            if e["version"]:
                parts.append(e["version"])
            if e["cooldown_until"]:
                parts.append(f"cooldown until {e['cooldown_until']}")
            print(_safe("  " + "  ".join(parts)))
    return 0


def _cmd_profile(args) -> int:
    adapter = registry().get(args.agent)
    if adapter is None:
        raise UsageError(f"unknown agent: {args.agent}")
    print(_safe(adapter.profile_path.read_text()), end="")
    return 0


def _cmd_jobs(args) -> int:
    for r in list_jobs(paths(), args.limit):
        agent = f"{r['agent']}:{r['model']}" if r.get("agent") else "-"
        dur = f"{r['duration_s']:.0f}s" if r.get("duration_s") is not None else "-"
        print(_safe("  ".join([r["id"], r["role"], r["status"], agent, dur, r["workdir"]])))
    return 0


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


def _cmd_cancel(args) -> int:
    job = _load_job(args.id)
    try:
        result = job.read_json("result.json")
    except (OSError, ValueError):
        result = {}
    if result.get("status") not in (None, "running"):
        print(_safe(f"job already finished: {result['status']}"))
        return 0
    pid, recorded = _read_pid_file(job.dir / "pid")
    if pid is not None and recorded is not None and start_time(pid) == recorded:
        try:
            os.kill(pid, signal.SIGTERM)
            print(_safe(f"sent SIGTERM to {pid}"))
            return 0
        except OSError:
            pass
    result.update(status="cancelled", exit_code=EXIT["cancelled"], ended=_now().isoformat())
    job.write_json("result.json", result)
    print(_safe(f"cancelled {job.id} (process not verified; nothing signalled)"))
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


def _cmd_plan(args) -> int:
    plan_file = Path(args.plan_file).resolve()
    plan = load_plan(plan_file)
    plan_dir = plan.path.parent
    state_file = plan_dir / "state.json"

    if args.plan_command == "run":
        from agenthub.planstate import load_or_init

        state = load_or_init(plan)

        def spawn(argv: list[str], log: Path) -> subprocess.Popen:
            f = open(log, "wb")
            return subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)

        return run_plan(plan, state, spawn, args.max_parallel)

    elif args.plan_command == "status":
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
                _cmd_plan(argparse.Namespace(plan_command="status", plan_file=args.plan_file, json=args.json))
                if all(ts.get("status") == "done" for t, ts in tasks.items() if any(pt.name == t for pt in plan.tasks)):
                    return 0
                return 1
            time.sleep(PLAN_WAIT_POLL_S)

        print(_safe(f"still running: {plan.slug}"))
        return 124
