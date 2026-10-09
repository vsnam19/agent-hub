# gwen bake-off bench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> In this repository the plan is executed with `agent-hub plan run` (one implement job per task); each task's brief
> points here.

**Goal:** Build the bench that measures harness × model × task for gwen (spec §7) and the script that manages the
Mac host (spec §3).

**Architecture:** `bench/gwen/` holds a fixture Python package, six task directories (brief, setup patch, reference
solution, hidden tests), a runner that copies the fixture into a fresh git repo, runs a harness through a local
recording proxy pointed at the Mac, gates the result and appends one JSON line per run, and a report generator.
`scripts/gwen-host.sh` installs and controls the Rapid-MLX launchd agent on the Mac over SSH.

**Tech Stack:** Python ≥ 3.12 stdlib only (argparse, tomllib, json, subprocess, http.server, http.client, threading),
pytest, ruff; Bash + shellcheck.

**Spec:** `docs/superpowers/specs/2026-10-09-gwen-local-agent-design.md` (§3, §7). Decisions D-039..D-048.

## Global Constraints

- No runtime dependencies (D-015): stdlib only; tests use pytest; lint is `just lint` (ruff check + ruff format --check).
- Tests never contact the Mac, never run a real harness, never call `ssh` for real: fake executables on a test-only
  `PATH` (D-016), the autouse fixtures in `tests/conftest.py` stay in force.
- Tests never write into the repository or the current directory; everything goes under `tmp_path`.
- No test without an assertion; no `pass` bodies; no skipped tests.
- The API key is never printed, logged, or written into a results file; saved command lines replace it with `***`.
- Line length 120; ruff rules E, F, W, I, B.

## Review Focus

1. A harness that edits files outside `paths_allowed`, including deleting files → `scope_violation` lists them and
   `pass` is false — test in Task 2.
2. A harness that writes into `tests/hidden/` before the gate → hidden tests are copied over its files (agent cannot
   pre-empt them) — test in Task 2.
3. A harness that never exits → killed (whole process group) at `--timeout`, `timed_out: true`, gate still runs —
   test in Task 2.
4. Streaming (SSE) responses through the proxy arrive at the client incrementally and the usage in the final chunk is
   recorded — test in Task 2.
5. A task whose hidden tests already pass on the untouched start state (a useless task) → caught by the task-suite
   test in Task 1.

---

### Task 1: fixture package and six tasks

**Files:**
- Create: `bench/__init__.py` (empty), `bench/gwen/__init__.py` (empty)
- Create: `bench/gwen/fixture/` — a package `minilib` (`pyproject.toml`, `src/minilib/{__init__,paging,textstats,inventory,cli}.py`, `tests/{test_paging,test_textstats,test_inventory,test_cli}.py`, `data/items.csv`), about 300 lines in all
- Create: `bench/gwen/tasks/t1..t6/{task.toml,brief.md,setup.patch,solution.patch,hidden/test_*.py}`
- Create: `bench/gwen/tasks.py`
- Test: `tests/bench/__init__.py`, `tests/bench/test_gwen_tasks.py`

**Interfaces:**
- Produces: `bench.gwen.tasks.Task` and `load_tasks()` / `prepare(task, dest)` used by Task 2:

```python
@dataclass(frozen=True)
class Task:
    id: str                    # "t1"
    title: str
    dir: Path                  # bench/gwen/tasks/t1
    paths_allowed: list[str]   # fnmatch globs relative to the repo root, e.g. "src/minilib/paging.py", "tests/test_*.py"
    visible_tests: list[str]   # pytest paths relative to the repo root

    @property
    def brief(self) -> str: ...            # text of brief.md

FIXTURE_DIR: Path   # bench/gwen/fixture
TASKS_DIR: Path     # bench/gwen/tasks

def load_tasks() -> dict[str, Task]: ...   # keyed by id, sorted t1..t6
def prepare(task: Task, dest: Path) -> str: ...
    # copies FIXTURE_DIR to dest, `git init -b master`, commits "fixture", applies setup.patch with `git apply`
    # (an empty patch is allowed), commits "setup"; returns the setup commit sha. Git runs with
    # user.name/email set via -c, commit.gpgsign=false, GIT_TERMINAL_PROMPT=0, no hooks (-c core.hooksPath=/dev/null).
def install_hidden(task: Task, repo: Path) -> None: ...
    # deletes repo/tests/hidden if present, then copies task.dir/hidden/* to repo/tests/hidden/
```

