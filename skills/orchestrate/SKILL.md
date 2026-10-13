---
name: orchestrate
description: Use when delegating implementation work or a second-opinion code review to other coding agents through agent-hub — choosing a tier, writing the brief, running it on the agent pool by role with quota fallback and escalation, and verifying the result yourself.
---

# Orchestrate

You are the director. `agent-hub` runs a job on the first agent in a role's chain that still has quota and leaves a
record you can verify. You decompose, brief, verify and merge; the pool writes the code. This skill names no agent:
read the pool at run time. Placeholders below look like `<agent>:<model>`.

Prefer the `agent-hub` MCP tools when present in your session; use CLI commands otherwise.


## Non-negotiables

Each trigger maps to one action. The Principles below ground them; read a leaf skill in full before you rely on it.

- About to edit product code yourself → stop and delegate it (**principle-director-never-codes**).
- Writing a task → pick its tier from the Routing table before you write the brief (**principle-route-by-tier**).
- A job ended `gate-failed` or `failed` → move it one tier up, never the same tier again (**principle-escalate-one-tier**).
- A link is on cooldown or out of quota → let the chain fall back; do not hunt for a Claude-quota workaround (**principle-quota-first**).
- A job reports success → run the gate and read the diff yourself; the reply is not evidence (**principle-prove-it-works**).
- A change is ready to merge → review it on a model other than the one that wrote it (**principle-review-on-another-tier**).
- Reading output → read handoffs and `result.json`, not full replies or whole logs (**principle-guard-the-context-window**).
- Starting a job → never end your turn while it runs; keep calling `wait`.
- Writing a brief → never put secrets in it; briefs are stored in the job directory.
- A tree an agent already edited → no automatic fallback; the next step is yours.

## Routing

The director picks the tier when it writes the task; first match wins. The role's `kind` (`implement` or `review`)
decides CLI behaviour: brief headings, handoff footer, gates, read-only review, default timeouts.

| Tier (`--role`) | Pick when | Default chain, in order |
|---|---|---|
| `implement-hard` | design not settled in the brief; cross-cutting (≥ 3 modules or a shared interface); concurrency, security, data migration; a bug of unknown cause | strongest non-Claude model first, then other vendors, the larger Claude model last |
| `implement-small` | ≤ 2 files, ≤ ~100 changed lines, every name and behaviour stated in the brief, brief plus files to read ≤ ~6k tokens | local small model, then `claude:haiku` |
| `implement` | everything else: specified, several files, ≤ ~400 lines; more than that → split the task | `claude:haiku`, then other vendors' mid models |
| `review` | every review, including plan-mode `verifies` tasks | strongest non-Claude model first, other vendors, the larger Claude model last |

`agent-hub pool` is the live truth: it prints each role's kind, escalation target and chain, what is installed and
what is on cooldown. A user `pool.toml` overrides the defaults. `agent-hub profile <agent>` shows an agent's
weaknesses so you can shape the brief. `--agent <agent>:<model>` replaces the chain with that single link when one
model's output is poor or you need a specific one.

## Principles

Read the leaf skill in full for any principle you apply.

- **Director never codes** (**principle-director-never-codes**). Tempted to patch a file "quickly". You decompose, brief, verify and merge.
- **Route by tier** (**principle-route-by-tier**). Writing a task. Choose the cheapest tier whose criteria the brief meets.
- **Escalate one tier** (**principle-escalate-one-tier**). A task failed its gate. Reset, go one role up, show the failure.
- **Quota first** (**principle-quota-first**). Choosing or reordering a chain. Spend non-Claude quota before Claude quota.
- **Prove it works** (**principle-prove-it-works**). Before accepting or merging. Gates are not proof; run the result.
- **Review on another tier** (**principle-review-on-another-tier**). Reviewing. Never the model that implemented the change.
- **Guard the context window** (**principle-guard-the-context-window**). Output grows. Read handoffs, not full replies.

## The loop

1. **Decide what to delegate.** Implementation and second-opinion review go to the pool. Design, planning, research, and verification stay with you.
2. **Look at the pool.** Run `agent-hub pool` (or call `pool` MCP tool), then pick the tier from Routing. Run `agent-hub profile <agent>` to shape the brief to an agent's weaknesses.
3. **Write the brief** from `brief-template.md` (Goal / Input / Expected output / Constraints / Gate).
   Input names the files, the current behaviour and the interfaces. Expected output names the files to change,
   the exact test names that must pass, and the exact commands with their expected result. The Gate always
   includes the repository's full lint and the existing tests of every touched file, and forbids tests without
   assertions. The CLI refuses briefs lacking `## Input` or `## Expected output`. It then appends your standing
   orders (`~/.config/agent-hub/standing-orders.md`, then `<repo>/.agent-hub/standing-orders.md`) under
   `## Standing orders`, so do not restate them by hand; the text sent is recorded in the job's `brief.md`.
   Name the worker skills the task needs with `--skill` (e.g. `debug-playbook` for a bug of unknown cause).
