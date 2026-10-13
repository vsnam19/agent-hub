from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from agenthub.agents import registry
from agenthub.cli import ALL_JOBS, _job_repo, _repo_of
from agenthub.config import Paths, paths
from agenthub.jobs import get_job, list_jobs
from agenthub.pool import Cooldowns, load_pool, parse_link

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

    return server


def run_stdio() -> None:
    server = build_server()
    server.run("stdio")