`task.toml` format:

```toml
title = "Fix the off-by-one in paginate()"
paths_allowed = ["src/minilib/paging.py", "tests/test_paging.py"]
visible_tests = ["tests/test_paging.py"]
```

**The fixture** (`minilib`), no third-party imports, `pyproject.toml` with `[tool.pytest.ini_options] testpaths =
["tests"]`, `pythonpath = ["src"]`, and `[tool.ruff]` line-length 100, select E,F,W,I. Modules:
- `paging.py`: `paginate(items: list, page: int, size: int) -> list` (1-based page), `page_count(n: int, size: int) -> int`.
- `textstats.py`: `word_count(text) -> int`, `char_freq(text) -> dict[str, int]`.
- `inventory.py`: `@dataclass Item(name: str, qty: int, price: float)`, `load_items(path) -> list[Item]` (CSV with
  header `name,qty,price`), `total_value(items) -> float`, `low_stock(items, threshold) -> list[Item]`.
- `cli.py`: `main(argv: list[str] | None = None) -> int`, argparse subcommands `stats FILE` (prints `words: N` and
  `chars: N`) and `inventory FILE [--min-qty N]` (prints `name qty` per line of items with qty < N; default 5).
- Tests for each module (all passing on the fixture as shipped).

**The six tasks** (the brief is written for the agent, in agent-hub's brief shape: `# Brief`, `## Goal`, `## Input`,
`## Expected output`, `## Constraints`; it must not mention hidden tests):

| id | setup.patch makes | brief asks | hidden tests check |
|---|---|---|---|
| t1 | `paginate` off by one (page 1 skips the first `size` items); `tests/test_paging.py` has a test that now fails | fix the bug so the visible tests pass | first, middle, last, out-of-range pages; `page_count` edges (0 items, exact multiple) |
| t2 | nothing (empty patch) | add `top_words(text: str, k: int) -> list[tuple[str, int]]` to `textstats.py`: lower-case, words = `[a-z0-9']+`, sorted by count desc then word asc, `k <= 0` → `[]`; write tests in `tests/test_textstats.py` | ties, punctuation, case, k larger than vocabulary, k = 0 |
| t3 | nothing | rename `load_items` to `read_items` everywhere (module, cli, tests); behaviour unchanged; no alias left | `read_items` works; `inventory` has no attribute `load_items`; the cli still works |
| t4 | nothing | add `--json` to `stats`: prints `{"words": N, "chars": N}` (json.dumps, sort_keys); without the flag output is unchanged; add a test | JSON output parses and matches; plain output unchanged |
| t5 | `data/items.csv` gains a row whose quoted name contains a comma (`"Bolt, large",12,0.30`); `load_items` is changed from `csv.DictReader` to a manual `line.split(",")` parser, so `int(...)` raises `ValueError` on that row | brief contains **only** the traceback of `python -m minilib.cli inventory data/items.csv` (run with `PYTHONPATH=src`) and "make this command work" | the command's output on `data/items.csv`; quoted names with commas; plain rows |
| t6 | nothing | add `src/minilib/report.py` with `summarize(items) -> dict` (`count`, `total_qty`, `total_value` rounded to 2, `low_stock` = names with qty < 5 sorted), a `report FILE` subcommand printing the dict as sorted JSON, and tests in `tests/test_report.py` | `summarize` on a fixed list; empty list; cli `report` output |

