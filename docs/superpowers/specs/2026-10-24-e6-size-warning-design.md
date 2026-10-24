# E6 — size warning in `run` / `plan run`, docs for `plan draft`

Status: design, 2026-10-24. Phase E (D-094), after E5 (`agent-hub plan draft`, merged 2026-10-24).

## Why

`tasksize.check(text)` tells whether a brief is small enough for one cheap run (≤ 3 files changed, ≤ 4 numbered
deliverables, ≤ 6 named tests, the five headings). `plan draft` uses it to make the techlead re-split. A brief the
director writes by hand never meets the check, so a large brief goes to haiku unnoticed and costs review rounds.

## What

1. **`agent-hub run`**: for an implement job (role kind `implement`), after the brief is read and before the job
   starts, run `tasksize.check` on the brief text. Each problem → one line on stderr:
   `agent-hub: warning: brief size: <problem>`, then one line
   `agent-hub: warning: a large brief may be on purpose; split it or use 'agent-hub plan draft'`. The job runs as
   before; exit codes and stdout do not change. Review and plan kinds are not checked.
2. **`agent-hub plan run`**: before starting tasks, check every implement task's brief file the same way and print the
   warnings prefixed with the task name (`agent-hub: warning: task <name>: brief size: <problem>`) to stderr. The plan
   runs as before.
3. **Docs**: `README.md` "Plan mode" documents `agent-hub plan draft --spec <file> --slug <slug> [--base master]
   [-d <repo>]` (techlead writes `<state>/plans/<slug>/plan.json` + `briefs/`, one re-split, never runs it, table of
   sizes; exit 1 with the problems when the re-split is still too large) and the size warning.
   `skills/orchestrate/SKILL.md` "Plan mode" says: for a spec with more than a couple of tasks, run `plan draft`,
   read and edit the plan, then `plan run` — the director approves, the techlead splits.

## Out of scope

The MCP server (`run_job`, `plan_run`) — warnings there come later if wanted. Blocking on size (the warning never
blocks: a director brief can be large on purpose).

## Tests

- `run` with a large implement brief prints the warning lines on stderr and still starts the job (fake agent).
- `run` with a small brief prints no warning; a review job with a large brief prints none.
- `plan run` prints a warning per oversized implement task with its name; small tasks print none.
