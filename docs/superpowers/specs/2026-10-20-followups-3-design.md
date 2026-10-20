# Follow-ups 3 — design

Status: approved (director, 2026-10-20; `docs/follow-ups.md` "From plan followups-2"). Version target: v0.13.2.

## 1. A "success" implement job that changed nothing is `empty`
`agenthub/orchestrate.py`, after `guards.changes(repo, start_head)` (~line 652): for `kind == "implement"` with
`status == "ok"`, when `files == []` and `record["head"] == start_head` and the parsed handoff's `status` is
`success` (case-insensitive), set `status = "empty"` and `record["empty_reason"] = "success handoff but no change in
the work tree"`. A `blocked`/`partial` handoff or no handoff keeps today's behaviour (a blocked handoff is handled by
the plan runner, D-128). This happens before the exit code is chosen, so `exit_code` is 7 and the plan runner
escalates as for any `empty` job. Seen with agy 1.4.3 background tasks (D-128 follow-up). Tests with fake agents in
the existing orchestrate test style: success handoff + no change → `empty`; success + change → `ok`; blocked + no
change → `ok`; no handoff + no change → `ok`; an agent that committed (HEAD moved, clean tree) → `ok`.

## 2. Techlead brief asks for the tests that assert the old rule
`agenthub/templates/techlead-brief.md`: add one rule to the task-writing rules: "When a task changes an existing
behaviour or contract, grep `tests/` for assertions of the old behaviour and list every such test file in the
task's changed files (they count toward the file limit; split the task if needed)." Update the test that renders the
template (`tests/test_plandraft.py` or the file that asserts its text) to check the new sentence is present.