`paths_allowed` per task: only the files the task needs (t2: `src/minilib/textstats.py`, `tests/test_textstats.py`;
t6: `src/minilib/report.py`, `src/minilib/cli.py`, `tests/test_report.py`, `tests/test_cli.py`; etc.).
`solution.patch` is a reference solution applied on top of the setup commit.

- [ ] **Step 1: Write the failing task-suite test** — `tests/bench/test_gwen_tasks.py`:

```python
import subprocess
import sys

import pytest

from bench.gwen.tasks import FIXTURE_DIR, install_hidden, load_tasks, prepare

TASK_IDS = ["t1", "t2", "t3", "t4", "t5", "t6"]


def _pytest(repo, *paths):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths],
                          cwd=repo, capture_output=True, text=True)


def _ruff(repo):
    return subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", "."], cwd=repo,
                          capture_output=True, text=True)


def _apply(repo, patch):
    subprocess.run(["git", "apply", str(patch)], cwd=repo, check=True, capture_output=True)


def test_load_tasks_has_six():
    assert sorted(load_tasks()) == TASK_IDS


def test_fixture_tests_and_lint_pass(tmp_path):
    repo = tmp_path / "fx"
    prepare(load_tasks()["t2"], repo)  # t2 has an empty setup patch
    assert _pytest(repo).returncode == 0
    assert _ruff(repo).returncode == 0


@pytest.mark.parametrize("tid", TASK_IDS)
def test_hidden_tests_fail_before_solution(tmp_path, tid):
    task = load_tasks()[tid]
    repo = tmp_path / tid
    prepare(task, repo)
    install_hidden(task, repo)
    assert _pytest(repo, "tests/hidden").returncode != 0


@pytest.mark.parametrize("tid", TASK_IDS)
def test_solution_passes_everything(tmp_path, tid):
    task = load_tasks()[tid]
    repo = tmp_path / tid
    prepare(task, repo)
    _apply(repo, task.dir / "solution.patch")
    install_hidden(task, repo)
    assert _pytest(repo, *task.visible_tests).returncode == 0
    assert _pytest(repo, "tests/hidden").returncode == 0
    assert _ruff(repo).returncode == 0


@pytest.mark.parametrize("tid", TASK_IDS)
def test_solution_stays_in_scope(tmp_path, tid):
    import fnmatch
    task = load_tasks()[tid]
    repo = tmp_path / tid
    prepare(task, repo)
    _apply(repo, task.dir / "solution.patch")
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    changed = [line[3:] for line in out.splitlines()]
    assert changed
    assert all(any(fnmatch.fnmatch(p, g) for g in task.paths_allowed) for p in changed), changed


@pytest.mark.parametrize("tid", TASK_IDS)
def test_brief_has_required_sections_and_hides_hidden_tests(tid):
    brief = load_tasks()[tid].brief
    assert "## Goal" in brief and "## Input" in brief and "## Expected output" in brief
    assert "hidden" not in brief.lower()


def test_prepare_does_not_touch_fixture(tmp_path):
    before = sorted(p.relative_to(FIXTURE_DIR) for p in FIXTURE_DIR.rglob("*"))
    prepare(load_tasks()["t1"], tmp_path / "x")
    assert sorted(p.relative_to(FIXTURE_DIR) for p in FIXTURE_DIR.rglob("*")) == before
    assert not (FIXTURE_DIR / ".git").exists()
```

- [ ] **Step 2: Run it, confirm it fails** — `uv run pytest tests/bench/test_gwen_tasks.py -q` → ImportError.
- [ ] **Step 3: Write the fixture, `tasks.py`, then t1..t6** (setup patch, brief, hidden tests, solution patch). Make
      patches with `git diff` in a scratch copy under `/tmp`, never inside this repository.
- [ ] **Step 4: Run** `uv run pytest tests/bench -q` → all pass; `just lint` → clean. The fixture has its own ruff
      config; make sure the repository's `just lint` also passes on `bench/` (exclude `bench/gwen/fixture` and
      `bench/gwen/tasks/*/hidden` from the repository's ruff only if they conflict with its rules — prefer writing
      them to pass).
