import argparse
import fcntl
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenthub import guards
from agenthub.agents import registry
from agenthub.brief import BriefError, order_table
from agenthub.config import ConfigError, parse_duration, paths, role_kind
from agenthub.jobs import get_job, list_jobs
from agenthub.orchestrate import EXIT, RunArgs, readiness, run_gate_job, run_job, skills_hint
from agenthub.plan import PlanError
from agenthub.plan import load as load_plan
from agenthub.planrun import run_plan
from agenthub.pool import Cooldowns, load_pool, parse_link
from agenthub.procinfo import is_running, pid_file_dead, read_pid_file, start_time
from agenthub.workers import available_skills

WAIT_POLL_S = 1.0
WAIT_APPEAR_S = 10.0
PLAN_WAIT_POLL_S = 1.0
PLAN_WAIT_APPEAR_S = float(os.environ.get("AGENT_HUB_PLAN_WAIT_APPEAR_S", "10.0"))
ALL_JOBS = 100000  # every job record, for filters that must see past any --limit
SCHEDULE_UNIT = "agent-hub-skills-weekly"
SCHEDULE_DEFAULT_AT = "Sun *-*-* 20:17"
SCHEDULE_TIMEOUT_S = 30


class UsageError(Exception):
    pass


class ScheduleError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise UsageError(message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_write(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}_{time.time_ns()}")
    try:
        tmp.write_text(content)
        tmp.replace(path)
    except BaseException:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


class _TeeStream:
    def __init__(self, target):
        self._target = target
        self._buf = []

    def write(self, s: str):
        self._buf.append(s)
        return self._target.write(s)

    def flush(self):
        return self._target.flush()

    def isatty(self):
        return getattr(self._target, "isatty", lambda: False)()

    def fileno(self):
        return self._target.fileno()

    @property
    def encoding(self):
        return getattr(self._target, "encoding", "utf-8")

    def get_text(self) -> str:
        return "".join(self._buf)


@contextmanager
def _announcing(announce_path: str | None):
    if not announce_path:
        yield
        return
    import agenthub.orchestrate as orch

    orig_on_started = orch.on_started

    def announce(job_id: str) -> None:
        try:
            orig_on_started(job_id)
        finally:
            try:
                _atomic_write(Path(announce_path), f"{job_id}\n")
            except Exception as e:
                print(_safe(f"agent-hub: announce failed: {e}", sys.stderr), file=sys.stderr)

    orch.on_started = announce
    try:
        yield
    finally:
        orch.on_started = orig_on_started


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
        "--exclude",
        action="append",
        default=[],
        metavar="AGENT:MODEL[@EFFORT]",
        help="skip this link (repeatable; effort ignored when matching)",
    )
    run.add_argument(
        "--skill", action="append", default=[], dest="skills", metavar="NAME", help="worker skill to load (repeatable)"
    )
    run.add_argument("--announce", help=argparse.SUPPRESS)
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
    plan_review = plan_sub.add_parser(
        "review", help="gate and review each done task's HEAD (exit 0 only if every reviewed task is approved)"
    )
    plan_review.add_argument("plan_file", metavar="<plan.json>")
    plan_review.add_argument("--task", metavar="NAME", help="review only this task")
    plan_review.add_argument("--result", metavar="FILE", help=argparse.SUPPRESS)
    plan_triage = plan_sub.add_parser(
        "triage", help="triage every task that needs it, once (exit 1 when any task is blocked)"
    )
    plan_triage.add_argument("plan_file", metavar="<plan.json>")
    plan_merge = plan_sub.add_parser(
        "merge",
        help="merge each done task that agent-hub ready accepts, in dependency order (exit 1 at the first not ready)",
    )
    plan_merge.add_argument("plan_file", metavar="<plan.json>")
    plan_merge.add_argument("--into", metavar="BRANCH", help="branch to merge into (default: the plan's base)")
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

    hub = sub.add_parser("hub", help="worker plugins in the installed hosts (codex, agy)")
    hub_sub = hub.add_subparsers(dest="hub_command", required=True, parser_class=_Parser)
    hub_status = hub_sub.add_parser("status", help="show per host and plugin whether it is installed and current")
    hub_status.add_argument("--json", action="store_true")
    hub_install = hub_sub.add_parser("install", help="install or refresh worker plugins (dry-run unless --apply)")
    hub_install.add_argument("--apply", action="store_true", help="run the commands")
    hub_install.add_argument("--host", action="append", choices=["codex", "agy"], help="only this host (repeatable)")

    skills = sub.add_parser("skills", help="learn skills from past work")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True, parser_class=_Parser)
    mine = skills_sub.add_parser("mine", help="extract what repeats from job records and transcripts, redacted")
    mine.add_argument("--since", help="start of the window, a date or ISO timestamp")
    mine.add_argument("--days", type=int, help="window of the last N days (overrides --since)")
    mine.add_argument("--out", help="output directory (default: <state>/skills/<date>)")
    mine.add_argument("--transcripts", help="Claude Code projects directory (default: ~/.claude/projects)")
    mine.add_argument("--json", action="store_true")
    weekly = skills_sub.add_parser("weekly", help="mine, draft, eval, review and release learned skills (local only)")
    weekly.add_argument("--dry-run", action="store_true", help="print the steps; run nothing, write nothing")
    weekly.add_argument("--max-cost-usd", type=float, default=3.0, help="eval budget in USD (default 3)")
    weekly.add_argument("--transcripts", help="Claude Code projects directory (default: ~/.claude/projects)")
    skills_sub.add_parser("report", help="print the newest weekly report")
    schedule = skills_sub.add_parser(
        "schedule", help="run skills weekly on a systemd user timer (dry-run unless --apply)"
    )
    schedule_mode = schedule.add_mutually_exclusive_group()
    schedule_mode.add_argument("--apply", action="store_true", help="write the units and enable the timer")
    schedule_mode.add_argument("--remove", action="store_true", help="disable the timer and delete its units")
    schedule.add_argument(
        "--at", default=SCHEDULE_DEFAULT_AT, help='systemd calendar expression (default "Sun *-*-* 20:17")'
    )

    gate = sub.add_parser("gate", help="run the gates on the committed HEAD and record them (exit 9 if one fails)")
    gate.add_argument("-d", "--dir", default=".", dest="workdir", help="clean work tree to gate")
    gate.add_argument("--gate", action="append", dest="gates", help="gate command to run after the repo gates")
    gate.add_argument("--announce", help=argparse.SUPPRESS)
    gate.add_argument("--json", action="store_true")

    sub.add_parser("mcp", help="run the MCP server over stdio")

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
            "hub": _cmd_hub,
            "skills": _cmd_skills,
            "mcp": _cmd_mcp,
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
        link = parse_link(args.agent)
        if link.effort and link.agent != "claude":
            raise ConfigError(f"effort is only supported for claude links: {args.agent}")
        links = [link]
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
    with _announcing(getattr(args, "announce", None)):
        result = run_job(run_args, p, pool, _now)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _emit(_summary(result))
    return EXIT[result["status"]]


