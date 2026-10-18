import asyncio
import contextlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agenthub import brief as briefmod
from agenthub import guards
from agenthub.agents import registry
from agenthub.cli import ALL_JOBS, WAIT_APPEAR_S, UsageError, _job_repo, _repo_of
from agenthub.config import ConfigError, Paths, parse_duration, paths, role_kind
from agenthub.jobs import get_job, list_jobs
from agenthub.orchestrate import readiness, skills_hint
from agenthub.plan import load as load_plan
from agenthub.pool import Cooldowns, load_pool, parse_link
from agenthub.procinfo import is_running, pid_file_dead, read_pid_file, start_time
from agenthub.workers import available_skills

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


def validate_job_id(job_id: str, p: Paths) -> Path:
    if not job_id or not str(job_id).strip():
        raise UsageError("job_id is required")
    if not isinstance(job_id, str):
        raise KeyError(f"unknown job: {job_id}")
    if any(c in job_id for c in ("/", "\\", "\0")) or job_id in (".", ".."):
        raise KeyError(f"unknown job: {job_id}")
    jobs_dir = p.jobs_dir.resolve()
    try:
        job_dir = (p.jobs_dir / job_id).resolve()
        if job_dir.parent != jobs_dir:
            raise KeyError(f"unknown job: {job_id}")
    except (ValueError, OSError, RuntimeError):
        raise KeyError(f"unknown job: {job_id}") from None
    return job_dir


def pool_data(p: Paths | None = None, now: datetime | None = None) -> dict[str, Any]:
    p = p or paths()
    pool = load_pool(p)
    cooldowns = Cooldowns(p.cooldowns_file)
    adapters = registry()
    now = now or datetime.now(timezone.utc)

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

    return {"roles": roles}


