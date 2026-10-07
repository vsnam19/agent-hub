# agent-hub

`agent-hub` lets one coding agent (the orchestrator, normally Claude Code) hand well-specified work to other coding
agents on this machine and get back a record it can verify. Jobs are picked by role (`implement`, `review`); each
role is an ordered chain of `agent:model` links, and the CLI walks the chain, skipping links that are on cooldown
and falling back to the next one on quota, auth, model-unavailable or not-installed. It has no runtime
dependencies (Python 3.12+ standard library only).

## Install

```sh
uv tool install --editable .
```

Install the `orchestrate` skill for Claude Code by symlinking it:

```sh
ln -s "$PWD/skills/orchestrate" ~/.claude/skills/orchestrate
```

## Pool

The shipped pool is `agenthub/default_pool.toml`. Override it with `$XDG_CONFIG_HOME/agent-hub/pool.toml` (or
`$AGENT_HUB_CONFIG/pool.toml`). A role defined in your file replaces that role entirely; other roles come from the
default, and `[defaults]` keys merge one by one.

```toml
[defaults]
quota_cooldown = "60m"        # used when the agent does not say when quota resets
timeout = { implement = "60m", review = "20m" }

[roles.implement]
chain = ["<agent>:<model>", "<agent>:<model>"]
```

Durations accept `s`, `m`, `h`. The agent part of a link must name a registered adapter under `agenthub/agents/`.
State (cooldowns and job directories) lives in `$XDG_STATE_HOME/agent-hub/` or `$AGENT_HUB_STATE`.

## Commands

```
agent-hub run --role {implement,review} [--brief BRIEF] -d WORKDIR [--agent AGENT] [--base BASE] [--timeout TIMEOUT] [--json]
agent-hub pool [--json] [--clear AGENT:MODEL]
agent-hub profile AGENT
agent-hub jobs [--limit LIMIT]
agent-hub show [--json] ID
agent-hub cancel ID
agent-hub wait -d WORKDIR [--timeout TIMEOUT] [--json]
```

- `wait` blocks until the newest job for that work directory ends (or until `--timeout`, default 9m), and prints its result like `show` (or `--json`).
- `run` runs one job in the foreground and prints a short summary then the reply; `--json` prints only the result
  record. `--brief` is a file, or `-` for stdin, and is required for `implement` (optional for `review`).
  `--agent` replaces the chain with that single `agent:model` link. `--timeout` takes a duration such as `30m`.
- `implement` briefs must contain `## Input` and `## Expected output` headings, and `-d` must be a git work tree on
  a non-default branch. The CLI never creates worktrees.
- `review` is read-only. The CLI computes `git diff <base>...HEAD` into the job's `diff.patch` and the reviewer reads
  that file; the reviewer never gets a shell. `--base` defaults to the repository's default branch. The work tree is
  fingerprinted before and after, and any change fails the job as `modified-tree`.
- `pool` lists every role, its chain, and per link whether it is installed, its version and any cooldown;
  `--clear` lifts one cooldown.
- `profile` prints an agent's `profile.md`, with strengths, weaknesses and what to put in a brief.
- `jobs` lists recent jobs, newest first; `show` prints a job's result and reply.
- `cancel` sends SIGTERM to the job's recorded pid, after verifying the pid against the process start time so a
  recycled pid is never signalled. If the process is gone, a job left `running` is marked `cancelled`.
- Human output on a TTY strips control characters from agent text; `--json` output is not altered.

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
