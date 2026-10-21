# agent-hub

System overview — how the pieces fit, this machine's setup, testing, lessons: [`docs/overview.md`](docs/overview.md).

`agent-hub` lets one coding agent (the orchestrator, normally Claude Code) hand well-specified work to other coding
agents on this machine and get back a record it can verify. Jobs are picked by role (`implement`, `review`); each
role is an ordered chain of `agent:model` links, and the CLI walks the chain, skipping links that are on cooldown
and falling back to the next one on quota, auth, model-unavailable or not-installed (a review whose tool was auto-denied also falls through to the next link). It has no runtime
dependencies (Python 3.12+ standard library only).

## Install

```sh
claude plugin marketplace add ~/Workspaces/tools/agent-plugins   # once; later: vsnam19/agent-plugins
claude plugin install agent-hub@agent-plugins
```

For a terminal, use `uv tool install`:

```sh
uv tool install --editable .
```

## Pool

The shipped pool is `agenthub/default_pool.toml`. Override it with `$XDG_CONFIG_HOME/agent-hub/pool.toml` (or
`$AGENT_HUB_CONFIG/pool.toml`). A role defined in your file replaces that role entirely; other roles come from the
default, and `[defaults]` keys merge one by one.

```toml
[defaults]
quota_cooldown = "60m"        # used when the agent does not say when quota resets
timeout = { implement = "60m", review = "20m" }

[roles.implement]                 # roles have a kind (implement | review) and an optional escalate target (D-061)
kind = "implement"
escalate = "implement-hard"
chain = ["<agent>:<model>", "<agent>:<model>"]
```

Durations accept `s`, `m`, `h`. The agent part of a link must name a registered adapter under `agenthub/agents/`.
State (cooldowns and job directories) lives in `$XDG_STATE_HOME/agent-hub/` or `$AGENT_HUB_STATE`.

### Model catalog

`pool` flags links whose model the host CLI does not list. Each agent's valid model ids are read into a 24 h cache,
`models.json` in the state directory (mode 600). `agent-hub pool --refresh-models` forces a refresh. A plain
`agent-hub pool` refreshes lazily when the cache is missing or older than 24 h, for installed agents only. Job runs only
read the cache.

- agy: `agy models` (SSH variables removed).
- codex: `codex debug models`.
- claude: a static alias list from its `agent.toml`; any id starting with `claude-` is accepted.
- gwen: not checked.

`pool` prints `unknown-model` next to a link whose agent has a fresh catalog without that model (`--json`:
`model_known` is `true`, `false` or `null`). A chain skips such a link at once and records `unknown-model` in the job's
attempts. A missing or stale catalog never blocks work.

`pool` prints `(same as default)` on an owner role whose chain equals the built-in chain, so you can delete that role
from `pool.toml`.

## Commands

```
agent-hub run --role ROLE [--brief BRIEF] -d WORKDIR [--agent AGENT:MODEL] [--exclude AGENT:MODEL]... [--base BASE] [--timeout TIMEOUT] [--gate CMD]... [--allow GLOB]... [--jev] [--json]
agent-hub pool [--json] [--clear AGENT:MODEL] [--refresh-models]
agent-hub profile AGENT
agent-hub jobs [--limit LIMIT] [--all]              # this repository's jobs; --all lists every project's
agent-hub show [--json] ID
agent-hub cancel ID | --all [-d WORKDIR]            # only jobs of the caller's repository (D-078)
agent-hub wait -d WORKDIR [--timeout TIMEOUT] [--json]
agent-hub plan run <plan.json> [--max-parallel N]
agent-hub plan status <plan.json> [--json] [--trail]
agent-hub plan wait <plan.json> [--timeout 9m]
agent-hub plan cancel <plan.json> [-d WORKDIR]       # only from the plan's repository
agent-hub orders [-d WORKDIR] [--json]              # standing orders and what enforces each (D-071)
agent-hub gate [-d WORKDIR] [--gate CMD]... [--json]   # gate the committed HEAD; repo gates from the merge base (D-080)
agent-hub ready -d WORKDIR [--json]               # merge check: HEAD gated + approved by a non-implementer (D-075)
```

- `wait` blocks until the newest job for that work directory ends (or until `--timeout`, default 9m), and prints its result like `show` (or `--json`).
- `run` runs one job in the foreground and prints a short summary then the reply; `--json` prints only the result
  record. `--brief` is a file, or `-` for stdin, and is required for `implement` (optional for `review`).
  `--agent` replaces the chain with that single `agent:model` link. `--timeout` takes a duration such as `30m`.
  `--gate` is a shell command to run after a successful attempt, stopping at the first failure; repeatable.
