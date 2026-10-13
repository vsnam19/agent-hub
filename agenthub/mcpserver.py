import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agenthub import brief as briefmod
from agenthub import guards
from agenthub.agents import registry
from agenthub.cli import ALL_JOBS, WAIT_APPEAR_S, UsageError, _job_repo, _repo_of
from agenthub.config import ConfigError, Paths, parse_duration, paths, role_kind
from agenthub.jobs import JOB_ID_RE, Job, get_job, list_jobs
from agenthub.orchestrate import readiness, skills_hint
from agenthub.pool import Cooldowns, load_pool, parse_link
from agenthub.workers import available_skills

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


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
    job = get_job(p, job_id)
    return job.read_json("result.json")


def clamp_timeout(timeout_s: Any) -> int:
    if isinstance(timeout_s, (int, float)):
        val = int(timeout_s)
    elif isinstance(timeout_s, str):
        try:
            val = int(timeout_s)
        except ValueError:
            val = int(parse_duration(timeout_s).total_seconds())
    else:
        val = 60
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
        try:
            brief_text = Path(brief_path).read_text()
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
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
    except OSError as e:
        return {"error": f"spawn failed: {e} (log: {log_file})", "log": str(log_file)}

    poll_deadline = spawn_monotonic + 10.0
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
            if proc.returncode != 0:
                err_msg = ""
                if log_file.is_file():
                    err_msg = log_file.read_text().strip()
                msg = err_msg or f"process exited with returncode {proc.returncode}"
                return {"error": f"{msg} (log: {log_file})", "log": str(log_file)}
            break

        time.sleep(0.05)

    if target_job_id is None:
        err_msg = ""
        if log_file.is_file():
            err_msg = log_file.read_text().strip()
        msg = err_msg or "job record did not appear within 10s"
        return {"error": f"{msg} (log: {log_file})", "log": str(log_file)}

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
                try:
                    import shutil

                    shutil.move(str(log_file), str(job_dir / "spawn.log"))
                except OSError:
                    pass
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


def _job_dir_workdir(job_dir: Path) -> str | None:
    for name in ("request.json", "result.json", "gate.json", "record.json"):
        f = job_dir / name
        if f.is_file():
            try:
                with open(f) as fp:
                    data = json.load(fp)
                if isinstance(data, dict):
                    w = data.get("workdir")
                    if w:
                        return str(w)
            except Exception:
                pass
    return None


def _find_job_for_workdir(p: Paths, resolved_workdir: Path) -> Job | None:
    if not p.jobs_dir.is_dir():
        return None
    entries = []
    for entry in os.scandir(p.jobs_dir):
        if entry.is_dir() and JOB_ID_RE.match(entry.name):
            entries.append(entry)
    entries.sort(key=lambda e: e.name, reverse=True)
    for entry in entries:
        dir_path = Path(entry.path)
        job_workdir = _job_dir_workdir(dir_path)
        if job_workdir and Path(job_workdir).expanduser().resolve() == resolved_workdir:
            return get_job(p, entry.name)
    return None


def wait_job(
    job_id: str | None = None,
    workdir: str | None = None,
    timeout_s: int = 60,
    p: Paths | None = None,
) -> dict[str, Any]:
    if job_id is None and workdir is None:
        raise UsageError("job_id or workdir is required")

    timeout = clamp_timeout(timeout_s)
    p = p or paths()
    start_mono = time.monotonic()

    target_job = None
    while time.monotonic() - start_mono <= min(float(timeout), WAIT_APPEAR_S):
        if job_id is not None:
            try:
                target_job = get_job(p, job_id)
                break
            except KeyError:
                pass
        elif workdir is not None:
            resolved_workdir = Path(workdir).expanduser().resolve()
            target_job = _find_job_for_workdir(p, resolved_workdir)
            if target_job is not None:
                break
        time.sleep(0.05)

    if target_job is None:
        if job_id is not None:
            raise KeyError(f"unknown job: {job_id}")
        raise UsageError(f"agent-hub: no job for {workdir}")

    while time.monotonic() - start_mono <= timeout:
        try:
            result = target_job.read_json("result.json")
        except (OSError, ValueError):
            result = {"status": "running"}

        if result.get("status") != "running":
            return _format_wait_result(result)

        time.sleep(0.1)

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
    def wait_job_tool(
        job_id: str | None = None,
        workdir: str | None = None,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        return wait_job(
            job_id=job_id,
            workdir=workdir,
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

    return server


def run_stdio() -> None:
    server = build_server()
    server.run("stdio")