- [ ] **Step 5: Commit** `feat(bench): gwen fixture and six tasks with hidden tests`.

---

### Task 2: runner, recording proxy, memory sampler, report

**Files:**
- Create: `bench/gwen/proxy.py`, `bench/gwen/memsample.py`, `bench/gwen/run.py`, `bench/gwen/report.py`,
  `bench/gwen/harnesses.toml`, `bench/gwen/results/.gitkeep`, `bench/gwen/README.md`
- Create: `tests/bench/fakes/fake-harness` (Python, executable), `tests/bench/fakes/ssh` (Python, executable),
  `tests/bench/fakeserver.py`
- Test: `tests/bench/test_gwen_proxy.py`, `tests/bench/test_gwen_memsample.py`, `tests/bench/test_gwen_run.py`,
  `tests/bench/test_gwen_report.py`

**Interfaces:**
- Consumes: `bench.gwen.tasks.{Task, load_tasks, prepare, install_hidden}` from Task 1.
- Produces:

```python
# proxy.py
class RecordingProxy:
    def __init__(self, upstream: str, log_path: Path): ...   # upstream like "http://100.86.30.90:8642"
    def start(self) -> str: ...      # binds 127.0.0.1:0 in a daemon thread, returns "http://127.0.0.1:<port>"
    def stop(self) -> None: ...
    def summary(self) -> dict: ...   # {"requests": int, "errors": int, "prompt_tokens": int,
                                     #  "completion_tokens": int, "cached_tokens": int, "max_prompt_tokens": int}

# memsample.py
class MemSampler:
    def __init__(self, ssh_target: str | None, interval: float = 10.0): ...   # None → disabled
    def start(self) -> None: ...
    def stop(self) -> dict | None: ...   # {"min_free_pct": int, "max_swap_mb": float, "samples": int} or None
def parse_sample(text: str) -> tuple[int | None, float | None]: ...
    # parses `memory_pressure -Q` ("System-wide memory free percentage: 41%") and
    # `sysctl vm.swapusage` ("vm.swapusage: total = 2048.00M  used = 1093.25M  free = 954.75M  (encrypted)")

# run.py
def run_one(harness: str, model: str, task: Task, *, base_url: str, api_key: str, out_root: Path,
            timeout: float, ssh_target: str | None, harnesses_file: Path) -> dict: ...   # returns the result record
def main(argv: list[str] | None = None) -> int: ...
```

**Proxy behaviour.** `ThreadingHTTPServer` on 127.0.0.1, any method and path forwarded to `upstream + path` with
`http.client`, request headers copied except `Host`/`Content-Length` (recomputed); the response status and headers
are copied except `Content-Length`/`Transfer-Encoding`/`Connection`; the body is streamed to the client in chunks
as it arrives (`read1`/`readline`), the reply sent with `Connection: close` and no length. Each exchange appends one
JSON line to `log_path`: `{"ts", "method", "path", "status", "request_bytes", "usage", "error"}` where `usage` comes
from a JSON body's `usage`, or from the last SSE `data:` line that contains `"usage"` (OpenAI chat
`prompt_tokens`/`completion_tokens`/`prompt_tokens_details.cached_tokens`; Responses `input_tokens`/`output_tokens`/
`input_tokens_details.cached_tokens`; Anthropic `input_tokens`/`output_tokens`/`cache_read_input_tokens`, which
also appear in `message_start`/`message_delta` events — sum them). Request bodies are not logged (they carry code);
headers are never logged. Upstream connection failure → 502 to the client and `"error"` recorded.

**harnesses.toml** — one table per harness; placeholders `{model}`, `{base_url}` (the proxy URL, no `/v1`),
`{api_key}`, `{brief_path}`, `{workdir}`, `{home}`; `prompt` is sent either via `{brief_text}` in argv or on stdin
when `stdin = true`. Initial content (command lines are confirmed by the orchestrator in stage A; tests use only the
fake harness):

