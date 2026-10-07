# agent-hub v1 Orchestrator CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> In this repository each task is delegated as one brief (Input / Expected output) to the implementing agent, and
> verified by the orchestrator before the next task starts.

**Goal:** Build the `agent-hub` CLI that runs implement/review jobs on a role-based chain of coding agents (agy,
codex, claude) with quota fallback, plus the `orchestrate` skill.

**Architecture:** A dependency-free Python package `agenthub`. Small modules with one job each (config, pool,
jobs, guards, env, brief, process) feed `orchestrate.py`, which walks a role's chain through per-agent adapters
discovered from `agenthub/agents/<name>/`. Tests drive real subprocesses against fake agent executables.

**Tech Stack:** Python ≥ 3.12 (stdlib only at runtime), uv, pytest, ruff, just, git.

**Spec:** `docs/superpowers/specs/2026-10-08-orchestrator-cli-design.md` (section numbers below refer to it).
Decisions: `docs/decisions.md`.

## Global Constraints

- Python `>=3.12`; runtime `dependencies = []`; dev group: `pytest>=8`, `ruff>=0.16`.
- Build backend `uv_build>=0.12,<0.13`, `module-name = "agenthub"`, `module-root = ""`; console script
  `agent-hub = "agenthub.cli:main"`.
- ruff: `target-version = "py312"`, `line-length = 120`, `select = ["E", "F", "W", "I", "B"]`.
- `just lint` = `uv run ruff check . && uv run ruff format --check .`; `just test` = `uv run pytest -q`. Both green
  at the end of every task, for the whole repository.
- Tests never execute a real `agy`, `codex` or `claude`, and never read the real `~/.config`, `~/.local/state`,
  `~/.codex`, `~/.claude` or `~/.gemini`. Every test that needs agents uses the fakes PATH from `conftest.py`.
- No test without an assertion. No `pass`-only or `assert True` tests.
- Paths come from `config.paths()` only; no module hardcodes `~`.
- English for code, comments, docs.

## Review Focus

1. A brief with Windows line endings or headings like `### Expected Output` (different case/level) → accepted
   (Task 2 test `test_brief_headings_any_case_and_level`).
2. Two jobs hitting quota at the same moment → the cooldown file is never corrupted (Task 1 test
   `test_cooldown_concurrent_writes`).
3. `-d` pointing at a subdirectory of a work tree, or a path with spaces → guards and git commands still work
   (Task 2 test `test_guard_subdirectory_and_spaces`).
4. An agent that exits but leaves a grandchild holding stdout → the CLI still returns, because stdio goes to files
   (Task 3 test `test_grandchild_holding_stdout_does_not_block`).
5. A reply containing "quota exceeded" text from a successful run → classified OK (Task 4 tests
   `test_<agent>_reply_mentioning_quota_is_ok` for each agent).

---

## File map

| File | Responsibility | Task |
|---|---|---|
| `pyproject.toml`, `justfile`, `agenthub/__init__.py` | packaging, gates | 1 |
| `agenthub/config.py` | paths, durations, pool loading/merging | 1 |
| `agenthub/default_pool.toml` | shipped pool | 1 |
| `agenthub/pool.py` | chain links, cooldown store | 1 |
| `agenthub/brief.py` | brief validation, review preamble | 2 |
| `agenthub/guards.py` | git checks, fingerprint, changes | 2 |
| `agenthub/env.py` | child env filtering | 2 |
| `agenthub/jobs.py` | job dirs, result records, listing | 2 |
| `agenthub/agents/__init__.py` | adapter base types, registry | 3 |
| `agenthub/process.py` | run one invocation with files, pgroup, timeout | 3 |
| `agenthub/agents/{agy,codex,claude}/` | adapters, agent.toml, profile.md | 4 |
| `tests/fakes/{agy,codex,claude}`, `tests/conftest.py` | fake agents, fixtures | 4 |
| `agenthub/orchestrate.py`, `agenthub/cli.py` | run loop, commands | 5 |
| `skills/orchestrate/SKILL.md`, `README.md`, `scripts/smoke.sh` | skill, docs, smoke | 6 |