- `implement` briefs must contain `## Input` and `## Expected output` headings, and `-d` must be a git work tree on
  a non-default branch. Single jobs never create worktrees (plan mode does, one per task). The CLI appends a footer asking for a structured handoff, which is parsed and included in the result. If `--jev` (or `AGENT_HUB_JEV=1`) is set, a missing handoff is inferred using `jev-code`. The result also includes `gate_touched`, listing changes that could weaken the gate.
- `review` is read-only. The CLI computes `git diff <base>...HEAD` into the job's `diff.patch` and the reviewer reads
  that file; the reviewer never gets a shell. `--base` defaults to the repository's default branch. The work tree is
  fingerprinted before and after, and any change fails the job as `modified-tree`. `--gate` is ignored.
- `pool` lists every role, its chain, and per link whether it is installed, its version and any cooldown;
  `--clear` lifts one cooldown; `--refresh-models` forces a model catalog refresh (see Model catalog).
- `profile` prints an agent's `profile.md`, with strengths, weaknesses and what to put in a brief.
- `jobs` lists recent jobs, newest first; `show` prints a job's result and reply.
- `cancel` sends SIGTERM to the job's recorded pid, after verifying the pid against the process start time so a
  recycled pid is never signalled. If the process is gone, a job left `running` is marked `cancelled`.
- Human output on a TTY strips control characters from agent text; `--json` output is not altered.


### Plan mode

Run a graph of tasks with `agent-hub plan run <plan.json>`. The file defines the graph:

```json
{
  "goal": "context for every task",
  "repo": "/abs/path/to/repo",
  "base": "master",
  "slug": "wait-cmd",
  "max_parallel": 2,
  "tasks": [
    {"name": "core", "role": "implement", "brief": "briefs/core.md", "gate": ["just lint"]},
    {"name": "core-review", "role": "review", "verifies": "core", "brief": null}
  ]
}
```

- `plan run` creates worktrees (`repo/.worktrees/slug-name`) and branches (`orch/slug/name`), running dependencies first and merging upstream branches into dependents. It saves state to `state.json` and resumes on restart. It polls the jobs and retries tasks based on exit codes.
- `plan status` prints the status of each task in the plan.
- `plan wait` blocks until all tasks in the plan are done (or failed/blocked), and prints the status.

### Jev (optional)

`--jev` (or `AGENT_HUB_JEV=1`) asks a local Jev decision model to classify an implement reply that has no handoff;
the answer goes to `handoff_inferred` and never changes the status. Setup: Ollama ≥ 0.35 with `nimble` pulled, and
`jev-code` on PATH (or `AGENT_HUB_JEV_BIN`): `npm install --prefix ~/.local/share/jev-code @french-castle/jev-code`.

### Standing orders

Rules that every implement and review brief must carry. agent-hub appends `~/.config/agent-hub/standing-orders.md`
(or `$AGENT_HUB_CONFIG/standing-orders.md`) and then `<repo>/.agent-hub/standing-orders.md`, each only if it exists and
is non-empty, under `## Standing orders`, after the brief and before the handoff footer. An unreadable file is skipped
with a warning on stderr. The text sent is recorded in the job's `brief.md`. A starting point is in
`docs/standing-orders.example.md`.

Each line is `N. <rule> — enforced by: <mechanism>` (`gate`, `gate-guard`, `scope`, `handoff-parser`, or `none`).
The suffix is stripped from the text sent. `agent-hub orders [-d WORKDIR] [--json]` lists every numbered rule with
its mechanism and source file (the user file, then the repo file) and exits 1 if a rule has no `— enforced by:`.

### Worker skills

Per-run worker hosts load the owner's worker skills on every run. `$AGENT_HUB_CONFIG/workers.toml` (default
`~/.config/agent-hub/workers.toml`) names the plugins and the list used when a repo says nothing:

```toml
default = ["telaf", "aspice"]          # enabled when the repo says nothing
[plugins]                              # name -> plugin directory (holds skills/<skill>/SKILL.md)
telaf = "~/Workspaces/tools/agent-plugins/plugins/telaf"
aspice = "~/Workspaces/tools/agent-plugins/plugins/aspice"
```

A repo's `.agent-hub/config.toml` may set `worker_plugins = ["telaf"]`, which replaces `default` for that repo. It can
only name plugins from `[plugins]`. Without `workers.toml` no worker gets extra plugins. Claude workers get
`--plugin-dir` per plugin; gwen workers get `skills.paths` and run with `OPENCODE_DISABLE_EXTERNAL_SKILLS=1`, so they
index only these skills. codex and agy read their globally installed plugins and are not affected.

codex and agy are installed hosts: they get the union of `[plugins]` through their own plugin commands. For codex
add the marketplace directory (holds `.agents/plugins/marketplace.json`) to `workers.toml`:

```toml
[hub]
codex_marketplace = "~/Workspaces/tools/agent-plugins"
```

