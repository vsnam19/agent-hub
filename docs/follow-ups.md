# Follow-ups

Open items found by reviews and runs, not yet scheduled. Audited against master on 2026-10-18: struck-through items are fixed. Small, well-specified ones suit `implement-small` (gwen) on a
small repository, or `implement` here (D-079).

| Found | Item | Source |
|---|---|---|
| 2026-10-12 | C: C++ fixture for the gwen bench (B done in v0.9.0, spec `2026-10-12-phase-b-skill-hub-design.md`) | D-077 |
| 2026-10-12 | Owner's call: agy has superpowers imported (2026-09-26): a headless `agy -p` run lists all 15 superpowers skills in its index (e.g. brainstorming: "You MUST use this before any creative work"), verified 2026-10-12; whether its SessionStart hook also runs headless is unverified; the `worker` plugin is now installed in agy (2026-10-12), so agy workers see both `test-first`/`debug-playbook`/`verify-before-done` and superpowers' TDD/debugging/verification skills — consider `agy plugin disable superpowers` | D-087 |
| 2026-10-12 | Repeat the skill checks that could not run: a live `codex exec` with an installed plugin (quota back 2026-11-07) and a gwen run with `skills.paths` (Mac host was down) | D-083 |
| 2026-10-12 | Owner's call: hand-copied `~/.codex/skills/telaf-api` and `telaf-legato` duplicate the installed `telaf` plugin; `agent-hub hub status` reports them as `stray` and never deletes them | D-089 |
| 2026-10-12 | Phase C: a live `--suite cpp` Stage A run (opencode × qwen3.5-9b-4bit) when the Mac host answers, then decide whether gwen takes C++ tasks (D-077, D-090) | D-090 |
| 2026-10-12 | Phase E E5 must also: sort tasks topologically before writing, treat `"reason": null` as empty, and reject a task whose brief fails `tasksize.check` headings (from review ff04) | review ff04 |
| 2026-10-13 | C++ bench: c4 (1782 s) and c6 (3519 s) report wall times above the 1200 s timeout with `timed_out: false`; the log has a 41-min gap (host likely suspended). Check `run_one`'s wall/timeout accounting across a suspend (monotonic clock), then repeat Stage A with `--repeat 2` | D-095 |
| 2026-10-13 | A network outage (`API Error: Can't reach the API server … EAI_AGAIN`) is classified `failed` (jobs 20261012-181839-implement-1cfb, 20261012-182120-review-b33c); it should be a transient outcome that falls through to the next link (or retries) like quota, never consume a review round. Small task: claude adapter classify + test | jobs 1cfb, b33c |
| 2026-10-13 | agy 1.3.4 no longer offers `gemini-3.1-pro-high` (`agy models`: gemini-3.8-flash-high/medium/low, claude-sonnet-4-6, claude-opus-4-6-thinking, gpt-oss-120b-medium); attempts fail as `model-unavailable`. The user pool override now uses `agy:gemini-3.8-flash-high`; `agenthub/default_pool.toml` and agy `profile.md` still name the old id — small task to update them | job 20261013-142316-implement-94bc |
| 2026-10-13 | First real `skills weekly` (report `~/.local/state/agent-hub/skills/2026-10-13/report.md`, branch `learned/2026-10-13` kept in agent-plugins): stopped correctly at eval (gate-before-review: with 1.00 = without 1.00). Small tasks: (1) report lists bumped versions under "Released" although nothing was released → "bumped (not released)"; (2) 189 unparsed findings — parse `- **medium**: \`file:line\` …` and similar formats; (3) drop the 1× categories from metrics, show groups; (4) drafting brief: each skill's positive eval case must be one the no-plugin baseline fails (state it in the case), and eval every drafted skill before deciding; (5) MCP proposal `ast-guard` for the owner | weekly 2026-10-13 |
| 2026-10-13 | `agent-hub run --role review -d <wt>` does not skip links that implemented the work tree (only plan mode passes `--exclude`); `ready` catches it afterwards ("reviewer is the implementer"), wasting a review. Small task: a review job excludes every implementer of the work tree by default (`agent:model`, effort ignored) | plan merge on plan-close-fix2 |
| 2026-10-14 | Phase F lows (review on `orch/phasef-fix10/gate-pid`, approve): (1) `run_gate_job`/`run_job` announce before `with _Cancel()` installs SIGTERM handlers — a cancel in that gap kills without `_finish`; (2) `_finish` read-check-write of `result.json` is not atomic against a fallback cancel write; (3) `tests/test_gate_cmd.py` cancel test sends SIGTERM to the pytest process itself and reads capsys from a thread — flaky risk, rewrite with a child process. Small tasks for the techlead loop (E8) | Phase F review |
| 2026-10-14 | Phase F F4/F5 lows (review on `orch/phasef2-fix3/mcp-manifest`, approve): ~~`plan_review` parent "running" write races the child's `--result` write; `plan_review` child not reaped; `plan_wait` does not detect a dead runner~~ (fixed: plan `deadlock`, E8 `_spawn_result_cli` writes the info file with `open(..., "x")`); still open: `plan_merge` with no done tasks returns the same shape as "nothing left" while the CLI exits 1. Small tasks for E8 | Phase F review |
- **MCP serverInfo version is empty** (2026-10-14): `initialize` returns `{"name": "agent-hub", "version": ""}`; pass the package version to `MCPServer`. Low.
- **tasksize counts dotted module names as files** (2026-10-14): a brief line "Stdlib plus `agenthub.triage` and `agenthub.planstate`" under Expected output counted 4 files for a 2-file task (E8 draft, `triage-apply`). Count only backticked spans with a `/` or a file extension. Low.
- **gateguard false positives** (2026-10-14, E8 review): (1) `skip-added` fires on the formatter comment `# fmt: skip`
  (`gateguard.py:442` regex `\bskip\b`) — ignore `# fmt: skip`/`# fmt: off` comments; (2) `assert-removed` fired on
  `test_server_lists_tools` in `tests/test_mcpserver.py` when the task only added two names to a `set(...) == {…}`
  literal — `_weakened` should not treat a grown expected-literal as weakening. Low; both were read and judged benign.