---

### Task 1: Package skeleton, config and pool

**Files:**
- Create: `pyproject.toml`, `justfile`, `agenthub/__init__.py`, `agenthub/config.py`, `agenthub/default_pool.toml`,
  `agenthub/pool.py`, `tests/__init__.py` (empty), `tests/test_config.py`, `tests/test_pool.py`

**Interfaces — Produces:**

```python
# agenthub/config.py
@dataclass(frozen=True)
class Paths:
    config_dir: Path   # $AGENT_HUB_CONFIG or $XDG_CONFIG_HOME/agent-hub or ~/.config/agent-hub
    state_dir: Path    # $AGENT_HUB_STATE  or $XDG_STATE_HOME/agent-hub  or ~/.local/state/agent-hub
    @property
    def jobs_dir(self) -> Path: ...        # state_dir / "jobs"
    @property
    def cooldowns_file(self) -> Path: ...  # state_dir / "cooldowns.json"
def paths() -> Paths: ...                  # reads os.environ every call
class ConfigError(Exception): ...
def parse_duration(text: str) -> timedelta # "90s" "60m" "2h" "1h30m"; else ConfigError
@dataclass(frozen=True)
class Pool:
    roles: dict[str, list[str]]            # role -> ["agent:model", ...]
    quota_cooldown: timedelta
    timeouts: dict[str, timedelta]         # role -> timeout
def load_pool(p: Paths | None = None) -> Pool  # default_pool.toml merged with config_dir/pool.toml (spec §3)

# agenthub/pool.py
@dataclass(frozen=True)
class Link:
    agent: str
    model: str
    def __str__(self) -> str: ...          # "agent:model"
def parse_link(text: str) -> Link          # exactly one ':' split on the first ':'; empty part -> ConfigError
class Cooldowns:
    def __init__(self, path: Path): ...
    def until(self, link: Link, now: datetime) -> datetime | None  # None if absent or expired
    def set(self, link: Link, until: datetime) -> None
    def clear(self, link: Link) -> bool    # True if something was removed
    def all(self, now: datetime) -> dict[str, datetime]  # only unexpired
```

Cooldown file format: `{"agy:claude-opus-4-6-thinking": "2026-10-12T09:00:00+07:00", ...}`. Every read-modify-write
holds `fcntl.flock(LOCK_EX)` on `cooldowns.json.lock`, writes to a temp file in the same dir and `os.replace`s it.
A missing or unparsable file reads as empty. Datetimes are timezone-aware; naive input raises `ValueError`.

`load_pool` rules: role tables in the user file replace the default role wholesale; `[defaults]` keys override one
by one (`timeout` sub-keys merge per role). A role without a timeout uses 60m. Invalid TOML or a bad duration →
`ConfigError` naming the file.

`default_pool.toml` content: exactly the block in spec §3.

- [ ] **Step 1: Scaffold.** `pyproject.toml` per Global Constraints (name `agent-hub`, version `0.1.0`, description
  "Run coding agents from one orchestrator, by role, with quota fallback"); `[tool.pytest.ini_options] testpaths =
  ["tests"]`; `justfile` with `default` (`@just --list`), `lint`, `test`, `smoke` (`scripts/smoke.sh`); run
  `uv sync`.
