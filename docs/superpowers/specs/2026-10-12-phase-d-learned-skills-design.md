# Phase D — learned skills: repeated work becomes a tested, released skill, every Sunday

Status: design, 2026-10-12. Decision D-091. Owner's request (2026-10-12): "when a piece of work is done again and
again, gather what the sessions learned, write a skill (or MCP), test it and release it; schedule it every Sunday
evening to update the skills".

## What repeats here (measured 2026-10-12, read-only)

- **User prompts barely repeat.** 597 typed prompts in `~/.claude/projects/*/*.jsonl`; apart from "continue" and
  the automated security-review prompt (254, headless), almost every one is unique. Mining prompt text finds nothing.
- **Shell commands repeat but say nothing** (`git status`, `ls`, heredocs): too low-level to be a skill.
- **What repeats is the work loop and what reviewers catch.** 247 agent-hub jobs: 84 plan tasks, ~55 reviews and
  9 "review round N fixes" briefs; a Phase B branch took 1–5 review rounds. The rounds exist because workers keep
  making the same kinds of mistakes (tests without assertions, missing timeouts, unverified claims about tools,
  scope creep…), and the director keeps writing the same fix briefs. That is the knowledge a skill should carry.

So the miner reads **agent-hub job records** first (review findings, fix briefs, statuses, round counts), and
**director transcripts** second, for repeated owner corrections ("không được…", "phải…") and repeated command
sequences of agent-hub itself.

## Design

### 1. Mine (deterministic, no model) — `agent-hub skills mine`

- Input: job records under the state dir since a window start (default: last weekly run, at least 28 days);
  transcripts under `~/.claude/projects/` for user messages and agent-hub command lines only.
- Extract: each review's findings (severity, file, one-line claim), fix-brief titles and their branch, round counts
  per branch, owner corrections (user messages matching a correction pattern list), job outcomes by tier.
- **Redact** before anything leaves the machine: secrets patterns (keys, tokens, `password=`, private keys, long
  base64/hex), e-mail addresses, absolute home paths → `~`. Excerpts are capped (300 chars each). Never raw
  transcripts in a brief; the extract is a file the drafting worker reads.
- Output: `<state>/skills/<date>/extract.json` + a short `extract.md`, and `metrics.json` (rounds per branch, findings
  per category) so the effect of a skill is measurable week over week.

### 2. Draft (a worker) — job in the agent-plugins repo

- One `implement-hard` job per weekly run, on branch `learned/<date>` in an agent-plugins worktree, with the
  extract copied into the worktree's ignored `.agent-hub/` dir. The worker clusters findings into at most **3**
  candidates (each seen ≥ 3 times across ≥ 2 branches, not already covered by an existing skill), and for each writes
  or updates a skill plus its eval cases. Fewer or none is a valid outcome.
- Two owned plugins (D-084 keeps worker and director skills apart): **`learned-worker`** (lessons for implementers:
  what reviewers keep finding, how to prove a change) and **`learned-director`** (repeated orchestration loops and
  owner corrections). Each skill ≤ 450 words with a rationalizations table (as D-087), sources listed by job id.
- **Skill, not MCP, by default.** A candidate becomes an MCP server only when it is a deterministic tool call that
  text cannot carry (a query, a computation); then the worker writes a proposal in the report, and an MCP server is a
  separate, owner-approved task. (No such candidate exists in today's data.)

### 3. Test — `claude plugin eval` with ablation

- Each new or changed skill ships `evals/<skill>/` cases (prompt + graders) in its plugin; the gate runs
  `claude plugin eval <plugin> --case <skill>* --ablation with-without --max-cost-usd <cap> --json`. A skill passes
  when the with-plugin arm meets the case graders and scores above the no-plugin baseline. Plus the agent-plugins repo
  gate (`check.sh`: manifests validated by claude, agy and codex; pytest).

### 4. Review, merge, release — the official flow only

- `agent-hub run --role review` (a reviewer that differs from the drafter), up to 2 automatic fix rounds with
  briefs generated from the findings, then `agent-hub ready`; merge only when `ready` passes (D-081: no bypass).
- Release = patch version bump of the changed plugin(s), commit on agent-plugins master, `agent-hub hub install
  --apply` (codex, agy); claude and gwen workers read the source per run (D-083). The director's own Claude Code
  picks up `learned-director` through its marketplace update. **Nothing is pushed** (standing rule: ask before push).
- Anything that fails (no candidate, eval below baseline, review not approved after 2 rounds, quota) stops the run
  with the branch kept and the reason in the report; nothing half-released.

### 5. Report and schedule

- `<state>/skills/<date>/report.md`: candidates, what was drafted, eval scores vs baseline, review verdict, released
  versions, metrics trend (rounds per branch, top finding categories), MCP proposals; `agent-hub skills report`
  prints the latest; a desktop notification (`notify-send`) when present.
- `agent-hub skills schedule [--apply]` installs a **systemd user timer** (the machine already uses user timers):
  `OnCalendar=Sun *-*-* 20:17` local time (Asia/Ho_Chi_Minh), `Persistent=true` (a missed Sunday runs at next
  login), service `agent-hub skills weekly`. Dry-run by default; `--apply` writes the units under
  `~/.config/systemd/user/` and runs `systemctl --user daemon-reload` + `enable --now`; `--remove` undoes it.
  Not CronCreate (session-only, expires after 7 days) and not a cloud routine (it cannot read this machine's records).
- `agent-hub skills weekly [--dry-run]` chains 1–5 and holds a lock so two runs never overlap.

## Tasks (plan `phased`, tiers per D-059, merged only through `agent-hub ready`)

| # | Task | Repo | Tier | Done means |
|---|---|---|---|---|
| 1 | `learned-worker` + `learned-director` plugin skeletons (all manifests, empty `skills/`, `evals/` layout, NOTICE) | agent-plugins | implement | `check.sh` validates both; one sample eval case runs with `claude plugin eval` |
| 2 | `skills mine`: extract, redaction, metrics | agent-hub | implement-hard | tests on synthetic job records and transcripts incl. secrets; a real run on this machine lists the review-round loop |
| 3 | `skills weekly`: draft → eval → review → ready → release → report, lock, dry-run | agent-hub | implement-hard | tests with fake `agent-hub run`/`claude plugin eval`; a real `--dry-run` |
| 4 | `skills schedule` + `skills report` | agent-hub | implement | tests on generated units; `--apply` on this machine, `systemctl --user list-timers` shows it |
| 5 | First real weekly run by hand, then decide thresholds | — | director | report reviewed with the owner |

## Owner decisions taken by default (change any)

- Release happens without a human only through `agent-hub ready` (independent approve + gate + eval above baseline);
  it is local and never pushed.
- At most 3 skills per week; MCP servers only as proposals.