- ~~**`plan_review` unreaped child hangs the director for hours — HIGH**~~ **Fixed 2026-10-14 (plan `deadlock`).** (2026-10-14): the MCP server starts
  `agenthub plan review` without a wait thread, so the finished runner stays a zombie until the server exits. A director
  waiting with `while kill -0 <pid>` (zombies answer `kill -0`) never wakes. Seen at least 3 times: E8 review finished
  13:20, session idle until /exit at 21:12 (session 4040f7e7). Fix: reap the child (wait thread as in `plan_run`) and
  write the final `--result` from the child only; directors wait on the result file's `status`, not on the pid.

## Director deadlock avoidance (2026-10-14)

A director that ends its turn while waiting on something that never signals is idle until the user notices (lost ~8 h,
three times, at E8 `plan review`). Rule: **every wait must end on success, failure, and a dead/zombie/stalled
process**, and the director keeps a harness-tracked waiter (a `run_in_background` command that exits) before it ends
a turn. Items:

- **Fixed 2026-10-14 (plan `deadlock`, merged):** (1) `plan_review` reaps its child and marks a runner that died without a result
  `failed`; `plan_review_wait` treats zombies as dead (`procinfo.is_running`). (2) `plan_wait` returns `stalled` when
  the recorded runner (`runner.pid` + start time) is gone while tasks are still `pending`/`running`.
- **`plan review` ignores SIGTERM/SIGINT:** the per-job signal handler (`orchestrate.py` ~231) stops the current
  review, and the review loop moves on to the next task; `agent-hub cancel` and `kill -INT` did not stop it, only
  SIGKILL did. The review loop should stop after a signal. Medium.
- **Wait-tool timeout vs the Claude Code MCP client:** `plan_review_wait`/`plan_wait` accept up to 300 s, but Claude
  Code moves an MCP call to the background after ~120 s and its later notification still says `running`. Cap the
  default below 120 s and document "call again" in the tool description. Low.
- **`wait_job` / `agent-hub wait` never detect a dead job (verified):** both loop on `result.json` `status ==
  "running"` with no liveness check (`mcpserver.py` `wait_job`, `cli.py` `_cmd_wait`); a SIGKILLed job waits forever.
  Fixed 2026-10-14 (`fix/wait-job-dead`). Review lows: `agent-hub wait --json` prints the dead-job reason as plain
  text (like the existing timeout line); the dead-job result is built in three copies (sync/async MCP, CLI) — share
  one helper.
