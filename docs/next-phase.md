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