```sh
agent-hub hub status [--json]                    # per host × plugin: current, stale, missing, per-run, stray …
agent-hub hub install [--apply] [--host codex]   # dry-run prints the commands; --apply runs them (exit 1 on failure)
```

`status` is read-only. `install` uses `codex plugin marketplace add` / `codex plugin add` and
`agy plugin validate` / `agy plugin install`; it never edits tool config and never deletes stray hand-copied skills
in `$CODEX_HOME/skills/` (they are only reported).

A job can name the skills it should use: `agent-hub run --skill debug-playbook --skill telaf ...` (repeatable), or
`"skills": ["debug-playbook"]` on a plan task. Each name must be a `skills/<skill>/SKILL.md` under any plugin in
`[plugins]`, not only the ones this run enables; an unknown name is a configuration error (exit 2) that lists the
available skills, and nothing is recorded. The CLI then appends a `## Skills` section to the brief, after the
standing orders and before the handoff footer, asking the worker to load and follow them (for implement and review
jobs alike). Without skills there is no section.

### Learned skills

`agent-hub skills weekly` turns repeated work into worker skills, once a week and local only. It mines the job records
and Claude Code transcripts for what repeats (review findings, fix rounds, owner corrections), drafts at most 3 skills
into the owned `learned-worker` and `learned-director` plugins with eval cases, and keeps a skill only if
`claude plugin eval` with the skill beats the run without it. The kept skills go through review and `agent-hub ready`,
then a local release. Nothing is pushed.

- `agent-hub skills mine [--days N | --since DATE] [--json]` extracts the repeats only, redacted, and writes them to
  `<state>/skills/<date>/` (`<state>` is `~/.local/state/agent-hub` by default).
- `agent-hub skills weekly [--dry-run] [--max-cost-usd 3]` runs the whole pipeline. `--dry-run` prints the steps and
  writes nothing.
- `agent-hub skills report` prints the newest `<state>/skills/<date>/report.md`.
- `agent-hub skills schedule` runs `weekly` every Sunday at 20:17 local time through a systemd user timer. Without a
  flag it prints the two unit files and the `systemctl --user` commands and changes nothing. `--apply` writes
  `agent-hub-skills-weekly.service` and `.timer` into `$XDG_CONFIG_HOME/systemd/user` (default
  `~/.config/systemd/user`), then enables the timer. `--at "<calendar>"` changes the time; it is checked with
  `systemd-analyze calendar` first. The service uses the PATH of the shell that ran `--apply`, so `claude`, `codex`,
  `agy` and `uv` must be on that PATH.
- `agent-hub skills schedule --remove` disables the timer, deletes both unit files and reloads systemd. Only these two
  units are touched.

### Audit log

Every agent action (director sessions of Claude Code, codex and agy, plus agent-hub jobs and plans) is appended to a
hash-chained log at `<state>/audit/YYYY-MM/YYYY-MM-DD.jsonl`. Logging is fail-open and observe-only; never stored:
model thinking, file contents, command output, bodies, auth headers.

- `agent-hub audit show [--plan P | --job J | --session S | --since T] [--kind K] [--json]`
- `agent-hub audit verify [--month YYYY-MM]`
- `agent-hub audit outward [--since T]`
- `agent-hub audit archive [--month YYYY-MM]`
- `agent-hub audit schedule [--apply] [--remove] [--at CALENDAR]`

MCP tools: `audit_status`, `audit_show`, `audit_archive_status`, `audit_archive_rerun`, `incidents`.

Owner steps after release:
1. restart Claude Code (plugin hooks load);
2. `agent-hub hub install --apply` (codex and agy hook entries);
3. trust the codex hook (codex asks on next start, or `/hooks`);
4. write the vault path as `vault = '...'` in audit.toml in the agent-hub config directory;
5. `agent-hub audit schedule --apply`.

### Incident log

Process failures (a wrong pick, a rejected plan, a rework loop) are logged on purpose, so the weekly mining can see them.

- `agent-hub incident add --kind K --what TEXT [--lesson TEXT] [--source director|techlead|tool|owner] [--plan P] [--task T] [--job J] [--attempts N] [--rounds N] [--minutes N] [--json]` appends one line to `incidents.jsonl` in the state directory and writes an audit event. `--kind` and `--what` are required; without either it exits 2.
- `agent-hub incident list [--since T] [--kind K] [--json]` prints the logged incidents.

The MCP tool `incidents(since?, kind?)` reads the same log and never writes it. It is registered with the audit tools.

`agent-hub skills weekly` counts incidents in its window. A kind with 3 or more incidents becomes a `learned-director`
skill draft (see Learned skills).

## MCP server

agent-hub ships an MCP server (`agent-hub mcp`) that exposes typed tools for Claude Code, Cursor, Codex, and other MCP clients. Claude Code sessions with the agent-hub plugin start the server automatically through `bin/agent-hub` (no extra needed).

A standalone install for other clients uses:

