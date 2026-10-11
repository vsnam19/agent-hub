# Model routing by task tier — Opus directs, agy Opus / Haiku / gwen work

Status: approved (owner's strategy, autonomous mode for the gaps). Decisions D-057..D-064.

## 1. Strategy (owner, 2026-10-11)

Goal **A — save Claude quota/tokens** (D-057). Owner's division of labour (D-058):

- **Claude Opus 5.5 — director**: decomposes work into tasks, assigns each a tier, writes briefs, verifies, merges.
  Never implements.
- **agy Opus (claude-opus-4-6-thinking)** — hard tasks and every review.
- **Claude Haiku** — normal implementation tasks.
- **gwen (local Qwen2.5-Coder-14B on the Mac)** — small, easy implementation tasks.

Ideas taken from pstack (Lauren Tan's agent workflows, Claude Code ports adjohn/pstack, lifeofladi/pstack-claude):
roles mapped to models in one file, not in skills; review panels whose seats differ by lens; prove-it-works;
bulk work in subagents, summaries back to the director. pstack defines no escalation rule; we add one (§4).

## 2. Tiers (D-059)

The director picks the tier when it writes the task. Criteria, first match wins:

| Tier / role | Pick when | Not when |
|---|---|---|
| `implement-hard` | design is not settled in the brief; cross-cutting change (≥ 3 modules or an interface others use); concurrency, security, data migration; a bug whose cause is unknown | — |
| `implement-small` | ≤ 2 files, ≤ ~100 changed lines, every name/behaviour stated in the brief, brief + files to read ≤ ~6k tokens (gwen's context is 14k, D-056) | anything needing repo-wide search or > 2 files |
| `implement` | everything else: specified, several files, ≤ ~400 lines | > 400 lines → split the task |
| `review` | every review, including plan-mode `verifies` tasks | — |

## 3. Chains (D-060)

`agenthub/default_pool.toml` (a user `pool.toml` still overrides):

```toml
[roles.implement-small]
kind = "implement"
escalate = "implement"
chain = ["gwen:qwen2.5-coder-14b", "claude:haiku"]

[roles.implement]
kind = "implement"
escalate = "implement-hard"
chain = ["claude:haiku", "agy:claude-sonnet-4-6", "agy:gemini-3.1-pro-high", "codex:gpt-5.6-terra"]

[roles.implement-hard]
kind = "implement"
chain = ["agy:claude-opus-4-6-thinking", "agy:claude-sonnet-4-6", "codex:gpt-5.6-terra", "claude:sonnet"]

[roles.review]
kind = "review"
chain = ["agy:claude-opus-4-6-thinking", "codex:gpt-5.6-terra", "agy:gemini-3.1-pro-high", "claude:sonnet"]
```

Order inside each chain follows goal A: links that do not use Claude quota first, except where the owner put Haiku
first (`implement`). `claude:sonnet` is the last resort only for hard work and reviews. The `gwen` link exists once
the gwen adapter lands (D-047); until then `implement-small` starts at `claude:haiku`.

## 4. Roles have a kind; escalation (D-061)

- A role's `kind` (`implement` | `review`, default = the role's name for the two legacy names) decides CLI behaviour
  everywhere the code now tests the role name: brief requirements (D-011), handoff footer, gates, the gate guard,
  read-only review, default timeouts (`timeout.implement` / `timeout.review` apply by kind).
- `run --role` accepts any role in the pool. `plan` task `role` likewise.
- **Escalation**: a role may name `escalate = "<role>"`. In plan mode, when a task ends `gate-failed` or `failed`
  after its retries (D-034), the runner resets the task's work tree to the task's start point and runs the task once
  more at the escalated role, with the last gate output appended to the brief ("A previous attempt by a smaller model
  failed this gate: …"). One escalation per step; the chain small → implement → hard is at most two escalations.
  `exhausted`/`refused` never escalate (that is quota, not ability). The task state records `escalated_from`.
- A single `run` does not escalate on its own; the skill tells the director to re-run at the next tier.

## 5. Reviews (D-062)

- Every implement task the director merges gets one `review` (agy Opus first).
- A change that touches security, concurrency, data, or a public interface gets a second review from a different
  vendor with a different lens: `--agent codex:gpt-5.6-terra` with the brief line "Lens: does it actually run and
  do what it claims — check error paths and inputs that break it" (pstack's evidence/correctness seats).
- A review never runs on the same `agent:model` that implemented the change (pstack: verifier on a different tier);
  plan mode skips that link in the review chain.
- Findings are input to the director, not a verdict.

## 6. Skills in pstack's style (D-063, owner: "phong cách hãy tham khảo pstack")

pstack (adjohn/pstack, read 2026-10-11) shapes its skills as: one **mode** skill listing *non-negotiables*
(trigger → what to do) and *principles* (one line each, pointing at a leaf skill); one short **leaf skill per
principle** (`principle-*`, ~20 lines: the rule, **Why**, **Pattern**, **Boundaries**, `disable-model-invocation:
true` so only the mode loads it); the coordinator "owns the program, never the code"; defaults for each role written
inline, an override file on top. agent-hub follows that shape:

- `skills/orchestrate/SKILL.md` is rewritten as the mode: *Non-negotiables*, *Routing* (the tier table with inline
  defaults; `agent-hub pool` shows the live chains), *Principles* (list → leaf skills), *Autonomy*, *Statuses*.
  Existing content (the loop, plan mode, statuses, long sessions) is kept, reorganised, not dropped.
- Leaf skills under `skills/`, each `disable-model-invocation: true`:
  `principle-director-never-codes`, `principle-route-by-tier`, `principle-escalate-one-tier`,
  `principle-quota-first`, `principle-prove-it-works` (gates are not proof; run the result on real data),
  `principle-review-on-another-tier`, `principle-guard-the-context-window` (read handoffs, not full replies).
- The role → model mapping stays in `pool.toml` (the CLI enforces it — pstack's "encode lessons in structure"),
  not in a markdown file.

## 7. Standing orders (D-064)

pstack pastes a numbered `preferences.md` into every spawn ("directives decay across resumes"). agent-hub's CLI
appends **standing orders** to every implement and review brief, after the brief and before the handoff footer:
`~/.config/agent-hub/standing-orders.md` (user) then `<repo>/.agent-hub/standing-orders.md` (project), each if it
exists, under a heading `## Standing orders`. The text sent is recorded in the job's `brief.md`. Seeded user file:
the rules learned so far — run the repository's full lint and the existing tests of every file you touch; never write
a test without assertions; never write into the repository root from tests; never edit gate configuration; do not
call real external services from tests. The verifier rule from pstack ("run a unit's verifier on a different model
tier from its worker") is enforced in code: a `review` skips any link with the same `agent:model` that produced the
change when that is known (plan mode `verifies`), D-062.

## 8. Out of scope

Effort levels per role (Claude Code's Agent tool cannot set effort per call; headless `claude -p` has no effort flag
in use here), arena/swarm (parallel attempts cost quota, against goal A), a cost meter.
