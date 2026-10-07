# agent-hub v1 — orchestrator CLI and skill

Status: approved for implementation (autonomous mode, see `docs/decisions.md`). Date: 2026-10-08.

## 1. Purpose

Let one agent (Claude Code, the *orchestrator*) hand well-specified work to other coding agents on this machine —
agy, codex, Claude Code headless, and any added later — and get back a record it can verify. The orchestrator keeps
design, planning, verification and the final decision; the pool does the writing (`implement`) and gives second
opinions (`review`).

Success means:

- `agent-hub run --role implement --brief b.md -d <worktree>` makes the change through the best agent that still has
  quota, and reports which agent and model actually ran, what changed, and the agent's reply.
- A quota failure moves to the next link automatically and remembers the cooldown for later jobs.
- `agent-hub run --role review` gets a read-only review of a branch from a different model, and fails loudly if the
  reviewer touched the tree.
- Adding an agent means adding one directory under `agenthub/agents/`; nothing else changes.
- The `orchestrate` skill teaches Claude the full loop without naming any agent.

Out of scope for v1 (each a later spec): the MCP server; installing skills and MCP servers into each agent's
config (the *hub*); the vsndevenv module and GUI page; roles other than implement and review; detached jobs; a
queue or daemon; per-agent skills.

## 2. Repository layout

```
agent-hub/
  pyproject.toml  uv.lock  justfile  README.md  .gitignore
  agenthub/
    __init__.py
    cli.py            argparse entry point: run, pool, profile, jobs, show, cancel
    config.py         paths (XDG + AGENT_HUB_CONFIG / AGENT_HUB_STATE overrides), pool loading
    default_pool.toml the shipped pool
    pool.py           chain resolution, cooldown store (file-locked)
    jobs.py           job ids, job directory, result record, listing
    guards.py         work-tree checks, fingerprint, changed files, diffstat
    env.py            child environment filtering
    process.py        run one invocation: files for stdio, process group, timeout, cancel
    brief.py          brief validation and the review preamble
    orchestrate.py    the run loop: walk the chain, attempt, classify, fall back, write result
    agents/
      __init__.py     Adapter base, Outcome, Request, Invocation, registry (discovers subpackages)
      agy/      __init__.py (adapter)  agent.toml  profile.md
      codex/    __init__.py  agent.toml  profile.md
      claude/   __init__.py  agent.toml  profile.md
  skills/
    orchestrate/SKILL.md
  tests/
    fakes/            fake agy, codex, claude executables (Python scripts)
    conftest.py       isolated HOME/XDG dirs, a PATH of fakes, temp git repos
    test_*.py
  scripts/smoke.sh    real-agent smoke test, run by hand only
  docs/decisions.md  docs/superpowers/{specs,plans}/
```

## 3. Configuration

**Pool.** `agenthub/default_pool.toml` ships with the package; `$XDG_CONFIG_HOME/agent-hub/pool.toml` (or
`$AGENT_HUB_CONFIG/pool.toml`) overrides it. A role defined in the user file replaces that role entirely; other
roles come from the default. `[defaults]` keys merge one by one.

```toml
[defaults]
quota_cooldown = "60m"        # used when the agent does not say when quota resets
timeout = { implement = "60m", review = "20m" }

[roles.implement]
chain = ["agy:claude-opus-4-6-thinking", "agy:claude-sonnet-4-6", "agy:gemini-3.1-pro-high",
         "codex:gpt-5.6-terra", "claude:sonnet"]

[roles.review]
chain = ["codex:gpt-5.6-terra", "agy:gemini-3.1-pro-high", "claude:sonnet"]
```

A link is `agent:model`; the agent part must name a registered adapter (unknown names are a config error, exit 2).
Durations accept `s`, `m`, `h`.

**agent.toml** (per agent):

```toml
binary = "codex"               # looked up on PATH
version_args = ["--version"]   # for `agent-hub pool`
env_passthrough = []           # secret-looking variables this agent legitimately needs
read_only = "native"           # "native" if the agent has a real read-only mode, else "best-effort"
```

**State.** `$XDG_STATE_HOME/agent-hub/` (or `$AGENT_HUB_STATE`): `cooldowns.json` and `jobs/`.

## 4. Commands