```sh
uv tool install --editable '<repo>[mcp]'
agent-hub mcp
```

### Tools

- `pool`: Show roles, chains, and cooldowns (same data as `agent-hub pool --json`).
- `jobs`: List recent jobs of a repository or all repositories.
- `show_job`: Show a job's result by id.
- `run_job`: Run a job detached, returning its job id immediately.
- `wait_job`: Wait for a job to finish, bounded by `timeout_s` (at most 100s).
- `gate_start`: Gate a work tree's committed HEAD detached, returning the gate job id.
- `ready`: Check readiness of a work tree's committed HEAD to merge.
- `plan_run`: Start a plan run detached, returning the plan slug and runner pid immediately.
- `plan_status`: Show per-task status of a plan, optionally with decision trail.
- `plan_wait`: Wait for a plan to finish, bounded by `timeout_s` (at most 100s).
- `plan_review`: Review done tasks of a plan detached, returning a review handle.
- `plan_review_wait`: Wait for a plan review to finish, bounded by `timeout_s` (at most 100s).
- `plan_merge`: Merge ready tasks of a plan in dependency order.
- `plan_triage`: Start a plan triage detached, returning a handle; wait with `plan_review_wait(handle)`.
- `plan_close`: Start a plan close loop detached, returning a handle; wait with `plan_review_wait(handle)`.

### Async pattern

Long jobs are asynchronous: tool calls never block indefinitely. Starting a job returns an identifier immediately (`start → id → bounded wait`):
- `run_job` returns `{"job_id": "<id>"}`; wait with `wait_job(job_id=..., timeout_s=...)`.
- `gate_start` returns `{"job_id": "<id>"}`; wait with `wait_job(job_id=..., timeout_s=...)`.
- `plan_run` returns `{"plan": "<slug>", "runner_pid": ...}`; wait with `plan_wait(plan_path=..., timeout_s=...)`.
- `plan_review` returns `{"handle": "<handle>", ...}`; wait with `plan_review_wait(handle=..., timeout_s=...)`.

While running, `wait_job`, `plan_wait` and `plan_review_wait` return status `"running"` when `timeout_s` (at most 100s) elapses; call wait again until the job finishes.

### Destructive tools

Only `plan_merge` is destructive (`destructiveHint=True`) because it modifies git branches and checkouts by merging ready tasks. All other tools are read-only (`readOnlyHint=True`) or start detached background jobs.

## Claude Code hooks

When installed as a Claude Code plugin, `agent-hub` registers two lifecycle hooks (`hooks/hooks.json`):

- `SessionStart` (triggered on `startup`, `resume`, `clear`, `compact`): checks for in-flight work in the repository (alive background jobs, active or unmerged plans, and any autopilot resume protocol in `docs/next-phase.md`) and injects it as context for the session. If nothing is running, it remains silent.
- `Stop`: guards against ending turns while detached background jobs or plan runners are still actively running in the repository, blocking the exit and reminding you to wait with `agent-hub wait -d <wt>` or `agent-hub plan wait <plan>`. To prevent hanging sessions indefinitely, the Stop guard gives up after 20 blocks.

To turn them off:
- Disable the plugin with `/plugin` disable in Claude Code.
- Alternatively, disable hooks in Claude Code settings (`settings.json`) by setting `"disableAllHooks": true` or removing the entries in settings.

## Exit codes

| Exit | Status | Meaning |
|---|---|---|
| 0 | `ok` | An agent finished and returned a non-empty reply. |
| 1 | `failed` | The agent failed for a reason that is not quota, auth or model. |
| 2 | | Usage or configuration error (bad flags, unknown agent, brief missing headings, unknown job id). |
| 3 | `refused` | A guard refused the work directory. |
| 4 | `exhausted` | No link could run (all skipped, or each fell through). |
| 5 | `modified-tree` | A review changed the work tree. |
| 6 | `timeout` | The attempt hit its timeout. |
| 7 | `empty` | The agent exited cleanly but returned nothing. |
| 8 | `interrupted` | Quota, auth or model failure after the tree had changed; no fallback. |
| 9 | `gate-failed` | A gate command failed. |
| 10 | `scope-violation` | `--allow` was given and files outside the globs changed. |
| 130 | `cancelled` | `cancel` or Ctrl-C. |
| 124 | | `wait` only: the job is still running when the timeout expires. |

## Development

```sh
just lint     # ruff check + ruff format --check
just test     # pytest -q, against fake agents only
just smoke    # real agents, run by hand; spends quota
```

Tests never run a real agent. Design decisions and their reasons are in [docs/decisions.md](docs/decisions.md);
the design is in `docs/superpowers/specs/`.

## Credits

The rationalization tables in the brief template and the principle skills follow the idea in
[agent-skills](https://github.com/addyosmani/agent-skills) by Addy Osmani (MIT License).