def _cmd_hub(args) -> int:
    from agenthub import hub

    config_dir = paths().config_dir
    if args.hub_command == "status":
        rows = hub.status(config_dir)
        print(_safe(hub.rows_json(rows) if args.json else hub.format_rows(rows)))
        return 0
    return hub.install(config_dir, args.apply, tuple(args.host or hub.HOSTS), emit=lambda text: print(_safe(text)))


def _skills_repo(config_dir: Path) -> Path:
    import tomllib

    try:
        data = tomllib.loads((config_dir / "workers.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UsageError(f"cannot read workers.toml: {e}") from None
    hub = data.get("hub")
    directory = hub.get("codex_marketplace") if isinstance(hub, dict) else None
    if not isinstance(directory, str):
        raise UsageError("workers.toml needs [hub] codex_marketplace = the skills repo")
    return Path(directory).expanduser().resolve()


def _cmd_skills_weekly(args) -> int:
    from agenthub import skillweekly

    if not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0:
        raise UsageError("--max-cost-usd must be a finite positive number")
    p = paths()
    default_transcripts = Path(os.environ.get("HOME", "~")).expanduser() / ".claude" / "projects"
    transcripts = Path(args.transcripts) if args.transcripts else default_transcripts
    cfg = skillweekly.Config(
        state_dir=p.state_dir,
        skills_repo=_skills_repo(p.config_dir),
        transcripts=transcripts,
        max_cost_usd=args.max_cost_usd,
    )
    report = skillweekly.weekly(cfg, _now(), skillweekly.run_command, args.dry_run)
    if args.dry_run:
        print(_safe(report.render()), end="")
    else:
        print(_safe(report.one_line()))
        if report.exit_code != 3:
            print(_safe(f"report: {cfg.state_dir / 'skills' / report.date / 'report.md'}"))
    return report.exit_code


def _cmd_skills_report() -> int:
    reports = sorted((paths().state_dir / "skills").glob("*/report.md"), key=lambda f: f.parent.name)
    if not reports:
        print("agent-hub: no weekly report yet", file=sys.stderr)
        return 1
    print(_safe(reports[-1].read_text(encoding="utf-8", errors="replace")), end="")
    return 0


def _unit_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path(os.environ.get("HOME", "~")).expanduser() / ".config"
    return Path(config_home) / "systemd" / "user"


def _systemd_word(value: str, *, exec_line: bool = False) -> str:
    """Quote a unit-file value only when it has characters systemd would split or expand.

    Only ExecStart= expands $VARS, so a literal $ is doubled there and nowhere else.
    """
    specials = " \t\"'\\%$" if exec_line else " \t\"'\\%"
    if not any(c in value for c in specials):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if exec_line:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def _unit_texts(exe: str, at: str) -> tuple[str, str]:
    service = (
        "[Unit]\n"
        "Description=agent-hub learned skills, weekly\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={_systemd_word(exe, exec_line=True)} skills weekly\n"
        f"Environment={_systemd_word('PATH=' + os.environ.get('PATH', ''))}\n"
        "TimeoutStartSec=6h\n"
        "Nice=10\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run agent-hub learned skills weekly\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={at}\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _schedule_run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=SCHEDULE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ScheduleError(f"{argv[0]}: {e}") from None


def _systemctl(*args: str) -> None:
    done = _schedule_run(["systemctl", "--user", *args])
    if done.returncode != 0:
        raise ScheduleError((done.stderr or done.stdout).strip() or f"systemctl --user {' '.join(args)} failed")


def _check_calendar(at: str) -> None:
    if not at.strip() or "\n" in at:
        raise UsageError(f"invalid --at: {at!r}")
    # "--" keeps a value such as --version from being read as an option
    done = _schedule_run(["systemd-analyze", "calendar", "--", at])
    if done.returncode != 0:
        raise UsageError(f"invalid --at {at!r}: {(done.stderr or done.stdout).strip()}")


def _read_unit(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _cmd_skills_schedule(args) -> int:
    unit_dir = _unit_dir()
    service_path = unit_dir / f"{SCHEDULE_UNIT}.service"
    timer_path = unit_dir / f"{SCHEDULE_UNIT}.timer"
    timer = timer_path.name
    try:
        if args.remove:
            if not (service_path.exists() or timer_path.exists()):
                print("nothing scheduled")
                return 0
            # remove the files even when disable fails, so a retry is not stuck on a half-removed timer
            failures = []
            try:
                _systemctl("disable", "--now", timer)
            except ScheduleError as e:
                failures.append(f"disable {timer}: {e}")
            for path in (service_path, timer_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    failures.append(f"unlink {path}: {e}")
            try:
                _systemctl("daemon-reload")
            except ScheduleError as e:
                failures.append(f"daemon-reload: {e}")
            if failures:
                raise ScheduleError("; ".join(failures))
            print(_safe(f"removed: {timer_path}"))
            return 0

        _check_calendar(args.at)
        exe = shutil.which("agent-hub")
        if exe is None:
            raise ScheduleError("agent-hub is not on PATH")
        service, timer_text = _unit_texts(os.path.abspath(exe), args.at)
        if not args.apply:
            print(_safe(f"# {service_path}\n{service}\n# {timer_path}\n{timer_text}"), end="")
            print(f"would run: systemctl --user daemon-reload; systemctl --user enable --now {timer}")
            print("dry run: nothing written; add --apply to schedule")
            return 0

        before = {path: _read_unit(path) for path in (service_path, timer_path)}
        timer_existed = before[timer_path] is not None
        was_enabled = timer_existed and _schedule_run(["systemctl", "--user", "is-enabled", timer]).returncode == 0
        was_active = timer_existed and _schedule_run(["systemctl", "--user", "is-active", timer]).returncode == 0
        files_match = before[service_path] == service and before[timer_path] == timer_text
        if files_match and was_enabled and was_active:
            print(_safe(f"already scheduled: {timer_path} ({args.at})"))
            return 0
        unit_dir.mkdir(parents=True, exist_ok=True)
        try:
            service_path.write_text(service, encoding="utf-8")
            timer_path.write_text(timer_text, encoding="utf-8")
            _systemctl("daemon-reload")
            _systemctl("enable", "--now", timer)
        except (ScheduleError, OSError) as orig_err:
            # all-or-nothing: stop the timer, put back what was there before this attempt (new files are
            # removed), then reload so systemd forgets the rolled-back units; errors here must not mask the first
            with suppress(ScheduleError):
                _systemctl("disable", "--now", timer)
            restore_errors: list[str] = []
            for path, text in before.items():
                try:
                    if text is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(text, encoding="utf-8")
                except OSError as e:
                    restore_errors.append(f"restore {path}: {e}")
            with suppress(ScheduleError):
                _systemctl("daemon-reload")
            if was_enabled:
                with suppress(ScheduleError):
                    _systemctl("enable", timer)
            if was_active:
                with suppress(ScheduleError):
                    _systemctl("restart", timer)
            print(_safe(f"agent-hub: {orig_err}", sys.stderr), file=sys.stderr)
            for msg in restore_errors:
                print(_safe(f"agent-hub: {msg}", sys.stderr), file=sys.stderr)
            return 1
        print(_safe(f"scheduled: {timer_path} ({args.at})"))
        return 0
    except (ScheduleError, OSError) as e:
        print(_safe(f"agent-hub: {e}", sys.stderr), file=sys.stderr)
        return 1


def _cmd_skills(args) -> int:
    from agenthub import skillmine

    if args.skills_command == "weekly":
        return _cmd_skills_weekly(args)
    if args.skills_command == "report":
        return _cmd_skills_report()
    if args.skills_command == "schedule":
        return _cmd_skills_schedule(args)
    state_dir = paths().state_dir
    now = _now()
    since = _mine_window(args, state_dir, now)
    if args.transcripts:
        transcripts = Path(args.transcripts)
    else:
        transcripts = Path(os.environ.get("HOME", "~")).expanduser() / ".claude" / "projects"
    out = Path(args.out) if args.out else state_dir / "skills" / now.strftime("%Y-%m-%d")
    extract = skillmine.mine(state_dir, transcripts, since, now)
    try:
        summary = skillmine.write(extract, out)
    except OSError as e:
        raise UsageError(f"cannot write {out}: {e.strerror or e}") from None
    text = _safe(json.dumps(extract.to_json(), indent=2, ensure_ascii=False) if args.json else summary)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="backslashreplace").decode(encoding))
    return 0


def _mine_window(args, state_dir: Path, now: datetime) -> datetime:
    if args.days is not None:
        if not 1 <= args.days <= 3650:
            raise UsageError("--days must be between 1 and 3650")
        return now - timedelta(days=args.days)
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError:
            raise UsageError(f"invalid --since: {args.since!r}") from None
        return since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    since = now - timedelta(days=28)
    try:
        last_run = (state_dir / "skills" / "last-run").read_text(encoding="utf-8", errors="replace")
        last = datetime.fromisoformat(last_run.strip())
    except (OSError, ValueError):
        return since
    return min(since, last if last.tzinfo else last.replace(tzinfo=timezone.utc))


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
    with _announcing(getattr(args, "announce", None)):
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
    return read_pid_file(path)


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

        pid_file = target_job.dir / "pid"
        if pid_file_dead(pid_file):
            try:
                result = target_job.read_json("result.json")
            except (OSError, ValueError):
                pass
            if result.get("status") == "running":
                pid, _ = read_pid_file(pid_file)
                print(_safe(f"job process {pid} is not running; result.json was never finished"))
                return 1
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


def _live_runner(plan_dir: Path) -> int | None:
    """Return the recorded runner pid when it is alive and verified (is_running and matching
    start_time, not this process), else None. Unreadable file -> None."""
    state_file = plan_dir / "state.json"
    try:
        data = json.loads(state_file.read_text())
        if not isinstance(data, dict):
            return None
    except (OSError, ValueError):
        return None
    runner = data.get("runner")
    if not isinstance(runner, dict):
        return None
    pid = runner.get("pid")
    recorded = runner.get("start_time")
    if (
        isinstance(pid, int)
        and recorded is not None
        and pid != os.getpid()
        and is_running(pid)
        and start_time(pid) == recorded
    ):
        return pid
    return None


def _claim_runner(state, plan_dir: Path, plan=None) -> None:
    """Record this process as the plan's runner with its start time, so `plan cancel` can verify it.
    Refuses if another verified runner process is still running.
    The pid also goes to `runner.pid`, which still names the runner when state.json cannot be read.
    Callers should pass `plan` to refresh runner and task state under the lock."""
    with open(plan_dir / "runner.lock", "a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            raise UsageError("plan is busy (triage in progress); try again later") from None
        try:
            live = _live_runner(plan_dir)
            if live is not None:
                raise UsageError(f"plan runner {live} is still running; wait or cancel it first")
            if plan is not None:
                from agenthub.planstate import load_or_init

                fresh = load_or_init(plan)
                state.runner = fresh.runner
                state.tasks = fresh.tasks
            pid = os.getpid()
            state.runner = {"pid": pid, "start_time": start_time(pid)}
            state.save()
            (plan_dir / "runner.pid").write_text(json.dumps(state.runner))
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


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


def _task_head(tstate) -> tuple[str | None, str | None]:
    """Read a task's HEAD without guards.inspect; returns (head, None) or (None, problem)."""
    if not tstate.worktree or not Path(tstate.worktree).is_dir():
        return None, f"work tree missing: {tstate.worktree}"
    try:
        return guards._run_git(Path(tstate.worktree), "rev-parse", "HEAD"), None
    except (subprocess.CalledProcessError, OSError):
        return None, f"cannot read HEAD: {tstate.worktree}"


def _cmd_plan(args) -> int:
    plan_file = Path(args.plan_file).resolve()
    if args.plan_command == "cancel":
        return _cmd_plan_cancel(args, plan_file)
    plan = load_plan(plan_file)
    plan_dir = plan.path.parent
    state_file = plan_dir / "state.json"

    if args.plan_command == "run":
        from agenthub.planstate import load_or_init

        known_skills = available_skills(paths().config_dir) if any(t.skills for t in plan.tasks) else {}
        for task in plan.tasks:
            for skill in task.skills:
                if skill not in known_skills:
                    raise UsageError(f"task {task.name}: unknown skill {skill}: {skills_hint(known_skills)}")

        state = load_or_init(plan)
        _claim_runner(state, plan_dir, plan=plan)

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

    elif args.plan_command == "review":
        result_path = Path(args.result) if getattr(args, "result", None) else None
        orig_stdout, orig_stderr = sys.stdout, sys.stderr
        tee_out, tee_err = None, None
        if result_path is not None:
            tee_out = _TeeStream(orig_stdout)
            tee_err = _TeeStream(orig_stderr)
            sys.stdout, sys.stderr = tee_out, tee_err

        ret_code = 1
        err_thrown = None
        try:
            from agenthub.planrun import review_task
            from agenthub.planstate import load_or_init

            if not state_file.is_file():
                raise UsageError("state.json not found")
            state = load_or_init(plan)
            tasks = [t for t in plan.tasks if (t.kind or t.role) == "implement" and state.task(t.name).status == "done"]
            if args.task is not None:
                tasks = [t for t in tasks if t.name == args.task]
                if not tasks:
                    raise UsageError(f"no done implement task named {args.task}")
            if not tasks:
                print("no done tasks to review", file=sys.stderr)
                ret_code = 1
                return ret_code

            all_approved = True
            for task in tasks:
                result = review_task(plan, task, state.task(task.name), _now)
                if result.get("up_to_date"):
                    print(_safe(f"{task.name} up to date (review job={result['job']})"))
                    continue
                all_approved = all_approved and result["review"] == "approve"
                print(_safe(f"{task.name} gate={result['gate']} review={result['review']} job={result['job']}"))
                if result["reason"]:
                    print(_safe(result["reason"]), file=sys.stderr)
            ret_code = 0 if all_approved else 1
            return ret_code
        except BaseException as e:
            err_thrown = e
            ret_code = 2 if isinstance(e, (UsageError, ConfigError, BriefError, PlanError)) else 1
            raise
        finally:
            if result_path is not None:
                sys.stdout, sys.stderr = orig_stdout, orig_stderr
                out_text = tee_out.get_text() if tee_out else ""
                err_text = tee_err.get_text() if tee_err else ""
                lines = [line.strip() for line in out_text.splitlines() if line.strip()]
                if not err_text and err_thrown is not None:
                    err_text = str(err_thrown)
                err_lines = err_text.splitlines()
                stderr_tail = "\n".join(err_lines[-40:]).strip()
                record = {
                    "status": "ok" if ret_code == 0 else "failed",
                    "exit_code": ret_code,
                    "lines": lines,
                    "stderr_tail": stderr_tail,
                }
                _atomic_write(result_path, json.dumps(record, indent=2) + "\n")

    elif args.plan_command == "triage":
        from agenthub import triagerun
        from agenthub.planstate import load_or_init

        if not state_file.is_file():
            raise UsageError("state.json not found")
        with open(plan_dir / "runner.lock", "a") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                raise UsageError("plan is busy (triage in progress); try again later") from None
            try:
                live = _live_runner(plan_dir)
                if live is not None:
                    raise UsageError(f"plan runner {live} is still running; wait or cancel it first")
                state = load_or_init(plan)
                tasks = [
                    t for t in plan.tasks if (t.kind or t.role) == "implement" and state.task(t.name).status == "done"
                ]
                jobs = list_jobs(paths(), ALL_JOBS)
                triaged_count = 0
                skipped_count = 0
                any_blocked = False

                interrupted = False

                def _handle_signal(signum, frame):
                    nonlocal interrupted
                    interrupted = True

                old_sigint = signal.signal(signal.SIGINT, _handle_signal)
                old_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
                try:
                    for task in tasks:
                        tstate = state.task(task.name)
                        if not tstate.worktree:
                            print(_safe(f"{task.name} skipped: no work tree recorded"))
                            skipped_count += 1
                            if interrupted:
                                break
                            continue
                        head, problem = _task_head(tstate)
                        if problem:
                            print(_safe(f"{task.name} skipped: {problem}"))
                            skipped_count += 1
                            if interrupted:
                                break
                            continue
                        result = triagerun.triage_task(plan, task, tstate, jobs, head, _now)
                        if (
                            result is not None
                            and result.get("status") == "blocked"
                            and result.get("reason") == "triage job cancelled"
                        ):
                            interrupted = True
                        if interrupted:
                            if (
                                result is not None
                                and result.get("status") == "blocked"
                                and result.get("reason") == "triage job cancelled"
                            ):
                                tstate.status = "done"
                                tstate.reason = ""
                            state.save()
                            break
                        if result is None:
                            continue
                        triaged_count += 1
                        state.save()
                        if result.get("status") == "blocked":
                            any_blocked = True
                            print(_safe(f"{task.name} blocked: {result.get('reason', '')}"))
                        else:
                            counts = result.get("counts") or {}
                            fix = counts.get("fix", 0)
                            reject = counts.get("reject", 0)
                            blocked = counts.get("blocked", 0)
                            brief_part = f" brief={result['fix_path']}" if result.get("fix_path") else ""
                            print(_safe(f"{task.name} fix={fix} reject={reject} blocked={blocked}{brief_part}"))
                finally:
                    signal.signal(signal.SIGINT, old_sigint)
                    signal.signal(signal.SIGTERM, old_sigterm)

                if interrupted:
                    return 130
                if skipped_count > 0:
                    print(_safe(f"skipped {skipped_count} task(s)"))
                    return 1
                if triaged_count == 0:
                    print(_safe("nothing to triage"))
                    return 0
                return 1 if any_blocked else 0
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    elif args.plan_command == "merge":
        from agenthub.planrun import merge_order, merge_target_problem, merge_task
        from agenthub.planstate import load_or_init

        if not state_file.is_file():
            raise UsageError("state.json not found")
        state = load_or_init(plan)
        target = args.into or plan.base
        tasks = [
            t for t in merge_order(plan) if (t.kind or t.role) == "implement" and state.task(t.name).status == "done"
        ]
        if not tasks:
            print("no done tasks to merge", file=sys.stderr)
            return 1
        try:
            problem = merge_target_problem(plan.repo, target)
        except guards.GuardError as e:
            raise UsageError(str(e)) from None
        if problem:
            print(_safe(f"agent-hub: {problem}", sys.stderr), file=sys.stderr)
            return 3

        jobs = list_jobs(paths(), ALL_JOBS)
        for task in tasks:
            result = merge_task(plan, task, state.task(task.name), target, jobs)
            if result["status"] == "merged":
                print(_safe(f"{task.name} merged into {target} at {result['sha']}"))
            elif result["status"] == "already-merged":
                print(_safe(f"{task.name} already merged"))
            elif result["status"] == "not-ready":
                for problem in result["problems"]:
                    print(_safe(f"{task.name} not ready: {problem}"))
                return 1
            else:
                print(_safe(f"{task.name} merge failed: {result['reason']}", sys.stderr), file=sys.stderr)
                return 1
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


def _cmd_mcp(args) -> int:
    try:
        import mcp.server.mcpserver  # noqa: F401
    except (ImportError, ModuleNotFoundError):
        raise UsageError("mcp SDK not installed; install with: uv tool install 'agent-hub[mcp]'") from None

    from agenthub.mcpserver import run_stdio

    run_stdio()
    return 0
