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

## Commands

```
agent-hub run --role ROLE [--brief BRIEF] -d WORKDIR [--agent AGENT:MODEL] [--exclude AGENT:MODEL]... [--base BASE] [--timeout TIMEOUT] [--gate CMD]... [--allow GLOB]... [--jev] [--json]
agent-hub pool [--json] [--clear AGENT:MODEL]
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
  `--clear` lifts one cooldown.
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
