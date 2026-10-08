# agent-hub v0.3 — handoffs, gates, plan mode

Status: approved (autonomous mode). Decisions D-030..D-034. Builds on the v1 spec
(`2026-10-08-orchestrator-cli-design.md`); section numbers below are this document's.

## 1. Handoff (D-031)

### 1.1 Footer

`orchestrate.run_job` appends this footer to the text sent for every **implement** job (after the brief, and after any
upstream handoffs or retry notes):

```
---
When you are done, end with this handoff as your final message, exactly these headings:

## Status
success | partial | blocked

## What I did
- <one line per change>

## Verification
unit-test-verified | type-check-only | not-verified

## Notes
- <assumptions, surprises, anything the orchestrator must know; "(none)" if nothing>

## Follow-ups
- <tasks you suggest; "(none)" if nothing>
```

The footer is also written into the job's `brief.md` (what was sent is what is recorded).

### 1.2 Parsing — `agenthub/handoff.py`

`parse(reply: str) -> dict | None`: find the **last** `## Status` heading in the reply and parse from there.
Headings match case-insensitively, any level ≥ 2. Result:

```json
{"status": "success", "verification": "unit-test-verified",
 "did": ["..."], "notes": ["..."], "followups": ["..."]}
```

- `status` must be one of success/partial/blocked (first word of the section, lowercased, trailing punctuation
  stripped); otherwise the whole parse returns `None`.
- `verification` is the first word of its section if it is one of the three values, else `"not-verified"`.
- List sections: lines starting with `- ` or `* ` (marker stripped); a lone `(none)` → `[]`. Missing section → `[]`.

`result.json` gains `handoff` (the dict or `null`) and `handoff_missing` (bool). The job status is never changed by
the handoff. Review jobs: no footer, `handoff: null`, `handoff_missing: false`.

`run` (human output) prints, when a handoff exists, `handoff: <status> · <verification>` after the `changed:` line.

## 2. Gate (D-032)

- `run --gate "<command>"`, repeatable; `RunArgs.gates: list[str]` (default `[]`). Ignored for review jobs.
- After the attempt loop, only when the job's status is `ok`: for each command in order, run
  `["bash", "-c", cmd]` with cwd = workdir, env = `env.child_env(os.environ, ())[0]`, stdin `/dev/null`, timeout
  `GATE_TIMEOUT_S = 600`, stdout+stderr to `<job dir>/gate/<n>.log` (via `process.run` semantics: files, own
  process group). Stop at the first failing command.
- `result.json.gate`: `[{"cmd": ..., "rc": ..., "duration_s": ..., "tail": "<last 40 lines>", "log": "<path>"}]`
  (`[]` when no gates).
- Any non-zero rc (or timeout, rc = -signal) → status `gate-failed`, `EXIT["gate-failed"] = 9`.
- `run` human output prints `gate: passed (N)` or `gate: FAILED: <cmd> (rc X)` and the tail.

## 3. Plan mode (D-033, D-034)

### 3.1 Plan file

```json
{
  "goal": "context for every task",
  "repo": "/abs/path/to/repo",
  "base": "master",
  "slug": "wait-cmd",
  "max_parallel": 2,
  "tasks": [
    {"name": "core", "role": "implement", "brief": "briefs/core.md",
     "gate": ["just lint", "just test"], "paths_allowed": ["agenthub/**", "tests/**"],
     "depends_on": [], "agent": null, "max_attempts": 2, "timeout": "60m"},
    {"name": "core-review", "role": "review", "verifies": "core", "brief": null}
  ]
}
```