```toml
[qwen-code]
argv = ["qwen", "--yolo", "-m", "{model}", "-p", "{brief_text}"]
env = { OPENAI_BASE_URL = "{base_url}/v1", OPENAI_API_KEY = "{api_key}", OPENAI_MODEL = "{model}" }

[opencode]
argv = ["opencode", "run", "--model", "gwen/{model}", "{brief_text}"]
env = { OPENCODE_CONFIG = "{home}/opencode.json" }
files = { "opencode.json" = '''{"provider": {"gwen": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "{base_url}/v1", "apiKey": "{api_key}"}, "models": {"{model}": {}}}}}''' }

[aider]
argv = ["aider", "--model", "openai/{model}", "--message-file", "{brief_path}", "--yes-always", "--no-auto-commits",
        "--no-gitignore", "--no-show-model-warnings", "--no-check-update", "--analytics-disable"]
env = { OPENAI_API_BASE = "{base_url}/v1", OPENAI_API_KEY = "{api_key}" }

[mini-swe-agent]
argv = ["mini", "--model", "openai/{model}", "--task", "{brief_text}", "--yolo", "--exit-immediately"]
env = { OPENAI_API_BASE = "{base_url}/v1", OPENAI_API_KEY = "{api_key}", MSWEA_CONFIGURED = "true" }

[codex]
argv = ["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write", "-m", "{model}",
        "-c", "model_provider=\"gwen\"",
        "-c", "model_providers.gwen={name=\"gwen\", base_url=\"{base_url}/v1\", env_key=\"GWEN_API_KEY\", wire_api=\"responses\"}"]
stdin = true
env = { GWEN_API_KEY = "{api_key}", CODEX_HOME = "{home}/.codex" }

[claude]
argv = ["claude", "-p", "--model", "{model}", "--permission-mode", "bypassPermissions", "--no-session-persistence"]
stdin = true
env = { ANTHROPIC_BASE_URL = "{base_url}", ANTHROPIC_AUTH_TOKEN = "{api_key}", CLAUDE_CONFIG_DIR = "{home}/.claude" }
```

`files` are written (placeholders filled) under `{home}` before the run. Placeholders are substituted with
`str.replace`, not `str.format` (the JSON braces must survive).