| Command | Does | Exit |
|---|---|---|
| `run --role R --brief FILE\|- -d DIR [--agent A:M] [--base REF] [--timeout D] [--json]` | One job (§5). `--agent` replaces the chain with that single link. `--base` (review only, default: the repo's default branch) is the ref the review compares against. | §6 |
| `pool [--json] [--clear A:M]` | Every role, its chain, and per link: installed?, version, cooldown until. `--clear` removes one cooldown. | 0 |
| `profile AGENT` | Prints the agent's `profile.md`. | 0 / 2 |
| `jobs [--limit N]` | Newest first: id, role, status, agent:model, duration, workdir. | 0 |
| `show ID [--json]` | The job's result and reply. | 0 / 2 |
| `cancel ID` | SIGTERM to the job's recorded CLI pid (which then stops its attempt, §5.1); if the pid is gone, marks a job left `running` as `cancelled`. | 0 / 2 |

`run` prints a short human summary then the reply; `--json` prints only the result record (§5.4).

## 5. The run loop

### 5.1 Before any attempt

1. Load the pool; resolve the chain (or the single `--agent` link).
2. Read the brief (file or stdin). `implement`: require `## Input` and `## Expected output` headings (case-insensitive,
   any heading level ≥ 2) else exit 2. `review`: the brief is optional; the CLI prepends the review preamble
   (§5.3).
3. Guards. `-d` must exist and be inside a git work tree. `implement`: the current branch must not be the default
   branch (`origin/HEAD` if set, else `main`/`master` whichever exists) and HEAD must not be detached — else exit 3.
4. Create the job directory `jobs/<YYYYmmdd-HHMMSS>-<role>-<4 hex>/` with `request.json`, `brief.md` (the text as
   sent), and `pid` (the CLI's own pid). On SIGTERM or SIGINT the CLI kills the running attempt's process group,
   records `cancelled` and exits 130. A `result.json` with status `running` is written now and replaced at the end.
5. Fingerprint the work tree: `git rev-parse HEAD` + `git status --porcelain=v1 -z --untracked-files=all` + a hash
   of the content of every changed or untracked file.

### 5.2 Attempts

For each link in order:

- Skip, recording why, if: the adapter's binary is not on PATH (`not-installed`); the link is on cooldown
  (`cooldown`).
- Otherwise build the invocation through the adapter and run it (`process.py`): cwd = workdir, stdin = the brief
  file or `/dev/null`, stdout/stderr to `attempts/<n>-<agent>-<model>/{stdout,stderr}`, new session (own process
  group), filtered environment (§7), timeout from `--timeout` or the role default. On timeout: SIGTERM to the
  group, SIGKILL after 10 s.
- The adapter classifies the attempt into an **Outcome**: `ok`, `quota` (with optional reset time), `auth`,
  `model-unavailable`, `empty`, `timeout`, `failed`. It also extracts the reply text into `reply.md`.
- `quota` writes the cooldown. Then:
  - `ok` → done.
  - `quota`, `auth`, `model-unavailable` → recompute the fingerprint; if unchanged, try the next link; if changed,
    stop with status `interrupted` (exit 8).
  - `empty`, `timeout`, `failed` → stop.
- If every link was skipped or fell through: status `exhausted` (exit 4).

### 5.3 Review specifics

The preamble sent before the orchestrator's brief:

> You are reviewing the changes on the current branch against `<base>` in this repository. Read them with
> `git diff <base>...HEAD` and the files involved. Do not modify any file. Report findings as a list, most severe
> first; for each give: severity (critical/important/minor), file:line, the problem, and a concrete failure
> scenario. Say "no findings" if there are none.

After the attempt, the fingerprint is recomputed; any change sets status `modified-tree` (exit 5) regardless of the
outcome. Review never falls back after an attempt that changed the tree.

### 5.4 Result record

`result.json`:

```json
{
  "id": "20261008-143012-implement-a1f3",
  "role": "implement", "status": "ok", "exit_code": 0,
  "agent": "agy", "model": "claude-sonnet-4-6",
  "workdir": "/abs/path", "base": null,
  "started": "2026-10-08T14:30:12+07:00", "ended": "…", "duration_s": 812.4,
  "attempts": [
    {"n": 1, "agent": "agy", "model": "claude-opus-4-6-thinking", "outcome": "skipped", "reason": "cooldown until 2026-10-12T09:00:00+07:00"},
    {"n": 2, "agent": "agy", "model": "claude-sonnet-4-6", "outcome": "ok", "rc": 0, "duration_s": 812.1}
  ],
  "changed_files": ["src/x.py", "tests/test_x.py"],
  "diffstat": " 2 files changed, 40 insertions(+), 3 deletions(-)",
  "reply_path": "/abs/jobs/…/attempts/2-agy-claude-sonnet-4-6/reply.md"
}
```

`changed_files` / `diffstat` compare against the HEAD recorded at job start and include uncommitted and untracked
files (the agent may or may not commit).

## 6. Exit codes and statuses

| Exit | Status | Meaning |
|---|---|---|
| 0 | `ok` | An agent finished and returned a non-empty reply. |
| 1 | `failed` | The agent failed for a reason that is not quota/auth/model. |
| 2 | — | Usage or configuration error (bad flags, unknown agent, brief missing required headings, unknown job id). |
| 3 | `refused` | A guard refused the work directory. |
| 4 | `exhausted` | No link could run (all skipped, or each fell through). |
| 5 | `modified-tree` | A review changed the work tree. |
| 6 | `timeout` | The attempt hit its timeout. |
| 7 | `empty` | The agent exited cleanly but returned nothing. |
| 8 | `interrupted` | Quota/auth/model failure after the tree had changed; no fallback. |
| 130 | `cancelled` | `cancel` or Ctrl-C. |

## 7. Child environment

Start from the CLI's environment. Remove every variable whose name matches
`(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)` or starts with `FNOX_`, `AWS_`, `GH_`,
`GITHUB_`, except names in the adapter's `env_passthrough`. Always remove `CLAUDECODE` and `CLAUDE_CODE_*`.
`result.json` never records environment values; `request.json` records only the names removed.

## 8. Adapters

Base interface (`agenthub/agents/__init__.py`):

```python
@dataclass
class Request:
    role: str            # "implement" | "review"
    model: str
    workdir: Path
    brief_path: Path     # the full text to send (preamble included for review)
    attempt_dir: Path
    git_common_dir: Path

@dataclass
class Invocation:
    argv: list[str]
    stdin_path: Path | None   # None → /dev/null

class Outcome(StrEnum): OK, QUOTA, AUTH, MODEL_UNAVAILABLE, EMPTY, TIMEOUT, FAILED

@dataclass
class Classified:
    outcome: Outcome
    reply: str
    reset_at: datetime | None = None
    detail: str = ""

class Adapter:
    name: str; config: AgentConfig; profile_path: Path
    def build(self, req: Request) -> Invocation: ...
    def classify(self, rc: int, attempt_dir: Path) -> Classified: ...   # reads stdout/stderr files there
```

Timeouts are decided by `process.py`, not the adapter. Classification scans the agent's **diagnostics** (stderr,
structured error fields), never the model's reply text, so a reply mentioning "quota" is not misread.

### 8.1 agy

- argv: `agy --model M --output-format json --add-dir <attempt_dir> [--dangerously-skip-permissions | --mode plan]
  -p "Read the file <attempt_dir>/brief.md and carry out the task it describes. Work in the current directory."`
  (`-p` last: it takes the prompt as its value.) implement → skip-permissions; review → `--mode plan`.
  The brief is copied to `<attempt_dir>/brief.md`. stdin `/dev/null`.
- stdout is one JSON object; parse with `json.loads(strict=False)` (agy emits raw newlines in strings). Reply =
  `response`; error text = `error` field + stderr.
- Classify (case-insensitive on error text): `quota`, `rate limit`, `resource exhausted` → QUOTA; `unauthenticated`,
  `unauthorized`, `sign in`, `please authenticate`, `reauth` → AUTH; `not a known model`, `is not recognized as a
  known model`, `invalid --model` → MODEL_UNAVAILABLE; stderr `print timeout after … returning partial output` →
  TIMEOUT; rc ≠ 0 otherwise → FAILED; rc 0 with blank reply → EMPTY; else OK. If stdout is not JSON, fall back to
  stdout as the reply.

### 8.2 codex

- argv: `codex exec -m M -C <workdir> --json -o <attempt_dir>/reply.md --skip-git-repo-check` plus implement →
  `-s workspace-write -c sandbox_workspace_write.network_access=true --add-dir <git_common_dir>`; review →
  `-s read-only`. Brief on stdin (codex reads stdin when no prompt is given).
- Reply = `reply.md` written by `-o`. Error text = stderr + the `message` of JSONL events whose `type` is `error`
  or `turn.failed`.
- Classify: `usage limit`, `usage_limit_reached`, `rate limit`, `quota` → QUOTA, reset from `resets_in_seconds` or
  `try again at <time>` when present; `not logged in`, `login`, `401`, `unauthorized` → AUTH;
  `model … not found`, `unsupported model`, `does not exist` → MODEL_UNAVAILABLE; then rc/empty as for agy.

### 8.3 claude

- argv: `claude -p --model M --output-format json --no-session-persistence` plus implement →
  `--permission-mode bypassPermissions`; review → `--allowedTools Read Grep Glob "Bash(git diff:*)"
  "Bash(git log:*)" "Bash(git show:*)" "Bash(git status:*)"`. Brief on stdin.
- stdout is one JSON object: reply = `result`; `is_error` / `subtype` / `result` text for errors.
- Classify: `usage limit`, `hit your limit`, `rate limit`, `quota` in an error result → QUOTA, reset from
  `resets <time>` / `|<epoch>` when present; `invalid api key`, `please run /login`, `not logged in`,
  `authentication` → AUTH; `model … not found`, `invalid model` → MODEL_UNAVAILABLE; then rc/empty.

### 8.4 profile.md

Each profile states, in under 40 lines: strengths, weaknesses and known bad habits (for agy/Gemini: "writes
assertion-free tests to satisfy required names — scan every new test"), what to put in a brief for this agent,
and its read-only quality for review.

## 9. The `orchestrate` skill

`skills/orchestrate/SKILL.md`, frontmatter `name: orchestrate`, a description that triggers on delegating
implementation, a second-opinion review, or "use the agent pool". Body:

1. **Decide what to delegate.** Only implementation and second-opinion review go to the pool; design, planning,
   research, verification and the final judgement stay with you. Tiny edits are cheaper to do yourself.
2. **Look at the pool.** `agent-hub pool`; `agent-hub profile <agent>` for the agents you are about to use.
3. **Write the brief** from the template (Goal / Input / Expected output / Constraints / Gate). Input names the files,
   current behaviour and interfaces; Expected output names the files to change, the exact test names that must
   pass, and the exact commands with their expected result. The gate always includes the repository's full lint
   and the existing tests of every touched file, and forbids tests without assertions.
4. **Prepare a worktree** on a feature branch (the CLI refuses the default branch).
5. **Run in the background:** `agent-hub run --role implement --brief brief.md -d <worktree> --json` with the Bash
   tool's `run_in_background`; do other work until notified.
6. **Read the result** and act on its status: `ok` → verify; `exhausted` → report which links failed and why;
   `interrupted` / `failed` / `timeout` / `empty` → inspect the tree and the reply, then re-brief or take over;
   never re-run blindly on a dirty tree.
7. **Verify yourself** in the worktree: run the gate, read the diff, scan new tests for missing assertions. The
   agent's claims are not evidence.
8. **Optional second opinion:** `agent-hub run --role review -d <worktree> --base <default branch>`; treat findings
   as input to your own judgement.
9. To force a link: `--agent agent:model` (for example when one model's output is poor).

The skill names no specific agent or model; examples use placeholders.

## 10. Testing

- `tests/fakes/{agy,codex,claude}`: Python scripts that record their argv, stdin and selected environment to
  `$FAKE_LOG_DIR/<name>.<n>.json`, then act on `FAKE_<NAME>_SCENARIO` (a comma list consumed one per call):
  `ok`, `quota`, `quota-reset` (prints a reset time), `auth`, `model`, `empty`, `fail`, `slow` (sleeps past a
  short timeout, spawning a child that also sleeps), `write` (creates a file in cwd, then succeeds), `write-quota`
  (writes, then reports quota). Each fake prints output in its real agent's format (§8).
- `conftest.py` gives every test an isolated `HOME`, `XDG_*`, `AGENT_HUB_*`, and a `PATH` of the fakes + `/usr/bin:/bin`
  only (plus git), and a temp git repo with a `master` default branch and a `feat/x` branch checked out.
- Coverage that must exist: brief validation; each guard; fallback on quota/auth/model/not-installed; no fallback on
  fail/timeout; `interrupted` after write-quota; cooldown written, honoured, expired and cleared; review read-only
  argv per agent and `modified-tree`; timeout kills the process group (the fake's child is gone); env filtering;
  each adapter's argv and classification on its fake output, including a reply that contains the word "quota"
  classified OK; result.json shape; `jobs`, `show`, `cancel`, `pool`, `profile`; unknown agent in pool → exit 2.
- `just lint` = `ruff check` + `ruff format --check`; `just test` = `pytest -q`. Both must be green.
- `scripts/smoke.sh` (`just smoke`): creates a temp repo, and for each installed agent runs a tiny implement job
  (add a function and its test) and a review job, printing statuses. Run by hand; never in `just test`.

## 11. README

What it is, install (`uv tool install --editable .`), the skill symlink
(`ln -s "$PWD/skills/orchestrate" ~/.claude/skills/orchestrate`), the pool file, the commands, exit codes, and a
pointer to `docs/decisions.md`.