- **Orchestrate skill:** state the rule above in the Non-negotiables ("never wait on `kill -0 <pid>`; wait on the
  result file or the wait tool, and cover dead processes"). The skill lives in `agent-plugins`.
- **Resume notes:** a resume note must name the last review/plan result file, so a new session reads finished
  results instead of re-running (E8 review was re-run although its result had been on disk since 13:20).
- ~~**agy OAuth expiry is classified `failed`, not auth**~~ **Fixed 2026-10-14 (`fix/agy-ssh-auth`, `agents/agy` maps the markers to AUTH).** (2026-10-14): with an expired agy login every run prints an
  OAuth URL, `Waiting for authentication (timeout 60s)...`, `Error: authentication timed out.` and is recorded as
  `failed` — so plan tasks retry twice and escalate, and reviews end `failed` (3 E8 reviews, every `implement`
  attempt of plan `deadlock`). Map "authentication failed or timed out" to the auth outcome (cooldown the agy links,
  fall through the chain, no escalation). **Root cause (verified):** under SSH (`SSH_CONNECTION`/`SSH_TTY` set) agy
  uses empty file-based token storage instead of the keyring; blanking those vars makes `agy -p` work. Fix task
  `agy-ssh-auth` (adapter blanks them + auth markers). No re-login needed.
- ~~**Race in stalled detection**~~ **Fixed (plan `deadlock`, `plan-run-claims`; E8: `plan_run` refuses while a verified runner lives).** MCP `plan_run` returned before the new runner
  wrote `runner.pid`, so `plan_wait` on a resumed plan saw the previous runner's dead pid → false `stalled`. Fix
  task `plan-run-claims` writes `runner.pid` in `plan_run` right after spawn.
- **agy keyring override lows (review `20261014-154835-review-85cf`, approve):** SSH vars are blanked, not removed
  (`Invocation.env` cannot unset; works with agy 1.3.5's truthiness check — breaks if agy switches to a presence
  check); `_secret_service_running` is cached per process (a keyring started later is missed until restart); the
  existing `run_fake` agy tests call the real `dbus-send` when the test run itself is under SSH — patch the probe in
  an autouse fixture. Low.
- **Per-branch fixes on stacked plan branches need a cascade** (2026-10-18, E8): fixing review findings on the branch
  that introduced the code leaves descendants with the old code; their reviews (base `master` for multi-dependency
  tasks) re-raise the ancestors' findings, and `plan merge` hits conflicts (E8: `agenthub/triage.py` touched by 4
  stacked branches, `tests/test_mcpserver.py` changed on master meanwhile). The director merged each fixed branch down
  the DAG with a per-definition 3-way merge script, then re-reviewed every changed head (≈60 review jobs). `plan close`
  should do this itself: after a fix round, merge the task's branch into its `done` dependents and re-review them
  (today it only flags them, `CloseResult.stale`). Medium.
- **`plan triage`/`plan close` count merged tasks as skipped** (2026-10-18): after `plan merge` removes the work trees,
  `agent-hub plan triage <e8 plan>` prints `skipped 16 task(s)` and exits 1. Tasks whose branch is already merged into
  the base (or deleted) should be left out silently. Low.
- **`plan review` lets the fix author approve** (2026-10-18): `review_task` excludes only the task's first implementer;
  after a fix round by another model (agy Gemini here) the review chain picked that same model, and `ready` refused
  (`reviewer is the implementer`). Exclude every model that implemented a commit on the branch. Medium.
- **`test_child_does_not_import_cwd_agenthub` flakes under parallel gates** (2026-10-15): with 4–6 `uv run pytest`
  gates at once it failed once with `git add` → `index.lock: File exists` in its own temp repo (job
  20261014-200442-implement-5046); passes alone and on rerun. Low.
- **Gemini handoff claims** (2026-10-18): job 20261014-232223-implement-c3e1 reported "backwards-compatible aliases"
  for renamed tests that did not exist. Handoffs remain unverified claims; the director's diff read caught it. Info.