def jobs_data(
    repo_dir: str = ".",
    all: bool = False,
    limit: int = 20,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    mine = None if all else _repo_of(repo_dir)
    rows = [r for r in list_jobs(p, ALL_JOBS) if all or _job_repo(r) == mine][:limit]
    return {"jobs": rows}


def show_job_data(job_id: str, p: Paths | None = None) -> dict[str, Any]:
    p = p or paths()
    validate_job_id(job_id, p)
    job = get_job(p, job_id)
    return job.read_json("result.json")


def clamp_timeout(timeout_s: Any) -> int:
    if isinstance(timeout_s, bool):
        raise UsageError(f"timeout cannot be boolean: {timeout_s}")
    if isinstance(timeout_s, (int, float)):
        if not math.isfinite(timeout_s):
            raise UsageError(f"timeout must be finite: {timeout_s}")
        val = int(timeout_s)
    elif isinstance(timeout_s, str):
        try:
            val = int(timeout_s)
        except ValueError:
            try:
                val = int(parse_duration(timeout_s).total_seconds())
            except (ConfigError, ValueError):
                raise UsageError(f"invalid timeout: {timeout_s!r}") from None
    elif timeout_s is None:
        val = 60
    else:
        raise UsageError(f"invalid timeout type: {type(timeout_s).__name__}")
    return max(1, min(300, val))


def validate_run_args(
    workdir: str,
    role: str | None = None,
    agent: str | None = None,
    brief_path: str | None = None,
    skills: list[str] | None = None,
    p: Paths | None = None,
) -> tuple[Path, str, str]:
    if not workdir or not str(workdir).strip():
        raise UsageError("the following arguments are required: -d/--dir")
    workdir_path = Path(workdir).expanduser().resolve()

    if role is None:
        raise UsageError("the following arguments are required: --role")

    p = p or paths()
    pool = load_pool(p)
    kind = role_kind(pool, role)

    if agent is not None:
        link = parse_link(agent)
        if link.effort and link.agent != "claude":
            raise ConfigError(f"effort is only supported for claude links: {agent}")
        links = [link]
    else:
        links = [parse_link(x) for x in pool.roles[role].chain]

    adapters = registry()
    for link in links:
        if link.agent not in adapters:
            raise ConfigError(f"unknown agent in chain: {link.agent}")

    if brief_path is None:
        if kind == "implement":
            raise UsageError("--brief is required for implement")
        brief_text = ""
    else:
        bp = Path(brief_path)
        if not bp.is_absolute() or str(brief_path) == "-":
            raise UsageError(f"brief_path must be an absolute path: {brief_path}")
        if not bp.is_file():
            raise UsageError(f"brief_path must be an existing regular file: {brief_path}")
        try:
            brief_text = bp.read_text()
        except OSError as e:
            raise UsageError(f"cannot read brief: {e}") from None

    briefmod.validate(kind, brief_text)

    if skills:
        known_skills = available_skills(p.config_dir)
        for s in skills:
            if s not in known_skills:
                raise ConfigError(f"unknown skill {s!r}: {skills_hint(known_skills)}")

    return workdir_path, kind, brief_text


def run_job(
    workdir: str,
    role: str | None = None,
    agent: str | None = None,
    brief_path: str | None = None,
    gates: list[str] | None = None,
    skills: list[str] | None = None,
    base: str | None = None,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    gates = gates or []
    skills = skills or []

    workdir_path, _kind, _ = validate_run_args(
        workdir=workdir,
        role=role,
        agent=agent,
        brief_path=brief_path,
        skills=skills,
        p=p,
    )

    cmd = [
        sys.executable,
        "-P",
        "-m",
        "agenthub",
        "run",
        "-d",
        str(workdir_path),
        "--role",
        role,
    ]
    if agent is not None:
        cmd.extend(["--agent", agent])
    if brief_path is not None:
        cmd.extend(["--brief", str(brief_path)])
    if base is not None:
        cmd.extend(["--base", base])
    for g in gates:
        cmd.extend(["--gate", g])
    for s in skills:
        cmd.extend(["--skill", s])
    cmd.append("--json")

    return _spawn_detached(cmd=cmd, workdir_path=workdir_path, p=p, log_prefix="mcp-run")


def _spawn_detached(
    cmd: list[str],
    workdir_path: Path,
    p: Paths,
    log_prefix: str = "mcp-run",
) -> dict[str, Any]:
    env = os.environ.copy()

    p.state_dir.mkdir(parents=True, exist_ok=True)
    log_file = p.state_dir / f"{log_prefix}-{time.time_ns()}.log"

    full_cmd = list(cmd)
    is_temp_announce = False
    if "--announce" in full_cmd:
        announce_file = Path(full_cmd[full_cmd.index("--announce") + 1])
    else:
        announce_file = p.state_dir / f"{log_prefix}-announce-{time.time_ns()}.txt"
        full_cmd.extend(["--announce", str(announce_file)])
        is_temp_announce = True

    spawn_monotonic = time.monotonic()

    try:
        with open(log_file, "w") as log_fd:
            proc = subprocess.Popen(
                full_cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
                cwd=p.state_dir,
            )
    except OSError as e:
        return {"error": f"spawn failed: {e} (log: {log_file})", "log": str(log_file)}

    poll_deadline = spawn_monotonic + 60.0
    target_job_id = None

    while time.monotonic() < poll_deadline:
        if announce_file.is_file():
            try:
                content = announce_file.read_text().strip()
                if content:
                    target_job_id = content
                    break
            except OSError:
                pass

        if proc.poll() is not None:
            if announce_file.is_file():
                try:
                    content = announce_file.read_text().strip()
                    if content:
                        target_job_id = content
                        break
                except OSError:
                    pass
            if is_temp_announce and announce_file.is_file():
                try:
                    announce_file.unlink(missing_ok=True)
                except OSError:
                    pass
            tail = ""
            if log_file.is_file():
                try:
                    lines = log_file.read_text(errors="replace").splitlines()
                    tail = "\n".join(lines[-40:]).strip()
                except OSError:
                    pass
            prefix = f"child exited with code {proc.returncode} before announcing a job"
            msg = f"{prefix}\n{tail}" if tail else prefix
            return {"error": f"{msg} (log: {log_file})", "log": str(log_file)}

        time.sleep(0.05)

    if target_job_id is None:
        # Timeout: terminate the child's process group and clean up the announce file
        import signal

        pgid = None
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pass

        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
        else:
            try:
                proc.terminate()
            except OSError:
                pass

        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, Exception):
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
            else:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait()
            except Exception:
                pass

        try:
            announce_file.unlink(missing_ok=True)
        except OSError:
            pass
        err_msg = ""
        if log_file.is_file():
            try:
                lines = log_file.read_text(errors="replace").splitlines()
                err_msg = "\n".join(lines[-40:]).strip()
            except OSError:
                pass
        msg = err_msg or "job record did not appear within 60s"
        return {"error": f"{msg} (log: {log_file})", "log": str(log_file)}

    threading.Thread(target=proc.wait, daemon=True).start()

    if is_temp_announce and announce_file.is_file():
        try:
            announce_file.unlink(missing_ok=True)
        except OSError:
            pass

    res: dict[str, Any] = {"job_id": target_job_id, "workdir": str(workdir_path)}
    job_dir = p.jobs_dir / target_job_id
    if log_file.is_file():
        if job_dir.is_dir():
            try:
                log_file.replace(job_dir / "spawn.log")
            except OSError:
                res["log"] = str(log_file)
        else:
            res["log"] = str(log_file)

    return res


def validate_gate_args(workdir: str) -> Path:
    if not workdir or not str(workdir).strip():
        raise UsageError("the following arguments are required: -d/--dir")
    return Path(workdir).expanduser().resolve()


def gate_start(
    workdir: str,
    gates: list[str] | None = None,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    gates = gates or []
    workdir_path = validate_gate_args(workdir)

    cmd = [
        sys.executable,
        "-P",
        "-m",
        "agenthub",
        "gate",
        "-d",
        str(workdir_path),
    ]
    for g in gates:
        cmd.extend(["--gate", g])
    cmd.append("--json")

    return _spawn_detached(cmd=cmd, workdir_path=workdir_path, p=p, log_prefix="mcp-gate")


def ready(workdir: str, p: Paths | None = None) -> dict[str, Any]:
    if not workdir or not str(workdir).strip():
        raise UsageError("the following arguments are required: -d/--dir")
    p = p or paths()
    try:
        repo = guards.inspect(Path(workdir).expanduser().resolve())
    except guards.GuardError as e:
        raise UsageError(str(e)) from None

    head = guards.head(repo)
    jobs = list_jobs(p, 100000)
    problems = readiness(jobs, repo.root, head)
    if problems:
        return {"ready": False, "problems": problems}
    return {"ready": True, "sha": head}


def _gate_summary(gates: list[dict] | None) -> str | None:
    if not gates:
        return None
    failed = next((g for g in gates if g.get("rc") != 0), None)
    if failed:
        return f"FAILED: {failed.get('cmd')} (rc {failed.get('rc')})"
    return f"passed ({len(gates)})"


def _format_wait_result(result: dict[str, Any]) -> dict[str, Any]:
    res = dict(result)
    res["gate_summary"] = _gate_summary(result.get("gate"))
    handoff = result.get("handoff")
    if isinstance(handoff, dict):
        res["handoff_status"] = handoff.get("status")
        res["handoff_verification"] = handoff.get("verification")
        res["handoff_summary"] = f"{handoff.get('status')} · {handoff.get('verification')}"
    else:
        res["handoff_status"] = None
        res["handoff_verification"] = None
        res["handoff_summary"] = None
    res.setdefault("state", result.get("status"))
    res.setdefault("job_id", result.get("id"))
    return res


def wait_job(
    job_id: str,
    timeout_s: int = 60,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    validate_job_id(job_id, p)

    timeout = clamp_timeout(timeout_s)
    start_mono = time.monotonic()

    target_job = None
    while time.monotonic() - start_mono <= min(float(timeout), WAIT_APPEAR_S):
        try:
            target_job = get_job(p, job_id)
            break
        except KeyError:
            pass
        time.sleep(0.05)

    if target_job is None:
        raise KeyError(f"unknown job: {job_id}")

    while time.monotonic() - start_mono <= timeout:
        job_result_path = target_job.dir / "result.json"
        if not job_result_path.is_file():
            time.sleep(0.1)
            continue
        try:
            result = target_job.read_json("result.json")
        except (OSError, ValueError):
            time.sleep(0.1)
            continue

        if result.get("status") != "running":
            return _format_wait_result(result)

        pid_file = target_job.dir / "pid"
        if pid_file_dead(pid_file):
            try:
                result = target_job.read_json("result.json")
            except (OSError, ValueError):
                pass
            if result.get("status") == "running":
                pid, _ = read_pid_file(pid_file)
                return _format_wait_result(
                    {
                        **result,
                        "status": "failed",
                        "exit_code": 1,
                        "reason": f"job process {pid} is not running; result.json was never finished",
                    }
                )
            return _format_wait_result(result)

        time.sleep(0.1)

    return {"state": "running"}


async def wait_job_async(
    job_id: str,
    timeout_s: int = 60,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    validate_job_id(job_id, p)

    timeout = clamp_timeout(timeout_s)
    start_mono = time.monotonic()

    target_job = None
    while time.monotonic() - start_mono <= min(float(timeout), WAIT_APPEAR_S):
        try:
            target_job = get_job(p, job_id)
            break
        except KeyError:
            pass
        await asyncio.sleep(0.05)

    if target_job is None:
        raise KeyError(f"unknown job: {job_id}")

    while time.monotonic() - start_mono <= timeout:
        job_result_path = target_job.dir / "result.json"
        if not job_result_path.is_file():
            await asyncio.sleep(0.1)
            continue
        try:
            result = target_job.read_json("result.json")
        except (OSError, ValueError):
            await asyncio.sleep(0.1)
            continue

        if result.get("status") != "running":
            return _format_wait_result(result)

        pid_file = target_job.dir / "pid"
        if pid_file_dead(pid_file):
            try:
                result = target_job.read_json("result.json")
            except (OSError, ValueError):
                pass
            if result.get("status") == "running":
                pid, _ = read_pid_file(pid_file)
                return _format_wait_result(
                    {
                        **result,
                        "status": "failed",
                        "exit_code": 1,
                        "reason": f"job process {pid} is not running; result.json was never finished",
                    }
                )
            return _format_wait_result(result)

        await asyncio.sleep(0.1)

    return {"state": "running"}


class TaskList(list):
    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, str):
            for t in self:
                if isinstance(t, dict) and t.get("name") == item:
                    return t
            raise KeyError(item)
        return super().__getitem__(item)


def validate_plan_args(plan_path: str, p: Paths | None = None) -> tuple[Path, Any]:
    if not plan_path or not str(plan_path).strip():
        raise UsageError("plan_path is required")
    plan_file = Path(plan_path).expanduser().resolve()
    plan = load_plan(plan_file)

    p = p or paths()
    known_skills = available_skills(p.config_dir) if any(t.skills for t in plan.tasks) else {}
    for task in plan.tasks:
        for skill in task.skills:
            if skill not in known_skills:
                raise UsageError(f"task {task.name}: unknown skill {skill}: {skills_hint(known_skills)}")

    return plan_file, plan


def _reap_review(proc: subprocess.Popen, result_file: Path, stderr_file: Path) -> None:
    """Reap the review runner; if it died without a final result, record a failure."""
    proc.wait()
    info: dict[str, Any] = {}
    try:
        loaded = json.loads(result_file.read_text())
        if isinstance(loaded, dict):
            info = loaded
    except (OSError, ValueError):
        pass
    if info.get("status") in ("ok", "failed"):
        return
    try:
        tail = stderr_file.read_text(errors="replace")[-2000:]
    except OSError:
        tail = ""
    failed = {
        "handle": info.get("handle"),
        "plan": info.get("plan"),
        "status": "failed",
        "exit_code": getattr(proc, "returncode", None) or 1,
        "lines": [],
        "stderr_tail": tail or "runner exited without a result",
    }
    result_file.write_text(json.dumps(failed))


def _spawn_result_cli(
    subcommand: str,
    plan_file: Path,
    plan: Any,
    extra_args: list[str] | None = None,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    review_dir = p.state_dir / "mcp" / "plan-review"
    review_dir.mkdir(parents=True, exist_ok=True)
    handle = f"{subcommand}-{plan.slug}-{time.time_ns()}"
    result_file = review_dir / f"{handle}.json"
    stdout_file = review_dir / f"{handle}.stdout"
    stderr_file = review_dir / f"{handle}.stderr"

    cmd = [
        sys.executable,
        "-P",
        "-m",
        "agenthub",
        "plan",
        subcommand,
        str(plan_file),
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(["--result", str(result_file)])

    env = os.environ.copy()
    repo_root = str(Path(__file__).resolve().parent.parent)
    if "PYTHONPATH" in env:
        if repo_root not in env["PYTHONPATH"].split(os.pathsep):
            env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env['PYTHONPATH']}"
    else:
        env["PYTHONPATH"] = repo_root

    p.state_dir.mkdir(parents=True, exist_ok=True)

    try:
        with contextlib.ExitStack() as stack:
            stdout_fd = stack.enter_context(open(stdout_file, "w"))
            stderr_fd = stack.enter_context(open(stderr_file, "w"))
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_fd,
                stderr=stderr_fd,
                start_new_session=True,
                env=env,
                cwd=p.state_dir,
            )
    except OSError as e:
        return {"error": f"spawn failed: {e}", "handle": handle}

    info = {
        "handle": handle,
        "plan": plan.slug,
        "pid": proc.pid,
        "status": "running",
        "stdout": str(stdout_file),
        "stderr": str(stderr_file),
    }
    # The child may already have written its final result: never clobber it.
    with contextlib.suppress(FileExistsError):
        with open(result_file, "x") as f:
            f.write(json.dumps(info))

    threading.Thread(target=_reap_review, args=(proc, result_file, stderr_file), daemon=True).start()

    return {"handle": handle, "plan": plan.slug, "pid": proc.pid}


def _spawn_plan_cli(
    subcommand: str,
    plan_path: str,
    extra_args: list[str] | None = None,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    plan_file, plan = validate_plan_args(plan_path=plan_path, p=p)

    state_file = plan.path.parent / "state.json"
    if state_file.is_file():
        try:
            state_data = json.loads(state_file.read_text())
            if isinstance(state_data, dict):
                runner = state_data.get("runner")
                if isinstance(runner, dict):
                    pid = runner.get("pid")
                    recorded = runner.get("start_time")
                    if (
                        isinstance(pid, int)
                        and not isinstance(pid, bool)
                        and recorded is not None
                        and is_running(pid)
                        and start_time(pid) == recorded
                    ):
                        return {"error": f"plan runner {pid} is still running; wait or cancel it first"}
        except (OSError, ValueError):
            pass

    cmd = [
        sys.executable,
        "-P",
        "-m",
        "agenthub",
        "plan",
        subcommand,
        str(plan_file),
    ]
    if extra_args:
        cmd.extend(extra_args)

    env = os.environ.copy()
    repo_root = str(Path(__file__).resolve().parent.parent)
    if "PYTHONPATH" in env:
        if repo_root not in env["PYTHONPATH"].split(os.pathsep):
            env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env['PYTHONPATH']}"
    else:
        env["PYTHONPATH"] = repo_root

    p.state_dir.mkdir(parents=True, exist_ok=True)
    log_file = p.state_dir / f"mcp-plan-{subcommand}-{time.time_ns()}.log"

    try:
        with open(log_file, "w") as log_fd:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
                cwd=p.state_dir,
            )
    except OSError as e:
        return {"error": f"spawn failed: {e} (log: {log_file})", "log": str(log_file)}

    # Record the new runner now: runner.pid otherwise names the previous runner until the child claims it,
    # and plan_wait in that window would report a false stalled.
    with contextlib.suppress(OSError):
        (plan.path.parent / "runner.pid").write_text(json.dumps({"pid": proc.pid, "start_time": start_time(proc.pid)}))

    threading.Thread(target=proc.wait, daemon=True).start()

    return {"plan": plan.slug, "runner_pid": proc.pid}


def plan_run(
    plan_path: str,
    p: Paths | None = None,
) -> dict[str, Any]:
    return _spawn_plan_cli("run", plan_path=plan_path, p=p)


def plan_triage(
    plan_path: str,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    plan_file, plan = validate_plan_args(plan_path=plan_path, p=p)
    return _spawn_result_cli("triage", plan_file, plan, p=p)


def plan_close(
    plan_path: str,
    rounds: int = 3,
    p: Paths | None = None,
) -> dict[str, Any]:
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
        raise UsageError(f"rounds must be an integer >= 1: {rounds!r}")
    p = p or paths()
    plan_file, plan = validate_plan_args(plan_path=plan_path, p=p)
    return _spawn_result_cli("close", plan_file, plan, extra_args=["--rounds", str(rounds)], p=p)


def plan_status(
    plan_path: str,
    trail: bool = False,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    plan_file, plan = validate_plan_args(plan_path=plan_path, p=p)
    plan_dir = plan.path.parent
    state_file = plan_dir / "state.json"

    if not state_file.is_file():
        raise UsageError("state.json not found")

    try:
        with open(state_file, "r") as f:
            state_data = json.load(f)
    except (OSError, ValueError) as e:
        raise UsageError(f"cannot read state: {e}") from None

    tasks_data = state_data.get("tasks", {})
    tasks = TaskList()
    for t in plan.tasks:
        ts = tasks_data.get(t.name, {})
        tasks.append(
            {
                "name": t.name,
                "status": ts.get("status", "pending"),
                "attempts": ts.get("attempts", []),
                "branch": ts.get("branch", ""),
                "handoff": ts.get("handoff", ""),
            }
        )

    res: dict[str, Any] = {"plan": plan.slug, "tasks": tasks}

    if trail:
        trail_file = plan_dir / "decisions.tsv"
        if not trail_file.is_file():
            raise UsageError("decisions.tsv not found")
        lines = trail_file.read_text().splitlines()
        trail_rows = []
        if lines:
            header = lines[0].split("\t")
            for line in lines[1:]:
                cells = line.split("\t")
                if len(cells) < len(header):
                    cells = cells + [""] * (len(header) - len(cells))
                trail_rows.append(dict(zip(header, cells, strict=False)))
        res["trail"] = trail_rows

    return res


def validate_review_handle(handle: Any, p: Paths) -> tuple[str, Path]:
    if isinstance(handle, dict) and "handle" in handle:
        handle = handle["handle"]
    if not isinstance(handle, str):
        raise KeyError(f"unknown review handle: {handle}")
    if not handle or not str(handle).strip():
        raise UsageError("handle is required")
    if any(c in handle for c in ("/", "\\", "\0")) or handle in (".", ".."):
        raise KeyError(f"unknown review handle: {handle}")
    review_dir = (p.state_dir / "mcp" / "plan-review").resolve()
    review_dir.mkdir(parents=True, exist_ok=True)
    review_file = (review_dir / f"{handle}.json").resolve()
    if review_file.parent != review_dir:
        raise KeyError(f"unknown review handle: {handle}")
    return handle, review_file


def plan_review_wait(
    handle: str | dict,
    timeout_s: Any = 60,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    handle_str, info_file = validate_review_handle(handle, p)

    timeout = clamp_timeout(timeout_s)
    start_mono = time.monotonic()

    while time.monotonic() - start_mono <= min(float(timeout), WAIT_APPEAR_S):
        if info_file.is_file():
            break
        time.sleep(0.05)
    else:
        if not info_file.is_file():
            raise KeyError(f"unknown review handle: {handle_str}")

    while time.monotonic() - start_mono <= timeout:
        if not info_file.is_file():
            time.sleep(0.05)
            continue
        try:
            info = json.loads(info_file.read_text())
        except (OSError, ValueError):
            time.sleep(0.05)
            continue

        if info.get("status") in ("ok", "failed") and "lines" in info:
            return {
                "state": info["status"],
                "status": info["status"],
                "exit_code": info.get("exit_code", 0),
                "lines": info["lines"],
                "stderr_tail": info.get("stderr_tail", ""),
                "handle": handle_str,
                "plan": info.get("plan"),
            }

        pid = info.get("pid")
        if pid:
            if not is_running(pid):
                time.sleep(0.1)
                try:
                    info = json.loads(info_file.read_text())
                    if "lines" in info:
                        return {
                            "state": info.get("status", "failed"),
                            "status": info.get("status", "failed"),
                            "exit_code": info.get("exit_code", 1),
                            "lines": info["lines"],
                            "stderr_tail": info.get("stderr_tail", ""),
                            "handle": handle_str,
                            "plan": info.get("plan"),
                        }
                except (OSError, ValueError):
                    pass
                return {
                    "state": "failed",
                    "status": "failed",
                    "exit_code": 1,
                    "lines": [],
                    "stderr_tail": info.get("stderr_tail", "process died unexpectedly"),
                    "handle": handle_str,
                    "plan": info.get("plan"),
                }

        time.sleep(0.1)

    return {"state": "running"}


async def plan_review_wait_async(
    handle: str | dict,
    timeout_s: Any = 60,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    handle_str, info_file = validate_review_handle(handle, p)

    timeout = clamp_timeout(timeout_s)
    start_mono = time.monotonic()

    while time.monotonic() - start_mono <= min(float(timeout), WAIT_APPEAR_S):
        if info_file.is_file():
            break
        await asyncio.sleep(0.05)
    else:
        if not info_file.is_file():
            raise KeyError(f"unknown review handle: {handle_str}")

    while time.monotonic() - start_mono <= timeout:
        if not info_file.is_file():
            await asyncio.sleep(0.05)
            continue
        try:
            info = json.loads(info_file.read_text())
        except (OSError, ValueError):
            await asyncio.sleep(0.05)
            continue

        if info.get("status") in ("ok", "failed") and "lines" in info:
            return {
                "state": info["status"],
                "status": info["status"],
                "exit_code": info.get("exit_code", 0),
                "lines": info["lines"],
                "stderr_tail": info.get("stderr_tail", ""),
                "handle": handle_str,
                "plan": info.get("plan"),
            }

        pid = info.get("pid")
        if pid:
            if not is_running(pid):
                await asyncio.sleep(0.1)
                try:
                    info = json.loads(info_file.read_text())
                    if "lines" in info:
                        return {
                            "state": info.get("status", "failed"),
                            "status": info.get("status", "failed"),
                            "exit_code": info.get("exit_code", 1),
                            "lines": info["lines"],
                            "stderr_tail": info.get("stderr_tail", ""),
                            "handle": handle_str,
                            "plan": info.get("plan"),
                        }
                except (OSError, ValueError):
                    pass
                return {
                    "state": "failed",
                    "status": "failed",
                    "exit_code": 1,
                    "lines": [],
                    "stderr_tail": info.get("stderr_tail", "process died unexpectedly"),
                    "handle": handle_str,
                    "plan": info.get("plan"),
                }

        await asyncio.sleep(0.1)

    return {"state": "running"}


def plan_review(
    plan_path: str,
    task: str | None = None,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    plan_file, plan = validate_plan_args(plan_path=plan_path, p=p)
    plan_dir = plan.path.parent
    state_file = plan_dir / "state.json"

    if not state_file.is_file():
        raise UsageError("state.json not found")

    from agenthub.planstate import load_or_init

    state = load_or_init(plan)
    tasks = [t for t in plan.tasks if (t.kind or t.role) == "implement" and state.task(t.name).status == "done"]
    if task is not None:
        tasks = [t for t in tasks if t.name == task]
        if not tasks:
            raise UsageError(f"no done implement task named {task}")
    if not tasks:
        return {"reason": "no done tasks to review", "error": "no done tasks to review"}

    res = _spawn_result_cli(
        "review",
        plan_file,
        plan,
        extra_args=["--task", task] if task is not None else None,
        p=p,
    )
    if "pid" in res:
        res["runner_pid"] = res["pid"]
    return res


def plan_merge(
    plan_path: str,
    into: str | None = None,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    plan_file, plan = validate_plan_args(plan_path=plan_path, p=p)
    plan_dir = plan.path.parent
    state_file = plan_dir / "state.json"

    if not state_file.is_file():
        raise UsageError("state.json not found")

    from agenthub.planrun import merge_order, merge_target_problem, merge_task
    from agenthub.planstate import load_or_init

    state = load_or_init(plan)
    target = into or plan.base
    tasks = [t for t in merge_order(plan) if (t.kind or t.role) == "implement" and state.task(t.name).status == "done"]
    if not tasks:
        return {"merged": [], "stopped_at": None}

    try:
        problem = merge_target_problem(plan.repo, target)
    except guards.GuardError as e:
        raise UsageError(str(e)) from None

    if problem:
        raise UsageError(problem)

    jobs = list_jobs(p, ALL_JOBS)
    merged: list[str] = []
    stopped_at: dict[str, Any] | None = None

    for task in tasks:
        result = merge_task(plan, task, state.task(task.name), target, jobs)
        if result["status"] == "merged":
            merged.append(task.name)
        elif result["status"] == "already-merged":
            merged.append(task.name)
        elif result["status"] == "not-ready":
            stopped_at = {
                "task": task.name,
                "status": "not-ready",
                "problems": result.get("problems", []),
                "reason": result.get("reason", ""),
            }
            break
        else:
            stopped_at = {
                "task": task.name,
                "status": result.get("status", "failed"),
                "problems": result.get("problems", []),
                "reason": result.get("reason", ""),
            }
            break

    return {"merged": merged, "stopped_at": stopped_at}


def _dead_runner_pid(plan_dir: Path, state_data: dict[str, Any]) -> int | None:
    """Pid of the recorded plan runner when it is gone or its pid was reused, else None."""
    runner: Any = None
    try:
        runner = json.loads((plan_dir / "runner.pid").read_text())
    except (OSError, ValueError):
        pass
    if not isinstance(runner, dict):
        runner = state_data.get("runner")
    if not isinstance(runner, dict):
        return None
    pid = runner.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    if not is_running(pid):
        return pid
    recorded = runner.get("start_time")
    if isinstance(recorded, int) and not isinstance(recorded, bool) and start_time(pid) != recorded:
        return pid
    return None


def _plan_stalled(
    plan_file: Path, plan_dir: Path, state_file: Path, state_data: dict[str, Any], p: Paths
) -> dict[str, Any] | None:
    """Stalled result when tasks are unfinished and the runner is dead; None otherwise."""
    pid = _dead_runner_pid(plan_dir, state_data)
    if pid is None:
        return None
    try:
        with open(state_file, "r") as f:
            fresh = json.load(f)
    except (OSError, ValueError):
        fresh = state_data
    tasks_data = fresh.get("tasks", {})
    if tasks_data and not any(ts.get("status") in ("pending", "running") for ts in tasks_data.values()):
        return plan_status(plan_path=str(plan_file), trail=False, p=p)
    return {
        "state": "stalled",
        "reason": f"plan runner {pid} is not running; tasks still pending/running",
        **plan_status(plan_path=str(plan_file), trail=False, p=p),
    }


def plan_wait(
    plan_path: str,
    timeout_s: Any = 60,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    if not plan_path or not str(plan_path).strip():
        raise UsageError("plan_path is required")

    plan_file, plan = validate_plan_args(plan_path=plan_path, p=p)
    plan_dir = plan.path.parent
    state_file = plan_dir / "state.json"

    timeout = clamp_timeout(timeout_s)
    start_mono = time.monotonic()

    while time.monotonic() - start_mono <= min(float(timeout), WAIT_APPEAR_S):
        if state_file.is_file():
            break
        time.sleep(0.05)
    else:
        if not state_file.is_file():
            raise UsageError("state.json not found")

    while time.monotonic() - start_mono <= timeout:
        if not state_file.is_file():
            time.sleep(0.05)
            continue
        try:
            with open(state_file, "r") as f:
                state_data = json.load(f)
        except (OSError, ValueError):
            time.sleep(0.05)
            continue

        tasks_data = state_data.get("tasks", {})
        if tasks_data and not any(ts.get("status") in ("pending", "running") for ts in tasks_data.values()):
            return plan_status(plan_path=str(plan_file), trail=False, p=p)
        stalled = _plan_stalled(plan_file, plan_dir, state_file, state_data, p)
        if stalled is not None:
            return stalled

        time.sleep(0.1)

    return {"state": "running"}


async def plan_wait_async(
    plan_path: str,
    timeout_s: Any = 60,
    p: Paths | None = None,
) -> dict[str, Any]:
    p = p or paths()
    if not plan_path or not str(plan_path).strip():
        raise UsageError("plan_path is required")

    plan_file, plan = validate_plan_args(plan_path=plan_path, p=p)
    plan_dir = plan.path.parent
    state_file = plan_dir / "state.json"

    timeout = clamp_timeout(timeout_s)
    start_mono = time.monotonic()

    while time.monotonic() - start_mono <= min(float(timeout), WAIT_APPEAR_S):
        if state_file.is_file():
            break
        await asyncio.sleep(0.05)
    else:
        if not state_file.is_file():
            raise UsageError("state.json not found")

    while time.monotonic() - start_mono <= timeout:
        if not state_file.is_file():
            await asyncio.sleep(0.05)
            continue
        try:
            with open(state_file, "r") as f:
                state_data = json.load(f)
        except (OSError, ValueError):
            await asyncio.sleep(0.05)
            continue

        tasks_data = state_data.get("tasks", {})
        if tasks_data and not any(ts.get("status") in ("pending", "running") for ts in tasks_data.values()):
            return plan_status(plan_path=str(plan_file), trail=False, p=p)
        stalled = _plan_stalled(plan_file, plan_dir, state_file, state_data, p)
        if stalled is not None:
            return stalled

        await asyncio.sleep(0.1)

    return {"state": "running"}


def build_server() -> "MCPServer":
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations
    except (ImportError, ModuleNotFoundError) as e:
        raise ImportError("mcp SDK not installed; install with: uv tool install 'agent-hub[mcp]'") from e

    server = MCPServer("agent-hub")

    @server.tool(
        name="pool",
        description="Show roles, chains, and cooldowns (same data as agent-hub pool --json)",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def pool() -> dict[str, Any]:
        return pool_data()

    @server.tool(
        name="jobs",
        description="List recent jobs of a repository or all repositories",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def jobs(repo_dir: str, all: bool = False) -> dict[str, Any]:
        return jobs_data(repo_dir=repo_dir, all=all)

    @server.tool(
        name="show_job",
        description="Show a job's result by id",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def show_job(job_id: str) -> dict[str, Any]:
        return show_job_data(job_id=job_id)

    @server.tool(
        name="run_job",
        description="Run a job detached, returning its job id immediately",
    )
    def run_job_tool(
        workdir: str,
        role: str | None = None,
        agent: str | None = None,
        brief_path: str | None = None,
        gates: list[str] | None = None,
        skills: list[str] | None = None,
        base: str | None = None,
    ) -> dict[str, Any]:
        return run_job(
            workdir=workdir,
            role=role,
            agent=agent,
            brief_path=brief_path,
            gates=gates or [],
            skills=skills or [],
            base=base,
        )

    @server.tool(
        name="wait_job",
        description="Wait for a job to finish, bounded by timeout_s (at most 300s)",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def wait_job_tool(
        job_id: str,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        return await wait_job_async(
            job_id=job_id,
            timeout_s=timeout_s,
        )

    @server.tool(
        name="gate_start",
        description="Gate a work tree's committed HEAD detached, returning the gate job id",
    )
    def gate_start_tool(workdir: str, gates: list[str] | None = None) -> dict[str, Any]:
        return gate_start(workdir=workdir, gates=gates)

    @server.tool(
        name="ready",
        description="Check readiness of a work tree's committed HEAD to merge",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def ready_tool(workdir: str) -> dict[str, Any]:
        return ready(workdir=workdir)

    @server.tool(
        name="plan_run",
        description="Start a plan run detached, returning the plan slug and runner pid immediately",
    )
    def plan_run_tool(plan_path: str) -> dict[str, Any]:
        return plan_run(plan_path=plan_path)

    @server.tool(
        name="plan_status",
        description="Show per-task status of a plan, optionally with decision trail",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def plan_status_tool(plan_path: str, trail: bool = False) -> dict[str, Any]:
        return plan_status(plan_path=plan_path, trail=trail)

    @server.tool(
        name="plan_wait",
        description="Wait for a plan to finish, bounded by timeout_s (at most 300s)",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def plan_wait_tool(
        plan_path: str,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        return await plan_wait_async(plan_path=plan_path, timeout_s=timeout_s)

    @server.tool(
        name="plan_review_wait",
        description="Wait for a plan review to finish, bounded by timeout_s (at most 300s)",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def plan_review_wait_tool(
        handle: str,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        return await plan_review_wait_async(handle=handle, timeout_s=timeout_s)

    @server.tool(
        name="plan_review",
        description="Review done tasks of a plan detached, returning a review handle",
    )
    def plan_review_tool(plan_path: str, task: str | None = None) -> dict[str, Any]:
        return plan_review(plan_path=plan_path, task=task)

    @server.tool(
        name="plan_merge",
        description="Merge ready tasks of a plan in dependency order",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def plan_merge_tool(plan_path: str, into: str | None = None) -> dict[str, Any]:
        return plan_merge(plan_path=plan_path, into=into)

    @server.tool(
        name="plan_triage",
        description=(
            "Start a plan triage detached, returning a handle immediately; wait with plan_review_wait(handle)"
        ),
    )
    def plan_triage_tool(plan_path: str) -> dict[str, Any]:
        return plan_triage(plan_path=plan_path)

    @server.tool(
        name="plan_close",
        description=(
            "Start a plan close loop detached, returning a handle immediately; wait with plan_review_wait(handle)"
        ),
    )
    def plan_close_tool(plan_path: str, rounds: int = 3) -> dict[str, Any]:
        return plan_close(plan_path=plan_path, rounds=rounds)

    return server


def run_stdio() -> None:
    server = build_server()
    server.run("stdio")
