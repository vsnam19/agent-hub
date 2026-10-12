# Phase E — a techlead role that splits work into small tasks

Status: design, 2026-10-12. Decision D-094. Owner's request (2026-10-12): "có thể sẽ cần 1 ông trưởng phòng hoặc
techlead để split task, ông này sẽ làm role như techlead ngoài đời", after "chúng ta cần split task đủ nhỏ".

## Why

- The director (Opus) splits work today. Its briefs grew too large: `skills-mine` (parser + redaction + CLI + metrics
  + grouping in one brief) took 12+ review rounds; each round found another edge in a different concern.
- Splitting is judgement on the repository, not orchestration: a cheaper model with read access does it as well as
  a tech lead does in a team, and the director's context stays small (principle-guard-the-context-window).
- Small tasks are what haiku (`implement`) handles well (owner, 2026-10-12: implement work goes to haiku).

## The role

- A new job kind **`plan`** and role **`techlead`** (chain `claude:sonnet`, `agy:claude-opus-4-6-thinking`,
  `agy:gemini-3.1-pro-high`). A plan job is **read-only** like a review (same per-agent read-only invocation: claude
  `--tools Read,Grep,Glob,Skill --strict-mcp-config`, agy plan mode, codex read-only sandbox), with no diff: it reads
  the spec and the repository.
- Its brief (template `agenthub/templates/techlead-brief.md`) asks for: tasks in dependency order, each with a role
  (`implement-small` | `implement` | `implement-hard`, default `implement`, a reason when not default), and a full
  brief per task (Goal / Input / Expected output / Constraints / Gate) — **one concern, at most 3 files changed,
  at most 4 numbered deliverables, at most 6 named tests**, the repository gates, exact test names, a real check when
  behaviour touches an external tool. Output: exactly one fenced `json` block `{"goal", "tasks": [{"name", "role",
  "depends_on", "reason", "brief"}]}` in the final message.

## The size check (deterministic)

`agenthub/tasksize.py` checks one brief: required headings; numbered deliverables under `## Expected output` ≤ 4;
files listed as changing (the `Only … change` constraint and backticked paths under Expected output) ≤ 3; named tests
≤ 6. It returns problems, never edits. Used by `plan draft` (reject → one re-split) and by `agent-hub run`/`plan run`
as a warning (a director-written brief can still be large on purpose; the warning says so).

## The command

`agent-hub plan draft --spec <file> --slug <slug> [--base master] [-d <repo>]`: runs the techlead job, parses the JSON
block, size-checks every brief; if any fails, runs the techlead once more with the problems listed; writes
`<state>/plans/<slug>/plan.json` + `briefs/<task>.md` (gates from the repository's `.agent-hub/config.toml`), prints
a table (task, role, files, deliverables, tests). It **never runs the plan**: the director reads, edits if needed and
starts `plan run` — a tech lead proposes, the manager approves.

## Tasks (small, in order; each `implement` unless noted)

| # | Task | Files | Done means |
|---|---|---|---|
| E1 | `tasksize.check(brief_text) -> list[str]` | `agenthub/tasksize.py`, test | tests on a small brief (no problems) and on each limit |
| E2 | parse a techlead reply into plan + briefs | `agenthub/plandraft.py`, test | tests: valid block, no block, two blocks, bad role, cycle in `depends_on`, unknown dependency |
| E3 | job kind `plan` + role `techlead` read-only, no diff | `agenthub/orchestrate.py`, `default_pool.toml`, test | a plan job's argv is read-only per agent; no `diff.patch`; reply kept |
| E4 | techlead brief template | `agenthub/templates/techlead-brief.md`, test | rendered with the spec path and the size limits from `tasksize` |
| E5 | `agent-hub plan draft` (E1–E4 wired, one re-split) | `agenthub/cli.py`, test | fake techlead: pass, re-split once, give up with problems; real run on this spec |
| E6 | size warning in `run`/`plan run`; orchestrate skill + README | `agenthub/cli.py`, `skills/orchestrate/SKILL.md`, `README.md` | warning test; docs |