- [ ] **Step 2: Write the failing tests** in `tests/test_config.py`:
  `test_paths_honour_agent_hub_env`, `test_paths_fall_back_to_xdg`, `test_paths_fall_back_to_home`,
  `test_parse_duration_valid` (parametrized: `90s`→90 s, `60m`, `2h`, `1h30m`→5400 s),
  `test_parse_duration_invalid` (parametrized: `""`, `10`, `5d`, `m`, `-5m` → `ConfigError`),
  `test_load_pool_default_only` (no user file → implement chain equals the five links of §3, review timeout 20 min),
  `test_load_pool_user_role_replaces_default_role`, `test_load_pool_user_defaults_merge` (user sets only
  `quota_cooldown = "10m"` → timeouts still from default), `test_load_pool_invalid_toml_names_file`.
  And `tests/test_pool.py`: `test_parse_link_ok`, `test_parse_link_invalid` (`"agy"`, `":m"`, `"a:"`),
  `test_parse_link_model_with_colon_kept` (`"codex:gpt:5"` → model `"gpt:5"`), `test_cooldown_roundtrip`,
  `test_cooldown_expired_reads_none`, `test_cooldown_clear`, `test_cooldown_corrupt_file_reads_empty`,
  `test_cooldown_naive_datetime_rejected`, `test_cooldown_concurrent_writes` (8 processes via
  `multiprocessing` each `set` a distinct link; afterwards the file parses and holds all 8).
  Use `monkeypatch.setenv` / `tmp_path`; never touch the real home.
- [ ] **Step 3:** `uv run pytest -q` → FAIL (modules missing).
- [ ] **Step 4:** Implement `config.py`, `pool.py`, `default_pool.toml` (load it with `importlib.resources`).
- [ ] **Step 5:** `just lint && just test` → all pass.
- [ ] **Step 6: Commit** `feat: package skeleton, config paths, pool and cooldowns`.

---

### Task 2: Brief, guards, env, jobs

**Files:**
- Create: `agenthub/brief.py`, `agenthub/guards.py`, `agenthub/env.py`, `agenthub/jobs.py`, `tests/gitrepo.py`,
  `tests/test_brief.py`, `tests/test_guards.py`, `tests/test_env.py`, `tests/test_jobs.py`

**Interfaces — Consumes:** `config.Paths`. **Produces:**

```python
# agenthub/brief.py
class BriefError(Exception): ...
def validate(role: str, text: str) -> None
    # implement: needs a heading (level >= 2, '#'*n + space) whose text is "input" and one whose text is
    # "expected output", case-insensitive, trailing spaces/colon ignored, \r\n tolerated; else BriefError listing
    # the missing ones. review: always OK. unknown role: BriefError.
def review_text(base: str, brief: str | None) -> str
    # spec §5.3 preamble with <base> substituted, then "\n\n## Orchestrator notes\n\n" + brief if brief is non-blank

# agenthub/guards.py
class GuardError(Exception): ...
@dataclass(frozen=True)
class Repo:
    root: Path            # git rev-parse --show-toplevel
    common_dir: Path      # git rev-parse --git-common-dir, made absolute
    branch: str | None    # None when detached
    default_branch: str   # origin/HEAD target if set, else "main" if it exists, else "master" if it exists, else GuardError
def inspect(workdir: Path) -> Repo          # GuardError if not a dir / not in a work tree
def check_implement(repo: Repo) -> None     # GuardError if detached or branch == default_branch
def fingerprint(repo: Repo) -> str          # sha256 hex of HEAD + porcelain -z (untracked all) + content of each listed path
def head(repo: Repo) -> str
def changes(repo: Repo, since: str) -> tuple[list[str], str]
    # (sorted changed paths incl. untracked, diffstat text) vs commit `since`, covering commits + worktree + untracked.
    # diffstat = `git diff --stat <since>` output's last line, plus "N untracked" note if any; "" when nothing changed.

# agenthub/env.py
SECRET_NAME = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)")
SECRET_PREFIXES = ("FNOX_", "AWS_", "GH_", "GITHUB_")
def child_env(base: Mapping[str, str], passthrough: Iterable[str] = ()) -> tuple[dict[str, str], list[str]]
    # (filtered env, sorted removed names). Always removes CLAUDECODE and CLAUDE_CODE_*; passthrough never rescues those.

# agenthub/jobs.py
@dataclass
class Job:
    id: str; dir: Path
    def write_json(self, name: str, data: dict) -> None   # atomic (temp + os.replace), indent=2
    def read_json(self, name: str) -> dict
    def attempt_dir(self, n: int, agent: str, model: str) -> Path  # creates dir/attempts/<n>-<agent>-<model-with-/-and-:-replaced-by-_>
def new_job(p: Paths, role: str, now: datetime) -> Job      # id "%Y%m%d-%H%M%S-<role>-<4 hex>"; creates the dir
def get_job(p: Paths, job_id: str) -> Job                   # KeyError if missing
def list_jobs(p: Paths, limit: int) -> list[dict]           # result.json of newest jobs first (by id), skips dirs without one
```

