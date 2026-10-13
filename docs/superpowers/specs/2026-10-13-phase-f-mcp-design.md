# Phase F — agent-hub as an MCP server

Status: design, 2026-10-13. Decision D-098. Owner: "tại sao chúng ta không build agenthub thành mcp? (cho việc reuse
sau này)" → "đồng ý, mcp nên có sớm".

## Why

- Every client the owner uses speaks MCP (Claude Code, Claude Desktop, codex, agy, Cursor); a typed server makes
  agent-hub reusable from any of them, including from the Mac.
- The director drove agent-hub through shell glue and that glue failed this session (word splitting ran a review on
  the wrong directory, a suspend killed background shells, duplicate reviews). Tools with typed arguments and JSON
  results remove the glue.
- Per-tool permissions: read-only tools can be allowed freely; `plan_merge` asks.

## Shape

- **The CLI stays the core**; the MCP server is a thin layer that calls the same Python functions. One source of
  truth for behaviour, records and rules (`ready`, gates, no push).
- Official SDK: `mcp` (2.x, `mcp.server.mcpserver.MCPServer`), as an **optional** dependency (`agent-hub[mcp]`,
  `mcp>=2.3,<3`), so the core stays stdlib-only. `agent-hub mcp` runs the server on stdio; without the extra it
  exits 2 with the install hint.
- **Long jobs are asynchronous.** A tool call never blocks for a whole job: `run_job` / `gate_start` / `plan_run` /
  `plan_review` start a detached process and return its id at once; `wait_job` / `plan_wait` wait at most
  `timeout_s` (≤ 300) and return `running` or the result. Results are JSON: status, exit code, agent, model,
  verdict, handoff summary, gate summary, paths to the full record.
- Tool annotations: read-only tools carry `readOnlyHint`; `plan_merge` carries `destructiveHint`. Nothing pushes.
- Token cost: Claude Code loads MCP tools deferred, so the always-on cost stays small; the tool descriptions are kept
  to one line each.

## Tools

| Tool | Kind | Does |
|---|---|---|
| `pool` | read | roles, chains, cooldowns (as `agent-hub pool --json`) |
| `jobs`, `show_job` | read | recent jobs of a repository; one job's result |
| `run_job` | start | `run --role/--agent/--brief/-d/--gate/--skill/--base` detached; returns job id |
| `wait_job` | read | bounded wait on a job id or work dir |
| `gate_start`, `ready` | start / read | gate the committed HEAD; readiness check |
| `plan_run`, `plan_status`, `plan_wait`, `plan_review` | start / read | plan mode |
| `plan_merge` | destructive | merge ready tasks in dependency order through `ready` |

## Tasks (small; implement chain; review chain)

| # | Task | Files |
|---|---|---|
| F1 | optional dep + `agent-hub mcp` + read tools `pool`, `jobs`, `show_job` | `pyproject.toml`, `agenthub/mcpserver.py`, test |
| F2 | `run_job` (detached) + `wait_job` (bounded) | `agenthub/mcpserver.py`, test |
| F3 | `gate_start`, `ready` | `agenthub/mcpserver.py`, test |
| F4 | plan tools (after `plan review`/`plan merge` merge) | `agenthub/mcpserver.py`, test |
| F5 | plugin wiring: `.mcp.json` in the agent-hub plugin, README, orchestrate skill uses the tools | plugin files, docs |
| F6 | codex / agy registration through their official `mcp` commands via `hub install` | `agenthub/hub.py`, test |