Validation (`agenthub/plan.py`, `load(path) -> Plan`, raises `PlanError` with every problem listed): required
`goal`, `repo` (existing git work tree), `base` (existing branch), `slug` (`[a-z0-9-]+`), `tasks` (non-empty);
task `name` unique `[a-z0-9-]+`; `role` implement|review; implement needs `brief` (path relative to the plan
file's directory, must exist); review needs `verifies` naming an implement task; `depends_on` names existing tasks;
no cycles (a review implicitly depends on what it verifies); `agent` null or `agent:model`; defaults
`max_parallel` 2, `gate` [], `paths_allowed` [] (= anything), `max_attempts` 2, `timeout` = role default.

### 3.2 State — `<plan dir>/state.json`

```json
{"slug": "...", "started": "...", "updated": "...",
 "tasks": {"core": {"status": "pending|running|done|failed|blocked|scope-violation|skipped",
                    "attempts": [{"job": "<job id>", "status": "<job status>", "exit_code": 0}],
                    "worktree": "/abs", "branch": "orch/<slug>/core", "start_ref": "<sha>",
                    "reason": "", "handoff": "handoffs/core.md"}}}
```

Written atomically after every change. `plan run` on an existing state resumes: `running` tasks whose job is no
longer running are re-read from their job's `result.json`; `done/failed/blocked/scope-violation/skipped` stay.

### 3.3 The loop — `agenthub/planrun.py`

`run_plan(plan, paths, spawn) -> int` where `spawn(argv: list[str], log: Path) -> Popen` is injectable (tests pass a
fake). Each round:

1. Mark `skipped` (reason `dependency <x> <status>`) every pending task with a dependency in a terminal non-`done`
   status.
2. Ready = pending tasks whose dependencies are all `done`. Start ready tasks while running < `max_parallel`.
3. Start a task:
   - implement: worktree `<repo>/.worktrees/<slug>-<name>`, branch `orch/<slug>/<name>`. If it exists (resume /
     retry) reuse it. Start ref: no implement dependency → `base`; one → that task's branch head; several → `base`,
     then `git merge --no-ff --no-edit <branch>` for each (with `-c core.hooksPath=/dev/null`); a conflict →
     `git merge --abort`, task `blocked` (reason `merge conflict with <branch>`).
   - review: no worktree of its own; runs in the verified task's worktree with `--base <its start_ref>`.
   - Brief text sent = plan `goal` as `## Goal (whole plan)` + the task brief + for each `done` dependency
     `## Upstream handoff: <name>` with its handoff file's text + retry notes (§3.4).
   - Spawn `[sys.executable, "-m", "agenthub", "run", "--role", R, "--brief", <file>, "-d", <dir>, "--json",
     ("--agent", A) if set, ("--timeout", T), ("--gate", g)…, ("--base", ref) for review]`; the composed brief is
     written to `<plan dir>/briefs-sent/<name>-<attempt>.md`; stdout to `<plan dir>/logs/<name>-<attempt>.json`.
4. Poll every `PLAN_POLL_S = 2.0` s. A finished child: read its JSON result (stdout file); record the attempt;
   write `handoffs/<name>.md` = the job's reply text (implement) or review findings (review), with a header line
   `<!-- task <name> attempt <n> job <id> status <status> -->`.
5. Outcome for implement: job `ok` → check `changed_files` against `paths_allowed` (`fnmatch` with `**` meaning any
   depth; empty list allows all) → `scope-violation` (reason lists the files) or `done`. Otherwise apply §3.4.
   Review: job `ok` → `done`; `modified-tree` → `failed`; others → §3.4.
6. Stop when nothing is pending or running. Exit 0 if every task is `done`, else 1. SIGTERM/SIGINT: send SIGTERM
   to every running child, mark them `failed` (reason `cancelled`), exit 130.

### 3.4 Retries (D-034)

Attempts so far `< max_attempts` → set back to `pending` with a retry note appended to the next brief:

| Job status | Retry? | Note |
|---|---|---|
| `gate-failed` | yes | `## Previous attempt failed the gate` + failing cmd + its tail |
| `timeout` | yes | `## Previous attempt timed out` — work in smaller steps and commit as you go |
| `failed`, `empty`, `interrupted` | yes (once, even if max_attempts is larger) | `## Previous attempt ended with <status>` + its handoff/notes if any |
| `exhausted`, `refused`, `modified-tree`, `cancelled` | no | — |

No retry left → `blocked` for `exhausted`, otherwise `failed`. The worktree is kept as is between attempts.

### 3.5 Commands

- `plan run <plan.json> [--max-parallel N]` — the loop above (foreground).
- `plan status <plan.json> [--json]` — one line per task: name, status, attempts, branch, last job status,
  handoff status/verification if parsed; exit 0.
- `plan wait <plan.json> [--timeout 9m]` — block until no task is `pending`/`running` (poll `state.json`), then
  print `plan status` and exit 0 if all `done` else 1; still running at timeout → `still running: <slug>`, 124.
  No `state.json` yet → wait up to 10 s for it, then exit 2.

## 4. Skill (`skills/orchestrate/SKILL.md`)

Add, without naming agents: when a goal has several tasks, write a plan file and use `plan run` (background) +
`plan wait`; always pass the gate (`--gate` / `gate:`) with the repository's lint and tests; read `handoff`
first, the full reply only when needed; check the diff for edits to the gate itself (tests, justfile, lint
config); merge verified `orch/<slug>/*` branches yourself. Status table gains `gate-failed` (9). Re-run
`scripts/skill-test/run.sh` afterwards (D-027).