`tests/gitrepo.py`: `make_repo(path: Path, default: str = "master", branch: str | None = "feat/x") -> Path` — `git
init -b <default>`, local `user.name/email`, `commit.gpgsign=false`, one committed file `README.md`, then checkout
`branch` if given. All git calls in code use `subprocess.run([...], cwd=..., check=..., capture_output=True,
text=True)` with `GIT_TERMINAL_PROMPT=0`.

- [ ] **Step 1: Write failing tests.**
  brief: `test_implement_brief_ok`, `test_implement_brief_missing_both_lists_both`,
  `test_brief_headings_any_case_and_level` (`### Expected Output:` with `\r\n` → ok), `test_h1_does_not_count`,
  `test_review_brief_optional`, `test_unknown_role`, `test_review_text_contains_base_and_notes`,
  `test_review_text_without_notes_has_no_notes_section`.
  guards: `test_inspect_not_a_repo`, `test_inspect_missing_dir`, `test_default_branch_master`,
  `test_default_branch_main_preferred_when_both_exist`, `test_check_implement_refuses_default_branch`,
  `test_check_implement_refuses_detached`, `test_check_implement_ok_on_feature`,
  `test_guard_subdirectory_and_spaces` (repo at `tmp_path/"my repo"`, workdir its subdir `src` → root is the repo),
  `test_fingerprint_changes_on_edit_untracked_and_commit` (three separate changes each alter it; no change → equal),
  `test_changes_reports_committed_modified_untracked`, `test_changes_empty`,
  `test_common_dir_of_linked_worktree` (a `git worktree add` dir → common_dir is the main repo's `.git`).
  env: `test_removes_secret_like_names` (parametrized over `GH_TOKEN`, `OPENAI_API_KEY`, `MY_SECRET`,
  `DB_PASSWORD`, `FNOX_X`, `AWS_REGION`, `GITHUB_X`, `SSH_PRIVATE_KEY`), `test_keeps_normal_names` (`PATH`, `HOME`,
  `LANG`, `TERM`, `KEYBOARD`), `test_passthrough_keeps_named`, `test_claudecode_always_removed`.
  jobs: `test_new_job_id_format_and_dir`, `test_ids_unique_same_second`, `test_write_json_atomic_roundtrip`,
  `test_attempt_dir_sanitises_model`, `test_get_job_missing`, `test_list_jobs_newest_first_and_limit`,
  `test_list_jobs_skips_dirs_without_result`.
- [ ] **Step 2:** `uv run pytest -q` → new tests FAIL.
- [ ] **Step 3:** Implement the four modules.
- [ ] **Step 4:** `just lint && just test` → pass.
- [ ] **Step 5: Commit** `feat: brief validation, git guards, env filtering, job records`.

---

### Task 3: Adapter base and process runner

**Files:**
- Create: `agenthub/agents/__init__.py`, `agenthub/process.py`, `tests/test_process.py`, `tests/test_registry.py`

**Interfaces — Consumes:** nothing new. **Produces:**

```python
# agenthub/agents/__init__.py
class Outcome(StrEnum):
    OK = "ok"; QUOTA = "quota"; AUTH = "auth"; MODEL_UNAVAILABLE = "model-unavailable"
    EMPTY = "empty"; TIMEOUT = "timeout"; FAILED = "failed"
@dataclass(frozen=True)
class AgentConfig:
    binary: str; version_args: list[str]; env_passthrough: list[str]; read_only: str  # "native" | "best-effort"
@dataclass(frozen=True)
class Request:
    role: str; model: str; workdir: Path; brief_path: Path; attempt_dir: Path; git_common_dir: Path
@dataclass(frozen=True)
class Invocation:
    argv: list[str]; stdin_path: Path | None
@dataclass(frozen=True)
class Classified:
    outcome: Outcome; reply: str; reset_at: datetime | None = None; detail: str = ""
class Adapter:
    name: str                        # set by the registry from the directory name
    config: AgentConfig              # loaded from agent.toml beside the module
    profile_path: Path               # profile.md beside the module
    def build(self, req: Request) -> Invocation: raise NotImplementedError
    def classify(self, rc: int, attempt_dir: Path) -> Classified: raise NotImplementedError
    def installed(self) -> bool      # shutil.which(config.binary) is not None
    def version(self) -> str | None  # first line of `<binary> <version_args>` (10 s timeout), None on any error
def registry() -> dict[str, Adapter]
    # imports every subpackage of agenthub.agents that has agent.toml; the subpackage must define ADAPTER
    # (an Adapter subclass); returns {name: instance}. Cached per process.
def common_stderr_text(attempt_dir: Path) -> str     # contents of attempt_dir/"stderr" or ""

# agenthub/process.py
@dataclass(frozen=True)
class Ran:
    rc: int; timed_out: bool; duration_s: float; pgid: int
def run(inv: Invocation, cwd: Path, env: dict[str, str], attempt_dir: Path, timeout: timedelta,
        on_start: Callable[[int], None] = lambda pgid: None) -> Ran
    # stdout -> attempt_dir/"stdout", stderr -> attempt_dir/"stderr" (files opened by us, passed as fds);
    # stdin from inv.stdin_path or /dev/null; start_new_session=True; on_start(pid) after spawn;
    # wait with timeout; on timeout killpg SIGTERM, wait 10 s, killpg SIGKILL; rc = -signal when killed.
    # After the leader exits, also killpg SIGTERM any remaining group members (ignore ProcessLookupError).
def terminate(pgid: int) -> None   # SIGTERM group, 10 s grace, SIGKILL; ignores missing groups
```

Registry test: until Task 4 there are no agents, so `test_registry.py` creates a throwaway package under
`tmp_path` and points a helper `discover(package_dir: Path, package_name: str) -> dict[str, Adapter]` at it;
`registry()` is `discover(<agenthub/agents dir>, "agenthub.agents")`. Expose `discover` too.

- [ ] **Step 1: Write failing tests.**
  process (use small `sys.executable -c` scripts written to `tmp_path`): `test_stdout_stderr_go_to_files`,
  `test_stdin_from_file`, `test_stdin_devnull_when_none`, `test_nonzero_rc`, `test_env_is_exact` (child sees a
  var we set and not one we omitted), `test_timeout_kills_group` (script spawns a sleeping child that writes its pid
  to a file, then sleeps; timeout 1 s → `timed_out`, and after return the child pid no longer exists),
  `test_grandchild_holding_stdout_does_not_block` (leader starts a detached-in-group grandchild that inherits
  stdout and sleeps 30 s, leader exits 0 at once → `run` returns in < 15 s with rc 0),
  `test_on_start_receives_pgid`, `test_terminate_missing_group_is_quiet`.
  registry: `test_discover_loads_adapter_with_config_and_profile`, `test_discover_skips_package_without_agent_toml`,
  `test_discover_rejects_missing_ADAPTER` (raises `TypeError` naming the package), `test_installed_and_version`
  (binary = a script in a tmp PATH dir printing `x 1.2.3`), `test_version_none_when_missing`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just lint && just test` → pass.
- [ ] **Step 5: Commit** `feat: adapter base, registry and process runner`.

---

### Task 4: The three adapters and their fakes

**Files:**
- Create: `agenthub/agents/{agy,codex,claude}/{__init__.py,agent.toml,profile.md}`,
  `tests/fakes/{agy,codex,claude}` (executable Python, `#!/usr/bin/env python3`), `tests/fakes/_common.py`,
  `tests/conftest.py`, `tests/test_adapter_agy.py`, `tests/test_adapter_codex.py`, `tests/test_adapter_claude.py`

**Interfaces — Consumes:** Task 3 types. **Produces:** `ADAPTER` in each subpackage; fixtures in `conftest.py`:

```python
@pytest.fixture
def env_sandbox(tmp_path, monkeypatch) -> SimpleNamespace
    # sets HOME, XDG_CONFIG_HOME, XDG_STATE_HOME, AGENT_HUB_CONFIG, AGENT_HUB_STATE under tmp_path;
    # PATH = tests/fakes + dirs of `git` and `python3` + /usr/bin:/bin; FAKE_LOG_DIR = tmp_path/"fakelog";
    # returns ns(home, config, state, fakelog)
@pytest.fixture
def repo(tmp_path) -> Path        # gitrepo.make_repo(tmp_path/"repo") on feat/x
def fake_calls(fakelog: Path, name: str) -> list[dict]   # helper: recorded calls in order
```

Fakes (`_common.py` shared logic): each call appends `{argv, stdin, cwd, env: {k: v for selected keys
CLAUDECODE, GH_TOKEN, HOME, PATH}}` to `$FAKE_LOG_DIR/<name>.<n>.json`; scenario = next item of
`FAKE_<NAME>_SCENARIO` (comma list; the consumed count is tracked in `$FAKE_LOG_DIR/<name>.count`; default `ok`).
`--version` prints `<name> 9.9.9` and is not recorded. Output formats per scenario and agent:

| scenario | agy (stdout JSON, rc) | codex (JSONL stdout + `-o` file, rc) | claude (stdout JSON, rc) |
|---|---|---|---|
| ok | `{"status":"SUCCESS","response":"done: <first 20 chars of prompt/stdin>"}`, 0 | events `{"type":"thread.started"}` `{"type":"turn.completed"}`; `-o` file `done`, 0 | `{"type":"result","subtype":"success","is_error":false,"result":"done"}`, 0 |
| ok-quota-text | as ok with response `"quota exceeded is mentioned here"` | `-o` file same text | result same text |
| quota | `{"status":"ERROR","error":"RESOURCE_EXHAUSTED: quota exceeded"}`, 1 | `{"type":"error","message":"You've hit your usage limit. Try again later."}`, 1 | `{"type":"result","is_error":true,"result":"Claude AI usage limit reached"}`, 1 |
| quota-reset | error adds nothing (agy has no reset) — same as quota | event has `"resets_in_seconds": 3600`, 1 | result `"You've hit your limit · resets 1760000000"` → reset epoch, 1 |
| auth | error `"UNAUTHENTICATED: please sign in"`, 1 | message `"401 Unauthorized: not logged in"`, 1 | result `"Invalid API key · Please run /login"`, 1 |
| model | error `"model foo is not a known model"`, 1 | message `"model 'x' not found"`, 1 | result `"invalid model: x"`, 1 |
| empty | `{"status":"SUCCESS","response":""}`, 0 | `-o` file empty, 0 | result `""`, 0 |
| fail | stderr `boom`, rc 2, stdout empty | stderr `boom`, 2 | stderr `boom`, 2 |
| slow | spawns `sleep 60` child, then sleeps 60 | same | same |
| write | creates `cwd/agent_wrote.txt`, then ok | same | same |
| write-quota | creates the file, then quota | same | same |

Adapter argv exactly per spec §8.1–8.3 (`brief_path` copied to `attempt_dir/"brief.md"` by the agy adapter in
`build`). Classification per spec §8.1–8.3: scan stderr + structured error fields only, never the reply. Reset time:
codex `resets_in_seconds` → now + seconds; claude `resets <epoch>` or `|<epoch>` → that UTC instant; otherwise
`None`. agent.toml: agy `read_only = "best-effort"`, codex and claude `"native"`; all `env_passthrough = []`.

profile.md content (≤ 40 lines each, spec §8.4):
- agy: hosts Claude Opus/Sonnet 4.6 and Gemini models; strong at bulk scaffolding and tests; Gemini models have
  written assertion-free tests to match required names — scan every new test; prompt arrives as a pointer to
  brief.md; review is best-effort read-only (plan mode), the CLI's fingerprint is the real guard.
- codex: GPT models, strong at precise multi-file edits and code review; real sandbox (workspace-write / read-only);
  network on for implement; good first choice for review.
- claude: Claude Code headless; last fallback for implement; review restricted to read + git read commands.

- [ ] **Step 1: Write failing tests**, per agent `X` in {agy, codex, claude}:
  `test_X_build_implement_argv`, `test_X_build_review_argv` (exact lists; codex implement includes
  `--add-dir <common_dir>`; agy review has `--mode plan` and no skip-permissions),
  `test_X_classify_ok`, `test_X_classify_quota`, `test_X_classify_auth`, `test_X_classify_model_unavailable`,
  `test_X_classify_empty`, `test_X_classify_fail`, `test_X_reply_mentioning_quota_is_ok`,
  and for codex/claude `test_X_quota_reset_time_parsed`. Each classification test runs the fake through
  `process.run` with the adapter's `build` output under `env_sandbox`, then calls `classify`. Plus
  `test_fake_records_call` and `test_registry_finds_three_agents` (names == {"agy","codex","claude"}, each
  `profile_path` exists and is ≤ 40 lines).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement fakes, conftest, adapters, agent.toml, profile.md; `chmod +x tests/fakes/{agy,codex,claude}`.
- [ ] **Step 4:** `just lint && just test` → pass.
- [ ] **Step 5: Commit** `feat: agy, codex and claude adapters with fake agents`.

---

### Task 5: Run loop and CLI

**Files:**
- Create: `agenthub/orchestrate.py`, `agenthub/cli.py`, `tests/test_run.py`, `tests/test_cli.py`

**Interfaces — Consumes:** everything above. **Produces:**

```python
# agenthub/orchestrate.py
EXIT = {"ok": 0, "failed": 1, "refused": 3, "exhausted": 4, "modified-tree": 5, "timeout": 6, "empty": 7,
        "interrupted": 8, "cancelled": 130}
@dataclass
class RunArgs:
    role: str; brief: str; workdir: Path; links: list[Link]; timeout: timedelta; base: str | None
def run_job(args: RunArgs, p: Paths, pool: Pool, now: Callable[[], datetime]) -> dict   # returns result.json content

# agenthub/cli.py
def main(argv: list[str] | None = None) -> int
```

`run_job` implements spec §5 exactly: guards (GuardError → status `refused`, still written as a job), job dir,
`request.json` (role, links, workdir, base, timeout_s, removed_env names), `brief.md`, `pid`, initial `result.json`
status `running`; fingerprint; per link: skip not-installed/cooldown; `attempt.json` `{n, agent, model, pgid,
started}` written via `on_start`; run; classify (a `Ran.timed_out` overrides to TIMEOUT); write `reply.md` in the
attempt dir from `Classified.reply`; quota → `Cooldowns.set(link, reset_at or now()+pool.quota_cooldown)`;
fallback rules of §5.2; review: base default = `repo.default_branch`, sends `brief.review_text`, `modified-tree`
check (§5.3); final `result.json` per §5.4 with `changed_files`/`diffstat` from `guards.changes(repo, start_head)`.
SIGTERM/SIGINT during an attempt: handler calls `process.terminate(pgid)`, writes status `cancelled`, exit 130.

CLI per spec §4: `run` (`--brief -` reads stdin; `--agent` single link; `--timeout` parse_duration; prints summary
lines `status: …`, `agent: …:…`, `changed: N files`, `job: <id>`, blank line, reply — or only JSON with `--json`;
returns `EXIT[status]`); usage/config/brief errors print `agent-hub: <message>` to stderr, exit 2.
`pool` prints per role `role:` then per link `  agent:model  installed|missing  [version]  [cooldown until …]`;
`--json` → `{"roles": {role: [{"link","installed","version","cooldown_until"}]}}`; `--clear L` prints `cleared L`
or `no cooldown for L`. `profile`, `jobs` (columns id, role, status, agent:model, duration, workdir), `show`,
`cancel` per §4.

- [ ] **Step 1: Write failing tests** (`test_run.py` calls `run_job` with a pool built in-test; `test_cli.py`
  calls `main([...])` and checks exit code + output; subprocess runs use `[sys.executable, "-m", "agenthub", ...]`, so
  create `agenthub/__main__.py` calling `cli.main`; to make an agent "not installed", build a PATH dir holding only
  the other fakes):
  `test_implement_ok_records_everything`, `test_implement_refused_on_default_branch` (exit 3, job recorded),
  `test_brief_missing_headings_exit_2` (cli), `test_fallback_on_quota_sets_cooldown`,
  `test_quota_reset_time_used_for_cooldown`, `test_fallback_on_auth_no_cooldown`, `test_fallback_on_model`,
  `test_skip_not_installed` (link to an agent whose binary is absent from PATH), `test_skip_cooldown`,
  `test_no_fallback_on_fail`, `test_timeout_status_and_no_fallback`, `test_empty_status`,
  `test_write_then_quota_is_interrupted`, `test_exhausted_when_all_quota`, `test_review_ok_default_base`,
  `test_review_modified_tree` (fake scenario `write` → exit 5), `test_review_brief_includes_preamble`
  (fake's recorded stdin/brief contains `git diff master...HEAD`), `test_secret_env_not_passed`
  (`GH_TOKEN` set in the test env → fake's recorded env lacks it; `request.json` lists `GH_TOKEN`),
  `test_cancel_running_job` (start `agent-hub run` as a subprocess with scenario `slow`, wait for `attempt.json`,
  `main(["cancel", id])` → process exits 130, result status `cancelled`, the fake's sleeping child is gone),
  `test_agent_override_single_link`, `test_unknown_agent_in_pool_exit_2`, `test_pool_output_and_clear`,
  `test_pool_json`, `test_profile_prints_and_unknown_exit_2`, `test_jobs_and_show`, `test_show_unknown_exit_2`,
  `test_run_json_output_is_result`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just lint && just test` → pass.
- [ ] **Step 5: Commit** `feat: run loop with role chains and quota fallback, and the CLI`.

---

### Task 6: orchestrate skill, README, smoke test

**Files:**
- Create: `skills/orchestrate/SKILL.md`, `skills/orchestrate/brief-template.md`, `README.md`, `scripts/smoke.sh`,
  `tests/test_skill.py`

SKILL.md per spec §9 (frontmatter `name: orchestrate`, `description:` "Use when delegating implementation work or a
second-opinion code review to other coding agents through agent-hub — writing the brief, running it on the agent
pool by role with quota fallback, and verifying the result yourself."). The nine steps of §9, the status table from
spec §6 with the action for each status, and the rule that agent claims are not evidence. `brief-template.md`:
sections `## Goal`, `## Input`, `## Expected output`, `## Constraints`, `## Gate` with one-line guidance each.
README per spec §11. `scripts/smoke.sh`: `set -euo pipefail`; temp repo on `feat/smoke`; for each of agy, codex,
claude that is installed: `agent-hub run --role implement --agent <agent>:<model from $SMOKE_<AGENT>_MODEL or the
first link for that agent in the implement chain>` with a brief asking to add `add(a, b)` in `calc.py` and a test,
then a review job; print `<agent> implement=<status> review=<status>`; never run from tests.

- [ ] **Step 1: Write failing tests** `test_skill.py`: `test_skill_frontmatter` (name `orchestrate`, description
  present and < 1024 chars), `test_skill_names_no_agent` (the words `agy`, `codex`, `gemini`, `opus`, `sonnet`,
  `gpt` do not appear, case-insensitive), `test_skill_covers_every_status` (each status from `orchestrate.EXIT` is
  mentioned), `test_template_has_required_headings` (`brief.validate("implement", template)` passes),
  `test_smoke_script_is_executable_and_shellchecked` (executable bit; `shellcheck` run if on PATH, else skip).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Write the files.
- [ ] **Step 4:** `just lint && just test` → pass.
- [ ] **Step 5: Commit** `feat: orchestrate skill, README and smoke test`.
