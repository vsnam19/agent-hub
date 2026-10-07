---
name: orchestrate
description: Use when delegating implementation work or a second-opinion code review to other coding agents through agent-hub — writing the brief, running it on the agent pool by role with quota fallback, and verifying the result yourself.
---

# Orchestrate

You are the orchestrator. `agent-hub` runs a job on the first agent in a role's chain that still has quota and
leaves a record you can verify. This skill names no agent: read the pool at run time. Placeholders below look
like `<agent>:<model>`.

## The loop

1. **Decide what to delegate.** Only implementation and second-opinion review go to the pool. Design, planning,
   research, verification and the final judgement stay with you. A tiny edit is cheaper to do yourself.
2. **Look at the pool.** Run `agent-hub pool` to see each role's chain, what is installed and what is on
   cooldown. Run `agent-hub profile <agent>` for every agent you are about to use and shape the brief to its
   known weaknesses.
3. **Write the brief** from `brief-template.md` (Goal / Input / Expected output / Constraints / Gate).
   Input names the files, the current behaviour and the interfaces. Expected output names the files to change,
   the exact test names that must pass, and the exact commands with their expected result. The Gate always
   includes the repository's full lint and the existing tests of every touched file, and forbids tests without
   assertions. The CLI refuses an implement brief that lacks `## Input` or `## Expected output` (exit 2).
4. **Prepare a worktree** on a feature branch. The CLI never creates one and refuses the default branch or a
   detached HEAD (`refused`, exit 3).
5. **Run in the background:**
   `agent-hub run --role implement --brief brief.md -d <worktree> --json`
   using the Bash tool's `run_in_background`, then do other work until you are notified. Do not poll.
6. **Read the result** and act on its `status` (table below). Never re-run blindly on a dirty tree.
7. **Verify yourself** in the worktree: run the gate, read the whole diff, and scan every new test for missing
   assertions. The agent's claims are not evidence.
8. **Optional second opinion:** `agent-hub run --role review -d <worktree> --base <default branch>`.
   The CLI computes `git diff <base>...HEAD` into the job's `diff.patch` and the reviewer reads that file; the
   reviewer never gets a shell. Treat findings as input to your own judgement, not as a verdict.
9. **Force a link** with `--agent <agent>:<model>` when one model's output is poor or you need a specific one.
   It replaces the chain with that single link.

## Statuses

`result.json` (also `agent-hub show <id> --json`) carries `status` and `exit_code`. Inspect a finished job with
`agent-hub show <id>`; list recent ones with `agent-hub jobs`.

| Status | Exit | Meaning | What you do next |
|---|---|---|---|
| `ok` | 0 | An agent finished with a non-empty reply. | Verify (step 7). Do not trust the reply. |
| `failed` | 1 | Failure that is not quota, auth or model. | Read the reply and stderr in the attempt directory, inspect the tree, then re-brief or take over. |
| `refused` | 3 | A guard refused the work directory. | Fix the directory: use a git work tree on a feature branch, then run again. |
| `exhausted` | 4 | No link could run (all skipped or fell through). | Report which links failed and why; `agent-hub pool` shows cooldowns; wait, clear one with `pool --clear`, or do it yourself. |
| `modified-tree` | 5 | A review changed the work tree. | Treat the review as untrustworthy; inspect and revert the change, then re-run or discard. |
| `timeout` | 6 | The attempt hit its timeout. | Inspect the tree and any partial reply; re-brief with a smaller scope or a longer `--timeout`. |
| `empty` | 7 | The agent exited cleanly but returned nothing. | Inspect the tree; the work may still be done. Re-brief or take over. |
| `interrupted` | 8 | Quota, auth or model failure after the tree had changed; no fallback. | Inspect the half-done tree, then finish it yourself or re-brief a fresh run from a clean state. |
| `cancelled` | 130 | `agent-hub cancel` or Ctrl-C. | Inspect the tree; start again only if still wanted. |

Exit code 2 has no status: it means a usage or configuration error (bad flag, unknown agent, brief missing a
required heading, unknown job id). Fix the call and run again.

## Rules

- An agent's claims are not evidence. "All tests pass" in a reply means nothing until you ran the gate.
- Fallback only happens while the tree is unchanged; once an agent has edited files, the next step is yours.
- Never put secrets in a brief; briefs are stored in the job directory.
