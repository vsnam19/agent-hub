> **Update 2026-10-18 — E8 merged.** All 16 E8 tasks (`plan triage` + `plan close`, D-101/D-102) went through review
> rounds (agy Gemini fixes, claude:opus reviews), were cascaded down the stacked branches, approved by a model other
> than the fix author and merged into master through `ready` (not pushed). Master: lint clean, 1105 tests pass. gwen is
> off (owner); route to agy Gemini first to save Claude tokens. Open items from this round are in `docs/follow-ups.md`
> (cascade in `plan close`, merged tasks counted as skipped, fix author may review). **Next:** release (version bump,
> `hub install`, plugin reload) when the owner asks; dogfood `plan close` on a small plan; then F6, E5–E7.

> **Update 2026-10-14, night — deadlock + agy fixes merged; E8 review results on disk.** Merged on master (not
> pushed): plan `deadlock` (`plan_review` reaps its runner, zombie-aware waits, `plan_wait` → `stalled` on a dead
> runner) and `fix/agy-ssh-auth` (agy headless works under SSH via the keyring). Released as **v0.10.1** (plugin
> installed; MCP server smoke-tested). The installed CLI is an editable install of this repo.
> **E8 review is done; do not re-run it.** Results:
> `~/.local/state/agent-hub/mcp/plan-review/review-e8-1791955390390950145.json` (16 lines: 6 approve,
> 7 request-changes, 3 failed because agy auth was broken → re-review those on agy). Next: fix E8 findings by rounds,
> then `plan merge`. Wait rule: never `kill -0 <pid>`; wait on result files with a waiter that also exits on a
> dead/zombie process (see `docs/follow-ups.md` "Director deadlock avoidance"). `wait_job`/`agent-hub wait` also detect a dead
> job (`fix/wait-job-dead`, merged).

> **Update 2026-10-14, later — E8 implemented, not yet reviewed/merged.** Design
> `docs/superpowers/specs/2026-10-14-e8-techlead-loop-design.md` (D-101: `plan triage` + `plan close` first, `verify`
> kind later as E8b). The techlead job `20261014-021547-techlead-c0e9` (claude:sonnet) split it into 16 tasks; the plan
> is `~/.local/state/agent-hub/plans/e8/plan.json` (base master, slug `e8`). `plan run` finished: **all 16 tasks done**,
> gates passed; `triage-task` and `mcp-triage-close` have `gate touched: 1` (read those hunks in review).
> **Next:** `agent-hub plan review ~/.local/state/agent-hub/plans/e8/plan.json` → fix findings (rounds) →
> `agent-hub plan merge …` (through `ready`, never push) → then use the new `plan triage`/`plan close` on its own
> follow-ups (dogfood). Restart Claude Code first so the agent-hub MCP tools load (`/compact` does not reload MCP;
> the server itself answers with all 13 tools). Then F6, E5–E7, follow-ups.

> **Update 2026-10-14 — v0.10.0.** Phases C, D, F done; E partly. Read this block, then `docs/overview.md`,
> `docs/decisions.md` (D-090..D-100), `docs/follow-ups.md`, and the user memory (pool, small tasks, techlead verifies).
>
> - **C** (D-090, D-095): C++ suite in the gwen bench; gwen 2/6 on C++ → no C++ tasks for gwen yet.
> - **D** (D-091..D-093): `skills mine|weekly|report|schedule`; timer Sun 20:17 installed; first real run stopped
>   correctly at eval (follow-ups: eval cases must beat the baseline, unparsed findings, report wording).
> - **E** (D-094, D-099): done E0 effort links, E1 `tasksize`, E2 `plandraft`, E3 kind `plan` + `techlead`, E4 template.
>   Next: **E8 techlead loop** (`verify` mode + `plan triage`) — the director must stop checking each task (owner) —
>   then E5 `plan draft`, E6, E7 techlead bench (opus@low vs sonnet vs gemini).
> - **F** (D-098, D-100): `agent-hub mcp` with 13 tools, shipped in the plugin's `mcpServers`; next F6 (codex/agy
>   registration through their own `mcp` commands via `hub install`).
> - **Loop now in the tool** (D-097): `plan run` → `plan wait` → `plan review` → `plan merge`. Do not drive work with
>   shell loops. Pool (owner): implement = agy Sonnet → agy Gemini 3.8 Flash → haiku; implement-small = haiku → gwen;
>   review = agy Gemini 3.8 Flash → claude:opus; local LLMs only on the Mac (D-096).
> - Nothing pushed. Project overview PDF: `docs/agent-hub-overview.pdf` (update its status section after E8).

# Next phase — resume note (2026-10-12)

> **Update 2026-10-12, later:** Phase B is done (agent-hub v0.9.0, D-083..D-089). Every worker host now gets
> `worker`, `telaf`, `aspice`: claude and gwen per run (`~/.config/agent-hub/workers.toml`), codex and agy installed by
> `agent-hub hub install --apply` (all `current`). Next is **Phase C** below. Open items: `docs/follow-ups.md`.