4. **Prepare a worktree** on a feature branch. The CLI refuses the default branch or detached HEAD.
5. **Run the job, then wait for it.** Prefer MCP tools when present: call `run_job` (detached, returns `job_id`), then call `wait_job(job_id, timeout_s)` in a bounded loop until finished. Otherwise, start it with the Bash tool's `run_in_background`:
   `agent-hub run --role <tier> --brief brief.md -d <worktree> --json`
   Then call `agent-hub wait -d <worktree> --timeout 9m`. Exit 124 means it is running: do independent work, then call `wait` again. Any other exit is the
   job's own exit code and `wait` has printed its result. Steps 6–7 happen before your final reply; never end
   your turn while a job you started is still running, because nothing wakes you after your turn ends.
6. **Read the result** and act on its `status` (table below). Never re-run blindly on a dirty tree. A single `run`
   never escalates by itself: after a failure, re-run at the role's `escalate` target (see `agent-hub pool` or `pool` MCP tool).
7. **Verify yourself** in the worktree: run the gate, read the whole diff, and scan every new test for missing
   assertions. Check the diff for edits to the gate itself (tests, justfile, lint config) and pass the repository's
   lint and tests as `--gate`. The agent's claims are not evidence. Read every `gate_touched` hunk; gate-touched findings
   are judged by the reviewer, never by you. Once verified, commit the work yourself if the agent did not (some agents cannot commit by design).
   After committing, run `agent-hub gate -d <worktree>` (or `gate_start` + `wait_job`), then review, then `agent-hub ready` (or `ready` MCP tool).
8. **Review every change you merge:** start it the same way (MCP `run_job` + `wait_job`, or background CLI + `wait`):
   `agent-hub run --role review -d <worktree> --base <default branch>`.
   The CLI computes `git diff <base>...HEAD` into the job's `diff.patch` and the reviewer reads that file; the
   reviewer never gets a shell. A change touching security, concurrency, data or a public interface gets a second
   review from a different vendor with a different lens (for example `--agent <other vendor>` and the brief line
   "Lens: does it actually run and do what it claims — check error paths and inputs that break it").
   `--exclude <agent>:<model>` skips the link that wrote the change. Treat findings as input to your own judgement,
   not as a verdict. Before merging, run `agent-hub ready -d <worktree>` (or `ready` MCP tool): it exits 1 (or returns `ready: false`) unless the current `HEAD` passed
   its gates and a different model approved that same commit.
9. **Force a link** with `--agent <agent>:<model>` when one model's output is poor or you need a specific one.
   It replaces the chain with that single link.

## Plan mode

When a goal has several tasks, write a plan file. Prefer the MCP tools when present (`plan_run`, `plan_wait`, `plan_status`, `plan_review`, `plan_merge`); otherwise use `agent-hub plan run` (background) + `agent-hub plan wait` until it stops returning 124. Plan file fields: `goal`, `repo`, `base`, `slug`, `tasks`
(with `name`, `role`, `brief` or `verifies`, `depends_on`; `role` is any pool role). Use `"gate"` to pass the
repository's lint and tests. Read `handoffs/` first, the full reply only when needed, and use `plan status` (or `plan_status` MCP tool). Merge
verified `orch/<slug>/*` branches with `plan_merge` or yourself.


- **Escalation.** When an implement task ends `gate-failed`, `failed`, `timeout`, `empty` or `interrupted` after its
  retries and its role names an `escalate` target, the runner resets the task's tree to its start point and runs it
  once at that role, with the failing gate output appended to the brief. At most one step per escalation; the chain
  small → normal → hard is at most two. `exhausted` and `refused` never escalate: that is quota, not ability.
  `plan status` shows `escalated from <role>`. An escalated run uses the escalated role's chain, not a pinned agent.
- **Autocommit.** When a gated implement task passes with work left uncommitted, the runner commits it on the task
  branch so dependent tasks start from it. A task you chain from needs no commit by the agent.
- **Review other-model.** A `verifies` task skips the link that implemented the task it checks; if no other
  reviewer exists the task is blocked with a reason instead of a self-review.

## MCP tools vs CLI

Prefer the MCP tools when present in your session; fall back to the CLI commands otherwise:

| Action | MCP tool (prefer) | CLI command (fallback) |
|---|---|---|
| Read pool | `pool()` | `agent-hub pool` |
| List / show jobs | `jobs(repo_dir)`, `show_job(job_id)` | `agent-hub jobs`, `agent-hub show <id>` |
| Run job & wait | `run_job(...)` → `wait_job(job_id, timeout_s)` | `agent-hub run ...` (background) → `agent-hub wait` |
| Gate committed HEAD | `gate_start(workdir)` → `wait_job(job_id, timeout_s)` | `agent-hub gate -d <worktree>` |
| Readiness check | `ready(workdir)` | `agent-hub ready -d <worktree>` |
| Plan mode | `plan_run`, `plan_status`, `plan_wait`, `plan_review`, `plan_merge` | `agent-hub plan run/status/wait/review/merge` |

## Claude Code agents


The plugin ships role agents for Claude Code's Agent tool. Use them for work that needs no job record:

- `agent-hub:implementer` for a trivial change whose checks the brief states, where a job's overhead is not worth it. You still verify it (step 7).
- Anything that needs gates, records or fallback is an agent-hub job (step 5). That is the default.
- `agent-hub:explorer` for any read that would otherwise pull many files into your context. It returns a digest with `path:line` citations.
- `agent-hub:reviewer-correctness`, `agent-hub:reviewer-evidence` and `agent-hub:reviewer-design` form a panel for a risky change. Use the panel as the second lens of step 8 when the review chain is unavailable. Start the seats in one message so they run in parallel.

## Long sessions

Every request re-reads the whole conversation, so a long session gets expensive with each tool call. At a phase
boundary — plan written → implementation, a plan finished and merged, after an abandoned approach — first make sure
the next phase needs nothing from the chat: plan file, handoffs, decisions and commits are on disk. Then tell the user
to run `/compact` or start a new session. Do not compact in the middle of a task: file paths and partial state are lost.

## Autonomy

Proceed without asking on reversible work: preparing worktrees, running jobs, re-briefing, escalating, reviewing,
committing on feature branches and merging verified `orch/<slug>/*` branches locally. Pause and ask before
anything irreversible or outward-facing: pushing, creating a remote or a pull request, deleting branches, worktrees
or data, and force operations. "Don't stop" or "run until done" from the user means keep going, not skip those.

## Statuses

`result.json` (also `agent-hub show <id> --json`) carries `status` and `exit_code`. Inspect a finished job with
`agent-hub show <id>`; list recent ones with `agent-hub jobs`.

| Status | Exit | Meaning | What you do next |
|---|---|---|---|
| `ok` | 0 | An agent finished with a non-empty reply. | Verify (step 7). Do not trust the reply. |
| `failed` | 1 | Failure that is not quota, auth or model. | Read the reply and stderr in the attempt directory, inspect the tree, then re-brief or escalate. |
| `refused` | 3 | A guard refused the work directory. | Fix the directory: use a git work tree on a feature branch, then run again. |
| `exhausted` | 4 | No link could run (all skipped or fell through). | Report which links failed and why; `agent-hub pool` shows cooldowns; wait, clear one with `pool --clear`, or do it yourself. |
| `modified-tree` | 5 | A review changed the work tree. | Treat the review as untrustworthy; inspect and revert the change, then re-run or discard. |
| `timeout` | 6 | The attempt hit its timeout. | Inspect the tree and any partial reply; re-brief with a smaller scope or a longer `--timeout`. |
| `empty` | 7 | The agent exited cleanly but returned nothing. | Inspect the tree; the work may still be done. Re-brief or escalate. |
| `interrupted` | 8 | Quota, auth or model failure after the tree had changed; no fallback. | Inspect the half-done tree, then finish it yourself or re-brief a fresh run from a clean state. |
| `gate-failed` | 9 | A gate command failed. | Read the gate tail; re-brief, escalate one tier, or fix. |
| `scope-violation` | 10 | `--allow` was given and the run changed files outside the globs (`scope_violation` lists them). | Nothing is reverted; inspect the listed files, revert them or re-brief with a tighter scope. |
| `cancelled` | 130 | `agent-hub cancel` or Ctrl-C. | Inspect the tree; start again only if still wanted. |

Exit code 2 has no status: it means a usage or configuration error (bad flag, unknown agent or role, brief missing a
required heading, unknown job id). Fix the call and run again.

`wait` exits 124 while the job is still running.

## Rules

- Never end your turn while a job you started is still running; keep calling wait.
- An agent's claims are not evidence. "All tests pass" in a reply means nothing until you ran the gate.
- Fallback only happens while the tree is unchanged; once an agent has edited files, the next step is yours.
- Never put secrets in a brief; briefs are stored in the job directory.
