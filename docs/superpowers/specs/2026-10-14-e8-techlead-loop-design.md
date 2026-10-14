# E8 — the techlead closes the loop (`plan triage`, `plan close`)

Status: design, 2026-10-14. Decisions D-099 (owner: the techlead verifies each task, not the director), D-101 (staging).

## Goal

A real plan closes with only two director acts: approve the plan going in, read one report coming out. Between them
the tool runs implement → gate → review → **triage** → fix → gate → review until every task is `ready` or `blocked`.

## Today

- `plan run` runs tasks; `plan review` gates and reviews every done task on its HEAD and returns per task
  `{gate, review: approve|request-changes|failed, job, reason}`; `plan merge` merges what `ready` accepts.
- A task set back to `pending` with `TaskState.reason` set is re-run **in its existing work tree** (`run_plan` reuses
  `.worktrees/<slug>-<task>`), and `reason` is appended to the brief (`compose_brief`). That is the fix mechanism.
- Retries count `len(tstate.attempts)` against `task.max_attempts`; a fix round must not be eaten by that count.

## Design

1. **Triage job.** For each task whose latest `plan review` result is `request-changes` (or gate failed), run one
   job of kind `plan` on role `techlead` (read-only, existing E3 machinery) with a brief rendered from
   `agenthub/templates/triage-brief.md`: the task's brief, the review job's reply (findings), the gate tail if any,
   the path of the review's `diff.patch`, and the work tree path. The techlead must not be any implementer of the task
   (`exclude` like reviews).
2. **Triage reply** (parsed by `agenthub/triage.py`, deterministic, like `plandraft.parse`): exactly one fenced `json`
   block `{"findings": [{"id", "severity", "decision": "fix"|"reject"|"blocked", "evidence"}], "fix_brief": "..."}`.
   - `fix`: goes into `fix_brief` (one brief per task, Goal/Input/Expected output/Constraints/Gate; size-checked with
     `tasksize.check`, problems → the task is `blocked` with them).
   - `reject`: `evidence` must cite `path:line` in the work tree or a recorded job id; no evidence → treated as `fix`.
   - `blocked`: needs the director (design trade-off without a D-xxx, spec conflict); the task stops `blocked`.
   - Every `medium`/`high` finding must be listed; an unlisted one → the task is `blocked` ("triage incomplete").
3. **Apply.** `fix_brief` is written to `<plan dir>/fixes/<task>-<round>.md`; the task is set to `pending` with
   `reason` = the fix brief, `fix_rounds += 1` (new `TaskState` field, saved), and attempts for the retry limit count
   only since the last fix round. Rejected findings are written to `<plan dir>/fixes/<task>-<round>-rejected.md` and
   passed to the next review as "already answered" context, so a repeat must bring new evidence (D-081).
4. **`agent-hub plan triage <plan>`** does steps 1–3 once for every task that needs it; prints per task: decision
   counts, fix brief path, or `blocked` + reason. Exit 0 when all triaged tasks got a fix or nothing needed triage,
   1 when any is blocked.
5. **`agent-hub plan close <plan> [--rounds 3]`** loops `plan run` → `plan review` → `plan triage` until every
   implement task's review is an independent `approve` with `ready` passing, or it is `blocked`/`failed`, or a task hit
   `--rounds` fix rounds (then `blocked: fix rounds exhausted`). It never merges. At the end it writes
   `<plan dir>/report.md` (one page: per task status, rounds, findings fixed/rejected/blocked with one line each,
   review job ids, what needs the director) and prints its path.
6. **MCP.** `plan_triage`, `plan_close` (start, detached) and the report through `plan_status`; same thin-layer rule.

## Later (E8b, D-101)

The `verify` job kind (techlead may run read/test commands, never write) for disproofs that need a run. Until then a
disproof must cite code or a recorded gate/job; anything that needs a run is a `fix` (add a test) or `blocked`.