Written before compacting. A fresh session starts here: read this, then `docs/overview.md`, then act.

## Status at the end of the previous phase

- agent-hub **v0.8.0** on `master`, 519 tests green, plugin `agent-hub@agent-plugins` installed (restart Claude Code to
  load it). No worktrees or plans in flight. Decisions D-001..D-082; open items `docs/follow-ups.md`.
- Done in the phase: tiered routing (D-057..D-061), gwen = Qwen3.5-9B + opencode (D-069, D-079), pstack trust
  mechanics (D-071..D-076, D-080), scoped cancel (D-078), **no gate bypass** (D-081, D-082), role agents in the plugin.
- Quota: agy Claude models back 2026-10-12 16:16 UTC; codex monthly limit until 2026-11-06 19:18 UTC.
- The owner's standing rules for this work: autonomous decisions logged in `docs/decisions.md`; Opus directs and never
  codes; route by tier; no gate bypass, official fixes, findings fixed not parked; ask before push/remote creation.

## Phase B — one skill set for every worker (learned from addyosmani/agent-skills)

Goal: agy, codex, claude and gwen (opencode) all get the same skills — including the owner's `telaf` and `aspice` —
through each tool's **official** plugin/skill mechanism, so domain knowledge reaches every worker (D-019, D-077).

What agent-skills (MIT, github.com/addyosmani/agent-skills, read 2026-10-12) shows:

| Host | Mechanism | Install |
|---|---|---|
| Claude Code | `.claude-plugin/plugin.json` + `skills/<name>/SKILL.md` | `claude plugin marketplace add …` / `claude plugin install …` |
| Codex | `.codex-plugin/plugin.json` (points `skills` at `./skills/`) + `.agents/plugins/marketplace.json` | `codex plugin marketplace add <repo-or-path>` then `codex plugin add <plugin>@<marketplace>` |
| agy (Antigravity CLI) | first-class plugins; lands in `~/.gemini/config/plugins/<name>/` | `agy plugin install <git-url-or-path>`; check `agy plugin list`, `agy plugin validate` |
| OpenCode | reads `.claude/skills/<name>/SKILL.md` and `.agents/skills/<name>/SKILL.md` | copy/link skill dirs (global or per repo) |

One `skills/` directory, same `name` + `description` frontmatter, serves all of them; no duplication.

**Task 1 (spike) done 2026-10-12** — findings and the revised task list (2, 3a, 3b, 4–6) are in
`docs/superpowers/specs/2026-10-12-phase-b-skill-hub-design.md`; decisions D-083..D-086. The list below is the
original plan; the spec's table supersedes it.

### Tasks (tiers per D-059; run as an agent-hub plan, merge only through `agent-hub ready`)

1. **Spike (director, read-only):** on this machine, verify each host's real mechanism with the installed versions
   (`codex --version` 0.152.x, `agy` 1.3.x, `opencode` 1.18.x): where skills land, how they are listed, whether a
   headless run (`codex exec`, `agy -p`, `opencode run`) actually loads them. Write the findings into a spec section.
2. **`agent-plugins` marketplace gains Codex and agy manifests** (`implement`): `.codex-plugin/` +
   `.agents/plugins/marketplace.json` per plugin repo that carries skills (`agent-hub`, `telaf`, `aspice`), validated by
   each tool's own validator; tests check the manifests point at the existing `skills/`.
3. **`agent-hub hub install|status` command** (`implement-hard`): installs/updates the marketplace's plugins into
   codex, agy and opencode with their official commands (never by hand-editing their config), reports per host what is
   installed and which version; dry-run by default, `--apply` to change; scoped to the user's tools only.
4. **Worker briefs mention available skills** (`implement`): the brief footer names the skills relevant to the task
   (e.g. TDD, systematic debugging, telaf) so headless workers load them; measured by a skill-test run per host.
5. **Adopt from agent-skills** (`implement`, small): (a) an anti-rationalization table in `brief-template.md` and the
   principle skills ("I know the bug, I'll just fix it" → "reproduce first"…); (b) a debugging playbook
   reproduce → localize → reduce → fix → guard, used when a task is a bug of unknown cause; (c) optionally install
   agent-skills itself into every worker after reviewing its license and content.
6. **agy review adapter fix** (`implement`, from follow-ups): the review preamble tells agy it has no shell (read
   `diff.patch` and files only); "permission auto-denied" → `failed` with a reason, not `empty`; a smoke review on
   agy must return a verdict.

### Then phase C

A C++ fixture for the gwen bench (CMake + gtest, six tasks with hidden tests) before C++ tasks go to gwen (D-077).

## How to start the new session

1. Restart Claude Code (loads plugin 0.8.0), open `~/Workspaces/tools/agent-hub`.
2. Prompt: *"Đọc docs/next-phase.md và docs/overview.md, bắt đầu Phase B từ task 1 (spike)."*
3. The director follows the `agent-hub:orchestrate` skill; brainstorm/spec for B goes to
   `docs/superpowers/specs/`, decisions to `docs/decisions.md` (from D-083).