**Runner behaviour** (`run_one`):
1. `run_dir = out_root / f"{ts}-{harness}-{model}-{task.id}"` with `home/`, `repo/`; `prepare(task, repo)`.
2. Start `RecordingProxy(base_url, run_dir/"proxy.jsonl")` and `MemSampler(ssh_target)`.
3. Write the brief to `run_dir/brief.md`. Run the harness with `cwd=repo`, `start_new_session=True`,
   stdout/stderr to `run_dir/stdout`/`stderr`, env = the current env minus variables whose names contain `TOKEN`,
   `SECRET`, `API_KEY`, `PASSWORD` (then the table's `env` added), plus `HOME={home}`,
   `XDG_CONFIG_HOME={home}/.config`, `XDG_DATA_HOME={home}/.local/share`, `XDG_STATE_HOME={home}/.local/state`,
   `XDG_CACHE_HOME={home}/.cache`. On timeout kill the process group (SIGTERM, 5 s, SIGKILL).
4. Stop the proxy and the sampler. Changed files = `git status --porcelain --untracked-files=all` against the setup
   commit plus `git diff --name-only <setup>` (agent commits count too); `scope_violation` = changed paths matching
   no `paths_allowed` glob.
5. `install_hidden`, then gate in `repo` with `sys.executable`: `-m ruff check --no-cache .`, `-m pytest -q -p
   no:cacheprovider <visible_tests>`, `-m pytest -q -p no:cacheprovider tests/hidden`; 300 s each; rc and the last
   20 output lines kept.
6. Record (also written to `run_dir/result.json`): `{"ts", "harness", "model", "task", "pass", "rc", "timed_out",
   "wall_s", "gate": {"ruff": rc, "visible": rc, "hidden": rc}, "scope_violation": [...], "changed": [...],
   "usage": proxy.summary(), "mem": sampler result, "run_dir", "argv"}` with the API key replaced by `***` in
   `argv`. `pass` = all three gate rc 0 and no scope violation.

`main`: `--harness` (repeatable or `all`), `--model` (repeatable), `--task` (repeatable or `all`), `--repeat N`
(default 1), `--base-url` (default `$GWEN_BASE_URL` or `http://100.86.30.90:8642`), `--api-key-file` (default
`~/.config/gwen/api-key`), `--ssh` (default `sammy@sammys-macbook-air`; `--no-mem` disables), `--timeout` seconds
(default 1200), `--results` (default `bench/gwen/results/<YYYY-MM-DD>.jsonl`), `--out-root` (default
`$XDG_STATE_HOME/agent-hub/bench` or `~/.local/state/agent-hub/bench`). Runs the cross product sequentially,
appends each record to the results file, prints one line per run (`t1 qwen-code qwen3.5-9b-4bit PASS 312s`).
Exit 0 when every run completed (pass or fail), 2 on bad arguments or a missing harness table.

**report.py**: `python -m bench.gwen.report <results.jsonl>... [-o out.md]` → markdown: one table per model, rows =
harness, columns = t1..t6 (`2/2`, `1/2`, `–`), then pass rate, median wall time, mean prompt/completion tokens, max
swap MB, min free %. `def render(records: list[dict]) -> str`.

- [ ] **Step 1: Write the failing tests.** Required tests (names exact):
  - `test_gwen_proxy.py`: `test_forwards_json_and_records_usage` (fake upstream returns OpenAI JSON with usage →
    client gets identical body, log has usage, `summary()` sums), `test_streams_sse_incrementally` (fake upstream
    writes 3 SSE events 0.3 s apart; client receives the first event before the upstream finishes — assert on
    timestamps; last event's usage recorded), `test_anthropic_usage_summed`, `test_upstream_down_gives_502`,
    `test_log_has_no_headers_or_bodies` (the Authorization value and a request-body marker string never appear in
    the log file).
  - `test_gwen_memsample.py`: `test_parse_sample`, `test_parse_garbage_returns_none`, `test_sampler_with_fake_ssh`
    (fake `ssh` on PATH prints canned output; `interval=0.05`; `stop()` gives `samples >= 2`, the min/max),
    `test_disabled_sampler_returns_none`.
  - `test_gwen_run.py` (fake server from `tests/bench/fakeserver.py`, fake harness from `tests/bench/fakes/` via a
    test `harnesses.toml` in `tmp_path`; the fake harness reads `FAKE_HARNESS_MODE`):
    `test_solution_mode_passes` (mode `solve`: applies the task's `solution.patch` and makes one request through
    `OPENAI_BASE_URL` → `pass` true, usage recorded), `test_noop_mode_fails_hidden` (t1, does nothing → `pass`
    false, `gate.visible != 0`), `test_scope_violation` (mode `outside`: solves and also edits `README.md` and
    deletes `data/items.csv` → both listed, `pass` false), `test_hidden_tests_cannot_be_preempted` (mode
    `prehide`: writes a passing `tests/hidden/test_x.py` and does not solve → `gate.hidden != 0`),
    `test_timeout_kills_group` (mode `hang` spawns a child that sleeps; `timeout=1` → `timed_out` true, the child
    pid is gone, gate still recorded), `test_isolated_home_and_secret_env` (harness dumps env: `HOME` is under the
    run dir, `GH_TOKEN` absent, `OPENAI_API_KEY` present), `test_api_key_redacted` (the key is in no file under the
    run dir and not in the results file; `argv` shows `***`), `test_main_cross_product_appends_jsonl`
    (`--harness fake --task t1 --task t2 --repeat 2` → 4 lines), `test_unknown_harness_exit_2`.
  - `test_gwen_report.py`: `test_render_counts_and_rates` (fixed records → exact cells `2/2`, `0/1`, pass-rate
    line), `test_render_handles_missing_usage_and_mem`.
- [ ] **Step 2: Run them, confirm they fail.** `uv run pytest tests/bench -q`.
- [ ] **Step 3: Implement** `proxy.py`, `memsample.py`, `run.py`, `report.py`, `harnesses.toml`, the fakes and the
      fake server (stdlib `ThreadingHTTPServer`; modes: OpenAI JSON, OpenAI SSE with delays, Anthropic SSE).
- [ ] **Step 4: Run** `uv run pytest -q` (whole suite) and `just lint` → all green.
- [ ] **Step 5: Write `bench/gwen/README.md`**: what the bench measures, how to run stage A/B (commands from spec §7.3),
      where results and run dirs go, how to add a harness or a task.
- [ ] **Step 6: Commit** `feat(bench): gwen runner, recording proxy, memory sampler and report`.

---

### Task 3: Mac host control script

**Files:**
- Create: `scripts/gwen-host.sh`, `scripts/gwen-host/gwen-serve`, `scripts/gwen-host/dev.vsn.gwen.plist`
- Test: `tests/test_gwen_host.py`, `tests/fakes/ssh-recorder` (Python, executable; not named `ssh` in that dir —
  the test links it as `ssh` into a tmp PATH dir)

**Interfaces:** none consumed; the script is used by the orchestrator and documented in `bench/gwen/README.md`.

`scripts/gwen-host/gwen-serve` and `scripts/gwen-host/dev.vsn.gwen.plist` are exactly the files already installed
on the Mac on 2026-10-09 (the orchestrator pastes them into the brief). `gwen-host.sh` (Bash, `set -euo
pipefail`, shellcheck-clean), target `${GWEN_SSH:-sammy@sammys-macbook-air}`, key file
`${GWEN_KEY_FILE:-$HOME/.config/gwen/api-key}`:

| Command | Does over SSH |
|---|---|
| `install` | `uv tool install rapid-mlx@latest`; creates `~/.config/gwen` (700), `~/Library/Logs/gwen`; copies the key (stdin, never argv) to `~/.config/gwen/api-key` (600); copies `gwen-serve` to `~/.local/bin/` (755) and the plist to `~/Library/LaunchAgents/`; writes `qwen3.5-4b-4bit` to `~/.config/gwen/model` if absent; `launchctl bootout` (ignore failure) then `bootstrap gui/$(id -u)` |
| `switch <model>` | validates `<model>` against `^[A-Za-z0-9._/-]+$`; writes it to `~/.config/gwen/model`; `launchctl kickstart -k gui/$(id -u)/dev.vsn.gwen` |
| `status` | prints the model file, `launchctl print gui/$(id -u)/dev.vsn.gwen` state line, the last 5 log lines; then, locally, `curl -sS -m 5 -H "Authorization: Bearer <key>" http://<host>:8642/v1/models` (key read from the file, passed via a curl config on stdin so it is not in argv) |
| `stop` | `launchctl bootout gui/$(id -u)/dev.vsn.gwen` |
| `logs [n]` | `tail -n ${n:-50} ~/Library/Logs/gwen/serve.log` |

The host for curl is `${GWEN_HTTP_HOST:-100.86.30.90}`.

- [ ] **Step 1: Write failing tests** (`tests/test_gwen_host.py`, fake ssh records argv and stdin to `FAKE_LOG_DIR`;
      fake `curl` likewise): `test_switch_writes_model_and_kickstarts`, `test_switch_rejects_bad_model` (`"x; rm -rf ~"`
      → exit 2, no ssh call), `test_install_sends_key_on_stdin_not_argv` (the key value appears in no recorded argv),
      `test_status_curl_key_not_in_argv`, `test_unknown_command_usage_exit_2`, `test_shellcheck_clean` (runs
      `shellcheck scripts/gwen-host.sh scripts/gwen-host/gwen-serve`; if `shellcheck` is not on PATH the test fails with
      a clear message, it does not skip).
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` and `just lint` → green.
- [ ] **Step 5: Commit** `feat(scripts): gwen-host.sh controls the Rapid-MLX agent on the Mac`.
